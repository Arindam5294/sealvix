#!/usr/bin/env bash
# One-shot setup for a fresh Vultr Cloud Compute VM (Ubuntu 24.04, 2 vCPU / 4 GB).
# Usage (as root, from the repo folder):  bash scripts/setup-vm.sh
set -euo pipefail
APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"

echo "== Docker =="
if ! command -v docker >/dev/null; then
  curl -fsSL https://get.docker.com -o /tmp/get-docker.sh && sh /tmp/get-docker.sh
fi

echo "== gVisor (runsc) =="
if ! command -v runsc >/dev/null; then
  apt-get update && apt-get install -y apt-transport-https ca-certificates curl gnupg
  curl -fsSL https://gvisor.dev/archive.key | gpg --dearmor -o /usr/share/keyrings/gvisor-archive-keyring.gpg
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/gvisor-archive-keyring.gpg] https://storage.googleapis.com/gvisor/releases release main" \
    > /etc/apt/sources.list.d/gvisor.list
  apt-get update && apt-get install -y runsc
fi
# apt package should register runsc with Docker; make sure it did
docker info 2>/dev/null | grep -q runsc || runsc install
systemctl restart docker
docker run --rm --runtime=runsc hello-world >/dev/null
echo "gVisor OK"

echo "== Sandbox images =="
docker pull node:22-bookworm-slim
docker pull python:3.12-slim

echo "== Containment smoke test (expect BLOCKED lines) =="
docker run --rm --runtime=runsc --network none --read-only --user 1000:1000 --pids-limit 64 python:3.12-slim \
  python -c "
import urllib.request,os
try: urllib.request.urlopen('http://example.com',timeout=3); print('NETWORK OPEN (bad)')
except Exception as e: print('BLOCKED network:',e)
try: open('/etc/passwd','a'); print('ROOT WRITABLE (bad)')
except Exception as e: print('BLOCKED write:',e)
"

echo "== App =="
apt-get install -y python3-venv git
python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"
mkdir -p /etc/sealvix
[ -f /etc/sealvix/env ] || cat > /etc/sealvix/env <<ENV
SANDBOX_RUNTIME=runsc
DATA_DIR=/var/lib/sealvix
# Vultr Serverless Inference key (host only; never passed into a sandbox)
VULTR_INFERENCE_KEY=
VULTR_MODEL=glm-5.3
VULTR_FALLBACK_MODEL=deepseek-v4.1-flash-1017
ENV
chmod 600 /etc/sealvix/env
mkdir -p /var/lib/sealvix

cat > /etc/systemd/system/sealvix.service <<UNIT
[Unit]
Description=Sealvix
After=docker.service
Requires=docker.service

[Service]
WorkingDirectory=$APP_DIR
EnvironmentFile=/etc/sealvix/env
ExecStart=$APP_DIR/.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000
Restart=always

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable --now sealvix

echo "== HTTPS via Caddy on <ip>.sslip.io =="
IP="$(curl -fsS https://api.ipify.org || hostname -I | awk '{print $1}')"
DOMAIN="${DOMAIN:-$IP.sslip.io}"
docker rm -f caddy >/dev/null 2>&1 || true
docker run -d --name caddy --restart unless-stopped --network host -v caddy_data:/data \
  caddy:2 caddy reverse-proxy --from "$DOMAIN" --to 127.0.0.1:8000
command -v ufw >/dev/null && { ufw allow 22/tcp; ufw allow 80/tcp; ufw allow 443/tcp; } || true

echo
echo "Done. Open: https://$DOMAIN"
echo "Add your inference key to /etc/sealvix/env, then: systemctl restart sealvix"
