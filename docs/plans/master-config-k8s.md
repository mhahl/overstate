## Status

Implemented as a three-node Salt master cluster (2026-09-20): units 1–9
landed in trio form, not the two-pod failover pair this plan originally
described. The pair-era text below has been updated to the trio reality;
the safety invariants (admin-only, blocking validation, refusal-first,
audit-everything, no auto-restart) are unchanged. Remaining proofs are
the broken-config drill and the pod-kill failover drill, both
owner-scheduled against production.

## Goal

Let Overstate own the Salt master and salt-api configuration as a cloud-native
total solution: an admin can browse, edit, validate, and version every owned
master-config file from the browser, explicitly restart the masters from the
UI, and manage reactor configuration (the `reactor:` stanza plus reactor SLS
bodies), all against the Kubernetes deployment in the `overstate` namespace.
The masters run as a three-node Salt cluster (isolated filesystem, Raft
over 4507) sharing one keypair and one Postgres job cache: Overstate
manages all three masters' shared ConfigMap, rolls them one at a time,
and accepts minion keys on all three.
This plan supersedes `docs/plans/master-config-management.md`, whose SSH and
host-file mechanism does not apply to the k8s topology; its safety invariants
(admin-only, blocking validation, refusal-first, audit-everything, no
auto-restart) carry over unchanged.

## Success Criteria

- An admin can browse the data keys of the `salt-master-config` ConfigMap from
  a Master Config page, open any key in the vendored CodeMirror editor, and
  save: invalid YAML is blocked (not advisory), the whole ConfigMap is
  snapshotted to the history ConfigMap before the write, a raced save refuses
  with the current revision and writes nothing, and every outcome is audited.
- A Restart button patches the `salt-master` StatefulSet to roll the three
  master pods one at a time (RollingUpdate + OrderedReady + `minAvailable: 2`
  PDB, so Raft quorum survives), polls the rollout and salt-api back to
  healthy inside a bounded timeout, and audits the result; outside the
  cluster (dev) it refuses with the reason and
  shows the equivalent `kubectl` command instead.
- An admin can edit reactor SLS bodies under the shared reactor roots and set
  the `reactor:` mapping stanza in `master.conf`; the live mapping stays
  readable through the existing runner-based Reactor page.
- Operators and viewers see no Master Config page and get 403 on forged POSTs.
- Three master pods serve behind the existing Services; every job action
  publishes on all three pods under one shared JID and minion results merge
  into one view no matter which pod a minion is attached to, and killing one
  pod leaves minions servable with no key re-acceptance (quorum holds at
  2 of 3).
- `.venv/bin/pytest -q` green; existing Files/reactor/git contracts untouched.

## Context And Current Facts

- Owned config surface is the `salt-master-config` ConfigMap
  (`deploy/kubernetes/salt-master-config.yaml`): data keys `master.conf`
  (cluster block — `cluster_id`, Raft election timing, `localfs_key` —
  plus an include of the per-pod `cluster-identity.conf` stamp) and
  `api.conf` (eauth grants plus netapi clients). It arrives at
  `/home/salt/data/config` as one leg of a projected volume alongside the
  owner-held `salt-master-db` (`returner.conf`) and `salt-master-cluster`
  (`cluster.conf`) Secrets, which the custom image
  (`quay.io/sigaint/overstate-salt-master:lts-pg14`, stock `:lts` plus
  `psycopg2-binary` plus the DNS-identity patch) reads as config drop-ins;
  Salt reads config once at startup, so edits need a master restart.
  Accepted minion keys live on per-pod `keys` PVCs; the shared master
  keypair (`salt-master-keys`), the pinned cluster identity
  (`salt-master-cluster-keys`: `cluster.pem`/`cluster.pub`), the cluster
  join secret (`salt-master-cluster`), and the returner credentials
  (`salt-master-db`) are all owner-held Secrets, never committed.
  salt-api TLS is image-minted — all five stay outside the editor's reach
  by construction. Placement is one pod per hostname (required
  anti-affinity) behind a `minAvailable: 2` PDB; minion MQ is per-node
  (`salt-master-mq` `internalTrafficPolicy: Local` + Traefik hostPorts).
