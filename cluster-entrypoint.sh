#!/bin/bash
# Pod-aware entrypoint for the salt-master cluster.
#
# Invariants (the shared ConfigMap cannot express per-pod values,
# and the base image regenerates /etc/salt/master at every boot):
# - `interface` is the pod IP (salt requires an IP literal to bind).
# - `id` / `cluster_node_id` are the stable pod DNS name
#   (<POD_NAME>.<headless-svc>); `cluster_peers` is the other
#   members (self only when the set would otherwise be empty, so
#   Salt's empty-peers preflight cannot fire). Including self in a
#   3-node set makes Raft count a loopback vote twice
#   (CandidacyError, empty voter set, salt-api cluster_retry 401s).
# - Identity is a writable drop-in on the keys PVC
#   (`cluster-identity.conf`), included from the shared ConfigMap,
#   written synchronously before exec so the first preflight always
#   sees peers. The watcher restamps that file only; it never edits
#   /etc/salt/master (the base chowns that tree under `set -e`).
# - `.cluster_ready` means this node sees a Raft leader (not "peer
#   pubs exist"). Deleted at wrapper start. `.joined` still means
#   every live peer's key is on disk, for recovery only.
# - cluster.pem/pub are copied from Secret salt-master-cluster-keys
#   onto the PVC before the daemon starts, so a pod cannot mint its
#   own cluster identity.
#
# Peers come from the Kubernetes API (EndpointSlices, all pod names
# incl. self via publishNotReadyAddresses) with constructed-name
# fallback; the API set additionally drives dead-key pruning and the
# stalled-join rejoin. Fallback keeps stamp-only behavior.
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
# <NODE_NAME>.pem/pub copies (same key content, daemon-owned) so the
# migration finds its target and is skipped. Refresh on content drift
# so a rotated Secret takes effect instead of a stale PVC copy
# shadowing the new key. KEYS_DIR/KEY_OWNER/NODE_NAME are overridable
# for tests.
KEYS_DIR="${KEYS_DIR:-/home/salt/data/keys}"
KEY_OWNER="${KEY_OWNER:-salt:salt}"
# Mirrors cluster_pki_dir in master.conf; override for tests.
PEER_KEYS_DIR="${PEER_KEYS_DIR:-$KEYS_DIR/_cluster/peers}"
CLUSTER_PKI_DIR="${CLUSTER_PKI_DIR:-$KEYS_DIR/_cluster}"
# Owner-held cluster.pem/pub (Secret salt-master-cluster-keys). Copied
# onto the PVC before the daemon starts so Salt cannot mint a per-pod
# cluster identity. Unset/missing in compose: skip, leave generation
# to Salt. Override for tests.
CLUSTER_KEYS_SRC="${CLUSTER_KEYS_SRC:-/home/salt/data/cluster-keys}"
IDENTITY_CONF="${IDENTITY_CONF:-$KEYS_DIR/cluster-identity.conf}"
READY_MARK="${READY_MARK:-$KEYS_DIR/.cluster_ready}"

