## Goal

Implement Overstate v1: a boring-to-operate Salt control-plane UI (Flask + HTMX + Alpine + DaisyUI 5, Postgres, Redis, Podman-first) that proxies salt-api for keys, minions, jobs, states/conformity, schedules, and events, satisfying the 8-step acceptance in `PLAN.md`.

## Success Criteria

- A stranger can `podman compose up`, point Overstate at an existing master + salt-api, and complete all 8 acceptance steps: login, accept a pending key, see minion inventory with grains, run `test.ping` + dry-run highstate, watch the job land live and persist, toggle a schedule, see conformity, confirm salt-api health + audit row.
- Every mutating click produces a Salt JID; the UI never invents success.
- salt-api remains the only control plane; Postgres holds snapshots/returns, Redis holds cache/pub-sub only.

## Context And Current Facts

- Workspace root contains exactly one file: `PLAN.md` (~8KB). No code, tests, configs, or plan-directory conventions exist yet.
- `PLAN.md` pins the product contract: stack table, data-model sketch (`users`, `settings`, `minions`, `jobs`, `job_returns`, `audit_events`, `saved_jobs`, `watched_states`), sidenav/tabs structure, package name `overstate_ui`, env-only secrets, local-admin auth v1, dedicated `overstate` eauth user with tight execution list.
- Non-goals for v1 are explicit: no master-config editing, no SLS/pillar editor, no multi-user eauth mapping, no LDAP/OAuth, no syndic/salt-ssh/pkg workflows.
- Highest-risk integration is the salt-api boundary (eauth token lifecycle, wheel/local/runner coverage, event stream bridge to SSE) plus the returner path that gets job data into Postgres.

## Constraints And Non-goals

- Constraints: three containers (`overstate`, `postgres`, `redis`); master stays outside. gunicorn serves; SSE for live jobs. CSRF on every HTMX POST. argon2 local-admin hash, seed admin only when zero users exist. Never store the salt-api password in the settings table.
- Non-goals: everything listed under `PLAN.md` Non-goals (v1) plus v1.1/v2+ items (nodegroup targets, minion compare, schedule-add UI, SLS editor, multi-user roles). Schedule-add waits until list + toggle is solid.

## Key Decisions

- **App layout: single Flask package `overstate_ui` with blueprint-per-surface** (dashboard, minions, keys, jobs, states, schedules, events, settings, auth). Rejected: one flat `app.py` (will not survive 8 surfaces) and per-surface microservices (violates boring-to-operate).
- **Salt integration: one `salt_client` module owning eauth token + wheel/local/runner calls, faked in tests.** All blueprints call it; nothing else touches salt-api. Rejected: per-blueprint HTTP calls (token handling would diverge).
- **Job persistence: configure the master's existing postgres/pgjsonb returner against Overstate's DB; app reads `jobs`/`job_returns`, never the master's on-disk cache.** If the stock returner's schema fights the app's query needs, add a thin custom returner as a fallback, not a second store. Assumption: target masters can install/enable a returner.
- **Live updates: salt-api `/events` (or `salt_events`) → app-side bridge → Redis pub/sub → SSE endpoints consumed by HTMX.** Redis never stores job truth. Rejected: streaming raw event payloads to the browser (PLAN.md forbids it); server-side filtering stays in the bridge.
- **Inventory: `minions` table is a grains snapshot cache** refreshed on demand + periodic RQ job; Salt is source of truth. Rejected: treating Postgres as CMDB.
- **Migrations from day one: SQLAlchemy 2 + Alembic baseline in the first phase**, so later phases never hand-write schema changes.

## Recommended Approach

Build vertically in dependency order: scaffold + schema first, then the salt-api client (the riskiest seam, proven early against a real master), then read surfaces (keys, minions, dashboard), then write surfaces (jobs + live updates), then conformity/schedules/events, then ops polish and the full acceptance pass. Each phase ends with a real check against salt-api, not mocks alone. Server-rendered tables throughout; Alpine for widgets only.

## Work Plan

