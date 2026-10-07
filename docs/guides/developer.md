# Overstate developer guide

This is a control-plane UI, not a Salt library. The master does the
work through salt-api. The app renders pages, stores history, and
never invents success: a mutating click produces a JID, and the UI
reports what Salt returned for that JID.

## Layout

```
overstate_ui/        Flask package. One module per blueprint.
  __init__.py        App factory (create_app), theme plumbing.
  config.py          Environment-only configuration; secrets never go
                     in the DB.
  auth.py            Local login, OIDC, roles, seed_admin.
  authz.py           Scoped RBAC evaluator: roles, scopes, constrain.
  api.py             Bearer-token CI endpoint (POST /api/jobs/run).
  salt_client.py     salt-api wrapper: wheel, local, runner, events.
  fleet.py           Per-pod salt-api clients across the master pods
                     (headless DNS). Same-JID publish fan-out lives in
                     jobs_service.py (`_publish_all_async`).
  k8s.py             Minimal in-cluster Kubernetes client (stdlib only):
                     ConfigMap read/replace, StatefulSet restart, pod
                     listing.
  db.py              Engine, session, create_all.
  models.py          SQLAlchemy models.
  inventory.py       Grains snapshot refresh (RQ-safe import path).
  audit.py           log_event for mutating actions.
  settings.py        DEFS, SECTIONS, env fallback, settings routes.
  dashboard.py       Live stats + Postgres history.
  minions.py         List, detail, presence, CSV, onboard wizard.
  minions_helpers.py Roster merge, grains, onboarding, beacons: pure
                     helpers behind the minions routes.
  groups.py          Saved minion groups for the group job target.
  keys.py            Key tabs and wheel actions fanned out to every
                     reachable master pod.
  jobs.py            Run form, presets, history, detail, SSE stream.
  jobs_helpers.py    Job-runner constants and pure helpers (no routes,
                     no Salt calls).
  jobs_service.py    Job service layer: returner sync, live cache,
                     launch, batches. Called by routes and workers.
  console.py         Browser salt-master CLI console: salt-style
                     commands mapped onto the same guardrails as the
                     job runner (allowlist, confirm gate, audit row).
  states.py          Conformity, watched SLS list, recompute.
  schedules.py       Per-minion schedule CRUD through salt-api.
  pillar.py          Live pillar, snapshots, diff.
  mine.py            Mine browser: read mine data by target and
                     function (read-only).
  files.py           File-roots browser: operator edit/save (one commit
                     per save) plus admin push; listing/view stay
                     viewer-visible.
  git_sync.py        Checkout status, ff-only sync, single-file commit,
                     upstream push. Fixed git argv, no shell, bounded
                     timeouts, one-at-a-time lock, fixed failure words.
  masterconfig.py    Master Config: own the salt-master ConfigMap from
                     the browser (admin-only; snapshot to history, then
                     PUT-replace through the in-cluster client).
  reactor.py         Master reactor mapping: list, inspect SLS,
                     add/delete, export to file. Mapping writes go
                     through the owned ConfigMap, live changes through
                     the reactor runner fanned out to every pod.
  events.py          Filtered event-bus viewer and stream.
  users.py           Users, grants, local groups, service accounts.
  tasks.py           Background Salt queries over RQ: compatibility
                     facade re-exporting the task modules below.
  tasks_queue.py     RQ plumbing: queue access, short waits, app
                     contexts, capability cache.
  tasks_salt.py      Per-domain Salt wrappers, inline or on RQ workers.
  tasks_k8s.py       Kubernetes status probes, inline or on RQ workers.
  tasks_batch.py     Gated wave batches and orchestration runs over RQ.
  reconcile_cli.py   Hourly key-reconcile CronJob entrypoint
                     (`python -m overstate_ui.reconcile_cli`).
  seed_mock.py       Mock-data seeder for UI work without Salt.
  worker.py          RQ worker entrypoint (`python -m overstate_ui.worker`).
  wsgi.py            gunicorn entrypoint.
  templates/         50 templates: Jinja pages plus _rows partials for
                     HTMX.
alembic/             Migrations. The entrypoint runs upgrade head.
tests/               pytest suite, one file per area.
salt-config/         Dev master config: api.conf, dev.conf,
                     returner.conf, dev TLS PKI.
salt-srv/            Demo file roots (top.sls, demo.sls).
scripts/             Dev stack helpers and TLS tooling.
```

