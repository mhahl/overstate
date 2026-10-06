"""Job service layer: returner sync, live cache, launch, batches.

Called by the jobs routes and by RQ workers. No route decorators here.
"""

from __future__ import annotations

import datetime as dt
import time

import httpx
from flask import flash, redirect, url_for
from flask_login import current_user

from .audit import log_event
from .dashboard import get_salt
from .db import get_session
from .fleet import pod_clients
from .jobs_helpers import COMPLETE_AFTER_SECONDS, SYNTHETIC_JID_PREFIXES
from .models import Job, JobReturn, SaltReturn, SavedJob
from .salt_client import SaltApiError


def _verdicts_eligible(job: Job | None) -> bool:
    """Whether a completed job may stamp minion conformity.

    Grouping parents (batch-*) never ran on minions, and runner jobs
    (tgt_type runner) have no minion target, so neither may mark
    minions unreachable.
    """
    if job is None:
        return False
    if (job.jid or "").startswith("batch-"):
        return False
    if job.batch_group and job.jid == f"batch-{job.batch_group}":
        return False
    return (job.tgt_type or "") != "runner"


def _master_minions(jid: str) -> set[str] | None:
    """Minion ids the master still associates with jid.

    None when the master is silent or has forgotten the JID, so the
    caller falls back to the quiet-period heuristic.
    """
    try:
        payload = get_salt().runner("jobs.lookup_jid", jid=jid)[0]
    except Exception:  # noqa: BLE001 — master silent: caller falls back
        return None
    # Only an entry keyed by this JID counts: any other shape (a status
    # payload, an empty cache) means the master has forgotten the JID.
    data = payload.get(jid) if isinstance(payload, dict) else None
    if not isinstance(data, dict) or not data:
        return None
    return set(data)


def sync_job(jid: str, actor: str | None = None) -> Job | None:
    """Copy returner rows for jid into jobs/job_returns.

    Synthetic JIDs (batch-*, orch-*, ssh-*, sync-*) never appear in the
    returner tables; the worker owns their completion, so sync never
    completes them. A real JID completes once the master knows no
    unrecorded minions for it and the youngest return is older than
    COMPLETE_AFTER_SECONDS; once complete it stays sticky unless new
    minions report in.
    """
    session = get_session()
    job = session.get(Job, jid)
    if job is not None and job.jid.startswith(SYNTHETIC_JID_PREFIXES):
        return job
    rows = session.query(SaltReturn).filter_by(jid=jid).all()
    now = dt.datetime.now(dt.UTC)

    def aware(value: dt.datetime) -> dt.datetime:
        return value if value.tzinfo else value.replace(tzinfo=dt.UTC)

    if not rows:
        # No returner rows at all (lost returns, dead minion): age out
        # against started_at so the job can't sit in Running forever.
        if (
            job is not None
            and not job.complete
            and job.started_at is not None
            and (now - aware(job.started_at)).total_seconds() > COMPLETE_AFTER_SECONDS
        ):
            job.complete = True
            if _verdicts_eligible(job):
                from .states import apply_sync_verdicts

                apply_sync_verdicts(job, [])
            session.commit()
        return job
    if job is None:
        first = rows[0]
        job = Job(
            jid=jid,
            fun=first.fun,
            tgt="",
            tgt_type="glob",
            user=actor
            or (
                current_user.username
                if current_user.is_authenticated
                else "unknown"
            ),
        )
        session.add(job)
    stored = {
        row.minion_id: row for row in session.query(JobReturn).filter_by(jid=jid).all()
    }
    new_mids: set[str] = set()
    for r in rows:
        existing = stored.get(r.minion_id)
        success = str(r.success).lower() == "true"
        payload = r.payload if isinstance(r.payload, dict) else {}
        if existing is None:
            session.add(
                JobReturn(
                    jid=jid,
                    minion_id=r.minion_id,
                    success=success,
                    retcode=0,
                    payload=payload,
                )
            )
            new_mids.add(r.minion_id)
        else:
            existing.success = success
            existing.payload = payload
    if job.complete and not new_mids:
        session.commit()
        return job
    known = set(stored) | new_mids
    master_mids = _master_minions(jid)
    if master_mids is not None and not master_mids <= known:
        job.complete = False
    else:
        youngest = max(
            (aware(r.alter_time) for r in rows if r.alter_time),
            default=None,
        )
        if youngest is None:
            job.complete = (
                now - aware(job.started_at)
            ).total_seconds() > COMPLETE_AFTER_SECONDS
        else:
            job.complete = (now - youngest).total_seconds() > COMPLETE_AFTER_SECONDS
    if _verdicts_eligible(job):
        from .states import apply_sync_verdicts

        apply_sync_verdicts(job, rows)
    session.commit()
    return job


