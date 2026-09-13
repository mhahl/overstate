#!/usr/bin/env bash
# Rebuild Overstate images from this checkout and restart the services
# whose images changed. Postgres/redis keep running (data untouched).
# Run from a repo checkout: `sudo ./scripts/update.sh`.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
ETC=/etc/overstate
UNITS=/etc/containers/systemd

if [ "$(id -u)" -ne 0 ]; then
  echo "error: run as root" >&2
  exit 1
fi
if [ ! -f "$REPO/Containerfile" ] || [ ! -d "$REPO/quadlet" ]; then
  echo "error: run from an Overstate repo checkout" >&2
  exit 1
fi

echo "==> rebuilding images"
podman build -t localhost/overstate:latest "$REPO"
podman build -f "$REPO/Containerfile.salt-master" -t localhost/overstate-salt-master:latest "$REPO"

CHANGED=0
for unit in "$REPO"/quadlet/overstate-*.container "$REPO"/quadlet/overstate.network; do
  cmp -s "$unit" "$UNITS/$(basename "$unit")" 2>/dev/null || CHANGED=1
done
if [ ! -f "$ETC/Caddyfile" ]; then
  cp "$REPO/deploy/Caddyfile" "$ETC/Caddyfile"
  chmod 644 "$ETC/Caddyfile"
fi
if [ "$CHANGED" -eq 1 ]; then
  echo "==> unit files changed; reinstalling"
  cp "$REPO"/quadlet/overstate-*.container "$REPO"/quadlet/overstate.network "$UNITS/"
  systemctl daemon-reload
fi

echo "==> restarting salt-master, worker, app, caddy"
systemctl restart overstate-salt-master.service
API_UP=""
for _ in $(seq 1 30); do
  if [ "$(curl -sk -o /dev/null -w '%{http_code}' \
    https://127.0.0.1:8001/login)" = "401" ]; then
    API_UP=1
    break
  fi
  sleep 2
done
[ -n "$API_UP" ] || echo "WARNING: salt-api is not answering; check 'journalctl -u overstate-salt-master'" >&2
systemctl restart overstate-worker.service overstate-app.service \
  overstate-caddy.service
systemctl --no-pager --lines=0 status overstate-app.service \
  overstate-worker.service overstate-salt-master.service \
  overstate-caddy.service
echo "update done: migrations run automatically in the app entrypoint"