## App factory and request flow

`create_app` builds the Flask app, registers every blueprint, wires
Flask-Login, CSRF, and the shared `SaltClient` on
`current_app.extensions["salt_client"]`. View code reaches Salt
through that client, never through raw HTTP. `wsgi.py` creates the
app, retries `create_all` while Postgres wakes, seeds the admin once,
and serves under gunicorn.

Configuration comes from the environment through `config.py`. The
`OIDC_*` keys double as fallbacks for DB settings; everything else is
plain app config. Tests use `TestConfig` with CSRF off.

## salt_client

`SaltClient` owns the service-account token: login against salt-api
with the eauth user, cache the token, re-auth on expiry. Three call
shapes cover the whole UI:

- `wheel(...)` for keys and key lists.
- `local(tgt, fun, ...)` for minion calls, sync or async.
- `runner(...)` for jobs, manage, and orchestration.

`event_stream()` yields parsed bus events for the Events page and job
streams. All Salt failures surface as `SaltApiError`, which views
catch and flash. Add a new Salt call as a method here so token
handling and error shape stay in one place.

## Settings machinery

`DEFS` maps each key to label, help, default, and optional options.
`SECTIONS` groups keys into the four panels the template renders.
`OIDC_ENV_FALLBACK` maps OIDC keys to their config names.

`get_setting` resolves in order: settings table row, then env-backed
config for OIDC keys, then the detected master hostname for
`master_host`, then the DEF default. The save view loops over `DEFS`,
trims posted values, drops cleared OIDC rows so the environment
applies again, resets anything else empty to its default, and coerces
option values back to default on mismatch.

Add a setting by adding one `DEFS` entry, appending its key to a
`SECTIONS` group, and adding a fallback entry if the environment may
provide it. The form, save logic, and grouping test pick it up with
no template change. The template masks any key containing `secret`
as a password field.

## Auth and roles

`LEVELS = {"viewer": 0, "operator": 1, "admin": 2}`. `roles_required`
takes role names, computes the minimum level, and aborts 403 below
it; in scoped mode it checks a coarse permission floor instead, and
each view additionally calls `require("<specific perm>")`. Read-only
views take `read_required(perm)` in scoped mode, bare
`login_required` in legacy. Local users carry an argon2 hash; SSO
users carry `password_hash = None` plus the `(oidc_issuer, oidc_sub)`
identity key, so the login form can never authenticate them. Service
accounts (`kind=service`) authenticate only by Bearer token on
`/api/jobs/run`, never by login.

Scoped RBAC lives in `authz.py`: fourteen roles in `ROLE_PERMS`,
scopes (fleet, minion group id, id list, id glob, snapshot grain,
file prefix), and union evaluation with no denies. `constrain_target`
rewrites non-fleet publishes to an explicit snapshot list before any
Salt call; fleet grants keep Salt's own target semantics. Salt-ssh
stays fleet-only, and compound/nodegroup targets 403 without one.
`provision_oidc_user` finds or creates the account by identity key,
replaces the IdP group claim wholesale, and recomputes the `role`
cache without minting a viewer grant. Group mapping wins over manual
edits on every login. Keep it that way; split-brain authorization is
worse than surprise. Manual grants are additive and survive login.

Delegates (`grant.delegate`) write only inside the subset rule in
`delegate_grant_error`: minion-matcher scope within their own,
permissions they already hold there, no fleet, no `grant.admin`, no
self-edit, no backfilled ladder rows.

`rbac_mode` and `rbac_role_fallback` resolve DB row, else non-empty
env, else `legacy` / `on`, and never insert on read. They are not in
`OIDC_ENV_FALLBACK`. Only the exact strings `scoped` and `off` flip
behavior, and `scoped` is inert while `ENFORCEMENT_COMPLETE` is false.
`users.role` stays the legacy authority and the scoped compatibility
cache (`admin`, `operator`, `viewer`, `scoped`, `none`); only
`refresh_role_cache` writes it.

## Data model and migrations

Models live in `models.py`: users, settings, minions (grains plus
conformity jsonb), jobs, job returns, saved jobs, watched states,
pillar snapshots, audit events. Postgres holds history; Redis holds
nothing durable.