- The app already runs as `overstate-app` ServiceAccount (`app.yaml:19`) with
  a namespaced Role (`rbac.yaml:13-28`): ConfigMap get/list/watch/create/
  update/patch/delete, StatefulSet get/list/watch/patch (no delete),
  Deployments read-only, pods/log/events read. No ClusterRole exists and none
  is needed. Kustomization is `deploy/kubernetes/kustomization.yaml`.
- Reusable machinery (verified this run): `roles_required`
  (`overstate_ui/auth.py:53`), `log_event`
  (`overstate_ui/audit.py:12`), edit/save/stale patterns with `safe_join`,
  `read_text`, `MAX_BYTES`, base-SHA checks (`overstate_ui/files.py:44-390`),
  fixed failure words (`overstate_ui/git_sync.py:169-232`), env-only settings
  (`overstate_ui/config.py`), blueprint registration
  (`overstate_ui/__init__.py:74-89`), vendored `static/editor.bundle.js`
  (used by `templates/file_edit.html`).
- Reactor today: mapping is read/changed through salt-api `reactor.*` runner
  calls, SLS bodies are read-only, export renders the mapping as YAML for
  hand-commit (`overstate_ui/reactor.py:1-7, 309-346`). `REACTOR_ROOTS` is
  `/srv/states/reactor` in-cluster (`app.yaml:86-87`), but nothing seeds that
  directory — the seed initContainer only creates `/srv/states/salt`
  (`app.yaml:26-34`).
- Gaps found this run: `docs/install-kubernetes.md` is referenced by the
  kustomization header but does not exist; `tests/test_deploy.py` asserts only
  Caddyfile/Quadlet artifacts, nothing under `deploy/kubernetes/`; the app
  runs 2 replicas, so any in-process single-flight lock does not exclude
  cross-replica races on shared state.
- Owner decisions settled this run: history is k8s-native (ConfigMap
  revisions kept as snapshots plus the audit log; revert re-patches previous
  data) — not git-backed; scope is whole owned ConfigMap including `api.conf`
  (lockout risk accepted); restart is always an explicit second click, never
  automatic on save; roles are admins only.

## Constraints And Non-goals

- Admin-only UI and routes (`roles_required("admin")`); viewers/operators get
  no links and 403s server-side. CSRF on every POST, audit row per outcome.
- Edit-existing-keys-only in v1: no create/delete/rename of ConfigMap keys
  from the UI. New keys arrive via git/kubectl, keeping the blast radius at
  the settled scope.
- Invalid YAML blocks the save with the parse error. Non-YAML text saves
  without the YAML check but keeps every other guard.
- Never `--force`, never delete the live ConfigMap: the app's Role must not
  grant `delete` on owned ConfigMaps (tightened in Unit 2).
- Restart performs exactly one fixed mutation: stamping the pod-template
  `restartedAt` annotation (the `kubectl rollout restart` mechanism). No
  user-supplied resource, patch body, or flags.
- No new pip dependencies (stdlib HTTPS client for the k8s API), no DB
  migration, no ClusterRole/ClusterRoleBinding, nothing outside the
  `overstate` namespace.
- Restarts roll one master pod at a time (RollingUpdate + OrderedReady,
  guarded by a `minAvailable: 2` PDB): the trio never bounces together, so
  Raft quorum (2 of 3) always serves minions and salt-api.
- Owner-provisioned Secrets (shared master keypair, cluster join secret,
  pinned cluster keypair, returner DB credentials, app secrets) are
  created once by hand following the install doc; the app never reads them
  and gains no Secret RBAC.
- Non-goals: minion-config management, master private-key custody inside the
  app (owner-held Secret only), TLS management, syndic hierarchies,
  SIGHUP hot-reload, in-editor Salt execution, collaborative editing,
  auto-rollout after save.

## Key Decisions

- **Stdlib in-cluster k8s client, no new dependency.** A small
  `overstate_ui/k8s.py` module talks to the API server over HTTPS with the
  pod's ServiceAccount token
  (`/var/run/secrets/kubernetes.io/serviceaccount/token`), namespace from the
  sibling `namespace` file, and server address from `KUBERNETES_SERVICE_HOST`
  / `KUBERNETES_SERVICE_PORT_HTTPS` — the documented in-pod access pattern.
  Rejected: the `kubernetes` pip client (a new dependency plus generated-API
  surface for four calls: ConfigMap get/replace, StatefulSet patch, pod
  read).
