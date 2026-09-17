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
# So stamp interface/id/peers from the pod IP: peers resolve live
# from the headless service (all pod IPs, ourselves included thanks
# to publishNotReadyAddresses), making the election set identical on
# every pod — exactly one founder. `cluster_peers` lives ONLY here,
# never in the shared ConfigMap (drop-ins beat the main file, so a
# static entry there would win over the stamp and mismatch it).
#
# Pod IPs change on every recreate while the daemon reads config
# once at startup, so a sibling recreate orphans this pod's peer
# list. The loop therefore runs forever: when live DNS disagrees
# with the stamped peers twice in a row, it restamps and bounces
# the daemon (pod IP unchanged, so the sibling's view of us stays
# valid). Restarts are jittered so two pods never bounce together,
# skipped while the daemon is still starting, and logged.
# Outside Kubernetes (dev compose) POD_NAME/POD_IP are unset and the
# base behavior is untouched.
set -u
MASTER_CONF=/etc/salt/master
MARKER="# Pod identity for cluster election (POD_NAME)."
LOG=/tmp/cluster-entrypoint.log
SVC=salt-master
# Salt 3008 migrates the legacy master.pem/pub to <id>.pem/pub on every
# boot where <id>.pem is absent, then DELETES the legacy files
# (MasterKeys._setup_keys → cache.flush on the master_keys bank). Our
# master.pem/pub are read-only Secret subPath mounts, so the delete
# raises SaltCacheError and the daemon never starts. Pre-seed writable
# <POD_IP>.pem/pub copies (same key content, daemon-owned) so the
# migration finds its target and is skipped. Refresh on content drift
# so a rotated Secret takes effect instead of a stale PVC copy
# shadowing the new key. KEYS_DIR/KEY_OWNER are overridable for tests.
KEYS_DIR="${KEYS_DIR:-/home/salt/data/keys}"
KEY_OWNER="${KEY_OWNER:-salt:salt}"

preseed_master_keys() {
  for ext in pem pub; do
    src="$KEYS_DIR/master.$ext"
    dest="$KEYS_DIR/$POD_IP.$ext"
    if [ ! -f "$dest" ] || ! cmp -s "$src" "$dest"; then
      # rm first (portable, and never writes through a symlink the
      # way cp -f alone would) — the daemon is not running yet.
      rm -f "$dest"
      cp -f "$src" "$dest"
      chown "$KEY_OWNER" "$dest"
      chmod 0400 "$dest"
      log "pre-seeded $dest from Secret keypair"
    fi
  done
}

log() {
  echo "$(date -u +%FT%TZ) $*" >>"$LOG"
}

peers_live() {
  getent hosts "$SVC" 2>/dev/null | awk '{print $1}' | sort -u || true
}

daemon_running() {
  supervisorctl status salt-master 2>/dev/null | grep -q '^salt-master *RUNNING'
}

if [ -n "${POD_NAME:-}" ] && [ -n "${POD_IP:-}" ]; then
  # Synchronous, before the base entrypoint starts the daemon below.
  preseed_master_keys
  (
    log "watching $MASTER_CONF as $POD_NAME ($POD_IP)"
    last_peers=""
    pending=""
    pending_n=0
    while true; do
      if [ -s "$MASTER_CONF" ]; then
        peers=$(peers_live)
        if [ -n "$peers" ]; then
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
            # Neutralize base lines (skip comments: idempotent; `&`
            # replays the match portably — no backreference).
            sed -i -E '/^#/! s/^(id|interface):/# superseded by cluster-entrypoint: &/' \
              "$MASTER_CONF"
            # Leading blank separates, trailing blank terminates the
            # block for the awk range above.
            printf '\n%s\n\n' "$want" >>"$MASTER_CONF"
            log "stamped identity (peers: $(echo "$peers" | tr '\n' ' '))"
          fi
          # Bounce the daemon onto fresh peers only when a previously
          # converged set actually changes (never on first stamp),
          # confirmed twice to ride out DNS blips.
          if [ -z "$last_peers" ]; then
            last_peers="$peers"
            pending=""
            pending_n=0
          elif [ "$peers" = "$last_peers" ]; then
            pending=""
            pending_n=0
          elif [ "$peers" = "$pending" ]; then
            pending_n=$((pending_n + 1))
            if [ "$pending_n" -ge 1 ] && daemon_running; then
              sleep $((RANDOM % 15))
              log "peers changed ($pending) — restarting salt-master"
              if supervisorctl restart salt-master >>"$LOG" 2>&1; then
                last_peers="$peers"
                pending=""
                pending_n=0
              else
                log "restart failed, will retry"
              fi
            fi
          else
            pending="$peers"
            pending_n=0
          fi
        else
          log "peer DNS not ready yet"
        fi
      fi
      sleep 10
    done
  ) >>"$LOG" 2>&1 &
fi

exec /sbin/entrypoint.sh "$@"