def live_returns_now(client, jid: str) -> list:
    """Display-only live returns from the master job cache.

    Never raises; [] when the cache expired the JID. Rows are
    lightweight stand-ins marked live=True — never DB rows, so
    the merge below can't duplicate or rewrite history.
    """
    from types import SimpleNamespace

    try:
        payload = client.runner("jobs.lookup_jid", jid=jid)[0]
    except (SaltApiError, httpx.HTTPError, KeyError, IndexError, TypeError):
        return []
    data = payload.get(jid, payload) if isinstance(payload, dict) else {}
    if not isinstance(data, dict):
        return []
    rows = []
    for mid, ret in data.items():
        # Verified live: values are the raw returns ({minion: True}
        # for test.ping) or per-minion dicts for richer jobs.
        if isinstance(ret, dict):
            success = bool(ret.get("success", True))
            rows.append(
                SimpleNamespace(
                    minion_id=mid,
                    success=success,
                    retcode=ret.get("retcode", 0),
                    payload=ret.get("return", ret),
                    live=True,
                )
            )
        else:
            rows.append(
                SimpleNamespace(
                    minion_id=mid, success=bool(ret), retcode=0, payload=ret, live=True
                )
            )
    return rows


def build_sls_preview(
    fun: str, args: list[str], matched: list | None, via: str
) -> tuple[list, str | None, str | None]:
    """(sections, minion, note) for the review page.

    Sections are [(sls, {state-id: ...})]; note explains a missing
    or failed render. Only state.apply with sls args renders;
    anything else returns ([], None, None) and the page is
    unchanged. Firing is never blocked: failures become notes.
    """
    if fun != "state.apply" or not args:
        return [], None, None
    from .dashboard import ping_target

    minion = (matched or [None])[0] or ping_target()
    if minion is None:
        return [], None, "No minion available to render against."
    from .tasks import queue_or_none, show_sls_now, show_sls_task, wait_for

    try:
        queued = queue_or_none(show_sls_task, minion, args, via)
        if queued is None:
            rendered = show_sls_now(get_salt(), minion, args, via)
        else:
            status, value = wait_for(queued, wait=8.0)
            if status == "pending":
                return (
                    [],
                    minion,
                    (
                        "Preview unavailable: render still "
                        "running — firing stays available."
                    ),
                )
            if status != "ready":
                return (
                    [],
                    minion,
                    (f"Preview unavailable: render failed in the background: {value}"),
                )
            rendered = value
    except (SaltApiError, httpx.HTTPError) as exc:
        return [], minion, f"Preview unavailable: salt-api error: {exc}"
    sections = [(sls, rendered[sls]) for sls in args if sls in rendered]
    missing = [sls for sls in args if sls not in rendered]
    note = f"Preview unavailable for: {', '.join(missing)}." if missing else None
    return sections, minion, note


