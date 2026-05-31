#!/usr/bin/env bash
#
# Smoke-test the daily rollover: a long-running incident periodically strikes
# its current message and pushes a fresh one (replying to the old), so the chat
# shows a chain and pings once per rollover window.
#
# Waiting a real 24h is impractical, so point this at a tggw started with a tiny
# rollover window. For example, a throwaway instance on a spare port:
#
#   ROLLOVER_HOURS=0.0014 PORT=18099 \
#     .venv/bin/python app.py >/tmp/tggw-roll.log 2>&1 &
#   BASE_URL=http://localhost:18099 ./tests/smoke-rollover.sh
#
# 0.0014h ≈ 5s, and the script waits ROLL_WAIT (default 8s) between firings so
# each one crosses the window and rolls over. Set ROLL_WAIT to comfortably
# exceed the server's rollover window.
#
# Sequence (watch your Telegram chat):
#   firing            -> NEW message (push)             action: sent
#   firing (> window) -> strike old + NEW reply (push)  action: rolled
#   firing (> window) -> strike + NEW reply (push)       action: rolled
#   resolved          -> strike + recovery reply (push) action: resolved
#
# Requires API_AUTH_TOKEN (environment or .env); sends to the server's chat id.

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
ROLL_WAIT="${ROLL_WAIT:-8}"
GROUP_KEY="${GROUP_KEY:-smoke-rollover-devdm}"
TEMPLATE="${TEMPLATE:-vps-network-monitoring}"

if [[ -z "$TOKEN" ]]; then
  echo "error: set API_AUTH_TOKEN (in the environment or .env) so requests authenticate." >&2
  exit 1
fi

pp() { if command -v jq >/dev/null 2>&1; then jq -c '{action, telegram_message_id, key}'; else cat; fi; }

post() {
  curl -sS -X POST "$BASE_URL/api/grafana" \
    -H "Authorization: Bearer $TOKEN" \
    -H "Content-Type: application/json" \
    -d "$1" | pp
}

# $1 = status, $2 = message text. Template travels as the `service` label.
payload() {
  printf '{"status":"%s","groupKey":"%s","commonLabels":{"observer":"devhome","device":"devdm","service":"%s"},"message":"%s"}' \
    "$1" "$GROUP_KEY" "$TEMPLATE" "$2"
}

step() { echo; echo "=== $1 ==="; echo "expect: $2"; }

echo "tggw rollover smoke -> $BASE_URL  (group key: $GROUP_KEY, roll wait: ${ROLL_WAIT}s)"
echo; echo "--- health ---"; curl -sS "$BASE_URL/health"; echo

step "1) first firing" "action=sent, NEW message (push)"
post "$(payload firing '🔴 Grafana Alert\nmessage: devdm down (loss 90%)\nobserver: devhome')"
sleep "$ROLL_WAIT"

step "2) still firing past the window" "action=rolled, strike #1 + NEW message replying to it (push), duration carries over"
post "$(payload firing '🔴 Grafana Alert\nmessage: devdm down (loss 90%)\nobserver: devhome')"
sleep "$ROLL_WAIT"

step "3) still firing past the window again" "action=rolled, strike #2 + NEW reply (push)"
post "$(payload firing '🔴 Grafana Alert\nmessage: devdm down (loss 90%)\nobserver: devhome')"
sleep "$ROLL_WAIT"

step "4) resolved" "action=resolved, strike the latest + recovery reply (push)"
post "$(payload resolved '🟢 Grafana Alert\nmessage: devdm recovered\nobserver: devhome')"

echo
echo "done. The chat should show a reply-chain of struck messages, each duration"
echo "larger than the last (it tracks the whole incident), ending in the recovery."
