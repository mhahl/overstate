## Status

Draft — grill in progress (target confirmed 2026-09-15). Nothing here is accepted until you explicitly approve the settled contract.

## Grill decisions (Draft)

- G1 (Draft): git from the UI is status + `pull --ff-only` sync only — no push/commit/branch UI. Settled by the owner 2026-09-15 (accepted the recommendation).
- G2 (Draft): app checkout mount goes `:ro` to `:rw` (master stays `:ro`) with production ownership documented. Settled by the owner 2026-09-15 (accepted the recommendation).
- G3 (Draft): conformity verdicts update on returner sync with Recompute kept as backfill. Settled by the owner 2026-09-15 (accepted the recommendation).
- G4 (Draft): unreachable means targeted-but-silent at age-out; presence never overwrites a return-backed verdict. Settled by the owner 2026-09-15 (accepted the recommendation).
- G5 (Draft): non-empty watch list narrows verdicts to those SLS files; empty list keeps whole-job semantics with a partial flag on fallback. Settled by the owner 2026-09-15 (accepted the recommendation).
- G6 (Draft): minion detail States tab renders last stored return first with an explicit bounded Refresh; failures show stored data plus an advisory note. Settled by the owner 2026-09-15 (accepted the recommendation).

## Scope contract (accepted)

Boundary: this decision record only. Out of scope: runtime code, tests, migrations, PRs, commits. Owner reply `accept` 2026-09-15 (this session); record stays Draft. Later stages need their own interview; execution words never widen this boundary.

## Goal

Make state health trustworthy and file browsing fast, and let operators see and sync the git checkout behind the file browser from the UI — without turning the app into an SLS editor. Salt stays the source of truth for minion state; git stays the source of truth for state files; every mutating click remains explicit, gated, and audited.

## Success Criteria

- The States page answers "what drifted, where, since when" for every minion: ok / drifted / unknown / unreachable verdicts are correct, watched SLS names actually narrow the verdict, each verdict links to the job return that produced it, and stale verdicts can never masquerade as fresh checks.
- Conformity updates itself when job returns land (no manual Recompute needed to stay current); Recompute remains as an explicit backfill.
- The minion detail States tab loads from stored returns first and only fans out to `state.show_highstate` on explicit refresh, so it stays fast when minions are down.
- The file browser stays content-read-only and still answers "find this formula, read it, know which revision it is": search/filter, directory grouping, readable rendering with line numbers, safe handling of big/binary files, and the git revision stays visible. No file edit/save route exists.
- Git sync is managed from the UI: the Files page shows branch, current SHA, clean/dirty, behind/ahead vs upstream, and the last few commits; an operator-gated Sync button runs the equivalent of `git pull --ff-only` on the checkout, reports the new SHA or the reason it refused, and logs an audit row. Non-fast-forward, dirty-tree, and non-checkout states refuse with an explanation instead of forcing anything.
- `.venv/bin/pytest -q` green; RBAC and audit behavior unchanged in spirit (viewers change nothing, operators+ sync, every mutation logs).

## Context And Current Facts

