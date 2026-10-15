# Upstream Salt patches

Overstate runs stock Salt on the masters plus a small set of surgical
changes to Salt's experimental cluster code. This file is the complete
record: every change made to upstream Salt sources, where it lives, the
exact anchor it replaces, what breaks without it, and how it is verified.

Base at time of writing:

* Salt `3008.2`, Python `3.14.6` inside
  `ghcr.io/cdalvaro/docker-salt-master:lts@sha256:df5bb4555108f21655f97f5be11f5404a5596bd475cb8181ab2c88bd91adb12c`
  (see `Containerfile.salt-master:7`).
* Extra runtime deps baked in `Containerfile.salt-master:9-11`:
  `psycopg2-binary==2.9.13` for the shared Postgres job cache,
  `redis==6.4.0` for the shared Redis minion-data cache.
* Production topology: 3-master `StatefulSet` (`deploy/kubernetes/salt-master.yaml:11-22`),
  `OrderedReady`, one master per hostname, `minAvailable: 2`.

## Vehicle and safety net

All changes ship through `salt-cluster-identity-patch.py` (repo root),
applied once at image build time by `Containerfile.salt-master:43-46`:

```dockerfile
COPY salt-cluster-identity-patch.py /usr/local/bin/salt-cluster-identity-patch.py
RUN SALT_PY="$(ls /opt/saltstack/salt/bin/python3* ...)" \
    && "$SALT_PY" /usr/local/bin/salt-cluster-identity-patch.py \
    "$("$SALT_PY" -c 'import os, salt; ...')"
```

Mechanism:

* `PATCHES` in `salt-cluster-identity-patch.py:37-369` is a list of
  `(relpath, old, new)` tuples.
* `apply_patches()` in `salt-cluster-identity-patch.py:376-394` requires
  each `old` anchor to occur **exactly once** in its file. Zero matches
  (upstream removed/renamed the code) or 2+ matches (upstream duplicated
  it) raises `PatchError` and fails the image build loudly. There is no
  runtime patching and no fuzzy matching.
* The resolver does not hardcode a Python version path; it asks the
  installed `salt` package for its own root (`salt_root()` in
  `salt-cluster-identity-patch.py:397-401`).
* Patches are coupled to the base Salt version **by design**. A Salt
  upgrade that drifts any anchor is supposed to break the build so a
  human re-reads the upstream function completely before re-anchoring.

Tests:

* `tests/test_salt_cluster_patch.py:35-48`
  `test_applies_each_patch_exactly_once` applies every entry against
  synthetic trees.
* `tests/test_salt_cluster_patch.py:51-71` missing anchor, duplicate
  anchor, and missing file are all hard errors.
* `tests/test_salt_cluster_patch.py:74-91` pins the transport fix
  semantics: exactly one send, drop cached client, re-raise.
* `tests/test_salt_cluster_patch.py:94-114` pins the reconcile fix:
  replay `CONFIG` entries after snapshot restore.
* `tests/test_salt_cluster_patch.py:117-136` pins the storage fix:
  `SaltStorage` must construct `Cache(opts, driver="localfs")`.
* `tests/test_salt_cluster_patch.py:139-165` well-formedness: every
  replacement must mention its intent marker and every expected upstream
  file must be present.

How to verify a built image:

```sh
podman run --rm --pull=never --platform linux/amd64 \
  --entrypoint /opt/saltstack/salt/bin/python3 \
  localhost/overstate-salt-master:TAG \
  -c 'import redis; from salt.cluster.consensus.storage import SaltStorage;
opts={"cache":"redis","cachedir":"/tmp/probe"};
s=SaltStorage("probe",opts); assert s._cache.driver=="localfs";
print("raft localfs ok, redis-py", redis.__version__)'
```

## Where local state lives: `localfs` on the PVC

Three different Salt cache usages coexist and must not be confused:

| Use | Driver | Scope | Location |
|---|---|---|---|
| Minion data (grains, Pillar, Mine) | `redis` via global `cache:` | shared across all 3 masters | internal `redis` Service, DB `'0'`, `overstate_prod_*` prefixes |
| Master PKI (`keys.cache_driver`) | `localfs_key` | per-master | keys PVC |
| Raft log / vote / snapshot (`SaltStorage`) | forced `localfs` by patch | per-master | keys PVC under `cachedir` |
| Job cache returner | `pgjsonb` (Postgres) | shared | CNPG `salt` DB |

