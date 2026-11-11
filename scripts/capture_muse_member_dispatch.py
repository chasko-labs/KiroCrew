#!/usr/bin/env python3
"""Drive muse-code-acp through a member-dispatch round trip and MEASURE it.

The bar ``ACP_BACKENDS_MEMBER_DISPATCH`` holds is a live session whose
``session/new`` array carried Crew's dispatch entry, the model called
``session_status`` on it, ONE permission request fired, and the server
answered. codex, opencode and KAS met it with
``test/fixtures/acp_frames/<backend>/member-dispatch-live.jsonl``; this
script is that drive for the muse backend, as one command:

    python3 scripts/capture_muse_member_dispatch.py [--timeout 120]

It spawns the adapter the way Crew spawns it (``_resolve_muse_acp_bin``,
imported not re-implemented), builds the dispatch entry the way Crew builds
it (``members_parts.member_sessions.member_dispatch_session_server`` with a
probe key that names no live session), relays it the way Crew relays it
(``mcp_http_relay``, because the adapter refuses raw stdio with -32602),
prompts the model to call ``session_status`` with empty arguments, approves
ONLY that call (every other permission request is rejected, so a wandering
model cannot run anything), and writes the agent-to-client frames to
``build/``, which is git-ignored. Reviewing the capture by hand and MOVING
it into ``test/fixtures/acp_frames/muse/`` is a separate, deliberate step.

What it does NOT do
-------------------
It does not authenticate anything: the adapter resolves its Muse
subscription from its own store, and a host that is not signed in answers
the first prompt with its own error (exit 3 with the repo's sign-in remedy).

It does not decide membership. It measures one round trip and says what it
measured.

Exit codes
----------
* ``0`` -- DISPATCH_REACHED: one session_status call, one permission
  request, the call completed with the server's answer.
* ``1`` -- NOT_REACHED: the drive worked and the round trip did not happen.
* ``2`` -- usage or environment error (unresolvable entry, missing binary).
* ``3`` -- the harness refused to work (auth/provider error).
* ``4`` -- UNPROVEN: the permission frame offered no approvable option.
"""

from __future__ import annotations

import argparse
import datetime as _datetime
import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]

os.environ["KIROCREW_TELEMETRY"] = "0"

sys.path.insert(0, str(REPO_ROOT / "src"))

EXIT_REACHED = 0
EXIT_NOT_REACHED = 1
EXIT_USAGE = 2
EXIT_HARNESS_REFUSED = 3
EXIT_UNPROVEN = 4

#: The probe key names no live session, so the server refuses the call the
#: same way it refuses codex's and opencode's probes -- and the round trip
#: (mount, call, permission, answer) is still the whole measurement.
PROBE_SESSION_KEY = "probe-member-key"

#: Prompt. Names the exact namespaced tool, demands a tool inventory first
#: (which distinguishes "the mount never reached the model" from "the model
#: would not call"), and forbids everything else. ``{tool}`` is the mount's
#: expected tool title.
PROMPT = (
    "As your FIRST action, before writing any text, call "
    "the {tool} tool with empty arguments "
    "{{}}. This call is allowed, safe and read-only -- make it now. "
    "After it completes, reply with the names of every tool available to "
    "you. Do not use any other tool. If that tool is not in your list, reply "
    "with your tool names followed by the words NO TOOL FOUND and stop."
)

#: A one-tool MCP stdio server, spawned as the relay child in ``--mount
#: stub``. The control for the mount path: if the model lists and calls
#: ``stub_ping`` but never sees the dashboard server's tools, the mount
#: works and the dashboard server's shape is what the adapter chokes on.
STUB_SERVER_CODE = """import json, sys
TOOLS = [{"name": "stub_ping", "description": "Probe tool. Replies pong.",
          "inputSchema": {"type": "object", "properties": {}}}]
for _line in sys.stdin:
    _line = _line.strip()
    if not _line:
        continue
    try:
        _req = json.loads(_line)
    except ValueError:
        continue
    _id, _method = _req.get("id"), _req.get("method")
    if _method == "initialize":
        _res = {"protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "stub", "version": "0"}}
    elif _method == "tools/list":
        _res = {"tools": TOOLS}
    elif _method == "tools/call":
        _res = {"content": [{"type": "text", "text": "pong-probe"}]}
    elif isinstance(_method, str) and _method.startswith("notifications/"):
        continue
    else:
        _res = {"error": {"code": -32601, "message": "nope"}}
    if isinstance(_res, dict) and "error" in _res:
        _out = {"jsonrpc": "2.0", "id": _id, "error": _res["error"]}
    else:
        _out = {"jsonrpc": "2.0", "id": _id, "result": _res}
    sys.stdout.write(json.dumps(_out) + chr(10))
    sys.stdout.flush()
"""

