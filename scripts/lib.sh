# Shared helpers for the Overstate dev scripts. Source, do not execute.
# shellcheck shell=bash

COMPOSE_FILE="${COMPOSE_FILE:-compose.yml}"

detect_compose() {
    if podman compose version >/dev/null 2>&1; then
        echo "podman compose"
    elif command -v podman-compose >/dev/null 2>&1; then
        echo "podman-compose"
    else
        echo "error: no compose provider (need 'podman compose' plugin or podman-compose)" >&2
        return 1
    fi
}

wait_for_postgres() {
    local tries=30
    while [ "$tries" -gt 0 ]; do
        if $COMPOSE exec -T postgres pg_isready -U overstate >/dev/null 2>&1; then
            return 0
        fi
        tries=$((tries - 1))
        sleep 2
    done
    echo "error: postgres did not become ready" >&2
    return 1
}

wait_for_service() {
    local service=$1
    local tries=30
    while [ "$tries" -gt 0 ]; do
        if $COMPOSE ps 2>/dev/null | grep -E "$service.*Up" >/dev/null; then
            return 0
        fi
        tries=$((tries - 1))
        sleep 2
    done
    echo "error: service '$service' did not come up" >&2
    return 1
}
