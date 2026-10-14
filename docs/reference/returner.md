# Job return path (grill decision D1: stock-first, then upsert)

Every Salt job's returns flow through a `pgjsonb`-family returner into
Overstate's Postgres. The dev master runs the **stock `pgjsonb`
returner** (`salt-config/returner.conf`); the production cluster is
moving to the idempotent **`pgjsonb_upsert`** variant (§3) for the
duplicate-write reason below. Either way there is no second store.

## 1. How it works

1. The dev master (`salt-master` service, see `Containerfile.salt-master`)
   sets `master_job_cache` (`salt-config/returner.conf` in dev; the
   owner-held `salt-master-db` Secret projected as `returner.conf` in
   production — never the owned ConfigMap, passwords are enforced out of
   it by test), so every job stores its load and returns.
   (`ext_job_cache` must stay unset: it handles publish-time loads only
   and disables return storage.)
2. The returner writes two transport tables, owned by the master:
   `jids` (jid → job load) and `salt_returns` (one row per minion return).
   The app creates them (`create_all`) and versions them in Alembic, but
   never writes them.
3. The app reads `salt_returns` into its own `jobs` / `job_returns` tables
   (Phase 5), which is what the UI renders. The transport tables are never
   queried by templates.

## 2. Why the stock returner is not enough on the cluster

`salt_returns` has primary key `(jid, id)`, and the stock returner's
write is a bare `INSERT` with no `ON CONFLICT`. On a single master that
is one write per return and never collides. On the 3-master cluster all
three members share the one table: a minion return lands first on the
master the minion is attached to (that `INSERT` wins), then replicates
over the cluster bus to the other two members, whose cache writers fire
the identical `INSERT` — and both fail with
`psycopg2.errors.UniqueViolation`, twice each (once from the `_get_serv`
context manager, once from `returner()`).

Observed in production (Oct 2026): 29 shared failed JIDs in 40 minutes,
every failure on masters 1/2, none on master 0; the table held 775 rows
for 775 distinct JIDs — no data loss, pure log spam scaling as
`(masters − 1) × returns`. It also masks genuine database failures in
the noise. Upstream already concedes the pattern: `save_load()` writes
`jids` with `ON CONFLICT (jid) DO UPDATE` (salt issue #22171) — the
`salt_returns` write was simply never given the same treatment, because
upstream returners assume one writer per table and Overstate's
shared-cache multi-writer topology is not Salt's supported one (Salt's
own clustering routes cache ownership per entry via Raft + HashRing;
Enterprise sidesteps it with a single RaaS ingestion service).

## 3. `pgjsonb_upsert`: the idempotent variant

`salt-extmods/pgjsonb_upsert.py` (virtual name `pgjsonb_upsert`) is the
stock returner with exactly one change: the `salt_returns` write carries
`ON CONFLICT (jid, id) DO NOTHING`. First writer wins (today's effective
outcome), late duplicates are absorbed, and a `rowcount == 0` is logged
at debug so duplicate volume stays observable without tracebacks.
Everything else — `save_load` (already an upsert upstream), getters,
`clean_old_jobs`, `event_return` — is delegated to the stock module, so
upstream fixes keep flowing without a rebase. It reads the existing flat
`returner.pgjsonb.*` keys, so no credential changes are needed; the only
config change is `master_job_cache: pgjsonb_upsert`.

Why a wrapper, not a tree patch: `docs/reference/upstream-salt-patches.md`
patches only cluster-identity code with exact-string anchors that fail
the build on drift. An `INSERT`-line anchor would be churn-prone across
Salt upgrades; the wrapper keeps the delta to one SQL clause.

Why `DO NOTHING` and not `DO UPDATE`: it preserves first-seen
semantics (original `alter_time` kept). `DO UPDATE` would let a stale
bus-replicated duplicate overwrite a fresher row and lie about when the
minion answered. The choice is pinned by
`tests/test_pgjsonb_upsert.py::test_conflict_clause`.

### Status: built, NOT yet activated

The module is baked into the master image (`Containerfile.salt-master`
`COPY`s it to the baked-in `extension_modules` dir
`/var/cache/salt/master/extmods` — ephemeral, not on any PVC and not
under `file_roots`, so it must ship in the image; masters cannot pick
it up from the srv share) and covered by
`tests/test_pgjsonb_upsert.py` (10 tests: SQL clause, stock-identical
params, duplicate absorption, genuine-error surfacing, delegation of
every other entry point). Activation is a separate, deliberate step:

1. Rebuild + roll the master image (module present in extmods).
2. Flip `master_job_cache: pgjsonb` → `pgjsonb_upsert` in
   `salt-config/returner.conf` (dev) and in the owner-held
   `salt-master-db` Secret (production).
3. Rolling restart the `salt-master` StatefulSet (minions ride out the
   blip on TCP; never flip the config against an image lacking the
   module — return storage would break on every master).
4. Verify: publish `test.ping`, confirm zero `UniqueViolation` on all
   three masters, confirm rows still land, spot-check UI job history.

## Pointing a real master at Overstate

1. Install a postgres driver where the master runs (`psycopg2`).
2. Create the `jids` / `salt_returns` schema: run `alembic upgrade head`
   against the Overstate database (revision `46615d932d32` adds them).
3. Add to the master config (adapt host/user/pass):
   `master_job_cache: pgjsonb`, plus the flat `returner.pgjsonb.*`
   connection keys from `salt-config/returner.conf` (flat dotted keys —
   the master job-cache path does not traverse a nested mapping).
4. If the stock schema ever fights the app's queries, the agreed fallback
   is a thin custom returner writing `jobs` / `job_returns` directly —
   not a second store.

## Audit

Every mutating click also writes an `audit_events` row (who, action, JID)
via `overstate_ui.audit.log_event`, independent of the return path, so an
action is traceable even if its returns never land.
