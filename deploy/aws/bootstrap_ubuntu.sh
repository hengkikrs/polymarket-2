#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/poly-v3}"
SERVICE_DIR="/etc/systemd/system"

if [[ "$(id -u)" == "0" ]]; then
  echo "Run this script as the ubuntu user, not root."
  exit 1
fi

cd "$APP_DIR"

echo "[1/8] Installing OS packages..."
sudo apt update
sudo apt install -y git python3 python3-venv python3-pip build-essential curl tar

echo "[2/8] Creating runtime folders..."
mkdir -p "$APP_DIR/runtime_data" "$APP_DIR/logs" "$APP_DIR/backups"

echo "[3/8] Creating Python virtualenv..."
python3 -m venv "$APP_DIR/.venv"
source "$APP_DIR/.venv/bin/activate"
pip install -U pip
pip install -r "$APP_DIR/requirements.txt"
pip install pytest

echo "[4/8] Preparing .env..."
if [[ ! -f "$APP_DIR/.env" ]]; then
  cp "$APP_DIR/deploy/aws/env.example" "$APP_DIR/.env"
  chmod 600 "$APP_DIR/.env"
  echo "Created $APP_DIR/.env from deploy/aws/env.example. Edit it before live trading."
else
  chmod 600 "$APP_DIR/.env"
  echo "Existing .env found; keeping it."
fi

echo "[5/8] Marking helper scripts executable..."
chmod +x "$APP_DIR"/deploy/aws/*.sh

echo "[6/8] Installing systemd services..."
for service in poly-bot.service poly-dashboard.service poly-tracker.service; do
  sed "s#__APP_DIR__#$APP_DIR#g" "$APP_DIR/deploy/aws/systemd/$service" | sudo tee "$SERVICE_DIR/$service" >/dev/null
done

echo "[7/8] Enabling services..."
sudo systemctl daemon-reload
sudo systemctl enable poly-bot poly-dashboard poly-tracker

echo "[8/8] Running tests before start..."
python -m pytest -q

echo "Starting services..."
sudo systemctl restart poly-bot poly-dashboard poly-tracker
bash "$APP_DIR/deploy/aws/healthcheck.sh" || true

echo
echo "Done. Check status with:"
echo "  sudo systemctl status poly-bot"
echo "  sudo journalctl -u poly-bot -f"
echo
echo "Dashboard is bound to 127.0.0.1. Use SSH tunnel:"
echo "  ssh -L 5004:127.0.0.1:5004 -L 5005:127.0.0.1:5005 ubuntu@EC2_PUBLIC_IP"
