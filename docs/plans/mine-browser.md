# Mine browser — plan (Draft)

Read the Salt mine through a dedicated page: pick a target and a
mine function, see per-minion values. The other half of the
existing `mine.update` preset, which refreshes mine data today
with nowhere to view it.

## Status
Final. Accepted by the owner 2026-09-13.

## Goal
Operators can answer "what does the fleet report for X" from the
mine without shelling into the master: a Mine page queries by
target plus function and renders per-minion values, with a clear
staleness story and a one-click refresh path.

## Success Criteria
- A Mine page at `/mine` (side-nav entry) accepts a target,
  target type, and mine function, and renders a per-minion
  values table from `mine.get`.
- Empty results explain themselves (nothing stored for that
  function, or minions down) instead of showing a blank table.
- A staleness note plus a link to the existing `mine-update`
  preset tells operators how fresh the data is and how to
  refresh it.
- Denied or failed reads flash the Salt error, never fake data.
- `.venv/bin/pytest -q` green.

## Context And Current Facts
- `mine.update` is granted (`salt-config/api.conf:13`) and
  wired as a run-form preset (`jobs.py:106,139-140`), but
  nothing reads the mine back. `mine.get` is not granted —
  proven live this run: `mine.get` via salt-api answers 401
  for the eauth user.
- Live contract, verified on the dev master this run via
  `sys.doc`: `mine.get(tgt, fun, tgt_type='glob',
  exclude_minion=False)`; `fun` takes one function or a
  list/comma-separated string; executed as a normal local call
  against any single minion, which returns the master's cached
  values for the whole target expression. Mine functions on
  the minion: `delete, flush, get, get_docker, send, update,
  valid` — no index call exists, so the function is typed,
  not picked.
- Read precedents: pillar index (`pillar.py`, per-minion
  snapshots), single-minion reads via `ping_target()`
  (`dashboard.py:78-81`), worker
  `queue_or_none`/`wait_for` with sync fallback (`tasks.py`),
  `TGT_TYPES` plus saved-group→list resolution (`jobs.py:30`,
  `resolve_group_target`).
- Side nav may now change (owner lifted the 2026-09-12 freeze
  on 2026-09-13; recorded in project memory).

## Constraints And Non-goals
- Read-only v1: `mine.get` only. `mine.delete`/`mine.flush`
  (cache-destructive) are excluded on purpose; no new
  mutations, no new tables or migrations.
- Grant change ships with the code: add `mine.*` to
  `salt-config/api.conf` and the `docs/deployment.md` mirror
  (grill M2). Delete/flush stay UI-less in v1.
- Non-goals: mine-function discovery UI (no index call
  exists), per-value history or diffing, `mine.send` from the
  UI, presence subscriptions.

## Key Decisions
- **Standalone `/mine` page with a nav entry.** Mine is
  fleet-scoped (target × function), not per-minion state, so
  it doesn't belong in the minion-detail tabs; the lifted nav
  freeze makes a first-class page possible. Entry placed with
  the data-browsing items (after Pillar). Rejected: stuffing
  it into Minions detail or the job form (wrong scope in both
  cases).
- **New `mine.py` blueprint mirroring `pillar.py`.**
  `GET /mine` (login_required): target + target type +
  function form, `mine.get` through the worker/fallback
  against the `ping_target()` minion, per-minion table.
  Rejected: reusing the jobs runner (different arg shape and
  no JID/audit semantics needed for a read).
- **Reuse `TGT_TYPES` with group→list resolution.**
  Same target language as firing, including saved groups
  resolved via `resolve_group_target`; grain/compound
  pass through as `mine.get` natively supports them.
  Rejected: glob-only (arbitrary restriction with no
  technical basis).
- **Staleness is stated, not solved.** `mine.get` carries no
  timestamps, so the page notes data is cached and links the
  existing `mine-update` preset for refresh. Rejected:
  background refresh machinery (new scheduler surface for a
  v1 browser).
- **Free-text function, no discovery.** No `mine.list` exists
  to populate a picker; the field takes examples
  (`network.ip_addrs`) and unknown functions yield the empty
  state. Rejected: faking an index from `sys.list_functions`
  (execution functions ≠ populated mine functions).

## Recommended Approach
Single unit: grant, blueprint, nav entry, template, tests,
two docs lines. No sequencing boundary inside it is worth a
split.

## Work Plan
1. **Grant.** Add `- mine.*` to `salt-config/api.conf` and
   the `docs/deployment.md` grant block (grill M2).
2. **Blueprint.** New `overstate_ui/mine.py` + registration
   in `__init__.py`: `GET /mine` (login_required) reads
   target/tgt_type/fun from the query string, resolves saved
   groups to list, calls `mine.get` via worker/fallback
   against the `ping_target()` minion, renders
   `mine.html` with entries/error/empty states. SaltApiError
   flashes; empty (no data or unknown function) explains.
3. **Nav + template.** Side-nav Mine entry after Pillar;
   `mine.html` reuses the schedules/pillar card/table idioms
   (function form on top, per-minion values table, empty
   state, staleness note with `mine-update` preset link).
4. **Tests.** New `tests/test_mine.py` (stubbed `mine.get`:
   table renders, empty explains, denial flashes, viewer
   reads; group target resolves to list).
5. **Docs.** `docs/user.md` short Mine section (the
   `docs/deployment.md` grant block is covered by step 1).

## Validation Plan
- `.venv/bin/pytest -q` green; new `tests/test_mine.py`; no
  migration involved.
- Live on the dev stack: the dev mine is unpopulated, so
  seed once via `mine.send` from the master shell with a
  clearly-marked test key, read it back through `/mine`,
  then let it expire (or flush from the shell — dev only,
  documented in the validation notes, never in the app).
- Highest-risk check: the 401-to-grant path. Prove the page
  denies cleanly before the grant lands and reads after,
  against the dev master.

## Risks / Rollback
- Unpopulated mine functions are the common case, not an
  error: the empty state must say "nothing stored" and point
  at refresh, or operators will file it as broken.
- `mine.get` values are arbitrary JSON; render defensively
  (tojson fallback per value, never assume strings).
- Rollback: additive blueprint, nav entry, template, one
  grant line; revert the diff (grant removal re-denies).

## Open Questions
None. Call shape, grants, minion-pick, and empty-mine
behavior were all verified above; seeding is a validation
detail with a documented path.

## Sources
None. No external claims inform this plan; the `mine.get`
contract came from live `sys.doc` on the dev master this
run, and all other evidence is workspace-bound.

## Grill decisions (Final)
- M1 (Draft): Mine entry sits with the data-browsing items,
  right after Pillar. Settled by the owner 2026-09-13
  (accepted the recommendation).
- M2 (Draft): broad `mine.*` grant, mirroring `schedule.*`.
  Settled by the owner 2026-09-13 (chose the alternative over
  the narrow recommendation). Consequence: `mine.delete` and
  `mine.flush` become callable by the service account; v1
  still exposes no UI for them.

## Unresolved
None. Accepted by the owner 2026-09-13.
