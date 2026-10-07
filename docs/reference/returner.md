# Job return path (grill decision D1: stock-first)

Every Salt job's returns flow through the **stock `pgjsonb` returner** into
Overstate's Postgres. No custom Salt code.

## How it works

1. The dev master (`salt-master` service, see `Containerfile.salt-master`)
   sets `master_job_cache` to `pgjsonb` (`salt-config/returner.conf`), so
   every job stores its load and returns. (`ext_job_cache` must stay unset:
   it handles publish-time loads only and disables return storage.)
2. The returner writes two transport tables, owned by the master:
   `jids` (jid → job load) and `salt_returns` (one row per minion return).
   The app creates them (`create_all`) and versions them in Alembic, but
   never writes them.
3. The app reads `salt_returns` into its own `jobs` / `job_returns` tables
   (Phase 5), which is what the UI renders. The transport tables are never
   queried by templates.

## Pointing a real master at Overstate

1. Install a postgres driver where the master runs (`psycopg2`).
2. Create the `jids` / `salt_returns` schema: run `alembic upgrade head`
   against the Overstate database (revision `46615d932d32` adds them).
3. Add to the master config (adapt host/user/pass):
   `master_job_cache: pgjsonb`, plus the flat `returner.pgjsonb.*`
   connection keys from `salt-config/returner.conf` (flat dotted keys —
   the master job-cache path does not traverse a nested mapping).
4. If the stock schema ever fights the app's queries, the agreed fallback
   is a thin custom returner writing `jobs` / `job_returns` directly —
   not a second store.

## Audit

Every mutating click also writes an `audit_events` row (who, action, JID)
via `overstate_ui.audit.log_event`, independent of the return path, so an
action is traceable even if its returns never land.
