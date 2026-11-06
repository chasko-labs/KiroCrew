"""Personal Remote Control: the sign-in rule, the setting, the live stop, the CLI and route.

An API-key or external IdP sign-in cannot turn personal Remote Control on from the
dashboard, the terminal command or config; the person is told why; one-click off
stops a running relay without a restart and stays off across one; a personal Kiro
sign-in is unaffected. Nearby inputs that reach the same code are covered too:
sign-out, an account switch, a sign-in kind changing while it is on, an unknown
sign-in kind, an unreadable vault, and an enterprise build's lock.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
import pytest_asyncio
from relay_client_helpers import OWNER, TOKEN, FakeCrew, FakeRelay, StaticHostTokenSource

from kiro_crew.auth.store import KasToken, TokenStore
from kiro_crew.config.integration_sections import TunnelConfig
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.remote_control import guard as guard_mod
from kiro_crew.remote_control import policy, setting
from kiro_crew.remote_control.defaults import REMOTE_CONTROL_ON_BY_DEFAULT
from kiro_crew.remote_control.guard import RemoteControlGuard
from kiro_crew.remote_control.policy import Decision, allowed
from kiro_crew.testing.clock import ManualClock
from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS, async_wait_until, await_until

_FUTURE = datetime(2099, 1, 1, tzinfo=timezone.utc)


def _token(provider: str, identity: str = "builder_id", auth_method: str | None = None) -> KasToken:
    return KasToken(
        access_token="atk-secret",
        expires_at=_FUTURE,
        provider=provider,
        identity=identity,
        refresh_token="rtk-secret",
        auth_method=auth_method,
    )


@pytest.fixture
def vault(tmp_path, monkeypatch) -> TokenStore:
    """The gateway's vault, under tmp_path, with no ``KIRO_API_KEY`` in the env."""
    from kiro_crew.auth import bridge

    store = TokenStore(tmp_path / "vault")
    monkeypatch.setattr(bridge, "default_token_store", lambda: store)
    monkeypatch.delenv("KIRO_API_KEY", raising=False)
    # Crew's vault owns the sign-in and this is a personal Crew, unless a test says
    # otherwise: no test runs the real `kiro-cli whoami` or reads the real config.
    monkeypatch.setattr(policy, "_kiro_cli_owns_sign_in", lambda: False)
    monkeypatch.setattr(policy, "_is_team_crew", lambda: False)
    return store


@pytest.fixture
def config_file(tmp_path, monkeypatch) -> Path:
    path = tmp_path / "config.json"
    path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr("kiro_crew.config.loader.config_path", lambda: path)
    monkeypatch.setattr(
        "kiro_crew.config.loader.config_local_path", lambda: tmp_path / "local.json"
    )
    return path


