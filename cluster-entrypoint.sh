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
#   default (`id: master` on every pod) matches nothing.
#
# So stamp both as this pod's StatefulSet DNS name (which also
# resolves for peer connections). The base rewrites the file in
# several passes, so normalize for the first two minutes — boot
# only; the daemon reads the file once at startup. Normalizing
# means exactly one copy each: our previous block is removed, any
# base `id:`/`interface:` lines are commented out (salt's strict YAML
# loader rejects duplicate keys outright), then ours are appended.
# salt-master-0 sorts first on every pod, so pod 0 founds and pod 1
# joins, on every boot. Outside Kubernetes (dev compose) POD_NAME is
# unset and the base behavior is untouched.
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
        n_if=$(grep -cF -e "$want_if" "$MASTER_CONF" || true)
        n_id=$(grep -cF -e "$want_id" "$MASTER_CONF" || true)
        if [ "$n_if" != 1 ] || [ "$n_id" != 1 ]; then
          tmp=$(mktemp)
          # Drop every previous block of ours in one pass (fixed
          # strings: our pod-specific values never occur elsewhere).
          grep -vF -e "$MARKER" -e "$want_if" -e "$want_id" \
            "$MASTER_CONF" >"$tmp" && cat "$tmp" >"$MASTER_CONF"
          rm -f "$tmp"
          # Neutralize base lines (skip comments so this is idempotent;
          # `&` replays the match portably — no backreference).
          sed -i -E '/^#/! s/^(id|interface):/# superseded by cluster-entrypoint: &/' \
            "$MASTER_CONF"
          printf '\n%s\n%s\n%s\n' "$MARKER" "$want_if" "$want_id" \
            >>"$MASTER_CONF"
          log "stamped identity (had $n_if interface, $n_id id lines)"
        fi
      fi
      sleep 1
    done
    log "watch done"
  ) >>"$LOG" 2>&1 &
fi

exec /sbin/entrypoint.sh "$@"
