## Status

Superseded in mechanism (2026-09-15): the project pivoted to Kubernetes —
master configuration is owned via ConfigMaps and restarts happen through
K8s RBAC (`deploy/kubernetes/`), not SSH plus host files. The safety
invariants below (admin-only, blocking validation, refusal-first,
audit-everything) carry over; the SSH/host-file units do not.

## Goal

Let Overstate manage the Salt master configuration: browse, edit, validate, and stage every file under the master's config directory, plus restart the master from the UI when SSH is configured. Salt states stay in git under `FILE_ROOTS`; master config becomes git-backed too, with one commit per save for history and rollback.

## Success Criteria

- An admin can browse every file under `/etc/overstate/salt-config` (prod) from a Master Config page, open any UTF-8 text file in the CodeMirror editor, and save: invalid YAML is blocked (not advisory), the save is one git commit touching only that file, and every outcome is audited.
- A stale-base save refuses with the current revision and writes nothing, exactly like state-file saves.
- An admin Restart button restarts the master over SSH when configured, polls salt-api back to healthy, and audits the result; without SSH configured (dev compose included) it refuses with the reason and shows the manual command instead.
- Operators and viewers see no Master Config page and get 403 on forged POSTs.
- `.venv/bin/pytest -q` green; existing Files/reactor contracts untouched.

## Context And Current Facts

- Prod master is the cdalvaro container; its config is the host dir `/etc/overstate/salt-config`, copied once from repo `salt-config/` on install (`scripts/install.sh:86-88`) and mounted read-only into the container (`deploy/quadlet/overstate-salt-master.container:22`). The app container has no mount for it today.
- Config changes need a master restart today: `install.sh:213-219` restarts `overstate-salt-master.service` after salt-config changes, with the comment "Salt reads its config once at startup".
- The reactor mapping (`reactor:` config section) is already managed through salt-api runner calls (`overstate_ui/reactor.py:1-7`, `reactor.list/add/delete`); SLS bodies under `REACTOR_ROOTS` are read-only in the UI. `docs/deployment.md:165-169` states mapping writes land in master config, not git.
- `REACTOR_ROOTS` is set only in dev compose (`compose.yml:25`); `install.sh`/`overstate.env` never set it, so in prod it falls back to the relative default from `overstate_ui/config.py:45` instead of `/srv/states/reactor`. Any reactor-file scope must fix that first.
- Reusable machinery (verified this run): `roles_required` hierarchy (`overstate_ui/auth.py:52-62`), `log_event` audit (`overstate_ui/audit.py:12-17`), edit/save/commit patterns with `safe_join`, `read_text`, `MAX_BYTES`, base-SHA/hash stale checks (`overstate_ui/files.py`), fixed-argv git helpers with single-flight lock and fixed failure words (`overstate_ui/git_sync.py: git_commit_file`, `_single_flight`, `_failure` pattern), vendored CodeMirror bundle loaded only on edit pages, `THIRD-PARTY-LICENSES.md` record.
- The app image already ships `openssh-client` (for push deploy keys); no new package needed for SSH restart. The eauth grant list (`salt-config/api.conf`) covers minion-side `service.*` only — nothing restarts the master over salt-api, so restart needs the SSH path.
- Owner decisions settled this run: scope = whole salt-config directory (including `api.conf`/`returner.conf`, lockout risk accepted); restart = UI button over SSH mirroring the push deploy-key pattern; roles = admins only.

## Constraints And Non-goals

- Admin-only UI and routes (`roles_required("admin")`); viewers/operators get no links and 403s server-side. CSRF on every POST, audit row per outcome, same as Files.
- Whole directory means `/etc/overstate/salt-config` only. `/etc/overstate/tls` is a separate mount and stays untouched; minion configs, `/etc/salt/master` on bare-metal masters, and multi-master setups are out of scope (one Overstate per master, per topology).
- Invalid YAML blocks the save with the parse error — advisory is wrong here because a broken master config stops the master from starting and Salt never gets to be truth. Non-YAML text files (certs pasted as text, READMEs) save without the YAML check but keep every other guard.
- Serialized git discipline inherited from `git_sync.py`: fixed argv, no shell, no user flags/refs, bounded timeout, shared single-flight lock, fixed failure words (SSH stderr carries hostnames — logged server-side, never rendered).
- Never `--force`. Edit-existing-only in v1: no create/delete/rename from the UI. New drop-ins arrive via git or by hand, keeping the blast radius at the settled scope.
- Restart runs exactly one fixed host command over SSH with `BatchMode=yes`, strict host checking, and a pinned `known_hosts` — no user-supplied host, command, or flags. A separate SSH key from the git deploy key (different trust scope).
- Non-goals: minion-config management, TLS/PKI management, multi-master, SIGHUP hot-reload (full restart only), in-editor Salt execution, collaborative editing, auto-restart after save (restart is always an explicit second click).

