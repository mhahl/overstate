# Overstate Codebase Review

**Repository:** `/workspace/developer/overstate`
**Review Date:** 2026-10-06
**Branch:** `main` (at commit `987e2ae`)

---

## 1. Repo Health

### Git History & Branch Structure
- **Single branch:** `main` with 100+ commits; clean linear history with conventional commit messages
- **Dependabot branches:** 4 active dependabot PRs (docker base, npm daisyui, lucide, simple-icons)
- **Commit patterns:** Feature-based commits grouped by "Release app image pairXX" + descriptive fix commits
- **No tags/releases visible** in local clone

### Test Coverage
- **65 test files** in `tests/` covering:
  - Auth (local + OIDC): 8 test files
  - AuthZ/RBAC (legacy + scoped): 12 test files
  - Jobs, batches, orchestrate: 8 test files
  - Minions, keys, groups: 10 test files
  - Files, git sync, reactor, pillar, mine: 8 test files
  - Dashboard, console, events, audit: 6 test files
  - Cluster entrypoint, peer discovery, TLS: 5 test files
  - API, schedules, states, deploy artifacts: 8 test files
- **Fixtures:** Shared `conftest.py` with rate-limiter isolation and FakeRedis
- **DB:** Tests use SQLite in-memory; seed via production code paths (`seed_admin`, `seed_mock`)

### CI Configuration
- **No GitHub Actions workflows** found (only `.github/dependabot.yml`)
- Dependabot configured for: pip (weekly), npm (weekly), docker (weekly — notes Containerfile* naming not auto-detected)

### Dependency Freshness
| Package | pyproject.toml | requirements.lock | Status |
|---------|---------------|-------------------|--------|
| Flask | >=3.1 | 3.1.3 | Current |
| SQLAlchemy | >=2.0 | 2.0.52 | Current |
| Alembic | >=1.14 | 1.20.0 | Current |
| httpx | >=0.27 | 0.28.1 | Current |
| redis | >=5.0 | 8.1.0 | Current |
| rq | >=1.16 | 2.12.0 | Current |
| gunicorn | >=22.0 | 26.2.0 | Current |
| argon2-cffi | >=23.1 | 25.1.0 | Current |
| Authlib | >=1.3 | 1.8.0 | Current |

**npm deps:** tailwindcss 4.3.3, daisyui 5.7.39 (locked), codemirror 6.0.2 — all current as of review.

### License
- **Apache-2.0** (`LICENSE`) — copyright 2026 Mark Hahl
- **THIRD-PARTY-LICENSES.md** documents all deps; **one LGPL-3.0-only** (psycopg) used as separate package — compliant

---

## 2. Logic and Routing

### Main Entry Points
| File | Purpose |
|------|---------|
| `overstate_ui/__init__.py` | Flask app factory (`create_app`), blueprint registration, SaltClient extension, security headers, RBAC template globals |
| `overstate_ui/wsgi.py` | Gunicorn entrypoint; DB migration retry + admin seeding |
| `overstate_ui/worker.py` | RQ worker entrypoint (`python -m overstate_ui.worker`) |

### Blueprint Registration (17 blueprints)
```
auth → console → dashboard → keys → masterconfig (legacy) → minions → mine
→ groups → pillar → reactor → jobs → settings → states → schedules
→ events → audit → users → files → api
```

### Business Logic Modules
| Module | Responsibility |
|--------|----------------|
| `salt_client.py` | **Only** module talking to salt-api; eauth token lifecycle, wheel/local/runner/SSE wrappers |
| `auth.py` | Local login (argon2), OIDC SSO (Authlib), rate limiting (Redis + memory fallback), admin seeding |
| `authz.py` | **Scoped RBAC evaluator** — pure function over Postgres grants; 14 built-in roles, 4 scope kinds (fleet, minion, prefix, delegate) |
| `jobs.py` / `jobs_service.py` / `jobs_helpers.py` | Job launch, confirm gates, batch waves, orchestrate, live SSE stream, sync from returner |
| `tasks.py` + `tasks_*.py` | RQ background tasks: inventory refresh, capability probes, SLS preview, batch/orchestrate executors |
| `fleet.py` | Fan-out publish to all master pods with same JID (core HA invariant) |
| `keys.py` | Key accept/reject/delete fanned to all master pods; union roster display |
| `minions.py` / `minions_helpers.py` | Inventory snapshot, grains cache, presence, conformity |
| `files.py` / `git_sync.py` | File browser (Wunderbaum tree), editor (CodeMirror), git sync/commit/push |
| `masterconfig.py` | Master ConfigMap edit (PUT-replace with resourceVersion), history snapshots, rollout |
| `reactor.py` / `pillar.py` / `mine.py` / `schedules.py` | Domain-specific CRUD + read views |
| `settings.py` | DB-backed settings (theme, defaults, OIDC fallback) |
| `audit.py` | Audit event logging (denies, actions, outcomes) |
| `inventory.py` / `groups.py` / `states.py` / `events.py` / `console.py` / `users.py` / `dashboard.py` / `api.py` | Feature blueprints |

