# Install on openSUSE Leap 16

Salt runs in a container (Quadlet `overstate-salt-master`), the same image
as dev — decided 2026-09-13. The image pins the Salt version, so dev and
prod stay identical and the host only needs Podman.

## Install

```sh
sudo ./scripts/install.sh --admin-password 'pick-one'
```

What it does: checks Leap 16 (root), installs podman/openssl, builds
both images, lays down `/etc/overstate` (env, salt-config copy, TLS) and
`/var/lib/overstate/srv`, generates secrets plus a self-signed CA/certs
(hostname in the app cert, `salt-master` in the api cert), installs six
Quadlet units, and starts everything. Re-running is safe: secrets, certs,
and data are kept.

Without `--admin-password`, the initial `admin` password is printed to the
app log once: `podman logs overstate-app | grep 'seeded admin'`.

## Layout

- Units: `/etc/containers/systemd/overstate-{app,worker,salt-master,postgres,redis,caddy}.container` + `overstate.network`
- Config: `/etc/overstate/overstate.env` (0600), `/etc/overstate/salt-config`, `/etc/overstate/tls`, `/etc/overstate/Caddyfile`
- State roots: `/var/lib/overstate/srv` (edit SLS/pillar here)
- Data: podman volumes `overstate-pgdata`, `overstate-saltdata` (master keys), `overstate-caddy-data` (ACME certs)
- Web: `https://overstate.sigaint.au` via Caddy. salt-api: `https://overstate-api.sigaint.au` via Caddy, plus `127.0.0.1:8001` locally.

## Reverse proxy (Caddy)

Caddy terminates public TLS with automatic ACME certificates and proxies
to the self-signed backends, verifying them against our CA rather than
skipping verification. Prerequisites: DNS A records for both names pointed
at the host, inbound ports 80+443 open. Override the names with
`APP_DOMAIN` / `API_DOMAIN` in `overstate.env`. Edit `/etc/overstate/Caddyfile`
for extra routes, then `systemctl restart overstate-caddy`.

## Minion ports and firewall

The `overstate-salt-master` unit publishes the master's ZeroMQ ports on
all host interfaces: 4505/tcp (publish) and 4506/tcp (request/return).
Minions initiate both connections, so minion-side firewalls need
outbound 4505+4506 only, no inbound rules. salt-api is intentionally
not published: it stays on `127.0.0.1:8001` and is fronted by Caddy,
so never open port 8001 to a network.

On the master host (firewalld on Leap 16):

```sh
sudo firewall-cmd --permanent --add-port=4505/tcp --add-port=4506/tcp
sudo firewall-cmd --reload
sudo firewall-cmd --list-ports  # expect 4505/tcp 4506/tcp (plus 80/443)
```

Then confirm the ports listen before enrolling:

```sh
ss -ltn | grep -E '4505|4506'
```

A minion that reaches the master shows up under Pending on the Keys
page.

Point each minion at the master hostname (`master: <host>` in
`/etc/salt/minion`), start it, accept its key on the Keys page after
checking the fingerprint, and run `test.ping` from the Jobs page.
Details live in `docs/deployment.md` under "Enroll minions".

Existing installs pick the published ports up with
`sudo ./scripts/update.sh`, which reinstalls changed units,
reloads systemd, and restarts the master. The dev compose stack
deliberately does not publish these ports: its master runs with
`auto_accept` and a built-in minion only.

### If a minion was auto-accepted

Installs before this fix deployed the dev-only `auto_accept` config to
`/etc/overstate/salt-config/dev.conf`, so any minion that connected was
accepted without fingerprint review. `update.sh` (or a re-run of
`install.sh`) deletes that file and restarts the master, which stops
future auto-accepts — but keys accepted while it was on stay accepted.
For each such minion: review the Keys page for anything you did not
manually accept, delete the key, and let the minion re-enroll so it
lands under Pending. Then verify the fingerprint, accept it by hand,
and run `test.ping` from the Jobs page.

## Update / uninstall

```sh
sudo ./scripts/update.sh        # rebuild from this checkout, restart app stack
sudo ./scripts/uninstall.sh         # remove units, keep config+data
sudo ./scripts/uninstall.sh --purge # also delete config, state, volumes
```

Migrations run inside the app entrypoint on every boot.

## Production hardening

- Public TLS is Caddy's ACME certs; the self-signed internal certs only
  matter if you expose a backend directly. To use your own public cert,
  put it in Caddy (e.g. a `tls cert key` site block) instead.
- Change the admin password after first login; delete the
  `ADMIN_PASSWORD=` line from `overstate.env`.
- Accept minion keys on the Keys page (no auto-accept, no built-in minion).
- Back up `/var/lib/overstate` and `overstate-pgdata` / `overstate-saltdata`.
- Put a reverse proxy with TLS in front if 8000 must face a network.
