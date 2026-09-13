# Beacon support — plan and decision record (Draft)

Topic: beacon visibility plus runtime toggles in Overstate. Beacon
definitions live in pillar (git-managed, read-only to the app); the
minion-detail Beacons tab lists them with their source and offers
enable / disable toggles only.

## Status
Final. Accepted by the owner 2026-09-13.

## Goal
Give operators beacon visibility and the one safe runtime lever:
the minion-detail **Beacons** tab lists pillar-defined beacons with
their config and offers enable / disable toggles. Definitions are
never edited from the UI.

## Success Criteria
- An operator opening a minion's Beacons tab sees each configured
  beacon with its config and enabled state, not a raw JSON blob; an
  empty beacon set shows an explicit empty state.
- An operator can enable or disable a beacon from that tab; the
  action round-trips through salt-api, flashes the Salt-confirmed
  result, and writes an audit row.
- A viewer sees the same list but no toggle controls, and forged
  POSTs are rejected server-side.
- A down or denying minion produces an inline error, never a fake
  success.
- `.venv/bin/pytest -q` stays green; `docs/user.md` describes the
  new controls.

## Context And Current Facts
- `overstate_ui/minions.py:20` lists `DETAIL_TABS` including
  `"beacons"`; `minions.py:350-351` fetches the tab with a single
  live call `client.local(mid, "beacons.list")[0].get(mid)` and
  `templates/minion_detail.html:130` dumps whatever it returns as
  JSON. That is the entire beacon support today.
- The schedules feature is the template to copy:
  `overstate_ui/schedules.py` (list with `return_yaml=False`,
  `parse_schedule_list`, `SCHEDULE_ACTIONS` map, `log_event`,
  `roles_required("operator")`) plus
  `templates/schedules.html` (per-entry enable/disable table).
  Beacons get the read plus toggle half of that pattern; the
  add/modify/delete half does not apply (definitions live in
  pillar, which Overstate never writes).
- `salt-config/api.conf` grants `schedule.*` but nothing for
  beacons, so `beacons.list` and both toggles will be denied until
  the narrow grant ships with the code.
- `tests/test_keys_minions.py:46-48` stubs `beacons.list`
  returning `{}`; no test exercises beacon toggles, parsing, or
  gating.
- `docs/user.md:99` documents "Beacons. Beacon configuration,
  read-only." — that line must be updated to list-plus-toggle.
- Salt 3006 `salt.modules.beacons` (inspected 2026-09-13,
  https://docs.saltproject.io/en/3006/ref/modules/all/salt.modules.beacons.html):
  `list_` exposed as `beacons.list` with `return_yaml` /
  `include_pillar` / `include_opts`, `enable_beacon(name)` /
  `disable_beacon(name)` for single-beacon toggles
  (`enable`/`disable` hit every beacon and stay out of the UI).

## Constraints And Non-goals
- Side navigation stays exactly as-is (project constraint,
  2026-09-12): no new nav entry, no reorder. Beacon controls live
  inside the existing minion-detail Beacons tab (and its POST
  routes); there is no new top-level `/beacons` page.
- `salt-api` remains the only control plane; no direct
  minion-config file writes, no new broker or tables, no
  beacon-event streaming in this round.
- Pillar stays read-only: no add, modify, delete, or save of
  beacon definitions from Overstate (grill B0). No JSON config
  form ships.
- Non-goals: `beacons.list_available` browser, beacon reactor
  wiring, fleet-wide beacon search, global enable/disable-all,
  `reset`.

## Key Decisions (grill-settled, Draft)
- **B0 — Beacons live in pillar.** Git-managed definitions;
  Overstate never writes them.
- **B1 — Tab lists pillar beacons plus enable/disable toggles**
  (runtime state only).
- **B2 — Narrow grant**: `beacons.list`,
  `beacons.enable_beacon`, `beacons.disable_beacon` in
  `salt-config/api.conf`. No `beacons.*`.
- **B3 — Entries carry a `pillar` source badge; no
  where-to-edit pointer.** The tab does not claim to know which
  pillar SLS defines what.
- **Read path**: `beacons.list` with `return_yaml=False`, a
  `parse_beacon_list` helper mirroring `parse_schedule_list`,
  raw-string fallback when unparseable.
- **RBAC and audit follow schedules**: reads `login_required`;
  toggles `roles_required("operator")`, viewer-gated controls,
  `beacon-enable:<name>` / `beacon-disable:<name>` audit rows.

## Work Plan
1. Grant: the three narrow beacon functions in
   `salt-config/api.conf`.
2. Read path: `return_yaml=False` list, `parse_beacon_list`,
   entries/raw/error tab context in `overstate_ui/minions.py`.
3. Toggle routes: enable/disable POSTs (operator-gated, CSRF,
   audit-logged); SaltApiError flashes, never fake success.
4. Template: entries table with `pillar` badges and toggle
   buttons in the Beacons branch of `minion_detail.html`;
   viewer-gated forms; empty and error states.
5. Tests: new `tests/test_beacons.py` (stubbed client: list
   renders entries, toggles publish correct fun/args plus audit
   rows, viewer POSTs get 403, denial path flashes).
6. Docs: `docs/user.md` Beacons bullet and section; grant note
   in deploy docs.

## Validation Plan
- `.venv/bin/pytest -q` green; `tests/test_beacons.py` covers
  list, both toggles, RBAC gating, denial flash.
- Live on the dev stack: toggle a real beacon off and on;
  flashes match Salt returns; audit rows exist.
- Highest-risk check: whether a toggle on a pillar-sourced
  beacon survives a pillar refresh. Prove on the dev stack; if
  it reverts, the tab must say so instead of implying
  persistence.

## Risks / Rollback
- Toggle persistence across pillar refresh is unverified (see
  validation); result becomes a documented caveat, not a silent
  behavior.
- Beacon payload shapes vary by module and Salt version; raw
  fallback instead of crashes.
- Missing grant flashes denials; checklist names the three
  functions.
- Rollback: revert the diff; tab falls back to today's dump. No
  migration.

## Grill decisions (Final)
- B0 (Draft): beacons live in pillar, not minion-local config.
  Settled by the owner 2026-09-13.
- B1 (Draft): tab lists pillar beacons plus enable/disable
  toggles (runtime state only, no definition edits). Settled by
  the owner 2026-09-13 (accepted the recommendation).
- B2 (Draft): narrow grant — `beacons.list`,
  `beacons.enable_beacon`, `beacons.disable_beacon` only.
  Settled by the owner 2026-09-13 (accepted the
  recommendation).
- B3 (Draft): `pillar` source badge on entries, no
  where-to-edit pointer. Settled by the owner 2026-09-13.

## Unresolved
None. Decision tree exhausted; awaiting explicit acceptance to
mark Final.
