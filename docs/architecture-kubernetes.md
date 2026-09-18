# Kubernetes architecture: Overstate + Salt master trio

How-to lives in `install-kubernetes.md`; the non-Kubernetes path lives in
`deployment.md`. This document explains **what** runs on the cluster,
**why** it is shaped this way, what it assumes, where it can break, and
whether Kubernetes is the right home for it at all.

Everything Overstate manages lives in the `overstate` namespace.
Cluster-wide pieces it depends on but does not own: Traefik (HTTP ingress
plus the 4505/4506 TCP entrypoints), the Longhorn StorageClass, DNS, and
cert-manager ClusterIssuers.

## 1. Goals and non-goals

Goals:

- Run the Overstate UI, its worker, and the Salt masters as one
  self-describing unit (`kubectl apply -k deploy/kubernetes`).
- Survive the loss of any single master pod with no minion re-enrollment
  and no job-history gap.
- Let an admin edit master configuration, reactor files, and keys from
  the browser, with versioning and one-at-a-time rolling restarts.
- Keep all secrets owner-created and uncommitted; the repo holds no
  credentials.

Non-goals:

- Multi-cluster or multi-site Salt. One Overstate manages the masters in
  its own namespace.
- Zero-downtime everything: brief control-plane pauses (single worker,
  single Redis, single Postgres instance) are accepted; only the
  master trio and the app itself are redundant.
- Replacing Salt semantics: job execution, grains, and states behave
  exactly as upstream Salt defines them.

## 2. Topology

```text
                    ┌────────────────────────────────────────────┐
  browser ──443──► │ Traefik: overstate.sigaint.au (IngressRoute)│
                    └──────────────┬─────────────────────────────┘
                                   │ ClusterIP
                    ┌──────────────▼─────────────────────────────┐
                    │ app Deployment ×2 (Django + gunicorn)      │
                    │  UI, salt-api fan-out, ConfigMap edits     │
                    └──────┬───────────────┬─────────────┬───────┘
                           │               │             │
              per-pod salt-api ×2    Redis ×1      CNPG Postgres ×1
              (headless DNS)     (sessions,   (app DB `overstate`
              publish + keys      cache,       + job cache `salt`)
              fan-out             queues)
                           │
              ┌────────────▼──────────────────────────────────────┐
              │ salt-master StatefulSet ×3 overstate-salt-master │
              │  pod-0..pod-2:  same keypair, shared ConfigMap, │
              │  per-pod accepted-keys PVC, PG job-cache returner │
              └────────────┬──────────────────────────────────────┘
                           │ Traefik TCP 4505/4506 → salt-master-mq
                    ┌──────▼──────┐
                    │   minions   │  (one long-lived ZMQ connection
                    └─────────────┘   each, to exactly one pod)
```

Component inventory (all in `deploy/kubernetes/`):

| Object | Kind | Redundancy | State |
|---|---|---|---|
| `app` | Deployment ×2 | 2 replicas | Stateless (sessions/cache in Redis) |
| `worker` | Deployment ×1 | **1 replica** | Stateless, restarts fast |
| `salt-master` | StatefulSet ×3 | 3 replicas, ordered rollout | Accepted keys on per-pod `keys` PVC (5Gi RWO Longhorn) |
| `salt-master` (headless) | Service, ClusterIP None | — | Stable per-pod DNS for fan-out |
| `salt-master-api` | Service ClusterIP :8000 | Load-balanced | Default salt-api target |
| `salt-master-mq` | Service ClusterIP :4505/:4506 | Load-balanced | Minion ZMQ via Traefik TCP routers (`salt-mq-routes.yaml`) |
| `redis` + `redis-data` | Deployment ×1 + PVC 5Gi RWO | **1 replica** | Cache/sessions/queues |
| `overstate-db` | CNPG Cluster | **instances: 1** | App DB + shared `salt` job-cache DB |
| `srv-data` | PVC 10Gi RWX Longhorn | Shared volume | App writes (git checkout), masters mount read-only file roots |
| `salt-master-config` | ConfigMap (owned) | — | Whole master config, UI-editable, versioned |
| `salt-master-config-history` | ConfigMap | — | Last 20 config snapshots |
| `key-reconcile` | CronJob, hourly | One Job at a time (`Forbid`) | Completes same-fingerprint key trust across pods; read-only on the cluster |
| `salt-master-db` | Secret (owner-held) | — | `returner.conf` drop-in (PG password) |
| `salt-master-keys` | Secret (owner-held) | — | Shared master keypair (fleet identity) |
| `overstate-secrets` | Secret (owner-held) | — | Django, admin, Redis, salt-api passwords |
| IngressRoute + Certificate | Traefik / cert-manager | — | Public HTTPS for the UI |

