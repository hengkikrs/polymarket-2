# Poly V3 AWS EC2 24/7 Tutorial

This guide runs Poly V3 continuously on an AWS EC2 VPS using `systemd`.

Use EC2 for the always-on bot process. Do not use Vercel for the executor:
serverless functions are request-bound, while this bot needs a long-running
market loop, orderbook polling, local runtime state, and restart supervision.

No secrets are stored in this folder. Keep real `.env` values only on the EC2
instance.

## What This Deploy Kit Adds

- `bootstrap_ubuntu.sh`: first-time Ubuntu setup.
- `systemd/*.service`: always-on services for bot, dashboard, and tracker.
- `healthcheck.sh`: checks services plus dashboard/tracker HTTP endpoints.
- `update.sh`: safe update flow with backup, `git pull`, tests, restart.
- `backup_runtime.sh`: archives `runtime_data`.
- `restore_runtime.sh`: restores a runtime backup and leaves the bot stopped.
- `cloud-init-user-data.sh`: optional EC2 user-data starting point.
- Dashboard `/api/health`: non-secret health endpoint for local monitoring.

## AWS Cost Guard First

Before launching anything:

1. Open AWS Billing / Cost Management.
2. Create an AWS Budget for your credit, for example `$20`, `$50`, and `$90`
   alert thresholds.
3. Add your email notification.
4. Check EC2 pricing for your selected region before launch.

AWS references:

- EC2 pricing: https://aws.amazon.com/ec2/pricing/on-demand/
- Security groups: https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/ec2-security-groups.html
- Security group SSH rules: https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/security-group-rules-reference.html
- Billing alarms: https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/monitor_estimated_charges_with_cloudwatch.html

## Recommended AWS Configuration

Start conservative:

- Region: closest stable region to you, commonly Singapore (`ap-southeast-1`)
  or Tokyo (`ap-northeast-1`). Pricing differs by region.
- AMI: Ubuntu Server 24.04 LTS or 22.04 LTS.
- Instance: `t3.small` x86_64, 2 vCPU, 2 GB RAM.
- Storage: 20 GB gp3.
- Public IP: yes, for SSH.
- Security group:
  - inbound TCP 22 from your current IP only.
  - no inbound 5004/5005.
  - outbound allow all.

`t3.micro` can be tried for paper mode, but this bot runs the bot, dashboard,
tracker, CLOB polling, and JSON writes. `t3.small` is the safer first target.

## Security Model

Keep dashboard and tracker private:

- `DASH_HOST=127.0.0.1`
- access via SSH tunnel only
- do not open ports `5004` or `5005` in the AWS security group

Keep keys private:

- Never commit `.env`.
- Never paste Polymarket private keys into GitHub Actions, Vercel, or public logs.
- This repo already ignores `.env`, `.env.*`, `runtime_data/`, `logs/`, `*.pem`.

## Step 1: Prepare GitHub

Use a private GitHub repository if possible.

From your local machine:

```bash
git status
git remote -v
```

Make sure `.env` is ignored:

```bash
git check-ignore .env
git check-ignore runtime_data/trades.json
```

Push code to GitHub. Do not push runtime data or secrets.

## Step 2: Launch EC2

In AWS Console:

1. EC2 -> Instances -> Launch instance.
2. Name: `poly-v3-bot`.
3. AMI: Ubuntu Server LTS.
4. Instance type: `t3.small`.
5. Key pair: create or select one. Download the `.pem`.
6. Network settings:
   - Auto-assign public IP: enabled.
   - Security group inbound:
     - SSH, TCP 22, source: `My IP`.
   - Do not add HTTP, HTTPS, 5004, or 5005.
7. Storage: 20 GB gp3.
8. Launch.

On Windows, set key file permissions if SSH complains:

```powershell
icacls C:\path\poly-v3.pem /inheritance:r
icacls C:\path\poly-v3.pem /grant:r "$($env:USERNAME):R"
```