def resolve_group_target(name: str) -> tuple[list[str], int]:
    """Group members pinned to the snapshot roster.

    Returns (targets, stale_count). Raises SaltApiError when the
    group is unknown or nothing in it is known.
    """
    from .models import Minion, MinionGroup

    session = get_session()
    group = session.query(MinionGroup).filter_by(name=name).first()
    if group is None:
        raise SaltApiError(f"unknown group '{name}'")
    roster = {row.id for row in session.query(Minion.id).all()}
    targets = sorted(m for m in (group.members or []) if m in roster)
    if not targets:
        raise SaltApiError(f"group '{name}' matches no known minions")
    return targets, len(group.members or []) - len(targets)


SYNC_WAIT_SECONDS = 20
"""Wall-clock budget for a sync run to wait on the returner. Publishes
are instant (async under the hood); the wait covers minions answering
inside one request while staying far under the 90 second worker
timeout. Slow-but-alive minions report after the budget through the
normal returner sync — same as async jobs today."""
SSH_SALT_TIMEOUT = 180


def _payload_success(payload) -> bool:
    """Success flag for a locally observed minion payload: an explicit
    flag wins, scalars use truthiness, mappings default True."""
    if isinstance(payload, dict):
        if "success" in payload:
            return bool(payload["success"])
        return True
    return bool(payload)


def _split_sync_result(result) -> tuple[str | None, dict]:
    """(real JID or None, {minion_id: payload}) from a sync salt-api reply.

    Sync local/ssh replies usually carry just per-minion returns; when
    the reply keeps the real JID alongside them it is preferred, and a
    synthetic JID is only used when no JID is present.
    """
    items = result if isinstance(result, list) else [result]
    mapping: dict = {}
    jid: str | None = None
    for item in items:
        if not isinstance(item, dict):
            continue
        for key, value in item.items():
            if key == "jid":
                if value and jid is None:
                    jid = str(value)
            elif key == "return" and isinstance(value, dict):
                mapping.update(value)
            else:
                mapping[key] = value
    return jid, mapping


def _new_jid() -> str:
    """Salt-format job id, shared across the pair for one user action."""
    return dt.datetime.now(dt.UTC).strftime("%Y%m%d%H%M%S%f")


def _force_beacon_kwargs(
    fun: str, args: list, kwarg: dict | None
) -> tuple[list, dict | None]:
    """Strip ``include_pillar``/``include_opts`` from positional ``args``
    (the job form and the console both publish them as tokens) and force
    both false on the kwarg dict, so a ``beacons.list`` publish never
    carries pillar-sourced config onto the wire. Other functions pass
    through unchanged. The dict is non-empty afterwards, so
    ``SaltClient.local``'s ``if kwarg:`` does not drop it."""
    if fun != "beacons.list":
        return args, kwarg
    cleaned: list = []
    for item in args or []:
        if isinstance(item, dict):
            item = {
                k: v
                for k, v in item.items()
                if k not in ("include_pillar", "include_opts")
            }
            cleaned.append(item)
        elif isinstance(item, str) and item.split("=")[0] in (
            "include_pillar",
            "include_opts",
        ):
            continue
        else:
            cleaned.append(item)
    forced = dict(kwarg or {})
    forced["include_pillar"] = False
    forced["include_opts"] = False
    return cleaned, forced


def _publish_all_async(
    clients, tgt: str, fun: str, args: list, tgt_type: str, jid: str,
    kwarg: dict | None = None,
) -> list[str]:
    """Publish one local_async per pod under the shared jid.

    Pods publish in parallel, capped at the pod count: one slow salt-api
    must not delay the publish on the next pod. Only the bare publish
    runs off-thread — clients are resolved and Flask state is read by
    the caller, never in the pool. Returns missed pod names, raising
    the last SaltApiError when every pod missed.
    """
    import concurrent.futures

    failures: list[tuple[str, SaltApiError]] = []

    def _one(pair) -> None:
        name, cli = pair
        try:
            cli.local(
                tgt,
                fun,
                arg=args,
                tgt_type=tgt_type,
                asynchronous=True,
                jid=jid,
                kwarg=kwarg,
            )
        except SaltApiError as exc:
            failures.append((name, exc))

    if len(clients) < 2:
        for pair in clients:
            _one(pair)
    else:
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(clients)) as pool:
            list(pool.map(_one, clients))
    if failures and len(failures) == len(clients):
        raise failures[-1][1]
    return [name for name, _ in failures]


