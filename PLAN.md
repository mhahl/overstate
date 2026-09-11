# Overstate

A Salt GUI for managing minions, keys, jobs, schedules, and state application on Linux fleets (openSUSE, Fedora, and friends).

Named after Salt’s old orchestration layer that sat *above* highstate. The GUI is the thing that thinks it is in charge of the run.

Not a Salt master. Not an SLS IDE. A control plane UI in front of **salt-api**.

## Goals

- Match the useful Alcali feature set: job archive, live job updates, key management, minion inventory, schedules, conformity.
- Stay boring to operate: Flask + HTMX + Alpine + DaisyUI 5, Postgres, Redis, Podman-first.
- Proxy Salt. Do not become Salt.

## Non-goals (v1)

- Editing `master.conf` or restarting `salt-master` / `salt-api` from the UI
- SLS / pillar file editor or git-backed formula catalog
- Multi-user ACL mapped onto Salt eauth (local admin only)
- LDAP / OAuth / custom Salt auth module
- Syndic, salt-ssh, patch/pkg workflows
- Treating Postgres as a CMDB that is more true than Salt

## Architecture

```
[browser] --HTMX/SSE--> [overstate: gunicorn]
                              |
              +---------------+---------------+
              |               |               |
         salt-api         Postgres          Redis
         (wheel /         jobs, grains      cache,
          local /         snapshots,        pub/sub,
          runner)         settings,         SSE fan-out
                          audit, users
```

Rules:

1. **salt-api is the only control plane.** Wheel for keys, local client for minion calls, runner for jobs / orch / manage. No SSH, no scraping `/etc/salt/pki`.
2. **Postgres is the system of record** for users, settings, audit, inventory *snapshots*, and job returns. Use the Salt postgres/pgjsonb returner (or a thin Overstate returner) so the master writes `jids` / returns / events into the same DB. Do not rely on the master’s on-disk job cache (`keep_jobs` will eat history).
3. **Redis is not the job store.** Use it for grains cache, pub/sub for live events, and optional RQ for “refresh all minions” so a request thread does not fire `grains.items` at the whole fleet.
4. **Three containers:** `overstate`, `postgres`, `redis`. The Salt master stays on the host (or its own container) with salt-api enabled. Rootless Podman + compose + quadlet.
5. **Every mutating click is a Salt job.** Accept key, apply state, toggle schedule → JID → poll or subscribe → persist return. The UI does not invent success.

### What “manage the master” means

Allowed in v1:

- Master status: version, uptime, connected count, salt-api health
- Wheel / runner calls the service account is allowed to make
- Read-only hint of fileserver envs / top targets
- Explicit `saltutil.sync_all` / `saltutil.refresh_pillar` actions

Not v1: edit master config, restart services, write into `/srv/salt`.

## Stack

| Layer | Choice |
|---|---|
| UI | DaisyUI 5.7.x + Tailwind 4 + HTMX 2 + Alpine |
| App | Flask, Jinja, Flask-Login, CSRF on every HTMX POST |
| ORM | SQLAlchemy 2 + Alembic |
| DB | Postgres (`jsonb` for grains and job returns) |
| Cache / live | Redis |
| Jobs in the app | Optional RQ for background inventory refresh |
| Serve | gunicorn (not Flask dev server); SSE for live jobs |
| Pack | `Containerfile`, `compose.yml` for `podman compose`, quadlet example |
| Secrets | Env only: `DATABASE_URL`, `REDIS_URL`, `SALT_API_URL`, `SALT_EAUTH_USER`, `SALT_EAUTH_PASSWORD`, `SECRET_KEY` |

Package name: `overstate_ui` (avoid colliding with historic Salt “OverState”).

Auth v1: single local admin, argon2 hashes, signed cookie session, login rate limit. Seed admin only when no users exist. Salt-api password never goes in the settings table.

Salt identity: dedicated eauth user `overstate` with `@wheel`, `@runner`, and a tight execution list. Do not log the GUI in as a human with `.*`.

## Feature set

### v1 — ship when this works against a real master

**Auth**

- Local admin login / logout

**Dashboard**

