# Reactor add wizard — plan and decision record (Final)

Topic: replace the inline two-field reactor add form with a guided,
stepped flow: event pattern → SLS file → review blast radius and
confirm, with an optional durability lever (record in `master.conf`).

## Status
Final. Scope accepted by the owner 2026-09-22. Implementation is a
separate request; accepting this record approves no code stage.

## Goal
An operator adding a reactor mapping gets guided past the three
ways the current form misleads: a mistyped event that never fires,
an SLS ref that points nowhere browsable, and a runtime-only add
that vanishes on restart. Each step validates server-side; the
review step states the blast radius (fires with master privileges,
fleet-wide, fanned out to every master pod) before anything runs.

## Success Criteria
- An operator clicking "Add reactor" walks event → SLS → review →
  confirmed result, with each step re-rendering its own validation
  errors and never losing already-entered values.
- Event step offers preset patterns from the bus families the events
  viewer already knows (`TAG_CHOICES`) plus a custom field under the
  existing `EVENT_RE` rule.
- SLS step lists files under `REACTOR_ROOTS` (bounded walk) plus a
  custom-ref field; custom refs outside the roots carry the same
  "listed but not browsable" note the table already uses.
- Review step shows event, SLS, pod fan-out count, and the
  master-privileges warning; confirm runs the existing runner
  fan-out and audit rows unchanged.
- Persistence offer (admin only): "also record in the `master.conf`
  reactor stanza" appends through the Master Config snapshot
  machinery (base-resourceVersion race refusal, YAML gate) and
  states that a restart is still required — saving never restarts.
- A viewer sees no wizard entry and forged step POSTs are rejected
  server-side, same role gates as today.
- Reactor-down masters produce the honest warning at review time
  ("mapping will not list until a reactor runs") instead of the
  silent no-op success observed 2026-09-22.
- `.venv/bin/python -m pytest tests/test_reactor.py -q` green, full
  suite green, `ruff check` / `ruff format --check` clean.

## Context And Current Facts
- Today: inline form on `/reactor/` (`POST /reactor/add`,
  `overstate_ui/reactor.py:300`, template
  `overstate_ui/templates/reactor.html:22`) — free-text event + SLS,
  operator-gated, fan-out to all pods, `reactor-add[:partial]`
  audit. Validation: `EVENT_RE`, `SLS_RE`, max lengths.
- SLS resolution helpers already exist: `sls_to_rel`, `_safe_join`,
  `nearest_tag` (`reactor.py:56`).
- Persistence machinery already exists: `masterconfig.save`
  (snapshot-first, stale-base refusal, YAML gate) and explicit-only
  `masterconfig.restart`. The wizard must reuse both, not duplicate.
- K8s PUT path fixed pair16 (`k8s.py` JSON content type); wizard
  persistence depends on it.
- Live finding 2026-09-22: `reactor.add` returns success while the
  reactor system is down and the mapping never lists. The wizard
  review step must surface liveness (from `_live_mappings`
  reachability) before confirm.

## Constraints And Non-goals
- No new Salt call shapes: confirm reuses the runner fan-out as-is.
- No eauth broadening; no SLS body editing (read-only stands).
- No wizard state in Redis or server session: steps carry forward
  via hidden form fields (stateless, bookmarkable, works when the
  worker/cache is down — same fallback posture as the rest of the app).
- Saving the stanza never restarts; restart stays an explicit
  second click per Master Config convention.
- Non-goals: editing existing mappings in place (delete + re-add),
  firing test events, multi-mapping batch add, Thorium.

## Key Decisions
- D1 (settled 2026-09-22): stepped wizard over an enriched single
  form. Owner chose the wizard.
- D2 (settled 2026-09-22): persistence offered in-flow (admin
  only), not runtime-only. Owner chose the offer.
- D3 (settled 2026-09-22): SLS picker from `REACTOR_ROOTS` plus a
  custom-ref field. Owner chose picker + custom.
- D4 (proposed): inline quick-add on `/reactor/` becomes an "Add
  reactor" button into the wizard; one add path, not two.
- D5 (proposed): liveness check at review (reachable pods vs
  `NOT_RUNNING`) blocks nothing but warns loudly; confirm stays
  allowed so the flow matches runner semantics.

## Work Plan
1. Skeleton: `GET /reactor/add` step router + hidden-field
   carry-over + per-step templates; role gates (operator+ entry,
   viewer 403).
2. Step 1 event: preset list from `TAG_CHOICES` + custom field,
   `EVENT_RE`/`MAX_EVENT_LEN` validation, values preserved.
3. Step 2 SLS: bounded `REACTOR_ROOTS` walk for the picker +
   custom field, `sls_to_rel` browsability note, `SLS_RE` check.
4. Step 3 review + confirm: blast-radius summary, liveness
   warning, confirm POST reuses runner fan-out + audit; divergent
   and partial flashes unchanged.
5. Persistence slice (admin only): stanza append via
   `masterconfig` snapshot machinery, restart-required note,
   `masterconfig-save` audit row; restart link, never auto.
6. Cutover + docs: replace inline form with entry button, update
   `docs/user.md`, tests per slice below.

## Validation Plan
- New tests in `tests/test_reactor.py` (stubbed runner, fake k8s
  transport): step validation errors preserve input; forged
  viewer POSTs 403; picker lists fixture files; custom unbrowsable
  ref warns; confirm fans out and audits; persistence appends the
  stanza and snapshots; reactor-down review warns.
- Full `.venv/bin/python -m pytest -q`; `ruff check`, `ruff format
  --check`.
- Manual against a live master: wizard add lists on re-render;
  persistence stanza survives a restart; denial surfaces as error.

## Risks
- Persistence without restart diverges live vs file (same known
  divergence as runner writes): mitigated by the restart-required
  note, the existing divergent banner, and export cross-link.
- Picker walk cost on large roots: cap file count and depth, fall
  back to custom-only with a note.
- Multi-pod skew mid-wizard (mapping changes between steps):
  review re-reads live state at render; confirm reports
  partial/divergent as today.

- D6 (settled 2026-09-22): SLS picker is a flat list of relative
  paths. Owner chose flat.
- D7 (settled 2026-09-22): presets are `TAG_CHOICES` plus recent
  custom events from audit rows. Owner chose extend.

- D8 (settled 2026-09-22): recents are the last 5 distinct
  `reactor-add` event patterns, presets excluded. Owner accepted
  the recommendation.

## Open Questions
None.