#: Frame classes dropped from the written capture. Inventories and model
#: prose the corpus refuses: the harness's own command list, run ids and
#: wall-clock times, and thinking chunks no parser reads.
DROPPED_UPDATES = ("available_commands_update", "session_info_update")
DROPPED_THOUGHTS = ("agent_thought_chunk",)

REFUSAL_MARKERS = (
    "authentication required",
    "failed to resolve provider",
    "no provider",
    "not authenticated",
    "unauthorized",
    "api key",
    "no credentials",
)


class CaptureError(Exception):
    """Something about the host or the arguments."""


class HarnessRefused(Exception):
    """Auth or provider error rather than a turn."""


def sign_in_remedy() -> str:
    try:
        from kiro_crew.agent_sdk.host_auth import declaration_for
        from kiro_crew.acp_backends import ACP_BACKEND_MUSE

        declaration = declaration_for(ACP_BACKEND_MUSE)
    except Exception:
        return ""
    return getattr(declaration, "sign_in_remedy", "") or ""


def resolve_adapter_argv() -> list[str]:
    """Crew's own adapter resolution, imported not re-implemented."""
    try:
        from kiro_crew.acp.harness import muse as muse_mod
    except Exception as exc:
        raise CaptureError(
            f"cannot import kiro_crew.acp.harness.muse: {exc}"
        ) from exc
    argv, searched = muse_mod._resolve_muse_acp_bin()
    if not argv:
        raise CaptureError(
            f"muse-code-acp not found ({searched}). Install it with "
            "'npm i -g @bex-co/muse-code-acp@0'."
        )
    return list(argv)


def build_dispatch_element() -> dict[str, Any]:
    """Crew's own dispatch entry for a probe member session."""
    from kiro_crew.members_parts.member_sessions import (
        member_dispatch_session_server,
    )

    element = member_dispatch_session_server(PROBE_SESSION_KEY)
    if element is None:
        raise CaptureError(
            "member_dispatch_session_server resolved to None: the dashboard "
            "server command is not resolvable on this host, so there is no "
            "entry to mount."
        )
    if not isinstance(element, dict):
        raise CaptureError(
            f"dispatch entry has an unexpected shape: {type(element)}"
        )
    return dict(element)


def start_relay(element: dict[str, Any]):
    """One HTTP relay in front of the dispatch stdio entry."""
    from kiro_crew import mcp_http_relay as relay_mod

    command = element.get("command")
    args = element.get("args") or []
    if not isinstance(command, str) or not command:
        raise CaptureError(
            f"dispatch entry carries no stdio command: {sorted(element)}"
        )
    argv = [command, *(a for a in args if isinstance(a, str))]
    env_pairs = element.get("env") or []
    env = {
        str(p.get("name")): str(p.get("value", ""))
        for p in env_pairs
        if isinstance(p, dict) and p.get("name")
    } or None
    name = str(element.get("name") or "kirocrew-dashboard")
    try:
        relay = relay_mod.start(argv, env)
    except Exception as exc:
        raise CaptureError(f"relay child would not start ({exc})") from exc
    return relay, {"name": name, "type": "http", "url": relay.url, "headers": []}


def build_stub_element() -> dict[str, Any]:
    """A one-tool stdio element for the ``--mount stub`` control."""
    return {
        "name": "stub",
        "type": "stdio",
        "command": sys.executable,
        "args": ["-c", STUB_SERVER_CODE],
        "env": [],
    }