def _await_returner(jid: str, budget: float = SYNC_WAIT_SECONDS) -> bool:
    """Wait up to budget seconds for returner rows to land in the DB.

    Each pass runs sync_job (which copies whatever salt_returns holds
    so far — the same call the job page already makes per view) and
    then checks for stored JobReturn rows. Returns True on the first
    stored return, False on budget expiry. Completion is never forced
    here: sync_job owns it on its 60-seconds-quiet terms.
    """
    session = get_session()
    deadline = time.monotonic() + budget
    while True:
        sync_job(jid)
        if session.query(JobReturn.jid).filter_by(jid=jid).first() is not None:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(1)


def _schedule_inner_fun(args: list, kwarg: dict | None) -> str | None:
    """The ``function=`` argument of a ``schedule.add`` publish."""
    if isinstance(kwarg, dict) and isinstance(kwarg.get("function"), str):
        return kwarg["function"]
    for item in args or []:
        if isinstance(item, dict) and isinstance(item.get("function"), str):
            return item["function"]
        if isinstance(item, str):
            name, sep, value = item.partition("=")
            if sep and name == "function":
                return value
    return None


def launch(
    tgt: str,
    tgt_type: str,
    fun: str,
    args: list,
    asynchronous: bool,
    via: str = "local",
    kwarg: dict | None = None,
    tgt_requested: str | None = None,
    actor_user=None,
) -> str:
    """Fire a job via salt-api; record the Job row. Returns the jid.

    ``actor_user`` is a service-account owner for token API fires: authz
    evaluates as that user and audit rows carry its name. Cookie flows
    leave it unset and evaluate as the logged-in user.

    Local publishes fan out to every master pod under one shared jid:
    publish buses are per-master, so a single-pod publish would miss
    minions attached to the other pod. Unreachable pods degrade to a
    warning (never a silent partial) and converge on the next action.
    salt-ssh stays single-pod: the roster executes from one master.

    In scoped mode the target is intersected with the caller's scope
    before any publish (``tgt=*`` never reaches salt-api for a scoped
    caller), and ``beacons.list`` publishes with pillar-sourced config
    forced off. ``jobs.tgt`` stores what Salt received;
    ``jobs.tgt_requested`` stores what was typed.
    """
    from .authz import (
        AuthzDenied,
        audit_deny,
        constrain_target_verbose,
        has_fleet,
        may_define_schedule,
        may_define_schedule_fleet,
        publish_perm,
        rbac_mode,
    )

    typed = tgt_requested if tgt_requested is not None else tgt
    narrowed: set[str] = set()
    perm = ""
    scoped = rbac_mode() == "scoped"
    who = actor_user if actor_user is not None else current_user
    actor_name = getattr(who, "username", None) or "unknown"
    # Browser warnings only for cookie flows; token fires name the actor
    # in audit rows and skip flashes there is no browser to warn.
    notice_actor = actor_name if actor_user is not None else None
    if scoped:
        perm = publish_perm(fun) or ""
        if not perm:
            denied = AuthzDenied(fun, "unknown function class")
            audit_deny(fun, detail=denied.detail, actor=actor_name)
            raise denied
        if via == "ssh" and not has_fleet(who, perm):
            # salt-ssh targets are often absent from the snapshot, so a
            # scoped intersection would silently drop the intended roster.
            denied = AuthzDenied(perm, "ssh needs a fleet grant", detail="needs-fleet")
            audit_deny(perm, detail=denied.detail, actor=actor_name)
            raise denied
        args, kwarg = _force_beacon_kwargs(fun, args, kwarg)
        try:
            tgt, tgt_type, requested, hit = constrain_target_verbose(
                who, perm, tgt, tgt_type
            )
        except AuthzDenied as exc:
            audit_deny(exc.perm, detail=exc.detail, actor=actor_name)
            raise
        if requested is not None and hit is not None:
            narrowed = requested - hit
        if fun == "schedule.add":
            inner = _schedule_inner_fun(args, kwarg)
            if hit is None:
                # Fleet grant: the target publishes unchanged; check the
                # inner function fleet-wide instead of per snapshot id.
                fleet_ok = may_define_schedule_fleet(who, inner)
            else:
                scheduled = hit
                fleet_ok = (
                    inner is not None
                    and bool(scheduled)
                    and all(may_define_schedule(who, mid, inner) for mid in scheduled)
                )
            if not fleet_ok:
                denied = AuthzDenied(perm, "schedule not allowed here")
                audit_deny(perm, detail="no-grant", actor=actor_name)
                raise denied
    client = get_salt()
    if tgt_type == "group":
        targets, stale = resolve_group_target(tgt)
        if stale:
            flash(f"Group '{tgt}': {stale} stale members skipped.", "warning")
        tgt, tgt_type = ",".join(targets), "list"
    if via == "ssh":
        # salt-ssh has no async path: synchronous over the roster. The
        # HTTP round trip must outlast the Salt timeout, and each minion
        # payload is stored so the job is complete with returns.
        result = client.local(
            tgt,
            fun,
            arg=args,
            tgt_type=tgt_type,
            timeout=SSH_SALT_TIMEOUT,
            via="ssh",
            kwarg=kwarg,
            http_timeout=SSH_SALT_TIMEOUT + 5,
        )
        real_jid, mapping = _split_sync_result(result)
        jid = real_jid or f"ssh-{int(time.time())}"
        session = get_session()
        job = Job(
            jid=jid,
            fun=fun,
            tgt=tgt,
            tgt_type=tgt_type,
            tgt_requested=typed,
            user=actor_name,
        )
        session.add(job)
        for mid, payload in mapping.items():
            session.add(
                JobReturn(
                    jid=jid,
                    minion_id=str(mid),
                    success=_payload_success(payload),
                    retcode=0,
                    payload=payload,
                )
            )
        job.complete = True
        session.commit()
        ssh_ids = sorted(mapping)
        log_event(
            actor_name,
            f"run-ssh:{fun}",
            jid=jid,
            minion_id=ssh_ids[0] if len(ssh_ids) == 1 else None,
            detail=",".join(ssh_ids) if len(ssh_ids) > 1 else None,
        )
        _audit_narrowed(narrowed, perm if scoped else "", jid, notice_actor)
        return jid
    clients = pod_clients(client)
    if asynchronous:
        jid = _new_jid()
        missed = _publish_all_async(clients, tgt, fun, args, tgt_type, jid, kwarg)
        session = get_session()
        session.add(
            Job(
                jid=jid,
                fun=fun,
                tgt=tgt,
                tgt_type=tgt_type,
                tgt_requested=typed,
                user=actor_name,
            )
        )
        session.commit()
        run_mid, run_detail = _run_audit_ids(tgt, tgt_type)
        log_event(
            actor_name, f"run:{fun}", jid=jid, minion_id=run_mid, detail=run_detail
        )
        _audit_narrowed(narrowed, perm if scoped else "", jid, notice_actor)
        _warn_missed(missed, fun, jid, notice_actor)
        return jid
    # Sync rides the same async publish, then reads the returner: a
    # synchronous local on a pod that does not own the minion waits out
    # the whole Salt timeout, serially per pod — minutes against a 90
    # second worker. Completion stays with sync_job; a sync click that
    # outruns the budget simply leaves the job running like async does.
    jid = _new_jid()
    missed = _publish_all_async(clients, tgt, fun, args, tgt_type, jid, kwarg)
    session = get_session()
    job = Job(
        jid=jid,
        fun=fun,
        tgt=tgt,
        tgt_type=tgt_type,
        tgt_requested=typed,
        user=actor_name,
    )
    session.add(job)
    session.commit()
    run_mid, run_detail = _run_audit_ids(tgt, tgt_type)
    log_event(actor_name, f"run:{fun}", jid=jid, minion_id=run_mid, detail=run_detail)
    _audit_narrowed(narrowed, perm if scoped else "", jid, notice_actor)
    _warn_missed(missed, fun, jid, notice_actor)
    _await_returner(jid, SYNC_WAIT_SECONDS)
    return jid


