"""In-product Muse subscription login: ``/api/muse-login/*``.

The gateway host (a headless cloud box) cannot open a browser, but the
owner can: ``muse login`` is a device flow — it prints a code plus an
``https://auth.meta.com/oauth/device/`` URL and waits for the approval to
land in the browser. These routes let the owner drive that flow from the
``/crew`` backends panel instead of an SSH session:

* ``POST /api/muse-login/start`` — owner-only. Resolves the ``muse``
  binary through the harness's own resolver, spawns ``muse login`` with
  piped output, and answers with the ``{code, url}`` the owner approves.
* ``GET /api/muse-login/status`` — owner-only. ``pending`` while the
  process waits, ``ready`` once it exits 0 with ``~/.muse/auth.json``
  present, ``failed``/``expired`` otherwise (with a short tail of the
  process output as the reason, never credential contents).
* ``POST /api/muse-login/cancel`` — owner-only. Kills a pending login.

Single-flight: one login at a time per gateway process. A second start
while one is pending returns the pending one (same code, same URL),
because a second ``muse login`` would print a second code and orphan the
first approval. Terminal states linger until the next start so the panel
can render the outcome; only ``pending`` blocks.

The credential file is NEVER read here — only its path is fenced (see
``agent_sdk.host_auth`` for the declaration) and its existence checked.
The login writes it; the harness reads it. Nothing in these routes learns
a token, and the process output returned on failure is truncated to a few
hundred characters so a verbose adapter cannot smuggle the file through
stdout.

Error bodies are dict literals with a ``code`` (the error-code contract
ratchet), and every mutating route is owner-gated (the owner-gate
invariant walk).
"""

from __future__ import annotations

import asyncio
import logging
import re
import threading
import time
from pathlib import Path
from typing import Any

from aiohttp import web

from kiro_crew.dashboard.handlers._shared import require_owner_dashboard_request

logger = logging.getLogger(__name__)

#: How long a pending approval may wait before it is reaped (device codes
#: themselves expire server-side; ten minutes bounds the orphaned process).
LOGIN_TIMEOUT_SECONDS = 600.0
#: Failure reason length cap: enough for the adapter's last line, never a file.
_FAILURE_TAIL_CHARS = 400

#: ``muse login`` prints the device URL with the code embedded, then asks to
#: confirm the bare code matches (captured live against muse 1.4.15).
_DEVICE_URL_RE = re.compile(
    r"https://auth\.meta\.com/oauth/device/\?code=([A-Za-z0-9-]+)"
)
_CONFIRM_CODE_RE = re.compile(
    r"confirm this code matches:\s*([A-Za-z0-9-]+)", re.IGNORECASE
)

_STATES = frozenset({"none", "pending", "ready", "failed", "expired", "cancelled"})


class _LoginSession:
    """One ``muse login`` child and what it has printed so far."""

    def __init__(self, proc: asyncio.subprocess.Process) -> None:
        self.proc = proc
        self.output = ""
        self.code = ""
        self.url = ""
        self.state = "pending"
        self.started_at = time.time()
        self.detail = ""


_lock = threading.Lock()
_session: _LoginSession | None = None


def parse_login_output(text: str) -> tuple[str, str]:
    """Extract ``(code, url)`` from ``muse login`` output, or ``("", "")``.

    Pure (no process, no filesystem) so tests drive it with captured
    transcripts. The URL carries the code; the confirm line repeats it —
    both must agree, otherwise the panel would show a URL approving one
    grant beside a code naming another.
    """
    url_match = _DEVICE_URL_RE.search(text)
    code_match = _CONFIRM_CODE_RE.search(text)
    if url_match is None or code_match is None:
        return "", ""
    code = code_match.group(1)
    if url_match.group(1) != code:
        return "", ""
    return code, url_match.group(0)


def muse_auth_path() -> Path:
    """The credential file ``muse login`` writes (existence only, never read)."""
    return Path.home() / ".muse" / "auth.json"


def _snapshot_locked() -> dict[str, Any]:
    """Snapshot; the caller holds :data:`_lock`."""
    if _session is None:
        return {"state": "none", "code": "", "url": "", "detail": ""}
    return {
        "state": _session.state,
        "code": _session.code if _session.state == "pending" else "",
        "url": _session.url if _session.state == "pending" else "",
        "detail": _session.detail,
    }


def _snapshot() -> dict[str, Any]:
    with _lock:
        return _snapshot_locked()


def _audit(operation: str, outcome: str, error: str = "") -> None:
    try:
        from kiro_crew.sel import sel as _sel

        _sel().log_api_access(
            caller="dashboard-owner",
            operation=operation,
            outcome=outcome,
            source="dashboard",
            resources="muse-login",
            error=error,
        )
    except Exception:  # pragma: no cover - audit must never change the outcome
        logger.debug("SEL audit for muse-login failed", exc_info=True)


async def _resolve_muse_binary() -> str | None:
    """The ``muse`` executable via the harness resolver (blocking: off-loop)."""
    import kiro_crew.acp.harness.muse as muse_harness

    binary, _searched = await asyncio.to_thread(muse_harness._resolve_muse_bin)
    return binary