Connect:

```powershell
ssh -i C:\path\poly-v3.pem ubuntu@EC2_PUBLIC_IP
```

## Step 3: Clone and Bootstrap

On EC2:

```bash
sudo apt update
sudo apt install -y git
sudo mkdir -p /opt/poly-v3
sudo chown ubuntu:ubuntu /opt/poly-v3
git clone https://github.com/hengkikrs/polymarket-2.git /opt/poly-v3
cd /opt/poly-v3
bash deploy/aws/bootstrap_ubuntu.sh
```

What the bootstrap does:

1. installs OS packages
2. creates `.venv`
3. installs Python dependencies
4. creates `runtime_data`, `logs`, and `backups`
5. creates `.env` from `deploy/aws/env.example` if missing
6. installs `systemd` services
7. runs tests
8. starts bot, dashboard, and tracker

If the repo is private, clone with SSH:

```bash
ssh-keygen -t ed25519 -C "poly-v3-ec2"
cat ~/.ssh/id_ed25519.pub
```

Add the public key to GitHub as a deploy key, then:

```bash
git clone git@github.com:hengkikrs/polymarket-2.git /opt/poly-v3
```

## Step 4: Configure `.env`

Edit only on EC2:

```bash
nano /opt/poly-v3/.env
chmod 600 /opt/poly-v3/.env
```

Start in mock mode:

```env
MOCK_MODE=true
DASH_HOST=127.0.0.1
DASH_PORT=5004
LOG_LEVEL=INFO
```

Fill Polymarket keys only if needed. For mock mode, credentials can remain empty
unless your current code path needs authenticated live balance checks.

Live mode requires explicit confirmation:

```env
MOCK_MODE=false
LIVE_TRADING_CONFIRM=I_UNDERSTAND_THIS_IS_REAL_MONEY
```

For early live testing, keep size small:

```env
END_WINDOW_LIVE_TRADE_USD=1.00
SAFETY_MAX_LIVE_TRADE_USD=10.0
SAFETY_MAX_LIVE_WINDOW_EXPOSURE_USD=20.0
```

Restart after env changes:

```bash
sudo systemctl restart poly-bot poly-dashboard poly-tracker
```

## Step 5: Check Services

```bash
sudo systemctl status poly-bot
sudo systemctl status poly-dashboard
sudo systemctl status poly-tracker
```

Follow bot logs:

```bash
sudo journalctl -u poly-bot -f
```

Run healthcheck:

```bash
cd /opt/poly-v3
bash deploy/aws/healthcheck.sh
```

Expected output includes:

```text
OK service poly-bot
OK service poly-dashboard
OK service poly-tracker
OK http dashboard http://127.0.0.1:5004/api/health
OK http tracker http://127.0.0.1:5005/api/data
Healthcheck passed.
```

## Step 6: Open Dashboard Safely

From your Windows laptop, open an SSH tunnel:

```powershell
ssh -i C:\path\poly-v3.pem -L 5004:127.0.0.1:5004 -L 5005:127.0.0.1:5005 ubuntu@EC2_PUBLIC_IP
```

Keep that SSH window open, then browse locally:

- http://localhost:5004
- http://localhost:5005

Do not expose 5004 or 5005 publicly unless you add proper authentication,
TLS, IP allowlisting, and understand the risk.

## Step 7: Control Trading State

Use the dashboard buttons when possible.

CLI checks:

```bash
cat /opt/poly-v3/runtime_data/bot_control.json
curl -s http://127.0.0.1:5004/api/health
```

If you need to force stop trading at file level:

```bash
cd /opt/poly-v3
python - <<'PY'
import json
from pathlib import Path
Path("runtime_data/bot_control.json").write_text(
    json.dumps({"trading_enabled": False}, indent=2),
    encoding="utf-8",
)
PY
sudo systemctl restart poly-bot
```

## Step 8: Update Bot Code

