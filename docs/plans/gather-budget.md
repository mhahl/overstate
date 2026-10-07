## Goal
Stop the web workers waiting on minions that will not answer. A sync
job and a console `--sync` line publish once per master and then read
the postgres returner. A page that needs one minion skips the live
call when the cached roster already says that minion is not up.
Console runners that ping the fleet carry a short timeout.

## Success Criteria
- `launch()` in sync mode never sends Salt `timeout: 60` and never
  sets `http_timeout` to 65. It publishes `local_async` to every
  master pod under one jid, then reads `salt_returns` for at most
  20 seconds of wall clock.
- Pods are published in parallel. One slow salt-api does not delay
  the login or publish on the next pod.
- salt-ssh stays a single-pod synchronous call with its 180 second
  Salt timeout.
- Console `--sync` still prints `minion: ok` lines when those returns
  are already in `salt_returns` inside the 20 second budget. If the
  budget expires, the command returns the jid and the job link, and
  the job stays incomplete.
- Opening a minion overview, schedule, pillar, beacons, mine, or raw
  tab, or the schedules page, does not call `client.local` when
  `cached_roster` is reachable and the id is not in the up set. The
  states tab stays stored-only. An up minion still gets the live call.
- `salt-run manage.status` and `salt-run manage.versions` send Salt
  `timeout: 5` and HTTP timeout 8. Other console runners get HTTP
  timeout 8 and no Salt `timeout` kwarg.
- `.venv/bin/pytest -q` is green. No salt-master image, manifest, or
  gunicorn worker count changes.

## Context And Current Facts
- `jobs_service.launch()` fans local publishes out through
  `pod_clients()`. The async branch sends `local_async` with a shared
  jid, one pod after another. The sync branch calls `client.local`
  with `timeout=SYNC_SALT_TIMEOUT` (60) and `http_timeout=65` per pod,
  in series, and writes `JobReturn` rows from that HTTP body.
- Publish buses are per master. Reconcile accepts a key on every pod,
  so a pod the minion is not connected to waits the full Salt timeout
  on a synchronous local call. Three pods is up to 180 seconds.
  Gunicorn's worker timeout is 90 seconds
  (`scripts/docker-entrypoint.sh`). The console `fetch` has no timeout
  and prints `request failed: no server response.` when the worker
  is killed.
- Outside the cluster `pod_clients()` returns `[("master", default)]`.
  Tests that stub `get_salt()` and call `launch()` see one client.
- Returns land in `salt_returns` through the stock `pgjsonb`
  returner. `sync_job(jid)` copies those rows into `job_returns`.
  The app does not write `salt_returns`. `sync_job` completes a job
  only after `COMPLETE_AFTER_SECONDS` (60) of quiet, so a 20 second
  wait must not expect `job.complete` from `sync_job` alone.
- `jobs.lookup_jid` on `get_salt()` sees only the sticky pod's job
  cache. Do not use it as the wait. `salt_returns` is the shared copy.
- `cached_roster` (`minions_helpers.py`) serves a 30 second Redis
  entry. A miss calls `key.list_all` and `manage.status` with
  `ROSTER_HTTP_TIMEOUT` 8. The minion list, presence poll, groups,
  onboard, and schedules already use it. `presence_of` is `up` when
  the id is in the up set.
- Minion detail still calls `get_salt().local` on overview
  (`grains.items`), schedule, pillar, mine, beacons (two calls when
  entries exist), and raw (schedule plus pillar). The default Salt
  timeout is 10 and the default HTTP cap is 30. The states tab does
  not call Salt until Refresh. `schedules.index` calls
  `schedule.list` on the selected minion, or the first accepted id.
  `pillar.capture_pillar` calls `pillar.items` on an explicit capture.
- Those calls use the sticky client. A minion that is up on another
  pod looks the same as a down minion: the call waits out the Salt
  timeout and returns nothing. Do not fan those calls out. A serial
  fan-out would multiply the wait.
- Console `_cmd_runner` calls `get_salt().runner(fun, **kwargs)` with
  no timeout. Master `timeout` is unset, so Salt's default of 5
  seconds applies to `manage.status`. The HTTP cap is the client
  default of 30 seconds. `http_timeout` must never be placed in the
  salt-api payload (`tests/test_salt_client.py`).
- `pod_clients()` already caches one `SaltClient` per pod per
  process. Do not rebuild that cache.
- `rest_cherrypy` `thread_pool` defaults to 100. Master `timeout`
  defaults to 5 and `gather_job_timeout` to 10. Neither is a change
  in this plan. The include cannot override the image's
  `rest_cherrypy` block.
