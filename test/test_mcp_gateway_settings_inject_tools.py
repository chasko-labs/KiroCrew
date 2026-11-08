"""A ``settings/mcp.json`` server is pooled only into an agent whose ``tools`` names it.

The rewriter wraps each stubbed ``settings/mcp.json`` server into an agent's overlay,
and ``session_servers.pooled_session_servers`` sends every wrapped overlay entry at
ACP ``session/new``, where kiro-cli and KAS mount it. A stub for a server the agent's
``tools`` never names is therefore a server the author left out, offered and callable
anyway. Leaving it out of the overlay costs nothing ``tools`` could reach: for an agent
with ``includeMcpJson: true`` the host's own ``mcp.json`` merge still starts it, unpooled.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

from kiro_crew.mcp_gateway.rewriter import (
    _WRAPPER_MARKER,
    _injectable_settings_servers,
    _rewrite_single_spec,
)
from kiro_crew.mcp_utils import mcp_server_alias

_ABSENT = object()


def _settings() -> dict[str, Any]:
    return {"mcpServers": {"beta-mcp": {"command": sys.executable, "args": ["-b"]}}}


def _rewrite(spec: dict[str, Any], settings: dict[str, Any], stub_servers, tmp_path: Path):
    stubbed = frozenset(stub_servers)
    return _rewrite_single_spec(
        spec,
        stubs_dir=tmp_path / "stubs",
        socket_path=tmp_path / "gw.sock",
        work_dir=tmp_path / "wd",
        sandbox_mode="auto",
        approval_mode="interactive",
        stub_servers=stubbed,
        inject_servers=_injectable_settings_servers(settings, stubbed),
    )


@pytest.mark.parametrize(
    ("tools", "injected"),
    [
        (["*"], True),
        ("*", True),
        (["fs_read", "@beta-mcp"], True),
        (["@beta-mcp/x"], True),
        (["fs_read", "@other"], False),
        ([], False),
        (_ABSENT, False),
        ({"beta-mcp": True}, False),
    ],
    ids=[
        "star-entry",
        "star-value",
        "whole-server",
        "one-tool",
        "other-server",
        "empty",
        "absent",
        "malformed",
    ],
)
def test_a_settings_stub_follows_the_agents_tools(
    tmp_path: Path, tools: object, injected: bool
) -> None:
    spec: dict[str, Any] = {"name": "agent-a", "mcpServers": {}, "includeMcpJson": True}
    if tools is not _ABSENT:
        spec["tools"] = tools
    new_spec, wrapped = _rewrite(spec, _settings(), ["beta-mcp"], tmp_path)
    servers = new_spec.get("mcpServers", {})
    assert ("beta-mcp" in servers) is injected
    assert wrapped == (1 if injected else 0)
    if injected:
        assert servers["beta-mcp"][_WRAPPER_MARKER] is True


def test_a_slash_named_settings_server_is_named_by_its_alias(tmp_path: Path) -> None:
    """The stub is keyed by the slash-free alias, which is what ``tools`` can spell."""
    raw = "npm:@playwright/mcp"
    alias = mcp_server_alias(raw)
    settings = {"mcpServers": {raw: {"command": sys.executable}}}
    named = {"name": "agent-a", "mcpServers": {}, "tools": [f"@{alias}"]}
    unnamed = {"name": "agent-a", "mcpServers": {}, "tools": ["@beta-mcp"]}

    new_spec, _ = _rewrite(named, settings, [alias], tmp_path / "named")
    assert alias in new_spec["mcpServers"]

    new_spec, wrapped = _rewrite(unnamed, settings, [alias], tmp_path / "unnamed")
    assert alias not in new_spec.get("mcpServers", {})
    assert wrapped == 0


def test_only_the_named_settings_server_is_injected(tmp_path: Path) -> None:
    """Two stubbed settings servers, one named: the agent gets exactly that one."""
    settings = {
        "mcpServers": {
            "beta-mcp": {"command": sys.executable, "args": ["-b"]},
            "slack-mcp": {"command": sys.executable, "args": ["-s"]},
        }
    }
    spec = {"name": "agent-a", "mcpServers": {}, "tools": ["fs_read", "@beta-mcp"]}
    new_spec, wrapped = _rewrite(spec, settings, ["beta-mcp", "slack-mcp"], tmp_path)
    assert set(new_spec["mcpServers"]) == {"beta-mcp"}
    assert wrapped == 1