Wiring:

* `deploy/kubernetes/salt-master-config.yaml:32-33` sets
  `cluster_pki_dir: /home/salt/data/keys/_cluster` and
  `cachedir: /home/salt/data/keys/cache`.
* `deploy/kubernetes/salt-master.yaml:108-109` mounts the per-pod `keys`
  volume at `/home/salt/data/keys` (read-write).
* `deploy/kubernetes/salt-master.yaml:211-218` provisions it:
  `volumeClaimTemplates: keys`, `storageClassName: longhorn`,
  `ReadWriteOnce`, `5Gi`. The claims are `keys-salt-master-0/1/2` and
  follow their pod.
* `deploy/kubernetes/salt-master-config.yaml:45` sets
  `keys.cache_driver: localfs_key` (not `mmap_key`: `mmap` left an empty
  `master_keys` bank per PVC, each pod minted its own `cluster.pub`,
  and minions logged `The master key has changed`).
* `deploy/kubernetes/salt-master-config.yaml:39` sets the global
  `cache: redis`. Without the storage patch below, this one line would
  also redirect Raft persistence to Redis.

On-disk Raft layout (per master, all under its own PVC):

```text
<cachedir>/cluster/consensus/<node_id>/<ring_id>/       state + snapshot
<cachedir>/cluster/consensus/<node_id>/<ring_id>/log/   one key per log index
<cachedir>/.cluster_joined.<node_id>                    join sentinel
<cachedir>/health/ready                                 serving gate
/home/salt/data/keys/.cluster_ready                     leader-visible gate
```

`<node_id>` is the stable DNS name (see §1), `<ring_id>` is `cluster`
for the main group. The entrypoint deletes only the two serving gates
on every boot (`cluster-entrypoint.sh` boot section) and deliberately
keeps `consensus/` and `.cluster_joined.*`.

Why `localfs` and not shared Redis for Raft:

* Isolation: each voter must remember its own `term`/`voted_for`. A
  shared backend makes a single Redis outage or key expiry equivalent
  to losing all three voters at once.
* Durability: `SaltStorage._fsync_bank_key` fsyncs the just-written
  `<bank>/<key>.p` file and its parent directory. That path only
  implements `localfs`; other drivers silently skip fsync. A voter that
  crashes after voting must not re-vote in the same term; an acked log
  entry must survive power loss. Redis (single instance, async
  persistence) does not give that guarantee.
* Availability during rollout: switching the global cache driver while
  pods restart one at a time creates a mixed cluster where some voters
  read local disk and one voter reads empty Redis. Membership replays
  then diverge.
* Debuggability: `salt-run cluster.members` reads the **local**
  persisted log. When storage follows Redis, that runner needs the
  Redis client just to answer "who is the leader".

## Patch inventory

15 anchored replacements across 6 upstream files. Counted by
`test_applies_each_patch_exactly_once`:

| # | Upstream file | Anchor subject | Patcher lines |
|---|---|---|---|
| 1 | `salt/cluster/consensus/storage.py` | module docstring: localfs for Raft | `salt-cluster-identity-patch.py:38-52` |
| 2 | `salt/cluster/consensus/storage.py` | `SaltStorage.__init__` driver pin | `salt-cluster-identity-patch.py:53-64` |
| 3 | `salt/cluster/consensus/service.py` | Raft `node_id` | `salt-cluster-identity-patch.py:65-81` |
| 4 | `salt/cluster/consensus/service.py` | `SaltPeer` local id | `salt-cluster-identity-patch.py:82-102` |
| 5 | `salt/channel/server.py` | `_cluster_is_ready` forkserver fallback | `salt-cluster-identity-patch.py:103-137` |
| 6 | `salt/master.py` | founder join-existing | `salt-cluster-identity-patch.py:138-181` |
| 7 | `salt/channel/server.py` | founder join-existing (publish path) | `salt-cluster-identity-patch.py:182-224` |
| 8 | `salt/channel/server.py` | join-sentinel filename | `salt-cluster-identity-patch.py:225-235` |
| 9 | `salt/channel/server.py` | `bootstrap_pool` identity | `salt-cluster-identity-patch.py:236-248` |
| 10 | `salt/master.py` | `bootstrap_pool` identity | `salt-cluster-identity-patch.py:249-261` |
| 11 | `salt/master.py` | founder comparison | `salt-cluster-identity-patch.py:262-266` |
| 12 | `salt/cluster/ring_membership.py` | ownership check 1 | `salt-cluster-identity-patch.py:267-279` |
| 13 | `salt/cluster/ring_membership.py` | ownership check 2 (`ring.owns`) | `salt-cluster-identity-patch.py:280-294` |
| 14 | `salt/cluster/consensus/raft/node.py` | `reconcile_membership` replay | `salt-cluster-identity-patch.py:295-324` |
| 15 | `salt/cluster/consensus/peer.py` | drop dead cached publisher | `salt-cluster-identity-patch.py:325-368` |

