# Overstate deployment guide

Overstate ships as containers. Salt stays outside them: the master
runs on the host or its own machine, and the app talks to it through
salt-api. This guide covers the dev stack, pointing the app at a real
master, setting that master up, enrolling minions, and hardening
production.

## Dev stack

You need Podman (or Docker) with compose. Copy the env file, bring
the stack up, open the app.

```sh
cp .env.example .env
./scripts/dev-up.sh
```

`dev-up.sh` generates the dev TLS certificates on first run, builds
changed images, waits for Postgres, and installs the salt-api cert.
The app lands at `https://127.0.0.1:8000`. Your browser warns about
the self-signed dev CA; add an exception. For a clean rebuild, run
`./scripts/dev-rebuild.sh`, which tears down, rebuilds the app image
without cache, and restarts. Pass `--fresh` to drop volumes too,
`--seed` to load mock data.

Five containers start: `overstate`, `worker`, `salt-master`,
`postgres`, `redis`. The worker runs the RQ background queue for
long Salt queries (fleet refresh, dashboard probes); the app falls
back to synchronous calls when the worker is down, so the stack
works without it but slower. Redis is the queue transport plus the
short-TTL capability cache. The master is dev-only: it auto-accepts
keys and runs a built-in minion so the stack works unattended.
Never copy that behavior to a real master.

First boot prints the generated admin password in the app container
log. Grab it, log in, store it in your vault.

```sh
podman logs overstate_overstate_1 | grep "seeded admin"
```

`./scripts/seed-mock.sh --force` fills the UI with fake fleet data
for frontend work. `./scripts/dev-down.sh` stops everything.

## Point Overstate at a real master

Three values connect the app: `SALT_API_URL`, `SALT_API_USER`, and
`SALT_API_PASSWORD`. Set them in `.env` or your deployment
environment. The app verifies the salt-api certificate against
`SALT_API_VERIFY_CA`; mount your CA there or TLS verification fails
closed. Plain `http://` URLs work for local testing only; production
must use HTTPS.

Job history needs the returner. Point the master at the app Postgres
with the stock `pgjsonb` returner (details below). Without it the
History tab stays empty and job detail shows only live data.

### File roots: one checkout, one writer, two readers

`FILE_ROOTS` points the file browser at a checkout of your Salt
states. Follow the whole chain before changing any link of it:

1. **Host directory.** Production keeps the checkout at
   `/var/lib/overstate/srv` (`scripts/install.sh` creates it and
   seeds demo files on first install; dev uses `./salt-srv`).
2. **App mount — writable.** The app container mounts that directory
   at `/srv/states` with `:rw` (`compose.yml` for dev,
   `deploy/quadlet/overstate-app.container` for production), and
   `FILE_ROOTS=/srv/states/salt` points inside it. The Files page
   reads the listing from here, and the operator-gated **Sync now**
   button runs `git fetch` + `git pull --ff-only` on it — the same
   flags as `scripts/sync-file-roots.sh`, which stays available for
   cron or a sidecar if you prefer sync outside the app. The app is
   the only in-app writer: it never edits, commits, or pushes. Pick
   one sync actor per site (button or cron, not both): two writers
   racing on the same checkout trip git's lock and the loser just
   reports a refusal, harmless but noisy.
3. **Master mount — read-only.** The salt-master container mounts the
   same host directory (`/home/salt/data/srv`, `:ro`) and serves it
   as its file roots, so minions enforce exactly what the browser
   shows. The master never needs write access; keep it that way.
4. **After a sync.** The browser shows the new git SHA immediately,
   but the master serves from its fileserver cache, so `state.apply`
   can lag one refresh behind. This is expected, not a failed sync.

Sync activates only when the directory is a git checkout with an
upstream: the install seed is plain files, so turn it into a checkout
(clone your states repo there) to light up the button. Anything else
— diverged branches, uncommitted trees, missing upstream — makes Sync
refuse with the git reason and change nothing, and every attempt is
audited.

## Set up the Salt master

Install Salt on openSUSE or Fedora from the distribution packages.
You need the master plus the API daemon.

```sh
# openSUSE
sudo zypper install salt-master salt-api
# Fedora
sudo dnf install salt-master salt-api
```

### API user and permissions