def mount_plan(mount: str) -> tuple[dict[str, Any], str, str, str]:
    """(session element, expected tool title, prompt, capture file stem)."""
    if mount == "stub":
        return (
            build_stub_element(),
            "mcp__stub__stub_ping",
            PROMPT.format(tool="mcp__stub__stub_ping"),
            "muse-stub-control.jsonl",
        )
    if mount == "dispatch":
        return (
            build_dispatch_element(),
            "mcp__kirocrew-dashboard__session_status",
            PROMPT.format(tool="mcp__kirocrew-dashboard__session_status"),
            "muse-member-dispatch.jsonl",
        )
    raise CaptureError(f"unknown --mount {mount!r}: want dispatch or stub")


def _first_allow_option(params: Any) -> str:
    """The id of an allow-once-shaped option, or an empty string."""
    if not isinstance(params, dict):
        return ""
    options = params.get("options")
    if not isinstance(options, list):
        return ""
    texts: list[tuple[str, dict]] = []
    for option in options:
        if not isinstance(option, dict) or not option.get("optionId"):
            continue
        texts.append((json.dumps(option).lower(), option))

    def _wants(text: str) -> bool:
        return "allow" in text or "approve" in text or "yes" in text

    for text, option in texts:
        if _wants(text) and "once" in text:
            return str(option["optionId"])
    for text, option in texts:
        if _wants(text):
            return str(option["optionId"])
    return ""


