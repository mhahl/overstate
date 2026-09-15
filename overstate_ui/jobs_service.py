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


def sync_job(jid: str) -> Job | None:
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
            user=current_user.username if current_user.is_authenticated else "unknown",
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


SYNC_SALT_TIMEOUT = 60
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


def launch(
    tgt: str,
    tgt_type: str,
    fun: str,
    args: list,
    asynchronous: bool,
    via: str = "local",
) -> str:
    """Fire a job via salt-api; record the Job row. Returns the jid."""
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
            user=current_user.username,
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
        log_event(current_user.username, f"run-ssh:{fun}", jid=jid)
        return jid
    if asynchronous:
        result = client.local(tgt, fun, arg=args, tgt_type=tgt_type, asynchronous=True)
        jid = result[0]["jid"] if isinstance(result, list) else result["jid"]
        session = get_session()
        session.add(
            Job(
                jid=jid,
                fun=fun,
                tgt=tgt,
                tgt_type=tgt_type,
                user=current_user.username,
            )
        )
        session.commit()
        log_event(current_user.username, f"run:{fun}", jid=jid)
        return jid
    result = client.local(
        tgt,
        fun,
        arg=args,
        tgt_type=tgt_type,
        timeout=SYNC_SALT_TIMEOUT,
        http_timeout=SYNC_SALT_TIMEOUT + 5,
    )
    real_jid, mapping = _split_sync_result(result)
    jid = real_jid or f"sync-{int(time.time())}"
    session = get_session()
    job = Job(jid=jid, fun=fun, tgt=tgt, tgt_type=tgt_type, user=current_user.username)
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
    if mapping:
        job.complete = True
    session.commit()
    log_event(current_user.username, f"run:{fun}", jid=jid)
    return jid


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
    tgt: str, tgt_type: str, fun: str, args: list, batch: dict, save_as: str = ""
):
    """Start a gated wave batch: parent row, enqueue or run inline."""
    import uuid

    from .tasks import queue_or_none, run_wave_batch, run_wave_batch_task

    roster = resolve_batch_roster(tgt, tgt_type)
    if roster is None:
        flash("Batch mode supports list, glob, and group targets.", "error")
        return redirect(url_for("jobs.new"))
    if not roster:
        flash("Batch roster is empty: no known minions match.", "error")
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
            user=current_user.username,
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
    log_event(current_user.username, f"batch-start:{group}")
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
    job = queue_or_none(
        run_wave_batch_task,
        group,
        waves,
        fun,
        args,
        batch["stop_after"],
        current_user.username,
    )
    if job is None:
        result = run_wave_batch(
            group, waves, fun, args, batch["stop_after"], current_user.username
        )
        flash(
            f"Batch {result['status']}: "
            f"{result['failures']} failures over {len(waves)} waves.",
            "warning",
        )
    else:
        flash(f"Batch queued: {len(waves)} waves. Watch this page.", "success")
    return redirect(url_for("jobs.detail", jid=f"batch-{group}"))