Create a dedicated system user for the app, not a human account, and
give it a long random password. The app logs in as this user over
salt-api. Grant the minimum your operators need. Start from this
shape and trim:

```yaml
external_auth:
  pam:
    overstate:
      - test.ping
      - grains.items
      - pillar.items
      - mine.update
      - state.apply
      - state.highstate
      - state.show_highstate
      - state.show_sls
      - schedule.*
      - mine.*
      - beacons.list
      - beacons.enable_beacon
      - beacons.disable_beacon
      - saltutil.sync_all
      - saltutil.refresh_pillar
      - saltutil.kill_job
      - sys.*
      - "@wheel"
      - "@runner"
      - "@jobs"
```

Every function the UI offers must appear here or the call fails with
a permission error on the detail page. Package and service management
(`pkg.install`, `pkg.remove`, `service.restart`, `ps.kill_pid`) are
separate grants; add them only if your operators run those presets.
Salt-ssh needs the `ssh` client in `netapi_enable_clients` plus SSH
access from the master to the targets.

### salt-api TLS

Enable HTTPS on salt-api with a real certificate. Generate or obtain
one for the master's API hostname, then set the CherryPy block:

```yaml
rest_cherrypy:
  port: 8000
  host: 0.0.0.0
  ssl_crt: /etc/pki/tls/certs/salt-api.crt
  ssl_key: /etc/pki/tls/private/salt-api.key
```

Restart `salt-api`. Confirm with `curl -k https://master:8000/login`
expecting a 401 (reachable, auth required), then without `-k` once
your CA is trusted. Copy that CA certificate to the Overstate host
and mount it at `SALT_API_VERIFY_CA`.

### Firewall

Open three ports on the master: 4505 and 4506 for minion traffic,
8000 (or your API port) for Overstate. Minions initiate both
connections, so minion-side firewalls need no inbound rules. The
app server needs Postgres (5432) only if the returner writes from a
different host than the database.

### File roots

```yaml
file_roots:
  base:
    - /srv/salt
```

Keep `/srv/salt` in git. The `base` environment needs `top.sls`.
Overstate reads this tree for the file browser and applies states
from it. The demo tree in `salt-srv/` shows the shape: one top file
plus SLS files beside it.

### Job returner

Job history persists only if the master writes returns to Postgres.
Configure the stock `pgjsonb` returner with flat dotted keys (Salt
resolves these literally on the job-cache path; a nested mapping
does not apply):

```yaml
master_job_cache: pgjsonb
returner.pgjsonb.host: <postgres host>
returner.pgjsonb.port: 5432
returner.pgjsonb.db: overstate
returner.pgjsonb.user: overstate
returner.pgjsonb.pass: <long random password>
```

Create the tables by starting Overstate once against that database;
the entrypoint migrates before serving. The master only writes, so
grant that Postgres user INSERT on the job tables and nothing else
if your policy wants it tight. Leave `ext_job_cache` unset: it
handles publish-time loads only and disables return storage.

### Harden the master

- `auto_accept` stays off. Accept every key by fingerprint.
- Never expose salt-api to the internet. Bind it to the management
  network or front it with a reverse proxy and client certificates.
- Rotate the eauth password on your normal schedule. Update the app
  env and restart the app container; the client re-authenticates.
- Keep the master patched with the distribution updates. Salt
  versions skew across distros already; the dashboard shows each
  minion's version so drift stays visible.

## Enroll minions

Install the minion package and point it at the master.

```sh
# openSUSE
sudo zypper install salt-minion
# Fedora
sudo dnf install salt-minion
```

Write `/etc/salt/minion` with one line:

```yaml
master: salt.example.com
```

Start and enable the service, then accept the key:

```sh
sudo systemctl enable --now salt-minion
sudo salt-key -a new-minion-01
```

Faster path: on the Minions list choose Onboard minion, pick the
distro, type the minion ID and master hostname, and download the
generated join script. Run it as root on the new machine. It installs the
package, writes the master line, and starts the service. The key
lands in Pending; accept it on the Keys page after checking the
fingerprint, then run `test.ping` from the Jobs page to confirm.

Minions need TCP 4505/4506 outbound to the master and a correct clock.
Wrong time breaks key exchange in confusing ways; run NTP everywhere.

