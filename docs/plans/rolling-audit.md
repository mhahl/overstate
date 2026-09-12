## Goal
Make fleet-wide jobs safe and make the audit trail visible: rolling
execution with failure gates on the job form, plus a filterable audit
page. Both ship together because every gated wave writes audit rows
the new page displays.

## Success Criteria
- An operator can run a job across a target in waves (count or
  percent per wave) with a stop-after-N-failures gate, and watch
  each wave land on the job detail page.
- A batch can be cancelled between waves; no new Salt calls start
  after cancel.
- Any user can open the audit page, filter by user and action, page
  through history, and jump from a row to its job.
- Salt-ssh and sync runs keep working unchanged; ungated async runs
  behave exactly as today.

## Context And Current Facts
- `launch()` (`overstate_ui/jobs.py:144`) fires exactly one Salt job
  per run and records one `Job` row; no `batch` concept exists
  anywhere in code or templates.
- `SaltClient.local()` accepts a `kwarg` dict forwarded to salt-api,
  and the RQ worker (`overstate_ui/tasks.py`, `worker.py`) already
  runs long Salt work with `queue_or_none`/`wait_for` plus sync
  fallback.
- `sync_job()` copies returner rows into `jobs`/`job_returns` and
  marks complete heuristically; `JobReturn.success` is the per-minion
  verdict the gate reads.
- `AuditEvent` (`overstate_ui/models.py:90`) stores user, action,
  optional jid, and timestamp with an index; `log_event` callers
  cover jobs, keys, schedules, users, and rotation. No UI reads the
  table.
- The job form (`templates/job_new.html`) posts tgt, tgt_type, fun,
  args, mode, via, confirm, and save_as.

## Constraints And Non-goals
- No Salt-master writes, no new broker, no new auth machinery.
- Waves reuse existing primitives only: `local` publish, returner
  tables, `sync_job`, RQ worker, `log_event`.
- Salt-ssh stays single-shot (no JID, no returns to gate on).
- No per-wave approval prompts; cancel is the only intervention.
- Out of scope: Salt-native `batch` kwarg (one opaque JID, no
  stop), scheduled batches, wave retry policies.

## Key Decisions
- **App-driven waves, not Salt-native batch.** The run splits the
  resolved target roster into ordered list-target waves; each wave
  is one normal async publish whose returns the gate evaluates
  before the next wave starts. Rejected: salt-api `batch`, which
  runs to completion with no stop and one opaque JID.
- **One parent Job row plus one child row per wave.** Children link
  via a new nullable `jobs.batch_group` column (Alembic revision);
  the parent carries the gate config and final verdict. Rejected:
  audit-only grouping (unqueryable) and single-row-per-batch
  (loses per-wave returns).
- **Cancel via Redis flag, not DB polling.** The worker checks
  `salt:batch:<group>:cancel` between waves; the stop button sets
  it with a TTL. Rejected: DB flag column (extra migration and
  polling for a transient signal).
- **Audit page is read-only for every logged-in role.** Viewers see
  everything else in the app; audit is consistent with that.
  Rejected: operator-and-up (splits a read surface for no
  sensitivity gain; usernames and actions are already visible).
- **Defaults: 25% waves, stop after first failure.** Safe out of the
  box for destructive presets; one field change relaxes either.
  Flagged as assumptions, adjustable at approval.

## Recommended Approach
Two units, audit first (smaller, independent), then rolling
execution. Rolling execution reuses the worker, the returner sync,
and the audit writer; nothing new may invent success.

## Work Plan
1. **Audit page.** New `audit` blueprint in `overstate_ui/audit.py`
   (today logic-only): `GET /audit/` with `user` and `action`
   substring filters, newest-first pagination honoring the
   `page_size` setting, JID cells linking to job detail when set.
   Sidenav entry under Observe. Tests: seeded rows render, filters
   narrow, pagination bounds hold, viewer role reaches the page,
   JID links resolve.
2. **Rolling execution with failure gates.**
   - Migration: nullable `jobs.batch_group` string plus index.
   - `tasks.run_wave_batch(group, waves, fun, args, gate)`: loop
     publish (list target, async) → wait/poll returns via
     `sync_job` → count failures → stop early when the gate trips
     or the cancel flag appears; write parent/child `Job` rows and
     `log_event` per wave and on stop/cancel/complete.
   - Job form: batch mode (off / count / percent), wave size,
     stop-after-N-failures; destructive confirm keeps working by
     confirming the full target once. Saved jobs store the gate
     config alongside fun/tgt/args.
   - Detail page: wave list with per-wave JID links, gate status
     banner (running / stopped-by-gate / cancelled / complete),
     cancel button while running.
   - Tests: roster splitting (count and percent, remainder wave),
     gate trips at the threshold, cancel flag stops the loop with
     no further publish (stubbed client counts calls), saved-job
     round-trip keeps gate config, migration upgrades and
     downgrades on SQLite.

## Validation Plan
- `.venv/bin/pytest -q` green after each unit; new files
  `tests/test_audit_page.py` and `tests/test_batches.py` plus the
  migration round-trip.
- Live on the dev stack: gated run against the dev minion with a
  forced failure (e.g. `test.fail` or unknown function) stops after
  wave one with the banner and audit rows; cancel mid-batch starts
  no further waves (worker log shows one publish).
- Highest-risk check: the wave loop's stop conditions. Prove gate
  trip, cancel, and all-complete paths with the stubbed client
  before touching the live stack.

## Risks / Rollback
- Roster resolution races key changes mid-batch; waves pin the
  minion list at start and the detail page shows the pinned count.
- `sync_job` completion is heuristic; a wave with zero returns
  waits out the timeout rather than hanging forever.
- Worker loss mid-batch leaves the parent row running; on next
  dashboard load a sweeper marks stale running batches unknown.
  Document, don't auto-resolve.
- Rollback: unit 1 is additive (new blueprint, no migration);
  unit 2's migration downgrades cleanly and old code ignores the
  column.

## Open Questions
None. Batch size and failure defaults are assumptions above.