1. **Scaffold + ops skeleton.** Package `overstate_ui` (app factory, config from env), `Containerfile`, `compose.yml` (overstate/postgres/redis), quadlet example, Alembic baseline, gunicorn entrypoint, base Jinja layout with DaisyUI 5.7.x + Tailwind 4 + HTMX 2 + Alpine, login/logout + seeded local admin (argon2, rate-limited) + CSRF.
2. **Salt client + master status.** `salt_client` (eauth login/refresh as `overstate` user, wheel/local/runner wrappers, `/events` consumer), salt-api health probe (URL, token age, `@wheel`/`@runner` checks), dashboard shell (counts from live calls, last failures from DB).
3. **Persistence.** Tables per sketch with `jsonb` grains/returns and indexes on `jid`, `minion_id`, `fun`, `alter_time`; master returner wiring docs; `audit_events` write path for every mutating action.
4. **Keys + Minions.** Key tabs (Pending/Accepted/Rejected/Denied) with fingerprint + accept/reject/delete (never auto-accept); minion table (server-side search/filter/paginate, grain columns) + detail tabs (Overview/States/Jobs/Schedule/Pillar-ro/Beacons-ro) + presence (`test.ping`, last-seen, accepted-but-dead) + on-demand + RQ inventory refresh.
5. **Jobs + live.** Runner form (glob/list/grain/compound/nodegroup target, fun + args, sync/async, `test=True`), first-class buttons (`test.ping`, `state.apply`, highstate, dry-run), Running/History/Saved tabs, foldable highstate detail (summary first), Redis pub/sub → SSE live updates, optional kill/signal, `saved_jobs` CRUD.
6. **Conformity + schedules + events.** Watched states + per-minion conformity (ok/drifted/unknown/unreachable) from last highstate; schedule list + enable/disable/delete; filtered event viewer (`salt/job`, `salt/auth`, minion starts); settings DB page (defaults, page size, theme, columns, watched states, retention display); CSV export + output formatters (highstate-pretty/JSON/raw).

## Validation Plan

- `pytest` per phase (salt client faked; blueprint POST tests assert a JID is created and an audit row written, never bare success text).
- `podman compose up` + seed admin login against a real master with salt-api; walk the 8-step acceptance in `PLAN.md` verbatim.
- Live-job check: fire `test.ping`, watch SSE update arrive, confirm the return persists in History after refresh.
- Negative checks: expired eauth token recovers; rejected-key minion shows accepted-but-dead correctly; kill/signal degrades cleanly when the master disallows it.
- Highest-risk step: phase 2 salt-api probe + phase 3 returner round-trip against the real master. If returns do not land in Postgres, nothing downstream is verifiable.

## Risks / Rollback

- **salt-api version skew** (Tumbleweed master vs Fedora minions): mitigate by showing versions on the dashboard; no rollback needed (read-only display).
- **Returner unavailable on target master:** fallback to thin custom returner; rollback is config-only (point master back at its job cache, Overstate degrades to live-only history).
- **Event-volume overload:** server-side filtering + caps on rendered rows; SSE degrades to poll.
- Safe to abandon per phase: no production data exists yet; `alembic downgrade` per migration, `podman compose down -v` resets local state.

## Open Questions

None. `PLAN.md` answers scope, stack, and acceptance; reversible defaults (blueprint layout, SSE-via-Redis, stock returner first) are stated above as decisions.

## Grill Log (Final — accepted by owner)

- **D1 — return path: SETTLED (stock-first).** Master uses stock `postgres`/`pgjsonb` returner into Overstate's DB; thin custom returner only if schema fights queries. Accepted in grill session.
- **D2 — event-bridge shape: SETTLED (filtered bridge + SSE).** App-side consumer on salt-api `/events`, server-side filtering in the bridge, Redis pub/sub fan-out to per-job SSE endpoints. Polling only as degradation path. Accepted in grill session.
- **D3 — eauth scope: SETTLED (tight allowlist).** `overstate` eauth user gets `@wheel` + `@runner` plus execution allowlist: `test.ping`, `state.apply`, `state.highstate`, `state.show_highstate`/`show_sls`, `schedule.*`, `saltutil.sync_all`, `saltutil.refresh_pillar`, `grains.items`, `pillar.items`, read-only `sys.*`. New functions fail closed at eauth. Accepted in grill session.
