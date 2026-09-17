#!/bin/bash
# Pod-aware entrypoint for the salt-master cluster.
#
# Two cluster mechanisms key on per-pod identity that the shared
# ConfigMap cannot express, and the base image regenerates
# /etc/salt/master at every boot (wiping anything baked in):
#
# - Founder election sorts on `interface`; with the default 0.0.0.0
#   every pod sorts first and bootstraps a SOLO cluster.
# - The AES key exchange addresses peers by `cluster_peers` entries,
#   and each node looks its own entry up by its bare `id`; the base
#   default (`master` on every pod) matches nothing.
#
# So stamp both as this pod's StatefulSet DNS name (which also
# resolves for peer connections). The base rewrites the file in
# several passes, so normalize (never duplicate: salt's strict YAML
# loader rejects duplicate keys) for the first two minutes — boot
# only; the daemon reads the file once at startup. Later YAML keys
# win, so these beat base defaults. salt-master-0 sorts first on
# every pod, so pod 0 founds and pod 1 joins, on every boot. Outside
# Kubernetes (dev compose) POD_NAME is unset and the base behavior is
# untouched.
set -u
MASTER_CONF=/etc/salt/master
MARKER="# Pod identity for cluster election (POD_NAME)."
LOG=/tmp/cluster-entrypoint.log

log() {
  echo "$(date -u +%FT%TZ) $*" >>"$LOG"
}

if [ -n "${POD_NAME:-}" ]; then
  (
    log "watching $MASTER_CONF as $POD_NAME"
    want_if="interface: ${POD_NAME}.salt-master"
    want_id="id: ${POD_NAME}.salt-master"
    for _ in $(seq 1 120); do
      if [ -s "$MASTER_CONF" ]; then
        n=$(grep -cF -e "$want_if" "$MASTER_CONF" || true)
        if [ "$n" != 1 ]; then
          tmp=$(mktemp)
          grep -vF -e "$MARKER" -e "$want_if" -e "$want_id" \
            "$MASTER_CONF" >"$tmp" && cat "$tmp" >"$MASTER_CONF"
          rm -f "$tmp"
          printf '\n%s\n%s\n%s\n' "$MARKER" "$want_if" "$want_id" \
            >>"$MASTER_CONF"
          log "stamped identity (had $n copies)"
        fi
      fi
      sleep 1
    done
    log "watch done"
  ) >>"$LOG" 2>&1 &
fi

exec /sbin/entrypoint.sh "$@"
