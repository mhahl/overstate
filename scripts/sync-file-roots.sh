#!/bin/sh
# Sync the shared-states checkout the Overstate file browser reads.
# TARGET is the srv roots: salt/ (file roots, served + browsed) beside
# pillar/ (served by the master). The app also writes here through the
# Files page (edit commits, admin push); this script stays available
# for cron/sidecar sync. Fails closed: non-ff updates are refused,
# never forced.
set -eu
TARGET="${1:-./salt-srv}"
if [ ! -d "$TARGET/.git" ]; then
  echo "error: $TARGET is not a git checkout" >&2
  exit 1
fi
git -C "$TARGET" pull --ff-only
# World-readable bits so the non-root master workers traverse the bind
# mount: a root umask of 027 otherwise hides the tree again.
chmod -R a+rX "$TARGET"
git -C "$TARGET" rev-parse --short HEAD
