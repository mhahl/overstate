# Salt-master cluster / salt-api remediation

Implement this so the three-pod Salt master cluster forms a Raft quorum
and salt-api login succeeds. Do not redesign fan-out, keys, or the UI.

Live incident (overstate namespace, 2026-09-17): all three pods Ready,
`salt-master-api` had endpoints, salt-api answered GET `/` with 200, and
every POST `/login` (VIP and each pod FQDN) returned 401 with a matching
eauth password. `salt-run cluster.members` on pod-0:

```
voters: []  learners: []  leader_id: null  term: 5751  is_clustered: false
```

Ready was a PVC `.joined` file. There was no leader. salt-api 401 is
`cluster_retry`, not a bad secret.

## Goal

A rolling start of `StatefulSet/salt-master` (replicas 3, OrderedReady)
ends with:

- `salt-run cluster.members` on every pod shows the same non-null
  `leader_id` and `voter_count >= 2`
- POST `/login` with the `overstate` PAM user returns 200 and a token
  on the VIP and on each pod
- the app/worker stop logging `salt-api login failed: HTTP 401`
- a later rolling restart does not dump term into the thousands or
  empty the voter set

## Already in the tree (do not redo)

These are present in the working tree and must stay. Confirm tests still
cover them; do not revert.

| Item | Where |
|---|---|
| DNS identity (`id` / `cluster_node_id` / peers = `<pod>.<svc>`, `interface` = pod IP) | `cluster-entrypoint.sh`, `salt-cluster-identity-patch.py` |
| `.joined` deleted at wrapper start | `cluster-entrypoint.sh` |
| Readiness: `.joined` **and** TCP 8000 | `deploy/kubernetes/salt-master.yaml` |
| Liveness: `supervisorctl status salt-master` RUNNING | same |
| `sessionAffinity: ClientIP` on `salt-master-api` | same |
| Maiden stamp after supervisord + one bounce if the daemon already started | `apply_maiden_stamp` |
| Restamps do **not** `supervisorctl restart` (only maiden catch-up + rate-limited recovery bounce) | watcher loop |
| Image tag `lts-pg9`, `imagePullPolicy: Always` | StatefulSet + `test_deploy.py` |

The image running in the cluster when this was diagnosed still bounced
on every peer-set change and did not delete `.joined`. Shipping the
current tree is necessary but **not sufficient**: live Raft had 0
voters *after* peer pubs existed on disk.

## Remaining defects (implement these)

### R1 — `cluster_peers` must not include self

**Bug.** `build_want` writes every discovered name, including this
pod. Salt documents `cluster_peers` as the *other* masters. Live
pod-1: `CandidacyError: Already received a reply from this peer:
salt-master-1.salt-master`. That poisons the election (term 5751).

**Change.** In `cluster-entrypoint.sh` `build_want` (and any fallback
that feeds it):

- `id` / `cluster_node_id` stay this pod's DNS name
- `cluster_peers` is the input set **minus** `$node_name`
- if that leaves the list empty (solo diagnostic replica), keep a
  single self entry so Salt's "cluster_id set but cluster_peers empty"
  preflight does not fire. Three-replica production never takes this
  branch: fallback still constructs the other ordinals.

Fallback (`peers_fallback`) may still emit all ordinals; `build_want`
strips self. Do not stamp unborn peers as *self*.

Tests in `tests/test_cluster_entrypoint.py`:

- `build_want` with `{self, A, B}` emits `cluster_peers: [A, B]` only
- `build_want` with `{self}` only emits `cluster_peers: [self]`
- recovery/prune still key files as `<dns-name>.pub`

### R2 — Identity must exist before the first preflight

**Bug.** Phase-1 waits for supervisord then stamps `/etc/salt/master`.
The base image has already started `salt-master`. Live pod-0/1:

```
[CRITICAL] cluster_id is set but cluster_peers is empty.
[CRITICAL] Master failed pre flight checks, exiting
```

The maiden bounce recovers *sometimes*; OrderedReady + three pods
bouncing is what produced the empty voter set.

**Change (preferred).** Stop racing the generated main file.

1. Stamp identity to a **writable** drop-in on the keys PVC, e.g.
   `/home/salt/data/keys/cluster-identity.conf` (same `build_want`
   body, no marker required in `/etc/salt/master`).
2. Add to the owned ConfigMap `master.conf`:

   ```yaml
   default_include: /home/salt/data/config/*.conf
   ```

   already exists via the image. Add an extra include the daemon
   always reads, e.g. in `master.conf`:

   ```yaml
   include: /home/salt/data/keys/cluster-identity.conf
   ```

   Salt merges includes; this file is per-pod and not in the
   ConfigMap, so it cannot fight a static `cluster_peers` drop-in.
3. Write that file **synchronously** in the wrapper *before*
   `exec /sbin/entrypoint.sh` (after `preseed_master_keys` /
   `wait_for_peer_dns`). No supervisord wait.
4. The background watcher updates the same file (atomic rename, as
   `stamp_identity` already does). Do not restart on restamp.