## 3. The three decisions that shape everything

**3.1 Publish buses are per-master, so the UI fans out every publish.**
A ZeroMQ publish on pod-0 only reaches minions whose long-lived
connection currently terminates on pod-0. There is no shared bus. So
`overstate_ui/fleet.py` publishes every job to **both** pods with the
**same JID**. Each single-homed minion therefore receives the job exactly
once (from whichever pod it is attached to), executes once, and returns
once — to that same pod. This is the load-bearing invariant of the whole
cluster; section 7 lists what breaks if it is violated.

**3.2 Master identity is shared, accepted keys are not.**
All three pods mount the same `master.pem`/`master.pub` from the owner-held
`salt-master-keys` Secret, so from a minion's perspective there is one
master identity: either pod authenticates, and a pod reschedule never
forces re-enrollment. But Salt stores accepted minion keys as files in
the local PKI dir, and two masters must never share one PKI dir
concurrently (no locking — shared writes corrupt it). Hence accepted
keys live on **per-pod PVCs** that survive reschedules, and every key
mutation (accept, reject, delete) is fanned out to all three pods by
`overstate_ui/keys.py` (`accept-on-both`). The Keys roster shown in the
UI is the **union** of all three pods with per-pod state chips.

**3.3 Job history lives in Postgres, not on either master.**
Salt's default local job cache dies with its pod. Both masters run the
`postgres_local_cache` returner against one shared `salt` database on
the CNPG cluster, so returns from either pod land in one store and
either master (and the UI) reads the merged history. The returner
credentials arrive as the `salt-master-db` Secret projected into the
config dir as `returner.conf` — the owned ConfigMap never holds
passwords (enforced by test).

## 4. Data flows

- **Run a job:** UI → per-pod salt-api clients (headless DNS) publish the
  same JID on all three pods → each minion gets it once via its single ZMQ
  connection → executes → returns to its attached pod → returner writes
  to shared PG → UI reads merged history from PG.
- **Key action:** UI → idempotent wheel call on every reachable pod →
  union roster refresh. An unreachable pod is shown degraded, not fatal.
- **Master config edit:** admin-only UI → whole-ConfigMap PUT-replace
  (with resourceVersion) → snapshot appended to history (cap 20) →
  StatefulSet rolls one pod at a time → health poll per pod.
  Bad-edit recovery is UI-first (**Revert to last snapshot**); if the UI
  itself is locked out, the kubectl fallback is in
  `install-kubernetes.md` §3.
- **File roots:** the app holds the single writer (git checkout onto
  `srv-data`, RWX); both masters mount it read-only and serve states
  from it. No master ever writes states.
- **Reactor:** reactor SLS bodies are admin-gated in the UI and seeded
  via `salt-seed.yaml`. Each master runs its own reactor on its own
  event bus: minion-originated events are visible only to the pod that
  minion is attached to.
- **Default salt-api reads** (anything not explicitly fanned out) go to
  `https://salt-master-api:8000`, the load-balanced VIP. Each
  `SaltClient` owns its token and re-logs-in on 401, so a token minted
  on pod-0 failing on pod-1 self-heals at the cost of one extra login.

## 5. Identity and secrets model

