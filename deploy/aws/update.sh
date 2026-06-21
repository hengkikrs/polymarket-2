#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/poly-v3}"

cd "$APP_DIR"

echo "[1/6] Stopping services..."
sudo systemctl stop poly-bot poly-dashboard poly-tracker

echo "[2/6] Backing up runtime_data..."
bash "$APP_DIR/deploy/aws/backup_runtime.sh"

echo "[3/6] Pulling latest code..."
git pull --ff-only

echo "[4/6] Updating Python dependencies..."
source "$APP_DIR/.venv/bin/activate"
pip install -r "$APP_DIR/requirements.txt"

echo "[5/6] Running tests..."
python -m pytest -q

echo "[6/6] Restarting services..."
sudo systemctl restart poly-bot poly-dashboard poly-tracker
bash "$APP_DIR/deploy/aws/healthcheck.sh"

