#!/bin/sh
# Sync the file-roots checkout that the Overstate file browser reads.
# The app never writes here; this script (or a cron/sidecar running it) is
# the only writer. Fails closed: non-ff updates are refused, never forced.
set -eu
TARGET="${1:-./salt-srv}"
if [ ! -d "$TARGET/.git" ]; then
  echo "error: $TARGET is not a git checkout" >&2
  exit 1
fi
git -C "$TARGET" pull --ff-only
git -C "$TARGET" rev-parse --short HEAD