- **Optimistic concurrency via `resourceVersion`, not reimplemented hashes.**
  The edit form records the ConfigMap's `resourceVersion`; save patches with
  it and a `409 Conflict` becomes the stale-base refusal showing the current
  revision. This also closes the 2-replica race that an in-process lock
  cannot. Rejected: porting the git-HEAD hash scheme (a second truth
  competing with the API server's).
- **History is a sibling ConfigMap plus the audit log.** Before each save,
  the whole ConfigMap data map snapshots into `salt-master-config-history`
  (bounded to the last 20 revisions, D1); revert re-patches a snapshot and
  restarts. Audit rows (`masterconfig-save:<key>:<resourceVersion>`,
  refusals, `master-restart:<result>`) stay the human-readable trail.
  Rejected: git-backed per-save commits (the in-cluster app holds no
  checkout; a shared checkout across 2 replicas reintroduces locking), and
  audit-only history (unrevertable by construction).
- **Validation is YAML-parse plus guardrails, honestly bounded.** The app
  image ships no Salt, so a true `salt-master --config-test` dry parse is
  impossible in place; the gate is blocking YAML parse, top-level-mapping
  shape check, per-key size gate (well under the 1 MiB ConfigMap limit), and
  a lockout banner on `api.conf`. Crash safety comes from the bounded
  health poll plus one-click revert, not from pretending the UI can prove a
  config boots. Rejected: advisory-only YAML (a broken shared config rolls
  out to all three masters) and no validation path at all.
- **Tighten the Role as part of the work.** Scope the app Role to the owned
  names (`resourceNames: [salt-master-config, salt-master-config-history]`
  and `[salt-master]`), keep create (first history write) but drop `delete`
  on ConfigMaps — deleting the live config must be impossible for the app.
  Rejected: leaving the broad verbs (violates the never-delete constraint).
- **Reactor stanza through the editor, mapping stays on the runner.** The
  `reactor:` stanza is just YAML in `master.conf` (Unit 3 covers it); reactor
  SLS bodies become admin-editable files under `/srv/states/reactor` reusing
  the Files patterns; the event-to-SLS mapping keeps using
  `reactor.add/delete/list` (live master truth, already audited). Rejected:
  rewriting the mapping into files (two writers on one truth).
- **One StatefulSet, three replicas, one shared ConfigMap.** All three
  masters run identical config from `salt-master-config` (projected with
  the `salt-master-db` and `salt-master-cluster` Secrets, since subPath
  file mounts into the read-only ConfigMap mount fail with ENOTDIR on
  these nodes); per-pod PVCs keep separate accepted-keys dirs (D9); the
  shared keypair Secret mounts read-only into every pod. Peer identity is
  the stable pod DNS name (`<POD_NAME>.salt-master`, stamped by
  `cluster-entrypoint.sh` into the `cluster-identity.conf` drop-in the
  shared ConfigMap includes), never the pod IP. Service selectors match
  all three pods, so MQ and salt-api fan out with no manifest surgery.
  Rejected: two StatefulSets (double the manifests and restart paths for
  no behavioral difference).
- **Shared job cache via the stock `pgjsonb` returner (D11, gate closed).**
  All three masters run `master_job_cache: pgjsonb` with flat
  `returner.pgjsonb.*` keys straight into the app's own `overstate`
  database (tables `jids` / `salt_returns`, created by the app — no
  separate `salt` database), so salt-api on any master answers
  consistently and app Jobs code is unchanged. The whole returner block,
  password included, lives in the owner-held `salt-master-db` Secret
  projected as `returner.conf`; the owned ConfigMap never holds
  passwords (enforced by test). The custom master image ships
  `psycopg2-binary` for it. Rejected: `postgres_local_cache` (its
  `master_job_cache.postgres.*` path would silently fall back to local
  cache on a wrong prefix), Redis cache, and app-side dual lookup
  (every Jobs read fans out; merge bugs for free).
- **Accept-on-all-three through the existing Keys page.** Acceptance calls
  the salt-api wheel/key function against each master pod (via the per-pod
  DNS through the headless service); the UI shows the union roster with
  per-pod state chips, and an unreachable pod degrades instead of failing.
  A minion accepted on all three is servable whichever pod it lands on.
  The hourly `key-reconcile` CronJob plus the Keys-page **Review &
  reconcile** button complete same-fingerprint trust after scale-ups and
  outages (accept-only; rejects, denies, and mismatches stay human).

## Recommended Approach

One track, nine ordered units, each independently revertable, no migration.
Client and RBAC first (provable without UI), then the admin-only
browser/editor with history, then the restart button, then reactor bodies,
then docs polish, then the multi-master extension (units 7–9, which update
the restart and docs units they build on). The restart unit lands after the
editor on purpose: files must be safe before a button can roll the masters
onto them. Units 7–9 landed as a three-node Raft cluster, not the
originally planned two-pod pair — the unit text below records what was
built.

## Work Plan

1. **In-cluster k8s client (`overstate_ui/k8s.py`, stdlib only).**
   Discovery (token/namespace/server, `K8S_NAMESPACE` env override for
   staging/tests), `get_configmap`, `replace_configmap` (PUT-replace
   carrying the base `resourceVersion`; the API server 409s a stale base),
   `restart_statefulset` (stamp
   `kubectl.kubernetes.io/restartedAt` on pod-template metadata),
   `rollout_status`/`pod_ready` reads. Outside a cluster every mutation
   refuses with a fixed reason naming the equivalent `kubectl` command.
   Tests: `tests/test_master_config_k8s.py` with a fake HTTPS transport
   (409 path, restart patch-body assertion, refusal outside cluster, token
   file never logged).
2. **History ConfigMap + tightened RBAC.** New
   `deploy/kubernetes/salt-master-config-history.yaml` (empty data map,
   annotated as app-managed); `rbac.yaml` scoped to owned `resourceNames`,
   ConfigMap verbs minus `delete`; wire into `kustomization.yaml`.
   Tests: extend `tests/test_deploy.py` with k8s assertions (resources
   present, no ClusterRole, no `delete` verb on ConfigMaps for the app
   Role, history ConfigMap referenced).
3. **Master Config browser + editor (admin-only).** New `masterconfig`
   blueprint (`/master-config`, registered in `__init__.py`): key listing,
   view, edit (reuse `editor.bundle.js`, textarea fallback), save (UTF-8 +
   size gate, unknown-key 404, identical-content no-op, YAML-parse gate for
   `*.conf`, snapshot-then-patch with base `resourceVersion`, 409 becomes
   stale refusal, audit every outcome). Lockout banner on `api.conf`.
   Tests: matrix mirroring `tests/test_files.py` (RBAC 403s, unknown key,
   stale race, invalid YAML blocked with live data untouched, snapshot
   written before patch, audit rows).
4. **Restart button (admin-only).** `POST /master-config/restart` stamps
   `restartedAt`, polls the StatefulSet rollout (new pod Ready) then
   salt-api login healthy inside a bounded timeout, flashes the outcome,
   audits `master-restart:<result>`; unhealthy-after-timeout links the
   one-click revert (re-patch last snapshot + restart). Dev/unconfigured
   refuses with the manual `kubectl rollout restart` command. Tests:
   fake-transport success/timeout/refusal matrix, operator-403, audit rows.
5. **Reactor bodies under the admin gate (built).** Ensure `/srv/states/reactor`
   exists (seed initContainer addition, first-boot-only like states);
   admin-only edit/save for reactor SLS reusing Unit 3 guards. Mapping
   mutations fan out to all pods through the runner (per-master reactor
   systems would otherwise diverge); export renders the union for
   pasting into the stanza. Tests: missing-roots bootstrap, admin
   gate, operator v4 flows untouched.
6. **Docs + audit polish.** Create the missing `docs/install-kubernetes.md`
   (secret creation, apply, restore runbook); `docs/user.md` Master Config
   section (scope, blocking validation, stale flow, restart semantics);
   verify every new mutation logs by grepping audit rows in tests.
7. **Cluster-trio manifests (built).** `salt-master.yaml` at `replicas: 3`
   as a Salt cluster (isolated filesystem, Raft over 4507): read-only
   shared-keypair Secret volume (`salt-master-keys`, owner-created, never
   committed), pinned cluster identity (`salt-master-cluster-keys`:
   `cluster.pem`/`cluster.pub`, copied onto each PVC by
   `cluster-entrypoint.sh` before the daemon can mint its own), cluster
   join secret (`salt-master-cluster`), per-pod `keys` PVCs retained for
   accepted minion keys, required one-per-hostname anti-affinity,
   `RollingUpdate` + `OrderedReady`, and a `minAvailable: 2` PDB; minion
   MQ is per-node (`salt-master-mq` `internalTrafficPolicy: Local` +
   Traefik hostPorts). Unit 4's restart rolls one pod at a time against
   this strategy. Gate outcome, closed: stock `:lts` ships no PG driver,
   so the driver rides our own image (`Containerfile.salt-master`,
   `psycopg2-binary` plus the DNS-identity build-time patch, built to
   Quay as `overstate-salt-master:lts-pg14`, StatefulSet pointed at it).
   Tests: `tests/test_deploy.py` (`test_master_trio_replicas_and_image`,
   Secret volumes without committed Secrets, no app Secret RBAC).
8. **Shared job cache + owner Secrets (built).** Stock `pgjsonb` returner
   (`master_job_cache: pgjsonb` with flat `returner.pgjsonb.*` keys)
   straight into the app's own `overstate` database — no separate `salt`
   database; `jids` + `salt_returns` tables created by the app — with the
   whole block, password included, in the owner-held `salt-master-db`
   Secret merged into the config dir by the projected volume (subPath file
   mounts into the read-only ConfigMap mount fail with ENOTDIR on these
   nodes; ConfigMaps never hold passwords — enforced by test). A legacy
   `postgres_local_cache` block, or an empty `master_job_cache` value,
   leaves the masters on local disk: jobs run but complete with no output
   in the UI. `docs/install-kubernetes.md` §§4–6 carry the
   keypair/cluster-key/returner procedures, Secret-creation commands, and
   the rotation runbooks (replace Secret + roll the pods one at a time).
   Tests: ConfigMap renders without embedding secrets; Secrets absent
   from the repo and kustomization.
9. **Accept-on-all-three + publish fan-out + trio drill (built, drill
   pending).** Keys page fans acceptance to all three pods with per-pod
   state and audit; every Jobs publish fans out to all three pods
   (per-pod salt-api DNS through the headless service,
   `overstate_ui/fleet.py`) under one shared JID with results merged into
   one view. An unreachable pod degrades, never silently splits.
   Convergence help: hourly `key-reconcile` CronJob plus the Keys-page
   **Review & reconcile** button (same-fingerprint accept-only rule).
   Live proofs (owner-scheduled): action returns merge from minions on any
   pod; delete one pod and confirm minions stay servable with no
   re-acceptance (quorum at 2 of 3). Tests: fan-out matrix on fake
   transports (one-master-down acceptance and publish still converge).

## Validation Plan

- `.venv/bin/pytest -q` green after every unit; focused suites:
  `tests/test_master_config_k8s.py` (client + editor + restart matrix),
  `tests/test_deploy.py` (k8s manifests + RBAC), frozen `tests/test_files.py`,
  `tests/test_git_sync.py`, `tests/test_reactor.py` untouched.
- Static: `kubectl apply --dry-run=client -k deploy/kubernetes` clean;
  rendered-RBAC review (`kustomize build` piped to a verb audit) proving no
  cluster scope and no ConfigMap `delete`.
- Live (staging namespace or a scratch copy first): edit a key, confirm
  snapshot-then-patch and audit rows; save with a raced external `kubectl
  edit` and confirm stale refusal; restart and confirm salt-api health
  returns; revert a change through history and confirm recovery.
- Highest-risk check: restart onto a deliberately broken `master.conf` —
  prove the rolling update stalls on the unready pod while the two
  survivors keep serving last-good config, the health-timeout path fires
  (never reported as success), the audit names the outcome, and one-click
  revert plus restart recovers. Run against production only with the
  owner's explicit scheduling.
- Trio proofs (production, owner-scheduled): merged action results from
  minions attached to any pod (fan-out proof); pod-kill failover (delete
  one pod, minions stay servable, no re-acceptance, quorum holds at 2 of
  3); rolling restart never drops below two Ready masters. Gate outcome,
  closed: the custom image ships `psycopg2-binary` (Unit 7).

## Risks / Rollback

- Lockout via `api.conf`/eauth edit (accepted risk): mitigated by blocking
  validation, pre-write snapshots, point-of-action banner; recovery is
  revert + restart, executable over `kubectl` by the cluster-admin owner —
  strictly better than the old host-console requirement.
- Master CrashLoops after restart: bounded rollout + salt-api health poll,
  explicit unhealthy outcome, one-click revert runbook; no auto-rollback
  (automation cannot be trusted with a down master — a human confirms).
  With the shared ConfigMap, a broken config stalls the rolling update on
  the unready pod while the two surviving pods keep serving last-good
  config — the timeout path fires with the fleet still managed.
- History ConfigMap growth: hard cap at 20 snapshots (oldest dropped on
  write); per-key size gate keeps total far under the 1 MiB ConfigMap limit.
- Scope creep into keys/TLS: physically unreachable (owner-held Secret +
  per-pod PVCs with no app Secret RBAC, plus image-managed TLS); tests
  assert the editor cannot address them.
- Shared keypair rotation is the sharpest multi-master edge: replacing the
  Secret changes master identity fleet-wide, so the runbook rolls pods one
  at a time and minions must trust the new key before the old pod leaves.
  A botched rotation is the one failure revert-plus-restart cannot fix
  alone — it needs the documented re-acceptance procedure.
- PG returner is a new runtime dependency of all three masters: if CNPG is
  down, job results stop persisting while minion control still works; the
  plan accepts degraded history over blocked control, and the drill proves
  it.
- Rollback: each unit reverts independently (`git revert`); no migration to
  unwind; removing the feature is deleting the blueprint registration plus
  re-applying the previous Role, after which kubectl/git management keeps
  working. Trio rollback is removing the `master_job_cache` block (local
  disk cache is the safe subset) — never `replicas: 1`: a solo pod
  crash-loops (the image requires non-empty `cluster_peers`), so 1 replica
  is a brief diagnostic only.

## Open Questions

None. History model (k8s-native revisions + audit), scope (whole owned
ConfigMap including `api.conf`), restart (explicit UI button, never
automatic), and roles (admins only) were settled by the owner, and the
multi-master extension (three-node cluster, shared keypair + pinned
cluster identity + pgjsonb cache, D6–D12) was settled in the grill
interview and built as a trio; the remaining validation unknowns
(broken-config recovery timing, staging fidelity) are validation steps
with designed fallbacks, not questions. (`psycopg` presence, the old
gate, is closed — the custom image ships `psycopg2-binary`.)

## Sources

- https://kubernetes.io/docs/reference/labels-annotations-taints/
  (`kubectl.kubernetes.io/restartedAt`: `kubectl rollout restart` works by
  patching pod-template metadata with this annotation; inspected this run.)
- https://kubernetes.io/docs/tasks/run-application/access-api-from-pod/
  (in-pod API access: `KUBERNETES_SERVICE_HOST`/`PORT` env, SA token and
  namespace files; inspected this run.)
- https://docs.saltproject.io/en/3006/ref/returners/all/salt.returners.pgjsonb.html
  (implemented path: `master_job_cache: pgjsonb` with flat
  `returner.pgjsonb.*` keys plus the `jids`/`salt_returns` schema, against
  the app's own `overstate` database; inspected this run.)
- https://docs.saltproject.io/en/3006/ref/returners/all/salt.returners.postgres_local_cache.html
  (considered and rejected: its `master_job_cache.postgres.*` prefix means
  a wrong prefix silently falls back to local cache; inspected this run.)
- All other evidence is workspace code, manifests, tests, and owner answers
  cited inline above.

## Decision Record (grill, Draft)

- **D1 (Draft): whole-ConfigMap snapshots.** Every save snapshots the entire
  `salt-master-config` data map into `salt-master-config-history` (cap 20
  revisions); revert re-patches a whole snapshot so multi-key changes restore
  atomically. Rejected: per-key snapshots (non-atomic multi-key restore).
- **D2 (Draft): bounded validation, no validator Job.** The save gate stays
  YAML-parse + shape + size + banner; crash safety rests on the health poll
  and one-click revert. A pre-patch validator Job (candidate config booted in
  a throwaway master-image pod) is a deferred proposal for a later stage,
  not this plan.
- **D3 (Draft): broken-config drill on production, owner-scheduled.** Only
  the live master (minions, keys, Traefik) can prove the timeout-and-revert
  path; the owner picks the window. Rejected: scratch-only drill (proves
  plumbing, not recovery).
- **D4 (Draft): tighten the app Role.** Scope to owned `resourceNames`,
  keep `create`, drop ConfigMap `delete`; re-applied to the live cluster in
  Unit 2 (namespace-scoped only). Rejected: leaving broad verbs as
  convention-only enforcement.
- **D5 (Draft): reactor SLS bodies are admin-only.** Reactor bodies execute
  with master privileges and fire fleet-wide; the mapping stays
  operator-usable via the runner page. Rejected: operator body editing
  (consistency with v4, but mistaken bodies auto-fire with no approval).
- **D6 (Built): boundary widened to multi-master.** Owner explicitly added
  multi-master support after the D1–D5 interview; it was previously a
  non-goal. Topology settled in D7–D12 below, built as a three-node Raft
  cluster rather than the originally sketched two-pod pair.
- **D7 (Built): three-node Salt cluster, one shared keypair.** Three
  master pods (isolated filesystem, Raft over 4507) present the same
  public key behind the existing Services. Overstate manages the shared
  ConfigMap and rolls the pods one at a time under RollingUpdate +
  OrderedReady with a `minAvailable: 2` PDB. Peer identity is the stable
  pod DNS name, stamped by `cluster-entrypoint.sh`. Rejected: syndic
  hierarchy (cross-fleet tiering, not this fleet) and independent fleet
  (no HA, one master per minion).
- **D8 (Built): owner-provisioned keypair plus cluster Secrets,
  generation documented.** The owner generates the Salt RSA keypair once
  (procedure in `docs/install-kubernetes.md` §4), stores it in a plain
  non-expiring `salt-master-keys` Secret, and all three masters mount it
  read-only; the app gets no Secret access. Same treatment for the
  cluster join secret (`salt-master-cluster`) and the pinned cluster
  identity (`salt-master-cluster-keys`: `cluster.pem`/`cluster.pub`,
  §6). Rejected: cert-manager (X.509 with expiry/renewal; renewal would
  rotate master identity and break every minion pin at once),
  app-distributed keys (most sensitive bytes in app hands), shared RWX
  volume (infra dependency).
- **D9 (Built): per-master writable PKI, accept-on-all-three.** Each
  master keeps its own accepted-keys dir (per-pod PVC, existing pattern);
  the Keys page fans acceptance to all three masters (union roster with
  per-pod chips, degrade-on-unreachable) so a minion is servable
  whichever it lands on; the hourly `key-reconcile` CronJob plus the
  Review & reconcile button complete same-fingerprint trust. Rejected:
  shared RWX PKI volume (infra dependency, shared fate).
- **D10 (Built): active-active trio with a shared job-cache returner.**
  All three masters serve behind one Service; job results land in a
  shared store so lookups hit whichever master the app asks. Rejected:
  active-passive (consistent but standby idle, needs a second Traefik
  port pair).
- **D11 (Built): Postgres as the shared job cache, via `pgjsonb`.** All
  three masters set `master_job_cache: pgjsonb` with flat
  `returner.pgjsonb.*` keys into the app's own `overstate` database;
  salt-api reads through it so app Jobs code is unchanged. Returner
  credentials owner-provisioned (`salt-master-db`, projected as
  `returner.conf`) and documented. Gate closed: the custom image ships
  `psycopg2-binary`. Rejected: `postgres_local_cache` (wrong-prefix
  silent fallback), Redis (eviction semantics, loads the app broker).
- **D12 (Built): full active-active with publish fan-out.** Publish buses
  are per-master, so single-pod publish misses minions attached to the
  other pods. The app publishes every job on all three pods (per-pod
  salt-api DNS through the headless service, `overstate_ui/fleet.py`)
  under one shared JID and merges results into one view; the shared PG
  cache keeps returns visible from any. Minion MQ is per-node
  (`internalTrafficPolicy: Local` + Traefik hostPorts), so each minion is
  single-homed and receives the fanned-out job exactly once. Rejected:
  parking at `replicas: 1` (a solo pod crash-loops — diagnostic only).
