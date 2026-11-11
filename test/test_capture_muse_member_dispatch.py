"""``scripts/capture_muse_member_dispatch.py`` -- the parts that need no model.

The script's REASON for existing is a live drive, and a live drive needs the
adapter, the relay and a Muse subscription this suite has none of. What it can
hold is everything around the drive: the verdict read off synthetic frames,
the allow-once pick, the redaction, and the drive itself against a FAKE
adapter that mounts one tool, asks once, and completes -- which asserts that
this script, handed a harness that performs the round trip, says
DISPATCH_REACHED and writes the frames that show it.
"""

from __future__ import annotations

import importlib.util
import json
import pathlib
import sys

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPT_PATH = REPO_ROOT / "scripts" / "capture_muse_member_dispatch.py"


def _load():
    spec = importlib.util.spec_from_file_location(
        "capture_muse_member_dispatch", SCRIPT_PATH
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def cap():
    return _load()


FAKE_ADAPTER = """import json, sys
def send(obj):
    sys.stdout.write(json.dumps(obj) + chr(10))
    sys.stdout.flush()
def turn(prompt_id):
    send({"jsonrpc": "2.0", "method": "session/update", "params": {
        "sessionId": "sess-1", "update": {"sessionUpdate": "tool_call",
        "toolCallId": "call-1", "title": "mcp__stub__stub_ping",
        "rawInput": {"server": "stub", "tool": "stub_ping",
                     "arguments": {}}}}})
    send({"jsonrpc": "2.0", "id": 900, "method": "session/request_permission",
          "params": {"sessionId": "sess-1",
                     "toolCall": {"toolCallId": "call-1",
                                  "title": "mcp__stub__stub_ping"},
                     "options": [{"optionId": "allow-once-x",
                                  "kind": "allow_once",
                                  "name": "Allow once"}]}})
    answer = json.loads(sys.stdin.readline())
    ok = (answer.get("result", {}) or {}).get("outcome", {}) or {}
    assert ok.get("optionId") == "allow-once-x", answer
    send({"jsonrpc": "2.0", "method": "session/update", "params": {
        "sessionId": "sess-1", "update": {"sessionUpdate": "tool_call_update",
        "toolCallId": "call-1", "status": "completed",
        "rawOutput": {"result": "pong-probe"}}}})
    send({"jsonrpc": "2.0", "id": prompt_id,
          "result": {"stopReason": "end_turn"}})
for _line in sys.stdin:
    if not _line.strip():
        continue
    _req = json.loads(_line)
    _id, _method = _req.get("id"), _req.get("method")
    if _method == "initialize":
        send({"jsonrpc": "2.0", "id": _id, "result": {
            "protocolVersion": 1,
            "agentInfo": {"name": "fake-acp", "version": "9.9"}}})
    elif _method == "session/new":
        send({"jsonrpc": "2.0", "id": _id,
              "result": {"sessionId": "sess-1"}})
    elif _method == "session/set_mode":
        send({"jsonrpc": "2.0", "id": _id,
              "result": {"currentModeId": "readOnly"}})
    elif _method == "session/prompt":
        turn(_id)
    elif isinstance(_method, str):
        send({"jsonrpc": "2.0", "id": _id,
              "error": {"code": -32601, "message": "nope"}})
"""


def _driver(cap, tmp_path, title="mcp__stub__stub_ping"):
    return cap.Driver(
        [sys.executable, "-c", FAKE_ADAPTER],
        tmp_path,
        10.0,
        60.0,
        expected_title=title,
    )


# ── arguments ──


def test_defaults_mount_the_dispatch_entry(cap):
    args = cap.parse_args([])
    assert args.mount == "dispatch"
    assert args.timeout == 120.0
    assert args.turn_timeout == 420.0


def test_stub_mount_names_itself(cap):
    element, title, prompt, stem = cap.mount_plan("stub")
    assert element["name"] == "stub"
    assert title == "mcp__stub__stub_ping"
    assert title in prompt
    assert stem == "muse-stub-control.jsonl"


def test_dispatch_mount_is_the_member_entry(cap):
    element, title, prompt, stem = cap.mount_plan("dispatch")
    assert element["name"] == "kirocrew-dashboard"
    assert title == "mcp__kirocrew-dashboard__session_status"
    assert title in prompt
    assert stem == "muse-member-dispatch.jsonl"


# ── permission picks ──


def test_allow_pick_prefers_once(cap):
    params = {
        "options": [
            {"optionId": "allow-always", "kind": "allow_always"},
            {"optionId": "allow-once", "kind": "allow_once"},
        ]
    }
    assert cap._first_allow_option(params) == "allow-once"


def test_allow_pick_with_no_options_is_unproven(cap):
    assert cap._first_allow_option({"options": []}) == ""
    assert cap._first_allow_option({}) == ""


def test_reject_pick_prefers_once(cap):
    params = {
        "options": [
            {"optionId": "deny-always", "kind": "reject_always"},
            {"optionId": "deny-once", "kind": "reject_once"},
        ]
    }
    assert cap._first_reject_option(params) == "deny-once"


# ── verdict ──


def _call_frame(title, server="stub", tool="stub_ping"):
    return {
        "jsonrpc": "2.0",
        "method": "session/update",
        "params": {
            "sessionId": "s",
            "update": {
                "sessionUpdate": "tool_call",
                "toolCallId": "call-1",
                "title": title,
                "rawInput": {"server": server, "tool": tool, "arguments": {}},
            },
        },
    }


def _done_frame():
    return {
        "jsonrpc": "2.0",
        "method": "session/update",
        "params": {
            "sessionId": "s",
            "update": {
                "sessionUpdate": "tool_call_update",
                "toolCallId": "call-1",
                "status": "completed",
            },
        },
    }


def test_measure_reached_on_mount_call_gate_answer(cap, tmp_path):
    driver = _driver(cap, tmp_path)
    driver.frames = [_call_frame("mcp__stub__stub_ping"), _done_frame()]
    driver.dispatch_permission_requests = 1
    driver.allowed_tool = "mcp__stub__stub_ping"
    code, reason = cap.measure(driver)
    assert code == cap.EXIT_REACHED, reason


def test_measure_not_reached_without_the_call(cap, tmp_path):
    driver = _driver(cap, tmp_path)
    driver.frames = [_done_frame()]
    code, reason = cap.measure(driver)
    assert code == cap.EXIT_NOT_REACHED
    assert "never called" in reason


# ── redaction ──


def test_normalize_drops_inventories_and_neutralizes_host(cap, tmp_path):
    cwd = str(tmp_path)
    frames = [
        {
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {
                "sessionId": "sess-1",
                "update": {
                    "sessionUpdate": "available_commands_update",
                    "commands": [],
                },
            },
        },
        {
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {
                "sessionId": "sess-1",
                "update": {
                    "sessionUpdate": "agent_thought_chunk",
                    "content": {"type": "text", "text": "hmm"},
                },
            },
        },
        {
            "jsonrpc": "2.0",
            "method": "session/request_permission",
            "params": {
                "sessionId": "sess-1",
                "cwd": cwd,
                "toolCall": {"title": "t"},
            },
            "id": 9,
        },
    ]
    lines = cap.normalize(frames, "sess-1", cwd)
    blob = json.dumps(lines)
    assert len(lines) == 1
    assert "SESSION" in blob
    assert "sess-1" not in blob
    assert cwd not in blob
    assert "~/work" in blob


# ── the drive, against a fake ──


def test_drive_says_reached_against_a_performing_adapter(cap, tmp_path):
    with _driver(cap, tmp_path) as driver:
        driver.initialize()
        assert driver.agent_version == "9.9"
        driver.new_session({"name": "stub"})
        assert driver.session_id == "sess-1"
        note = driver.set_mode("readOnly")
        assert "readOnly" in note
        assert driver.set_mode("default").startswith("default kept")
        stop = driver.prompt("call it")
        assert stop == "end_turn"
    assert driver.allowed_tool == "mcp__stub__stub_ping"
    assert driver.dispatch_permission_requests == 1
    code, reason = cap.measure(driver)
    assert code == cap.EXIT_REACHED, reason
