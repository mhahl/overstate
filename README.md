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

Target is Kubernetes: everything lives in the `overstate` namespace
and applies with one command:

```sh
kubectl apply -k deploy/kubernetes
```

Read `docs/install-kubernetes.md` for the setup walkthrough
(secrets first, then apply, then verify) and
`docs/architecture-kubernetes.md` for the design, assumptions, and
risks behind the manifests.

## Layout

- `overstate_ui/` holds the app: routes per area, Salt client,
  background tasks, templates.
- `tests/` holds the pytest suite, one file per area.
- `deploy/kubernetes/` holds the production manifests (Kustomize).
- `scripts/` holds dev helpers (`dev-up.sh`, `seed-mock.sh`, …).
- `docs/` holds operator, deployment, and developer guides.

## License

Apache-2.0, see `LICENSE`. Dependency licences live in
`THIRD-PARTY-LICENSES.md`.
