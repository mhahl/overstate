#!/bin/sh
# App container entrypoint: migrate, then serve HTTPS when certs are
# mounted, else plain HTTP with a loud warning (plain HTTP is never valid
# outside local dev).
set -eu
try_migrate() {
  alembic upgrade head 2> /tmp/mig.log || {
    if grep -q "already exists" /tmp/mig.log \
        && ! alembic current 2>/dev/null | grep -qE '^[0-9a-f]+'; then
      # Version-less database created by create_all: its schema already
      # matches head, so stamp it before upgrading. Never stamp a
      # versioned database: replicas racing one migration also report
      # "already exists", and stamping there rewinds version history and
      # bricks later boots (2026-10-18: a stamp of 95ffc8849775 re-ran
      # ADD role forever).
      alembic stamp head && alembic upgrade head 2> /tmp/mig.log
    else
      # Versioned database, a different error, or a migration race:
      # retry without stamping and let the winner advance the version.
      sleep 2
      alembic upgrade head 2> /tmp/mig.log
    fi
  }
}
MIGRATED=""
for i in $(seq 1 30); do
  if try_migrate; then
    MIGRATED=1
    break
  fi
  echo "migration attempt $i failed; retrying..." >&2
  sleep 2
done
if [ -z "$MIGRATED" ]; then
  echo "error: migrations did not apply; refusing to boot" >&2
  tail -5 /tmp/mig.log >&2
  exit 1
fi
ARGS="-b 0.0.0.0:8000"
# SSE streams hold a sync worker for the stream lifetime (~60s), so one
# worker stalls every other request; default to more than one.
WORKERS="${WEB_CONCURRENCY:-4}"
ARGS="$ARGS -w $WORKERS"
# Worker timeout must exceed the dashboard's worst-case synchronous Salt
# budget (~7 sequential probe calls x dashboard.SYNC_HTTP_TIMEOUT) so a
# sick master degrades the page instead of killing the worker mid-request.
ARGS="$ARGS --timeout 90"
if [ -n "${TLS_CERT:-}" ] && [ -n "${TLS_KEY:-}" ] \
    && [ -f "$TLS_CERT" ] && [ -f "$TLS_KEY" ]; then
  echo "serving HTTPS (cert $TLS_CERT)"
  ARGS="$ARGS --certfile=$TLS_CERT --keyfile=$TLS_KEY"
else
  echo "WARNING: TLS_CERT/TLS_KEY not mounted; serving plain HTTP" >&2
fi
# shellcheck disable=SC2086
exec gunicorn $ARGS overstate_ui.wsgi:app
