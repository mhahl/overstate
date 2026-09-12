## Goal
Close three fleet-operation gaps in one round: saved minion groups
usable as job targets, killing a runaway job from its detail page,
and a first-class orchestration runner.

## Success Criteria
- An operator can save the current bulk selection (or a typed list)
  as a named group, reopen it from the Minions page, and fire a job
  at it by choosing the group target type.
- A running job's detail page offers Kill; after killing, the
  detail shows which minions reported the kill and the action is
  audit-logged.
- An operator can run `state.orchestrate` with a mods name plus
  saltenv, pillar override, and test mode from a dedicated page,
  and watch returns like a normal job.
- Ungated, sync, and salt-ssh runs behave exactly as today.

## Context And Current Facts
- No Groups UI exists: `templates/minions.html` has search plus
  table only, and no `/groups` route exists in `minions.py`.
  `TGT_TYPES` (`jobs.py:30`) already includes `nodegroup`
  (master-defined passthrough); bulk selection plus
  `suggest_glob` (`jobs.py:52`) resolve checked minions to a glob.
- Kill mechanism verified on the installed Salt
  (`salt-call --local sys.doc saltutil.kill_job`): sends SIGKILL
  to the named job's process via a normal local publish
  (`salt '*' saltutil.kill_job <jid>`). The runner-style
  `jobs.kill_job` is not available on this master image, so the
  plan does not depend on it.
- `SaltClient.runner()` exists and the dashboard already calls
  `manage.status` through it; `@runner` is granted in
  `salt-config/api.conf`, which covers `state.orchestrate`.
  `saltutil.kill_job` is not in the execution grant list and must
  be added.
- The RQ worker plus `queue_or_none`/`wait_for` and `sync_job`
  already carry long Salt work; orchestration runs there.
- `log_event` covers jobs, keys, schedules, users, rotation, and
  batch waves; kill and orchestrate join that list.

## Constraints And Non-goals
- No Salt-master writes, no new broker, no new auth machinery.
- Groups are app-owned data about minion IDs, resolved to list
  targets at fire time; the plan never edits master nodegroups.
- No scheduled orchestration, no orchestrate-state editor, no
  pillar-file editing (pillars stay read-only; orchestrate takes a
  small inline pillar override only).
- Kill is best-effort by nature (a finished or unreachable minion
  reports nothing); the UI reports per-minion kill returns, never
  claims cluster-wide death.

## Key Decisions
- **Saved static groups in a new `minion_groups` table**
  (Alembic; id, unique name, member ID list JSON, timestamps),
  created from the minion bulk selection or typed IDs, edited and
  deleted on the Minions page. New `group` job target type
  resolving to `list` at fire time, so batches, presets, and saved
  jobs work unchanged. Rejected: master nodegroups (defined in
  master config, invisible to salt-api) and dynamic grain facets
  (a second query language to maintain).
- **Kill via `saltutil.kill_job` local publish** to the job's
  original target, fired from the detail page, operator and up,
  audit-logged with the kill job's JID. Rejected: runner
  `jobs.kill_job` (absent on this master image; version-dependent
  elsewhere).
- **Dedicated orchestrate page** (`/jobs/orchestrate`: mods,
  saltenv, test toggle, inline pillar JSON) launching async
  `state.orchestrate` through the worker with returns rendered by
  the existing job-detail machinery. Rejected: cramming runner
  args into the execution job form (different arg shape, runner
  versus local path, confusing validation).
- **Grant change ships with the code**: add `saltutil.kill_job`
  to the dev `salt-config/api.conf` execution list and document
  the production equivalent; `@runner` already covers
  orchestration.

## Recommended Approach
Three independent units, shippable in any order: groups, kill,
orchestrate. Groups first (it unlocks group targeting for the
other two), then kill, then orchestrate.

## Work Plan
1. **Groups.** Migration for `minion_groups`. Minions page section:
   create from checked rows, list with member counts, rename,
   edit members, delete with confirm. `group` target type in
   `TGT_TYPES` resolving to list in `launch()` and batch roster
   resolution. Tests: CRUD routes, unknown-member tolerance
   (stale IDs resolve out with a note), group target fires a list
   job, migration round-trip on SQLite.
2. **Job kill.** Detail page Kill button while a job is incomplete
   (operator and up, confirm inline like destructive functions):
   publishes `saltutil.kill_job` with the job's JID to the job's
   target, records a linked kill Job row, audit-logs both JIDs.
   Kill returns render in a small section on the detail page.
   Tests: kill publishes with correct fun/args/target (stubbed
   client counts calls), viewer/operator gating, missing-JID and
   already-complete guards, eauth-denial flash path.
3. **Orchestrate page.** `GET/POST /jobs/orchestrate` (operator and
   up): mods (required, validated as a dotted name), saltenv,
   test-mode toggle, inline pillar JSON with parse-error
   reporting. Async runner launch through the worker with sync
   fallback, returns synced into normal `Job`/`JobReturn` rows so
   history, detail, and audit work untouched. Tests: validation
   rejects bad mods and bad JSON, launch passes runner args
   through (stubbed), returns land in history.

## Validation Plan
- `.venv/bin/pytest -q` green after each unit; new files
  `tests/test_groups.py`, `tests/test_kill.py`,
  `tests/test_orchestrate.py`; existing migration test covers the
  new revision up and down.
- Live on the dev stack: group run against two dev minions,
  kill a `sleep`-style long job (`test.sleep`? use
  `cmd.run sleep 120` if available, else a slow highstate) and
  confirm per-minion kill returns, orchestrate the demo SLS and
  confirm returns in history.
- Highest-risk check: kill semantics. Prove with the stubbed
  client that kill targets exactly the job's original target with
  the job's JID, and document that silence from a minion means
  unknown, not dead.

## Risks / Rollback
- Stale group members (deleted minions) resolve out silently with
  a count note; groups never fail a fire.
- `saltutil.kill_job` needs the eauth grant on real masters;
  without it the button flashes the denial instead of failing
  silently (capability checklist names the grant).
- Orchestrate output shape differs from execution returns;
  detail rendering must tolerate missing `succeeded` summaries
  (already handled by the generic JSON fallback).
- Rollback: groups migration downgrades; kill and orchestrate are
  additive routes and templates.

## Open Questions
None. Group scoping, kill mechanism, and orchestrate shape were
all settled from workspace and installed-Salt evidence above.

## Grill decisions (Final)
Status: Final. Accepted by the owner.

Settled:
- G1 (Draft): groups ship full CRUD (create, rename, edit members,
  delete). Stale-member pruning needs the edit UI.

- G2 (Draft): kill is single-click with no retype confirm.
  Speed wins for runaway jobs; the audit row records it.

- G3 (Draft): orchestrate ships the inline pillar JSON box with
  parse-error reporting. Real states need pillar input.

Unresolved: none.
