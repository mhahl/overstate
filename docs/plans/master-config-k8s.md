## Goal

Let Overstate own the Salt master and salt-api configuration as a cloud-native
total solution: an admin can browse, edit, validate, and version every owned
master-config file from the browser, explicitly restart the master from the UI,
and manage reactor configuration (the `reactor:` stanza plus reactor SLS
bodies), all against the Kubernetes deployment in the `overstate` namespace.
The masters run as an active-active failover pair sharing one keypair and
one Postgres job cache: Overstate manages both masters' shared ConfigMap,
restarts them one at a time, and accepts minion keys on both.
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
- A Restart button patches the `salt-master` StatefulSet to roll the master
  pods one at a time, polls the rollout and salt-api back to healthy inside
  a bounded timeout, and audits the result; outside the cluster (dev) it refuses with the reason and
  shows the equivalent `kubectl` command instead.
- An admin can edit reactor SLS bodies under the shared reactor roots and set
  the `reactor:` mapping stanza in `master.conf`; the live mapping stays
  readable through the existing runner-based Reactor page.
- Operators and viewers see no Master Config page and get 403 on forged POSTs.
- Two master pods serve behind the existing Services; every job action
  publishes on both pods and minion results merge into one view no matter
  which pod a minion is attached to, and killing one pod leaves minions
  servable with no key re-acceptance.
- `.venv/bin/pytest -q` green; existing Files/reactor/git contracts untouched.

## Context And Current Facts

- Owned config surface is the `salt-master-config` ConfigMap
  (`deploy/kubernetes/salt-master-config.yaml:15-66`): data keys
  `master.conf` (near-empty overrides) and `api.conf` (eauth grants plus
  netapi clients). It mounts read-only at `/home/salt/data/config`
  (`salt-master.yaml:44-47`), which the cdalvaro image reads as config
  drop-ins; Salt reads config once at startup, so edits need a master restart.
  Accepted minion keys live on per-pod `keys` PVCs (`salt-master.yaml:74-81`);
  the master keypair moves to an owner-held Secret in Unit 7. salt-api TLS is
  image-minted — all three stay outside the editor's reach by construction.
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
- Restarts roll one master pod at a time (StatefulSet default): the pair
  never bounces together, so one master always serves minions and salt-api.
- Owner-provisioned Secrets (shared master keypair, master DB user) are
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
  out to both masters) and no validation path at all.
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
- **One StatefulSet, two replicas, one shared ConfigMap.** Both masters
  run identical config from `salt-master-config`; per-pod PVCs keep separate
  accepted-keys dirs (D9); the shared keypair Secret mounts read-only into
  both pods. Service selectors already match both pods, so MQ and salt-api
  fan out with no manifest surgery. Rejected: two StatefulSets (double the
  manifests and restart paths for no behavioral difference).
- **Shared job cache via CNPG `master_job_cache`.** Both masters read/write
  job results to Postgres, so salt-api on either master answers consistently
  and app Jobs code is unchanged. The `master_job_cache` + credentials block
  lives in the owned ConfigMap; the DB user and keypair Secret are the two
  owner-provisioned Secrets. Rejected: Redis cache (D11) and app-side dual
  lookup (every Jobs read fans out; merge bugs for free).
- **Accept-on-both through the existing Keys page.** Acceptance calls the
  salt-api wheel/key function against each master pod (via the per-pod
  DNS through the headless service); the UI shows per-master acceptance
  state. A minion accepted on both is servable whichever pod it lands on.

## Recommended Approach

One track, nine ordered units, each independently revertable, no migration.
Client and RBAC first (provable without UI), then the admin-only
browser/editor with history, then the restart button, then reactor bodies,
then docs polish, then the multi-master extension (units 7–9, which update
the restart and docs units they build on). The restart unit lands after the
editor on purpose: files must be safe before a button can roll the masters
onto them.

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
5. **Reactor bodies under the admin gate.** Ensure `/srv/states/reactor`
   exists (seed initContainer addition, first-boot-only like states);
   admin-only edit/save for reactor SLS reusing Unit 3 guards; mapping and
   export unchanged on the runner. Tests: missing-roots bootstrap, admin
   gate, operator v4 flows untouched.
