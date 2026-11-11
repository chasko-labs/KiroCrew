"""Muse Code's agent-config mirror (``muse-code-acp`` adapter + ``muse serve``).

Measured against @bex-co/muse-code-acp 0.11.0 driving host 1.4.14
(SDK 1.4.12), 2026-11-09, NOT re-inferred -- plus a second measurement on
host 1.4.15 (the ``muse`` CLI auto-updated), 2026-11-11, which REGRESSED the
relayed-MCP path below. Every claim below names the probe that produced it;
anything unmeasured is marked open.

Wire facts:
* ``initialize`` answers protocolVersion 1; prompt image+embeddedContext;
  mcpCapabilities ``{"http": true}`` ONLY; loadSession true; session
  list/close/resume/fork; interactivePermissions true.
* ``session/new`` REQUIRES cwd + mcpServers. A stdio-shaped element is
  refused with ``-32602`` (expects ``url``): unlike opencode, this
  adapter does NOT tolerate untagged stdio members. The accepted shape is
  ``{name, type: "http", url, headers: []}`` (headers array required).
* ``session/load`` needs the full params and fails pre-turn ("not found
  in the muse session store" — the store write happens on the first
  turn); post-turn load succeeds and echoes the mode inventory.
* HOST 1.4.15 REGRESSION (2026-11-11,
  ``test/fixtures/acp_frames/muse/member-dispatch-no-dial-measured.jsonl``,
  driven by ``scripts/capture_muse_member_dispatch.py --mount stub``):
  ``session/new`` still ACCEPTS the relayed ``{name, type: http, url,
  headers: []}`` element, but ``muse serve`` never dials it -- the relay
  child logged zero MCP methods across the settle wait and the whole turn
  (STUB_LOG), the model listed native tools only ("NO TOOL FOUND"), no
  ``session/request_permission`` fired. Member_thread_tools therefore stays
  unclaimed: the H6 round trip (mount, call, gate, answer) cannot complete
  while serve does not connect. Re-run the drive after a host or adapter
  change before claiming anything on this path.
* Modes: ``default`` ("On request" — approvals offered through ACP
  permission requests unless the native policy settles them),
  ``readOnly``, ``plan``, ``bypassApprovals`` (auto-approve — NEVER
  selected, same bar as goose ``auto``), ``rejectApprovals``.
* Permission frames: low-risk ops auto-settle with no frame (measured:
  /tmp file write, ~/.ssh read — both completed, file verified). The
  ``session/requestPermission`` plumbing is in the adapter and forwards
  to the client; a live escalation frame was OBSERVED for an MCP tool
  call (``mcp__stub__stub_ping``) via the relay below.
* MCP via :mod:`kiro_crew.mcp_http_relay`: a stdio stub behind the relay
  was listed, called by the model, and permission-gated end to end.
  stdio travels ONLY through the relay — never sent raw.
"""

from __future__ import annotations

import logging
from typing import Any, Mapping

from kiro_crew.acp_backends import ACP_BACKEND_MUSE
from kiro_crew.providers.mirrors.base import (
    RESOURCES_NO_READER,
    AgentConfigMirror,
    Concern,
    Disposition,
    Ruling,
)

logger = logging.getLogger(__name__)

#: Adapter + host releases every fact below was measured against.
MUSE_VERIFIED_ADAPTER = "0.11.0"
MUSE_VERIFIED_HOST = "1.4.14"
MUSE_VERIFIED_SDK = "1.4.12"

#: npm installer for the adapter (mirrors opencode's pinned-major style;
#: re-measure before raising).
MUSE_ADAPTER_INSTALL = "npm i -g @bex-co/muse-code-acp@0"

#: Permission mode Crew selects. ``default`` routes escalations through ACP;
#: ``bypassApprovals`` is refused the way goose ``auto`` is.
MUSE_PERMISSION_MODE = "default"
MUSE_REFUSED_MODES = ("bypassApprovals",)


class MuseCodeMirror(AgentConfigMirror):
    """Projects the agent spec onto the Muse ACP adapter."""

    backend: str = ACP_BACKEND_MUSE

    def rulings(self) -> Mapping[Concern, Ruling]:
        return {
            Concern.MCP_SERVERS: Ruling(
                Disposition.TRANSLATED,
                "stdio servers travel through kiro_crew.mcp_http_relay as "
                "{name, type: http, url, headers: []} (measured on host "
                "1.4.14: raw stdio refused -32602; relayed stub listed, "
                "called, permission-gated). REGRESSED on host 1.4.15 "
                "(2026-11-11): session/new still accepts the element but "
                "serve never dials it -- see the module docstring. HTTP "
                "servers pass through unchanged.",
            ),
            Concern.TOOL_ALLOWLIST: Ruling(
                Disposition.DELIVERED,
                "tools array is honoured at prompt time (measured: "
                "no-tools turn completed with tools available and uncalled).",
            ),
            Concern.DENIED_TOOLS: Ruling(
                Disposition.WITHHELD,
                "no per-tool deny channel measured on this adapter; "
                "disabled tools are withheld by not mounting their server. "
                "OPEN: re-check if the adapter gains deny rules.",
            ),
            Concern.AUTO_APPROVE: Ruling(
                Disposition.WITHHELD,
                "bypassApprovals mode is never selected (same bar as goose "
                "auto); default mode auto-settles only low-risk ops natively.",
            ),
            Concern.MODEL: Ruling(
                Disposition.WITHHELD,
                "model selection stays with the Muse subscription/session; "
                "no model id is sent. Since 2026-11-11 the session/new "
                "result ADVERTISES a model config option (current "
                "muse-spark-1.2, member-dispatch-no-dial-measured.jsonl), "
                "but no set has been measured. OPEN: drive "
                "session/set_config_option with configId model and verify "
                "pinning before claiming.",
            ),
            Concern.MODEL_ALLOWLIST: Ruling(
                Disposition.WITHHELD,
                "same as MODEL, plus the advertised allowlist holds exactly "
                "one entry (muse-spark-1.2). No allowlist channel measured.",
            ),
            Concern.PERMISSION_MODE: Ruling(
                Disposition.TRANSLATED,
                "Crew selects default (On request); escalations arrive as "
                "session/requestPermission (measured on MCP tool call). "
                "bypassApprovals refused.",
            ),
            Concern.PROMPT: Ruling(
                Disposition.WITHHELD,
                "not a mirror concern on any backend: the prompt reaches the "
                "harness as ordinary prompt text (prompt blocks incl. embedded "
                "text measured working), not through the spec projection.",
            ),
            Concern.RESOURCES: Ruling(
                Disposition.WITHHELD,
                RESOURCES_NO_READER,
            ),
            Concern.HOOKS: Ruling(
                Disposition.WITHHELD,
                "no hooks channel measured on this adapter.",
            ),
            Concern.TOOLS_SETTINGS: Ruling(
                Disposition.WITHHELD,
                "no toolsSettings channel measured; Crew's own gate tiers "
                "still read permission requests as on every backend.",
            ),
        }

    def session_params(self, agent: str | None, **kwargs: Any) -> dict[str, Any]:
        """Wire face: nothing beyond the shared array (relayed by the client).

        The stdio->relay rewrite happens on the spawn path that owns the
        relay lifecycles, not here: this method stays a synchronous
        in-memory read per the shared-call-site rule.
        """
        return {}