def _run_audit_ids(tgt: str, tgt_type: str) -> tuple[str | None, str | None]:
    """(minion_id, detail) for a run row from the published target. One
    snapshot id names it, so scoped audit readers in scope can see
    another user's run; several ids stay null with the list in detail;
    unevaluable targets stay null."""
    from .authz import published_snapshot_ids

    mids = published_snapshot_ids(tgt, tgt_type)
    if not mids:
        return None, None
    if len(mids) == 1:
        return next(iter(mids)), None
    return None, ",".join(sorted(mids))


def _audit_narrowed(
    narrowed: set[str], perm: str, jid: str, actor: str | None = None
) -> None:
    """Record a ``job-constrained`` row when the intersection dropped
    ids. The stored detail names them for fleet audit; the flash and the
    confirm page stay anonymous counts. Token fires pass their actor name
    and skip the flash: there is no browser to warn."""
    if not narrowed or not perm:
        return
    dropped = ",".join(sorted(narrowed))
    log_event(
        actor or current_user.username,
        "job-constrained",
        jid=jid,
        outcome="allow",
        permission=perm,
        detail=dropped,
    )
    if actor is not None:
        return
    flash(
        f"{len(narrowed)} minions are outside your scope and will not be touched.",
        "warning",
    )


def _warn_missed(
    missed: list[str], fun: str, jid: str, actor: str | None = None
) -> None:
    """Name unreachable pods loudly: partial results are never silent.

    Token fires skip the flash and carry the actor name in the row."""
    for name in missed:
        if actor is None:
            flash(f"{name} is unreachable, so results may be partial.", "warning")
        log_event(actor or current_user.username, f"run-partial:{fun}:{name}", jid=jid)