6. **Docs + audit polish.** Create the missing `docs/install-kubernetes.md`
   (secret creation, apply, restore runbook); `docs/user.md` Master Config
   section (scope, blocking validation, stale flow, restart semantics);
   verify every new mutation logs by grepping audit rows in tests.
7. **Failover-pair manifests.** `salt-master.yaml` to `replicas: 2`,
   read-only shared-keypair Secret volume (name/env-documented, Secret
   itself owner-created, never committed), per-pod `keys` PVCs retained for
   accepted minion keys; update Unit 4's restart to assert rolling
   one-at-a-time (second pod Ready before the first restarts). Gate
   outcome: stock `:lts` ships no PG driver, so the driver rides our own
   image (`Containerfile.salt-master`, built to Quay, StatefulSet pointed
   at it) — D11 stands, redirected rather than reopened. Tests: `test_deploy.py`
   (replicas, Secret volume without committed Secret, no app Secret RBAC).
8. **Shared job cache + owner Secrets.** `postgres_local_cache` (its
   `master_job_cache.postgres.*` keys are the documented master-cache
   path; `pgjsonb` documents only `returner.*` keys, and a wrong prefix
   would silently fall back to local cache) with the whole block —
   password included — in an owner-held Secret merged into the config dir
   by a projected volume (subPath file mounts into the read-only ConfigMap
   mount fail with ENOTDIR on these nodes; ConfigMaps never hold
   passwords). Dedicated
   `salt` database + least-privilege role, `jids` + `salt_returns` tables
   from the documented schema; `docs/install-kubernetes.md` gains the keypair-generation
   procedure, both Secret-creation commands, and the rotation runbook
   (replace Secret + roll both, one at a time). Tests: ConfigMap renders the
   block without embedding secrets; docs commands quoted verbatim in tests.
9. **Accept-on-both + publish fan-out + pair drill.** Keys page fans
   acceptance to both pods with per-master state and audit; every Jobs
   publish fans out to both pods (per-pod salt-api DNS through the
   headless service) with results merged into one view (shared explicit
   JID when the API supports it, paired JIDs merged otherwise — decided
   in implementation with tests). Live proofs: action returns merge from
   minions on either pod; delete pod-0 and confirm minions stay servable
   with no re-acceptance (failover proof). Tests: fan-out matrix on fake
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
  prove the pod goes unready, the health-timeout path fires (never reported
  as success), the audit names the outcome, and one-click revert plus
  restart recovers. Run against production only with the owner's explicit
  scheduling: the single master serves the live fleet while down.
- Multi-master proofs (production, owner-scheduled): merged action
  results from minions attached to either pod (fan-out proof); pod-kill
  failover (delete pod-0, minions stay servable, no re-acceptance, pod-1
  takes all); rolling restart never leaves zero Ready masters. Gate
  outcome: stock image lacks the driver, so Unit 7 ships it in our own
  image (D11 redirected, not reopened).

## Risks / Rollback

- Lockout via `api.conf`/eauth edit (accepted risk): mitigated by blocking
  validation, pre-write snapshots, point-of-action banner; recovery is
  revert + restart, executable over `kubectl` by the cluster-admin owner —
  strictly better than the old host-console requirement.
- Master CrashLoops after restart: bounded rollout + salt-api health poll,
  explicit unhealthy outcome, one-click revert runbook; no auto-rollback
  (automation cannot be trusted with a down master — a human confirms).
  With the shared ConfigMap, a broken config stalls the rolling update on
  the unready pod while the surviving pod keeps serving last-good config —
  the timeout path fires with the fleet still managed.
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
- PG returner is a new runtime dependency of both masters: if CNPG is down,
  job results stop persisting while minion control still works; the plan
  accepts degraded history over blocked control, and the drill proves it.
