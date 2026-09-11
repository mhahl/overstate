#!/usr/bin/env bash
# Install our CA-signed certs as the salt-api server cert and restart salt-api.
# Why a script: the master image regenerates its own self-signed cert and
# appends its rest_cherrypy block to the main config file at every boot, so
# neither bind-mounts on its cert paths (its startup deletes them) nor
# ssl_crt/ssl_key in our mounted api.conf (the main file wins the merge)
# can take effect declaratively. Post-boot install is the reliable hook.
set -euo pipefail
cd "$(dirname "$0")/.."
# shellcheck source=lib.sh
source scripts/lib.sh

COMPOSE=$(detect_compose)
MASTER=$(podman ps --format "{{.Names}}" | grep -E "salt-master" | head -1)
if [ -z "$MASTER" ]; then
  echo "error: salt-master container is not running" >&2
  exit 1
fi
podman cp salt-config/tls/api.crt "$MASTER:/etc/pki/tls/certs/localhost.crt"
podman cp salt-config/tls/api.key "$MASTER:/etc/pki/tls/certs/localhost.key"
# shellcheck disable=SC2086
$COMPOSE -f "$COMPOSE_FILE" exec -T salt-master supervisorctl restart salt-api
sleep 5
curl --cacert salt-config/tls/ca.crt -s -o /dev/null \
  -w "salt-api TLS with dev CA: %{http_code}\n" https://127.0.0.1:8001/login