def resolve_batch_roster(tgt: str, tgt_type: str) -> list[str] | None:
    """Pin the wave roster from the snapshot table. List and glob only."""
    import fnmatch

    from .models import Minion

    roster = sorted(row.id for row in get_session().query(Minion.id).all())
    if tgt_type == "list":
        wanted = [t.strip() for t in tgt.split(",") if t.strip()]
        return [mid for mid in roster if mid in wanted]
    if tgt_type == "glob":
        return [mid for mid in roster if fnmatch.fnmatchcase(mid, tgt)]
    if tgt_type == "group":
        try:
            targets, _ = resolve_group_target(tgt)
        except SaltApiError:
            return []
        return targets
    return None


def run_batched(
    tgt: str,
    tgt_type: str,
    fun: str,
    args: list,
    batch: dict,
    save_as: str = "",
    actor_user=None,
):
    """Start a gated wave batch: parent row, enqueue or run inline.

    ``actor_user`` names a service-account owner for token API fires, as
    in :func:`launch`; cookie flows leave it unset."""
    import uuid

    from flask import abort

    from .authz import (
        AuthzDenied,
        audit_deny,
        constrain_target_verbose,
        has_fleet,
        minions_with,
        publish_perm,
        rbac_mode,
        require,
    )
    from .tasks import queue_or_none, run_wave_batch, run_wave_batch_task

    narrowed: set[str] = set()
    who = actor_user if actor_user is not None else current_user
    actor_name = getattr(who, "username", None) or "unknown"
    if rbac_mode() == "scoped":
        # Own the scoped constrain: an unknown group or an empty
        # intersection is 403 here, not a batch error page. Waves
        # additionally need job.batch on the whole roster.
        perm = publish_perm(fun) or ""
        if not perm:
            audit_deny(fun, detail="no-grant", actor=actor_name)
            abort(403)
        if actor_user is not None:
            from .authz import authorize

            try:
                authorize(who, perm)
                authorize(who, "job.batch")
            except AuthzDenied as exc:
                audit_deny(exc.perm, detail=exc.detail, actor=actor_name)
                abort(403)
        else:
            require(perm)
            require("job.batch")
        try:
            _, _, requested, hit = constrain_target_verbose(who, perm, tgt, tgt_type)
        except AuthzDenied as exc:
            audit_deny(exc.perm, detail=exc.detail, actor=actor_name)
            abort(403)
        if hit is None:
            # Fleet grant: today's roster semantics, including group
            # targets resolved against the masters.
            roster = resolve_batch_roster(tgt, tgt_type)
            if roster is None:
                flash("Batch mode supports list, glob, and group targets.", "error")
                return redirect(url_for("jobs.new"))
            if not roster:
                flash("No known minions match.", "error")
                return redirect(url_for("jobs.new"))
            narrowed = set()
        else:
            roster = sorted(hit)
            narrowed = requested - hit
        if not has_fleet(who, "job.batch") and not set(roster) <= minions_with(
            who, "job.batch"
        ):
            audit_deny("job.batch", detail="out-of-scope", actor=actor_name)
            abort(403)
        if (
            save_as
            and not has_fleet(who, "job.save")
            and not set(roster) <= minions_with(who, "job.save")
        ):
            audit_deny("job.save", detail="out-of-scope", actor=actor_name)
            abort(403)
    else:
        roster = resolve_batch_roster(tgt, tgt_type)
        if roster is None:
            flash("Batch mode supports list, glob, and group targets.", "error")
            return redirect(url_for("jobs.new"))
        if not roster:
            flash("No known minions match.", "error")
            return redirect(url_for("jobs.new"))
    from .tasks import split_roster

    waves = split_roster(roster, batch["mode"], batch["size"])
    group = uuid.uuid4().hex[:16]
    session = get_session()
    session.add(
        Job(
            jid=f"batch-{group}",
            fun=fun,
            tgt=tgt,
            tgt_type=tgt_type,
            user=actor_name,
            batch_group=group,
            batch_state={
                "mode": batch["mode"],
                "size": batch["size"],
                "stop_after": batch["stop_after"],
                "status": "running",
                "failures": 0,
                "waves": len(waves),
                "waves_done": 0,
            },
        )
    )
    session.commit()
    log_event(actor_name, f"batch-start:{group}")
    if rbac_mode() == "scoped":
        _audit_narrowed(
            narrowed,
            publish_perm(fun) or "",
            f"batch-{group}",
            actor_name if actor_user is not None else None,
        )
    if save_as:
        session.add(
            SavedJob(
                name=save_as,
                fun=fun,
                tgt=tgt,
                tgt_type=tgt_type,
                args=args,
                batch=batch,
            )
        )
        session.commit()
    # A batch of beacons.list carries the same forced kwargs as an
    # interactive fire, or the positional token would skip the rule.
    wave_args, wave_kwarg = _force_beacon_kwargs(fun, args, None)
    job = queue_or_none(
        run_wave_batch_task,
        group,
        waves,
        fun,
        wave_args,
        batch["stop_after"],
        actor_name,
        wave_kwarg,
    )
    if job is None:
        result = run_wave_batch(
            group,
            waves,
            fun,
            wave_args,
            batch["stop_after"],
            actor_name,
            kwarg=wave_kwarg,
        )
        flash(
            f"Batch {result['status']}: "
            f"{result['failures']} failures over {len(waves)} waves.",
            "warning",
        )
    else:
        flash(f"Batch queued: {len(waves)} waves.", "success")
    return redirect(url_for("jobs.detail", jid=f"batch-{group}"))