def _stored(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8")).get("tunnel", {}).get("relay_enabled")


# ── the sign-in rule ─────────────────────────────────────────────────────────


class TestTheSignInRule:
    @pytest.mark.parametrize(
        "provider", ["BuilderId", "Google", "Github", "Enterprise", "Internal"]
    )
    def test_a_personal_kiro_sign_in_is_allowed(self, provider) -> None:
        assert allowed(_token(provider)) == (True, "")

    def test_an_api_key_is_refused(self) -> None:
        assert allowed(_token("ApiKey")) == (False, policy.REASON_API_KEY)

    def test_the_env_api_key_is_refused_when_the_vault_is_empty(self) -> None:
        assert allowed(None, env_api_key=True) == (False, policy.REASON_API_KEY)

    def test_a_stored_personal_sign_in_outranks_the_env_api_key(self) -> None:
        # The same precedence KasAuthProvider applies: the vault wins.
        assert allowed(_token("BuilderId"), env_api_key=True) == (True, "")

    def test_an_external_idp_is_refused_by_provider_or_auth_method(self) -> None:
        assert allowed(_token("ExternalIdp", "external_idp")) == (False, policy.REASON_EXTERNAL_IDP)
        # The auth method decides even under a provider that would otherwise pass.
        assert allowed(_token("Enterprise", auth_method="external_idp")) == (
            False,
            policy.REASON_EXTERNAL_IDP,
        )

    def test_an_unknown_sign_in_kind_is_refused(self) -> None:
        assert allowed(_token("SomethingNew")) == (False, policy.REASON_UNKNOWN_SIGN_IN)

    def test_no_sign_in_is_refused(self) -> None:
        assert allowed(None) == (False, policy.REASON_NOT_SIGNED_IN)

    def test_every_refusal_has_a_notice(self) -> None:
        for reason in (
            policy.REASON_API_KEY,
            policy.REASON_EXTERNAL_IDP,
            policy.REASON_NOT_SIGNED_IN,
            policy.REASON_UNKNOWN_SIGN_IN,
            policy.REASON_LOCKED,
        ):
            assert Decision(False, reason).notice
        assert Decision(True).notice == ""


class TestTheCurrentDecision:
    def test_reads_the_vault(self, vault) -> None:
        assert policy.current_decision() == Decision(False, policy.REASON_NOT_SIGNED_IN)
        vault.save(_token("BuilderId"))
        assert policy.current_decision() == Decision(True)

    def test_an_external_idp_in_the_vault(self, vault) -> None:
        vault.save(_token("ExternalIdp", "external_idp", auth_method="external_idp"))
        assert policy.current_decision().reason == policy.REASON_EXTERNAL_IDP

    def test_the_env_api_key(self, vault, monkeypatch) -> None:
        monkeypatch.setenv("KIRO_API_KEY", "ksk-test")
        assert policy.current_decision().reason == policy.REASON_API_KEY

    def test_an_unreadable_vault_refuses(self, monkeypatch) -> None:
        from kiro_crew.auth import bridge

        def _broken() -> TokenStore:
            raise OSError("vault unreadable")

        monkeypatch.setattr(bridge, "default_token_store", _broken)
        assert policy.current_decision().reason == policy.REASON_UNKNOWN_SIGN_IN

    def test_an_enterprise_lock_refuses_before_the_vault(self, vault, monkeypatch) -> None:
        vault.save(_token("BuilderId"))
        monkeypatch.setattr("kiro_crew.edition_locks.local_enable_locked", lambda _c: True)
        assert policy.current_decision().reason == policy.REASON_LOCKED

    def test_the_snapshot_read_pairs_the_verdict_with_its_markers(self, vault) -> None:
        vault.bump_signin_generation()
        vault.save(_token("BuilderId"))
        decision, gen = policy.current_decision_with_generation()
        assert decision == Decision(True)
        assert gen == policy.sign_in_generation()

    @pytest.mark.parametrize("snapshot", [False, True])
    def test_a_team_crew_is_never_refused(self, vault, monkeypatch, snapshot) -> None:
        # A team Crew leases with its own crew API key, so the personal rule skips it.
        monkeypatch.setenv("KIRO_API_KEY", "ksk-test")
        monkeypatch.setattr(policy, "_is_team_crew", lambda: True)
        decision = (
            policy.current_decision_with_generation()[0] if snapshot else policy.current_decision()
        )
        assert decision == Decision(True)

    def test_a_team_crew_still_honours_the_enterprise_lock(self, vault, monkeypatch) -> None:
        monkeypatch.setattr(policy, "_is_team_crew", lambda: True)
        monkeypatch.setattr("kiro_crew.edition_locks.local_enable_locked", lambda _c: True)
        assert policy.current_decision().reason == policy.REASON_LOCKED

    @pytest.mark.parametrize("snapshot", [False, True])
    def test_a_running_personal_relay_is_judged_on_its_own_settings(
        self, vault, monkeypatch, snapshot
    ) -> None:
        # config.json now says team (edited while the relay ran), but the running
        # relay still leases as a personal Crew: its own settings decide the rule.
        monkeypatch.setenv("KIRO_API_KEY", "ksk-test")
        monkeypatch.setattr(policy, "_is_team_crew", lambda: True)
        decide = policy.current_decision_with_generation if snapshot else policy.current_decision

        def verdict(**kwargs: Any) -> Decision:
            answer = decide(**kwargs)
            return answer[0] if isinstance(answer, tuple) else answer

        assert verdict(team=False) == Decision(False, policy.REASON_API_KEY)
        # Without a running relay's answer, config.json decides (a prospective enable).
        assert verdict() == Decision(True)

    def test_the_guard_judges_the_provider_it_guards(self, vault, monkeypatch) -> None:
        from kiro_crew.relay_client.provider import RelaySettings, RelayTunnelProvider

        monkeypatch.setenv("KIRO_API_KEY", "ksk-test")
        monkeypatch.setattr(policy, "_is_team_crew", lambda: True)
        personal = RelayTunnelProvider(RelaySettings(enabled=True, host_url="", crew_domain=""))
        guard = RemoteControlGuard(MagicMock(), personal, requested=True)
        assert guard._decide()[0] == Decision(False, policy.REASON_API_KEY)
        team = RelayTunnelProvider(
            RelaySettings(enabled=True, host_url="", crew_domain="", lease_token_type="API_KEY")
        )
        monkeypatch.setattr(policy, "_is_team_crew", lambda: False)
        guard = RemoteControlGuard(MagicMock(), team, requested=True)
        assert guard._decide()[0] == Decision(True)

    @pytest.mark.parametrize(
        ("pin", "expected", "runs"),
        [
            (("/opt/kiro/bin/kiro-cli", False), {"account_type": "BuilderId"}, True),
            ((None, True), None, False),  # installed only on PATH: unknown, not run
            ((None, False), {}, False),  # not installed: no sign-in
        ],
        ids=["pinned", "path-only", "absent"],
    )
    def test_kiro_cli_whoami_runs_only_the_pinned_binary(
        self, monkeypatch, pin, expected, runs
    ) -> None:
        # The guard polls unattended: a kiro-cli planted on an agent-writable PATH
        # must never be the one that runs.
        from kiro_crew import kiro_cli
        from kiro_crew.cloud import login_target

        ran: list[object] = []

        def whoami(kiro_bin: object = None, **_: Any) -> dict:
            ran.append(kiro_bin)
            return {"account_type": "BuilderId"}

        monkeypatch.setattr(kiro_cli, "pin_kiro_cli", lambda: pin)
        monkeypatch.setattr(login_target, "discover_local_identity", whoami)
        assert policy._kiro_cli_identity() == expected
        assert ran == ([pin[0]] if runs else [])

    def test_team_crew_reads_the_lease_token_type(self, monkeypatch) -> None:
        from kiro_crew.config import loader

        for value, team in (("API_KEY", True), ("", False)):
            cfg = KiroCrewConfig()
            cfg.tunnel = dataclasses.replace(cfg.tunnel, relay_lease_token_type=value)
            monkeypatch.setattr(loader.KiroCrewConfig, "load", classmethod(lambda _c, c=cfg: c))
            assert policy._is_team_crew() is team

    @pytest.mark.parametrize(
        ("identity", "expected"),
        [
            ({"account_type": "BuilderId"}, (True, "")),
            ({"account_type": "SocialGoogle"}, (True, "")),
            ({"account_type": "IamIdentityCenter"}, (True, "")),
            ({"account_type": "ApiKey"}, (False, policy.REASON_API_KEY)),
            ({"account_type": "ExternalIdP"}, (False, policy.REASON_EXTERNAL_IDP)),
            ({"account_type": "SomethingNew"}, (False, policy.REASON_UNKNOWN_SIGN_IN)),
            ({}, (False, policy.REASON_NOT_SIGNED_IN)),
            (None, (False, policy.REASON_UNKNOWN_SIGN_IN)),
        ],
    )
    def test_a_kiro_cli_sign_in_is_classified_from_whoami(
        self, vault, monkeypatch, identity, expected
    ) -> None:
        # The kiro-cli route: Crew's vault is empty and kiro-cli owns the sign-in.
        monkeypatch.setattr(policy, "_kiro_cli_owns_sign_in", lambda: True)
        monkeypatch.setattr(policy, "_kiro_cli_identity", lambda: identity)
        assert policy.current_decision() == Decision(*expected)
        assert policy.current_decision_with_generation()[0] == Decision(*expected)

    def test_the_kiro_cli_route_markers_move_on_an_interval(self, vault, monkeypatch) -> None:
        # kiro-cli's store moves none of the vault's markers, so a kiro-cli account
        # switch is found by a marker that moves every KIRO_CLI_RECHECK_SECONDS.
        clock = ManualClock()
        clock.install(monkeypatch, policy)
        monkeypatch.setattr(policy, "_kiro_cli_owns_sign_in", lambda: True)
        monkeypatch.setattr(policy, "_kiro_cli_identity", lambda: {"account_type": "BuilderId"})
        first = policy.sign_in_generation()
        assert policy.current_decision_with_generation()[1] == first
        clock.advance(policy.KIRO_CLI_RECHECK_SECONDS)
        assert policy.sign_in_generation() != first

    def test_the_vault_route_markers_do_not_move_by_time(self, vault, monkeypatch) -> None:
        clock = ManualClock()
        clock.install(monkeypatch, policy)
        first = policy.sign_in_generation()
        clock.advance(10 * policy.KIRO_CLI_RECHECK_SECONDS)
        assert policy.sign_in_generation() == first

    @pytest.mark.asyncio
    async def test_the_guard_sees_a_kiro_cli_switch_after_the_interval(
        self, vault, monkeypatch
    ) -> None:
        clock = ManualClock()
        identity = {"account_type": "BuilderId"}
        clock.install(monkeypatch, policy)
        monkeypatch.setattr(policy, "_kiro_cli_owns_sign_in", lambda: True)
        monkeypatch.setattr(policy, "_kiro_cli_identity", lambda: dict(identity))
        monkeypatch.setattr("kiro_crew.sel.sel", lambda: MagicMock())
        manager = _FakeManager(_FakeProvider())
        guard = RemoteControlGuard(manager, manager.provider, requested=True)
        await guard.tick()
        identity["account_type"] = "ExternalIdP"  # `kiro-cli login` with a company IdP
        await guard.tick()
        assert manager.stops == 0, "the cached verdict stands inside the interval"
        clock.advance(policy.KIRO_CLI_RECHECK_SECONDS)
        await guard.tick()
        assert manager.stops == 1

    @pytest.mark.parametrize("snapshot", [False, True])
    @pytest.mark.parametrize(
        ("account_type", "expected"),
        [
            ("ApiKey", Decision(False, policy.REASON_API_KEY)),
            ("ExternalIdP", Decision(False, policy.REASON_EXTERNAL_IDP)),
            ("BuilderId", Decision(True)),
        ],
    )
    def test_a_leftover_vault_sign_in_does_not_override_kiro_cli(
        self, vault, monkeypatch, account_type, expected, snapshot
    ) -> None:
        vault.save(_token("BuilderId"))
        monkeypatch.setattr(policy, "_kiro_cli_owns_sign_in", lambda: True)
        monkeypatch.setattr(policy, "_kiro_cli_identity", lambda: {"account_type": account_type})
        decision = (
            policy.current_decision_with_generation()[0] if snapshot else policy.current_decision()
        )
        assert decision == expected

    @pytest.mark.parametrize("snapshot", [False, True])
    def test_an_unreadable_owner_config_refuses_and_rechecks(
        self, tmp_path, monkeypatch, snapshot
    ) -> None:
        from kiro_crew.auth import bridge, whoami

        store = TokenStore(tmp_path / "vault")
        store.save(_token("ApiKey"))
        monkeypatch.setattr(bridge, "default_token_store", lambda: store)
        monkeypatch.delenv("KIRO_API_KEY", raising=False)
        monkeypatch.setattr(policy, "_kiro_cli_identity", lambda: {"account_type": "BuilderId"})

        def unreadable() -> None:
            raise whoami.ConfigUnreadable("owner config unreadable")

        monkeypatch.setattr(whoami, "load_sign_in_owner_config", unreadable)
        clock = ManualClock()
        clock.install(monkeypatch, policy)
        decision = (
            policy.current_decision_with_generation(team=False)[0]
            if snapshot
            else policy.current_decision(team=False)
        )
        assert decision == Decision(False, policy.REASON_UNKNOWN_SIGN_IN)
        marker = policy._kiro_cli_marker()
        assert marker.startswith("kiro-cli:")
        clock.advance(policy.KIRO_CLI_RECHECK_SECONDS)
        assert policy._kiro_cli_marker() != marker

    def test_the_markers_move_on_sign_in_and_sign_out(self, vault) -> None:
        before = policy.sign_in_generation()
        vault.bump_signin_generation()
        after_in = policy.sign_in_generation()
        vault.bump_signout_generation()
        assert len({before, after_in, policy.sign_in_generation()}) == 3


# ── the setting ──────────────────────────────────────────────────────────────


class TestTheSetting:
    def test_the_default_is_the_one_named_value(self) -> None:
        assert TunnelConfig().relay_enabled is REMOTE_CONTROL_ON_BY_DEFAULT
        # Pending a product decision: off until it is made.
        assert REMOTE_CONTROL_ON_BY_DEFAULT is False

    def test_the_section_builder_reads_the_field_default_for_an_absent_key(
        self, monkeypatch
    ) -> None:
        # The builder states no default of its own: an omitted key resolves to the
        # field's default, so flipping REMOTE_CONTROL_ON_BY_DEFAULT flips the load.
        # Patched through the sections facade, which mirrors the write to every owner.
        from kiro_crew.config import section_builders, sections

        assert section_builders._build_tunnel_config({}).relay_enabled is False
        real = sections.field_default
        monkeypatch.setattr(
            sections,
            "field_default",
            lambda dto, key: True if key == "relay_enabled" else real(dto, key),
        )
        assert section_builders._build_tunnel_config({}).relay_enabled is True
        assert (
            section_builders._build_tunnel_config({"relay_enabled": False}).relay_enabled is False
        )

    def test_turning_on_is_refused_and_writes_nothing(
        self, vault, config_file, monkeypatch
    ) -> None:
        monkeypatch.setenv("KIRO_API_KEY", "ksk-test")
        with pytest.raises(setting.RemoteControlRefused) as refused:
            setting.turn_on()
        assert refused.value.decision.reason == policy.REASON_API_KEY
        assert json.loads(config_file.read_text()) == {}

    def test_a_personal_sign_in_turns_it_on(self, vault, config_file) -> None:
        vault.save(_token("BuilderId"))
        setting.turn_on()
        assert _stored(config_file) is True
        assert setting.read().effective is True

    def test_off_is_explicit_and_survives_a_reload(self, vault, config_file) -> None:
        vault.save(_token("BuilderId"))
        setting.turn_on()
        setting.turn_off()
        # An explicit false, so a later default flip cannot turn it back on.
        assert _stored(config_file) is False
        assert KiroCrewConfig.load().tunnel.relay_enabled is False

    def test_off_also_writes_an_overlay_that_names_the_key(
        self, vault, config_file, tmp_path
    ) -> None:
        overlay = tmp_path / "local.json"
        overlay.write_text(json.dumps({"tunnel": {"relay_enabled": True}}), encoding="utf-8")
        setting.turn_off()
        assert _stored(overlay) is False

    def test_off_leaves_an_overlay_that_does_not_name_it(
        self, vault, config_file, tmp_path
    ) -> None:
        overlay = tmp_path / "local.json"
        overlay.write_text(json.dumps({"agent": {"bot_name": "x"}}), encoding="utf-8")
        setting.turn_off()
        assert json.loads(overlay.read_text()) == {"agent": {"bot_name": "x"}}

    def test_a_hand_edited_true_with_a_refused_sign_in_is_not_effective(
        self, vault, config_file
    ) -> None:
        config_file.write_text(json.dumps({"tunnel": {"relay_enabled": True}}), encoding="utf-8")
        vault.save(_token("ExternalIdp", "external_idp", auth_method="external_idp"))
        view = setting.read()
        assert view.requested is True and view.effective is False
        assert view.decision.reason == policy.REASON_EXTERNAL_IDP


# ── the live stop ────────────────────────────────────────────────────────────


class _FakeProvider:
    def __init__(self, running: bool = True, state: str = "connected") -> None:
        self.is_running = running
        self.state = state

    async def ensure_available(self, *, install: bool = True) -> str:
        return self.state if self.is_running else "disabled"


class _FakeManager:
    def __init__(self, provider: _FakeProvider, *, start_works: bool = True) -> None:
        self.provider = provider
        self.start_works = start_works
        self.starts = 0
        self.stops = 0

    async def start(self) -> None:
        self.starts += 1
        if self.start_works:
            self.provider.is_running = True

    async def stop(self) -> None:
        self.stops += 1
        self.provider.is_running = False


class _Inputs:
    def __init__(self, requested: bool = True, decision: Decision = Decision(True)) -> None:
        self.guard: RemoteControlGuard | None = None
        self._requested = requested
        self.decision = decision
        self.gen = ("a", "")
        #: The markers the locked vault read reports, when it differs from ``gen``.
        self.locked_gen: tuple[str, str] | None = None
        self.decides = 0

    @property
    def requested(self) -> bool:
        return self._requested

    @requested.setter
    def requested(self, value: bool) -> None:
        # What the live config watcher dispatches to the guard's `tunnel` applier.
        self._requested = value
        if self.guard is not None:
            self.guard.on_config(_tunnel_change(value))

    def decide(self) -> tuple[Decision, tuple[str, str]]:
        self.decides += 1
        return self.decision, self.locked_gen or self.gen


def _tunnel_change(relay_enabled: bool) -> Any:
    cfg = KiroCrewConfig()
    cfg.tunnel = dataclasses.replace(cfg.tunnel, relay_enabled=relay_enabled)
    return SimpleNamespace(old=None, new=cfg, changed=frozenset({"tunnel.relay_enabled"}))


def _guard(inputs: _Inputs, manager: _FakeManager, **kwargs: Any) -> RemoteControlGuard:
    guard = RemoteControlGuard(
        manager,
        manager.provider,
        requested=inputs.requested,
        decide=inputs.decide,
        generation=lambda: inputs.gen,
        **kwargs,
    )
    inputs.guard = guard
    return guard


@pytest.mark.asyncio
class TestTheGuard:
    async def test_a_running_relay_is_left_alone_while_on_and_allowed(self) -> None:
        inputs, manager = _Inputs(), _FakeManager(_FakeProvider())
        guard = _guard(inputs, manager)
        await guard.tick()
        await guard.tick()
        assert (manager.starts, manager.stops) == (0, 0)

    async def test_turning_it_off_stops_the_relay_at_once(self) -> None:
        inputs, manager = _Inputs(), _FakeManager(_FakeProvider())
        guard = _guard(inputs, manager)
        await guard.tick()
        inputs.requested = False
        await guard.tick()
        assert manager.stops == 1 and not manager.provider.is_running

    async def test_a_sign_in_that_stops_being_allowed_stops_the_relay(self) -> None:
        # The account switched to an API key while Remote Control was on.
        inputs, manager = _Inputs(), _FakeManager(_FakeProvider())
        guard = _guard(inputs, manager)
        await guard.tick()
        inputs.decision = Decision(False, policy.REASON_API_KEY)
        await guard.tick()
        assert manager.stops == 0, "a cached verdict is used until the markers move"
        inputs.gen = ("b", "")
        await guard.tick()
        assert manager.stops == 1

    async def test_a_stop_for_a_refused_sign_in_is_audited(self, monkeypatch) -> None:
        audit = MagicMock()
        monkeypatch.setattr("kiro_crew.sel.sel", lambda: audit)
        inputs, manager = _Inputs(), _FakeManager(_FakeProvider())
        guard = _guard(inputs, manager)
        await guard.tick()
        inputs.decision, inputs.gen = Decision(False, policy.REASON_API_KEY), ("b", "")
        await guard.tick()
        assert manager.stops == 1
        kwargs = audit.log_api_access.call_args.kwargs
        assert (kwargs["operation"], kwargs["outcome"]) == ("tunnel.stop_enforced", "denied")
        assert kwargs["resources"] == "remote_control_sign_in=api_key"

    async def test_turning_it_off_is_not_audited_as_a_refusal(self, monkeypatch) -> None:
        audit = MagicMock()
        monkeypatch.setattr("kiro_crew.sel.sel", lambda: audit)
        inputs, manager = _Inputs(), _FakeManager(_FakeProvider())
        guard = _guard(inputs, manager)
        await guard.tick()
        inputs.requested = False
        await guard.tick()
        assert manager.stops == 1
        audit.log_api_access.assert_not_called()

    async def test_off_stops_the_relay_without_reading_the_sign_in(self) -> None:
        # On the kiro-cli route a sign-in read is `kiro-cli whoami`, up to 15 s.
        inputs, manager = _Inputs(), _FakeManager(_FakeProvider())
        guard = _guard(inputs, manager)
        await guard.tick()
        inputs.requested = False
        inputs.gen = ("moved", "")  # would force a fresh read if one were made
        decides = inputs.decides
        await guard.tick()
        assert manager.stops == 1
        assert inputs.decides == decides

    async def test_sign_out_stops_the_relay(self) -> None:
        inputs, manager = _Inputs(), _FakeManager(_FakeProvider())
        guard = _guard(inputs, manager)
        await guard.tick()
        inputs.decision, inputs.gen = Decision(False, policy.REASON_NOT_SIGNED_IN), ("a", "out")
        await guard.tick()
        assert manager.stops == 1

    async def test_the_vault_is_read_only_when_the_markers_move(self) -> None:
        inputs, manager = _Inputs(), _FakeManager(_FakeProvider())
        guard = _guard(inputs, manager)
        for _ in range(5):
            await guard.tick()
        assert inputs.decides == 1

    async def test_turning_it_back_on_does_not_start_it_without_a_restart(self) -> None:
        # `relay_enabled` is restart-only for on: after off then on, the guard
        # leaves the relay down until the gateway restarts.
        inputs, manager = _Inputs(), _FakeManager(_FakeProvider())
        guard = _guard(inputs, manager)
        await guard.tick()
        inputs.requested = False
        await guard.tick()
        inputs.requested = True
        await guard.apply()
        await guard.tick()
        assert (manager.starts, manager.stops) == (0, 1)

    async def test_a_stop_it_did_not_make_is_never_undone(self) -> None:
        # The gateway's shutdown stops the relay; the inputs did not change.
        inputs, manager = _Inputs(), _FakeManager(_FakeProvider())
        guard = _guard(inputs, manager)
        await guard.tick()
        await manager.stop()
        for _ in range(3):
            await guard.tick()
        assert manager.starts == 0

    async def test_a_boot_refusal_is_not_started_when_the_sign_in_becomes_allowed(self) -> None:
        # Booted with an external IdP: the provider refused, nothing runs.
        inputs = _Inputs(decision=Decision(False, policy.REASON_EXTERNAL_IDP))
        manager = _FakeManager(_FakeProvider(running=False))
        guard = _guard(inputs, manager)
        await guard.tick()
        await guard.tick()
        assert (manager.starts, manager.stops) == (0, 0)
        # The person signs in with their own Kiro account: the relay connects after a
        # restart, never from the guard.
        inputs.decision, inputs.gen = Decision(True), ("b", "")
        await guard.tick()
        assert manager.starts == 0

    async def test_apply_after_a_watcher_refresh_stops_a_lagging_off(
        self, config_file, monkeypatch
    ) -> None:
        # One-click off wrote false before the watcher's own poll saw it; the
        # handler's refresh_now() makes the watcher adopt it before apply().
        from kiro_crew.config import live

        monkeypatch.setattr(live, "_WATCH", None)
        config_file.write_text(json.dumps({"tunnel": {"relay_enabled": True}}), encoding="utf-8")
        manager = _FakeManager(_FakeProvider())
        guard = RemoteControlGuard(
            manager,
            manager.provider,
            decide=lambda: (Decision(True), ("a", "")),
            generation=lambda: ("a", ""),
        )
        guard.subscribe()
        try:
            await live.watch().refresh_now()
            await guard.tick()
            setting.turn_off()
            assert manager.stops == 0, "the guard reads no config file of its own"
            await live.watch().refresh_now()
            await guard.apply()
            assert (manager.starts, manager.stops) == (0, 1)
        finally:
            await guard.stop()

    async def test_an_off_the_watcher_adopted_before_install_reaches_the_guard(
        self, config_file, monkeypatch
    ) -> None:
        # `kirocrew remote-control off` landed while the boot start read the
        # sign-in: the watcher adopted false before the guard subscribed.
        from kiro_crew.config import live

        monkeypatch.setattr(live, "_WATCH", None)
        config_file.write_text(json.dumps({"tunnel": {"relay_enabled": False}}), encoding="utf-8")
        # The first load rewrites the file (schema stamp); the second settles the
        # fingerprint, as the running watcher's next poll would.
        await live.watch().refresh_now()
        await live.watch().refresh_now()
        manager = _FakeManager(_FakeProvider())
        guard = RemoteControlGuard(
            manager,
            manager.provider,
            decide=lambda: (Decision(True), ("a", "")),
            generation=lambda: ("a", ""),
        )
        guard.subscribe()
        try:
            await guard.tick()
            assert manager.stops == 1
        finally:
            await guard.stop()

    async def test_racing_writes_end_on_the_one_that_landed_last(self) -> None:
        # An enable and a disable race; the disable is the last one the watcher
        # applied, and the enable's handler is the one that calls apply() last.
        inputs, manager = _Inputs(), _FakeManager(_FakeProvider())
        guard = _guard(inputs, manager)
        await guard.tick()
        inputs.requested = False
        await guard.apply()  # the disable's handler
        await guard.apply()  # the stale enable's handler, after it
        for _ in range(3):
            await guard.tick()
        assert (manager.starts, manager.stops) == (0, 1)

    async def test_a_sign_in_landing_between_the_two_reads_is_seen_next_tick(self) -> None:
        # The cheap marker read sees the new sign-in's marker, but the locked
        # vault read still holds the old account: its verdict is cached under the
        # OLD markers, so the next tick re-reads.
        inputs, manager = _Inputs(), _FakeManager(_FakeProvider())
        guard = _guard(inputs, manager)
        await guard.tick()
        inputs.gen, inputs.locked_gen = ("b", ""), ("a", "")
        await guard.tick()
        assert manager.stops == 0
        inputs.decision, inputs.locked_gen = Decision(False, policy.REASON_EXTERNAL_IDP), None
        await guard.tick()
        assert manager.stops == 1

    async def test_a_sign_in_change_during_the_probe_discards_it(self) -> None:
        # The vault read ran under the old markers; a switch to an API key landed
        # while it ran. Its verdict must not be cached or acted on.
        inputs, manager = _Inputs(), _FakeManager(_FakeProvider())
        guard = _guard(inputs, manager)
        await guard.tick()
        stale = Decision(True)
        refused = Decision(False, policy.REASON_API_KEY)
        reads: list[str] = []

        def decide() -> tuple[Decision, tuple[str, str]]:
            reads.append(inputs.gen[0])
            if inputs.gen == ("b", ""):
                inputs.gen = ("c", "")  # the switch lands during this read
                return stale, ("b", "")
            return refused, inputs.gen

        guard._decide = decide
        inputs.gen = ("b", "")
        await guard.tick()
        assert reads == ["b", "c"], "the read the markers moved under is redone"
        assert manager.stops == 1, "the redone read, not the stale one, decides"
        assert guard.decision == refused

    async def test_a_sign_in_that_keeps_changing_leaves_the_relay_for_the_next_poll(
        self,
    ) -> None:
        inputs, manager = _Inputs(), _FakeManager(_FakeProvider())
        guard = _guard(inputs, manager)
        await guard.tick()
        count = {"n": 0}

        def decide() -> tuple[Decision, tuple[str, str]]:
            count["n"] += 1
            before = inputs.gen
            inputs.gen = (f"moved-{count['n']}", "")
            return Decision(False, policy.REASON_API_KEY), before

        guard._decide = decide
        inputs.gen = ("b", "")
        await guard.tick()
        assert count["n"] == guard_mod._SETTLE_ATTEMPTS
        assert (manager.starts, manager.stops) == (0, 0)
        assert guard.decision == Decision(True), "no unsettled verdict was cached"

    async def test_off_stops_the_relay_while_a_sign_in_read_is_in_flight(self) -> None:
        # A poll's `kiro-cli whoami` can take 15 s. It must not hold the lock, or
        # the stop a one-click off asks for waits behind it past DRAIN_SECONDS.
        import threading

        inputs, manager = _Inputs(), _FakeManager(_FakeProvider())
        guard = _guard(inputs, manager)
        await guard.tick()
        entered, released = threading.Event(), threading.Event()

        def stalled() -> tuple[Decision, tuple[str, str]]:
            entered.set()
            released.wait(FALLBACK_TIMEOUT_SECS)
            return Decision(True), inputs.gen

        guard._decide = stalled
        inputs.gen = ("b", "")
        poll = asyncio.create_task(guard.tick())
        try:
            await async_wait_until(entered.is_set)
            inputs.requested = False
            await asyncio.wait_for(guard.apply(), timeout=guard_mod.DRAIN_SECONDS / 2)
            assert manager.stops == 1
        finally:
            released.set()
            await poll
        assert manager.starts == 0, "the probe that outlived the off starts nothing"

    async def test_a_drained_off_stops_within_the_drain_bound_during_a_sign_in_read(
        self,
    ) -> None:
        # The route the one-click off takes: stop_after_drain -> apply -> tick. With
        # the relay idle it must stop well inside DRAIN_SECONDS even though a poll is
        # stuck in a sign-in read.
        import threading

        inputs, manager = _Inputs(), _FakeManager(_FakeProvider())
        guard = _guard(inputs, manager)
        guard._idle = lambda: True
        await guard.tick()
        entered, released = threading.Event(), threading.Event()

        def stalled() -> tuple[Decision, tuple[str, str]]:
            entered.set()
            released.wait(FALLBACK_TIMEOUT_SECS)
            return Decision(True), inputs.gen

        guard._decide = stalled
        inputs.gen = ("b", "")
        poll = asyncio.create_task(guard.tick())
        try:
            await async_wait_until(entered.is_set)
            inputs.requested = False
            guard.stop_after_drain()
            deferred = guard._deferred
            assert deferred is not None
            await asyncio.wait_for(asyncio.shield(deferred), timeout=guard_mod.DRAIN_SECONDS)
            assert manager.stops == 1 and not manager.provider.is_running
        finally:
            released.set()
            await poll
        assert manager.starts == 0

    async def test_on_off_on_during_a_sign_in_read_waits_on_nothing(
        self,
    ) -> None:
        # A poll is stuck in a sign-in read when the owner turns Remote Control off
        # and on again. Neither write may wait on that read, and when it finally
        # returns it must not stop the relay a second time or start it at all: on
        # takes a restart.
        import threading

        inputs, manager = _Inputs(), _FakeManager(_FakeProvider())
        guard = _guard(inputs, manager)
        await guard.tick()
        entered, released = threading.Event(), threading.Event()
        calls = {"n": 0}

        def decide() -> tuple[Decision, tuple[str, str]]:
            calls["n"] += 1
            if calls["n"] == 1:
                entered.set()
                released.wait(FALLBACK_TIMEOUT_SECS)
            return Decision(True), inputs.gen

        guard._decide = decide
        inputs.gen = ("b", "")
        poll = asyncio.create_task(guard.tick())
        try:
            await async_wait_until(entered.is_set)
            inputs.requested = False
            await asyncio.wait_for(guard.apply(), timeout=guard_mod.DRAIN_SECONDS / 2)
            assert (manager.starts, manager.stops) == (0, 1)
            inputs.requested = True
            await asyncio.wait_for(guard.apply(), timeout=guard_mod.DRAIN_SECONDS / 2)
            assert (manager.starts, manager.stops) == (0, 1)
        finally:
            released.set()
            await poll
        assert (manager.starts, manager.stops) == (0, 1), "the late read acts on nothing"
        assert not manager.provider.is_running

    async def test_off_still_tears_down_a_client_that_stopped_itself(self) -> None:
        # A client that stopped itself is left in its error state and
        # answers "unavailable", not "disabled". Off must still run the tunnel stop,
        # which closes the provider's token source and ingress.
        inputs = _Inputs()
        manager = _FakeManager(_FakeProvider(state="unavailable"))
        guard = _guard(inputs, manager)
        await guard.tick()
        inputs.requested = False
        await guard.tick()
        assert manager.stops == 1

    async def test_the_drain_is_bounded(self) -> None:
        # A request that never finishes (a stream left open) cannot keep it on.
        inputs, manager = _Inputs(), _FakeManager(_FakeProvider())
        guard = _guard(inputs, manager, idle=lambda: False, drain_seconds=0)
        await guard.tick()
        inputs.requested = False
        guard.stop_after_drain()
        guard.stop_after_drain()  # a second click while one is pending is one stop
        await async_wait_until(lambda: manager.stops == 1)
        assert not manager.provider.is_running and manager.stops == 1

    async def test_failed_stop_reservation_preserves_other_writers(self) -> None:
        inputs, manager = _Inputs(), _FakeManager(_FakeProvider())
        guard = _guard(inputs, manager)
        inputs.requested = False
        with guard.reserve_stop():
            with pytest.raises(OSError), guard.reserve_stop():
                raise OSError("write failed")
            assert guard.stop_pending
            await guard.tick()
            assert manager.stops == 0
        assert not guard.stop_pending
        await guard.tick()
        assert manager.stops == 1

    async def test_shutdown_cancels_a_pending_off(self) -> None:
        inputs, manager = _Inputs(), _FakeManager(_FakeProvider())
        guard = _guard(inputs, manager, idle=lambda: False, drain_seconds=30.0)
        inputs.requested = False
        guard.stop_after_drain()
        assert guard.stop_pending
        await guard.stop()
        assert not guard.stop_pending and manager.stops == 0

    async def test_relay_idle_reads_every_live_connection(self) -> None:
        busy, done = asyncio.Event(), asyncio.Event()
        done.set()
        provider = MagicMock()
        provider._client._live = {MagicMock(http_streams_idle=done)}
        assert guard_mod._relay_http_idle(provider) is True
        provider._client._live.add(MagicMock(http_streams_idle=busy))
        assert guard_mod._relay_http_idle(provider) is False
        provider._client = None
        assert guard_mod._relay_http_idle(provider) is True

    async def test_install_skips_a_provider_that_is_not_the_relay(self) -> None:
        assert guard_mod.install_for_relay(MagicMock(), MagicMock()) is None
        assert guard_mod.current_guard() is None


@pytest.mark.asyncio
async def test_one_click_off_closes_a_live_relay_connection(monkeypatch) -> None:
    """End to end over a fake relay: connected, then off, and the leg closes."""
    from kiro_crew import tunnel as tunnel_pkg
    from kiro_crew.platform import build_default_context, set_context
    from kiro_crew.relay_client.ingress import LoopbackIngress
    from kiro_crew.relay_client.protocol import WS_MESSAGE_MAX_BYTES
    from kiro_crew.relay_client.provider import RelaySettings, RelayTunnelProvider
    from kiro_crew.relay_client.token_source import HostCredential
    from kiro_crew.tunnel.manager import TunnelState
    from kiro_crew.tunnel.setup import setup_tunnel

    monkeypatch.setattr(policy, "current_decision", lambda **_: Decision(True))
    monkeypatch.setattr(tunnel_pkg, "_tunnel_public_url", "")
    async with FakeRelay() as relay, FakeCrew() as crew:
        cfg = KiroCrewConfig()
        cfg.tunnel = dataclasses.replace(cfg.tunnel, relay_enabled=True)
        # config.json names no relay; with no lease service the settings name
        # both the host URL and the crew domain, as an edition builds them.
        provider = RelayTunnelProvider(
            RelaySettings(
                enabled=True,
                host_url=relay.url,
                crew_domain="us-east-1.relay.example",
            ),
            token_source=StaticHostTokenSource(
                HostCredential(TOKEN, crew_kind="personal", owner_user_id=OWNER)
            ),
            ingress_factory=lambda: LoopbackIngress(
                "127.0.0.1",
                crew.port,
                gateway=crew.gateway,
                max_ws_message_bytes=WS_MESSAGE_MAX_BYTES,
            ),
            client_version="0.0.0-test",
        )
        set_context(dataclasses.replace(build_default_context(cfg), tunnel=provider))
        middleware = MagicMock()
        middleware._is_token_auth = True
        manager = await setup_tunnel(
            middlewares=[middleware],
            allowed_origins=set(),
            tunnel_name_mode="username",
            tunnel_name_override="",
            port=crew.port,
            log_api_access=MagicMock(),
        )
        assert manager is not None
        guard = RemoteControlGuard(
            manager,
            provider,
            decide=lambda: (Decision(True), ("a", "")),
            generation=lambda: ("a", ""),
        )
        try:
            leg = await relay.next_leg()
            await leg.handshake()
            await async_wait_until(lambda: manager.state is TunnelState.CONNECTED)
            await guard.tick()
            guard.on_config(_tunnel_change(False))
            await guard.apply()
            assert await leg.wait_closed() == 1001
            assert await provider.ensure_available() == "disabled"
            assert manager.state is TunnelState.STOPPED
        finally:
            await manager.stop()


@pytest.mark.asyncio
async def test_the_provider_refuses_to_start_for_a_refused_sign_in(monkeypatch) -> None:
    from kiro_crew.relay_client.provider import RelaySettings, RelayTunnelProvider

    asked: list[dict[str, Any]] = []

    def refused(**kwargs: Any) -> Decision:
        asked.append(kwargs)
        return Decision(False, policy.REASON_EXTERNAL_IDP)

    monkeypatch.setattr(policy, "current_decision", refused)
    audit = MagicMock()
    monkeypatch.setattr("kiro_crew.sel.sel", lambda: audit)
    cfg = KiroCrewConfig()
    cfg.tunnel = dataclasses.replace(cfg.tunnel, relay_enabled=True)
    factory = MagicMock()
    provider = RelayTunnelProvider(
        RelaySettings.from_config(cfg), ingress_factory=MagicMock, client_factory=factory
    )
    await provider.start()
    factory.assert_not_called()
    # Judged on the settings it would run with (personal), not a re-read of config.json.
    assert asked == [{"team": False}]
    # The refusal is in the security event log, with its reason.
    audit.log_api_access.assert_called_once()
    kwargs = audit.log_api_access.call_args.kwargs
    assert (kwargs["operation"], kwargs["outcome"]) == ("tunnel.start_denied", "denied")
    assert kwargs["resources"] == "remote_control_sign_in=external_idp"
    snapshot = provider.status_snapshot()
    assert snapshot is not None and snapshot["state"] == "error"
    assert "external identity provider" in snapshot["error"]
    assert await provider.ensure_available() == "disabled"


def _cli(*argv: str) -> argparse.Namespace:
    from kiro_crew.remote_control.cli import register_parser

    parser = argparse.ArgumentParser(prog="kirocrew")
    register_parser(parser.add_subparsers(dest="command"))
    return parser.parse_args(["remote-control", *argv])


class TestTheTerminalCommand:
    def test_on_is_refused_for_an_api_key(self, vault, config_file, monkeypatch, capsys) -> None:
        from kiro_crew.remote_control.cli import run

        monkeypatch.setenv("KIRO_API_KEY", "ksk-test")
        with pytest.raises(SystemExit) as exit_:
            run(_cli("on"))
        assert exit_.value.code == 1
        assert "API key" in capsys.readouterr().err
        assert json.loads(config_file.read_text()) == {}

    def test_on_then_off_for_a_personal_sign_in(self, vault, config_file, capsys) -> None:
        from kiro_crew.remote_control.cli import run

        vault.save(_token("BuilderId"))
        run(_cli("on"))
        assert "connects after Kiro Crew restarts" in capsys.readouterr().out
        assert _stored(config_file) is True
        run(_cli("off"))
        assert _stored(config_file) is False
        assert "stays off" in capsys.readouterr().out

    def test_status_names_the_refusal(self, vault, config_file, capsys) -> None:
        from kiro_crew.remote_control.cli import run

        config_file.write_text(json.dumps({"tunnel": {"relay_enabled": True}}), encoding="utf-8")
        vault.save(_token("ExternalIdp", "external_idp", auth_method="external_idp"))
        with pytest.raises(SystemExit):
            run(_cli("status", "--json"))
        out = json.loads(capsys.readouterr().out)
        assert out == {
            "requested": True,
            "allowed": False,
            "reason": "external_idp",
            "notice": policy.NOTICES["external_idp"],
            "effective": False,
        }

    def test_config_set_refuses_turning_it_on(
        self, vault, config_file, monkeypatch, capsys
    ) -> None:
        from kiro_crew import cli_config

        audit = MagicMock()
        monkeypatch.setattr(cli_config, "sel", lambda: audit)
        monkeypatch.setenv("KIRO_API_KEY", "ksk-test")
        args = argparse.Namespace(
            config_action="set",
            key="tunnel.relay_enabled",
            value="true",
            file=None,
            value_base64=None,
            local=False,
        )
        with pytest.raises(SystemExit) as exit_:
            cli_config._run_config_cmd(args)
        assert exit_.value.code == 1
        assert "API key" in capsys.readouterr().err
        assert json.loads(config_file.read_text()) == {}
        audit.log_api_access.assert_called_once()
        assert audit.log_api_access.call_args.kwargs["outcome"] == "denied"


# ── the dashboard route ──────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def client(monkeypatch, vault, config_file):
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    from kiro_crew.dashboard.handlers import remote_control as handler

    async def _owner(_request, _op):
        return None

    monkeypatch.setattr(handler, "require_owner_dashboard_request", _owner)
    monkeypatch.setattr(handler, "sel", lambda: MagicMock())
    app = web.Application()
    app.router.add_get("/api/remote-control", handler.api_remote_control_get)
    app.router.add_put("/api/remote-control", handler.api_remote_control_put)
    async with TestClient(TestServer(app)) as test_client:
        yield test_client


@pytest.mark.asyncio
class TestTheRoute:
    @pytest.fixture(autouse=True)
    def _fresh_config_watcher(self, _floor_monkeypatch) -> None:
        # The off handler reloads through the process watcher; give each test its own.
        from kiro_crew.config import live

        _floor_monkeypatch.setattr(live, "_WATCH", None)

    async def test_get_carries_the_reason_for_the_notice(
        self, client, config_file, monkeypatch
    ) -> None:
        config_file.write_text(json.dumps({"tunnel": {"relay_enabled": True}}), encoding="utf-8")
        monkeypatch.setenv("KIRO_API_KEY", "ksk-test")
        body = await (await client.get("/api/remote-control")).json()
        assert body["requested"] is True and body["allowed"] is False
        assert body["reason"] == "api_key" and body["notice"]
        assert set(body) == {"requested", "allowed", "reason", "notice", "effective", "running"}

    async def test_the_dashboard_cannot_turn_it_on(self, client, vault, config_file) -> None:
        # Even for an allowed sign-in: turning on is the terminal command's job.
        vault.save(_token("BuilderId"))
        resp = await client.put("/api/remote-control", json={"enabled": True})
        assert resp.status == 400
        assert (await resp.json())["code"] == "invalid_field_value"
        assert json.loads(config_file.read_text()) == {}

    async def test_a_relay_that_ended_in_error_is_not_reported_connected(
        self, client, vault, config_file, monkeypatch
    ) -> None:
        config_file.write_text(json.dumps({"tunnel": {"relay_enabled": True}}), encoding="utf-8")
        vault.save(_token("BuilderId"))
        provider = _FakeProvider(state="unavailable")
        guard = RemoteControlGuard(_FakeManager(provider), provider)
        monkeypatch.setattr(guard_mod, "_guard", guard)
        guard.subscribe()
        body = await (await client.get("/api/remote-control")).json()
        assert body["running"] is False
        # ...while the stop decision still sees a client to clean up.
        assert await guard.running() is True

    async def test_put_off_writes_false_and_stops_the_relay(
        self, client, vault, config_file, monkeypatch
    ) -> None:
        config_file.write_text(json.dumps({"tunnel": {"relay_enabled": True}}), encoding="utf-8")
        manager = _FakeManager(_FakeProvider())
        # The real disk reader, so apply() sees what the PUT stored.
        guard = RemoteControlGuard(
            manager,
            manager.provider,
            decide=lambda: (Decision(True), ("a", "")),
            generation=lambda: ("a", ""),
        )
        await guard.tick()
        monkeypatch.setattr(guard_mod, "_guard", guard)
        guard.subscribe()
        resp = await client.put("/api/remote-control", json={"enabled": False})
        body = await resp.json()
        assert resp.status == 200
        # The stop's own config load runs right after the answer. Read the file
        # once it is done: a raw read racing it failed with EACCES on Windows CI.
        await async_wait_until(lambda: manager.stops == 1)
        assert _stored(config_file) is False
        assert body["requested"] is False and body["running"] is False

    async def test_put_off_answers_before_the_relay_stops(
        self, client, vault, config_file, monkeypatch
    ) -> None:
        # The off request came through the relay: its answer is still open on the
        # relay connection, so stopping the relay first would lose it.
        config_file.write_text(json.dumps({"tunnel": {"relay_enabled": True}}), encoding="utf-8")
        manager = _FakeManager(_FakeProvider())
        relay_idle = {"value": False}
        guard = RemoteControlGuard(
            manager,
            manager.provider,
            decide=lambda: (Decision(True), ("a", "")),
            generation=lambda: ("a", ""),
            idle=lambda: relay_idle["value"],
        )
        await guard.tick()
        monkeypatch.setattr(guard_mod, "_guard", guard)
        guard.subscribe()
        resp = await client.put("/api/remote-control", json={"enabled": False})
        assert resp.status == 200
        body = await resp.json()
        assert body["requested"] is False and body["running"] is False
        assert manager.stops == 0, "the relay must outlive the off request's answer"
        assert guard.stop_pending
        # A poll tick meanwhile sees the setting off but leaves the stop to the drain.
        await guard.tick()
        assert manager.stops == 0
        relay_idle["value"] = True  # the answer went back over the relay
        await async_wait_until(lambda: manager.stops == 1)
        assert not guard.stop_pending and not manager.provider.is_running

    @pytest.mark.parametrize("cached", [True, False])
    async def test_put_off_answers_while_a_sign_in_read_stalls(
        self, client, vault, config_file, monkeypatch, cached
    ) -> None:
        # The off request came through the relay and the relay drains within
        # DRAIN_SECONDS, while `kiro-cli whoami` can stall for up to 15 s. The
        # answer must not wait on a fresh sign-in read, or the drain closes the
        # relay under it.
        import threading

        from kiro_crew.dashboard.handlers import remote_control as handler

        config_file.write_text(json.dumps({"tunnel": {"relay_enabled": True}}), encoding="utf-8")
        released = threading.Event()

        def stalled(**_: Any) -> Decision:
            released.wait(FALLBACK_TIMEOUT_SECS)
            return Decision(True)

        monkeypatch.setattr(policy, "current_decision", stalled)
        monkeypatch.setattr(setting, "current_decision", stalled)
        monkeypatch.setattr(handler, "OFF_DECISION_WAIT_SECONDS", 0, raising=False)
        manager = _FakeManager(_FakeProvider())
        relay_idle = {"value": False}
        guard = RemoteControlGuard(
            manager,
            manager.provider,
            decide=lambda: (Decision(True), ("a", "")),
            generation=lambda: ("a", ""),
            idle=lambda: relay_idle["value"],
        )
        if cached:
            await guard.tick()
        monkeypatch.setattr(guard_mod, "_guard", guard)
        guard.subscribe()
        try:
            resp = await asyncio.wait_for(
                client.put("/api/remote-control", json={"enabled": False}),
                timeout=guard_mod.DRAIN_SECONDS / 2,
            )
            body = await resp.json()
        finally:
            released.set()
        assert resp.status == 200
        assert _stored(config_file) is False
        assert manager.stops == 0, "answered while the relay is still draining"
        assert body["requested"] is False and body["running"] is False
        assert body["effective"] is False
        # The guard's cached verdict, or, before its first read, an unknown sign-in.
        if cached:
            assert body["allowed"] is True and body["reason"] == ""
        else:
            assert body["allowed"] is False and body["reason"] == policy.REASON_UNKNOWN_SIGN_IN
        relay_idle["value"] = True
        await async_wait_until(lambda: manager.stops == 1)

    async def test_poll_during_off_overlay_lock_preserves_the_response(
        self, client, config_file, tmp_path, monkeypatch
    ) -> None:
        from kiro_crew.config import loader

        config_file.write_text(json.dumps({"tunnel": {"relay_enabled": True}}), encoding="utf-8")
        overlay = tmp_path / "local.json"
        overlay.write_text(json.dumps({"agent": {"bot_name": "x"}}), encoding="utf-8")
        manager = _FakeManager(_FakeProvider())
        relay_idle = asyncio.Event()
        guard = RemoteControlGuard(
            manager,
            manager.provider,
            decide=lambda: (Decision(True), ("a", "")),
            generation=lambda: ("a", ""),
            idle=relay_idle.is_set,
            drain_seconds=FALLBACK_TIMEOUT_SECS,
        )
        await guard.tick()
        monkeypatch.setattr(guard_mod, "_guard", guard)
        guard.subscribe()
        loop = asyncio.get_running_loop()
        overlay_waiting = asyncio.Event()
        real_lock = loader._config_write_lock

        @contextmanager
        def observed_lock(path, *, wait=True):
            if path == overlay:
                loop.call_soon_threadsafe(overlay_waiting.set)
            with real_lock(path, wait=wait):
                yield

        monkeypatch.setattr(loader, "_config_write_lock", observed_lock)
        request_task = None
        try:
            # The overlay does not override relay_enabled, so the base write is
            # visible to polling while its writer waits for the overlay lock.
            with real_lock(overlay, wait=False):
                request_task = asyncio.create_task(
                    client.put("/api/remote-control", json={"enabled": False})
                )
                await await_until(overlay_waiting)
                assert _stored(config_file) is False
                assert not request_task.done()
                await guard.tick()
                assert manager.stops == 0, "polling must not close the unanswered off request"
                assert guard.stop_pending
                assert guard._deferred is None, "the drain starts only after persistence finishes"
            resp = await await_until(request_task)
            assert resp.status == 200
            assert (await resp.json())["requested"] is False
            assert manager.stops == 0
            assert json.loads(overlay.read_text(encoding="utf-8")) == {"agent": {"bot_name": "x"}}
            relay_idle.set()
            await async_wait_until(lambda: manager.stops == 1)
            assert not guard.stop_pending
        finally:
            if request_task is not None:
                await await_until(request_task)
            await guard.stop()

    @pytest.mark.parametrize("partial_write", [False, True])
    async def test_failed_off_write_releases_poll_suppression(
        self, client, config_file, monkeypatch, partial_write
    ) -> None:
        config_file.write_text(json.dumps({"tunnel": {"relay_enabled": True}}), encoding="utf-8")
        manager = _FakeManager(_FakeProvider())
        guard = RemoteControlGuard(
            manager,
            manager.provider,
            decide=lambda: (Decision(True), ("a", "")),
            generation=lambda: ("a", ""),
        )
        monkeypatch.setattr(guard_mod, "_guard", guard)
        guard.subscribe()
        real_off = setting.turn_off

        def failed_off():
            assert guard.stop_pending
            if partial_write:
                real_off()
            raise OSError("write failed")

        monkeypatch.setattr(setting, "turn_off", failed_off)
        try:
            resp = await client.put("/api/remote-control", json={"enabled": False})
            assert resp.status == 500
            assert not guard.stop_pending
            assert guard._deferred is None
            # The watcher's own poll (woken by the loader's write) adopts what landed.
            from kiro_crew.config import live

            await live.watch().refresh_now()
            await guard.tick()
            assert manager.stops == int(partial_write)
        finally:
            await guard.stop()

    async def test_put_needs_valid_json(self, client) -> None:
        resp = await client.put("/api/remote-control", data="{")
        assert resp.status == 400
        assert (await resp.json())["code"] == "invalid_json"

    @pytest.mark.parametrize("body", [{"enabled": "false"}, {"enabled": 0}, {}, []])
    async def test_put_needs_enabled_false(self, client, body) -> None:
        resp = await client.put("/api/remote-control", json=body)
        assert resp.status == 400
        assert (await resp.json())["code"] == "invalid_field_value"
