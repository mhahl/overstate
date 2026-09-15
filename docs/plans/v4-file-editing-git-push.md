## Status

Final — accepted by the owner 2026-09-15 in this session (quoted acceptance: `accept`). Staged implementation still needs a separate explicit request.

Scope note: this record covers the v4 decision set only. Accepting it does not approve implementation or any later stage; each stage returns separately.

## Grill decisions (Draft)

- G1 (settled 2026-09-15): online editor is CodeMirror 6, built once and vendored under `overstate_ui/static/` — no CDN. Owner accepted the recommendation.
- G2 (settled 2026-09-15): editor ships in the first unit — edit view, CodeMirror bundle, and save→commit land together. Owner overrode the recommendation (textarea-first split); consequence: unit 1 carries the npm/bundler supply chain, and its validation must prove the textarea fallback saves with the bundle absent.
- G3 (settled 2026-09-15): invalid YAML shows an advisory warning; the save still proceeds. Salt is truth at apply time. Owner accepted the recommendation.
- G4 (settled 2026-09-15): push access comes from a deploy key on the checkout's remote, set up per `docs/deployment.md`. No credential UI, nothing in the DB. Owner accepted the recommendation.
- G5 (settled 2026-09-15): stale-base saves refuse with the current SHA plus a reload-and-reapply hint; nothing is written or committed. No merge UI. Owner accepted the recommendation.

## Scope contract (accepted 2026-09-15)

Boundary: this decision record only. Out of scope: runtime code, tests, migrations, PRs, commits. Owner reply `accept` this session. Later stages need their own interview; execution words never widen this boundary.

## Goal

Ship v4: edit state files in the browser and push them to git from the UI. Saving a file writes it to the `FILE_ROOTS` checkout and creates a local git commit; a separate admin-gated Push button sends committed work upstream. Editing happens in an embedded online code editor component (CodeMirror 6, vendored — an external dependency is explicitly authorized for this); a plain textarea remains as the no-JS fallback. Every mutation is role-gated, CSRF-protected, single-flight, and audited. Salt stays the source of truth for applies — saving never runs `state.apply`.

## Success Criteria

- An operator or admin can open any editable text file in the Files browser, save changes, and see the new local SHA; each save is exactly one local commit touching only that file.
- The edit page renders an online editor with line numbers and YAML highlighting for `.sls`/`.yaml`/`.yml` (plain text otherwise); if the editor bundle fails to load or JS is off, the textarea fallback still saves correctly.
- A separate Push action sends committed work upstream; it is visible and usable by admins only, refuses (never forces) on diverged / dirty / no-upstream / missing-credentials states with the reason shown, and logs an audit row either way.
- A concurrent edit can never silently overwrite another: saving against a stale base refuses with an explanation and nothing is committed.
- Viewers change nothing: edit UI is hidden from them and forged save/push POSTs are rejected server-side (403).
- Traversal, binary, and oversize files stay uneditable; git subprocesses keep fixed argv, no shell, bounded timeout, and remote URLs/tokens never reach the UI.
- `.venv/bin/pytest -q` green, including the existing read-only/traversal and ff-only-sync contracts.

## Context And Current Facts

- The Files browser is deliberately content-read-only today (`overstate_ui/files.py:1-2`): flat capped listing (`LIST_LIMIT = 5000`, `files.py:32-35`), `safe_join` traversal guard (`files.py:71-80`), 256 KiB + UTF-8 gate (`read_text`, `files.py:126-136`), revision SHA via parent-walk (`sync_revision`, `files.py:139-158`), and a "Read-only" banner (`overstate_ui/templates/file_view.html:9`).
- Git from the UI is status + `fetch` + `pull --ff-only` only (`overstate_ui/git_sync.py:1-11,132-171`): fixed-argv subprocesses with no shell and no user input (`_run`, `git_sync.py:46-58`), 30 s timeout (`GIT_TIMEOUT = 30`), one-at-a-time lock (`_single_flight`, `git_sync.py:28-39`), and failure words that never render git stderr so remote URLs with embedded tokens stay server-side (`_failure`, `git_sync.py:107-129`). Sync (`files.py:214-243`) and fetch (`files.py:246-268`) are operator-gated; a changed pull triggers an advisory `fileserver.update` (`_refresh_fileserver`, `files.py:200-211`).
- Roles are viewer < operator < admin with higher levels implying lower ones (`overstate_ui/auth.py:3-6,52-62`); every mutation logs via `log_event(user, action)` (`overstate_ui/audit.py:12-17`). `FILE_ROOTS` defaults to `salt-srv/salt` (`overstate_ui/config.py:44`); compose already mounts the checkout writable into the app (`./salt-srv:/srv/states:rw`) while the master stays read-only (`compose.yml:24-32`, `compose.yml:58`).
- Frontend convention is vendored-local, no CDN: `base.html:8-9` loads `htmx.min.js` and `alpine.min.js` from `overstate_ui/static/` via `url_for('static', ...)`; npm (`package.json`) currently serves only the CSS toolchain (Tailwind/daisyUI). View rendering uses server-side Pygments highlighting (`highlight_yaml`/`highlight_json`, `files.py:39-64`), which stays for the read-only view page.
- The accepted state-file-management record explicitly forbade edit/save and commit/push from the UI (G1, `docs/plans/state-file-management.md:7`, constraints `:44-49`). This v4 plan explicitly reverses that decision for the `FILE_ROOTS` checkout only; everything else in that record (conformity pipeline, read-only SLS preview) is untouched.
- Owner decisions settled this run: scope = all text files in the checkout; publish flow = save commits locally, push is a separate reviewed step; roles = operators edit and commit, only admins push; online editor component approved with an external dependency authorized.

