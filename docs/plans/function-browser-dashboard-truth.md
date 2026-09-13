# Function browser + dashboard truth — plan (Draft)

Two slices against the Salt surface we hold but don't use: a
function browser on the run-job form (the confirmed findability
pain) and live truth on the dashboard and job detail (versions,
in-flight jobs, master-cache sync).

## Status
Final. Accepted by the owner 2026-09-13.

## Goal
Operators find the right function with its docs before firing,
and the dashboard plus job detail report live master truth
instead of DB heuristics and stale snapshots.

## Success Criteria
- On `/jobs/new`, typing in the function field searches the live
  minion function index beyond the 13 hardcoded presets, and
  selecting a function shows its `sys.doc` summary with a
  one-click fill into the form.
- The dashboard shows Salt-version skew across the fleet
  (versions present, count per version) from the master, with the
  snapshot fallback when the master is unreachable.
- "Jobs in flight" reflects what the master still holds as
  active, and the job-detail sync pulls live returns from the
  master job cache before falling back to the DB heuristic.
- No grant changes: everything used is already covered by
  `sys.*`, `@runner`, and `@jobs`.
- `.venv/bin/pytest -q` green.

## Context And Current Facts
- `templates/job_new.html:71-72` already has a function
  combobox (`#job-fun` + `#fun-list`), but its source is
  `op_functions`: 13 hardcoded entries built from
  `OPERATION_GROUPS` (`jobs.py:147-152`). Free text covers the
  rest with zero guidance — the findability gap PRODUCT.md
  confirms as the primary pain.
- `sys.*` is granted (`salt-config/api.conf`) and nothing calls
  it. `sys.list_functions` returns the full live index;
  `sys.doc <fun>` returns per-minion docs. Both are read-only
  execution calls fittable through `SaltClient.local` against
  one cached minion (the existing `ping_target()` pattern,
  `dashboard.py:78-81`).
- Dashboard numbers split two ways today: keys/presence live
  via `salt_overview_now` (`tasks.py:158-166`, worker-backed),
  but `in_flight` counts DB rows (`dashboard.py:33`) and
  versions come only from grain snapshots. `manage.versions`
  and `jobs.active` are runner calls covered by `@runner`.
- Job sync is DB-only: `detail()` and the `sync` button call
  `sync_job` (`jobs.py:556,625`), which copies returner rows
  and guesses completion by age (`jobs.py:155-158`). The
  master job cache (`jobs.lookup_jid`, covered by `@jobs`) is
  never consulted, so a job whose returns haven't hit the
  returner yet looks stuck.
- `TAG_CHOICES` (`events.py:26`) already includes `salt/key`;
  presence detail and mine browsing stay out of this round.

## Constraints And Non-goals
- Side navigation stays exactly as-is (project constraint,
  2026-09-12): no new page for the browser; it lives inside
  `/jobs/new`. Dashboard gains panels, not pages.
- Read-only additions only: no new mutations, no new tables or
  migrations, no grant changes.
- Non-goals (explicit follow-ups, not this round): mine
  browser, `salt/presence` subscriptions, `state.show_sls`
  pre-apply preview, `pkg.list_pkgs` compare.
- Slow or fleet-wide calls go through the existing
  `queue_or_none`/`wait_for` worker pattern with the sync
  fallback; no new broker or cache machinery (short TTL reuse
  of the capability-cache shape only if needed).

## Key Decisions
- **Extend the existing combobox, don't build a picker page.**
  `#fun-list` source becomes hardcoded presets plus live
  `sys.list_functions` (deduplicated, presets first); a docs
  panel beside the field lazy-loads `sys.doc` for the
  highlighted function via HTMX and fills the field on click.
  Rejected: standalone browser page (needs nav, forbidden) and
  replacing presets (they encode operator-curated args/about).
- **Query one minion for docs, not the fleet.** `sys.doc`
  output is identical across same-version minions and huge
  across the fleet; `ping_target()` picks the minion, and a
  version-mismatch note covers skew. Rejected: fleet-wide doc
  fetch (slow, redundant).