@dataclass
class Driver:
    """A minimal ACP client: enough to prompt, permit once, and listen."""

    argv: list[str]
    cwd: Path
    timeout: float
    turn_timeout: float = 420.0
    next_id: int = 0
    frames: list[dict[str, Any]] = field(default_factory=list)
    stderr_tail: list[str] = field(default_factory=list)
    agent_version: str = "unmeasured"
    agent_name: str = ""
    session_id: str = ""
    allowed_tool: str = ""
    permission_requests: int = 0
    dispatch_permission_requests: int = 0
    other_rejections: int = 0
    expected_title: str = ""

    _lines: Any = None
    _proc: Any = None

    def __enter__(self) -> "Driver":
        import queue as _q

        self._lines = _q.Queue()
        env = dict(os.environ)
        try:
            self._proc = subprocess.Popen(
                list(self.argv),
                cwd=str(self.cwd),
                env=env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
            )
        except OSError as exc:
            raise CaptureError(f"cannot start {' '.join(self.argv)}: {exc}") from exc
        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()
        return self

    def __exit__(self, *_exc: object) -> None:
        proc = self._proc
        if proc is None:
            return
        for step in (proc.terminate, proc.kill):
            if proc.poll() is not None:
                break
            step()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                continue

    def _read_stdout(self) -> None:
        assert self._proc is not None and self._proc.stdout is not None
        for line in self._proc.stdout:
            self._lines.put(line)
        self._lines.put(None)

    def _read_stderr(self) -> None:
        assert self._proc is not None and self._proc.stderr is not None
        for line in self._proc.stderr:
            self.stderr_tail.append(line.rstrip())
            del self.stderr_tail[:-40]

    def _send(self, payload: dict[str, Any]) -> None:
        proc = self._proc
        if proc is None or proc.stdin is None:
            raise CaptureError("the harness process is gone")
        try:
            proc.stdin.write(json.dumps(payload) + "\n")
            proc.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise CaptureError(
                f"adapter closed its input after {len(self.frames)} frame(s): "
                f"{exc}. {self._stderr_hint()}"
            ) from exc

    def _stderr_hint(self) -> str:
        if not self.stderr_tail:
            return "It wrote nothing to stderr."
        tail = "\n  ".join(self.stderr_tail[-8:])
        return f"Its last stderr lines were:\n  {tail}"

    def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self.next_id += 1
        request_id = self.next_id
        self._send(
            {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
        )
        return self._pump_until(request_id, method)

    def _pump_until(self, request_id: int, method: str) -> dict[str, Any]:
        deadline = time.monotonic() + self.turn_timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CaptureError(
                    f"turn deadline ({self.turn_timeout:.0f}s) expired while "
                    f"answering {method} after {len(self.frames)} frame(s): "
                    f"{self.permission_requests} permission request(s). "
                    f"{self._stderr_hint()}"
                )
            try:
                line = self._lines.get(timeout=min(self.timeout, remaining))
            except queue.Empty:
                raise CaptureError(
                    f"adapter sent nothing for {self.timeout:.0f}s while answering "
                    f"{method}. {self._stderr_hint()}"
                ) from None
            if line is None:
                raise CaptureError(
                    f"adapter exited while answering {method}. {self._stderr_hint()}"
                )
            line = line.strip()
            if not line:
                continue
            try:
                frame = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(frame, dict):
                continue
            self.frames.append(frame)
            if frame.get("id") == request_id and (
                "result" in frame or "error" in frame
            ):
                error = frame.get("error")
                if error:
                    self._raise_for_error(method, error)
                result = frame.get("result")
                return result if isinstance(result, dict) else {}
            if "method" in frame and "id" in frame:
                self._answer(frame)

    def _raise_for_error(self, method: str, error: Any) -> None:
        message = ""
        if isinstance(error, dict):
            message = str(error.get("message") or "")
        blob = json.dumps(error) if not isinstance(error, str) else error
        lowered = f"{message} {blob}".lower()
        if any(marker in lowered for marker in REFUSAL_MARKERS):
            remedy = sign_in_remedy()
            raise HarnessRefused(f"{method} was refused: {message or blob}. {remedy}")
        raise CaptureError(f"{method} failed: {message or blob}. {self._stderr_hint()}")

    def _answer(self, frame: dict[str, Any]) -> None:
        """Approve ONLY the dispatch call; refuse everything else.

        The drive runs in the operator's environment, so approval is the one
        thing it must bound: the probe key names no live session, which makes
        the approved call a server refusal, while any other call -- a native
        tool the model reached for on its own -- is rejected unread.
        """
        method = frame.get("method") or ""
        request_id = frame.get("id")
        if method == "session/request_permission":
            self.permission_requests += 1
            params = frame.get("params") or {}
            tool = params.get("toolCall") or {}
            title = str(tool.get("title") or "")
            if self.expected_title and title == self.expected_title:
                self.dispatch_permission_requests += 1
                option_id = _first_allow_option(params)
                if option_id:
                    self.allowed_tool = title
                    self._send(
                        {
                            "jsonrpc": "2.0",
                            "id": request_id,
                            "result": {
                                "outcome": {
                                    "outcome": "selected",
                                    "optionId": option_id,
                                }
                            },
                        }
                    )
                    return
                raise CaptureError(
                    "UNPROVEN: the dispatch permission frame offered no "
                    f"approvable option: {json.dumps(params)[:400]}"
                )
            self.other_rejections += 1
            option_id = _first_reject_option(params)
            if option_id:
                self._send(
                    {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "result": {
                            "outcome": {
                                "outcome": "selected",
                                "optionId": option_id,
                            }
                        },
                    }
                )
                return
            self._send(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {"outcome": {"outcome": "cancelled"}},
                }
            )
            return
        self._send(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {
                    "code": -32601,
                    "message": f"{method} is not implemented by this capture client",
                },
            }
        )

    def initialize(self) -> None:
        result = self.request(
            "initialize",
            {
                "protocolVersion": 1,
                "clientCapabilities": {"fs": {"readTextFile": False, "writeTextFile": False}},
            },
        )
        info = result.get("agentInfo")
        if isinstance(info, dict):
            if info.get("version"):
                self.agent_version = str(info["version"])
            if info.get("name"):
                self.agent_name = str(info["name"])

    def new_session(self, element: dict[str, Any]) -> None:
        result = self.request(
            "session/new", {"cwd": str(self.cwd), "mcpServers": [element]}
        )
        session_id = result.get("sessionId")
        if not session_id:
            raise CaptureError(
                "session/new returned no sessionId: "
                f"{json.dumps(result)[:400]}"
            )
        self.session_id = str(session_id)

    def set_read_only(self) -> str:
        """Hold the turn to read-only, the way the codex probe did.

        The adapter auto-settles low-risk native ops with no permission
        frame, so in default mode the model explores the host instead of
        calling the mounted tool. Read-only gates native execution behind
        permission requests this client rejects, leaving the dispatch call
        -- approved once -- as the turn's only way forward.
        """
        try:
            result = self.request(
                "session/set_mode",
                {"sessionId": self.session_id, "modeId": "readOnly"},
            )
        except CaptureError as exc:
            return f"readOnly refused ({exc}); driving in default mode"
        return f"readOnly set ({json.dumps(result)[:160]})"

    def prompt(self, text: str) -> str:
        result = self.request(
            "session/prompt",
            {
                "sessionId": self.session_id,
                "prompt": [{"type": "text", "text": text}],
            },
        )
        return str(result.get("stopReason") or "")


