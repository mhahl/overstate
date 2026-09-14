#!/usr/bin/env bash
# Install our CA-signed api certs as the salt-api server cert and restart
# salt-api. The master image regenerates its own self-signed cert at every
# boot, so this must run after each (re)start of the salt-master container.
# install.sh and update.sh call it automatically; run it by hand after any
# manual master restart. Usage: install-api-tls.sh [crt] [key] [container].
set -euo pipefail

CRT="${1:-/etc/overstate/tls/api.crt}"
KEY="${2:-/etc/overstate/tls/api.key}"
MASTER="${3:-salt-master}"

for f in "$CRT" "$KEY"; do
  [ -f "$f" ] || { echo "error: missing $f" >&2; exit 1; }
done
if ! podman container exists "$MASTER" >/dev/null 2>&1; then
  echo "error: container '$MASTER' does not exist" >&2
  exit 1
fi
for _ in $(seq 1 30); do
  # 401 from /login proves salt-api is up (no token yet).
  if [ "$(curl -sk -o /dev/null -w '%{http_code}' \
    https://127.0.0.1:8001/login)" = "401" ]; then
    break
  fi
  sleep 2
done
podman cp "$CRT" "$MASTER:/etc/pki/tls/certs/localhost.crt"
podman cp "$KEY" "$MASTER:/etc/pki/tls/certs/localhost.key"
podman exec "$MASTER" supervisorctl restart salt-api
sleep 5
curl --cacert "$(dirname "$CRT")/ca.crt" -s -o /dev/null \
  -w "salt-api TLS with our CA: %{http_code}\n" https://127.0.0.1:8001/login
