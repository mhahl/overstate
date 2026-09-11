## Goal

Deliver the Overstate v2+ scope from `PLAN.md`: multi-user roles (OIDC), a read-only SLS/pillar browser over git-synced roots, package/service/process/restart fleet workflows, syndic/proxy-minion/salt-ssh coverage, and a hardened channel (TLS only, no mTLS) — without breaking the v1 contract (salt-api stays the only control plane, every mutation keeps a JID + audit row).

## Success Criteria

- A second login backend (corporate directory or OIDC provider) authenticates a non-admin user; an operator and a viewer see different UI capabilities enforced server-side.
- Any authenticated user can browse SLS/pillar files read-only from the UI served from git-synced roots; no role can edit or apply from the UI.
- Fleet workflows (package install, service restart, process signal, minion restart) run through confirm-gated actions with JIDs and history.
- Proxy minions and a syndic topology render correctly; salt-ssh targets run with documented limits.
- Browser↔Overstate and Overstate↔salt-api run over TLS with certificates; no mTLS (tokens + network policy).

## Context And Current Facts

- Codebase is ~1600 lines, 9 blueprints (`overstate_ui/`), `tests/` has 36 passing tests; v1 acceptance passed end to end.
- `overstate_ui/auth.py`: single local-admin model — `User` has no role column, one argon2 hash, seed-only-when-empty, in-process rate limiter, Flask-Login session. All blueprints use bare `@login_required` with no role concept.
- `overstate_ui/__init__.py`: one shared service `SaltClient` in `app.extensions` built from `SALT_EAUTH_*` env; every blueprint calls it. No per-user eauth exists anywhere.
- `overstate_ui/audit.py:log_event` already records user/action/JID for mutating actions; new surfaces reuse it.
- `salt-config/api.conf` holds the tight D3 allowlist; any new Salt function the UI needs must be added there first.
- v1.1 items (nodegroup targets, grain compare, schedule-add, JID copy) are assumed done first; they need no architectural changes.

## Constraints And Non-goals

- Constraints: keep three-container shape (+ dev salt-master); env-only secrets; CSRF on all POSTs; Postgres snapshots, Redis pub-sub only; gunicorn serving.
- Non-goals for v2 (Final): per-user Salt eauth identity mapping (app-level RBAC only — one service identity retained); in-app editing or sync triggers (git is edited outside, deployment syncs the checkout); salt-ssh roster editing; multi-master (syndic read paths only).

## Key Decisions

