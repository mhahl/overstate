#!/usr/bin/env bash
# Load mock data into the running dev stack (runs the seeder inside the
# overstate container, which already has DATABASE_URL pointed at postgres).
# Usage: seed-mock.sh [--force]
set -euo pipefail
cd "$(dirname "$0")/.."
# shellcheck source=lib.sh
source scripts/lib.sh

COMPOSE=$(detect_compose)
# shellcheck disable=SC2086
$COMPOSE -f "$COMPOSE_FILE" exec -T overstate python -m overstate_ui.seed_mock "$@"
