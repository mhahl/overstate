"""Gated wave batches and orchestration runs over RQ workers."""

from __future__ import annotations

from typing import Any

import httpx

from .models import JobReturn
from .salt_client import SaltApiError
from .tasks_queue import app_context, get_redis_client

BATCH_CANCEL_TTL = 3600


def split_roster(roster: list[str], mode: str, size: int) -> list[list[str]]:
    """Split an ordered roster into waves. mode is count or percent."""
    if mode == "percent":
        size = max(1, -(-len(roster) * size // 100))
    size = max(1, size)
    return [roster[i : i + size] for i in range(0, len(roster), size)]


def batch_cancel_key(group: str) -> str:
    return f"salt:batch:{group}:cancel"


def request_batch_cancel(group: str) -> bool:
    import redis

    try:
        get_redis_client().set(batch_cancel_key(group), "1", ex=BATCH_CANCEL_TTL)
        return True
    except redis.exceptions.RedisError:
        return False


def batch_cancelled(group: str) -> bool:
    import redis

    try:
        return bool(get_redis_client().get(batch_cancel_key(group)))
    except redis.exceptions.RedisError:
        return False


def _parent_state(session, group: str) -> tuple:
    """Return (parent, fresh state dict). Reassign after mutating."""
    from .models import Job

    parent = session.get(Job, f"batch-{group}")
    return parent, dict(parent.batch_state or {})


def run_wave_batch(
    group: str,
    waves: list[list[str]],
    fun: str,
    args: list[str],
    stop_after: int,
    user: str,
    wave_timeout: float = 600.0,
) -> dict:
    """Run waves with a failure gate. Worker task or inline fallback."""
    import time

    from .audit import log_event
    from .db import get_session
    from .jobs import sync_job
    from .models import Job, JobReturn
    from .tasks import batch_cancelled, build_client

    session = get_session()
    client = build_client()
    failures = 0
    missing_total = 0
    status = "complete"
    for num, wave in enumerate(waves, 1):
        if batch_cancelled(group):
            status = "cancelled"
            log_event(user, f"batch-cancelled:{group}")
            break
        targets = sorted(wave)
        try:
            result = client.local(
                targets, fun, arg=args, tgt_type="list", asynchronous=True
            )
            jid = result[0]["jid"] if isinstance(result, list) else result["jid"]
        except (SaltApiError, httpx.HTTPError, KeyError, IndexError, TypeError):
            child_jid = f"batch-{group}-w{num}"
            session.add(
                Job(
                    jid=child_jid,
                    fun=fun,
                    tgt=",".join(targets),
                    tgt_type="list",
                    user=user,
                    batch_group=group,
                    complete=True,
                )
            )
            session.commit()
            failures += len(targets)
            log_event(user, f"batch-wave-failed:{group}:{num}", jid=child_jid)
            if failures >= stop_after:
                status = "stopped"
                break
            continue
        session.add(
            Job(
                jid=jid,
                fun=fun,
                tgt=",".join(targets),
                tgt_type="list",
                user=user,
                batch_group=group,
            )
        )
        session.commit()
        deadline = time.monotonic() + wave_timeout
        while True:
            sync_job(jid)
            done = session.query(JobReturn).filter_by(jid=jid).count() >= len(targets)
            if done or time.monotonic() >= deadline:
                break
            time.sleep(5)
        returned = session.query(JobReturn).filter_by(jid=jid).count()
        wave_failures = (
            session.query(JobReturn).filter_by(jid=jid, success=False).count()
        )
        # Silent minions trip the failure gate too: no return by the
        # deadline counts the same as a failed return.
        missing = max(0, len(targets) - returned)
        missing_total += missing
        failures += wave_failures + missing
        log_event(user, f"batch-wave:{group}:{num}", jid=jid)
        parent, state = _parent_state(session, group)
        state.update(
            failures=failures,
            waves_done=num,
            status="running",
            missing=missing_total,
        )
        parent.batch_state = state
        session.commit()
        if failures >= stop_after:
            status = "stopped"
            log_event(user, f"batch-stopped:{group}")
            break
    parent = session.get(Job, f"batch-{group}")
    if parent is not None:
        parent.complete = True
        _, state = _parent_state(session, group)
        state.update(failures=failures, status=status, missing=missing_total)
        parent.batch_state = state
        session.commit()
    log_event(user, f"batch-{status}:{group}")
    return {"group": group, "status": status, "failures": failures, "waves": len(waves)}


def run_wave_batch_task(
    group: str,
    waves: list[list[str]],
    fun: str,
    args: list[str],
    stop_after: int,
    user: str,
) -> dict:
    with app_context():
        return run_wave_batch(group, waves, fun, args, stop_after, user)


def _orch_success(payload: Any) -> bool:
    """Scan orchestration output for any state reporting result False."""
    if isinstance(payload, dict):
        if payload.get("result") is False:
            return False
        return all(_orch_success(v) for v in payload.values())
    if isinstance(payload, list):
        return all(_orch_success(v) for v in payload)
    return True


def _store_job_return(session, jid: str, mid: str, success: bool, payload) -> None:
    """Insert or update one job return: retries must merge, not duplicate."""
    existing = session.query(JobReturn).filter_by(jid=jid, minion_id=mid).first()
    if existing is None:
        session.add(
            JobReturn(
                jid=jid,
                minion_id=mid,
                success=success,
                retcode=0 if success else 1,
                payload=payload,
            )
        )
    else:
        existing.success = success
        existing.payload = payload


def run_orchestrate_task(
    jid: str, mods: str, saltenv: str, test: bool, pillar: dict, user: str
) -> dict:
    """Run state.orchestrate and store its output as job returns."""
    from .audit import log_event
    from .db import get_session
    from .models import Job
    from .tasks import build_client

    with app_context():
        client = build_client()
        try:
            result = client.runner(
                "state.orchestrate",
                mods=mods,
                saltenv=saltenv or "base",
                pillar=pillar or {},
                test=test,
            )
        except (SaltApiError, httpx.HTTPError) as exc:
            # A denied or unreachable orchestrate must land in History as
            # a visible failure, never sit Running forever.
            session = get_session()
            job = session.get(Job, jid)
            if job is not None:
                job.complete = True
            _store_job_return(session, jid, "master", False, {"output": str(exc)})
            session.commit()
            log_event(user, f"orchestrate-failed:{mods}", jid=jid)
            return {"jid": jid, "minions": 0, "error": str(exc)}
        returns = result[0] if isinstance(result, list) else result
        if not isinstance(returns, dict):
            returns = {"output": returns}
        session = get_session()
        job = session.get(Job, jid)
        if job is not None:
            job.complete = True
        for mid, payload in returns.items():
            if not isinstance(payload, dict):
                payload = {"output": payload}
            _store_job_return(session, jid, str(mid), _orch_success(payload), payload)
        session.commit()
        return {"jid": jid, "minions": len(returns)}