## 1. Stable cluster identity (`cluster_node_id`)

### Problem

Upstream Salt 3008 experimental clustering keys identity on
`opts["interface"]`, the pod IP. In Kubernetes the pod IP changes on
every recreate (reschedule, rolling update, node drain). Consequences
observed or directly implied:

* The committed voter set accumulates dead IPs.
* A recreated pod rejoins under a new IP while the old IP remains a
  voter; quorum accounting references nodes that will never answer.
* Founder election (`bootstrap_pool[0]`), join sentinels
  (`.cluster_joined.<interface>`), and ring ownership (`ring.owns`)
  all disagree about "who am I" after an IP change.
* End state without the patch: masters defer all minion/API traffic
  with `cluster_retry` (salt-api 401s, minion `Master did not return a
  session key`, pillar/auth flap) until an operator wipes Raft state on
  all pods simultaneously.

### Fix

Introduce a free-form `cluster_node_id` opt, stamped by
`cluster-entrypoint.sh` as `<pod-name>.<headless-service>`
(e.g. `salt-master-0.salt-master`), a DNS name that survives recreates.
Every identity site prefers it and falls back to `interface` when unset,
so behavior without the opt is byte-for-byte upstream:

```python
SELF_NODE_ID = '(self.opts.get("cluster_node_id") or self.opts["interface"])'
# salt-cluster-identity-patch.py:33
```

Sites (patches 3–4, 8–13):

* `service.py`: `node_id = opts.get("cluster_node_id") or opts["interface"]`
  so the Raft node-id matches the keys in `cluster_peers` and the
  `peer_pushers` dict.
* `service.py`: `SaltPeer(..., self.opts.get("cluster_node_id") or
  self.opts["interface"], ...)` so the local side of each peer channel
  advertises the same stable id.
* `channel/server.py` + `master.py`: `bootstrap_pool = sorted({stable_id,
  *cluster_peers})` and the founder comparison
  `bootstrap_pool[0] == stable_id`. Without this, the lowest pod IP
  wins founder instead of the lowest DNS ordinal, and ordinals disagree
  across processes.
* `channel/server.py`: join sentinel becomes
  `.cluster_joined.<node_id>` instead of `.cluster_joined.<interface>`,
  so a recreated pod finds its own prior join record instead of
  treating every recreate as a first boot.
* `ring_membership.py` (2 sites): `node_id = opts.get("cluster_node_id")
  or opts.get("interface")` for ring ownership. Otherwise a pod whose IP
  changed would evaluate `ring.owns()` as a different node than its
  peers believe it to be.

### Wiring

`cluster-entrypoint.sh` stamps `interface` from `$POD_IP` and
`id`/`cluster_node_id`/`cluster_peers` from the stable DNS name into
`/home/salt/data/keys/cluster-identity.conf` on the PVC, which
`master.conf` includes (`deploy/kubernetes/salt-master-config.yaml:27-28`).
The headless Service keeps `publishNotReadyAddresses: true`
(`deploy/kubernetes/salt-master.yaml:231-234`) so peer DNS resolves
before readiness, breaking the founder chicken-and-egg.

### If removed

Pod recreates orphan Raft membership. Short-term symptom is one
unhealthy voter; long-term symptom is permanent quorum loss requiring
simultaneous Raft wipe. The build comment in
`Containerfile.salt-master:13-22` states this explicitly.

## 2. Founder joins instead of forking a second cluster

### Problem

