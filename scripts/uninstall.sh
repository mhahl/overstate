#!/usr/bin/env bash
# Remove Overstate Quadlets. Data survives by default; --purge deletes
# config, state, and podman volumes too (no recovery).
# Usage: `sudo ./scripts/uninstall.sh [--purge]`
set -euo pipefail

ETC=/etc/overstate
VAR=/var/lib/overstate
UNITS=/etc/containers/systemd
PURGE=0

for arg in "$@"; do
  case "$arg" in
    --purge) PURGE=1 ;;
    -h|--help) echo "Usage: sudo ./scripts/uninstall.sh [--purge]"; exit 0 ;;
    *) echo "error: unknown flag $arg" >&2; exit 1 ;;
  esac
done

if [ "$(id -u)" -ne 0 ]; then
  echo "error: run as root" >&2
  exit 1
fi

echo "==> stopping and disabling units"
for u in overstate-app overstate-worker overstate-salt-master \
    overstate-postgres overstate-redis overstate-caddy; do
  systemctl disable --now "$u.service" 2>/dev/null || true
done
rm -f "$UNITS"/overstate-*.container "$UNITS"/overstate.network
systemctl daemon-reload
podman network rm overstate 2>/dev/null || true

if [ "$PURGE" -eq 1 ]; then
  read -r -p "Delete $ETC, $VAR, and the pgdata/saltdata volumes? [y/N] " answer
  if [ "$answer" = "y" ] || [ "$answer" = "Y" ]; then
    podman volume rm overstate-pgdata overstate-saltdata overstate-caddy-data \
      overstate-caddy-config 2>/dev/null || true
    rm -rf "$ETC" "$VAR"
    echo "purged."
  else
    echo "kept data; units removed."
  fi
else
  echo "units removed; config and data kept in $ETC and $VAR."
fi
