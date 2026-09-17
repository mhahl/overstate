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
# So append both as this pod's StatefulSet DNS name (which also
# resolves for peer connections) and keep reconciling: the base
# rewrites the file in several passes, so re-append whenever the
# marker is missing. Later YAML keys win, so these beat base
# defaults. salt-master-0 sorts first on every pod, so pod 0 founds
# and pod 1 joins, on every boot. Outside Kubernetes (dev compose)
# POD_NAME is unset and the base behavior is untouched.
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
    for _ in $(seq 1 300); do
      if [ -s "$MASTER_CONF" ] && ! grep -qF "$MARKER" "$MASTER_CONF"; then
        if printf '\n%s\ninterface: %s.salt-master\nid: %s.salt-master\n' \
          "$MARKER" "$POD_NAME" "$POD_NAME" >>"$MASTER_CONF"; then
          log "appended identity"
        else
          log "append FAILED"
        fi
      fi
      sleep 1
    done
    log "watch done"
  ) >>"$LOG" 2>&1 &
fi

exec /sbin/entrypoint.sh "$@"