preseed_master_keys() {
  for ext in pem pub; do
    src="$KEYS_DIR/master.$ext"
    dest="$KEYS_DIR/$NODE_NAME.$ext"
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

preseed_cluster_keys() {
  # Pin cluster.pem/pub from the Secret onto cluster_pki_dir before
  # the daemon can find_or_create_keys(name="cluster"). A missing
  # source (compose) is a no-op. Refresh on content drift so a
  # rotated Secret replaces a minted PVC copy.
  src_pem="$CLUSTER_KEYS_SRC/cluster.pem"
  src_pub="$CLUSTER_KEYS_SRC/cluster.pub"
  [ -f "$src_pem" ] && [ -f "$src_pub" ] || return 0
  mkdir -p "$CLUSTER_PKI_DIR"
  for ext in pem pub; do
    src="$CLUSTER_KEYS_SRC/cluster.$ext"
    dest="$CLUSTER_PKI_DIR/cluster.$ext"
    if [ ! -f "$dest" ] || ! cmp -s "$src" "$dest"; then
      rm -f "$dest"
      cp -f "$src" "$dest"
      chown "$KEY_OWNER" "$dest"
      chmod 0400 "$dest"
      log "pre-seeded $dest from cluster-keys Secret"
    fi
  done
}

log() {
  echo "$(date -u +%FT%TZ) $*" >>"$LOG"
}

peers_live() {
  getent hosts "$SVC" 2>/dev/null | awk '{print $1}' | sort -u || true
}

peers_fallback() {
  # Constructed stable peer names when the Kubernetes API is
  # unreachable: StatefulSet ordinals 0..replicas-1 under the pod-name
  # prefix. IPs from getent cannot map back to names (no reverse
  # DNS), and a mixed IP/name set would match no identity anywhere,
  # so the fallback constructs instead of resolving. PEER_REPLICAS
  # overrides the default 3 for tests and custom topologies.
  prefix="${POD_NAME%-*}"
  replicas="${PEER_REPLICAS:-3}"
  i=0
  while [ "$i" -lt "$replicas" ]; do
    printf '%s-%s.%s\n' "$prefix" "$i" "${MASTER_HEADLESS_SERVICE:-$SVC}"
    i=$((i + 1))
  done
}

daemon_running() {
  supervisorctl status salt-master 2>/dev/null | grep -q '^salt-master *RUNNING'
}

build_want() {
  # Echo the identity block: interface stays the pod IP ($1, bind
  # address); id and cluster_node_id are the stable DNS name ($2);
  # cluster_peers is the newline-separated set ($3) minus self.
  # A solo set keeps self so Salt's empty-peers preflight cannot fire.
  pod_ip="$1"
  node_name="$2"
  peers="$3"
  want=$(printf '%s\ninterface: %s\nid: %s\ncluster_node_id: %s\ncluster_peers:' \
    "$MARKER" "$pod_ip" "$node_name" "$node_name")
  listed=""
  # Word-split on purpose: one list item per name.
  # shellcheck disable=SC2086
  for peer in $peers; do
    [ "$peer" = "$node_name" ] && continue
    want=$(printf '%s\n  - %s' "$want" "$peer")
    listed=1
  done
  if [ -z "$listed" ]; then
    want=$(printf '%s\n  - %s' "$want" "$node_name")
  fi
  printf '%s\n' "$want"
}

write_identity() {
  # Atomically replace identity drop-in $1 with want-block $2. This
  # file lives on the keys PVC and is included from the ConfigMap; the
  # daemon may read it at any instant, so rename-swap, never truncate.
  conf="$1"
  want="$2"
  dir=$(dirname "$conf")
  mkdir -p "$dir"
  tmp=$(mktemp "${conf}.XXXXXX")
  printf '%s\n' "$want" >"$tmp" || { rm -f "$tmp"; return 1; }
  chmod 0644 "$tmp"
  chown "$KEY_OWNER" "$tmp" 2>/dev/null || :
  mv -f "$tmp" "$conf"
}

mark_cluster_ready() {
  # Refresh $READY_MARK from cluster-ready.py (leader visible for
  # $1). Missing helper or a failed probe clears the mark so Ready
  # cannot stick to a previous boot.
  node_name="$1"
  script="${READY_PY:-$(dirname "$0")/cluster-ready.py}"
  if command -v python3 >/dev/null 2>&1 && [ -f "$script" ] \
    && python3 "$script" "$node_name" >/dev/null 2>&1; then
    touch "$READY_MARK"
  else
    rm -f "$READY_MARK"
  fi
}

stamped_have() {
  # Echo the currently stamped identity block from $1 (empty when the
  # file was never stamped, e.g. first boot).
  # NOTE: the marker line itself is part of the block ($0==m
  # prints it); without that, `have` can never equal `want`.
  awk -v m="$MARKER" '$0==m{s=1;print;next} s&&/^$/{s=0;next} !s{next} {print}' \
    "$1" || true
}

stamp_identity() {
  # Replace any stamped block in $1 with the want block $2: drop the
  # old range, neutralize base id/interface lines (skip comments:
  # idempotent), then append fenced by blanks (leading blank separates,
  # trailing blank terminates the range for stamped_have).
  # The swap is atomic (rename): the daemon may read the file at any
  # instant, including mid-stamp, and a truncate-and-rewrite would
  # hand it a half-written config (empty cluster_peers preflight).
  # Metadata is copied first so the rename preserves owner and mode.
  # No sed -i: its backup-suffix argument differs between GNU and BSD
  # sed, so rewrite through temp files like below.
  conf="$1"
  want="$2"
  tmp=$(mktemp "${conf}.XXXXXX")
  awk -v m="$MARKER" '$0==m{s=1;next} s&&/^$/{s=0;next} !s' \
    "$conf" >"$tmp" || return 1
  # Neutralize base lines (`&` replays the match portably — no backreference).
  tmp2=$(mktemp "${conf}.XXXXXX")
  sed -E '/^#/! s/^(id|interface):/# superseded by cluster-entrypoint: &/' \
    "$tmp" >"$tmp2" || { rm -f "$tmp" "$tmp2"; return 1; }
  rm -f "$tmp"
  printf '\n%s\n\n' "$want" >>"$tmp2" || { rm -f "$tmp2"; return 1; }
  # Best-effort metadata copy (--reference is GNU-only; the macOS
  # fallback only runs in tests/dev, where no daemon reads
  # concurrently). The rename itself is atomic everywhere.
  chmod --reference="$conf" "$tmp2" 2>/dev/null || chmod 0644 "$tmp2"
  chown --reference="$conf" "$tmp2" 2>/dev/null || :
  mv -f "$tmp2" "$conf"
}

wait_for_peer_dns() {
  # Wait until peer DNS resolves before the base entrypoint starts the
  # daemon: its first preflight fails when cluster_peers is empty and
  # the stamp below is what provides it. Never fails the boot: after
  # BOOT_WAIT_SECS the boot proceeds and the watcher plus supervisor
  # retries take over exactly as before.
  waited=0
  limit="${BOOT_WAIT_SECS:-120}"
  while [ -z "$(peers_live)" ] && [ "$waited" -lt "$limit" ]; do
    sleep 2
    waited=$((waited + 2))
  done
  if [ -n "$(peers_live)" ]; then
    log "peer DNS ready"
  else
    log "peer DNS not ready after ${limit}s, proceeding anyway"
  fi
}

apply_maiden_stamp() {
  # Stamp want-block $2 into conf $1, then bounce an already-running
  # daemon once ($3 = 1 when this boot already bounced). The base
  # entrypoint regenerates the config and starts the daemon
  # concurrently with the watcher, so the daemon may run on the
  # pre-stamp file; the bounce makes the maiden identity take effect
  # deterministically. Echoes 1 when a bounce was issued, else 0.
  conf="$1"
  want="$2"
  bounced="$3"
  stamp_identity "$conf" "$want"
  log "stamped identity (maiden)"
  if [ "$bounced" = 0 ] && daemon_running; then
    log "maiden stamp landed after daemon start — restarting salt-master"
    if supervisorctl restart salt-master >>"$LOG" 2>&1; then
      echo 1
    else
      log "maiden restart failed, will retry"
      echo 0
    fi
  else
    echo "$bounced"
  fi
}

api_query() {
  # Echo cluster-peers.py output for mode $1 (empty = peer DNS
  # names), or fail so the caller falls back to constructed names.
  # Every dependency is optional: python3, the script next to this
  # one, a readable SA token; the API itself may also refuse. Empty
  # output fails too: an empty member set is never actionable.
  command -v python3 >/dev/null 2>&1 || return 1
  script="${PEERS_PY:-$(dirname "$0")/cluster-peers.py}"
  [ -f "$script" ] || return 1
  [ -r "${SA_TOKEN_FILE:-/var/run/secrets/kubernetes.io/serviceaccount/token}" ] || return 1
  if [ -n "${1:-}" ]; then
    out=$(MASTER_HEADLESS_SERVICE="${MASTER_HEADLESS_SERVICE:-salt-master}" MASTER_STATEFULSET="${MASTER_STATEFULSET:-salt-master}" python3 "$script" "$1" 2>/dev/null) || return 1
  else
    out=$(MASTER_HEADLESS_SERVICE="${MASTER_HEADLESS_SERVICE:-salt-master}" python3 "$script" 2>/dev/null) || return 1
  fi
  [ -n "$out" ] || return 1
  printf '%s\n' "$out"
}

api_peers() {
  api_query ""
}

api_replicas() {
  api_query replicas
}

prune_dead_keys() {
  # Delete peer pubs in $1 absent from the newline-separated
  # authoritative API set $2 (stable DNS names). The shared
  # master.pub is never touched; callers only pass complete views.
  dir="$1"
  endpoints="$2"
  [ -d "$dir" ] || return 0
  for f in "$dir"/*.pub; do
    [ -e "$f" ] || continue
    base=$(basename "$f" .pub)
    case "$base" in
      ''|master) continue ;;
    esac
    if ! printf '%s\n' "$endpoints" | grep -qx "$base"; then
      rm -f "$f"
      log "pruned stale peer key $base"
    fi
  done
}

peer_recovery_action() {
  # Decide one recovery step for the API endpoint set $1 against the
  # peer key dir $2 (state dir $3, self node name $4, consecutive
  # incomplete count $5). Echoes exactly one of: ok | wait | bounce.
  # The caller performs the bounce; this function only records its
  # timestamp so restarts stay rate-limited across daemon restarts.
  rc_endpoints="$1"
  rc_peers_dir="$2"
  rc_state_dir="$3"
  rc_self="$4"
  rc_n="$5"
  rc_joined="$rc_state_dir/.joined"
  rc_last="$rc_state_dir/.last-recover-bounce"
  rc_after="${RECOVER_AFTER_N:-2}"
  rc_min_secs="${RECOVER_MIN_SECS:-600}"
  rc_missing=""
  # Word-split on purpose: one token per peer name.
  # shellcheck disable=SC2086
  for rc_peer in $rc_endpoints; do
    if [ ! -f "$rc_peers_dir/$rc_peer.pub" ]; then
      rc_missing="$rc_missing $rc_peer"
    fi
  done
  if [ -z "$rc_missing" ]; then
    touch "$rc_joined"
    echo ok
    return 0
  fi
  case " $rc_missing " in
    *" $rc_self "*)
      # Fresh identity: our own key is absent because the join has not
      # happened yet. Reset and wait — never bounce a joining pod.
      rm -f "$rc_joined"
      echo wait
      return 0
      ;;
  esac
  [ -f "$rc_joined" ] || { echo wait; return 0; }
  if [ "$rc_n" -ge "$rc_after" ]; then
    rc_now=$(date +%s)
    rc_last_bounce=0
    if [ -f "$rc_last" ]; then
      rc_last_bounce=$(cat "$rc_last")
      case "$rc_last_bounce" in
        ''|*[!0-9]*) rc_last_bounce=0 ;;
      esac
    fi
    if [ $((rc_now - rc_last_bounce)) -ge "$rc_min_secs" ]; then
      echo "$rc_now" >"$rc_last"
      echo bounce
      return 0
    fi
  fi
  echo wait
  return 0
}

maiden_stamp_fallback() {
  # Stamp the constructed fallback set ($2 pod IP, $3 node name) into
  # conf $1 when it differs, via the maiden path ($4 = already
  # bounced flag). Instant by design: no API call, so phase-1 wins
  # the race with the base entrypoint. Echoes the updated flag.
  conf="$1"
  pod_ip="$2"
  node_name="$3"
  bounced="$4"
  peers=$(peers_fallback)
  want=$(build_want "$pod_ip" "$node_name" "$peers")
  if [ "$(stamped_have "$conf")" != "$want" ]; then
    apply_maiden_stamp "$conf" "$want" "$bounced"
  else
    echo "$bounced"
  fi
}

if [ -n "${POD_NAME:-}" ] && [ -n "${POD_IP:-}" ]; then
  # Stable cluster identity: the pod DNS name survives recreates
  # while the pod IP does not (see header). NODE_NAME is overridable
  # for tests.
  NODE_NAME="${NODE_NAME:-$POD_NAME.${MASTER_HEADLESS_SERVICE:-$SVC}}"
  # Synchronous, before the base entrypoint starts the daemon below.
  preseed_master_keys
  preseed_cluster_keys
  wait_for_peer_dns
  # Each boot re-proves join and Raft-ready: drop previous markers
  # so the readiness probe holds the pod out of the Services.
  rm -f "$KEYS_DIR/.joined" "$READY_MARK"
  # Identity drop-in on the PVC, included from the ConfigMap: the
  # daemon's first preflight reads this file. Prefer live API names,
  # constructed ordinals otherwise. Never wait on supervisord.
  if api_out=$(api_peers); then
    peers="$api_out"
  else
    peers=$(peers_fallback)
  fi
  want=$(build_want "$POD_IP" "$NODE_NAME" "$peers")
  write_identity "$IDENTITY_CONF" "$want"
  log "stamped identity (boot peers: $(echo "$peers" | tr '\n' ' '))"
  (
    log "watching $IDENTITY_CONF as $POD_NAME ($POD_IP)"
    staged_want=""
    staged_n=0
    last_api_peers=""
    recover_n=0
    while true; do
      if api_out=$(api_peers); then
        peers="$api_out"
        api_ok=1
      else
        peers=$(peers_fallback)
        api_ok=0
      fi
      if [ -n "$peers" ]; then
        want=$(build_want "$POD_IP" "$NODE_NAME" "$peers")
        have=$(stamped_have "$IDENTITY_CONF")
        if [ "$have" != "$want" ]; then
          stamp_now=0
          if [ -z "$have" ]; then
            stamp_now=1
          else
            if [ "$want" = "$staged_want" ]; then
              staged_n=$((staged_n + 1))
            else
              staged_want="$want"
              staged_n=0
            fi
            [ "$staged_n" -ge 1 ] && stamp_now=1
          fi
          if [ "$stamp_now" = 1 ]; then
            write_identity "$IDENTITY_CONF" "$want"
            log "stamped identity (peers: $(echo "$peers" | tr '\n' ' '))"
            staged_want=""
            staged_n=0
          fi
        else
          staged_want=""
          staged_n=0
        fi
        # Prune and stalled-join bounce only on a complete replica
        # view: a shrinking slice during OrderedReady is not a stall.
        if [ "$api_ok" = 1 ]; then
          if [ "$peers" != "$last_api_peers" ]; then
            recover_n=0
            last_api_peers="$peers"
          fi
          if replicas=$(api_replicas) \
            && [ "$(printf '%s\n' "$peers" | grep -c .)" = "$replicas" ]; then
            prune_dead_keys "$PEER_KEYS_DIR" "$peers"
            recovery=$(peer_recovery_action "$peers" "$PEER_KEYS_DIR" "$KEYS_DIR" "$NODE_NAME" "$recover_n")
            case "$recovery" in
              ok) recover_n=0 ;;
              wait) recover_n=$((recover_n + 1)) ;;
              bounce)
                recover_n=0
                log "peer key exchange stalled, restarting salt-master to rejoin"
                if supervisorctl restart salt-master >>"$LOG" 2>&1; then
                  :
                else
                  log "recovery restart failed, will retry"
                fi
                ;;
            esac
          else
            recover_n=0
          fi
        fi
      else
        log "peer DNS not ready yet"
      fi
      mark_cluster_ready "$NODE_NAME"
      sleep 10
    done
  ) >>"$LOG" 2>&1 &
fi

exec /sbin/entrypoint.sh "$@"