### State Management
- **Postgres (SQLAlchemy 2.0 + Alembic):** Users, jobs/returns, minion snapshots, grants, audit, settings, saved jobs, API tokens
- **Redis:** RQ queues, session cache, capability cache, SSE pub/sub for live job updates
- **Salt master (external):** Source of truth for keys, grains, job execution, events; app caches snapshots only

---

## 3. Master Configuration

### Config Files
| File | Purpose |
|------|---------|
| `pyproject.toml` | Build config, deps, pytest/ruff/coverage settings |
| `requirements.lock` | Pinned runtime snapshot (regenerated after deliberate upgrades) |
| `package.json` / `package-lock.json` | npm build deps (tailwind, daisyui, codemirror, esbuild) |
| `.env.example` | Dev defaults for all env vars |
| `alembic.ini` | Migration config (DATABASE_URL from env) |
| `alembic/env.py` | Reads DATABASE_URL for autogenerate |
| `compose.yml` | Dev stack: app, worker, salt-master, postgres, redis |
| `Containerfile` | Multi-stage: node (CSS build) → python (app image) |
| `Containerfile.salt-master` | Salt master image (pinned digest + psycopg2 + cluster patch) |
| `cluster-entrypoint.sh` | Pod-aware entrypoint: stable DNS identity, Raft membership, peer key sync |

### Environment Variables (from `.env.example` + compose)
**Required in production (no defaults):**
- `SECRET_KEY` — Flask session signing
- `DATABASE_URL` — Postgres connection
- `REDIS_URL` + `REDIS_PASSWORD` — Authenticated Redis (RQ pickles payloads)
- `SALT_API_URL`, `SALT_EAUTH_USER`, `SALT_EAUTH_PASSWORD` — salt-api credentials
- `SALT_API_VERIFY_CA` — Path to CA cert or `false` (dev only)

**Optional (SSO):**
- `OIDC_ISSUER`, `OIDC_CLIENT_ID`, `OIDC_CLIENT_SECRET`, `OIDC_GROUPS_CLAIM`
- `OIDC_ADMIN_GROUPS`, `OIDC_OPERATOR_GROUPS`

**Kubernetes-specific (in `deploy/kubernetes/app.yaml`):**
- `TRUST_PROXY=1` — Behind Caddy/Traefik
- `FILE_ROOTS=/srv/states/salt`, `REACTOR_ROOTS=/srv/states/reactor`
- `TLS_CERT`, `TLS_KEY` — Mounted certs for HTTPS

### Secrets Handling
- **No secrets in repo** — all via env or Kubernetes Secrets
- **Kubernetes:** 5 owner-held Secrets created manually before apply:
  - `overstate-secrets` (SECRET_KEY, ADMIN_PASSWORD, REDIS_PASSWORD, SALT_API_USER_PASS)
  - `salt-master-keys` (master.pem/pub — shared master identity)
  - `salt-master-cluster` (cluster.conf with cluster_secret)
  - `salt-master-cluster-keys` (cluster.pem/pub — pinned cluster identity)
  - `salt-master-db` (returner.conf with PG password)
- **CNPG** manages `overstate-db-app` / `overstate-db-superuser` passwords
- **ConfigMap `salt-master-config`** is UI-editable; **never holds passwords** (enforced by test)

---

## 4. User Interface (overstate_ui)

### Stack
- **Framework:** Flask + Jinja2 templates (server-rendered)
- **Frontend:** DaisyUI 5 + Tailwind 4 + HTMX 2 + Alpine.js (vendored in `static/`)
- **Editor:** CodeMirror 6 (YAML mode) via esbuild bundle (`editor.bundle.js`)
- **File Tree:** Wunderbaum (`browser.bundle.js`)
- **Icons:** @iconify (lucide + simple-icons for OS brands)
- **Themes:** light / dark / wireframe (daisyUI themes)