## Production hardening

- `SECRET_KEY` must be long and random. Anyone holding it forges
  sessions. Generate one per deployment and never commit it.
- Serve the app over HTTPS. Mount real certificates at `TLS_CERT`
  and `TLS_KEY`. Without them the entrypoint serves plain HTTP and
  logs a warning; that mode exists for local dev only.
- Set real Postgres passwords in compose or your secrets manager.
  The defaults are public.
- Put the app behind a reverse proxy that forwards the client IP
  and host (`X-Forwarded-For/Host/Port`; the `deploy/Caddyfile`
  already does). The app trusts one proxy hop for these, which the
  login CSRF origin check requires. The login rate limit counts per
  address per worker; without the real IP, everyone behind the
  proxy shares one budget.
- Run one gunicorn worker per CPU as a starting point and raise it
  when dashboard loads slow down. Each worker holds its own rate
  limit counters and salt-api token.
- Back up Postgres nightly at minimum. It holds users, settings,
  inventory snapshots, job history, pillar snapshots, and the audit
  trail. Test restores; an untested backup is a rumor.
- Never mount the states checkout writable by the app. Sync it from
  git with `scripts/sync-file-roots.sh`, which refuses non-fast-forward
  updates instead of forcing them.
- Podman systems run the stack as systemd services through the
  per-service Quadlet units in `deploy/quadlet/` (app, worker,
  salt-master, postgres, redis, caddy, plus the network). Install
  them with `sudo ./scripts/install.sh`, which builds the images,
  lays down `/etc/overstate`, and enables the units as system
  services; `sudo ./scripts/update.sh` rebuilds and restarts,
  `sudo ./scripts/uninstall.sh` removes the units (config and data
  survive unless `--purge`). Adapt the unit files to your host
  before enabling anything by hand.

## Troubleshooting

- **Install fails with "transient or generated" on enable.** The
  Quadlet generator already wired the unit's `[Install]` section and
  systemd refuses `enable` on its own generated copy. `install.sh`
  detects this (`is-enabled` reports `generated`) and starts the
  unit directly. If it still fails, the unit file never generated:
  check `podman --version`, confirm the `.container` file is in
  `/etc/containers/systemd/`, and rerun `systemctl daemon-reload`.
- **Salt master logs "Permission denied" on keys/master.pem.** The
  data volume kept the previous container's SELinux label. Every
  mount in `deploy/quadlet/` carries a relabel flag (`:z` shared,
  `:Z` private), which heals this on restart: rerun
  `sudo ./scripts/update.sh`. If it persists, check Unix ownership
  versus MAC with `podman exec salt-master ls -laZ
  /home/salt/data/keys/` and `podman exec salt-master id`.
- **Migrations fail with "password authentication failed for user
  overstate".** Postgres sets its password only on first init, so a
  regenerated `overstate.env` no longer matches the data volume. On a
  fresh deploy with nothing to keep, stop the app and postgres,
  drop the stale volume, and start again: `systemctl stop
  overstate-app.service overstate-worker.service
  overstate-postgres.service && podman volume rm overstate-pgdata
  && systemctl start overstate-postgres.service
  overstate-app.service overstate-worker.service`. To keep existing
  data instead, `ALTER USER overstate PASSWORD` to the value in
  `DATABASE_URL` over the local socket.
- **Dashboard shows salt-api unreachable.** Check the URL, the CA
  mount, and the eauth password. `curl` the `/login` endpoint from
  the app container with the service credentials.
- **Salt calls fail with permission errors.** The function is missing
  from the eauth block. Add it, restart the master, retry.
- **History is empty but jobs run.** The returner is misconfigured or
  `ext_job_cache` is set. Fix `returner.conf` and check the master log
  for pgjsonb errors.
- **A minion stays pending.** The minion never reached the master, or
  someone already accepted a different key for that ID. Check the
  minion log, the clock, and ports 4505/4506.
- **Login loops or 403 on every page.** `SECRET_KEY` changed across
  a restart, invalidating signed cookies. Log in again. If it
  persists, one app instance has a different key than the others.
- **Migrations refuse to boot.** The entrypoint retries for a minute
  then exits. Read `/tmp/mig.log` in the container; a half-applied
  revision needs manual `alembic` repair against a backup.