def _expire_locked(session: _LoginSession) -> None:
    """Mark expired; the caller holds :data:`_lock`."""
    session.state = "expired"
    session.detail = "login timed out waiting for browser approval"


async def _watch_login(session: _LoginSession) -> None:
    """Pump the child's output, parse the device grant, reap on timeout/exit.

    Line-based, not read-until-EOF: ``muse login`` keeps stdout open for the
    whole approval wait, so EOF only arrives with the verdict. The grant
    (code + URL) prints in the first lines and must be visible while the
    process is still pending.
    """
    assert session.proc.stdout is not None
    loop = asyncio.get_running_loop()
    deadline = loop.time() + LOGIN_TIMEOUT_SECONDS
    try:
        while loop.time() < deadline:
            line = await asyncio.wait_for(
                session.proc.stdout.readline(), timeout=max(0.1, deadline - loop.time())
            )
            if not line:
                break
            text = line.decode("utf-8", errors="replace")
            with _lock:
                session.output += text
                code, url = parse_login_output(session.output)
                if code and url:
                    session.code = code
                    session.url = url
                    break
        else:
            raise asyncio.TimeoutError
    except asyncio.TimeoutError:
        try:
            session.proc.kill()
        except ProcessLookupError:
            pass
        with _lock:
            _expire_locked(session)
        _audit("muse_login", "expired", "login_timeout")
        return
    # The grant is on screen; now wait for the approval to land (or not).
    try:
        exit_code = await asyncio.wait_for(
            session.proc.wait(), timeout=max(0.1, deadline - loop.time())
        )
    except asyncio.TimeoutError:
        try:
            session.proc.kill()
        except ProcessLookupError:
            pass
        exit_code = await session.proc.wait()
        with _lock:
            _expire_locked(session)
        _audit("muse_login", "expired", "login_timeout")
        return
    with _lock:
        if exit_code == 0 and muse_auth_path().is_file():
            session.state = "ready"
            session.detail = ""
        elif exit_code == 0:
            session.state = "failed"
            session.detail = "muse login exited without writing its credential file"
        else:
            session.state = "failed"
            tail = session.output[-_FAILURE_TAIL_CHARS:]
            session.detail = f"muse login exited ({exit_code}): {tail.strip()}"
    _audit(
        "muse_login",
        session.state,
        "" if session.state == "ready" else "login_failed",
    )


async def api_muse_login_start(request: web.Request) -> web.Response:
    """POST /api/muse-login/start — begin (or rejoin) the device flow."""
    global _session
    owner_denied = await require_owner_dashboard_request(request, "muse_login.start")
    if owner_denied is not None:
        return owner_denied
    with _lock:
        if _session is not None and _session.state == "pending":
            return web.json_response({"ok": True, "rejoined": True, **_snapshot_locked()})
    binary = await _resolve_muse_binary()
    if not binary:
        _audit("muse_login", "denied", "muse_not_installed")
        return web.json_response(
            {"error": "muse is not installed on the gateway host", "code": "muse_not_installed"},
            status=409,
        )
    try:
        proc = await asyncio.create_subprocess_exec(
            binary,
            "login",
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except OSError as exc:
        _audit("muse_login", "denied", "muse_spawn_failed")
        return web.json_response(
            {"error": f"could not start muse login: {exc}", "code": "muse_spawn_failed"},
            status=500,
        )
    session = _LoginSession(proc)
    with _lock:
        _session = session
    asyncio.get_running_loop().create_task(_watch_login(session))
    # The grant prints within a second; wait briefly so the first answer
    # already carries the code instead of forcing a poll round-trip. The
    # session stays pending either way.
    for _ in range(20):
        await asyncio.sleep(0.25)
        with _lock:
            if session.code and session.url:
                break
            if session.state != "pending":
                break
    with _lock:
        snap = _snapshot_locked()
    _audit("muse_login", "success")
    return web.json_response({"ok": True, "rejoined": False, **snap})


async def api_muse_login_status(request: web.Request) -> web.Response:
    """GET /api/muse-login/status — the flow's current state (owner-only)."""
    owner_denied = await require_owner_dashboard_request(request, "muse_login.read")
    if owner_denied is not None:
        return owner_denied
    return web.json_response(_snapshot())


async def api_muse_login_cancel(request: web.Request) -> web.Response:
    """POST /api/muse-login/cancel — kill a pending login (owner-only)."""
    owner_denied = await require_owner_dashboard_request(request, "muse_login.cancel")
    if owner_denied is not None:
        return owner_denied
    with _lock:
        session = _session
        if session is None or session.state != "pending":
            return web.json_response(
                {"error": "no pending muse login", "code": "muse_login_not_pending"},
                status=409,
            )
    try:
        session.proc.kill()
    except ProcessLookupError:
        pass
    try:
        await asyncio.wait_for(session.proc.wait(), timeout=10)
    except asyncio.TimeoutError:
        pass
    with _lock:
        if session.state == "pending":
            session.state = "cancelled"
            session.detail = "cancelled by the owner"
    _audit("muse_login", "cancelled")
    return web.json_response({"ok": True, **_snapshot()})