- **Display-only merge for live job data.** Master-cache
  returns render marked "live" alongside DB rows; nothing
  writes into `SaltReturn`/`JobReturn`. The age heuristic
  stays as the fallback when the master cache has expired the
  JID. Rejected: ingesting live returns (schema/source
  confusion, double-count risk).
- **Versions tolerate unknown shapes.** `manage.versions`
  output varies by version; normalize defensively (per-minion
  map or grouped lists — verify against the dev master in
  implementation) and fall back to grain snapshots silently.
- **Zero grants ship.** `sys.doc`/`sys.list_functions` fall
  under `sys.*`; `manage.versions`/`jobs.active`/
  `jobs.lookup_jid` under `@runner`/`@jobs`. If the dev
  master denies any of them, that surfaces as a capability
  gap, not a plan change.

## Recommended Approach
Two independent units, shippable in either order. Browser
first (it touches the highest-pain surface), dashboard truth
second (it reuses the worker pattern the first unit also
follows for the function index).

## Work Plan
1. **Function index + docs.** Worker-safe helpers
   (`tasks.py` shape): `list_functions_now` (one minion,
   sorted unique names) and `doc_now(fun)` (one minion,
   trimmed summary + argspec). `GET /jobs/new` passes the
   merged index to the combobox; new `GET /jobs/fun-doc`
   (login_required, read-only) returns the docs fragment for
   the panel. Client-side: docs load on highlight, click
   fills the field. Tests: new `tests/test_function_browser.py`
   (stubbed `sys.list_functions`/`sys.doc`: merged list,
   presets-first, doc fragment renders, denial falls back to
   presets-only); viewer can read.
2. **Dashboard truth.** Extend the overview worker payload:
   `manage.versions` skew counts plus `jobs.active` JIDs.
   Dashboard renders a version-skew panel and derives
   in-flight from live-active ∩ DB-incomplete, each with the
   current DB/snapshot fallback when unreachable. Tests:
   extend `tests/test_jobs.py` / `tests/test_tasks.py`
   (stubbed versions/active: skew counts render, fallback
   intact, unreachable master keeps old numbers).
3. **Live job sync.** `sync` button and `detail()` consult
   `jobs.lookup_jid` first (display-only live merge), then
   existing `sync_job`. Tests: stubbed lookup returns merge
   marked live; expired-JID falls back to the heuristic with
   no duplicate rows.

## Validation Plan
- `.venv/bin/pytest -q` green after each unit; new
  `tests/test_function_browser.py`; extended jobs/tasks
  tests; no migration involved.
- Live on the dev stack: type an uncommon function
  (e.g. `network.interfaces`) in the run form, see its docs,
  fill and fire; dashboard shows dev-minion version skew;
  fire an async job and watch in-flight + detail sync track
  the master cache.
- Highest-risk check: `manage.versions` shape. Prove the
  normalizer against the dev master first (stub both
  candidate shapes in tests); if the shape surprises, the
  panel degrades to snapshots with no error.

## Risks / Rollback
- `sys.list_functions` output is large (thousands of names);
  fetched once per form render via worker/fallback, never
  per keystroke (filtering stays client-side like today).
- Master job cache expiry makes `lookup_jid` miss old JIDs;
  the DB heuristic remains the fallback, never removed.
- Version skew note: docs come from one minion; a fleet with
  mixed Salt versions gets an explicit note, not silent
  mixing.
- Rollback: additive routes, template branches, and worker
  helpers; revert the diff, no migration to downgrade.

## Open Questions
None. Grants, integration points, and fallback shapes were
all settled from workspace evidence above; remaining shape
risk (`manage.versions`) is a validation step, not a scope
question.

## Sources
None. No external claims inform this plan; every call named
is covered by grants already in `salt-config/api.conf`, and
remaining shape risks are marked verify-on-master above.
