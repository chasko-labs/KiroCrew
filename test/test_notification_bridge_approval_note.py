"""The pending-approval note (RFC notification-bridge-actions, phase B4p).

Pins that the note is published only with the preview on, exactly once per open
approval, carrying the producer meta key; that it reaches the bridge and never the
dashboard bell; that the Slack sink reuses a card already in a linked Slack thread;
and that the permission prompt hands the note over only while the approval is open.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
from types import SimpleNamespace
from typing import Any

import pytest

from kiro_crew.config.sections_dashboard import FEATURE_PREVIEW_FLAGS
from kiro_crew.dashboard.chat_turn import permission_prompt
from kiro_crew.notifications import bridge_approval_note as note_mod
from kiro_crew.notifications.bridge import BridgeTransportUnavailable
from kiro_crew.notifications.bridge_slack import SlackBridgeSink


def _cfg(on: bool) -> Any:
    return SimpleNamespace(dashboard=SimpleNamespace(feature_previews={note_mod.PREVIEW: on}))


class _State:
    """Stands in for DashboardState: records what reaches the bridge and the bell."""

    def __init__(self) -> None:
        self.notification_bridge = object()
        self.bridged: list[dict[str, Any]] = []
        self.bell: list[Any] = []

    def _bridge_note(self, note: dict[str, Any], publisher: str = "") -> None:
        self.bridged.append(note)

    def notify(self, *args: Any, **kwargs: Any) -> None:
        self.bell.append((args, kwargs))

    def _deliver_note(self, note: dict[str, Any]) -> None:
        self.bell.append(note)


def _publish(state: _State, *, on: bool = True, slack_card: bool = False):
    return note_mod.publish_pending_approval(
        state,
        _cfg(on),
        slot_key="chat-1",
        session_key="dashboard:chat-1",
        request_id="7",
        tool_title="execute_bash",
        session_title="Release notes",
        slack_card=slack_card,
    )


def test_the_preview_is_a_known_flag_and_off_by_default():
    assert note_mod.PREVIEW in FEATURE_PREVIEW_FLAGS
    assert not note_mod.enabled(SimpleNamespace(dashboard=SimpleNamespace(feature_previews={})))
    assert not note_mod.enabled(SimpleNamespace())


def test_with_the_preview_off_no_note_is_published():
    state = _State()
    assert _publish(state, on=False) is None
    assert state.bridged == [] and state.bell == []


def test_with_the_preview_on_one_note_reaches_the_bridge_and_not_the_bell():
    state = _State()
    note = _publish(state)
    assert state.bridged == [note]
    assert state.bell == [], "the pending note must never ring the dashboard bell"
    assert note["channel"] == "system.approval"
    assert note[note_mod.PRODUCER_KEY] == note_mod.PRODUCER
    assert note[note_mod.REQUEST_KEY] == "7"
    assert (note["slot"], note["session_key"]) == ("chat-1", "dashboard:chat-1")
    assert note_mod.is_pending_approval_note(note)
    assert note_mod.SLACK_CARD_KEY not in note
    assert "execute_bash" in note["body"] and "Release notes" in note["body"]
    assert "actions" not in note, "the note carries no buttons yet"


def test_a_config_pending_review_note_is_not_a_tool_approval():
    assert not note_mod.is_pending_approval_note({"channel": "system.approval"})


def test_a_gateway_without_a_bridge_publishes_nothing():
    state = _State()
    state.notification_bridge = None
    assert _publish(state) is None
    assert state.bridged == []


def test_a_failing_bridge_never_reaches_the_prompt():
    state = _State()

    def _boom(note: dict[str, Any], publisher: str = "") -> None:
        raise RuntimeError("bridge down")

    state._bridge_note = _boom  # type: ignore[method-assign]
    assert _publish(state) is None


class _FakeSlack:
    def __init__(self) -> None:
        self.posts: list[tuple[str, str]] = []

    async def open_dm(self, user_id: str) -> str | None:
        return "D-OWNER"

    async def post_message(self, channel: str, text: str) -> str:
        self.posts.append((channel, text))
        return "1.0"


@pytest.mark.asyncio
@pytest.mark.parametrize("slack_card", [True, False])
async def test_the_slack_sink_reuses_a_card_already_in_slack(slack_card):
    state = _State()
    note = _publish(state, slack_card=slack_card)
    slack = _FakeSlack()
    sink = SlackBridgeSink(SimpleNamespace(slack_client=slack, owner_id="U-OWNER"))
    if slack_card:
        # A skip, so the dispatcher audits it as skipped and keeps the token.
        with pytest.raises(BridgeTransportUnavailable):
            await sink.deliver(note)
        assert slack.posts == [], "the linked card is the one to answer; no second copy"
    else:
        await sink.deliver(note)
        assert len(slack.posts) == 1


@pytest.fixture()
def handed(monkeypatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr("kiro_crew.config.KiroCrewConfig.load", classmethod(lambda cls: _cfg(True)))
    monkeypatch.setattr(
        note_mod, "publish_pending_approval", lambda state, cfg, **kw: calls.append(kw)
    )
    return calls


_SLOT = SimpleNamespace(key="chat-1", display_title="Release notes")
_EVENT = SimpleNamespace(request_id=7, tool_name="execute_bash")


@pytest.mark.asyncio
async def test_an_approval_answered_while_the_config_loads_sends_nothing(handed):
    fut = asyncio.get_running_loop().create_future()
    fut.set_result("approved")
    await permission_prompt._bridge_pending_approval(
        _State(), _SLOT, "dashboard:chat-1", _EVENT, fut, None
    )
    assert handed == []


@pytest.mark.asyncio
async def test_the_hand_off_runs_beside_the_prompt_and_is_held(handed):
    """Scheduled, never awaited: no new await sits between registration and cleanup."""
    state = _State()
    state._background_tasks = set()  # type: ignore[attr-defined]
    fut = asyncio.get_running_loop().create_future()
    permission_prompt._schedule_bridge_pending_approval(
        state, _SLOT, "dashboard:chat-1", _EVENT, fut, None
    )
    assert handed == [], "the prompt returns before the hand-off runs"
    [task] = state._background_tasks  # type: ignore[attr-defined]
    await asyncio.wait_for(task, timeout=5)
    assert len(handed) == 1
    assert state._background_tasks == set()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_a_failing_hand_off_is_held_and_logged(monkeypatch, caplog):
    """No background set on the state: the module holds the task, and a raise is logged."""

    def _boom(state, cfg, **kw):
        raise RuntimeError("bridge exploded")

    monkeypatch.setattr("kiro_crew.config.KiroCrewConfig.load", classmethod(lambda cls: _cfg(True)))
    monkeypatch.setattr(note_mod, "publish_pending_approval", _boom)
    fut = asyncio.get_running_loop().create_future()
    held = permission_prompt._BRIDGE_HANDOFF_TASKS
    with caplog.at_level("WARNING", logger=permission_prompt.__name__):
        permission_prompt._schedule_bridge_pending_approval(
            _State(), _SLOT, "dashboard:chat-1", _EVENT, fut, None
        )
        [task] = held
        await asyncio.wait({task}, timeout=5)
        await asyncio.sleep(0)  # flake-ok: let the done callbacks run, nothing is timed
    assert held == set()
    assert "pending-approval bridge hand-off failed" in caplog.text


@pytest.mark.asyncio
async def test_the_prompt_hands_over_the_open_approval(handed):
    fut = asyncio.get_running_loop().create_future()
    await permission_prompt._bridge_pending_approval(
        _State(), _SLOT, "dashboard:chat-1", _EVENT, fut, "1700000000.0001"
    )
    calls = handed
    assert calls == [
        {
            "slot_key": "chat-1",
            "session_key": "dashboard:chat-1",
            "request_id": "7",
            "tool_title": "execute_bash",
            "session_title": "Release notes",
            "slack_card": True,
        }
    ]


def test_the_prompt_schedules_the_hand_off_only_while_the_approval_is_open():
    """Behind ``if not fut.done()``, and a plain call: the prompt never awaits it."""
    tree = ast.parse(inspect.getsource(permission_prompt))
    guarded = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.If)
        and ast.unparse(node.test) == "not fut.done()"
        and "_schedule_bridge_pending_approval" in ast.unparse(node)
    ]
    assert len(guarded) == 1
    awaited = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Await) and "_bridge_pending_approval" in ast.unparse(node)
    ]
    assert awaited == []