5. Keep neutralizing `id` / `interface` in `/etc/salt/master` if the
   image still writes them, **or** drop the `/etc/salt/master` append
   entirely if the include is sufficient. Prefer one source of truth
   (the PVC drop-in). Verify with
   `salt.config.master_config("/etc/salt/master")` that
   `cluster_peers` is non-empty before the first running daemon
   preflight — if the image preflights before includes load, keep a
   maiden bounce as a safety net, but the include must be on disk
   first.

If include-before-preflight is proven impossible in the cdalvaro
image, the fallback is: block `exec` until a one-shot wrapper has
written `/etc/salt/master` itself. Do not wait on supervisord.

Tests: wrapper writes the identity file before `exec`; empty
`cluster_peers` cannot appear in the file the daemon reads; maiden
bounce still covered.

### R3 — Ready means a Raft leader, not “files exist”

**Bug.** After the tree's TCP-8000 change, this incident would still
have gone Ready: salt-api was listening, `.joined` existed, peer
`.pub` files existed, `voter_count` was 0. App 401s.

**Change.** `.joined` (and Ready) only after **this node sees a
leader**. Implement a small root-only helper (stdlib, Salt's python)
that runs `salt-run cluster.members` or reads the in-process cluster
status and exits 0 iff `leader_id` is a non-empty string and
`voter_count >= 1` (prefer `>= 2` once all three are up; do not
deadlock OrderedReady — pod-0 as founder with a 1-voter group must
become Ready so pod-1 can start).

Practical rule:

- Founder (lowest ordinal / first in sorted bootstrap pool): Ready
  when `leader_id == self` **or** `leader_id` set and self is a voter
  or learner.
- Joiners: Ready when `leader_id` is set and this node is in
  `voters` or `learners`.

Keep the TCP 8000 check. Delete `.joined` at wrapper start (already
done). Stop treating “every `peers/<name>.pub` exists” as joined;
peer pubs are necessary but were present in the failed incident.

Probe must stay exec, no extra packages, bounded (timeoutSeconds 5).
Do not put the eauth password in the probe. Do not curl `/login`.

If `salt-run` is too heavy/slow for a 10s probe, poll a tiny status
file the watcher writes when `cluster.members` succeeds, and have
the watcher refresh it every loop. The probe then `test -f` that
file **and** TCP 8000. The watcher must not write the file on a
stale previous boot (delete at wrapper start, same as `.joined`).

Tests in `test_deploy.py` for the probe command shape; unit tests
for the helper's JSON contract (`leader_id` null → fail).

### R4 — Isolated-FS key driver

**Bug.** `cluster_isolated_filesystem: true` with
`keys.cache_driver: localfs_key` (live `master_config()` dump).
Salt 3008 documents `mmap_key` so isolated join can push key banks
as blobs.

**Change.** In `deploy/kubernetes/salt-master-config.yaml`
`master.conf`:

```yaml
cluster_isolated_filesystem: true
keys.cache_driver: mmap_key
```

Keep `cluster_port: 4507`. Live 3008.2 listened on **4507** on the
pod IP and `opts['cluster_port'] == 4507`; `cluster_pool_port`
stayed 4520 and nothing listened there. Do **not** move the Service
to 4520. Optionally set `cluster_pool_port: 4507` only if you
confirm 3008.2 dials peers on that opt; if `cluster.members` already
works over 4507, leave `cluster_pool_port` alone.

Test: ConfigMap YAML contains `keys.cache_driver: mmap_key` and
`cluster_port: 4507`.

### R5 — Recovery must not restart during a healthy roll

**Bug.** The deployed image restarted on peer-set changes (entrypoint
log: `peers changed … restarting salt-master` four times on pod-2
during one roll). Current tree removed that path. Recovery bounce
(`peer_recovery_action` → `supervisorctl restart`) can still fire
when EndpointSlices briefly omit a restarting peer and a pub is
“missing”.

**Change.**

- Recovery bounce only if `api_ok=1`, the slice count equals
  `spec.replicas`, this node was previously `ok` (`.joined` this
  boot), and the missing name is **not** an unready/restarting self.
- Never bounce because the set *grew* or *shrank*; only because a
  name that has been in the complete set for `RECOVER_AFTER_N` loops
  still has no pub.
- Rate limit stays (`RECOVER_MIN_SECS` 600).
- Watcher restamp of identity: no restart (already true — keep a
  test that the watcher loop has no `supervisorctl restart` except
  the recovery case and maiden).

### R6 — Docs match DNS identity

`docs/install-kubernetes.md` §7 still says leftover “Dead-IP” pubs
are inert. Architecture §8.4 still talks about adding session
affinity (already in the YAML). Update:

- seed/copy examples stay `salt-master-0.salt-master.pub` (already)
- drop Dead-IP wording
- §8.4: affinity is in-repo; residual risk is Raft-not-ready 401s
- comments in `salt-master.yaml` / `salt-master-config.yaml` that
  still say identity is stamped “from the pod IP” for peers — peers
  are DNS names, `interface` is the IP

## Operator one-shot after the new image (not code)

PVC Raft state from the incident is poisoned (term 5751, empty
voters). After the new image is built and the StatefulSet rolls:

1. Confirm `cluster.members` shows a leader. If it does, stop.
2. If voters stay empty after one OrderedReady roll: drain traffic
   (Ready will hold them out once R3 is in), then on **each** pod
   delete only the Raft log/snapshot under the cluster cache (not
   `cluster_pki_dir` minion keys, not `master.pem`). Exact paths are
   whatever 3008.2 stores under `cachedir` for
   `salt.cluster.consensus` (inspect with `find /var/cache/salt -iname
   '*raft*'` on a pod before documenting the rm in
   `docs/install-kubernetes.md` as a last-resort “reset membership”
   subsection). Then bounce one pod at a time, founder first.
3. Do not delete `_cluster/peers/*.pub` unless a name is a dead IP
   leftover; DNS-named pubs can stay.

Put that subsection in install §7. Do not automate a wipe on every
boot.

## Implementation order

1. **R1** (`build_want` strips self) + tests. No image-layout change.
2. **R2** (PVC identity include, stamp before exec) + tests.
3. **R3** (leader-based Ready) + tests. Depends on R1/R2 or Ready
   will never pass.
4. **R4** (mmap_key) — ConfigMap only; rolls with the STS.
5. **R5** recovery guard + regression test that restamps do not
   restart.
6. **R6** docs.
7. Build `quay.io/sigaint/overstate-salt-master:lts-pg9` (or bump
   the tag if you do not overwrite), deploy, then the operator
   verify below.

Keep commits/PRs split on that order if stacking; a single branch
is fine if the agent is executing this plan as one change.

## Files to touch

- `cluster-entrypoint.sh` — R1, R2, R5
- `deploy/kubernetes/salt-master-config.yaml` — R2 include, R4
- `deploy/kubernetes/salt-master.yaml` — R3 probe only if the
  command changes
- `tests/test_cluster_entrypoint.py` — R1, R2, R5
- `tests/test_deploy.py` — R3, R4, probe shape
- `docs/install-kubernetes.md` — R6 + optional raft wipe
- `docs/architecture-kubernetes.md` — R6 §8.4 / §8.10
- New helper only if R3 needs one (e.g. `cluster-ready.py` next to
  `cluster-peers.py`, COPY in `Containerfile.salt-master`)

Do not change `overstate_ui/*` for this plan. Fan-out and eauth
grants are fine. Do not rotate `overstate-secrets` / PAM; the
password already matched.

## Tests the agent must add or extend

```
.venv/bin/pytest -q tests/test_cluster_entrypoint.py tests/test_cluster_peers.py tests/test_salt_cluster_patch.py tests/test_deploy.py
```

Minimum new assertions:

- `cluster_peers` omits self when others exist
- identity file/block is non-empty before the wrapper `exec`
- watcher source contains no peer-change `supervisorctl restart`
  except maiden + recovery
- ConfigMap has `keys.cache_driver: mmap_key`
- readiness exec is not `test -f .joined` alone
- `cluster_peers:` still absent from the shared ConfigMap

## Verify on cluster (read, then a roll)

After image push + StatefulSet roll (OrderedReady, one pod at a time):

```sh
kubectl -n overstate exec salt-master-0 -- salt-run cluster.members
# leader_id set, voter_count >= 2, term not climbing every second

for p in salt-master-0 salt-master-1 salt-master-2; do
  kubectl -n overstate exec $p -- sh -c 'grep -A6 "^cluster_peers:" /home/salt/data/keys/cluster-identity.conf /etc/salt/master 2>/dev/null | head'
done
# each list is the other two names, not self

# from an app pod, POST /login (existing env, do not print the password)
# expect HTTP 200 and a token on https://salt-master-api:8000/login
# and on each https://salt-master-N.salt-master.overstate.svc:8000/login
```

Fail the work if term is in the thousands with `voters: []`, or if
login is 401 on all pods.

## Constraints and non-goals

- No Parallel `podManagementPolicy`. OrderedReady stays.
- No shared PKI PVC. Isolated FS + per-pod `keys` claims stay.
- Do not put `cluster_peers` or `cluster_secret` in the UI-owned
  ConfigMap body (secret stays projected `cluster.conf`).
- Do not run salt-api as root to “fix” 401; PAM/shadow was not the
  incident (password matched, GET `/` worked).
- Do not pin images to digests in this plan (existing TODO).
- Do not change Traefik MQ routes, CNPG, or Redis.
- Dev compose (`POD_NAME` unset) must keep working: wrapper no-ops
  and execs the base entrypoint.

## Done when

- Unit/deploy tests above are green
- A local image build still applies `salt-cluster-identity-patch.py`
  (anchors exact-once)
- On the overstate namespace: leader elected, login 200, app logs
  clean of salt-api 401 for a full OrderedReady roll
