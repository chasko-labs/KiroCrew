"""A chat session that lacks Kiro Crew's own MCP servers says so in its transcript.

On kiro-cli a custom agent gets ``kirocrew-core`` and ``kirocrew-cron`` through
its skill view only. A spawn that falls back to the authored agent (the view
ceiling reached, or projection off) runs without them. The unresolved-ref guard
records that in the gateway log and the slot list's ``mcp_report``, which no chat
surface renders, so the transcript has to say it. These tests pin:

1. ``chat_runner._notice_unresolved_control_plane`` appends one warn ``notice``
   row naming a control-plane server the session's report lists as unresolved,
   read from the guard's own output, and nothing for a third-party ref, a clean
   report, a provider without a report, or a session whose settings file
   withholds Kiro Crew's servers (the seed-declined notice covers that one).
2. A real dashboard turn on a new session whose report lists ``@kirocrew-core``
   shows that row in its transcript before the assistant's reply.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from turn_harness import ScriptedProvider, TurnScript, run_turn

from kiro_crew.acp.mcp_ref_guard import warn_unresolved_server_refs
from kiro_crew.acp.mcp_session_report import McpSessionReport
from kiro_crew.acp.types import EVENT_COMPLETE, EVENT_TEXT_CHUNK, STOP_REASON_END_TURN, AcpEvent
from kiro_crew.agent_sdk.backends import ACP_BACKEND_KIRO
from kiro_crew.dashboard import chat_runner

#: The spec the reported case runs: an agent a package manager installed, whose
#: tools name Crew's server and a server nothing defines, with no servers of its own.
_AUTHORED_SPEC = {
    "name": "reprobot",
    "mcpServers": {},
    "tools": ["fs_read", "@kirocrew-core", "@kc-repro-absent"],
    "includeMcpJson": False,
}


@pytest.fixture
def notices(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str, str]]:
    """Collect ``(role, text, cls)`` for every row appended via ``append_and_surface``."""
    seen: list[tuple[str, str, str]] = []
    monkeypatch.setattr(
        chat_runner,
        "append_and_surface",
        lambda state, slot, role, text, cls="", **kw: seen.append((role, text, cls)),
    )
    return seen


def _report_for(spec: dict[str, Any] | None) -> McpSessionReport:
    """A session report holding what the guard found for *spec* on kiro-cli."""
    report = McpSessionReport()
    report.begin_session([])
    if spec is not None:
        report.record_unresolved_refs(
            warn_unresolved_server_refs(
                spec, [], backend=ACP_BACKEND_KIRO, agent="reprobot", gateway_enabled=False
            )
        )
    return report


class _Provider:
    """A provider whose session report holds what the guard found for *spec*."""

    def __init__(self, spec: dict[str, Any] | None, *, withheld: object = None) -> None:
        self._report = _report_for(spec)
        if withheld is not None:
            self.settings_withheld_cause = lambda: withheld

    def mcp_session_report(self) -> McpSessionReport:
        return self._report


def _run(provider: object) -> None:
    asyncio.run(
        chat_runner._notice_unresolved_control_plane(SimpleNamespace(), SimpleNamespace(), provider)
    )


class TestNoticeEmitted:
    def test_a_fallback_session_names_the_missing_control_plane_server(self, notices: list) -> None:
        _run(_Provider(_AUTHORED_SPEC))
        [(role, text, cls)] = notices
        assert role == "notice"
        assert cls == "msg msg-warn"
        # The leading glyph is what NoticeCard reads the warn tone from.
        assert text.startswith("⚠ ")
        assert "kirocrew-core" in text
        assert "@kirocrew-core" in text
        assert "skill view" in text
        assert "kirocrew doctor" in text
        # A third-party ref stays in the log, the report and the doctor.
        assert "kc-repro-absent" not in text
        assert "kirocrew-cron" not in text

    def test_both_control_plane_servers_are_named_in_their_order(self, notices: list) -> None:
        spec = {**_AUTHORED_SPEC, "tools": ["@kirocrew-cron", "@kirocrew-core"]}
        _run(_Provider(spec))
        [(_, text, _)] = notices
        assert "servers kirocrew-core, kirocrew-cron." in text
        assert "defines them, so their tools" in text

    def test_a_settings_cause_that_is_empty_still_shows_the_row(self, notices: list) -> None:
        _run(_Provider(_AUTHORED_SPEC, withheld=""))
        assert len(notices) == 1


class TestNoNotice:
    def test_only_a_third_party_ref_unresolved(self, notices: list) -> None:
        spec = {**_AUTHORED_SPEC, "tools": ["fs_read", "@kc-repro-absent"]}
        _run(_Provider(spec))
        assert notices == []

    def test_a_spec_that_defines_the_server(self, notices: list) -> None:
        spec = {**_AUTHORED_SPEC, "mcpServers": {"kirocrew-core": {"command": "kirocrew-mcp"}}}
        _run(_Provider(spec))
        assert notices == []

    def test_a_clean_report(self, notices: list) -> None:
        _run(_Provider(None))
        assert notices == []

    def test_a_provider_without_a_report(self, notices: list) -> None:
        _run(SimpleNamespace(mcp_session_report=lambda: None))
        _run(None)
        assert notices == []

    @pytest.mark.parametrize("cause", ["session", "project"])
    def test_a_settings_file_that_withholds_crews_servers(self, notices: list, cause: str) -> None:
        """The seed-declined notice already says Crew's tools are off, and why."""
        _run(_Provider(_AUTHORED_SPEC, withheld=cause))
        assert notices == []


class _FallbackSession(ScriptedProvider):
    """A new session started on the authored agent, without its skill view."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._report = _report_for(_AUTHORED_SPEC)

    def mcp_session_report(self) -> McpSessionReport:
        return self._report


@pytest.mark.asyncio
async def test_a_new_session_shows_it_before_the_first_reply() -> None:
    """The notice is a transcript row of the session's first turn, so a session
    started on a fallback agent says so before its first reply."""
    record = await run_turn(
        TurnScript(
            events=[
                AcpEvent(kind=EVENT_TEXT_CHUNK, text="pong"),
                AcpEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN),
            ],
            provider=_FallbackSession,
        )
    )
    for rows in (record.history_rows, record.window):
        roles = [row.get("role") for row in rows]
        assert "assistant" in roles, roles
        [notice] = [
            index
            for index, row in enumerate(rows)
            if row.get("role") == "notice" and "kirocrew-core" in str(row.get("content", ""))
        ]
        assert str(rows[notice]["content"]).startswith("⚠ ")
        assert notice < roles.index("assistant")
