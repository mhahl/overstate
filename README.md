# Overstate

Overstate is a web UI in front of one Salt master. You accept keys,
fire jobs, apply states, and read the results here. Salt still does
the work. Every button maps to a real Salt call, and the UI shows the
JID so you can trace each action.

Operators get job runs, state applies, and batches. Admins get user
and settings management on top. Viewers can look at everything and
change nothing. Destructive functions ask you to type the target
before they fire.

## Stack

Flask and Jinja on the server, daisyUI in the browser, Postgres for
state, Redis and RQ for background Salt calls. Long queries run on a
worker; the app runs them inline when the worker is down. Salt stays
outside the containers and talks to the app through salt-api.

## Run it locally

You need Podman or Docker with compose.

```sh
cp .env.example .env
./scripts/dev-up.sh
```

Open `https://127.0.0.1:8000` and accept the self-signed cert. The
first boot prints the admin password in the app log:

```sh
podman logs overstate_overstate_1 | grep "seeded admin"
```

`./scripts/seed-mock.sh --force` fills the UI with fake fleet data
for frontend work. `./scripts/dev-down.sh` stops the stack.
`./scripts/dev-rebuild.sh --fresh` starts over.

## Tests and lint

```sh
.venv/bin/python -m pytest -q
ruff check overstate_ui tests
ruff format --check overstate_ui tests
```

Tests build the app on an in-memory database and seed through the
same paths production uses. Coverage config lives in
`pyproject.toml`; measure with `pytest -q --cov=overstate_ui`.

## Install it for real

Target is openSUSE Leap 16 with Podman, running the stack as
systemd Quadlets:

```sh
sudo ./scripts/install.sh --admin-password 'pick-one'
```

Units live in `deploy/quadlet/`, the Caddyfile in `deploy/`.
`sudo ./scripts/update.sh` rebuilds from the checkout.
`sudo ./scripts/uninstall.sh` removes the units and keeps data.
Read `docs/deployment.md` before you touch a master, and
`docs/install-opensuse.md` for the full install walkthrough.

## Layout

- `overstate_ui/` holds the app: routes per area, Salt client,
  background tasks, templates.
- `tests/` holds the pytest suite, one file per area.
- `deploy/quadlet/` holds the production units, `deploy/Caddyfile`
  the proxy config.
- `scripts/` holds install, update, uninstall, and dev helpers.
- `docs/` holds operator, deployment, and developer guides.

## License

Apache-2.0, see `LICENSE`. Dependency licences live in
`THIRD-PARTY-LICENSES.md`.