- Accepted vs pending keys
- Minions up / down / stale
- Jobs in flight
- Last failures
- salt-api health (URL, token age, last probe, whether `@wheel` / `@runner` work)

**Keys**

- Tabs: Pending / Accepted / Rejected / Denied
- Fingerprint
- Accept / reject / delete
- Never auto-accept

**Minions**

- Server-side table: search, filter, paginate
- Columns from grains: id, osfinger, osrelease, fqdn, IPs, cpuarch, num_cpus, mem_total, virtual, Salt version
- Presence: last-seen, `test.ping`, accepted-but-dead
- Detail tabs: Overview | States | Jobs | Schedule | Pillar (read-only) | Beacons (read-only)
- Inventory is a cache. Refresh on demand + periodic RQ job. Salt remains source of truth.

**Jobs**

- Runner: target type (glob, list, grain, compound, nodegroup), function + args, sync / async, `test=True`
- First-class buttons: `test.ping`, `state.apply`, highstate, dry-run highstate
- Saved / predefined jobs
- History + running from Postgres returner
- Detail with foldable highstate output (summary first; do not dump raw JSON into the DOM)
- Live updates via HTMX SSE (Redis pub/sub fed by salt-api `/events` or `salt_events`)
- Optional job kill / signal if the master allows it

**States / conformity**

- Apply SLS or highstate, with dry-run
- Per-minion conformity from last highstate (or watched SLS): ok / drifted / unknown / unreachable
- Watched custom states
- Orchestration: run `state.orchestrate` and show returns (name earned)

**Schedules**

- List minion (and master, if cheap) schedules
- Enable / disable / delete
- Add can wait until the list + toggle is solid

**Events**

- Filtered event viewer: `salt/job`, `salt/auth`, `salt/minion/*/start`
- Filter server-side. Do not stream raw payloads into the browser.

**Settings (DB)**

- Default target, page size, theme, visible grain columns, watched conformity states, job-retention display
- Not in DB: Redis, Postgres, salt-api credentials

**Audit**

- Who clicked accept-key / highstate / schedule-delete, when, JID

**Ops**

- CSV export of inventory
- Command output formatters: highstate-pretty, JSON, raw

### v1.1

- Nodegroups as a first-class target source
- Minion compare (grains; pkg list later)
- Schedule add UI
- More master runners the admin opts into

### v2+

- SLS editor / fileserver browser
- Multi-user roles mapped to eauth
- LDAP / OIDC
- Formula catalog, pkg upgrades (`zypper` / `dnf` via grains, not hardcoded)

## Navigation

Primary sidenav:

- Dashboard
- Minions
- Keys
- Jobs
- States
- Schedules
- Events
- Settings

Secondary sidenav on Minions: List | Groups | Stale

Jobs tabs: Running | History | Saved

Keys tabs: Pending | Accepted | Rejected | Denied

Minion detail: tabs as above. Third-level nav is tabs only.

Tables are server-rendered with search, filters, and pagination. Alpine is for widgets, not for holding the fleet in memory.

## Data model (sketch)

- `users`
- `settings` (key/value)
- `minions` (id, last_seen, key_status, grains jsonb, conformity jsonb)
- `jobs` (jid, fun, tgt, tgt_type, user, start, complete)
- `job_returns` (jid, minion_id, success, retcode, return jsonb)
- `audit_events`
- `saved_jobs`
- `watched_states`

Index `jid`, `minion_id`, `fun`, `alter_time`. Cap what you render; store full jsonb.

## Distro notes

Do not fork the UI per distro. Display `osfinger` and pkg manager as grains. Master on Tumbleweed vs minion on Fedora will skew Salt versions — show versions on the dashboard.

## v1 acceptance

A stranger can `podman compose up`, point Overstate at an existing master + salt-api, and:

1. Log in as local admin
2. Accept a pending key
3. See the minion in inventory with hardware / IP grains
4. Run `test.ping` and a dry-run highstate
5. Watch the job land live and persist in history
6. Toggle a schedule
7. See conformity from the last highstate
8. Confirm salt-api health and an audit row for the apply

If “manage configuration” still means an SLS editor after that, it is a different milestone.