## Key Decisions

- **Mount the config dir writable, reuse the Files machinery.** Add `/etc/overstate/salt-config:/srv/master-config:rw` (quadlet) plus the dev-compose equivalent against repo `salt-config/`. New `masterconfig` blueprint reuses `safe_join`/`read_text`/stale-check/CodeMirror patterns; no second editor layer. Rejected: managing via salt-api (no API writes arbitrary master files), and a sidecar agent (new service for a solved mount problem).
- **Git-init the config dir; commits are the backups.** `install.sh` initializes `/etc/overstate/salt-config` as a local git repo (no remote required) with the seed files committed; docs cover existing hosts (`git init`, add, commit). Every UI save is one commit via the existing `git_commit_file` helper, so rollback is `git revert` + restart, and the Files audit pattern carries over. Rejected: `.bak` sidecar files (a second truth competing with git) and plain files with no history (unrevertable by construction).
- **Whole directory, lockout risk owned explicitly.** `api.conf` and `returner.conf` are editable; a bad `api.conf` can lock the UI out of salt-api. Mitigations: blocking YAML validation, commit-per-save (revert path), a warning banner on auth-adjacent files, and docs requiring console access before editing them. Rejected: allowlisting only "safe" files (contradicts the settled whole-directory scope).
- **Restart over SSH with a dedicated key, refusal-first.** `GIT_SSH_COMMAND`-style env (`MASTER_SSH_COMMAND`) plus key at `/etc/overstate/ssh/master-restart-key` and pinned host key; the route runs only `systemctl restart overstate-salt-master`, then polls salt-api healthy with a bounded timeout. Unconfigured SSH (dev compose, missing key/host) refuses with the manual command shown. Rejected: password SSH (hangs, un-auditable), reusing the git deploy key (different trust scope), podman-socket mount (container escapes its sandbox), and auto-restart-on-save (surprise outages).
- **Fix `REACTOR_ROOTS` in prod as part of plumbing.** `install.sh` writes `REACTOR_ROOTS=/srv/states/reactor` into `overstate.env` so reactor SLS bodies resolve to the shared checkout; without this the reactor-file scope edits the wrong directory. Rejected: leaving the fallback (silently wrong target).
- **Reactor SLS bodies join the admin gate; mapping stays on the runner.** File bodies become editable under the same admin-only rules; the event→SLS mapping keeps using `reactor.add/delete/list` (live master truth, already audited). Rejected: rewriting the mapping into files (two writers on one truth).
- **Host reachability is an explicit assumption, validated in unit 4.** The container is assumed to reach the host SSH via a documented gateway address; if the assumption fails in implementation, the restart route keeps the refusal path and docs carry the manual command — the file-management value never depends on it.

## Recommended Approach

One track, five ordered units, each independently revertable, no DB migration. Plumbing first (mounts, env, git-init — all deploy artifacts, provable without UI), then the admin-only browser/editor reusing v4 patterns, then validation hardening (blocking YAML), then the SSH restart button, then docs and audit polish. The restart unit lands late on purpose: files must be safe before a button can restart the master onto them.

## Work Plan

