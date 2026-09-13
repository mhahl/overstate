# Pre-apply SLS preview — plan (Draft)

Show what `state.apply` will enforce, on the review page, before
it runs: render the requested SLS files via `state.show_sls`
against one target minion and display the state list with
foldable raw data next to the existing target/function/match
summary.

## Status
Final. Accepted by the owner 2026-09-13.

## Goal
No operator fires `state.apply` blind: the review page shows the
states about to be enforced, rendered for a real target minion,
before the Fire button is touched.

## Success Criteria
- Submitting `state.apply <sls…>` with real (non-test) mode
  lands on the existing review page with an added preview card:
  which minion rendered it, one section per requested SLS with
  its state IDs, and foldable raw state data.
- When rendering fails (unknown SLS, down minion, denial), the
  page shows a plain note instead of a preview — and the Fire
  button stays available. Preview never blocks firing.
- Non-state functions and `test=True` runs are unchanged (no
  preview section, same flow as today).
- `.venv/bin/pytest -q` green; no grant changes.

## Context And Current Facts
- Confirm flow: `run()` renders `job_confirm.html` for
  `CONFIRM_FUNS` in real mode (`jobs.py:358-370`); the page
  shows function, target, matched minions, mode (`job_confirm.html:7-34`)
  and posts back with `confirmed=yes`. `test=True` state runs
  skip review entirely (`jobs.py:85-88`).
- `state.show_sls` is granted (`salt-config/api.conf:18`,
  mirrored in `docs/deployment.md:93`) and nothing calls it.
- Live contract, verified on the dev master this run:
  `state.show_sls <sls>` renders against the targeted minion
  (default saltenv `base`, no topfiles); return shape is
  `{minion: {state-id: {module: [...], __sls__, __env__}}`
  (probed with the `demo` SLS on `dev-minion-01`).
- `resolve_batch_roster` (`jobs.py:403-421`) already pins the
  matched list for list/glob targets in the same view; other
  target types get `matched=None` with an explanatory note
  (`job_confirm.html:19-20`).
- Minion pick precedent: `ping_target()` (`dashboard.py:78-81`)
  returns one snapshot minion for single-minion reads.

## Constraints And Non-goals
- Review page only: no new page, no nav change (nav freeze
  holds), no new mutations or tables, no grant changes.
- Pillar stays read-only; preview renders with the minion's
  real pillar, never a custom override (unlike orchestrate's
  inline pillar box).
- Non-goals: highstate preview (covered by dry-run),
  `state.show_top` rendering, saltenv picker (base only, noted
  on the card), `test=True` flow changes.

## Key Decisions
- **Preview on the review card, per requested SLS.** One
  `state.show_sls` call per SLS name from the form args,
  rendered against a single minion, merged into a preview card
  between the summary and the Fire form. State IDs listed up
  front; raw data in per-SLS collapsibles reusing the
  job-detail foldable styling. Rejected: single comma-joined
  call (join syntax unverified over salt-api) and raw-only
  dump (state IDs answer "what will it touch" at a glance).
- **Preview minion: first matched, else snapshot first.**
  list/glob targets render on `matched[0]`; grain/compound/
  nodegroup/group targets render on `ping_target()` with the
  minion named on the card. Rejected: fleet-wide render
  (slow, redundant — same fileserver content) and blocking
  when unresolvable (note instead).
- **Same transport as the job, failure becomes a note.**
  `via=ssh` renders over the ssh path against the same
  minion; any SaltApiError (or empty result) renders
  "Preview unavailable: <reason>" and firing stays enabled.
  Rejected: silent omission (operator should know the
  preview is missing) and hard failure (preview is advisory).
- **Worker with sync fallback, like Unit 1.** New
  `show_sls_now`/`show_sls_task` in `tasks.py`; the view
  waits briefly and degrades to the note on pending/error.
  Rejected: bare synchronous render (request-thread risk on
  big SLS trees).
- **Zero grants ship.** `state.show_sls` is already granted;
  denial surfaces as the unavailable-note, not a plan change.

## Recommended Approach
Single unit: helper, view wiring, template card, tests, one
docs line. No sequencing boundary inside it is worth a split.

## Work Plan
1. **Render helper.** `show_sls_now(client, minion, sls, via)`
   plus `show_sls_task` in `tasks.py`: per-SLS calls merged
   to `{sls: {state-id: ...}}`, tolerant of string/dict/None
   returns; never raises past the view (view catches).
2. **View wiring.** `run()`'s confirm branch: pick the preview
   minion (matched[0] or `ping_target()`), queue/wait the
   task, pass `preview` (`{sls, states}` list or
   `{unavailable: reason}`) plus `preview_minion` into
   `job_confirm.html`. Only for `fun == "state.apply"` with
   sls args in real mode; everything else renders exactly as
   today.
3. **Template card.** Preview card between summary and form:
   header names the rendering minion and base saltenv; per
   SLS the state-ID list plus folded raw; unavailable-note
   variant. Viewer never reaches this page (confirm is
   operator-gated via `run()`), so no new RBAC surface.
4. **Tests.** New `tests/test_sls_preview.py` (stubbed
   `state.show_sls`): preview renders state IDs with minion
   label; denial renders the note with Fire intact; `test.ping`
   and `test=True` flows show no preview section.
5. **Docs.** One paragraph in `docs/user.md` Jobs/confirm
   area: what the preview shows, which minion renders it,
   base saltenv, advisory-only.

## Validation Plan
- `.venv/bin/pytest -q` green; new `tests/test_sls_preview.py`;
  no migration involved.
- Live on the dev stack: review `state.apply demo` — preview
  lists the demo file state rendered on the dev minion;
  review a bogus SLS name — note appears, Fire still works;
  fire the demo apply and confirm the enforced states match
  the preview.
- Highest-risk check: multi-SLS args and the ssh path. Prove
  `state.apply a b` renders both sections and an ssh-targeted
  review degrades to the note (or renders, documented either
  way).

## Risks / Rollback
- SLS render can be slow on big trees; worker wait is bounded
  and pending degrades to the note — the review page never
  hangs past today's behavior plus the wait window.
- Render context is one minion's pillar/grains; per-minion
  differences (pillar-targeted states) are noted on the card,
  not solved.
- Rollback: additive helper, view branch, and template card;
  revert the diff.

## Open Questions
None. Call shape, return shape, grants, and minion-pick
sources were all verified above; remaining transport risk
(ssh render) is a validation step with a designed fallback.

## Sources
None. No external claims inform this plan; the `show_sls`
contract was verified against the live dev master this run
(sys.doc plus a real `demo` render), and grants are already
in `salt-config/api.conf`.

## Grill decisions (Final)
- G1 (Draft): preview renders on the first matched minion,
  falling back to the snapshot-first minion with its name on
  the card. Settled by the owner 2026-09-13 (accepted the
  recommendation).
- G2 (Draft): advisory preview only — a failed render shows
  "Preview unavailable: reason" and Fire stays enabled; unknown
  SLS names never block firing. Settled by the owner
  2026-09-13 (accepted the recommendation).

## Unresolved
None. Accepted by the owner 2026-09-13.
