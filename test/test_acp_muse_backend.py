"""The muse spawn path: two components, a relay for stdio MCP, and its proof.

Pinned: resolution of ``muse-code-acp`` and ``muse`` separately (the error names
the missing half); the ``MUSE_CODE_EXECUTABLE`` naming with operator override
winning; the stdio->HTTP relay rewrite of the session MCP array (raw stdio is
refused ``-32602``); failed relays recorded as failed servers, never silent;
and the registry rows (routing, projection, labels, protocol version).
Refusal wording for a missing component belongs to the probe; the permission
frames the adapter emits are carried by
``test/fixtures/acp_frames/muse/``.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from kiro_crew.acp.harness import muse as muse_mod
from kiro_crew.acp.harness.muse import MuseLaunch
from kiro_crew.acp.transport_errors import AcpError
from kiro_crew.acp_backends import (
    ACP_BACKEND_MUSE,
    BASELINE_SELECTABLE_BACKENDS,
)
from kiro_crew.agent_sdk import backend_install as probe
from kiro_crew.agent_sdk.backends import (
    ACP_BACKENDS_INDEPENDENT_SETUP,
    ACP_BACKENDS_KNOWN,
    POLICY_ID_BY_BACKEND,
)
from kiro_crew.agent_sdk.backends_launch import (
    ACP_BACKEND_NODE_ADAPTER_PACKAGES,
    ACP_BACKEND_PROCESS_NAMES,
)
from kiro_crew.agent_sdk.backends_mcp import ACP_BACKENDS_SESSION_MCP_ARRAY
from kiro_crew.agent_sdk.backends_models import _MODEL_REGISTRY_NAMESPACE_BY_BACKEND
from kiro_crew.agent_sdk.backends_routing import ACP_BACKEND_ROUTING, Routing
from kiro_crew.agent_sdk.backends_sessions import (
    ACP_BACKENDS_ACP_CLIENT_SPAWNABLE,
    ACP_BACKENDS_HARNESS_OWNED_SESSIONS,
)
from kiro_crew.agent_sdk.drivers import acp as acp_driver
from kiro_crew.providers.mirrors.registry import (
    PROJECTIONS,
    PerToolDeny,
    ProjectionKind,
    mirror_for,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "acp_frames" / "muse"


@pytest.fixture
def clean_resolution(monkeypatch):
    """Both muse resolutions start unresolved and are left unresolved."""
    monkeypatch.setattr(muse_mod, "_muse_acp_argv_cache", muse_mod.launch_mod._UNRESOLVED)
    monkeypatch.setattr(muse_mod, "_muse_bin_cache", muse_mod.launch_mod._UNRESOLVED)
    yield
    monkeypatch.undo()


@pytest.fixture
def no_ambient_muse_env(monkeypatch):
    """The operator overrides never leak in from the recording host."""
    monkeypatch.delenv("MUSE_CODE_ACP_BIN", raising=False)
    monkeypatch.delenv("MUSE_CODE_EXECUTABLE", raising=False)


def _stub_session():
    async def _noop():
        return None

    return SimpleNamespace(
        _model="",
        _agent_version_read=True,
        _prepare_session_mcp=_noop,
        _start_muse_relays=_noop,
    )


# ── registry ──


def test_muse_is_a_known_selectable_backend():
    assert ACP_BACKEND_MUSE == "muse"
    assert ACP_BACKEND_MUSE in ACP_BACKENDS_KNOWN
    assert ACP_BACKEND_MUSE in BASELINE_SELECTABLE_BACKENDS
    assert ACP_BACKEND_MUSE in ACP_BACKENDS_INDEPENDENT_SETUP
    assert POLICY_ID_BY_BACKEND[ACP_BACKEND_MUSE] == "muse"
    assert ACP_BACKEND_PROCESS_NAMES[ACP_BACKEND_MUSE] == "muse-code-acp"
    assert ACP_BACKEND_NODE_ADAPTER_PACKAGES[ACP_BACKEND_MUSE] == "@bex-co/muse-code-acp"


def test_muse_routing_is_ask_by_construction():
    # The adapter offers every approval as a host-offered ACP choice and fails
    # closed itself: nothing to seed, no gate to load -- the kiro shape.
    assert ACP_BACKEND_ROUTING[ACP_BACKEND_MUSE] is Routing.AGENT_SPEC


def test_muse_projection_is_a_mirror_with_whole_server_deny():
    assert ACP_BACKEND_MUSE in ACP_BACKENDS_SESSION_MCP_ARRAY
    assert mirror_for(ACP_BACKEND_MUSE) is not None
    declared = PROJECTIONS[ACP_BACKEND_MUSE]
    assert declared.kind is ProjectionKind.MIRROR
    assert declared.per_tool_deny is PerToolDeny.WHOLE_SERVER
    assert _MODEL_REGISTRY_NAMESPACE_BY_BACKEND[ACP_BACKEND_MUSE] == "muse"


def test_muse_sessions_are_spawnable_and_harness_owned():
    assert ACP_BACKEND_MUSE in ACP_BACKENDS_ACP_CLIENT_SPAWNABLE
    assert ACP_BACKEND_MUSE in ACP_BACKENDS_HARNESS_OWNED_SESSIONS


def test_muse_protocol_version_matches_its_measured_handshake():
    from kiro_crew.acp.client_start import (
        _PROTOCOL_VERSION_BY_BACKEND,
        PROTOCOL_VERSION_MUSE,
    )

    assert _PROTOCOL_VERSION_BY_BACKEND[ACP_BACKEND_MUSE] == PROTOCOL_VERSION_MUSE == 1
    found = False
    for line in (FIXTURES / "handshake-measured.jsonl").read_text().splitlines():
        if '"protocolVersion": 1' in line:
            found = True
    assert found, "the literal must stay the measured wire fact"


# ── resolution ──


def test_adapter_override_is_honoured_as_is(
    tmp_path, monkeypatch, clean_resolution, no_ambient_muse_env
):
    entry = tmp_path / "index.js"
    entry.write_text("'use strict';\n")
    # The override names the entry script itself (a path, not an argv); the
    # shared ladder wraps a non-executable script with node.
    monkeypatch.setenv("MUSE_CODE_ACP_BIN", str(entry))
    argv, _searched = muse_mod._resolve_muse_acp_bin()
    assert argv is not None and argv[-1] == str(entry)


def test_agent_override_is_honoured_as_is(tmp_path, monkeypatch, no_ambient_muse_env):
    exe = tmp_path / "muse"
    exe.write_text("#!/bin/sh\n")
    monkeypatch.setenv("MUSE_CODE_EXECUTABLE", str(exe))
    found, _searched = muse_mod._resolve_muse_bin()
    assert found == str(exe)


def test_probe_names_each_absent_half(monkeypatch):
    from kiro_crew.acp.harness import muse as harness_muse

    monkeypatch.setattr(harness_muse, "_resolve_muse_acp_bin", lambda: (None, "/usr/bin"))
    monkeypatch.setattr(harness_muse, "_resolve_muse_bin", lambda: (None, "/usr/bin"))
    probe.clear_probe_cache()
    try:
        state = probe.probe_backend(ACP_BACKEND_MUSE)
        assert state.installed == "missing"
        assert set(state.missing_components) == {
            probe.COMPONENT_MUSE_ACP_ADAPTER,
            probe.COMPONENT_MUSE_CODE_CLI,
        }
    finally:
        probe.clear_probe_cache()


def test_probe_install_command_names_only_the_adapter_half(monkeypatch):
    from kiro_crew.acp.harness import muse as harness_muse

    monkeypatch.setattr(
        harness_muse,
        "_resolve_muse_acp_bin",
        lambda: (["node", "/n/muse-code-acp.js"], "/usr/bin"),
    )
    monkeypatch.setattr(harness_muse, "_resolve_muse_bin", lambda: (None, "/usr/bin"))
    probe.clear_probe_cache()
    try:
        state = probe.probe_backend(ACP_BACKEND_MUSE)
        assert state.installed == "missing"
        assert state.missing_components == (probe.COMPONENT_MUSE_CODE_CLI,)
        # The agent half is Meta's own installer: no command, and the remedy
        # lives with the harness, not the probe.
        assert state.install_command == acp_driver.muse_install_command()
        assert "muse" in muse_mod.MUSE_CODE_INSTALL_REMEDY
    finally:
        probe.clear_probe_cache()


# ── launch ──


def test_resolve_spawn_refuses_without_the_adapter(
    monkeypatch, clean_resolution, no_ambient_muse_env
):
    monkeypatch.setattr(muse_mod, "_resolve_muse_acp_bin", lambda: (None, "/usr/bin"))
    session = _stub_session()
    ctx = SimpleNamespace(session=session)
    with pytest.raises(AcpError, match="muse-code-acp not found"):
        asyncio.run(MuseLaunch().resolve_spawn(ctx))


def test_resolve_spawn_plans_the_resolved_argv(monkeypatch, clean_resolution, no_ambient_muse_env):
    argv = ["node", "/n/muse-code-acp.js"]
    monkeypatch.setattr(muse_mod, "_resolve_muse_acp_bin", lambda: (list(argv), "/usr/bin"))
    session = _stub_session()
    ctx = SimpleNamespace(session=session)
    plan = asyncio.run(MuseLaunch().resolve_spawn(ctx))
    assert plan.argv == argv
    assert "muse-code-acp" in plan.spawn_label


def test_apply_spawn_env_names_the_agent_but_loses_to_an_override(
    tmp_path, monkeypatch, no_ambient_muse_env
):
    exe = tmp_path / "muse"
    exe.write_text("#!/bin/sh\n")
    monkeypatch.setenv("MUSE_CODE_EXECUTABLE", str(exe))
    env: dict = {}
    MuseLaunch().apply_spawn_env(env)
    assert env["MUSE_CODE_EXECUTABLE"] == str(exe)

    env = {"MUSE_CODE_EXECUTABLE": "/operator/pinned/muse"}
    MuseLaunch().apply_spawn_env(env)
    assert env["MUSE_CODE_EXECUTABLE"] == "/operator/pinned/muse"


# ── relay composition ──


class _Relay:
    def __init__(self, url):
        self._url = url

    def element(self, name):
        return {"name": name, "type": "http", "url": self._url, "headers": []}


def _relay_session(elements, relays):
    from kiro_crew.acp import client_mcp_array as arr

    session = SimpleNamespace(
        _muse_relays=list(relays),
        _session_mcp_servers=lambda: list(elements),
    )
    return arr._muse_session_mcp_servers(session)


def test_composer_relays_stdio_and_passes_http_through():
    elements = [
        {
            "name": "crew",
            "command": "/usr/local/bin/crew-mcp",
            "args": [],
            "env": [],
            "type": "stdio",
        },
        {"name": "remote", "type": "http", "url": "https://mcp.example.com/mcp", "headers": []},
    ]
    out = _relay_session(elements, [("crew", _Relay("http://127.0.0.1:9/mcp"))])
    assert out == [
        {"name": "crew", "type": "http", "url": "http://127.0.0.1:9/mcp", "headers": []},
        {"name": "remote", "type": "http", "url": "https://mcp.example.com/mcp", "headers": []},
    ]


def test_composer_drops_a_stdio_element_with_no_relay():
    elements = [
        {"name": "dead", "command": "/nonexistent/mcp", "args": [], "env": [], "type": "stdio"},
    ]
    assert _relay_session(elements, []) == []


def test_failed_relay_start_is_recorded_not_silent(monkeypatch):
    from kiro_crew.acp import client_mcp_array as arr

    monkeypatch.setattr(arr, "asyncio", asyncio)
    elements = [
        {"name": "dead", "command": "/nonexistent/mcp-xyz", "args": [], "env": [], "type": "stdio"},
    ]
    session = SimpleNamespace(
        _muse_relays=[],
        _mcp_pruned=(),
        _mcp_pruned_omitted=0,
        backend="muse",
        _session_mcp_servers=lambda: list(elements),
    )
    asyncio.run(arr._discard_muse_relays(session))
    asyncio.run(arr._start_muse_relays(session))
    assert session._muse_relays == []
    assert [p.name for p in session._mcp_pruned] == ["dead"]


def test_relay_module_serves_the_measured_element_shape():
    from kiro_crew import mcp_http_relay as relay_mod

    relay = relay_mod.start(["cat"])
    try:
        assert relay.element("stub") == {
            "name": "stub",
            "type": "http",
            "url": relay.url,
            "headers": [],
        }
        assert relay.url.startswith("http://127.0.0.1:")
        assert relay.url.endswith("/mcp")
    finally:
        relay.stop()


def test_modes_fixture_carries_no_auto_approve_default():
    seen = set()
    for line in (FIXTURES / "modes-measured.jsonl").read_text().splitlines():
        message = json.loads(line)["message"]
        modes = message.get("result", {}).get("modes", {})
        if modes:
            seen = {mode["id"] for mode in modes.get("availableModes", [])}
            assert modes["currentModeId"] != "bypassApprovals"
    assert "bypassApprovals" in seen, "the refused mode must stay advertised-and-refused"
