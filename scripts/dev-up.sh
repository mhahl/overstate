#!/usr/bin/env bash
# Bring the dev stack up (build changed images) and wait for postgres.
set -euo pipefail
cd "$(dirname "$0")/.."
# shellcheck source=lib.sh
source scripts/lib.sh

if [ ! -f salt-config/tls/ca.crt ]; then
  ./scripts/gen-dev-certs.sh
fi
COMPOSE=$(detect_compose)
# shellcheck disable=SC2086
$COMPOSE -f "$COMPOSE_FILE" up -d --build
wait_for_postgres
./scripts/enable-api-tls.sh
echo "stack up: https://127.0.0.1:8000 (dev cert, add an exception or -k)"