| Secret | Held by | Contains | Rotation cost |
|---|---|---|---|
| `overstate-secrets` | Owner | Django, admin, Redis, salt-api passwords | Regenerate + restart all workloads |
| `salt-master-keys` | Owner | Shared master keypair | **Fleet-wide identity change** (roll pods one at a time, every minion must trust the new key before the last old pod leaves) |
| `salt-master-cluster-keys` | Owner | Pinned `cluster.pem` / `cluster.pub` | **Fleet-wide cluster identity** (entrypoint copies onto each PVC before the daemon can mint; minions cache this as `minion_master.pub`) |
| `salt-master-db` | Owner | Returner PG password | Replace Secret, roll pods one at a time |
| `overstate-db-app`, `-superuser` | CNPG | App/superuser PG passwords | CNPG-managed |

Rules: secrets are created by hand (`install-kubernetes.md` §1, §4,
§5), never committed, never mounted where the UI could echo them. The
app's Kubernetes RBAC is a namespace-scoped Role (no ClusterRole):
it may PUT-replace exactly the two config ConfigMaps and restart
exactly the master StatefulSet.

## 6. What survives what

| Failure | Effect | Why it is OK (or not) |
|---|---|---|
| One master pod killed | Minions reconnect via the MQ VIP to a survivor; publishes still fan out (dead pod errors, survivors deliver); history intact in PG | Core HA case — pending live drill proof |
| Master pod rescheduled | Accepted keys persist on its PVC; same identity from Secret | No re-enrollment |
| Bad master config edit | Previous snapshot re-applied from history, pods re-rolled | UI-first recovery |
| Postgres down | Masters keep serving; returns fail to persist; UI history degrades (explicit degraded mode) | **History gap, not outage** — but silent if unnoticed |
| Redis down | Sessions/cache/queues drop; app error-pages until it returns | Short full-UI outage |
| Worker down | Background jobs pause; Deployment restarts it | Minutes-scale pause |
| Whole node lost | Depends which pods were on it (see §8.4) | The trio is only three if pods sit on different nodes |
| Lost owner Secret | Regenerate everything that consumed it | No backup exists — operator procedure, not automation |

## 7. Assumptions (explicit)

1. **Minions are single-homed.** Each minion holds exactly one master
   connection (via the MQ VIP). A minion ever connected to both pods
   (e.g. a multi-master `master:` list) would receive fanned-out jobs
   twice and execute twice.
2. **Fan-out availability beats consistency.** A publish or key action
   that reaches only one pod (other down) is accepted as done on one
   pod; no distributed transaction, no retry queue. Divergence is
   reconciled by re-running the action, not by automation.
3. **Key state converges within the hour, not instantly.**
   Accept-on-both replicates what the UI does; the hourly reconcile
   (plus the one-click button) completes same-fingerprint trust after
   scale-ups and outages. Anything rejected/denied, globally pending,
   or fingerprint-mismatched still needs a human, surfaced via the
   per-pod chips and the Keys-page banner.
4. **Keys accepted before a pod existed are unknown to it.** Keys
   accepted before a later pod joined (e.g. pod-2) are unknown there
   until re-accepted or reconciled; minions that land on the new pod
   show as pending there.
5. **Clocks and DNS are trustworthy.** Same-JID fan-out assumes both
   pods agree on time; per-pod fan-out assumes headless DNS resolves.
6. **The out-of-repo halves exist.** Traefik 4505/4506 entrypoints, DNS,
   and the storage class are managed elsewhere; the in-repo MQ routes
   are inert until the entrypoints exist.
7. **Small fleet.** Master sizing (1Gi request / 2Gi limit), a single
   worker, and single Redis/Postgres instances assume tens of minions,
   not thousands.

## 8. Potential issues and risks

**High:**

- **8.1 Placement protection is partial.** The master trio carries
  required hostname anti-affinity plus a `minAvailable: 2` PDB (Raft
  quorum needs two voters), and
  the app preferred anti-affinity plus its own PDB — so a node loss or
  drain no longer takes the whole trio. What remains: no
  `topologySpreadConstraints`, no PDBs on the singletons (worker,
  Redis, single-instance Postgres), and Longhorn RWO volumes still pin
  rescheduled pods to the old node's data until replicated.
