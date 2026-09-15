## Status

Draft — grill in progress (target confirmed 2026-09-16). Nothing here is accepted until you explicitly approve the settled contract.

## Grill decisions (Draft)

- J1 (Draft): module docs live in a sticky right rail beside the form. Settled by the owner 2026-09-16 (accepted the recommendation).
- J2 (Draft): compact by tightening density, every section stays visible. Settled by the owner 2026-09-16 (accepted the recommendation).
- J3 (Draft): muted "Loading docs…" line while the fragment fetches. Settled by the owner 2026-09-16 (accepted the recommendation).

## Scope contract (accepted)

Boundary: this decision record only. Out of scope: runtime code, tests, commits. Owner reply `accept` 2026-09-16 (this session); record stays Draft. Later stages need their own interview; execution words never widen this boundary.

## Goal

Modernise the job run page (`/jobs/new`) so firing a job takes less
scrolling and reading module docs never moves the form: docs live in
a sticky side rail with its own scroll, the form stays put on every
viewport.

## Success Criteria

- On a desktop viewport the whole run form (target through Fire)
  fits with noticeably less scrolling than today, and the module
  docs panel can load, grow, or clear without shifting any form
  field by a single pixel.
- On mobile the page is one column, docs render after the form, and
  nothing overlaps or requires horizontal scrolling.
- Every current behavior still works: preset links, op-library
  search filter, function combobox (keyboard included), batch
  show/hide, bulk prefill, validation-error echo, viewer read-only
  state, and the fun-doc fragment route contract.
- `.venv/bin/pytest -q` green; ruff check/format clean on touched
  files.

## Context And Current Facts

- Page is `overstate_ui/templates/job_new.html` (256 lines, template
  + inline script): header, bulk alerts, a full-width operation
  library collapse, then one long form card (target, function, args,
  mode/transport/save, batch, Fire).
- Docs slot `#fun-doc` sits in normal flow inside the Function
  section (`job_new.html:87`); `loadFunDoc()` drops the
  `/jobs/fun-doc` fragment into it, pushing args and everything
  below down. Fragment is `overstate_ui/templates/_fun_doc.html`;
  route is `jobs.fun_doc` (`overstate_ui/jobs.py:280-292`),
  viewer-readable, bad names rejected 400
  (`tests/test_function_browser.py:98-137`).
- All docs JS is id-addressed (`job-fun`, `fun-list`, `fun-doc`);
  nothing about the slot's position is structural to the script.
- Tests pin `op-library-toggle`, preset echo, combobox contents —
  not the docs slot position or the section order
  (`tests/test_jobs.py:485-488`).
- Base content column is `max-w-7xl`, leaving room for a rail on
  `lg` screens; the app already uses `lg:sticky` rails nowhere,
  but DaisyUI `card` + Tailwind sticky utilities are the house
  pattern (e.g. detail pages).

## Constraints And Non-goals

- Layout and template work only. No route, validation, preset,
  RBAC, or salt-api changes; no new endpoints or query params.
- JS changes limited to a docs loading line and at most an
  empty-state line — no combobox, filter, batch, or submit logic
  touched.
- daisyUI component conventions and the wireframe theme stay;
  no new palette, no new dependency, no icon set change.
- Non-goals: dry-run flow, bulk-roster UX, function index
  performance, combobox redesign, mobile bottom action bar.

## Key Decisions

- **Docs move to a sticky right rail, not a drawer or modal.**
  A rail keeps the docs visible *while* typing args — the actual
  job-firing workflow — with zero focus management. A drawer
  would hide the form it documents and need focus trapping;
  rejected.
- **Rail sits after the form in the DOM, shown right on `lg`.**
  DOM order (form, then rail) means docs popping in can never
  push a form field on any viewport; on `lg` the rail is
  `sticky top-20` with its own `max-h + overflow-auto`. On
  mobile it stacks after Fire, so docs grow the page end only.
- **Keep the operation library collapse where it is.**
  It is the entry point operators know, preset links and its
  filter are tested, and moving it into the rail would cramp
  the card grid. Tighten its density instead.
- **Compact by tightening, not by hiding sections.**
  Reduce section padding, shrink the examples line, densify
  library cards — every control stays one glance away with no
  new interaction to learn. An accordion-per-section step
  wizard was considered and rejected: more clicks, more state,
  no tested demand.
- **Loading line while docs fetch.**
  `loadFunDoc` writes "Loading docs…" into the slot before
  fetch so the rail never flashes empty, then replaces it.
  One line, same code path, no new failure mode (catch
  already blanks on error).

## Recommended Approach

A template-level restructure of `job_new.html` plus the one-line
JS loading state. Same DOM ids, same endpoints, same tests plus
one new assertion that the docs slot lives in the rail. Ship as
one reviewable unit — there is no safe halfway split of a layout.

## Work Plan

1. **Grid + rail skeleton.** Wrap library + form and the new
   `<aside>` in `grid gap-4 lg:grid-cols-[minmax(0,1fr)_22rem]`;
   aside is `lg:sticky lg:top-20 self-start`, placed after the
   form in the DOM. Move the `#fun-doc` div (with its
   `data-doc-*` attributes) into a "Module docs" card in the
   aside with an empty-state line. Keep every id, name, route,
   and role gate identical.
2. **Density pass.** Section padding `py-5` → `py-4`, library
   card grid to tighter gaps, examples line to a single
   short line, header row slimmer. No control removed or
   reordered.
3. **Docs loading line.** In `loadFunDoc`, set the slot to a
   muted "Loading docs…" line before `fetch`; keep the
   existing error path. Rail panel gets `max-h-[60vh]
   overflow-auto` so long `sys.doc` output scrolls inside
   itself.
4. **Regression test.** Extend the run-page tests: `#fun-doc`
   renders inside `<aside>`, the form keeps `job-fun`,
   `op-library-toggle`, and the Fire action. Existing
   preset/combobox/batch tests stay green untouched.

## Validation Plan

- `.venv/bin/pytest -q` green; focused: `tests/test_jobs.py`,
  `tests/test_function_browser.py` (fragment contract frozen).
- Render `/jobs/new` and `/jobs/new?preset=highstate-dry`:
  confirm library, combobox, docs slot, batch toggles, Fire.
- Desktop check: type a function, watch docs load in the rail
  while target/args fields keep their pixel positions; long
  doc output scrolls inside the rail.
- Mobile-width check: single column, no horizontal scroll,
  docs after the form.
- Keyboard check: combobox arrows/Enter/Escape still drive
  selection and docs load.
- Highest-risk check: the preset + validation-echo path
  (`_new_url` round-trip) renders identically, since the
  form fields keep names and order — prove with a bad submit
  that values echo back.

## Risks / Rollback

- Sticky rail overlapping content on odd viewports:
  mitigated by standard `lg:` gating and `self-start`;
  falls back to plain stacking below `lg`.
- Combobox dropdown (`absolute` in the Function section)
  clipping inside the grid column: the column is
  `minmax(0,1fr)` with no overflow hidden, same as today.
- Rollback is one file revert (`job_new.html`) plus the
  one-line JS inside it; no migration, no route to unwind.

## Open Questions

None. Rail-vs-drawer and compact-vs-wizard were decided above
as reversible UI calls with reasons; everything else is
workspace fact.

## Sources

None. No external claims inform this plan; all evidence is
workspace code, tests, and docs cited above.
