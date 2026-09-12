# Overstate developer guide

This is a control-plane UI, not a Salt library. The master does the
work through salt-api. The app renders pages, stores history, and
never invents success: a mutating click produces a JID, and the UI
reports what Salt returned for that JID.

## Layout

```
overstate_ui/        Flask package. One module per blueprint.
  __init__.py        App factory (create_app), theme plumbing.
  auth.py            Local login, OIDC, roles, seed_admin.
  salt_client.py     salt-api wrapper: wheel, local, runner, events.
  db.py              Engine, session, create_all.
  models.py          SQLAlchemy models.
  inventory.py       Grains snapshot refresh (RQ-safe import path).
  audit.py           log_event for mutating actions.
  settings.py        DEFS, SECTIONS, env fallback, settings routes.
  dashboard.py       Live stats + Postgres history.
  minions.py         List, detail, presence, CSV, onboard wizard.
  keys.py            Key tabs and wheel actions.
  jobs.py            Run form, presets, history, detail, SSE stream.
  states.py          Conformity, watched SLS list, recompute.
  schedules.py       Per-minion schedule CRUD through salt-api.
  pillar.py          Live pillar, snapshots, diff.
  files.py           Read-only file-roots browser.
  events.py          Filtered event-bus viewer and stream.
  users.py           Admin-only role management.
  seed_mock.py       Mock-data seeder for UI work without Salt.
  tasks.py           RQ task functions, queue helper, capability probes.
  worker.py          RQ worker entrypoint (`python -m overstate_ui.worker`).
  wsgi.py            gunicorn entrypoint.
  templates/         Jinja pages plus _rows partials for HTMX.
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
it. Read-only views take bare `login_required`. Local users carry an
argon2 hash; SSO users carry `password_hash = None` plus the
`(oidc_issuer, oidc_sub)` identity key, so the login form can never
authenticate them.

`provision_oidc_user` finds or creates the account by identity key,
sets the display name from claims, recomputes the role from groups,
and commits. Group mapping wins over manual edits on every login.
Keep it that way; split-brain authorization is worse than surprise.

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
  and the JID. No silent writes.
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
  runs the same code synchronously when Redis is unreachable. Task
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

## UI work without Salt

`python -m overstate_ui.seed_mock` fills the database with fake
minions, jobs, returns, pillar snapshots, and audit rows. It refuses
to run on a non-empty database unless you pass `--force`. The dev
stack wrapper is `./scripts/seed-mock.sh --force`, which runs the
seeder inside the app container against the stack Postgres. Seeded
data exercises every page, including failure states; use it to check
a template change across full, empty, and error renders before you
push.