- App and worker ship as `quay.io/sigaint/overstate` with the tag in
  `deploy/kubernetes/kustomization.yaml` (`images.newTag`, currently
  `pair21`). `imagePullPolicy` is `Always`. Tags are not overwritten.
- Namespace is `overstate`. Rancher's API hostname often fails local
  DNS. A kubectl to the API IP without SNI returns NotFound. Unset
  any CONNECT proxy before `git push`.

## Constraints And Non-goals
- No salt-master image rebuild, no StatefulSet edit, no
  `master.conf` `timeout` or `gather_job_timeout`, no
  `thread_pool`, no gunicorn `--timeout` or `WEB_CONCURRENCY` change.
- No kill fan-out. `jobs.kill` stays on the sticky client. That is a
  separate correctness change.
- No new presence words and no change to the `live_roster` 3-tuple.
- Do not revert unrelated dirty files in the working tree. Touch only
  the files named in the work plan.
- Do not hardcode production minion DNS names into Python or templates.
- `fleet_keys_now` and `fleet_presence_now` stay single-client. Tests
  call them directly.

## Key Decisions
- **Sync local publishes become async, then a returner read.**
  Rejected: lowering `SYNC_SALT_TIMEOUT` but still calling synchronous
  `local` on each pod. A pod that does not own the minion still waits
  the whole timeout, and the waits are still serial.
- **The wait is 20 seconds of wall clock, one clock for every pod.**
  That is under the 90 second worker timeout with room for the
  publishes. Rejected: waiting until `sync_job` marks the job
  complete. That heuristic is 60 seconds of quiet and would bring the
  hang back.
- **Parallel publish with the existing clients.** `concurrent.futures`
  is enough. Cap the pool at the pod count (3). A pod that raises
  `SaltApiError` is still a missed pod: flash and
  `run-partial:{fun}:{name}` stay as they are.
- **Skip a single-minion `local` only when the roster read succeeded
  and the id is not up.** If `cached_roster` reports not reachable,
  keep today's live call. The page must not treat a failed presence
  read as "this minion is down."
- **Runner caps are per function.** `manage.status` and
  `manage.versions` get `timeout=5` inside the runner payload and
  `http_timeout=8`. `jobs.list_jobs`, `jobs.lookup_jid`, and
  `jobs.active` get `http_timeout=8` only. A `timeout` kwarg on those
  runners is not a Salt job timeout and must not be sent.

## Recommended Approach
Three code units, in order. Unit 1 removes the multi-minute hang.
Unit 2 removes the 10 second hang on a dead or foreign minion.
Unit 3 caps the console runners. Deploy is unit 4 and runs only
after pytest is green.

## Work Plan
1. **Sync publish reads the returner.** In `overstate_ui/jobs_service.py`:
   - Add `SYNC_WAIT_SECONDS = 20`. Leave `SYNC_SALT_TIMEOUT` in place
     only if something else still imports it. The sync branch must
     not pass it to `client.local`. `SSH_SALT_TIMEOUT` stays 180.
   - Add a helper that publishes one `local_async` call. Run it across
     `pod_clients()` with a thread pool. Collect misses the same way
     the async branch does. If every pod misses, raise the last
     `SaltApiError`.
   - Use that helper for both the async branch and the sync branch.
     Same shared jid, same `run:{fun}` audit, same `_warn_missed`.
   - Sync branch, after a successful publish: record the `Job` row,
     then poll `sync_job(jid)` about once a second until
     `JobReturn` rows exist for the jid or 20 seconds have elapsed.
     Do not set `job.complete = True` because a mapping came back
     from the HTTP body. Completion stays with `sync_job`.
   - If the budget expires with no rows, leave `complete` false.
     The job page already shows running jobs.
   - Console `_cmd_salt` in `overstate_ui/console.py`: when
     `--sync` returns and `JobReturn` rows exist, keep the current
     per-minion lines. When none exist, print the jid and one line
     that the returns are still arriving, plus the job link. Do not
     block past `launch()`.
   - Tests to change: `tests/test_job_sync.py`
     `test_sync_launch_persists_returns` and
     `test_sync_launch_uses_app_jid`. The stub's `local()` for a
     sync run must see `asynchronous=True` and must not see
     `http_timeout >= 60`. To keep the "returns are stored"
     assertion, the stub inserts a `SaltReturn` for the jid it was
     given (`fun`, `jid`, `minion_id`, `success="true"`, `payload`,
     `full_ret`) before returning. `test_ssh_launch_persists_returns`
     stays on the synchronous ssh client with `http_timeout >= 180`.
   - `tests/test_console.py` `test_salt_sync_prints_per_minion`
     needs the same `SaltReturn` insert from its transport, or the
     assertion changes to the "still arriving" line when no return
     is present. Prefer keeping `m1: ok` by inserting the return
     inside the `local_async` handler, using the jid from the body.