- Conformity engine (`overstate_ui/states.py:13-38`): `recompute_conformity()` takes the newest `Job.fun LIKE state.%`, stamps only minions with a `JobReturn` in that JID as ok/drifted, leaves everyone else untouched. Manual POST only (`states.py:100-105`); nothing calls it from `sync_job()` (`overstate_ui/jobs_service.py:23-87`).
- Watched states are stored (`overstate_ui/models.py:149-153`, `states.py:70-97`) but never read by `recompute_conformity()` — watching an SLS changes nothing on the page today. Docs claim narrowing (`docs/user.md:222-226`).
- Four verdicts are documented (`docs/user.md:214-220`: ok / drifted / unknown / unreachable) but only three are ever produced; nothing writes `unreachable`, and the template renders any other string as neutral (`overstate_ui/templates/_conformity_rows.html:9-12`). The `test_states_conformity.py` regression pins the "never attribute unchecked minions" rule.
- Minion detail States tab (`overstate_ui/minions.py:332-333`) does a live `state.show_highstate` on every page view — slow and error-prone when the minion is down; no cache, no fallback to stored returns.
- SLS preview exists and works (`overstate_ui/jobs_service.py:130-176`, `overstate_ui/tasks_salt.py:183-204`, `tests/test_sls_preview.py`, `docs/plans/sls-preview.md` accepted 2026-09-13): per-SLS `state.show_sls` on first-matched-or-snapshot minion, advisory-only, worker with sync fallback. This plan leaves its contract intact.
- File browser (`overstate_ui/files.py:1-98`) is deliberately content-read-only: flat `rglob("*")` list, no search, no directory tree, no pagination/cap, 256 KiB + UTF-8 gate with bare 404 for big/binary, `safe_join` traversal guard (`files.py:19-28`), git SHA via parent-walk (`files.py:58-77`), `FILE_ROOTS` defaults to `salt-srv/salt` (`overstate_ui/config.py:42`). Tests pin read-only + traversal rejection (`tests/test_files.py:43-57`). PLAN.md v1 non-goals forbid an SLS editor and writes into `/srv/salt` (`PLAN.md:15-22,55`); owner confirmed file contents stay read-only, with git sync as the allowed exception.
- Git sync today lives outside the app: `scripts/sync-file-roots.sh` runs `git -C "$TARGET" pull --ff-only` plus `rev-parse --short HEAD`, fails closed on non-ff and on non-checkouts; `docs/deployment.md:59-61` says `FILE_ROOTS` is a checkout, mount it read-only, sync from cron or a sidecar, the app never writes there. `compose.yml:22-26,52` mounts `./salt-srv:/srv/states:ro` into the app and `./salt-srv:/home/salt/data/srv:ro` into the master. So an in-app Sync button needs a deployment decision (writable checkout for the app) or it will fail against the `:ro` mount.
- Stack constraints (`PRODUCT.md:54-60`, `PLAN.md:57-73`): Flask + Jinja + HTMX + Alpine + daisyUI, Postgres jsonb, Redis/RQ with synchronous inline fallback, openSUSE Leap 16 Podman target, Apache-2.0. Roles viewer < operator < admin; destructive/operator actions gated, CSRF on every HTMX POST, audit trail.

## Constraints And Non-goals

- File contents stay read-only (owner decision). No edit/save route, no fileserver write, no git-commit/push-from-UI, no pillar write. The only git mutation allowed is sync (`fetch` + `pull --ff-only`); never `--force`, never push, never commit, never checkout-switch.
- Git subprocess discipline: fixed argv, no shell, no user-supplied flags/refs, bounded timeout, single-flight (one sync at a time), operator-role gate, CSRF, audit row with old/new SHA or refusal reason.
- No new framework, no new service, no new top-level nav section; third-level nav stays tabs-only per `PLAN.md:183`. Git status/sync lives on the existing Files page.
- RBAC: file views stay viewer-visible; Sync POST is operator+ only and forged requests are rejected server-side. Viewers see status but no Sync button.
- Salt remains truth for minion state; git remains truth for file contents. DB rows are snapshots/caches with source JIDs/SHAs; sync/backfill never invents success.
- Non-goals: SLS/pillar editor, formula catalog, commit/push/branch management from the UI, per-state enforce-from-UI, master config editing, LDAP/OIDC changes, multi-master, pkg-upgrade workflows.

## Key Decisions

