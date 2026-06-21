#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/poly-v3}"
BACKUP_DIR="${BACKUP_DIR:-$APP_DIR/backups}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
OUT="$BACKUP_DIR/runtime_data_$STAMP.tar.gz"

mkdir -p "$BACKUP_DIR"
cd "$APP_DIR"

if [[ ! -d runtime_data ]]; then
  echo "No runtime_data directory found."
  exit 0
fi

tar -czf "$OUT" runtime_data
chmod 600 "$OUT"
echo "$OUT"