### Structure
```
overstate_ui/
├── static/
│   ├── app.css              # Built from assets/app.css (tailwind + daisyui)
│   ├── htmx.min.js          # Vendored
│   ├── alpine.min.js        # Vendored
│   ├── editor.bundle.js     # CodeMirror 6 + YAML
│   ├── browser.bundle.js    # Wunderbaum file tree
│   └── overstate.svg
├── templates/
│   ├── base.html            # Layout, nav, command palette, HTMX error handling
│   ├── _confirm.html        # Type-to-confirm modal (included in base)
│   ├── *.html               # 50+ feature templates (jobs, minions, keys, etc.)
│   └── _*.html              # HTMX partials (_job_rows, _minion_rows, etc.)
├── assets/
│   ├── app.css              # Tailwind 4 + daisyUI + custom token bindings
│   ├── editor.js            # CodeMirror mount entry
│   └── browser.js           # Wunderbaum tree entry
└── *.py                     # 35 blueprint/service modules
```

### API Client Layer
- **No separate API client** — `SaltClient` in `salt_client.py` is the only HTTP client to salt-api
- **Internal API:** `/api/jobs/run` (POST, Bearer token) for CI; rate-limited, pinned to saved job or read-class calls
- **SSE:** `/jobs/<jid>/stream` for live job updates (Redis pub/sub fed by salt-api `/events`)

---

## 5. Deployment Documentation

### Compose (Local Dev)
- `compose.yml` — 5 services: app, worker, salt-master, postgres:16, redis:7-alpine
- `scripts/dev-up.sh` — cert gen, compose up, postgres wait, TLS enable
- `scripts/dev-down.sh` / `dev-rebuild.sh` / `seed-mock.sh` — lifecycle helpers

### Container Images
| Image | Base | Notes |
|-------|------|-------|
| App/Worker | python:3.14-slim | Multi-stage CSS build; git + openssh-client for Files page |
| Salt Master | ghcr.io/cdalvaro/docker-salt-master:lts@sha256:df5bb4... | Pinned digest; psycopg2-binary; cluster identity patch |

### Kubernetes (Production) — `deploy/kubernetes/`
| Manifest | Purpose |
|----------|---------|
| `kustomization.yaml` | Namespace `overstate`, image tag transformer (`overstate-app` → `quay.io/sigaint/overstate:pair23`) |
| `namespace.yaml` | Namespace |
| `rbac.yaml` | Namespace-scoped Role (PUT ConfigMaps, restart StatefulSet) |
| `app.yaml` | Deployment ×2, Service, PDB (minAvailable: 1) |
| `worker.yaml` | Deployment ×1 |
| `salt-master.yaml` | StatefulSet ×3 (OrderedReady), headless Service, API Service (ClientIP), MQ Service (Local), PDB (minAvailable: 2) |
| `salt-master-config.yaml` / `salt-master-config-history.yaml` | Owned ConfigMap + history (20 revs) |
| `salt-seed.yaml` | ConfigMap with initial file roots |
| `volumes.yaml` | `srv-data` (RWX 10Gi), `redis-data` (RWO 5Gi), PVC templates for master keys |
| `postgres.yaml` / `redis.yaml` | CNPG cluster (instances: 1) + Redis Deployment |
| `reconcile-cronjob.yaml` | Hourly key reconciliation (Forbid concurrency) |
| `salt-mq-routes.yaml` | Traefik TCP routes for 4505/4506 (nativeLB) |
| `ingress.yaml` | Traefik IngressRoute + cert-manager Certificate |

### Documentation
| File | Scope |
|------|-------|
| `docs/guides/install-kubernetes.md` | Step-by-step: secrets → apply → verify → restore runbook |
| `docs/reference/architecture-kubernetes.md` | Design, topology, data flows, assumptions, risks (§8), alternatives |
| `docs/guides/developer.md` | Conventions, tests, lint, coverage, deps |
| `docs/guides/user.md` / `docs/guides/admin.md` | Operator & admin guides |
| `docs/reference/rbac.md` / `docs/reference/sso.md` / `docs/reference/tls.md` | Feature-specific |
| `docs/plans/*.md` | 20+ feature design docs (reactor, groups, batches, etc.) |
| `docs/history/v3.md` – `docs/history/v3.9.md` | Versioned release notes |

### Cluster Scripts
- `cluster-entrypoint.sh` (492 lines) — Pod DNS identity, Raft membership, peer key sync, self-healing watcher
- `cluster-peers.py` / `cluster-ready.py` — API-backed peer discovery + readiness probe
- `salt-cluster-identity-patch.py` — Build-time Salt patch for `cluster_node_id` (fails on anchor drift)

---

## 6. Issues Found

