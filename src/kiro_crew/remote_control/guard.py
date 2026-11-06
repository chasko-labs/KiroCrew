"""Keep the running relay in step with the Remote Control setting and the sign-in.

``tunnel.relay_enabled`` picks the relay client at boot, so the field is marked
restart-only. Turning Remote Control OFF must not wait for a restart, and neither
may a sign-in that stops being allowed. :class:`RemoteControlGuard` runs in the
gateway beside the tunnel manager and, every :data:`POLL_SECONDS`:

* stops the relay through :meth:`TunnelManager.stop` whenever it is running but
  the setting is off or the sign-in is refused (level-triggered, so a stop that
  failed is retried).

It never starts the relay. Turning Remote Control on, or a sign-in that becomes
allowed again, takes a gateway restart, as ``restart=True`` on the field says.
The guard does nothing when the relay was not selected at boot.

The sign-in is re-read only when the vault's sign-in or sign-out marker moves
(:func:`~kiro_crew.remote_control.policy.sign_in_generation`); a token refresh
moves neither. Every file read runs off the event loop.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import contextmanager
from typing import Any, Callable, Iterator, NamedTuple, Optional

from kiro_crew.remote_control.policy import (
    Decision,
    audit_refusal,
    current_decision_with_generation,
    sign_in_generation,
)

logger = logging.getLogger(__name__)

POLL_SECONDS = 2.0

#: The longest a one-click off waits for the relay's open HTTP requests to finish
#: before it stops the relay anyway. The off request itself may have come through the
#: relay, and its answer must reach the browser before the relay closes.
DRAIN_SECONDS = 5.0
_DRAIN_POLL_SECONDS = 0.05

#: How many times one tick redoes a sign-in read whose markers moved under it.
_SETTLE_ATTEMPTS = 2


class _Probe(NamedTuple):
    """A sign-in read made outside the lock, with the markers it was made under."""

    decision: Decision
    #: The markers the vault read reported with the verdict (the cache key).
    locked: tuple[str, ...]
    #: The markers read just before it, to tell whether a sign-in landed during it.
    before: tuple[str, ...]


#: ``ensure_available()`` answers this when no relay client is running.
_NOT_RUNNING = "disabled"
#: ...and this only while the client is connected to the relay.
_CONNECTED = "connected"


def _relay_http_idle(provider: Any) -> bool:
    """Whether no relayed HTTP request is still open on the relay client.

    Reads the relay client's connections through their ``http_streams_idle`` event,
    which a connection clears when a request arrives and sets once every request's
    answer has gone back to the relay. A provider without a running client is idle.
    """
    client = getattr(provider, "_client", None)
    for conn in list(getattr(client, "_live", None) or ()):
        idle = getattr(conn, "http_streams_idle", None)
        if idle is not None and not idle.is_set():
            return False
    return True


class RemoteControlGuard:
    """Stops the relay whenever it is not on and allowed; never starts it."""

    def __init__(
        self,
        manager: Any,
        provider: Any,
        *,
        requested: bool = True,
        decide: Optional[Callable[[], tuple[Decision, tuple[str, ...]]]] = None,
        generation: Optional[Callable[[], tuple[str, ...]]] = None,
        poll_seconds: float = POLL_SECONDS,
        idle: Optional[Callable[[], bool]] = None,
        drain_seconds: float = DRAIN_SECONDS,
    ) -> None:
        self._manager = manager
        self._provider = provider
        #: ``tunnel.relay_enabled`` as the live config watcher last applied it
        #: (:meth:`on_config`). True at install: the guard exists only when the
        #: relay was selected at boot.
        self._requested = requested
        self._decide = decide or (lambda: current_decision_with_generation(team=_team(provider)))
        self._generation = generation or sign_in_generation
        self._poll = poll_seconds
        self._task: Optional[asyncio.Task[None]] = None
        self._lock = asyncio.Lock()
        self._gen: Optional[tuple[str, ...]] = None
        self._decision: Optional[Decision] = None
        self._idle = idle or (lambda: _relay_http_idle(provider))
        self._drain = drain_seconds
        #: The pending :meth:`stop_after_drain` task. While it runs, the poll leaves
        #: the relay alone, so it cannot close the connection under an open answer.
        self._deferred: Optional[asyncio.Task[None]] = None
        #: True from :meth:`stop_after_drain` until its drain ends (the apply after
        #: it is still :attr:`_deferred`, so :meth:`stop` can cancel it).
        self._draining = False
        self._stop_reservations = 0
        self._subscription: Any = None

    @property
    def decision(self) -> Optional[Decision]:
        return self._decision

    async def _probe(self) -> Optional[_Probe]:
        """Read the sign-in if the cached verdict is stale. Runs OUTSIDE the lock.

        The read may be ``kiro-cli whoami`` (up to 15 s), so holding the lock across
        it would make a one-click off wait for it. Returns ``None`` when the cache is
        fresh. Nothing is cached here: :meth:`_settle` commits the result under the
        lock, and only if the markers did not move while the read ran.
        """
        before = await asyncio.to_thread(self._generation)
        if self._decision is not None and before == self._gen:
            return None
        decision, locked = await asyncio.to_thread(self._decide)
        return _Probe(decision, locked, before)

    async def _settle(self, probe: Optional[_Probe]) -> Optional[Decision]:
        """Under the lock: the verdict to act on, or ``None`` to probe again.

        The cheap marker read decides whether *probe* still describes the sign-in.
        A verdict is cached under the markers the vault read reported WITH it, so a
        sign-in landing between the marker read and the vault read is seen on the
        next tick; a sign-in or sign-out landing while the probe ran discards it.
        """
        now = await asyncio.to_thread(self._generation)
        if probe is None:
            if self._decision is not None and now == self._gen:
                return self._decision
            return None
        if now != probe.before:
            return None
        self._decision, self._gen = probe.decision, probe.locked
        return probe.decision

    def on_config(self, change: Any) -> None:
        """The ``tunnel`` applier on the live config watcher: adopt ``relay_enabled``.

        The off edge of ``tunnel.relay_enabled`` is deliberately live; the on edge
        is boot-only. This applier only records the value, and the guard acts on it
        only to stop the relay (:meth:`_act`), never to start it, so ``restart=True``
        on the field stays true for turning it on.
        """
        self._requested = change.new.tunnel.relay_enabled is True

    async def running(self) -> bool:
        """Whether a relay client exists now, connected, starting or failed.

        The stop decision reads this, so a client that ended in error is still
        cleaned up. Whether other devices can reach this Crew is :meth:`connected`.
        """
        state = await self._provider.ensure_available(install=False)
        return state != _NOT_RUNNING

    async def connected(self) -> bool:
        """Whether the relay client is connected now, so other devices can reach Crew."""
        state = await self._provider.ensure_available(install=False)
        return state == _CONNECTED

    async def tick(self) -> None:
        """Read the setting and the sign-in once and bring the relay in line.

        The sign-in read (on the kiro-cli route a `kiro-cli whoami` of up to 15 s)
        runs before the lock is taken, so it never delays another tick, and in
        particular never delays the stop a one-click off asks for. Under the lock
        the setting is read again and the probe is used only if the markers held
        still; otherwise the probe is redone, at most :data:`_SETTLE_ATTEMPTS`
        times, after which this tick leaves the relay as it is for the next poll.
        """
        for _ in range(_SETTLE_ATTEMPTS):
            probe = await self._probe() if self._requested else None
            async with self._lock:
                requested = self._requested
                # Off decides on its own, without a sign-in verdict.
                decision = await self._settle(probe) if requested else None
                if requested and decision is None:
                    continue
                await self._act(requested, decision)
            return
        logger.info("Remote Control guard: the sign-in kept changing; deciding on the next poll")

    async def _act(self, requested: bool, decision: Optional[Decision]) -> None:
        """Stop the relay if *requested* and *decision* say it must not run. Holds the lock.

        Never starts it: turning it on, or a sign-in allowed again, takes a restart.
        """
        effective = requested and decision is not None and decision.ok
        if effective or not await self.running():
            return
        if self.stop_pending:
            return  # stop_after_drain stops it once the open answers are out
        refused = decision if requested and decision is not None else None
        logger.info(
            "Remote Control stopping the relay (%s)",
            refused.reason if refused is not None else "turned off",
        )
        if refused is not None:
            await asyncio.to_thread(audit_refusal, "tunnel.stop_enforced", refused)
        await self._manager.stop()

    async def apply(self) -> None:
        """Act on a write this process just made, without waiting for the poll.

        The caller has the live config watcher reload first (``refresh_now``), so
        :meth:`on_config` has adopted whichever write landed last. Forces a fresh
        read of the sign-in too, so a sign-in finished a moment ago is not judged
        by the cached verdict.
        """
        self._gen = None
        await self.tick()

    @contextmanager
    def reserve_stop(self) -> Iterator[None]:
        """Keep polling from closing an off request while its write is in flight.

        Reserve before persistence can publish false. On success the caller starts
        the drain before releasing; on failure polling resumes. Count overlapping
        writes so one request cannot release another request's reservation.
        """
        self._stop_reservations += 1
        try:
            yield
        finally:
            self._stop_reservations -= 1

    @property
    def stop_pending(self) -> bool:
        """Whether an off write or its response drain is still pending."""
        return self._stop_reservations > 0 or self._draining

    def stop_after_drain(self) -> None:
        """Act on a turn-off this process just wrote, after its answer is sent.

        A one-click off can arrive through the relay itself, and stopping the relay
        closes the connection that carries the answer. So this returns at once and
        a background task waits until the relay has no open HTTP request (the off
        request's own answer included), for at most :data:`DRAIN_SECONDS`, then runs
        :meth:`apply`. A request that did not come through the relay leaves nothing
        open, so the stop follows straight away.
        """
        if self._deferred is not None and not self._deferred.done():
            return
        self._draining = True
        self._deferred = asyncio.get_running_loop().create_task(
            self._drain_then_apply(), name="remote-control-off"
        )

    async def _drain_then_apply(self) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(self._drain, 0.0)
        # Yield once first, so the handler that scheduled this returns its answer
        # before the idle check can see the relay idle.
        await asyncio.sleep(0)
        while loop.time() < deadline:
            try:
                # On the loop, not a thread: it reads asyncio events, and is cheap.
                if self._idle():
                    break
            except Exception:  # noqa: BLE001 - an unreadable idle state ends the wait
                logger.debug("Remote Control drain check failed", exc_info=True)
                break
            await asyncio.sleep(_DRAIN_POLL_SECONDS)
        else:
            logger.info("Remote Control off: open requests outlived the drain; stopping anyway")
        # The drain ends before apply(), so its tick is not skipped as "stop pending".
        # The task handle is kept until apply() returns, so stop() still cancels a
        # start that apply() is waiting on.
        self._draining = False
        try:
            await self.apply()
        except Exception:  # noqa: BLE001 - the poll retries a stop that failed
            logger.warning("Remote Control off: stopping the relay failed", exc_info=True)

    async def _loop(self) -> None:
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - one bad tick must not end the guard
                logger.warning("Remote Control guard tick failed", exc_info=True)
            await asyncio.sleep(self._poll)

    def subscribe(self) -> None:
        """Register :meth:`on_config` on the live config watcher, once.

        Section-wide (``"tunnel"``): ``tunnel.relay_enabled`` is restart-marked, and
        ``config/live.py`` refuses a leaf applier there. The off edge is deliberately
        live and the on edge stays boot-only: this applier is read only to stop.
        """
        if self._subscription is None:
            from kiro_crew.config import live

            self._subscription = live.subscribe(
                "tunnel", callback=self.on_config, name="remote-control-guard"
            )
            # An off the watcher adopted before this guard was installed (during the
            # boot start's sign-in read) reaches it now, not only on the next change.
            live.watch().replay(self._subscription)

    def start(self) -> None:
        self.subscribe()
        if self._task is None or self._task.done():
            self._task = asyncio.get_running_loop().create_task(
                self._loop(), name="remote-control-guard"
            )

    async def stop(self) -> None:
        for task in (self._task, self._deferred):
            if task is None:
                continue
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._task = None
        self._deferred = None
        self._draining = False
        if self._subscription is not None:
            self._subscription.cancel()
            self._subscription = None


#: The gateway's guard, set when the relay was selected at boot. One per process.
_guard: Optional[RemoteControlGuard] = None


def install(manager: Any, provider: Any) -> RemoteControlGuard:
    """Create, start and publish the gateway's guard. Called once, on the loop."""
    global _guard
    guard = RemoteControlGuard(manager, provider)
    guard.start()
    _guard = guard
    return guard


def _team(provider: Any) -> Optional[bool]:
    """Whether the running relay leases as a team Crew, from its own fixed settings.

    A config edit while the relay runs does not change how it leases, so it must
    not change which rule judges it. ``None`` (read ``config.json``) only for a
    provider with no relay settings.
    """
    settings = getattr(provider, "settings", None)
    team = getattr(settings, "is_team_crew", None)
    return team if isinstance(team, bool) else None


def install_for_relay(manager: Any, provider: Any) -> Optional[RemoteControlGuard]:
    """:func:`install`, but only when *provider* is the relay client's provider.

    An edition's own tunnel provider is not Remote Control and is left alone.
    """
    from kiro_crew.relay_client.provider import RelayTunnelProvider

    if not isinstance(provider, RelayTunnelProvider):
        return None
    return install(manager, provider)


def current_guard() -> Optional[RemoteControlGuard]:
    return _guard


async def uninstall() -> None:
    """Stop the guard first at shutdown, so it cannot restart what shutdown stops."""
    global _guard
    guard, _guard = _guard, None
    if guard is not None:
        await guard.stop()
