#!/usr/bin/env bash
set -euo pipefail

if [[ "${1:-}" == "" ]]; then
  echo "Usage: $0 /opt/poly-v3/backups/runtime_data_YYYYmmddTHHMMSSZ.tar.gz"
  exit 1
fi

APP_DIR="${APP_DIR:-/opt/poly-v3}"
BACKUP="$1"

cd "$APP_DIR"
sudo systemctl stop poly-bot poly-dashboard poly-tracker
mkdir -p runtime_data
tar -xzf "$BACKUP" -C "$APP_DIR"
sudo systemctl restart poly-dashboard poly-tracker
echo "Runtime restored from $BACKUP. Bot remains stopped; start it manually after checking dashboard."