def _first_reject_option(params: Any) -> str:
    if not isinstance(params, dict):
        return ""
    options = params.get("options")
    if not isinstance(options, list):
        return ""
    for wanted in ("reject_once", "reject_always"):
        for option in options:
            if not isinstance(option, dict) or not option.get("optionId"):
                continue
            if str(option.get("kind") or "") == wanted:
                return str(option["optionId"])
    return ""


def _updates(frames: list[dict[str, Any]], kind: str) -> list[dict[str, Any]]:
    out = []
    for frame in frames:
        params = frame.get("params")
        if not isinstance(params, dict):
            continue
        update = params.get("update")
        if not isinstance(update, dict):
            continue
        if update.get("sessionUpdate") == kind:
            out.append(update)
    return out


def _call_text(call: dict[str, Any]) -> str:
    """Title plus raw input as one searchable string.

    ``tool_call`` updates carry ``content`` as a LIST of blocks on some
    adapters, so the title is read off the update's own ``title`` first and
    the raw input is stringified defensively.
    """
    bits = [str(call.get("title") or "")]
    try:
        bits.append(json.dumps(call.get("rawInput")))
    except (TypeError, ValueError):
        bits.append(str(call.get("rawInput")))
    return " ".join(bits)


def measure(driver: Driver) -> tuple[int, str]:
    """Read the membership bar off the drive: mount, call, gate, answer."""
    calls = _updates(driver.frames, "tool_call")
    server, _, tool = driver.expected_title.replace("mcp__", "").partition("__")
    dispatch_calls = [
        c
        for c in calls
        if tool in _call_text(c) and server in _call_text(c)
    ]
    permissions = [
        f
        for f in driver.frames
        if f.get("method") == "session/request_permission"
    ]
    if not dispatch_calls:
        return (
            EXIT_NOT_REACHED,
            "the model never called the dispatch tool "
            f"({len(calls)} tool_call(s), {len(permissions)} permission request(s))",
        )
    if driver.dispatch_permission_requests != 1:
        return (
            EXIT_NOT_REACHED,
            f"expected exactly one dispatch permission request, saw "
            f"{driver.dispatch_permission_requests} "
            f"({driver.other_rejections} other request(s) rejected)",
        )
    if not driver.allowed_tool:
        return (
            EXIT_NOT_REACHED,
            "the dispatch call was never approved",
        )
    completed = [
        u
        for u in _updates(driver.frames, "tool_call_update")
        if u.get("status") == "completed"
    ]
    if not completed:
        return (
            EXIT_NOT_REACHED,
            "the approved dispatch call never completed",
        )
    return (
        EXIT_REACHED,
        "one session_status call, one permission request, "
        "the call completed with the server's answer",
    )


