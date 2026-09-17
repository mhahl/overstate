#!/bin/bash
# Pod-aware entrypoint for the salt-master cluster.
#
# Three cluster mechanisms key on per-pod values that the shared
# ConfigMap cannot express, and the base image regenerates
# /etc/salt/master at every boot (wiping anything baked in):
#
# - Founder election sorts on `interface`; with the default 0.0.0.0
#   every pod sorts first and bootstraps a SOLO cluster.
# - The AES key exchange addresses peers by `cluster_peers` entries,
#   and each node looks its own entry up by its bare `id`; the base
#   default (`master` on every pod) matches nothing.
# - `interface` must be an IP literal (salt brackets it at startup),
#   so DNS names are rejected outright.
#
# So stamp interface/id/peers from the pod IP every boot: peers are
# resolved live from the headless service (all pod IPs, ourselves
# included thanks to publishNotReadyAddresses), which also makes the
# election set identical on every pod — exactly one founder.
# `cluster_peers` lives ONLY here, never in the shared ConfigMap
# (drop-ins beat the main file, so a static entry there would win
# over this one and mismatch the stamped identity). The base
# rewrites the file in several passes, so normalize for the first
# two minutes; salt's strict YAML loader rejects duplicate keys, so
# the old block is removed before the new one lands. Outside
# Kubernetes (dev compose) POD_NAME/POD_IP are unset and the base
# behavior is untouched.
set -u
MASTER_CONF=/etc/salt/master
MARKER="# Pod identity for cluster election (POD_NAME)."
LOG=/tmp/cluster-entrypoint.log
SVC=salt-master

log() {
  echo "$(date -u +%FT%TZ) $*" >>"$LOG"
}

if [ -n "${POD_NAME:-}" ] && [ -n "${POD_IP:-}" ]; then
  (
    log "watching $MASTER_CONF as $POD_NAME ($POD_IP)"
    for _ in $(seq 1 120); do
      if [ -s "$MASTER_CONF" ]; then
        peers=$(getent hosts "$SVC" | awk '{print $1}' | sort -u || true)
        if [ -n "$peers" ]; then
          # Desired block content (no trailing blank; both sides are
          # compared after command substitution strips them).
          want=$(printf '%s\ninterface: %s\nid: %s\ncluster_peers:' \
            "$MARKER" "$POD_IP" "$POD_IP")
          # Word-split on purpose: one list item per address.
          # shellcheck disable=SC2086
          for ip in $peers; do
            want=$(printf '%s\n  - %s' "$want" "$ip")
          done
          # NOTE: the marker line itself is part of the block ($0==m
          # prints it); without that, `have` can never equal `want`.
          have=$(awk -v m="$MARKER" '$0==m{s=1;print;next} s&&/^$/{s=0;next} !s{next} {print}' \
            "$MASTER_CONF" || true)
          if [ "$have" != "$want" ]; then
            tmp=$(mktemp)
            awk -v m="$MARKER" '$0==m{s=1;next} s&&/^$/{s=0;next} !s' \
              "$MASTER_CONF" >"$tmp" && cat "$tmp" >"$MASTER_CONF"
            rm -f "$tmp"
            # Neutralize base lines (skip comments: idempotent).
            sed -i -E '/^#/! s/^(id|interface):/# superseded by cluster-entrypoint: &/' \
              "$MASTER_CONF"
            # Leading blank separates, trailing blank terminates the
            # block for the awk range above.
            printf '\n%s\n\n' "$want" >>"$MASTER_CONF"
            log "stamped identity for $POD_IP (peers: $(echo "$peers" | tr '\n' ' '))"
          fi
        else
          log "peer DNS not ready yet"
        fi
      fi
      sleep 1
    done
    log "watch done"
  ) >>"$LOG" 2>&1 &
fi

exec /sbin/entrypoint.sh "$@"