- Rollback: each unit reverts independently (`git revert`); no migration to
  unwind; removing the feature is deleting the blueprint registration plus
  re-applying the previous Role, after which kubectl/git management keeps
  working. Multi-master rollback is `replicas: 1` plus removing the
  `master_job_cache` block — single-master behavior is the safe subset.

## Open Questions

None. History model (k8s-native revisions + audit), scope (whole owned
ConfigMap including `api.conf`), restart (explicit UI button, never
automatic), and roles (admins only) were settled by the owner, and the
multi-master extension (failover pair, shared keypair + PG cache, D6–D11)
was settled in the grill interview; the remaining validation unknowns
(broken-config recovery timing, `psycopg` presence, staging fidelity) are
validation steps with designed fallbacks, not questions.

## Sources

- https://kubernetes.io/docs/reference/labels-annotations-taints/
  (`kubectl.kubernetes.io/restartedAt`: `kubectl rollout restart` works by
  patching pod-template metadata with this annotation; inspected this run.)
- https://kubernetes.io/docs/tasks/run-application/access-api-from-pod/
  (in-pod API access: `KUBERNETES_SERVICE_HOST`/`PORT` env, SA token and
  namespace files; inspected this run.)
- https://docs.saltproject.io/en/3006/ref/returners/all/salt.returners.postgres_local_cache.html
  (`master_job_cache: postgres_local_cache` with `master_job_cache.postgres.*`
  keys, psycopg2 requirement; inspected this run.)
- https://docs.saltproject.io/en/3006/ref/returners/all/salt.returners.pgjsonb.html
  (documents only `returner.pgjsonb.*` keys plus the `jids`/`salt_returns`
  schema — no documented `master_job_cache.pgjsonb.*` path, hence rejected
  for the master cache; inspected this run.)
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
- **D6 (Draft): boundary widened to multi-master.** Owner explicitly added
  multi-master support after the D1–D5 interview; it was previously a
  non-goal. Topology and units settled in D7–D11 below — no implementation
  approved.
- **D7 (Draft): active-failover pair, one shared keypair.** Two master pods
  present the same public key behind the existing Services. Overstate
  manages the shared ConfigMap and rolls the pods one at a time.
  Rejected: syndic hierarchy (cross-fleet tiering, not this fleet) and
  independent fleet (no HA, one master per minion).
- **D8 (Draft): owner-provisioned shared keypair, generation documented.**
  The owner generates the Salt RSA keypair once (procedure documented in
  `docs/install-kubernetes.md`), stores it in a plain non-expiring Secret,
  and both masters mount it read-only; the app gets no Secret access.
  Rejected: cert-manager (X.509 with expiry/renewal; renewal would rotate
  master identity and break every minion pin at once), app-distributed keys
  (most sensitive bytes in app hands), shared RWX volume (infra dependency).
- **D9 (Draft): per-master writable PKI, accept-on-both.** Each master
  keeps its own accepted-keys dir (per-pod PVC, existing pattern); the Keys
  page fans acceptance to both masters so a minion is servable whichever it
  lands on. Rejected: shared RWX PKI volume (infra dependency, shared fate).
- **D10 (Draft): active-active with a shared job-cache returner.** Both
  masters serve behind one Service; job results land in a shared store so
  lookups hit whichever master the app asks. Rejected: active-passive
  (consistent but standby idle, needs a second Traefik port pair).
- **D11 (Draft): Postgres as the shared job cache.** Both masters set
  `master_job_cache` to CNPG; salt-api reads through it so app Jobs code is
  unchanged. Master DB user owner-provisioned and documented. Image must
  ship `psycopg` — proved in the first multi-master unit or this reopens.
  Rejected: Redis (eviction semantics, loads the app broker).
- **D12 (Draft): full active-active with publish fan-out.** Publish buses
  are per-master, so single-pod publish misses minions attached to the
  other pod. The app publishes every job on both pods (per-pod salt-api
  DNS) and merges results into one view; the shared PG cache keeps
  returns visible from either. Rejected: parking at `replicas: 1`
  (defers HA the pair was built for).