2. **Skip live calls for a minion the roster says is not up.**
   - Add `minion_is_up(client, mid) -> bool | None` next to
     `cached_roster`. `True` when the id is in the up set, `False`
     when the roster was reachable and the id is absent, `None` when
     the roster was not reachable.
   - In `overstate_ui/minions.py` `detail`, call it once. On `False`,
     skip every `client.local` in overview, schedule, pillar, mine,
     beacons, and raw. Set `error` or the tab's existing empty note
     to a single sentence: the minion is not responding, stored data
     is shown. On `None` or `True`, keep the current calls.
   - `refresh_one` and `pillar.capture_pillar`: on `False`, flash
     that sentence and do not call Salt. On `None` or `True`, call
     as today.
   - `schedules.index`: on `False`, render the page with the empty
     schedule and the same sentence as `error`. Do not call
     `schedule.list`.
   - Do not add a fan-out. Do not change the states tab.
   - Tests: extend the minion detail tests so a roster whose up set
     is empty does not issue `grains.items`, and a roster whose up
     set contains the id still does. The existing overview fixture
     that expects live grains (Fedora / address from the fake
     transport) must keep issuing `grains.items`. Add one schedules
     test for the not-up skip.
3. **Cap console runners.** In `_cmd_runner`:
   - `http_timeout = 8` on every allowed runner.
   - For `manage.status` and `manage.versions` only, also pass
     `timeout=5` as a runner keyword (it belongs in the payload).
   - Test in `tests/test_console.py`: the `manage.status` request
     body contains `"timeout": 5` and the transport timeout is 8.
     A `jobs.list_jobs` body does not contain `timeout`.
4. **Deploy the app only.** After pytest is green:
   - Build `Containerfile` for linux/amd64. Push
     `quay.io/sigaint/overstate:<new-tag>`. Do not reuse `pair21`
     or any tag already in Quay.
   - Set `newTag` in `deploy/kubernetes/kustomization.yaml` to that
     tag. Leave `newName` as `quay.io/sigaint/overstate`.
   - Commit the code and the tag bump only. Subject:
     `App: bound sync gathers to the returner`. Do not commit
     unrelated dirty files. Do not commit scratch files (the
     referenced `PLAN_REMEDIATE.md` never existed in the repo).
   - Push to `origin` `main` only when the owner has asked for
     commit and push. Unset `HTTPS_PROXY` before the push.
   - Apply only the app and the worker:
     `kubectl -n overstate apply -k deploy/kubernetes`
     is broader than this change. Prefer:
     `kubectl -n overstate set image deployment/overstate-app app=quay.io/sigaint/overstate:<new-tag>`
     and the same for `deployment/overstate-worker` container
     `worker`. Do not delete or roll `salt-master-*`.
   - If `rancher.syd.prod.sigaint.au` does not resolve, stop and
     report that. Do not point kubectl at the bare IP.

## Validation Plan
- `.venv/bin/pytest -q` after each unit. Focused first:
  `tests/test_job_sync.py`, `tests/test_console.py`,
  `tests/test_salt_client.py`, the minion detail and schedules tests.
- Highest-risk check: a sync run with one pod that never returns
  still finishes the HTTP request in about 20 seconds plus the
  publish, and the worker is not killed at 90 seconds. Prove it
  with a stub whose `local()` would hang if `asynchronous` is false,
  and assert the test returns without that hang.
- Second check: detail of an id absent from a reachable up set
  performs zero `local` calls. Detail of an id in the up set still
  performs the grains call the overview test expects.

## Risks / Rollback
- A sync click no longer waits for slow-but-alive minions past 20
  seconds. Their returns still appear on the job page once the
  returner writes them. The console line says they are still arriving.
- `sync_job` will not mark the job complete during the 20 second
  wait. That matches async jobs today.
- Skipping the live call uses the sticky master's up set. A minion
  attached to another pod is shown as not responding on that page.
  The sticky `local` call could not have reached it either.
- Rollback is a revert of the app image tag. Masters are untouched.

## Open Questions
None. The 20 second budget, the skip rule, and the runner timeout
split are settled from the code and the Salt defaults above.
