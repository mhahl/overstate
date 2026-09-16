# Reactor management — plan and decision record (Draft)

Topic: reactor visibility and management in Overstate. The reactor
mapping (event tag → SLS) lives in master config and is read and
changed through salt-api's `reactor` runner; reactor SLS bodies are
read from a reactor directory on disk, never edited from the UI.

## Status
Final. Accepted by the owner 2026-09-15.

## Goal
Give operators reactor visibility and the safe management levers:
a **Reactor** page lists the live event→SLS mapping as reported by
the master, each SLS viewable syntax-highlighted, with add and
delete of mappings round-tripping through salt-api with audit rows.
Viewers get read-only access.

## Success Criteria
- An operator opening `/reactor/` sees each configured event pattern
  with its SLS files, matching `salt-run reactor.list`; an empty
  reactor set shows an explicit empty state.
- Mutations (if in scope — open Q1) round-trip through salt-api,
  flash the Salt-confirmed result, and write an audit row.
- A viewer sees the mapping but no mutation controls, and forged
  POSTs are rejected server-side.
- A down or denying master produces an inline error, never a fake
  success.
- `.venv/bin/pytest -q` stays green; `ruff check` / `ruff format
  --check` clean; docs updated.

## Context And Current Facts
- Salt ships `salt.runners.reactor`: `list` (list configured
  reactors), `add(event, reactors)`, `delete(event)` (inspected
  2026-09-15,
  https://docs.saltproject.io/en/latest/ref/runners/all/salt.runners.reactor.html).
  These are `@runner` calls, reachable via the existing
  `SaltClient.runner()` (`overstate_ui/salt_client.py:166`).
- The shipped eauth in `salt-config/api.conf` already grants
  `"@runner"`, so no eauth change is expected (to verify against a
  narrower production master).
- Closest in-repo patterns: `schedules.py` (list + add +
  enable/disable/delete through salt-api, `SCHEDULE_ACTIONS` map,
  `log_event`, `roles_required("operator")`, query-string sort/dir)
  and `files.py` (read-only SLS browser: `safe_join`,
  `highlight_yaml`, `MAX_BYTES` guard).
- `events.py` renders the filtered bus viewer; a reactor row can
  deep-link to `/events/` with the nearest tag family preselected.
- Reactor SLS files live in a reactor directory (e.g.
  `/srv/reactor`), separate from `FILE_ROOTS`; the UI needs a new
  `REACTOR_ROOTS` env-backed config for reading bodies.
- `docs/developer.md`: the UI never invents success — a mutating
  click reports what Salt returned.

## Constraints And Non-goals
- SLS file contents are never edited from the UI (same as Files).
- No new Salt call shapes: reuse `SaltClient.runner()`.
- No eauth broadening: a denying master surfaces a denial.
- Non-goals (proposed): firing test events at the bus, editing
  master config files directly, Thorium, non-`base` saltenvs.

## Key Decisions
- D1 (settled 2026-09-15): full management in the first slice —
  mapping list + SLS view + add + delete. Owner chose "full mgmt".
- D2 (settled 2026-09-15): SLS bodies stay read-only; the UI
  manages the event→SLS mapping only. Owner chose read-only.
- D3 (settled 2026-09-15): delete uses a simple confirm page
  (one click, server-side). Owner chose "confirmation". (If typed
  confirmation was meant instead, say so before acceptance.)
- D4 (settled): mapping source is `runner reactor.list`, not
  master-config file reads (app has no master-filesystem access).
- D5 (settled): SLS bodies read-only via new `REACTOR_ROOTS`;
  mappings outside it still list with "source not browsable here".
- Known divergence (documented, not hidden): `reactor.add/delete`
  writes master reactor config, not the git checkout — unlike
  file-roots, the mapping is master-config state. The page and
  `docs/deployment.md` will say so.

## Work Plan (staged on D1)
1. Read path: `overstate_ui/reactor.py` (`GET /reactor/`, `GET
   /reactor/view`), `REACTOR_ROOTS` in `config.py`, blueprint +
   nav + palette + `reactor.html`.
2. Add mapping (only if D1 = full): `POST /reactor/add`,
   operator-only, validated, logged.
3. Delete mapping (only if D1 = full): confirm page + typed-tag
   check, `POST` runs `runner("reactor.delete", …)`, logged.
4. Cross-links + docs: `/events/` deep links, deployment/user docs.
5. Tests: new `tests/test_reactor.py` (stubbed runner; no live
   master needed).

## Validation Plan
- `.venv/bin/python -m pytest tests/test_reactor.py -q` per slice,
  then full `.venv/bin/python -m pytest -q`.
- `ruff check overstate_ui tests`, `ruff format --check`.
- Manual against dev master: page matches `salt-run reactor.list`;
  denial surfaces as error, never fake success.

## Risks
- Master-config writes bypass git review (only if D1 = full):
  mitigated by confirm gating, audit log (audit entry records the
  SLS so a delete is re-addable), and docs.
- `reactor.list` shape variance: normalize with a raw fallback,
  same as `parse_schedule_list`.

## Open Questions
- Q1: first-slice scope — settled: full manage (2026-09-15).
- Q2: SLS editing — settled: read-only (2026-09-15).
- Q3: delete confirm style — settled: simple confirm page (2026-09-15).
- Q4: export shape — settled: YAML-block export (2026-09-15).

## Amendment 2026-09-15 — export mapping for git
Owner request: support generating a committable artifact from the
live `reactor.list` mapping, for users who want the reactor config
in their git repo.
- D6 (settled 2026-09-15): export renders the live mapping as a
  master-config YAML block (`reactor:` stanza), copyable text plus
  download. Read-only generation; the app never writes git. Owner
  chose the recommendation.
- Work Plan gains a slice: `GET /reactor/export` (operator or
  viewer? — recommend viewer-visible, read-only) rendering the
  block as text + `Content-Disposition: attachment` download;
  unit test pins the rendered shape for a stubbed mapping.
