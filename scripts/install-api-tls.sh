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
api_up() { # any HTTP answer proves salt-api listens; /login needs no token
  case "$(curl -sk -o /dev/null -w '%{http_code}' \
    https://127.0.0.1:8001/login || true)" in
    200|401) return 0 ;;
    *) return 1 ;;
  esac
}
for _ in $(seq 1 30); do
  if api_up; then
    break
  fi
  sleep 2
done
podman cp "$CRT" "$MASTER:/etc/pki/tls/certs/localhost.crt"
podman cp "$KEY" "$MASTER:/etc/pki/tls/certs/localhost.key"
podman exec "$MASTER" supervisorctl restart salt-api
# CherryPy needs more than a moment after restart; poll instead of
# failing on the first attempt (curl exits 7 while nothing listens,
# which used to fail the whole unit under set -e).
CODE=""
for _ in $(seq 1 12); do
  CODE="$(curl --cacert "$(dirname "$CRT")/ca.crt" -s -o /dev/null \
    -w '%{http_code}' https://127.0.0.1:8001/login || true)"
  case "$CODE" in
    200|401) break ;;
  esac
  sleep 5
done
echo "salt-api TLS with our CA: $CODE"
case "$CODE" in
  200|401) ;;
  *)
    echo "error: salt-api did not come back with our cert" >&2
    exit 1
    ;;
esac
