"""A user-typed ``/compact`` holds the chat turn gate through its deferred wait.

On a shared kiro-cli chat runtime (``chat_runtime_sharing`` preview) a
``/compact`` whose terminal arrives late runs its ``wait_for_compaction`` receipt
AFTER the ``stream_command`` turn has released the per-process chat turn gate. In
that window a co-tenant chat can start a turn and receive this session's
OWNERLESS compaction terminal (the status names no issuer and is fanned
``runtime_global`` to every co-tenant runner), clearing the co-tenant's
already-streamed answer. ``chat_runner._run_chat`` therefore enters
``runtime_ownership.chat_turn_gate_hold(client)`` onto a turn-scoped exit stack
before the dispatch gates of every turn and closes it in the turn's ``finally``,
so the gate is held from the command turn through ``_turn_compact_command``'s
deferred wait. ``CompactionCoordinator`` holds the same gate for automatic ones.

The behavioural test drives the real hold and a real ``AcpSessionProvider`` gate
and proves a co-tenant cannot start a turn while the hold is in place. The source
contract keeps the hold at the call site.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect

import pytest

from kiro_crew.dashboard import chat_runner
from kiro_crew.runtime_ownership import chat_turn_gate_hold


def _gate_backed_provider(shared: bool = True):
    """An AcpSessionProvider whose per-process gate is a real asyncio lock."""
    from unittest.mock import MagicMock

    from kiro_crew.acp.session_provider import AcpSessionProvider

    gate_lock = asyncio.Lock()

    @contextlib.asynccontextmanager
    async def chat_turn_gate():
        async with gate_lock:
            yield

    runtime = MagicMock()
    runtime.chat_turn_gate = chat_turn_gate
    provider = AcpSessionProvider(MagicMock(), runtime, shared_runtime=shared)
    return provider, gate_lock


@pytest.mark.asyncio
async def test_a_co_tenant_cannot_start_a_turn_during_the_deferred_wait() -> None:
    provider, gate_lock = _gate_backed_provider()

    wait_entered = asyncio.Event()
    release_wait = asyncio.Event()

    async def deferred_compact():
        # The call-site shape: the hold is entered, the command turn and the
        # deferred receipt run inside it (the inner gate re-entry is a no-op).
        async with chat_turn_gate_hold(provider):
            async with provider._chat_turn_gate():
                pass
            wait_entered.set()
            await release_wait.wait()
            return "completed"

    compact_task = asyncio.create_task(deferred_compact())
    await asyncio.wait_for(
        wait_entered.wait(), timeout=30.0
    )  # flake-ok: awaits the Event the deferred-compact task sets on entry, bounded only to name a hang
    assert gate_lock.locked()

    cotenant_started = asyncio.Event()

    async def cotenant_turn():
        async with provider._chat_turn_gate():
            cotenant_started.set()

    # A second task is a different holder, so it contends for the lock.
    cotenant_task = asyncio.create_task(cotenant_turn())
    for _ in range(50):
        await asyncio.sleep(0)
    assert not cotenant_started.is_set(), "a co-tenant started a turn during the deferred wait"

    release_wait.set()
    assert (
        await asyncio.wait_for(compact_task, timeout=30.0) == "completed"
    )  # flake-ok: awaits the task release_wait.set() just unblocked, bounded only to name a hang
    await asyncio.wait_for(
        cotenant_task, timeout=30.0
    )  # flake-ok: awaits the co-tenant that proceeds once the gate frees, bounded only to name a hang
    assert cotenant_started.is_set()


@pytest.mark.asyncio
async def test_an_unshared_provider_gets_a_null_hold() -> None:
    # The preview off: the provider is not shared, so the hold takes no lock and
    # the /compact turn runs exactly as before.
    provider, gate_lock = _gate_backed_provider(shared=False)
    async with chat_turn_gate_hold(provider):
        assert not gate_lock.locked()

    class _NoHook:
        pass

    async with chat_turn_gate_hold(_NoHook()):
        pass  # does not raise


def test_the_gate_is_entered_before_the_dispatch_gates() -> None:
    """Every turn takes the hold before ``_turn_dispatch_gates``.

    Before the dispatch gates, so a Stop or a close that lands while this turn
    waits for a co-tenant's turn is seen by their checks;
    and so before ``stream_command``, which keeps a /compact's hold across its
    deferred wait.
    """
    src = inspect.getsource(chat_runner._run_chat)
    hold = src.index("await _turn_gate.enter_async_context(chat_turn_gate_hold(client))")
    assert hold < src.index("_flow = await _turn_dispatch_gates(turn)")
    assert hold < src.index("event_stream = client.stream_command(message)")
    # The deferred wait runs inside the same try, before the finally that closes it.
    assert src.index("await _turn_compact_command(turn)") < src.index("await _turn_gate.aclose()")
