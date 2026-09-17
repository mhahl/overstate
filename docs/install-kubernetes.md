# Install Overstate on Kubernetes

Design rationale, assumptions, and risks: `architecture-kubernetes.md`.
This page is the how-to.

Production target: a cluster with Traefik (443 ingress plus the
4505/4506 TCP entrypoints, managed in a separate repo), a default
StorageClass (Longhorn here), and free DNS for `overstate.sigaint.au`.
Everything Overstate manages lives in the `overstate` namespace —
never touch anything outside it.

## 1. Create the secret

Secrets are never committed. Create `overstate-secrets` before the
first apply:

```sh
kubectl -n overstate create secret generic overstate-secrets \
  --from-literal=SECRET_KEY="$(openssl rand -hex 32)" \
  --from-literal=ADMIN_PASSWORD="$(openssl rand -hex 16)" \
  --from-literal=REDIS_PASSWORD="$(openssl rand -hex 16)" \
  --from-literal=SALT_API_USER_PASS="$(openssl rand -hex 24)"
```

`ADMIN_PASSWORD` seeds the first admin login (change it in Users
afterwards). The other three have no recoverable default — losing
the Secret means regenerating and restarting every workload.

## 2. Apply

```sh
kubectl apply -k deploy/kubernetes
```

The kustomization pins the app/worker image tag (`images:`) — bump
`newTag` per release and re-apply. The app Deployment resolves its
database password from the CNPG-generated `overstate-db-app` secret;
salt-api credentials come from `overstate-secrets`.

File roots live on the shared `srv-data` volume: the app mounts it
writable (single writer — the git checkout for the file browser), the
masters read-only and serve states from it.

Verify:

```sh
kubectl -n overstate get pods
kubectl -n overstate exec statefulset/salt-master -- salt-key --list-all
```

## 3. Restore runbook

Config damage (bad master ConfigMap edit) is recovered from the UI:
Master config → **Revert to last snapshot** re-patches the previous
whole ConfigMap and restarts the masters. The last 20 revisions live
in the `salt-master-config-history` ConfigMap.

If the UI itself is locked out (bad `api.conf`):

```sh
# Read the last good snapshot:
kubectl -n overstate get configmap salt-master-config-history -o jsonpath='{.data.history\.json}'
# Re-apply its .data by hand:
kubectl -n overstate edit configmap salt-master-config
# Then roll the masters one at a time:
kubectl -n overstate rollout restart statefulset/salt-master
kubectl -n overstate rollout status statefulset/salt-master
```

Database damage is CNPG's domain (`overstate-db` cluster backups);
accepted minion keys live on the masters' per-pod `keys` PVCs and
survive reschedules.

## 4. Shared master keypair (failover trio)

All three master pods present one identity from the owner-held
`salt-master-keys` Secret (never committed). To bootstrap from the
live single master — same bytes, no rotation, minions unaffected:

```sh
kubectl -n overstate cp salt-master-0:/home/salt/data/keys/master.pem /tmp/mk.pem
kubectl -n overstate cp salt-master-0:/home/salt/data/keys/master.pub /tmp/mk.pub
chmod 600 /tmp/mk.pem
kubectl -n overstate create secret generic salt-master-keys \
  --from-file=master.pem=/tmp/mk.pem --from-file=master.pub=/tmp/mk.pub
shred -u /tmp/mk.pem /tmp/mk.pub 2>/dev/null || rm -P /tmp/mk.pem /tmp/mk.pub
```

Rotation (rare — it changes master identity fleet-wide): generate a
fresh pair (`salt-key --gen-keys master` on any machine with salt),
replace the Secret, roll the pods one at a time, and confirm every
minion trusts the new key before the last old pod leaves. A botched
rotation needs the re-acceptance procedure, not just revert.

## 5. Shared job cache (failover trio)

Job results live in a dedicated `salt` database so either master
answers consistently. One-shot provisioning as the CNPG superuser
(password in the `overstate-db-superuser` Secret):

```sql
CREATE ROLE salt LOGIN PASSWORD '<generated>';
CREATE DATABASE salt OWNER salt;
-- then, connected to the salt database (exact DDL from the
-- postgres_local_cache returner module itself):
CREATE TABLE jids (
  jid varchar(20) PRIMARY KEY,
  started TIMESTAMP WITH TIME ZONE DEFAULT now(),
  tgt_type text NOT NULL, cmd text NOT NULL, tgt text NOT NULL,
  kwargs text NOT NULL, ret text NOT NULL, username text NOT NULL,
  arg text NOT NULL, fun text NOT NULL);
CREATE TABLE salt_returns (
  added TIMESTAMP WITH TIME ZONE DEFAULT now(),
  fun text NOT NULL, jid varchar(20) NOT NULL, return text NOT NULL,
  id text NOT NULL, success boolean);
CREATE INDEX ON salt_returns (added);
CREATE INDEX ON salt_returns (id);
CREATE INDEX ON salt_returns (jid);
CREATE INDEX ON salt_returns (fun);
ALTER TABLE jids OWNER TO salt;
ALTER TABLE salt_returns OWNER TO salt;
```