- **8.2 Accepted-keys divergence converges within the hour.**
  Pre-pair keys (assumption 4), actions during a pod outage
  (assumption 2), and manual `salt-key` drift are completed by the
  hourly `key-reconcile` CronJob plus the one-click **Review &
  reconcile** on the Keys page. Both apply one rule only: accept on a
  pod what another pod already trusts with the identical fingerprint.
  Residual gap: quarantine by single-pod delete (instead of reject)
  gets re-completed on next minion contact — documented in `user.md`.
- **8.3 Postgres is instances: 1.** The shared job cache — the one
  component both masters depend on for consistent history — is a
  single instance. CNPG will recover it, but during the outage returns
  are lost, not queued. The HA story for masters is stronger than the
  HA story for the thing that makes them look like one master.

**Medium:**

- **8.4 Minion MQ is per-node, not RR.** `salt-master-mq` uses
  `internalTrafficPolicy: Local` and Traefik TCP routes set
  `nativeLB: true`, so a connection to a node's :4505/:4506 only
  reaches the master on that node. Minions should use the three
  node hostnames (`salt-c010` / `salt-42e5` / `salt-b2b6`) with
  `master_type: failover`. `sessionAffinity: ClientIP` remains a
  backstop on mq and salt-api.
- **8.5 salt-api TLS is unverified in-cluster** (`SALT_API_VERIFY_CA:
  false`; the image mints a self-signed cert at boot). Fine inside a
  trusted CNI, but any pod-compromise or CNI-sniffing position yields
  the salt-api password. Fix: pin the CA (TODO already in manifests).
- **8.6 Reactor misses during failover.** Minion-originated events go
  to the attached pod only; minions reconnecting mid-outage lose the
  events in the gap, and each pod's reactor fires only on its own bus.
  Reactor-driven flows are at-most-once per pod — acceptable for
  alerts, dangerous if any reactor action is load-bearing.
- **8.7 Traefik entrypoints live outside the repo.** Minion
  connectivity (4505/4506) depends on separately-managed Traefik
  config with no version coupling to this repo. Drift there silently
  disconnects the fleet.
- **8.8 Single worker, single Redis.** Background work and UI sessions
  have no redundancy; both restart fast, but a prolonged Redis volume
  issue is a full UI outage with no graceful degradation.
- **8.9 Minion request channels assume any pod serves any session.**
  The MQ Service spreads every new TCP connection across all three
  pods with no stickiness. If a minion's reconnect lands its 4506
  channel on a pod that never issued its AES session, pillar fetches
  and job returns fail session decrypt (`message authentication
  failed`) while subscribes (4505) keep working — the minion flaps
  up and down. Rule out version skew first (a 3006 minion against
  3008 masters fails similarly); if matched-version minions still
  flap, the fix is stickiness on `salt-master-mq`, not more replicas.
- **8.10 Peer identity is the stable pod DNS name (fixed).** Peer
  identity used to be the pod IP, so every reschedule orphaned the
  old ID in Raft membership (dead voters, lost quorum,
  `cluster_retry` on all traffic → salt-api 401s, minion pillar/auth
  flap) and in each pod's `_cluster/peers/` store (stalled join,
  `KeyError: 'aes'`). Identity is now `<POD_NAME>.<headless-svc>`
  (`id`, `cluster_node_id`, `cluster_peers`, peer key files); a
  build-time patch (`salt-cluster-identity-patch.py`, fails loudly on
  upstream drift) redirects salt's interface-keyed identity (Raft
  node-id, join sentinel, founder sort, ring ownership) to
  `cluster_node_id`, while `interface` keeps the pod IP for binding.
  Identity is a PVC drop-in (`cluster-identity.conf`) included from
  the shared ConfigMap and written before the daemon starts, so the
  first preflight always sees peers. `cluster_peers` is the other
  members (self only for a solo replica). Sibling churn restamps that
  file and does not restart the daemon. Scale one step at a time with
  the §7 runbook in `install-kubernetes.md`; the entrypoint discovers
  members via the API (dedicated read-only ServiceAccount), prunes
  dead peer keys and bounces stalled joins only on a complete replica
  view — constructed-name fallback keeps stamp-only behavior.
  Residual risk: scaling to 1 keeps
  a 3-voter Raft set with no quorum (documented limitation — shrink
  membership via a leader-driven removal before single-replica ops).