- **Conformity becomes event-driven with explicit backfill, not poll-or-manual.** `sync_job()` updates affected minions' verdicts as returner rows land; the States Recompute button stays as an explicit backfill/repair path. Rejected: periodic silent recompute (hides staleness) and leaving it manual-only (verdicts rot between clicks).
- **Watched SLS list actually narrows the verdict.** When the watch list is non-empty, a minion is ok only if every watched SLS's states in that job's return are clean; otherwise drifted. Empty watch list keeps today's whole-job semantics. Rejected: ignoring the table (current bug) and requiring a watch entry before any verdict (would blank existing fleets).
- **Unreachable becomes a real verdict with a defined owner.** A minion counts as unreachable for a state job when the job targeted it (pinned roster covers it) but no returner row arrived by the time the job ages out via `COMPLETE_AFTER_SECONDS`. Presence (`manage.status` down) is shown alongside but never overwrites a return-backed verdict. Rejected: deriving unreachable from presence alone (presence flaps) and dropping the fourth state (docs promise it).
- **Per-minion per-state detail comes from stored returns, not live calls.** The detail States tab renders the last stored highstate-style return (state-by-state chips + summary) and offers an explicit Refresh that runs `state.show_highstate` once. Rejected: live-on-every-view (current slowness) and never showing live data (operators need an on-demand truth check).
- **Files get a browse-first upgrade inside the content-read-only rule.** Server-side search/filter, directory-grouped listing with a result cap + pagination, line-numbered rendering with a size/binary explanation instead of a bare 404, revision SHA kept on both pages, and a top.sls/env hint when present. Rejected: client-side full-text index (Alpine must not hold the file tree), syntax-highlighting JS dependency (server-rendered escaping + line numbers first; highlighting only if zero-dependency), and any content write path (owner + PLAN forbid it).
- **Git from the UI means status/show/sync only — the script stays canonical.** The Sync action runs exactly what `scripts/sync-file-roots.sh` runs (`git pull --ff-only` + `rev-parse`), refactored so both call one helper; the UI adds read-only status (branch, SHA, clean/dirty, ahead/behind, last N commits via `git log --oneline -N`) and the button. Rejected: push/commit/branch UI (unasked, widens blast radius), force-pull on diverge (destroys the "modified elsewhere" audit story), and shelling out with user input (injection surface).
- **Sync runs bounded with single-flight and honest failures.** Fixed-argv subprocesses with a short timeout (fetch/pull each bounded, e.g. ~30s), an RQ-or-inline path matching the existing `queue_or_none`/`wait_for` pattern, a lock so two Sync clicks cannot interleave, and every outcome (new SHA, already-up-to-date, non-ff refused, dirty tree, no upstream, not-a-checkout, timeout) renders as a plain message plus an audit row. Rejected: blocking the request thread unbounded and silent auto-sync on page view (surprise mutations).
- **Full-rework allowance spent narrowly: one migration, bounded workers, one mount decision.** One Alembic migration for the conformity detail/history shape; all slow Salt calls keep the worker-with-sync-fallback pattern; the git sync needs `compose.yml` to give the app a writable checkout (app mount `:ro` to `:rw`, master stays `:ro`) plus a docs note for production ownership. Rejected: new daemons, new tables per state, and grant changes — none needed since `state.show_sls` / `state.show_highstate` are already callable paths.

## Recommended Approach

Three tracks, state health first. Track A rebuilds the conformity pipeline (engine + watched semantics + unreachable + detail-from-returns) behind one migration, keeping the "never attribute unchecked minions" invariant green throughout. Track B hardens the file browser inside the content-read-only rule (search, tree, caps, readable render). Track C adds the git status/sync card on the Files page reusing the sync script's flags, gated and audited, with the `:ro`-to-`:rw` mount fix for the app service. SLS preview contract is frozen; only its advisory-failure-note pattern is reused. Ship as ordered units below so each lands independently revertable.

## Work Plan

1. **Conformity engine: event-driven verdicts + unreachable.** Wire `sync_job()` to upsert `Minion.conformity` (`status`, `jid`, `checked_at`, `targeted` flag) as returner rows land; age-out rule marks targeted-but-silent minions `unreachable` once the job completes via `COMPLETE_AFTER_SECONDS`. Keep the narrow-glob invariant (only minions with returns, or provably targeted, are touched). Tests: extend `tests/test_states_conformity.py` (targeted-but-silent becomes unreachable; untargeted keeps prior verdict).
2. **Migration + verdict history shape.** One Alembic revision: enrich `Minion.conformity` JSON contract (no column change if jsonb suffices) plus a lightweight `state_conformity_history` (minion, jid, status, at) capped per minion for the history strip; `downgrade()` drops only the new table. Backfill script reuses `recompute_conformity()` semantics once. Tests: migration up/down on sqlite + Postgres CI path.
3. **Watched-states semantics that actually narrow.** `recompute_conformity()` and the sync hook filter the return payload to watched SLS names when the list is non-empty (payload `__sls__` grouping; unparseable payload falls back to whole-job verdict with a `partial` flag surfaced in UI). Empty list preserves today's behavior. Tests: watched subset ok/drifted matrix; unknown SLS name degrades, never blocks.
4. **States page + detail from stored returns.** States index gains status filter, `checked_at` age column, and history strip per minion; minion detail States tab renders last stored return with per-state chips and an explicit Refresh (bounded worker call to `state.show_highstate`, advisory failure note reusing the preview pattern). Sort/filter stay server-side with HTMX partials as today. Tests: page renders chips from fixture returns; live failure shows stored data + note, not a blank page.
5. **File browser browse-first upgrade (still content-read-only).** Server-side `q` search + directory grouping + capped/paginated listing (cap `list_tree()` fan-out, largest-first guard), `view` gains line numbers, binary/oversize explainer card (replacing bare 404 with a reason + size + revision), revision SHA on both pages, top.sls/env hint block when the checkout has one. Assert no content-POST route exists (extend `tests/test_files.py`). Tests: search, traversal still 404, oversize/binary explainer, cap behavior on deep fixture tree.
6. **Git status + Sync on the Files page.** Status card (branch, SHA via `sync_revision()`, clean/dirty via `git status --porcelain`, ahead/behind via `git rev-list --count`, last 10 via `git log --oneline`) plus an operator-gated Sync POST sharing one helper with `scripts/sync-file-roots.sh` semantics (`fetch` + `pull --ff-only`, fixed argv, timeout, single-flight, audit row with old/new SHA or refusal). Non-checkout renders "not a git checkout" with no button; diverge/dirty/no-upstream render the git reason with nothing forced. `compose.yml` app mount goes `:ro` to `:rw` (master stays `:ro`); docs note production checkout ownership. Tests: status parsing units, Sync success/already-up-to-date/non-ff-refused/dirty-refused/not-a-checkout matrix with stubbed git, viewer-403 + CSRF + audit assertions, traversal/write-absence assertions kept.
7. **Docs + audit polish.** Short paragraphs in `docs/user.md` (States verdicts, Refresh behavior, Files browser, git status/Sync meanings and refusal cases) and `docs/deployment.md` (writable app checkout, cron/sidecar now optional since UI sync exists, ownership/permissions). Verify each new mutation (watch/unwatch/recompute/refresh/sync) logs; add the missing audit row if found. Tests: audit assertion for the refresh and sync paths.

