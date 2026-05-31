#!/usr/bin/env bash
#
# Smoke-test the /api/grafana lifecycle against a running tggw instance.
#
# Walks the four states an alert goes through and pauses between each so you can
# watch the Telegram chat react: a single message that's pushed, edited in place,
# struck through, then replaced by a fresh recovery push.
#
#   first firing     -> NEW message (push)            action: sent
#   repeat, same     -> no-op (flood prevention)      action: unchanged
#   escalation       -> silent edit of same bubble    action: edited
#   resolved         -> strike firing + NEW push      action: resolved
#
# Usage:
#   ./tests/smoke.sh                       # against http://localhost:8080
#   BASE_URL=https://tggw.example.com ./tests/smoke.sh
#   PAUSE=8 ./tests/smoke.sh               # seconds between steps (default 5)
#
# Requires API_AUTH_TOKEN (read from the environment or a local .env).
# Sends to whatever TELEGRAM_CHAT_ID the server is configured with, so point the
# server at a throwaway test chat first.

set -euo pipefail

cd "$(dirname "$0")/.."

# Pull API_AUTH_TOKEN (and any overrides) from .env if present.
if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

BASE_URL="${BASE_URL:-http://localhost:8080}"
TOKEN="${TOKEN:-${API_AUTH_TOKEN:-}}"
PAUSE="${PAUSE:-5}"
GROUP_KEY="${GROUP_KEY:-smoke-devdm}"

if [[ -z "$TOKEN" ]]; then
  echo "error: set API_AUTH_TOKEN (in the environment or .env) so requests authenticate." >&2
  exit 1
fi

# Pretty-print JSON responses when jq is available, otherwise pass through raw.
pp() { if command -v jq >/dev/null 2>&1; then jq .; else cat; fi; }

TEMPLATE="${TEMPLATE:-vps-network-monitoring}"

post() {
  curl -sS -X POST "$BASE_URL/api/grafana" \
    -H "Authorization: Bearer $TOKEN" \
    -H "Content-Type: application/json" \
    -d "$1" | pp
}

payload() {
  # $1 = status, $2 = message text. The template travels as the `service` label.
  printf '{"status":"%s","groupKey":"%s","commonLabels":{"observer":"devhome","device":"devdm","service":"%s"},"message":"%s"}' \
    "$1" "$GROUP_KEY" "$TEMPLATE" "$2"
}

step() {
  echo
  echo "=== $1 ==="
  echo "expect: $2"
}

echo "tggw smoke test -> $BASE_URL  (group key: $GROUP_KEY, pause: ${PAUSE}s)"

echo
echo "--- health ---"
curl -sS "$BASE_URL/health" | pp

step "a) first firing" "action=sent, a NEW message (you should get a push)"
post "$(payload firing '🔴 Grafana Alert\nmessage: devdm down (loss 80.00%)\nobserver: devhome')"
sleep "$PAUSE"

step "b) repeat, identical text" "action=unchanged, no new message and no visible edit"
post "$(payload firing '🔴 Grafana Alert\nmessage: devdm down (loss 80.00%)\nobserver: devhome')"
sleep "$PAUSE"

step "c) escalation, different text" "action=edited, the SAME bubble updates silently (no push)"
post "$(payload firing '🔴 Grafana Alert\nmessage: devdm down (loss 100.00%)\nobserver: devhome')"
sleep "$PAUSE"

step "d) resolved" "action=resolved, firing bubble struck through + a NEW green push"
post "$(payload resolved '🟢 Grafana Alert\nmessage: devdm recovered\nobserver: devhome')"

echo
echo "done. The record is retired after resolve; a later firing starts a fresh message."
echo "inspect the store with: sqlite3 \"\${RECORD_DB_PATH:-tggw-records.db}\" 'select key, message_id, text from messages;'"
