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
if [ ! -f "$REPO/Containerfile" ] || [ ! -d "$REPO/deploy/quadlet" ]; then
  echo "error: run from an Overstate repo checkout" >&2
  exit 1
fi

echo "==> rebuilding images"
podman build -t localhost/overstate:latest "$REPO"
podman build -f "$REPO/Containerfile.salt-master" -t localhost/overstate-salt-master:latest "$REPO"

CHANGED=0
for unit in "$REPO"/deploy/quadlet/overstate-*.container "$REPO"/deploy/quadlet/overstate.network; do
  cmp -s "$unit" "$UNITS/$(basename "$unit")" 2>/dev/null || CHANGED=1
done
cmp -s "$REPO/deploy/systemd/overstate-salt-api-tls.service" \
  /etc/systemd/system/overstate-salt-api-tls.service 2>/dev/null || CHANGED=1
cmp -s "$REPO/scripts/install-api-tls.sh" \
  /usr/local/sbin/overstate-install-api-tls.sh 2>/dev/null || CHANGED=1
if [ ! -f "$ETC/Caddyfile" ]; then
  cp "$REPO/deploy/Caddyfile" "$ETC/Caddyfile"
  chmod 644 "$ETC/Caddyfile"
fi
if [ "$CHANGED" -eq 1 ]; then
  echo "==> unit files changed; reinstalling"
  cp "$REPO"/deploy/quadlet/overstate-*.container "$REPO"/deploy/quadlet/overstate.network "$UNITS/"
  cp "$REPO/deploy/systemd/overstate-salt-api-tls.service" /etc/systemd/system/
  cp "$REPO/scripts/install-api-tls.sh" /usr/local/sbin/overstate-install-api-tls.sh
  chmod 755 /usr/local/sbin/overstate-install-api-tls.sh
  systemctl daemon-reload
  systemctl enable overstate-salt-api-tls.service
fi

# dev.conf enables auto_accept for the dev stack only and must never sit
# on a real master. Hosts installed before install.sh stripped it heal
# here; the restart below applies the removal.
if [ -f "$ETC/salt-config/dev.conf" ]; then
  echo "==> removing dev-only auto_accept config (not for production)"
  rm -f "$ETC/salt-config/dev.conf"
fi

echo "==> restarting salt-master, worker, app, caddy"
systemctl restart overstate-salt-master.service
API_UP=""
for _ in $(seq 1 30); do
  case "$(curl -sk -o /dev/null -w '%{http_code}' \
    https://127.0.0.1:8001/login)" in
    200|401)
      API_UP=1
      break
      ;;
  esac
  sleep 2
done
[ -n "$API_UP" ] || echo "WARNING: salt-api is not answering; check 'journalctl -u overstate-salt-master'" >&2
# The fresh master serves a self-signed cert until the api-tls unit
# reinstalls ours; starting it here blocks until verification passes.
systemctl start overstate-salt-api-tls.service
systemctl restart overstate-worker.service overstate-app.service \
  overstate-caddy.service
systemctl --no-pager --lines=0 status overstate-app.service \
  overstate-worker.service overstate-salt-master.service \
  overstate-caddy.service
echo "update done: migrations run automatically in the app entrypoint"