### Critical (Production Blockers)
| # | File/Location | Issue | Severity |
|---|---------------|-------|----------|
| 1 | `deploy/kubernetes/app.yaml:77-78` | `SALT_API_VERIFY_CA: "false"` — in-cluster salt-api TLS unverified; any pod compromise/CNI sniff yields salt-api password | **Critical** |
| 2 | `deploy/kubernetes/salt-master.yaml:61` | Master image uses floating tag `:lts-pg14` not digest; redeploys may pull different image | **Critical** |
| 3 | `deploy/kubernetes/kustomization.yaml:50` | App image tag `pair23` hardcoded; no digest pinning | **Critical** |
| 4 | `overstate_ui/config.py:34` | `SALT_EAUTH_PASSWORD` defaults to `""` — missing password fails silently at runtime | **High** |
| 5 | `docker-entrypoint.sh:10-12` | Migration fallback stamps `95ffc8849775` (salt_events) hardcoded; breaks if baseline revision changes | **High** |

### High (Reliability / Security)
| # | File/Location | Issue | Severity |
|---|---------------|-------|----------|
| 6 | `architecture-kubernetes.md:222-226` | Postgres is `instances: 1` — shared job cache HA weaker than master trio | **High** |
| 7 | `architecture-kubernetes.md:250-252` | Redis single replica — full UI outage on Redis volume issue | **High** |
| 8 | `architecture-kubernetes.md:294-297` | Owner-held secrets have no backup; loss = fleet re-enrollment or full regen | **High** |
| 9 | `compose.yml:52` | Dev compose uses `SALT_API_VERIFY_CA: /srv/tls/ca.crt` but salt-master cert is self-signed; dev TLS chain incomplete | **High** |
| 10 | `overstate_ui/auth.py:106-107` | OIDC issuer must be `https://` but no validation of `OIDC_REDIRECT_URI` host against `PUBLIC_URL` | **High** |

### Medium (Maintainability / Operations)
| # | File/Location | Issue | Severity |
|---|---------------|-------|----------|
| 11 | `.github/` | No CI workflow (tests, lint, build, container scan) — only Dependabot | **Medium** |
| 12 | `Containerfile.salt-master:6` | Base image digest pinned but no Renovate/Dependabot for docker ecosystem (named `Containerfile*`) | **Medium** |
| 13 | `overstate_ui/authz.py:28` | `ENFORCEMENT_COMPLETE = True` hardcoded constant — rollback via flag row only, not code | **Medium** |
| 14 | `overstate_ui/tasks_queue.py` | `JOB_TIMEOUT = 1800` (30 min) hardcoded; orchestrate can exceed | **Medium** |
| 15 | `deploy/kubernetes/salt-master.yaml:147-152` | Readiness probe checks TCP 4507 on `$POD_IP` — fails if pod IP not ready; no DNS fallback | **Medium** |

### Low (Hygiene / Tech Debt)
| # | File/Location | Issue | Severity |
|---|---------------|-------|----------|
| 16 | `pyproject.toml:10-25` | Dependencies use `>=` lower bounds; `requirements.lock` pins but no `pip-tools`/`uv` workflow documented | **Low** |
| 17 | `overstate_ui/__init__.py:109-114` | CSP includes `'unsafe-inline'` for scripts/styles — required for HTMX/Alpine inline but weakens CSP | **Low** |
| 18 | `overstate_ui/salt_client.py:53` | `verify=config.SALT_API_VERIFY` accepts `bool | str`; `str` path not validated exists | **Low** |
| 19 | `scripts/gen-dev-certs.sh` | Not reviewed — generates self-signed CA for dev; cert lifetime/hardcoded values unknown | **Low** |
| 20 | `overstate_ui/models.py:331-332` | `SaltJid` / `SaltReturn` / `SaltEvent` models mirror upstream pgjsonb returner; app never writes but reads — coupling risk if schema drifts | **Low** |

---

## Summary

**Overstate** is a well-structured Flask control plane for Salt with:
- **Clean architecture:** Clear separation (SaltClient only salt-api caller, authz pure evaluator, tasks split by domain)
- **Strong RBAC:** Scoped grants model with 14 roles, 4 scope kinds, delegation rules, IdP mapping
- **Production-grade K8s manifests:** Kustomize, namespace-scoped, owner-held secrets, PDBs, ordered StatefulSet rolls
- **Comprehensive docs:** Install, architecture, risks, runbooks, feature plans
- **Good test hygiene:** 65 test files, SQLite in-memory, production code paths for seeding

**Top risks to address before production:**
1. **Fix `SALT_API_VERIFY_CA=false`** — pin salt-api CA or use cert-manager issued certs
2. **Pin all container images to digests** (app, worker, salt-master)
3. **Add CI pipeline** (tests, lint, container build/scan)
4. **Address single-instance Postgres/Redis HA gap** (CNPG replica, Redis Sentinel/Cluster)
5. **Document secret backup procedure** for owner-held Secrets