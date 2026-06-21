#!/usr/bin/env bash
set -euxo pipefail

# Optional EC2 User Data script.
# Replace REPO_URL before use. For private repos, prefer manual clone via SSH
# or a short-lived token rather than putting long-lived credentials here.

REPO_URL="https://github.com/hengkikrs/polymarket-2.git"
APP_DIR="/opt/poly-v3"

apt update
apt install -y git
mkdir -p "$APP_DIR"
chown ubuntu:ubuntu "$APP_DIR"

if [[ ! -d "$APP_DIR/.git" ]]; then
  sudo -u ubuntu git clone "$REPO_URL" "$APP_DIR"
fi

sudo -u ubuntu bash "$APP_DIR/deploy/aws/bootstrap_ubuntu.sh"

