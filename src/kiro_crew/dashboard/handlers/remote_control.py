"""``/api/remote-control``: the personal Remote Control setting, its notice, one-click off.

``GET`` reports what the setting asks for, whether the Kiro sign-in allows it, the
notice to show when it does not, and whether the relay is connected now.

``PUT {"enabled": false}`` is one-click off: it writes an explicit ``false`` (so it
stays off across a restart), answers, and then stops a running relay once the
relay's open requests (this one's answer included) are done, within
:data:`~kiro_crew.remote_control.guard.DRAIN_SECONDS`. Turning off never needs a
restart. The dashboard cannot turn Remote Control on: ``{"enabled": true}`` is
refused with 400 and writes nothing; that is the terminal command's job
(``kirocrew remote-control on``).

Both verbs are owner-only.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import nullcontext
from typing import Any

from aiohttp import web

from kiro_crew.config import live
from kiro_crew.dashboard.handlers._shared import require_owner_dashboard_request
from kiro_crew.remote_control import guard as guard_mod
from kiro_crew.remote_control import policy, setting
from kiro_crew.sel import sel

logger = logging.getLogger(__name__)

_OPERATION = "remote_control"


async def _view() -> dict[str, Any]:
    view = await asyncio.to_thread(setting.read)
    guard = guard_mod.current_guard()
    connected = False
    if guard is not None:
        try:
            connected = await guard.connected()
        except Exception:  # noqa: BLE001 - a status read must not fail the page
            logger.debug("Remote Control connection probe failed", exc_info=True)
    return {
        "requested": view.requested,
        "allowed": view.decision.ok,
        "reason": view.decision.reason,
        "notice": view.decision.notice,
        "effective": view.effective,
        # Connected to the relay, so other devices can reach this Crew. A client
        # that ended in error is not.
        "running": connected,
    }


async def api_remote_control_get(request: web.Request) -> web.Response:
    """GET /api/remote-control."""
    denied = await require_owner_dashboard_request(request, _OPERATION)
    if denied is not None:
        return denied
    return web.json_response(await _view())


def _audit(request: web.Request, outcome: str, resources: str) -> None:
    sel().log_api_access(
        caller=str(request.get("user") or "dashboard"),
        operation=_OPERATION,
        outcome=outcome,
        source="dashboard",
        resources=resources,
    )


async def api_remote_control_put(request: web.Request) -> web.Response:
    """PUT /api/remote-control ``{"enabled": false}``: one-click off, nothing else."""
    denied = await require_owner_dashboard_request(request, _OPERATION)
    if denied is not None:
        return denied
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON", "code": "invalid_json"}, status=400)
    enabled = body.get("enabled") if isinstance(body, dict) else None
    if enabled is not False:
        return web.json_response(
            {
                "error": "the dashboard can only turn Remote Control off: send enabled=false",
                "code": "invalid_field_value",
            },
            status=400,
        )

    guard = guard_mod.current_guard()
    stopping = False
    # The base write can become visible before an overlay write finishes.
    # Reserve the stop before persistence yields, then drain only on success.
    with guard.reserve_stop() if guard is not None else nullcontext():
        await asyncio.to_thread(setting.turn_off)
        if guard is not None:
            await live.watch().refresh_now()
            guard.stop_after_drain()
            stopping = True
    _audit(request, "allowed", "enabled=false")

    if not stopping:
        return web.json_response(await _view())
    # The drain stops the relay within DRAIN_SECONDS, and this answer may be riding
    # it. So nothing here may wait on a fresh sign-in read (on the kiro-cli route a
    # `kiro-cli whoami` of up to 15 s): the answer is the stored off plus the
    # guard's cached verdict, and the card re-reads the live state with GET.
    view = setting.SettingView(
        requested=await asyncio.to_thread(setting.requested),
        decision=await _off_decision(guard),
    )
    return web.json_response(
        {
            "requested": view.requested,
            "allowed": view.decision.ok,
            "reason": view.decision.reason,
            "notice": view.decision.notice,
            "effective": view.effective,
            # The stop is committed and bounded.
            "running": False,
        }
    )


#: How long the off answer waits for a sign-in read the guard has not cached yet,
#: well inside the relay drain so the answer is out before the relay closes.
OFF_DECISION_WAIT_SECONDS = 1.0


async def _off_decision(guard: Any) -> policy.Decision:
    """The guard's cached verdict; before its first read, one bounded fresh read."""
    cached = guard.decision
    if cached is not None:
        return cached
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(policy.current_decision), OFF_DECISION_WAIT_SECONDS
        )
    except asyncio.TimeoutError:
        return policy.Decision(False, policy.REASON_UNKNOWN_SIGN_IN)
