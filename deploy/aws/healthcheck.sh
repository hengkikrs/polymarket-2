#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/poly-v3}"
DASH_URL="${DASH_URL:-http://127.0.0.1:5004/api/health}"
TRACKER_URL="${TRACKER_URL:-http://127.0.0.1:5005/api/data}"

failures=0

check_service() {
  local name="$1"
  if systemctl is-active --quiet "$name"; then
    echo "OK service $name"
  else
    echo "FAIL service $name"
    failures=$((failures + 1))
  fi
}

check_http() {
  local name="$1"
  local url="$2"
  if curl -fsS --max-time 5 "$url" >/dev/null; then
    echo "OK http $name $url"
  else
    echo "FAIL http $name $url"
    failures=$((failures + 1))
  fi
}

check_service poly-bot
check_service poly-dashboard
check_service poly-tracker
check_http dashboard "$DASH_URL"
check_http tracker "$TRACKER_URL"

if [[ -f "$APP_DIR/runtime_data/bot_control.json" ]]; then
  echo "bot_control: $(cat "$APP_DIR/runtime_data/bot_control.json")"
fi

if [[ "$failures" -gt 0 ]]; then
  echo "Healthcheck failed: $failures problem(s)"
  exit 1
fi

echo "Healthcheck passed."

