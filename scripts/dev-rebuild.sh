#!/usr/bin/env bash
# Rebuild the dev stack from scratch and bring it back up.
# Usage: dev-rebuild.sh [--fresh] [--seed]   (--fresh also drops volumes)
set -euo pipefail
cd "$(dirname "$0")/.."
# shellcheck source=lib.sh
source scripts/lib.sh

FRESH=0
SEED=0
for arg in "$@"; do
    case "$arg" in
        --fresh) FRESH=1 ;;
        --seed) SEED=1 ;;
        *) echo "usage: dev-rebuild.sh [--fresh] [--seed]" >&2; exit 1 ;;
    esac
done

COMPOSE=$(detect_compose)
# shellcheck disable=SC2086
if [ "$FRESH" -eq 1 ]; then
    $COMPOSE -f "$COMPOSE_FILE" down --volumes
else
    $COMPOSE -f "$COMPOSE_FILE" down
fi
# shellcheck disable=SC2086
$COMPOSE -f "$COMPOSE_FILE" build --no-cache overstate worker
# shellcheck disable=SC2086
$COMPOSE -f "$COMPOSE_FILE" up -d
wait_for_postgres
wait_for_service overstate
./scripts/enable-api-tls.sh
if [ "$SEED" -eq 1 ]; then
    ./scripts/seed-mock.sh --force
fi
echo "stack rebuilt: https://127.0.0.1:8000 (dev cert, add an exception or -k)"
