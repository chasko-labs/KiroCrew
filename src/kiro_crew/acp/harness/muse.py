"""Muse Code, launched one process per session by ``AcpClient``.

Meta's Muse Code is reached through the community npm adapter ``muse-code-acp``
(``@bex-co/muse-code-acp``): a Node stdio server that drives the ``muse`` CLI
and translates ACP onto its operations. Launching it is three facts, all here:
where its entry script is (the shared Node-adapter ladder, cached for the
gateway's life), which version is installed (read before the spawn for the
install probe and diagnostics), and the native ``muse`` binary the adapter is
pointed at through ``MUSE_CODE_EXECUTABLE``.

Permission needs no seed and no loaded gate: the adapter asks per approval
itself, as host-offered ACP choices that fail closed on cancel or stale
replies, so Crew's job is answering the ``session/request_permission`` frames
it emits -- the measured ``test/fixtures/acp_frames/muse/`` corpus carries one.
See :data:`kiro_crew.agent_sdk.backends_routing.Routing.AGENT_SPEC`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from kiro_crew import model_registry
from kiro_crew.acp import launch as launch_mod
from kiro_crew.acp.harness.base import ProcessAdapter, SpawnContext, SpawnPlan
from kiro_crew.acp.transport_errors import AcpError
from kiro_crew.agent_sdk.backends import (
    ACP_BACKEND_MUSE,
    ACP_BACKEND_NODE_ADAPTER_PACKAGES,
    ACP_BACKEND_PROCESS_NAMES,
    ACP_BACKENDS_AGENT_STARTED_TURNS,
    NODE_ADAPTER_ENTRY_SEGMENTS,
    model_registry_namespace,
)
from kiro_crew.env import describe_search_path

# The client's logger name: these lines have always been filed under it.
logger = logging.getLogger("kiro_crew.acp.client")

__all__ = [
    "MUSE_ACP_BIN",
    "MUSE_ACP_NPM_PKG",
    "MUSE_CODE_BIN",
    "MUSE_INSTALL_COMMAND",
    "MUSE_CODE_INSTALL_REMEDY",
    "MuseLaunch",
]

MUSE_ACP_BIN = ACP_BACKEND_PROCESS_NAMES[ACP_BACKEND_MUSE]
# npm package that provides the muse-code-acp binary. Install it with
# ``npm i -g @bex-co/muse-code-acp`` (or add it as a project dependency);
# resolution also accepts a copy under a project-local ``node_modules`` so no
# global install is strictly required.
MUSE_ACP_NPM_PKG = ACP_BACKEND_NODE_ADAPTER_PACKAGES[ACP_BACKEND_MUSE]
# Entry script relative to the installed package directory (its package.json
# "bin" field: ``{"muse-code-acp": "dist/index.js"}``). Locates a copy under a
# project ``node_modules``.
_MUSE_ACP_PKG_ENTRY = Path(MUSE_ACP_NPM_PKG, *NODE_ADAPTER_ENTRY_SEGMENTS)
# The adapter's own import of the ACP SDK, the completeness check every Node
# adapter shares (see ``acp.launch.ACP_SDK_DEP_MARKER``).
_MUSE_ACP_DEP_MARKER = launch_mod.ACP_SDK_DEP_MARKER
# The adapter's documented floors: Node 22+ (its package ``engines``) and a
# separately installed ``muse`` with ``serve`` support (verified host 1.1.1 per
# the adapter README; measured here against 1.4.14).
MUSE_NODE_MIN_VERSION = (22, 0, 0)
# On-disk name of the Muse backend CLI. The adapter drives the installed
# ``muse`` executable -- it does not bundle one -- so the probe names it as its
# own component, the claude shape: having the adapter without the agent is a
# distinguishable half-install with its own remedy.
MUSE_CODE_BIN = "muse"
# Override env vars, read first by each resolver so an operator pin always wins.
_ENV_MUSE_ACP_BIN = "MUSE_CODE_ACP_BIN"
_ENV_MUSE_CODE_EXECUTABLE = "MUSE_CODE_EXECUTABLE"
# One command installs the adapter half; the agent half is Meta's own
# installer, so the probe suggests it by name rather than by command.
MUSE_INSTALL_COMMAND = f"npm i -g {MUSE_ACP_NPM_PKG}"
MUSE_CODE_INSTALL_REMEDY = (
    "install Muse Code separately (see https://dev.meta.ai/docs/muse-code/) "
    "and run `muse login`, or point MUSE_CODE_EXECUTABLE at its binary"
)
# Headless alternative to the browser login: the adapter process inherits its
# environment, so a key here reaches the host with no file changes.
MUSE_API_KEY_ENV = "META_API_KEY"

# Cache the PATH with the resolution result. A failed resolve is cached too, so
# recomputing PATH at the error site could report directories that were never searched.
_muse_acp_argv_cache: tuple[list[str] | None, str] | object = launch_mod._UNRESOLVED
_muse_bin_cache: tuple[str | None, str] | object = launch_mod._UNRESOLVED


def _resolve_vendored_muse_acp(pkg_dir: Path | None = None) -> str | None:
    """Return the path to a vendored muse-code-acp entry script, or None.

    The muse spelling of the ONE shared check,
    :func:`~kiro_crew.acp.launch._vendored_adapter_entry`:
    ``<root>/@bex-co/muse-code-acp/dist/index.js`` under each candidate
    ``node_modules`` root, accepted only when Node could import the adapter's
    dependency from the entry's real location. *pkg_dir* is threaded through so
    tests can inject a fake package layout.
    """
    return launch_mod._vendored_adapter_entry(
        _MUSE_ACP_PKG_ENTRY, _MUSE_ACP_DEP_MARKER, pkg_dir=pkg_dir
    )


def _resolve_muse_acp_bin() -> tuple[list[str] | None, str]:
    """Find the muse-code-acp Node entry script and its searched PATH.

    The shared ladder (:func:`~kiro_crew.acp.launch._resolve_node_adapter_argv`) with
    this adapter's three parameters; ``MUSE_CODE_ACP_BIN`` is the override.
    """
    return launch_mod._resolve_node_adapter_argv(
        bin_name=MUSE_ACP_BIN,
        override_env=_ENV_MUSE_ACP_BIN,
        vendored_entry=_resolve_vendored_muse_acp,
    )


def _resolve_muse_bin() -> tuple[str | None, str]:
    """Find the ``muse`` CLI binary the adapter drives, and the searched PATH.

    The adapter does not search PATH itself, so this resolution is required even
    when the host has ``muse`` installed.

    Resolution order:
      1. ``MUSE_CODE_EXECUTABLE`` env var (explicit override; honoured as-is).
      2. ``mise which muse`` (respects MISE_DATA_DIR and all mise config).
      3. Augmented PATH (``env.augmented_path`` — includes mise/nvm/fnm/volta
         shims and the npm global bin), so a non-login launchd/systemd gateway
         still finds an installed ``muse``.
    """
    # Read through the client at call time: its unit tests rebind
    # ``kiro_crew.acp.client.subprocess_mod`` under ``_mise_which``, and the client's
    # ``augmented_path`` for the directories searched.
    from kiro_crew.acp.client import _mise_which, augmented_path

    override = os.environ.get(_ENV_MUSE_CODE_EXECUTABLE)
    if override and Path(override).is_file():
        return override, "(MUSE_CODE_EXECUTABLE)"

    search_path = ""
    mise_resolved = _mise_which(MUSE_CODE_BIN)
    if mise_resolved:
        return mise_resolved, "(mise)"

    search_path = augmented_path(os.environ.get("PATH", ""))
    found = shutil.which(MUSE_CODE_BIN, path=search_path)
    return found, search_path


def _muse_adapter_installed_version(argv: list[str]) -> str:
    """The ``version`` in the package.json of the muse-code-acp *argv* runs, or ``""``.

    Read before spawn for the install probe and diagnostics. Only a manifest
    named :data:`MUSE_ACP_NPM_PKG` counts; a wrapper or an override that names
    something else answers ``""``. Blocking (small file reads); callers run it
    off the loop. The handshake's ``agentInfo.version`` is still checked before
    the first prompt.
    """
    for element in argv:
        try:
            here = Path(os.path.realpath(element))
        except (OSError, ValueError):
            continue
        if not here.is_file():
            continue
        shim_roots = [here.parent / "node_modules"]
        if here.parent.name == ".bin":
            shim_roots.append(here.parent.parent)
        candidates = [root / MUSE_ACP_NPM_PKG / "package.json" for root in shim_roots]
        candidates += [d / "package.json" for d in list(here.parents)[:4]]
        for manifest in candidates:
            try:
                if manifest.stat().st_size > 1 << 20:
                    continue
                data = json.loads(manifest.read_text(encoding="utf-8"))
            except (OSError, ValueError, RecursionError):
                continue
            if isinstance(data, dict) and data.get("name") == MUSE_ACP_NPM_PKG:
                version = data.get("version")
                return version.strip() if isinstance(version, str) else ""
    return ""


class MuseLaunch(ProcessAdapter):
    """muse-code-acp: a Node adapter that asks per approval itself."""

    backend = ACP_BACKEND_MUSE

    async def resolve_spawn(self, ctx: SpawnContext) -> SpawnPlan:
        """Resolve the adapter, read its version, then warm the session it serves.

        The session's model is folded onto the exact spelling the adapter
        advertised (from the persisted provider-model cache warmed by a prior
        session), so a model the static registry does not carry still resolves
        to the provider- and profile-qualified id the backend serves. Done first
        so the MCP warm below carries the same session the wire will. No-op on a
        cold cache (first-ever session), which is why the model is folded AGAIN
        after ``session/new`` has warmed it.

        No settings seed: unlike claude-agent-acp this adapter takes no
        permissions seed from Crew -- its Default mode already offers every
        approval as a host-offered ACP choice -- so there is nothing to write
        before the handshake and nothing to remove at teardown.
        """
        global _muse_acp_argv_cache  # noqa: PLW0603
        session = ctx.session
        assert session is not None, "muse-code-acp is launched for one session"
        session._model = model_registry.resolve_wire_model_id(
            session._model, model_registry_namespace(self.backend)
        )
        cached: tuple[list[str] | None, str] | object = _muse_acp_argv_cache
        if cached is launch_mod._UNRESOLVED:
            # Fenced on the resolution generation -- see ``acp.launch._resolution_generation``.
            epoch = launch_mod._resolution_epoch(self.backend)
            cached = await asyncio.to_thread(_resolve_muse_acp_bin)
            if launch_mod._resolution_epoch(self.backend) == epoch:
                _muse_acp_argv_cache = cached
        muse_argv, acp_search_path = cached if isinstance(cached, tuple) else (None, "")
        if not isinstance(muse_argv, list) or not muse_argv:
            raise AcpError(
                f"{MUSE_ACP_BIN} not found "
                f"({describe_search_path(acp_search_path)}). Install it with "
                f"'{MUSE_INSTALL_COMMAND}' (or add it as a project "
                f"dependency), or set {_ENV_MUSE_ACP_BIN} to its entry script."
            )
        argv: list[str] = muse_argv
        # The installed adapter's own version, read BEFORE the session warms:
        # diagnostics and the install probe both key off it. Off-loop: it reads files.
        session._agent_version_read = False
        session._muse_adapter_disk_version = await asyncio.to_thread(
            _muse_adapter_installed_version, argv
        )
        # Translate the agent spec into the session MCP array HERE: the array is
        # resolved off the loop before the handshake, and resolving it at the
        # session/new call site instead would put a disk read on the shared
        # construction path kiro-cli also walks (harness-parity H13).
        await session._prepare_session_mcp()
        # This adapter mounts only relayed HTTP elements, so every stdio server
        # in the warmed array gets a loopback relay in front of it now. A
        # respawn starts fresh (``_start_muse_relays`` discards first), and a
        # relay whose child cannot start leaves its server out of the wire
        # array, recorded as failed -- the session still starts with the rest.
        # Off-loop: process spawns.
        await session._start_muse_relays()
        # Bound here rather than in AcpClient.__init__ for the same reason: the
        # constructor is shared with kiro-cli.
        if self.backend in ACP_BACKENDS_AGENT_STARTED_TURNS:
            session._install_idle_reader()
        return SpawnPlan(
            argv=argv,
            spawn_label=launch_mod._adapter_spawn_label(
                argv,
                MUSE_ACP_BIN,
                pkg_entry=_MUSE_ACP_PKG_ENTRY,
                override_env=_ENV_MUSE_ACP_BIN,
            ),
            stderr_label=launch_mod._adapter_spawn_label(
                argv,
                "muse-acp",
                pkg_entry=_MUSE_ACP_PKG_ENTRY,
                override_env=_ENV_MUSE_ACP_BIN,
            ),
        )

    async def prepare_spawn_env(
        self,
        env: dict[str, str],
        *,
        to_thread: Callable[..., Awaitable[Any]],
    ) -> None:
        """:meth:`apply_spawn_env` through the guarded hop: its lookup runs ``mise``."""
        await to_thread(self.apply_spawn_env, env)

    def apply_spawn_env(self, env: dict[str, str], *, spawned_binary: str | None = None) -> None:
        """Point the adapter at a ``muse`` binary, unless one is named.

        The adapter drives an installed ``muse`` it does not vendor and does NOT
        search PATH for ``muse`` itself, so it is named explicitly. Only set when
        unset, so an operator override always wins. ``META_API_KEY`` passes
        through untouched when the operator sets it: it is the headless
        alternative to the browser login.
        """
        if env.get(_ENV_MUSE_CODE_EXECUTABLE):
            return
        muse_exe, _searched = _resolve_muse_bin()
        global _muse_bin_cache  # noqa: PLW0603
        _muse_bin_cache = (muse_exe, _searched)
        if muse_exe:
            env[_ENV_MUSE_CODE_EXECUTABLE] = muse_exe
        else:
            logger.warning(
                "%s not found on PATH; the muse-code-acp adapter will "
                "fail to reach its host. %s.",
                MUSE_CODE_BIN,
                MUSE_CODE_INSTALL_REMEDY,
            )