## Validation Plan

- `.venv/bin/pytest -q` green after every unit; focused files: `tests/test_states_conformity.py`, `tests/test_files.py` (extended: browse + git-sync matrix), `tests/test_sls_preview.py` (contract frozen — must stay green untouched), plus the new history/migration tests.
- Migration check: `alembic upgrade head && alembic downgrade -1 && alembic upgrade head` on a scratch DB; app boots and States page renders before and after.
- Live on the dev stack: run a narrow-target highstate, confirm covered minions flip ok/drifted with source-JID links, untargeted minions keep prior verdicts, and a targeted-but-silent minion reads unreachable after age-out; watch one SLS and confirm the verdict narrows; open a minion States tab with the minion down and confirm stored data + note instead of a hang.
- File checks live: search for a known SLS, open a large/binary file and confirm the explainer (not 404-blank), confirm revision SHA matches `git rev-parse --short HEAD` in the checkout, attempt POST to a content-write path and confirm none exists (404/405).
- Git checks live: push a commit to the upstream from elsewhere, click Sync, confirm the Files page shows the new SHA and the new file appears; push a divergent commit plus a local change, click Sync, confirm it refuses with the git reason and changes nothing; point `FILE_ROOTS` at a non-checkout and confirm the status card degrades with no Sync button; click Sync as viewer and confirm 403.
- Highest-risk check: the sync-hook verdict path under partial returns (some minions silent, some failed) — prove with a staged JID that verdicts, history rows, and age-out timing all agree, because a wrong stamp here destroys trust in the whole page. Second risk: Sync against a `:ro` mount or a hung remote — prove the timeout/refusal path instead of a hung page.

## Risks / Rollback

- Wrong-verdict risk: stamping a minion from a job that never targeted it. Mitigation: roster-pinning (list/glob/group resolution as in `resolve_batch_roster`) gates every write; the existing narrow-glob regression test stays green and is extended, not rewritten.
- Slow-Salt risk: `show_highstate` fan-out hanging the detail page. Mitigation: stored-returns-first, explicit Refresh only, bounded worker wait with advisory-note fallback (same pattern as `build_sls_preview`).
- File fan-out risk: `rglob` over a huge checkout on every request. Mitigation: cap + pagination + single directory walk per request; no in-memory tree in Alpine.
- Git risks: diverge/dirty forced-sync destroying remote work; hung fetch; concurrent Sync clicks; `:ro` mount making Sync fail; command injection. Mitigations: `--ff-only` always, refusal-first messaging, bounded timeouts, single-flight lock, `:rw` app mount with docs, fixed argv with no user input reaching git.
- Rollback: each unit reverts independently (`git revert`); the migration's `downgrade()` removes only the new history table, and conformity JSON additions are additive/read-tolerant so old code renders new rows as ok/drifted/unknown. Sync unit rollback restores the `:ro` mount and the cron/sidecar path keeps working.

## Open Questions

None. Scope (state health first, contents read-only), git rule (status + `pull --ff-only` sync only, no push/commit/force), and depth (full rework incl. one migration + one mount change) are owner-confirmed this run; remaining transport/timing risks are validation steps with designed fallbacks, not questions.

## Sources

None. No external claims inform this plan; all evidence is workspace code, tests, docs, and owner answers cited above.