The designated founder (`bootstrap_pool[0]`, normally
`salt-master-0.salt-master`) boots during a normal rolling restart while
peers are already live and holding the committed 3-voter config. Upstream
founder logic bootstraps a new single-node cluster in that case, forking
history. Two clusters then claim the same `cluster_id` with different
terms.

### Fix

Patches 6–7 (`salt-cluster-identity-patch.py:138-224`) add a
`_join_existing` probe before any founding decision, in both the
`SMaster` startup path (`salt/master.py`) and the publish-server path
(`salt/channel/server.py`):

```python
for _peer in self.opts.get("cluster_peers") or []:
    if _peer == _self:
        continue
    try:
        _s = _sock.create_connection((_peer, _port), timeout=2)
```

If any other peer answers on `cluster_port`/`cluster_pool_port` (4507),
the founder sets `_join_existing = True` and runs discover/join instead
of founding, logging `Founder saw live peer %s:%s — joining, not founding`.
The TCP probe is 2s per peer, no auth, no Raft RPC — just "someone is
already there".

Only the founder probes; joiners already join. Skipping self avoids a
pod joining itself during single-node bootstrap.

### If removed

Every `OrderedReady` restart risks a term fork whenever ordinal 0
restarts into a live cluster. Symptom is two leaders in different terms
and `cluster.members` disagreeing across pods.

## 3. Forkserver-safe cluster readiness

### Problem

Python 3.14 defaults to `forkserver`. Forked `MWorker` processes do not
inherit `SMaster.secrets`, so the in-memory `cluster_ready` event set by
the publisher is invisible to the workers actually serving pillar/auth.
Upstream `_cluster_is_ready` in `salt/channel/server.py` checked only:

```python
entry = salt.master.SMaster.secrets.get("cluster_ready")
if entry is None:
    return False
return entry["event"].is_set()
```

Result: the node is a committed Raft voter, the publisher knows it, but
every worker defers with `cluster_retry` forever. Minions log `Master
did not return a session key`; salt-api returns 401s.

### Fix

Patch 5 (`salt-cluster-identity-patch.py:103-137`) keeps the fast
in-memory check and adds a filesystem fallback:

```python
entry = salt.master.SMaster.secrets.get("cluster_ready")
if entry is not None and entry["event"].is_set():
    return True
try:
    import salt.cluster.healthchecks as _hc
    ready = _hc.health_dir(opts)
    if ready is not None and (ready / _hc.READY_SENTINEL).is_file():
        return True
except Exception:
    pass
return False
```

`PubServer` writes `<cachedir>/health/ready` once this node is a
committed voter. That file is on the PVC, visible to every worker
regardless of start method. The `try/except` keeps a missing
`healthchecks` module or unreadable dir as "not ready" rather than a
crash.

### Wiring to probes

`deploy/kubernetes/salt-master.yaml:146-151` readiness requires both
signals plus serving sockets:

```sh
test -f /home/salt/data/keys/.cluster_ready \
  && test -f /home/salt/data/keys/cache/health/ready \
  && exec 3<>/dev/tcp/127.0.0.1/8000 \
  && exec 4<>/dev/tcp/$POD_IP/4507
```

`.cluster_ready` is the entrypoint watcher's "a leader is visible for my
name" mark (from `cluster-ready.py` + `salt-run cluster.members`);
`health/ready` is Salt's own "I am a committed voter" mark. Both are
needed because persisted membership can look healthy while the live
request gate never opened — in that state every publish fails with
`cluster_retry`.

## 4. Reconcile replays post-snapshot CONFIG (membership wedge)

### Problem

Observed live: a restart with an empty snapshot but a 3-voter `CONFIG`
entry already in the persisted log. Upstream `reconcile_membership` in
`salt/cluster/consensus/raft/node.py` read only the restored snapshot:

```python
if self.membership_sm is None:
    return
voters = self.membership_sm.current_voters()
learners = self.membership_sm.current_learners()
if not voters and not learners:
    return  # nothing committed yet
```

Empty snapshot meant "nothing committed", so reconcile returned as a
no-op, `on_change` (cluster-ready) never fired, and the master deferred
all traffic even though its own log contained the committed voter set.

### Fix