- **Identity: OIDC-only via Authlib (Final).** Authlib's Flask client covers OAuth 2.0 + OpenID Connect behind one registry API ([docs](https://docs.authlib.org/en/v0.15.1/client/flask.html)); any provider works through its discovery URL, so no IdP choice is baked in. LDAP backend dropped per owner decision — no password-bind code path. Rejected: Flask-OAuthlib (its own PyPI page directs users to Authlib instead) and one-provider hardcoding (would re-litigate the IdP per deployment).
- **Roles: `viewer` / `operator` / `admin` column on `users`, enforced by a decorator (Final)**, not by hiding buttons alone. Viewer reads everything, changes nothing; operator runs jobs/keys/schedules/edits; admin manages users + settings. Rejected: Salt-side per-user eauth (multiplies allowlists and token handling for no v2 need — service identity stays).
- **Editor: read-only browser over git-synced roots (Final).** SLS/pillar files live in git; a deployment-managed checkout syncs them to the configured roots path and the app only reads. No in-UI editing, no write path, no backup or dry-run gate in the app. Rejected: in-app editing with backup, and an in-app sync trigger (would add a mutation surface outside salt-api).
- **Fleet workflows: presets + typed-confirm, reusing the jobs runner.** Destructive actions (service restart, minion restart, package changes) require typing the target, then flow through `launch()`/`sync_job`/SSE unchanged. No new execution path.
- **Syndic/proxy/ssh: adapt, don't rebuild.** Proxy minions already appear in key/minion lists (grains differ — handle missing keys gracefully); syndic topologies need key-roster awareness per master (read-only notes); salt-ssh runs through the `ssh` client with documented latency limits. Roster editing stays out.
- **Channel: TLS everywhere, no mTLS (Final).** salt-api TLS is `rest_cherrypy` `ssl_crt`/`ssl_key` ([docs](https://docs.saltproject.io/en/latest/ref/netapi/all/salt.netapi.rest_cherrypy.html)); the client switches to `verify=` a CA bundle and dev keeps `disable_ssl`. No client-certificate work — tokens + network policy are the authentication story.

## Recommended Approach

Five units in dependency order (Final): identity + RBAC first (fleet confirm-gating needs the operator role), then the read-only file browser, then fleet workflows (typed-confirm pattern originates here), then topology adaptations, then channel hardening last (touches deployment, verify each prior unit still passes after). Each unit lands behind the existing test + screenshot discipline.

## Work Plan

1. **Identity + RBAC.** `role` column + migration; Authlib OIDC backend (issuer/client-id/secret from env, callback route, JIT user provisioning as viewer); `roles_required` decorator replacing bare `login_required` on mutating routes; admin user-management page; tests for OIDC backend with fakes + role matrix. No LDAP code path.
2. **SLS/pillar browser (read-only).** `files` service (read-only under configured roots, path-allowlisted); browser page with syntax-highlighted view of states/pillars; no edit, dry-run, or apply in the app; deployment-managed git checkout keeps roots fresh (compose mount + dev sync script); viewer may read, no role gets a write path (test).
3. **Fleet workflows.** Preset pack (pkg.install/remove, service.restart/status, process signal, test.ping, mine update, minion restart) with typed-confirm interstitial; all through existing `launch()`; history/detail/SSE unchanged.
4. **Topology adaptations.** Proxy-minion grain gaps handled in list/detail/CSV; syndic key-roster read notes per master; salt-ssh `ssh`-client support with timeout + limits documented; eauth allowlist extended only as needed.
5. **Channel hardening.** Compose + dev-master TLS certs (self-signed dev CA), `verify=` CA bundle in `SaltClient`, `disable_ssl` dev-only; docs update. No mTLS work.

## Validation Plan

- `pytest` per unit (role matrix, OIDC fakes, browser read-only + path-traversal rejection, confirm flow, topology gaps).
- Live pass per unit against the dev stack: OIDC against a throwaway provider (or documented manual flow), file browser rendering `salt-srv/demo.sls` from the synced checkout, fleet preset on `dev-minion-01`, TLS verified by `curl` against both endpoints with the CA bundle.
- Highest-risk step: unit 1 identity migration + decorator swap — every existing test touches auth; run the full suite plus a manual login of each role.
- Screenshot discipline from v1 continues for every new page.

## Risks / Rollback

- **OIDC provider variance** (claim names, discovery quirks): mitigate with env-mapped claim paths; rollback is config-only (back to local-admin).
- **Stale file-roots checkout**: mitigate with a visible sync-revision indicator on the browser page; fix is re-running the deployment sync (no app rollback needed — app holds no write state).
- **Allowlist drift** (new functions 403 at eauth): surfaced by existing error banners; fix is `salt-config/api.conf` + master restart.
- **Migration risk on `users`**: additive `role` column with default `viewer`, existing admins backfilled to `admin`; `alembic downgrade` per migration.

## Open Questions

## Grill Status (Final — accepted by owner)

Settled (Final): identity — OIDC-only, LDAP dropped; roles — viewer/operator/admin, decorator-enforced; Salt identity — single service identity, no per-user eauth; file browser — read-only over deployment-managed git checkout, no in-app edit or sync; channel — TLS only, no mTLS; non-goals — roster editing, multi-master, in-app writes all stay out.
Unresolved: none. Owner accepted the scope contract (goals, non-goals, decisions, constraints, validation) — all decisions below are Final. Scope contract: deliverable is this plan plus the five implementation units in `overstate_ui/`; out-of-scope artifact classes are LDAP/mTLS code, in-app file writes, roster editing, and multi-master support. Done means per-unit pytest + live dev-stack pass + screenshots. Later stages return for their own approval; "go" authorizes only the accepted boundary.

## Sources

- [Authlib Flask OAuth Client](https://docs.authlib.org/en/v0.15.1/client/flask.html)
- [Salt rest_cherrypy netapi docs](https://docs.saltproject.io/en/latest/ref/netapi/all/salt.netapi.rest_cherrypy.html)
- [Flask-OAuthlib PyPI notice](https://pypi.org/project/Flask-OAuthlib/)
