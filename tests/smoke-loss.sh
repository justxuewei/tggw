#!/usr/bin/env bash
#
# Smoke-test a packet-loss progression through /api/grafana, mirroring the real
# vps-network alert rule: loss >10% fires (🟡 degraded), loss >80% escalates to
# 🔴 down, then it resolves. Under the "push on fire + resolve" policy, the only
# pushes are the first firing and the resolve; every loss change in between is a
# silent edit of the same bubble.
#
#   30% loss  -> 🟡 degraded, first firing  -> NEW push      action: sent
#   50% loss  -> 🟡 degraded, higher loss   -> silent edit   action: edited
#   100% loss -> 🔴 down, escalation         -> silent edit   action: edited
#   resolved  -> strike firing + green push  -> NEW push      action: resolved
#
# Usage:
#   ./tests/smoke-loss.sh
#   BASE_URL=https://tggw.example.com PAUSE=5 ./tests/smoke-loss.sh
#
# Requires API_AUTH_TOKEN (environment or .env). Sends to the server's
# configured TELEGRAM_CHAT_ID — point it at a throwaway test chat first.

set -euo pipefail

cd "$(dirname "$0")/.."

if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

BASE_URL="${BASE_URL:-http://localhost:8080}"
TOKEN="${TOKEN:-${API_AUTH_TOKEN:-}}"
PAUSE="${PAUSE:-5}"
GROUP_KEY="${GROUP_KEY:-smoke-loss-devdm}"
if [[ -z "$TOKEN" ]]; then
  echo "error: set API_AUTH_TOKEN (in the environment or .env) so requests authenticate." >&2
  exit 1
fi

pp() { if command -v jq >/dev/null 2>&1; then jq .; else cat; fi; }

TEMPLATE="${TEMPLATE:-vps-network-monitoring}"

post() {
  curl -sS -X POST "$BASE_URL/api/grafana" \
    -H "Authorization: Bearer $TOKEN" \
    -H "Content-Type: application/json" \
    -d "$1" | pp
}

# $1 = status, $2 = severity emoji, $3 = summary line. tggw appends the timeline,
# so the message carries no time field. The template travels as the `service` label.
payload() {
  printf '{"status":"%s","groupKey":"%s","commonLabels":{"observer":"devhome","device":"devdm","service":"%s"},"message":"%s Grafana Alert\\nmessage: %s\\nobserver: devhome"}' \
    "$1" "$GROUP_KEY" "$TEMPLATE" "$2" "$3"
}

step() { echo; echo "=== $1 ==="; echo "expect: $2"; }

echo "tggw loss smoke test -> $BASE_URL  (group key: $GROUP_KEY, pause: ${PAUSE}s)"
echo; echo "--- health ---"; curl -sS "$BASE_URL/health" | pp

step "1) loss 30% (🟡 degraded)" "action=sent, NEW message (push)"
post "$(payload firing '🟡' 'devdm degraded (loss 30.00%)')"
sleep "$PAUSE"

step "2) loss 50% (🟡 degraded)" "action=edited, same bubble updates silently (no push)"
post "$(payload firing '🟡' 'devdm degraded (loss 50.00%)')"
sleep "$PAUSE"

step "3) loss 100% (🔴 down, escalation)" "action=edited, same bubble flips 🟡->🔴 silently (no push)"
post "$(payload firing '🔴' 'devdm down (loss 100.00%)')"
sleep "$PAUSE"

step "4) resolved" "action=resolved, firing bubble struck through + green push (NEW message)"
post "$(payload resolved '🟢' 'devdm recovered')"

echo
echo "done. One bubble climbed 30%->50%->100% via silent edits, got struck on resolve,"
echo "and the recovery arrived as a separate green push. Record is retired afterward."
