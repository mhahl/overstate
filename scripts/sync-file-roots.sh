#!/bin/sh
# Sync the file-roots checkout that the Overstate file browser reads.
# The app also writes here through the Files page (edit commits, admin
# push); this script stays available for cron/sidecar sync. Fails
# closed: non-ff updates are refused, never forced.
set -eu
TARGET="${1:-./salt-srv}"
if [ ! -d "$TARGET/.git" ]; then
  echo "error: $TARGET is not a git checkout" >&2
  exit 1
fi
git -C "$TARGET" pull --ff-only
git -C "$TARGET" rev-parse --short HEAD