**Low (accepted, documented):**

- Image tags not pinned to digests (TODO in manifests).
- History ConfigMap capped at 20 revisions / 1MiB object limit —
  fine at current config sizes.
- `srv-data` is RWX Longhorn shared between app and masters: correct
  for single-writer file roots, but any second writer (a debug pod, a
  second app generation) would corrupt states with no locking.
- Owner-held secrets have no backup: losing `salt-master-keys` means
  fleet re-enrollment, losing `overstate-secrets` means regenerating
  everything. This is a deliberate trade (nothing committed) that
  needs an offline copy in a vault, not in the repo.

## 9. Is Kubernetes the right thing?

**Verdict: yes for this project, with eyes open — but it is a choice,
not a necessity.**

What Kubernetes earns here:

- **One artifact, one apply.** The whole system (UI, worker, masters,
  cache, routes, history plumbing) is a reviewable diff instead of a
  wiki page of manual master setup. That is the project's biggest
  operational win.
- **Cheap extra masters.** The failover trio is ~30 lines of
  StatefulSet plus fan-out code. On VMs the same trio means more
  machines, key sync, config sync, and a load balancer — all hand-run.
- **Recovery primitives that actually get used:** history ConfigMap +
  one-at-a-time rollouts already saved real debugging sessions in this
  project (bad-edit revert, reschedule-safe keys).
- **The owner already runs the platform.** Traefik, Longhorn, CNPG,
  DNS, and cert-manager exist and are managed. Overstate rides them
  instead of re-implementing backups, TLS, and storage.

What it costs:

- **Three new distributed-systems problems** (per-master buses,
  per-pod PKI, shared cache) that do not exist with one master on one
  VM — each solved above, but each a permanent sharp edge (see §8.1,
  §8.2, §8.3).
- **More to hold in your head at 2am:** Traefik entrypoints, projected
  volumes, StatefulSet ordinals, CNPG failover. The VM equivalent is
  two systemd units and a Postgres.
- **Dependency depth:** a Salt outage can now be caused by Longhorn,
  CNPG, Traefik, CoreDNS, or cert-manager — none of which Salt needs
  on bare metal.

Alternatives considered:

- **Single master on a VM + Overstate beside it** (`deployment.md`
  path): simplest possible, fewest moving parts. Right if the fleet
  tolerates a maintenance window for master restarts and the owner
  prefers pets over cattle. Loses browser config editing, versioned
  restarts, and cheap failover.
- **Two VMs with Salt's native multi-master:** minions list both
  masters, no fan-out code needed — but key/config sync between the
  VMs is manual, and there is still no shared job cache without the
  same Postgres returner. Trades code complexity for procedural
  complexity; worse auditability.
- **Managed control plane / Salt-as-a-service:** does not exist in a
  form that keeps minions on-premise with this UI. Not a real option.

When to retreat: if minion count stays tiny (single digits) and the
owner stops enjoying cluster maintenance, collapse to one master
replica and eventually to the VM path. The design degrades gracefully
in that direction — fan-out to one pod is just a publish, and the PG
returner works for a single master too. Nothing about the trio poisons
the simple topology.

## 10. Open proofs

- Pod-kill failover drill (minion reconnect, publish during outage,
  merged history) — owner-scheduled.
- Second-minion join against the trio (validates accept-on-both on a
  fresh key, and the reconcile path for pre-pair keys).