def normalize(frames: list[dict[str, Any]], session_id: str, cwd: str) -> list[dict]:
    """Corpus-ready lines: scrubbed, host-neutral, small."""
    from kiro_crew.acp._dispatch import redact_text

    home = os.path.expanduser("~")
    kept: list[dict] = []
    for frame in frames:
        if not isinstance(frame, dict) or "method" not in frame and "id" not in frame:
            continue
        params = frame.get("params")
        if isinstance(params, dict):
            update = params.get("update")
            if isinstance(update, dict):
                kind = update.get("sessionUpdate")
                if kind in DROPPED_UPDATES or kind in DROPPED_THOUGHTS:
                    continue
        blob = json.dumps(frame)
        blob = redact_text(blob)
        if home and home != "~":
            blob = blob.replace(home, "~")
        if session_id:
            blob = blob.replace(session_id, "SESSION")
        if cwd:
            blob = blob.replace(cwd, "~/work")
        try:
            kept.append(json.loads(blob))
        except json.JSONDecodeError:
            continue
    return kept


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Drive muse-code-acp through a member-dispatch round trip."
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=120.0,
        help="seconds to wait for any one wire answer",
    )
    parser.add_argument(
        "--cwd",
        default="",
        help="working directory for the probe session (created if missing)",
    )
    parser.add_argument(
        "--turn-timeout",
        type=float,
        default=420.0,
        help="seconds for the whole model turn before giving up (partial "
        "frames are still written)",
    )
    parser.add_argument(
        "--mount",
        default="dispatch",
        choices=("dispatch", "stub"),
        help="dispatch mounts Crew's session-control entry (the membership "
        "bar); stub mounts a one-tool server as the control for the mount "
        "path itself",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    if not args.timeout >= 1.0:
        print("error: --timeout must be at least 1 second", file=sys.stderr)
        return EXIT_USAGE

    try:
        adapter_argv = resolve_adapter_argv()
        element, expected_title, prompt, stem = mount_plan(args.mount)
        relay, http_element = start_relay(element)
    except CaptureError as exc:
        print(f"environment: {exc}", file=sys.stderr)
        return EXIT_USAGE

    cwd = Path(args.cwd) if args.cwd else (Path.home() / "workspaces" / "muse-probe")
    try:
        cwd.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        print(f"environment: cannot create {cwd}: {exc}", file=sys.stderr)
        return EXIT_USAGE

    build_dir = REPO_ROOT / "build"
    build_dir.mkdir(exist_ok=True)
    out_path = build_dir / stem

    driver: Driver | None = None
    stop = ""
    code, reason = EXIT_NOT_REACHED, "drive did not finish"
    try:
        with Driver(
            adapter_argv, cwd, args.timeout, args.turn_timeout, expected_title=expected_title
        ) as driver:
            try:
                driver.initialize()
                driver.new_session(http_element)
                mode_note = driver.set_read_only()
                print(f"mode: {mode_note}")
                stop = driver.prompt(prompt)
            except HarnessRefused as exc:
                print(f"refused: {exc}", file=sys.stderr)
                return EXIT_HARNESS_REFUSED
            except CaptureError as exc:
                if str(exc).startswith("UNPROVEN"):
                    print(str(exc), file=sys.stderr)
                    return EXIT_UNPROVEN
                reason = str(exc)
                code = EXIT_NOT_REACHED
            else:
                code, reason = measure(driver)
    except CaptureError as exc:
        reason = str(exc)
        code = EXIT_NOT_REACHED
    finally:
        try:
            relay.stop()
        except Exception:
            pass

    # Partial frames are evidence too: a stuck turn's last frames say where
    # it stuck, so the capture is written on every path that drove one.
    if driver is not None and driver.frames:
        lines = normalize(driver.frames, driver.session_id, str(cwd))
        header = {
            "_meta": {
                "backend": "muse",
                "recorded": "live",
                "date": _datetime.date.today().isoformat(),
                "agent_version": driver.agent_version,
                "agent_name": driver.agent_name,
                "note": (
                    "Driven over stdio by scripts/"
                    f"capture_muse_member_dispatch.py (--mount {args.mount}): "
                    "session/new carried exactly one element behind "
                    "mcp_http_relay (stdio is refused -32602), mode "
                    "readOnly set over session/set_mode. Dispatch: members."
                    "member_dispatch_session_server('probe-member-key'), "
                    "the model's session_status call raising one permission "
                    "request (approved once; every other request rejected), "
                    "the server answering (it refuses, as the probe key "
                    "names no live session). Stub: a one-tool server as the "
                    "control for the mount path. The session uuid reads "
                    "SESSION and the working directory reads ~/work; "
                    "available_commands_update, session_info_update and "
                    "agent_thought_chunk frames are dropped as the corpus "
                    "requires."
                ),
            }
        }
        with open(out_path, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(header) + "\n")
            for frame in lines:
                handle.write(json.dumps(frame) + "\n")
        print(f"frames: {len(lines)}")
        print(f"capture: {out_path}")
    print(f"verdict: {'DISPATCH_REACHED' if code == 0 else 'NOT_REACHED'}")
    print(f"reason: {reason}")
    print(f"stop: {stop or 'none'}")
    if code != 0:
        print(f"failed: {reason}", file=sys.stderr)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
