"""Which Kiro sign-ins may hold personal Remote Control, and what the person is told.

Personal Remote Control publishes this Crew to its owner's own Kiro account, so it
needs a sign-in that names a person. Two kinds do not:

* an API key (``KIRO_API_KEY``, or a stored credential whose provider is
  ``ApiKey``) is a bearer with no person behind it;
* an external identity provider sign-in (``auth_method`` or provider
  ``external_idp``) is an organisation's own IdP, which the lease service cannot
  bind to a personal Kiro account.

The rule is an allowlist, not a denylist: a provider this module does not know is
refused as ``unknown`` rather than let through, so a sign-in kind added later stays
off until someone decides it may hold Remote Control.

The rule is for PERSONAL Remote Control only. A team Crew
(``tunnel.relay_lease_token_type: "API_KEY"``) leases with its own crew API key,
which does not depend on the Kiro sign-in, so it is never refused here.

Where kiro-cli owns the sign-in, the sign-in is classified from
``kiro-cli whoami``'s ``accountType``, even if Crew's vault holds a credential.
An unreadable sign-in-owner config refuses as ``unknown``.

:func:`allowed` is pure. :func:`current_decision` reads the vault and config
(blocking IO, call it off the event loop), may run ``kiro-cli whoami`` on the
kiro-cli route, and never refreshes a token.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from kiro_crew.auth.store import KasToken

#: Providers whose sign-in names a person, so personal Remote Control may run.
PERSONAL_PROVIDERS: frozenset[str] = frozenset(
    {"BuilderId", "Google", "Github", "Enterprise", "Internal"}
)

#: Refusal codes. Each one has a sentence in :data:`NOTICES`.
REASON_API_KEY = "api_key"
REASON_EXTERNAL_IDP = "external_idp"
REASON_NOT_SIGNED_IN = "not_signed_in"
REASON_UNKNOWN_SIGN_IN = "unknown_sign_in"
REASON_LOCKED = "locked"

#: What the person reads when Remote Control is refused, keyed by reason code.
NOTICES: dict[str, str] = {
    REASON_API_KEY: (
        "Remote Control is off because Crew is signed in to Kiro with an API key. "
        "Sign in with your own Kiro account to use it."
    ),
    REASON_EXTERNAL_IDP: (
        "Remote Control is off because Crew is signed in to Kiro through an external "
        "identity provider. Sign in with your own Kiro account to use it."
    ),
    REASON_NOT_SIGNED_IN: "Remote Control is off because Crew is not signed in to Kiro.",
    REASON_UNKNOWN_SIGN_IN: (
        "Remote Control is off because Crew does not recognise this kind of Kiro " "sign-in."
    ),
    REASON_LOCKED: "Remote Control is managed by this build and cannot be turned on here.",
}


def notice_for(reason: str) -> str:
    """The sentence shown for *reason*, or ``""`` when nothing is refused."""
    return NOTICES.get(reason, "")


def allowed(token: Optional[KasToken], *, env_api_key: bool = False) -> tuple[bool, str]:
    """Whether *token* may hold personal Remote Control, and the refusal code if not.

    *token* is the vault's resolved credential, or ``None`` when the vault is empty.
    *env_api_key* says ``KIRO_API_KEY`` is set: it is the sign-in only when the
    vault is empty, the same precedence :class:`~kiro_crew.auth.provider.KasAuthProvider`
    applies. Returns ``(True, "")`` or ``(False, <reason code>)``.
    """
    if token is None:
        return (False, REASON_API_KEY) if env_api_key else (False, REASON_NOT_SIGNED_IN)
    provider = token.provider if isinstance(token.provider, str) else ""
    auth_method = token.auth_method if isinstance(token.auth_method, str) else ""
    if auth_method.lower() == "external_idp" or provider == "ExternalIdp":
        return False, REASON_EXTERNAL_IDP
    if provider == "ApiKey" or auth_method.lower() == "api_key":
        return False, REASON_API_KEY
    if provider in PERSONAL_PROVIDERS:
        return True, ""
    return False, REASON_UNKNOWN_SIGN_IN


def allowed_account_type(
    identity: Optional[dict], *, env_api_key: bool = False
) -> tuple[bool, str]:
    """:func:`allowed`, for a sign-in kiro-cli owns, from its ``whoami`` identity.

    *identity* is :func:`~kiro_crew.cloud.login_target.discover_local_identity`'s
    answer: ``None`` when nothing is known, ``{}`` when kiro-cli has no sign-in.
    Identity Center is allowed, as the vault's ``Enterprise`` provider is.
    """
    from kiro_crew.cloud.login_target import (
        ACCOUNT_TYPE_API_KEY,
        ACCOUNT_TYPE_IDENTITY_CENTER,
        is_personal_account_type,
    )

    if identity is None:
        return False, REASON_UNKNOWN_SIGN_IN
    account_type = str(identity.get("account_type") or "")
    if not account_type:
        return (False, REASON_API_KEY) if env_api_key else (False, REASON_NOT_SIGNED_IN)
    if account_type == ACCOUNT_TYPE_API_KEY:
        return False, REASON_API_KEY
    if account_type.lower() == "externalidp":
        return False, REASON_EXTERNAL_IDP
    if is_personal_account_type(account_type) or account_type == ACCOUNT_TYPE_IDENTITY_CENTER:
        return True, ""
    return False, REASON_UNKNOWN_SIGN_IN


def _is_team_crew() -> bool:
    """Whether this Crew leases as a team Crew, so the personal rule does not apply."""
    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.relay_client.provider import RelaySettings

    return RelaySettings.from_config(KiroCrewConfig.load()).is_team_crew


def _is_team(team: Optional[bool]) -> bool:
    """*team* when the caller knows it (a running relay's own settings), else ``config.json``."""
    return _is_team_crew() if team is None else team


def _kiro_cli_owns_sign_in() -> bool:
    from kiro_crew.auth.whoami import agent_owns_sign_in, load_sign_in_owner_config

    return not agent_owns_sign_in(load_sign_in_owner_config().agent)


def _kiro_cli_identity() -> Optional[dict]:
    """kiro-cli's ``whoami``, from the PINNED binary only.

    The guard's poll is unattended, so a ``kiro-cli`` found on an inherited
    ``PATH`` an agent can write to is never run (as
    :func:`~kiro_crew.cloud.login_target.whoami_names_personal_account`). No
    kiro-cli at all is no sign-in (``{}``); one installed only where the pin does
    not accept it is unknown (``None``). Both refuse.
    """
    from kiro_crew.cloud.login_target import discover_local_identity
    from kiro_crew.kiro_cli import pin_kiro_cli

    pinned, unpinned_exists = pin_kiro_cli()
    if not pinned:
        return None if unpinned_exists else {}
    return discover_local_identity(pinned)


def _decide(token: Optional[KasToken]) -> Decision:
    """Judge the sign-in owner's identity, using *token* only when Crew owns it."""
    env_api_key = bool(os.environ.get("KIRO_API_KEY"))
    if _kiro_cli_owns_sign_in():
        ok, reason = allowed_account_type(_kiro_cli_identity(), env_api_key=env_api_key)
    else:
        ok, reason = allowed(token, env_api_key=env_api_key)
    return Decision(ok, reason)


@dataclass(frozen=True)
class Decision:
    """The sign-in verdict at one moment: ``ok`` or a refusal ``reason``."""

    ok: bool
    reason: str = ""

    @property
    def notice(self) -> str:
        return notice_for(self.reason)


def current_decision(*, team: Optional[bool] = None) -> Decision:
    """The verdict for this Crew's sign-in now. Blocking file IO; never raises.

    *team* is whether the relay being judged leases as a team Crew. A running
    relay passes its own settings' answer, which a config edit after it started
    cannot change; ``None`` reads it from ``config.json``, for an enable that has
    not started a relay yet.

    An edition that locks local tunnel enables refuses as ``locked`` before the
    sign-in is read. A vault that cannot be read is refused as ``unknown`` rather
    than read as allowed.
    """
    from kiro_crew.edition_locks import local_enable_locked

    if local_enable_locked("tunnel"):
        return Decision(False, REASON_LOCKED)
    try:
        if _is_team(team):
            return Decision(True)
        from kiro_crew.auth.bridge import default_token_store

        token = default_token_store().resolve()
        return _decide(token)
    except Exception:  # noqa: BLE001 - an unreadable vault refuses, it never allows
        return Decision(False, REASON_UNKNOWN_SIGN_IN)


def current_decision_with_generation(
    *, team: Optional[bool] = None
) -> tuple[Decision, tuple[str, ...]]:
    """:func:`current_decision` and the markers it describes, read as one snapshot.

    A sign-in moves the sign-in marker and then saves its credential, both under the
    store's sign-out lock. Reading the marker and the vault outside that lock could
    pair the new account's marker with the old account's credential, and a caller
    caching the verdict under that marker would keep the old verdict. Read under the
    lock, the pair always describes one account. *team* is as for
    :func:`current_decision`. Blocking file IO; never raises.
    """
    from kiro_crew.edition_locks import local_enable_locked

    if local_enable_locked("tunnel"):
        return Decision(False, REASON_LOCKED), sign_in_generation()
    read: list[tuple[Optional[KasToken], tuple[str, ...]]] = []
    # Read before the verdict, so a kiro-cli switch after this read moves it next time.
    marker = _kiro_cli_marker()
    try:
        if _is_team(team):
            return Decision(True), sign_in_generation()
        from kiro_crew.auth.bridge import default_token_store

        store = default_token_store()

        def _read() -> None:
            read.append(
                (store.resolve(), (store.signin_generation(), store.signout_generation(), marker))
            )

        store.with_signout_lock(_read)
        token, gen = read[0]
        # Outside the lock: on the kiro-cli route this runs `kiro-cli whoami`.
        return _decide(token), gen
    except Exception:  # noqa: BLE001 - an unreadable vault refuses, it never allows
        return Decision(False, REASON_UNKNOWN_SIGN_IN), ("", "", marker)


def audit_refusal(operation: str, decision: Decision) -> None:
    """Record a relay the sign-in rule kept from running in the security event log.

    *operation* is ``tunnel.start_denied`` (the relay did not start) or
    ``tunnel.stop_enforced`` (a running relay was stopped). Never raises: an audit
    write that fails must not keep a refused relay running.
    """
    try:
        from kiro_crew.sel import sel

        sel().log_api_access(
            caller="tunnel_manager",
            operation=operation,
            outcome="denied",
            source="gateway",
            resources=f"remote_control_sign_in={decision.reason}",
        )
    except Exception:  # noqa: BLE001 - the refusal stands whether or not it is logged
        import logging

        logging.getLogger(__name__).warning("Remote Control refusal audit failed", exc_info=True)


#: How often a cached verdict is re-read where kiro-cli owns the sign-in. kiro-cli's
#: store moves none of the vault's markers, so its account changes are found by time.
KIRO_CLI_RECHECK_SECONDS = 30.0


def _kiro_cli_marker() -> str:
    """A marker that moves every :data:`KIRO_CLI_RECHECK_SECONDS` on the kiro-cli route.

    ``""`` where Crew's vault owns the sign-in, so the vault's markers alone decide.
    """
    try:
        if not _kiro_cli_owns_sign_in():
            return ""
    except Exception:  # noqa: BLE001 - an unknown owner re-checks, like the kiro-cli route
        pass
    return f"kiro-cli:{int(time.monotonic() // KIRO_CLI_RECHECK_SECONDS)}"


def sign_in_generation() -> tuple[str, ...]:
    """The markers that move on every account change: the vault's two, plus kiro-cli's.

    The vault's sign-in and sign-out markers move when Crew's own vault changes
    account; a token refresh moves neither. Where kiro-cli owns the sign-in, the
    third marker moves on an interval (:func:`_kiro_cli_marker`), so a kiro-cli
    account switch is found within :data:`KIRO_CLI_RECHECK_SECONDS`. A caller that
    caches :func:`current_decision` keyed on this triple re-reads only when one moves.
    ``("", "", <kiro-cli marker>)`` when the vault's markers cannot be read.
    """
    marker = _kiro_cli_marker()
    try:
        from kiro_crew.auth.bridge import default_token_store

        store = default_token_store()
        return store.signin_generation(), store.signout_generation(), marker
    except Exception:  # noqa: BLE001 - a missing marker reads as never written
        return "", "", marker