Use the helper:

```bash
cd /opt/poly-v3
bash deploy/aws/update.sh
```

Manual equivalent:

```bash
cd /opt/poly-v3
sudo systemctl stop poly-bot poly-dashboard poly-tracker
bash deploy/aws/backup_runtime.sh
git pull --ff-only
source .venv/bin/activate
pip install -r requirements.txt
python -m pytest -q
sudo systemctl restart poly-bot poly-dashboard poly-tracker
bash deploy/aws/healthcheck.sh
```

## Step 9: Backups and Restore

Create runtime backup:

```bash
cd /opt/poly-v3
bash deploy/aws/backup_runtime.sh
```

List backups:

```bash
ls -lh /opt/poly-v3/backups
```

Restore:

```bash
cd /opt/poly-v3
bash deploy/aws/restore_runtime.sh /opt/poly-v3/backups/runtime_data_YYYYmmddTHHMMSSZ.tar.gz
```

Restore intentionally leaves the bot stopped or only restarts dashboard/tracker
depending on script behavior. Check the dashboard before starting real trading:

```bash
sudo systemctl start poly-bot
```

## Step 10: Reboot Test

Confirm 24/7 behavior survives reboot:

```bash
sudo reboot
```

Reconnect after 1-2 minutes:

```bash
ssh -i C:\path\poly-v3.pem ubuntu@EC2_PUBLIC_IP
cd /opt/poly-v3
bash deploy/aws/healthcheck.sh
```

## Optional: EC2 User Data

`cloud-init-user-data.sh` is provided as a starting point. For private repos,
manual clone via SSH deploy key is safer because EC2 user-data can be visible to
users with EC2 metadata/API access.

If using it:

1. edit `REPO_URL`
2. paste into EC2 Advanced details -> User data
3. launch instance
4. SSH in and configure `.env`

Do not put Polymarket keys in EC2 user-data.

## Troubleshooting

### Bot service keeps restarting

```bash
sudo journalctl -u poly-bot -n 200 --no-pager
```

Common causes:

- `.env` missing or malformed
- live mode without `LIVE_TRADING_CONFIRM`
- package install failed
- network/API timeout

### Dashboard not opening in browser

On EC2:

```bash
curl -s http://127.0.0.1:5004/api/health
sudo systemctl status poly-dashboard
```

On laptop:

- confirm SSH tunnel is still open
- open `http://localhost:5004`, not the EC2 public IP

### Tracker not opening

```bash
curl -s http://127.0.0.1:5005/api/data >/dev/null && echo ok
sudo systemctl status poly-tracker
```

### Git pull fails

If repo is private, use an SSH deploy key:

```bash
ssh -T git@github.com
git remote -v
```

### Disk filling up

```bash
df -h
du -sh /opt/poly-v3/*
journalctl --disk-usage
```

Clean old journal logs if needed:

```bash
sudo journalctl --vacuum-time=7d
```

### Emergency stop

Fast stop:

```bash
sudo systemctl stop poly-bot
```

Keep dashboards online:

```bash
sudo systemctl restart poly-dashboard poly-tracker
```

## Operational Checklist Before Live

- AWS Budget created.
- Security group only allows SSH from your IP.
- Dashboard/tracker not public.
- `.env` exists on EC2 and has `chmod 600`.
- Bot has run in `MOCK_MODE=true` on EC2 for at least 24 hours.
- `bash deploy/aws/healthcheck.sh` passes.
- Dashboard shows expected current settings.
- `END_WINDOW_LIVE_TRADE_USD` is small.
- You understand live mode can lose real money.
- Only then set:

```env
MOCK_MODE=false
LIVE_TRADING_CONFIRM=I_UNDERSTAND_THIS_IS_REAL_MONEY
```

Then:

```bash
sudo systemctl restart poly-bot poly-dashboard poly-tracker
sudo journalctl -u poly-bot -f
```

