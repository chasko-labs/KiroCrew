#!/bin/sh
# Crew's muse-serve front: --trust-workspace for member sessions, passthrough
# for everything else.
#
# serve only dials session-mounted MCP servers when it starts trusting the
# workspace; without trust the member dispatch entry rides the session array
# unheard (member-dispatch-no-dial-measured.jsonl), with trust the round trip
# completes (member-dispatch-live.jsonl). The harness points
# MUSE_CODE_EXECUTABLE here instead of at the raw binary, and names the raw
# binary in KIROCREW_MUSE_BIN; the trust itself arrives per session in
# KIROCREW_MUSE_TRUST_WORKSPACE, set only when that session mounted the
# member dispatch entry. An operator pin of MUSE_CODE_EXECUTABLE skips this
# wrapper outright. Non-serve invocations (version probes, skills discovery)
# pass through unwrapped: trust changes nothing for them.
#
# Trust loads the workspace's skills and rules too, so it is scoped to member
# sessions in operator-owned project workspaces. Sandbox, approvals and the
# dashboard server's caller auth stand unchanged.
if [ "${1:-}" = "serve" ] && [ -n "${KIROCREW_MUSE_TRUST_WORKSPACE:-}" ]; then
    if [ -z "${KIROCREW_MUSE_BIN:-}" ]; then
        echo "muse-serve-trust: refusing trusted serve without KIROCREW_MUSE_BIN" >&2
        exit 2
    fi
    shift
    exec "$KIROCREW_MUSE_BIN" serve --trust-workspace "$@"
fi
if [ -n "${KIROCREW_MUSE_BIN:-}" ]; then
    exec "$KIROCREW_MUSE_BIN" "$@"
fi
exec muse "$@"