Patch 14 (`salt-cluster-identity-patch.py:295-324`) replays committed
`CONFIG` entries from the loaded log before evaluating, mirroring what
the `cluster.members` runner already does when it replays the persisted
log through a fresh `MembershipStateMachine`:

```python
for entry in getattr(getattr(self, "log", None), "entries", None) or []:
    if getattr(entry, "type", None) == LogEntryType.CONFIG:
        self.membership_sm.apply(entry.cmd, index=entry.index)
```

Only `CONFIG` entries move membership; `COMMAND` and `RING_REGISTRY`
are skipped. The `getattr` chain keeps a node without a loaded log on
the old no-op path.

### If removed

Any restart whose snapshot predates the latest committed membership
wedges that master as "no voters yet" permanently. The cluster still has
quorum, but the wedged node never serves.

## 5. Transport recovery: drop dead cached Raft clients

### Problem

`SaltPeer._publish` in `salt/cluster/consensus/peer.py` caches one
`_TCPPubServerPublisher` per peer pusher for the life of the process.
When the cached stream dies (peer pod restarted, RST mid-handshake),
every subsequent send through that pusher fails identically. Observed
live as 124 consecutive `StreamClosedError`s blacking out one peer
direction, starving elections of that peer's replies (no committed
voters → `cluster_retry` → salt-api 401s on every node).

### Fix

Patch 15 (`salt-cluster-identity-patch.py:325-368`) wraps the single
send:

```python
try:
    await client.send(raw)
except Exception:
    try:
        client.close()
    except Exception:
        pass
    try:
        del pusher._consensus_async_client
    except AttributeError:
        pass
    raise
```

The next send reconnects fresh (re-resolving the peer DNS, which may now
point at a new pod IP). Deliberately **no retry in the same call**:
retrying a possibly-delivered fire-and-forget publish could duplicate
Raft RPCs, and duplicate pre-vote replies poison candidacies
(`CandidacyError`) and stall elections. The caller still logs the
dropped RPC; the following heartbeat re-establishes the stream.

### If removed

One peer restart can permanently poison the sender's cached connection
to that peer until the sender itself restarts. Raft then runs with a
one-way partition.

## 6. Raft storage stays on per-master `localfs` (Redis incident)

### Problem

`SaltStorage` in `salt/cluster/consensus/storage.py` constructed its
cache as `salt.cache.Cache(opts)` with no driver argument, so it
followed whatever the global `cache:` opt said. The docstring even
advertised that (`localfs today, mmapcache tomorrow`).

Setting `cache: redis` for shared minion data (grains, Pillar, Mine)
therefore also redirected the Raft log, vote state, and snapshot to
Redis. Two failure layers resulted:

**Layer 1 — hard crash on masters without the client.** `Cache.list`
resolves `redis.list` through the loader. Masters running the pre-Redis
image have no `redis-py`, so `redis_cache.__virtual__()` disables the
module and the lookup raises:

```text
salt-run cluster.members
  runners/cluster.py: members()
  raft/log.py: Log.__init__ -> storage.load_log()
  storage.py: keys = self._cache.list(self._log_bank)
  cache/__init__.py: return self.modules[fun](bank, ...)
KeyError: 'redis.list'
```

During an `OrderedReady` rollout this is a mixed cluster: two old voters
cannot even read their own local log through the new global driver,
while the one new pod reads an empty Redis. Readiness gates never agree
and the rollout stalls with 0–2 of 3 Ready and empty API/MQ endpoints.

**Layer 2 — wrong even when the client exists.** Bank layout is
`cluster/consensus/<node_id>/<ring_id>/` plus `/log`, so three nodes do
not overwrite each other key-for-key on shared Redis, but sharing is
still incorrect:

* The new pod starts with an empty shared log while survivors hold
  committed history on disk; replays diverge.
* `_fsync_bank_key` only fsyncs `localfs` files (data file + parent
  directory for the rename). Other drivers fall through silently. Crash
  safety for votes and acked entries is lost.
* One Redis instance becomes a quorum dependency: Redis down means no
  voter can load its log even with 2 of 3 masters alive.

### Fix

Patches 1–2 (`salt-cluster-identity-patch.py:38-64`):

* Docstring rewritten to state the invariant: global `cache:` is for
  minion data and may point at shared Redis; it must not redirect
  consensus.
* Constructor pinned:

```python
# Raft state belongs to this master's PVC. Keep it on localfs even
# when Salt's global cache driver is changed for shared minion data.
# The durability helper below fsyncs the localfs log and metadata.
self._cache = salt.cache.Cache(opts, driver="localfs")
```

After the fix, `cache: redis` + `minion_data_cache: true` coexists with
`raft_driver == "localfs"` on every pod. Verified in-image with
`cache=redis` opts doing a `save_state`/`load_state` round-trip (see
Vehicle section), and live with all three pods reporting
`merged_cache=redis, raft_driver=localfs, ready=True` plus a
cross-master `store` on master-0 → `list`/`fetch`/`flush` on master-1
through the Redis driver.

### Rollout lesson recorded in `docs/guides/install-kubernetes.md`

For an existing trio, activate in two stages: (1) roll the patched image
to 3/3 Ready while the live ConfigMap is still `localfs`; (2) apply the
Redis ConfigMap, wait for projection on all pods, then
`rollout restart`. Never apply the whole Kustomization for this
upgrade. The Redis cache starts empty; per-master `localfs` minion data
is not bulk-migrated — minions repopulate on refresh/update.

## Deliberately not patched

* `salt/returners/pgjsonb.py` bare `INSERT INTO salt_returns`: in the
  3-master shared-cache topology every return is written by all three
  members and the two losers raise `UniqueViolation` on `(jid, id)`.
  Upstream fixed the same shape for `save_load()` with `ON CONFLICT`
  (issue #22171) but never for returns. Fixed as a delegating wrapper
  `salt-extmods/pgjsonb_upsert.py` (one SQL clause, everything else
  delegated) because an `INSERT`-line build anchor would churn across
  upgrades. Full analysis in `docs/reference/returner.md` §2–3; vehicle
  is the image `COPY` in `Containerfile.salt-master`, not this patcher.
* `salt/cluster/consensus/raft/node.py` `Candidacy.handle_reply`
  raising on same-term duplicates and pre-vote overlap were investigated
  as the double-delivery suspect and left alone: live duplicates match
  overlapping pre-vote rounds in an all-deny deadlock, not systematic
  double-sends, and one granted reply elects regardless. Tolerating
  duplicates would silence logs without unblocking elections. Revisit
  only on demonstrated systematic double-send.
* Salt `pam` auth, `rest_cherrypy` login, and token paths were
  investigated during the 401 incident and exonerated (credentials, PAM,
  `external_auth` verified; 401s were the `cluster_retry` gate in §3).
* `salt/cache/redis_cache.py` itself is unpatched. `cache.redis.timeout`
  from older examples is not a supported opt in 3008.2 and is omitted
  from `cache.conf`; prefixes use `overstate_prod_*` with the default
  `_` separator (slashes are rejected by the driver).

## Maintenance

Adding a patch:

1. Read the full upstream function, not just the hunk.
2. Choose the shortest anchor that still occurs exactly once (test in
   `tests/test_salt_cluster_patch.py:139-165` enforces `len(old) > 20`).
3. Prefer `opts.get("cluster_node_id") or opts[...]` fallback so unset
   means upstream behavior.
4. Add the synthetic-tree test case alongside the patch; watch it fail
   before implementing (repo TDD rule).
5. Rebuild the image and run the Vehicle verification plus the full
   suite (`1079 passed` at last Redis rollout).

Upgrading the base image:

1. Bump the digest in `Containerfile.salt-master:7`.
2. Rebuild; any `PatchError` names the drifted file and count.
3. Re-read the new upstream function, update the anchor, keep the
   replacement semantics. Do not weaken `exactly once`.
4. Re-run image verification and a staged rollout (image first on
   `localfs`, cache flip second).

Log signatures to grep when triaging:

* `Event from peer failed authentication: cluster` — peer AES handshake
  during mixed-config rollout, not a password leak; check image +
  `cache.conf` consistency before rotating secrets.
* `KeyError: 'redis.list'` from `cluster.members` — Raft storage is
  following the global cache driver (storage pin missing or old image).
* `StreamClosedError` bursts from `SaltPeer` — transport patch missing
  or a new failure mode around the cached publisher.
* `cluster_retry` + `Master did not return a session key` — readiness
  gate (§3) or reconcile wedge (§4), not minion keys.