The masters read the whole returner block — password included — from
the owner-held `salt-master-db` Secret, overlaid as one more config
drop-in (`returner.conf`):

```sh
cat > /tmp/returner.conf <<'EOF'
master_job_cache: postgres_local_cache
master_job_cache.postgres.host: overstate-db-rw
master_job_cache.postgres.user: salt
master_job_cache.postgres.passwd: '<generated>'
master_job_cache.postgres.db: salt
master_job_cache.postgres.port: 5432
EOF
chmod 600 /tmp/returner.conf
kubectl -n overstate create secret generic salt-master-db \
  --from-file=returner.conf=/tmp/returner.conf
shred -u /tmp/returner.conf 2>/dev/null || rm -P /tmp/returner.conf
```

Rotate the same way (replace Secret, roll the pods one at a time).
The owned ConfigMap never holds passwords — enforced by test.

## 6. Master cluster credential

The three run as a Salt master cluster (isolated filesystem), so the
Traefik TCP round-robin is the supported topology instead of a
split-brain. Peers authenticate each other with `cluster_secret`,
overlaid as a `cluster.conf` drop-in from the owner-held
`salt-master-cluster` Secret (never committed, never visible in the
Master Settings UI):

```sh
kubectl -n overstate create secret generic salt-master-cluster \
  --from-literal=cluster.conf="cluster_secret: '$(openssl rand -hex 32)'"
```

Rotation: replace the Secret, then roll the pods one at a time — a
peer with the old secret cannot rejoin, so confirm the new pods form
the cluster before the last old pod leaves. Removing a peer for good
also means deleting its `peers/<id>.pub` from the cluster key store
on the remaining pods.

## 7. Scale up/down + stale peer cleanup

Peer identity is the stable pod DNS name
(`<POD_NAME>.salt-master`, stamped by `cluster-entrypoint.sh`), so a
reschedule keeps the same peer ID and Raft membership survives it.
The entrypoint heals the rest itself: it prefers the EndpointSlice
member list over DNS (with constructed-name fallback when the API is
unreachable), prunes dead peer keys only when the slice count equals
the StatefulSet's desired replicas, and bounces a joined pod whose
key exchange stalls (rate-limited to one restart per 10 minutes).
That needs the `salt-master` ServiceAccount plus its read-only
`salt-master-peers` Role (both in-repo), and a master image built
with `cluster-peers.py` next to the entrypoint. Scale one step at a
time and confirm the join before the next step. Never run 1 replica
except as a brief diagnostic: the image requires a non-empty
`cluster_peers` and a solo pod crash-loops until DNS returns itself.

```sh
kubectl -n overstate scale statefulset salt-master --replicas=3
```

Wait for all pods Ready — Ready means this node sees a Raft leader
AND salt-api is accepting (the probe watches `.cluster_ready` plus
TCP 8000), so an Unready pod past its first minutes is a stuck join,
not a slow start; check its entrypoint log at
`/tmp/cluster-entrypoint.log` and `salt-run cluster.members`. Then
confirm every pod holds every live peer's key:

```sh
for p in salt-master-0 salt-master-1 salt-master-2; do echo "== $p";
kubectl -n overstate exec $p -- ls /home/salt/data/keys/_cluster/peers/; done
```

A pod missing a live peer's key has a stalled join: bounce its
daemon so it rejoins the settled cluster, then recheck after ~90s:

```sh
kubectl -n overstate exec salt-master-1 -- supervisorctl restart salt-master
```

If it still misses the peer, seed the public key from a pod that
has it and bounce again (public keys only — the AES handshake still
authenticates via `cluster_secret`):

```sh
kubectl -n overstate cp salt-master-0:/home/salt/data/keys/_cluster/peers/salt-master-0.salt-master.pub /tmp/peer.pub
kubectl -n overstate cp /tmp/peer.pub salt-master-1:/home/salt/data/keys/_cluster/peers/salt-master-0.salt-master.pub
```

While the join is incomplete the event forwarder crashes with
`KeyError: 'aes'` and pillar fetches plus job returns fail
fleet-wide — fix the join first, then retest minions. If the
automatic recovery already pruned and bounced (see the entrypoint
log at `/tmp/cluster-entrypoint.log`), just verify with the
listing above. Leftover `peers/*.pub` names that are not current
DNS identities (`<pod>.salt-master`) are inert (forwarding targets
come from live `cluster_peers`, not the files) and wait for the next
automatic prune on a complete replica view.

If `salt-run cluster.members` still shows `leader_id: null` and an
empty voter set after a full OrderedReady roll of a current image,
Raft membership on the volume is poisoned. Last resort, one pod at
a time, founder first: inspect `find /var/cache/salt -iname '*raft*'`
and delete only those Raft log/snapshot files (never `cluster_pki_dir`
minion keys, never `master.pem`). Bounce that pod and recheck
`cluster.members` before touching the next.