## Constraints And Non-goals

- Edit surface is existing text files under `FILE_ROOTS` only. `REACTOR_ROOTS` and the reactor page are untouched. No file creation, deletion, rename, move, or directory management.
- A file is editable only if `safe_join` accepts the path and `read_text` decodes it (UTF-8, within `MAX_BYTES`). Binary/oversize files keep the current explainer card with no edit affordance.
- Git discipline inherits `git_sync.py`: fixed argv, no shell, no user-supplied flags or refs, bounded timeout, shared single-flight lock with sync/fetch, CSRF on every POST, audit row per outcome. The only variable argv element is the `safe_join`-validated relative path, passed as a single argv item (never through a shell). Never `--force` (including `--force-with-lease`), never checkout-switch, never commit paths other than the single saved file.
- Push credentials are deployment-side (deploy key or credential helper on the checkout's remote). They never live in the DB, never appear in the UI, and missing/unusable credentials refuse with a fixed reason, never with git's raw stderr.
- Editor discipline: the editor bundle is built once from pinned npm packages and committed under `overstate_ui/static/`; the edit page loads it locally like htmx/alpine — no CDN or external runtime fetch (offline-safe, same pattern as today). The editor is progressive enhancement over a plain-textarea form: the POST carries plain text, so saving works with the bundle missing or JS disabled. Third-party license entry goes in `THIRD-PARTY-LICENSES.md` (repo convention).
- No new framework, service, table, migration, or top-level nav section. Flask + Jinja + HTMX + Alpine + daisyUI as today; edit/push live on the existing Files pages. The editor is a page-scoped component, not an app framework.
- Saving never triggers Salt execution. A YAML parse problem on `.sls`/`.yaml`/`.yml` is an advisory warning, not a block; Salt remains truth at apply time.
- Non-goals: new-file creation, deletion, server-side diff/merge UI, branch management, PR workflow, auto-push on save, pillar/reactor-roots editing, master-config editing, LDAP/OIDC changes, collaborative editing, in-editor Salt execution or lint backends.

## Key Decisions

- **Online editor is CodeMirror 6, vendored as one local bundle.** CodeMirror is MIT-licensed, ships as modular ES6 npm packages with a `basicSetup` baseline and line numbers as an opt-in extension, lists YAML among its supported languages, and documents that the packages must be combined with a bundler — which is exactly the vendored-bundle approach this repo already uses for htmx/alpine. Build once (bundler + `codemirror` + YAML language extension, versions pinned in `package.json`), commit the single output file under `overstate_ui/static/`, load it only on the edit page. Rejected: Monaco (the full VS Code editor — far heavier than a single-file text form needs, and its loader/AMD legacy adds integration surface for no v4 requirement); textarea-only (meets the letter of "editing" but concedes line numbers, highlighting, and mobile editing behavior the official component provides for free); any CDN-loaded editor (breaks the offline-safe vendored convention and the Podman/offline story).
- **Edit existing files only, one file per save.** The edit form is a new `GET /files/edit?path=` (viewer-hidden, operator+ server gate) rendering the editor (textarea fallback inside) plus the base SHA it was read at; `POST /files/save` (new) re-validates path, decodes, writes bytes, and commits only that path. Rejected: in-place editing on the view page (loses the explicit base for concurrency checks) and allowing create/delete (widens blast radius beyond the settled scope).
- **Each save is one local commit with a fixed identity.** Message subject is built server-side (`overstate(<username>): <rel>`), multi-line body dropped, committer set per-invocation via `git -c user.name=overstate -c user.email=overstate@localhost commit` so the result never depends on repo config; author is the operator's username, sanitized to one line. Rejected: batching saves into one commit (hides per-file audit) and taking identity from repo config (unowned machines commit as whoever).
- **Push is a separate admin-only step.** New `POST /files/push` (`roles_required("admin")`) runs fixed-argv `git push` under the shared lock and timeout; success flashes old → new SHA, any refusal flashes the fixed reason. Rejected: save-pushes-at-once (a bad save is instantly upstream and conflicts block editing — voted down by the owner) and operator push (wider blast radius than the settled role split).
- **Optimistic concurrency on every save.** The form carries the base SHA (and base content hash); save refuses when `HEAD` moved or the on-disk content no longer matches the base, rendering the refusal with the current SHA and a "reload and re-apply" hint. Nothing is written or committed on refusal. Rejected: last-writer-wins (silent overwrites) and file locking (lock files in a git checkout are a second source of truth).
- **Master visibility reuses the existing advisory refresh.** A successful save calls `_refresh_fileserver()` exactly like sync does: success is informational, failure warns that applies may lag. Push needs no refresh (it changes the remote, not the local tree the master reads).
- **Diverged means Sync first, never force.** Push on a diverged branch, dirty tree, missing upstream, or non-checkout refuses with the fixed reason; the operator path back is the existing Sync (`pull --ff-only`) or resolving it in git outside the app. Rejected: pull-then-push inside Push (surprise mutations) and any force variant.
- **Push authentication is a deployment prerequisite, documented not coded.** The app adds no credential UI; `docs/deployment.md` gains the deploy-key setup and a "push refuses without credentials" note. Rejected: token fields in Settings (secrets don't belong in the DB per `config.py` convention).

## Recommended Approach

One track, four ordered units, each independently revertable with no migration. Build the edit view with the CodeMirror bundle, textarea fallback, and save→commit path together in the first unit (per G2), then the admin push path, then concurrency/validation hardening, then docs and audit polish. Reuse `safe_join`, `read_text`, `sync_revision`, `git_status`, `_single_flight`, `_run`/`_failure`, `_refresh_fileserver`, and `log_event` throughout — one small commit helper joins `git_sync.py` rather than a second git layer.

## Work Plan

1. **Edit view + CodeMirror bundle + save→commit (operator+).** New `GET /files/edit` (CodeMirror editor enhancing a textarea fallback + base SHA, hidden from viewers) and `POST /files/save`: re-resolve path via `safe_join`, enforce UTF-8 + `MAX_BYTES`, write bytes, `git add -- <rel>` + fixed-identity `git commit` for that path only, advisory `_refresh_fileserver()`, audit `file-save:<rel>:<new-sha>`, redirect to the view page showing the new revision. Pin `codemirror` + YAML language extension in `package.json`, bundle once to a single `overstate_ui/static/editor.bundle.js` loaded only on the edit page (YAML highlighting for `.sls`/`.yaml`/`.yml`, plain text otherwise, line numbers); bundle failure or no-JS degrades to the working textarea. Add the `THIRD-PARTY-LICENSES.md` entry and a rebuild note in `docs/developer.md`. Pin the exact language-package name against the installed `codemirror` version at implementation time (YAML is in the official language list; the package name follows the `@codemirror/lang-*` family). Tests: save round-trip changes content + SHA, single-path commit only, edit page contains the bundle script + textarea fallback, save works with the bundle absent, no external URLs in the rendered page, license entry present, viewer GET/POST 403, traversal POST 404, binary/oversize POST refused, audit row asserted.
2. **Admin Push on the Files status card.** New `POST /files/push` (`roles_required("admin")`): shared lock + timeout + fixed argv `git push`, old/new SHA flash on success, fixed-reason refusal otherwise (`already up to date`, `diverged — sync first`, `dirty tree`, `no upstream`, `not a git checkout`, `push credentials missing`, `already running`, timeout), audit `git-push:<new>` / `git-push-refused:<reason>`. Status card shows ahead/behind (already in `git_status()`) so the button reads as "review then push". Tests: success, each refusal with stubbed git, operator-POST 403, viewer-GET hides button, no raw stderr in any response.
3. **Concurrency + input hardening.** Base-SHA/content-hash check on save (stale base refuses, nothing written); commit message sanitization (single line, no newlines/quotes reaching argv — argv stays fixed regardless); YAML files get a parse-and-warn pass (warning flash, save still proceeds); save and push share the one `_single_flight` lock. Tests: stale-base double-save refuses the second, message with newlines collapses to one commit subject, invalid YAML saves with warning, concurrent save+push serializes.
4. **Docs + audit polish.** Short paragraphs in `docs/user.md` (edit scope, editor behavior + fallback, base-stale flow, commit-per-save, admin push + refusal meanings) and `docs/deployment.md` (checkout needs a push-capable remote: deploy key / credential helper, ownership, Sync-remains-the-recovery-path). Verify each new mutation (save, push, refusals) logs; extend `tests/test_files.py` + `tests/test_git_sync.py` only — no new test files needed unless the matrix outgrows them.

## Validation Plan

- `.venv/bin/pytest -q` green after every unit; focused: `tests/test_files.py` (edit/save/RBAC/refusals/editor fallback), `tests/test_git_sync.py` (push matrix with stubbed git), plus the frozen contracts `tests/test_sls_preview.py` and `tests/test_states_conformity.py` must stay green untouched.
- Editor checks live: open the edit page with JS on (editor renders, YAML highlighted, line numbers present) and with JS off (textarea saves fine); confirm page source references only local `/static/` scripts; confirm the bundle file is committed and rebuildable from the pinned `package.json` versions.
- Live on the dev stack: edit `salt-srv/salt/demo.sls`, confirm one new commit touching only that file and the view page shows the new SHA; reload-and-save from two tabs and confirm the second refuses as stale with nothing committed; push as operator (403), then as admin (new SHA upstream); diverge the remote plus a local edit and confirm push refuses, Sync recovers or explains, push then succeeds.
- Refusal sweep live: binary file and >256 KiB file show no Edit button and POST refuses; `FILE_ROOTS` at a non-checkout shows no edit/push affordances; kill the remote (or break credentials) and confirm push refuses with the fixed reason and no token material in the response or logs.
- Highest-risk check: the stale-base path under a racing Sync (save in flight while `pull --ff-only` lands) — prove with a staged race that either the save commits onto the new base cleanly or refuses without writing, because a silent overwrite here destroys trust in the whole feature. Second risk: push against a diverged or credential-less remote — prove the refusal path instead of a hung page (timeout) or a leaked URL (stderr hygiene).

## Risks / Rollback

- Silent-overwrite risk: mitigated by the base-SHA/content check; refusal writes nothing.
- Bad-content risk (broken YAML pushed fleet-wide): mitigated by advisory parse warning, per-file single commits (easy `git revert`), and the standing rule that saving never applies anything; never force-push a fix — revert commits only.
- Credential-leak risk: mitigated by the `_failure` pattern (fixed words to the UI, raw stderr to server logs only); no credential input exists in the app.
- Hung-remote risk: bounded timeout on every git call plus the shared single-flight lock; a stuck push surfaces as a timeout refusal, never a hung page.
- Editor supply-chain risk: versions pinned in `package.json`, bundle committed (no CDN fetch at runtime), license recorded; a stale bundle only ever affects highlighting/editing comfort, never save correctness, because the textarea fallback posts the same plain text.
- Mount risk: none new — compose already gives the app `:rw` on the checkout and the master `:ro`.
- Rollback: each unit reverts independently (`git revert`); no migration exists to unwind, and old code renders new commits as ordinary file content plus SHAs. Removing the editor is deleting one static file plus its script tag.

## Open Questions

None. Scope (all existing text files under `FILE_ROOTS`), publish flow (save commits locally, separate admin push), role split (operators edit/commit, admins push), and editor direction (vendored CodeMirror 6 with textarea fallback) are settled; the exact YAML language-package pin is an implementation-time lookup with a designed fallback (plain text), not a question.

## Sources

- https://codemirror.net/ — MIT license, component feature set (line numbers, syntax highlighting, mobile, theming), YAML in the supported-language list.
- https://codemirror.net/docs/guide/ — modular ES6 npm packages (`@codemirror/state`, `@codemirror/view`, `@codemirror/commands`), `basicSetup` baseline from the `codemirror` package, bundler required to ship a bundle.
- https://raw.githubusercontent.com/microsoft/monaco-editor/main/README.md — Monaco is the full VS Code editor (npm ESM build); rejected as oversized for a single-file text form.