Schema changes go through Alembic. Generate a revision, review the
diff, and test upgrade plus downgrade on a scratch database. The
container entrypoint runs `upgrade head` at boot, and stamps
pre-Alembic databases before upgrading, so a revision must tolerate a
database that already has its tables. SQLite runs the test suite;
keep migrations portable across both backends, and use batch mode
for SQLite ALTER limitations.

## Conventions

- Every mutating view calls `log_event` with the user, the action,
  and the JID. No silent writes. Denied scoped attempts log a `deny`
  row (`outcome=deny`, permission, minion id when the route has one)
  and stop with 403; an id that exists but is out of scope is 403,
  never 404, so auditors see the attempt. Responses and flashes name
  only ids the caller supplied.
- Every POST carries a CSRF token, including HTMX requests. If your
  new form 400s, you forgot the token.
- HTMX swaps use `_rows.html` partials; full pages stay server
  rendered. Alpine handles widgets only.
- Flash messages report outcomes in plain words: what you did, the
  target, the result. Errors name the problem and the recovery.
- Sort, filter, and pagination state lives in query strings so pages
  stay linkable.
- Destructive Salt functions live in `DESTRUCTIVE_FUNS` and require
  typing the target to confirm. Add a function there when it destroys
  data or restarts services.
- Job detail streams poll `sync_job` on an interval the query string
  clamps between 0.05 and 5 seconds. Cap loops; streams must end.
- Fleet-wide or multi-call Salt queries go through the worker:
  `queue_or_none` enqueues, `wait_for` blocks briefly, and the view
  runs the same code synchronously when Redis is unreachable — except
  the dashboard, which renders its snapshot instantly and polls job
  state via `describe_job` instead of ever blocking on Salt. Task
  functions build their own app inside `isolated_app` and must stay
  importable by path with JSON-serializable returns. Capability
  results cache in Redis with a short TTL; probe functions stay
  read-only.

## Tests

Run `.venv/bin/pytest -q` from the repo root. Focused files run the
same way: `tests/test_auth.py`, `tests/test_rbac.py`, and friends.
Tests build the app on `TestConfig` against in-memory SQLite and seed
through the same `seed_admin` and `create_all` paths production uses.

Cover new behavior with a focused test in the matching file. A test
that posts a form proves the route, the guard, the save, and the
render in one shot; prefer that over unit-testing helpers in
isolation. The settings grouping test asserts every `DEFS` key
appears in exactly one `SECTIONS` group, so the panels cannot drift
from the data. Keep that invariant when you add keys.

## Lint, format, coverage, deps

`ruff check overstate_ui tests` and `ruff format --check overstate_ui
tests` are the gates; both must pass. Lint defaults come from ruff
with `target-version = "py312"` in `pyproject.toml`. There is no
Makefile: run the tools directly from the repo root.

Coverage config lives in `pyproject.toml` (`[tool.coverage.*]`,
source `overstate_ui`, branch mode). Measure with
`.venv/bin/pytest -q --cov=overstate_ui --cov-report=term-missing`;
baseline is ~84%. No `fail-under` gate is committed yet: add one in
CI once the baseline is ratified, not before.

Python runtime deps are pinned in `requirements.lock` (verified by a
clean install running the suite); the `Containerfile` installs `-r
requirements.lock .` so image builds are reproducible. JS deps float
on caret ranges with `package-lock.json` + `npm ci`. Weekly
`.github/dependabot.yml` covers pip, npm, and docker (docker only
advisories: files are named `Containerfile*`, which Dependabot does
not auto-detect).

The file editor (`assets/editor.js`, CodeMirror 6 + YAML) is bundled
once with `npm run build:editor` into the committed
`overstate_ui/static/editor.bundle.js`, which the edit page loads like
htmx/alpine — no CDN. Rebuild and commit the bundle after any editor
dependency bump; the save form posts the plain textarea, so the app
works with the bundle stale or missing.

## UI work without Salt

`python -m overstate_ui.seed_mock` fills the database with fake
minions, jobs, returns, pillar snapshots, and audit rows. It refuses
to run on a non-empty database unless you pass `--force`. The dev
stack wrapper is `./scripts/seed-mock.sh --force`, which runs the
seeder inside the app container against the stack Postgres. Seeded
data exercises every page, including failure states; use it to check
a template change across full, empty, and error renders before you
push.
