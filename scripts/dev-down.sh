#!/usr/bin/env bash
# Take the dev stack down. Pass -v/--volumes to also drop postgres data.
set -euo pipefail
cd "$(dirname "$0")/.."
# shellcheck source=lib.sh
source scripts/lib.sh

COMPOSE=$(detect_compose)
FLAGS=""
if [ "${1:-}" = "-v" ] || [ "${1:-}" = "--volumes" ]; then
    FLAGS="--volumes"
fi
# shellcheck disable=SC2086
$COMPOSE -f "$COMPOSE_FILE" down $FLAGS