1. **Plumbing: mounts, env, git-backed config dir.** Quadlet `:rw` mount for salt-config + dev-compose equivalent; `REACTOR_ROOTS=/srv/states/reactor` into `install.sh` env generation; `install.sh` git-inits `/etc/overstate/salt-config` with seed commit (idempotent, skipped when already a repo); docs note for existing hosts. Tests: extend `tests/test_deploy.py` (mount lines, env line, git-init idempotence via script dry-run or assertion on installer text).
2. **Master Config browser + editor (admin-only).** New blueprint (`/master-config`): listing (reuse `list_tree` shape against the new roots), view, edit (CodeMirror bundle, textarea fallback), save (UTF-8 + size gate, traversal 404s, binary/oversize refused, identical-content no-op, stale-base refusal, blocking YAML validation for `*.conf`/`*.yaml`/`*.yml`, one commit per save via `git_commit_file`, audit `masterconfig-save:<rel>:<sha>` / refusals). TLS-adjacent nothing (separate mount, unreachable). Tests: matrix mirroring `tests/test_files.py` (RBAC 403s, traversal, stale, invalid-YAML blocked with file untouched, single-path commit, audit rows).
3. **Blocking validation + pre-restart safety.** Save-time YAML gate with the parse error shown; warning banner when editing `api.conf`/`external_auth` (lockout risk restated at the point of action). Outside edits (install.sh, hand edits) stay legitimate — the stale check covers races without forbidding them. Tests: invalid YAML matrix (bad indent, tabs, empty file saves fine), banner presence on auth files.
4. **SSH Restart button (admin-only).** Docs-first key setup (`/etc/overstate/ssh/master-restart-key`, pinned host key, `MASTER_SSH_COMMAND` in `overstate.env`, host-side `authorized_keys` command restriction noted); `POST /master-config/restart` runs the fixed command under timeout + single-flight, polls salt-api healthy, flashes outcome, audits `master-restart:<result>`; refusals (no SSH configured, SSH failure, unhealthy-after-timeout with restore-and-restart procedure linked). Assumption check: container→host SSH reachability proven here or docs keep the manual path. Tests: stubbed-SSH refusal/success matrix (monkeypatched runner, no real sshd), operator-403, audit rows, no stderr/hostname leakage in responses.
5. **Reactor files under the admin gate + docs/audit polish.** Reactor SLS bodies editable via the same editor (admin-only; v4 operator SLS editing unchanged); mapping stays on the runner. `docs/user.md` (Master Config section: scope, blocking validation, stale flow, restart semantics), `docs/deployment.md` (mount chain, git-init, SSH key setup, restore procedure), `docs/install-opensuse.md` (key placement checklist). Verify every new mutation logs.

## Validation Plan

- `.venv/bin/pytest -q` green after every unit; focused: new `tests/test_master_config.py` (browser/editor/RBAC/validation matrix), `tests/test_deploy.py` (mounts, env, installer), plus frozen `tests/test_files.py`, `tests/test_git_sync.py`, `tests/test_reactor.py` green untouched.
- Live on the dev stack: point the new roots at a scratch checkout, edit a drop-in, confirm one single-path commit; save invalid YAML and confirm blocked with file untouched; save with a raced external edit and confirm stale refusal; configure SSH to a test target (or leave unconfigured) and confirm restart-button success/refusal plus audit rows; restart and confirm salt-api health returns.
- Refusal sweep: `api.conf` shows the lockout banner; non-checkout roots degrade; viewer/operator forged POSTs 403; SSH stderr never reaches the page.
- Highest-risk check: restart onto a config that fails to start the master — prove the health-timeout path fires, the audit names the outcome, and the documented restore (revert + restart) recovers, because a silent failed restart leaves the fleet unmanaged with a green-looking UI.

## Risks / Rollback

- Lockout via `api.conf`/eauth edit (accepted risk): mitigated by blocking validation, per-file commits, point-of-action banner, and docs requiring console access; recovery is revert + restart from the host.
- Master fails to start after restart: mitigated by bounded health poll, explicit unhealthy outcome (never reported as success), and the revert-and-restart runbook; no auto-rollback (automation cannot be trusted with a down master — a human confirms).
- SSH key exposure: root-only key dir, `:ro` mount, dedicated key (not the git deploy key), `BatchMode` + strict host checking, stderr hygiene; rotation = replace key + restart app.
- Scope creep into TLS/minions: the roots physically exclude them (separate mounts); tests assert unreachable paths 404.
- Rollback: each unit reverts independently (`git revert`); no migration to unwind; removing the feature is unmounting `:rw`→`:ro` plus deleting the blueprint registration, after which hand/git management keeps working.

## Open Questions

None. Scope (whole salt-config dir), restart (UI button over dedicated-key SSH), and roles (admins only) were settled by the owner this run; host gateway reachability is a validation step with a designed fallback (refusal + manual command), not a question.

## Sources

None. No external claims inform this plan; all evidence is workspace code, tests, docs, installer scripts, and owner answers cited above.
