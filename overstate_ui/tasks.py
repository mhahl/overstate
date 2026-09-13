"""Background Salt queries over RQ.

Long or fleet-wide Salt calls run in a worker so the request thread
stays fast. Views enqueue with :func:`queue_or_none` and wait briefly
with :func:`wait_for`; when Redis is unreachable the job is None and
the view runs the same code synchronously. Task functions must stay
importable by path and JSON-serializable in and out.
"""

from __future__ import annotations

import json
import time
from contextlib import contextmanager
from typing import Any, Callable

import httpx

from .salt_client import SaltApiError

QUEUE_NAME = "salt"
JOB_TIMEOUT = 300
RESULT_TTL = 600
CAPABILITY_CACHE_KEY = "salt:capabilities"
CAPABILITY_TTL = 300

CAPABILITY_CHECKS = [
    {"key": "wheel_ok", "feature": "Keys", "fun": "key.list_all",
     "grant": "Grant @wheel to the eauth user"},
    {"key": "runner_ok", "feature": "Presence", "fun": "manage.status",
     "grant": "Grant @runner to the eauth user"},
    {"key": "history_ok", "feature": "Job history",
     "fun": "returner tables",
     "grant": "Set master_job_cache: pgjsonb on the master"},
    {"key": "ping_ok", "feature": "Run jobs", "fun": "test.ping",
     "grant": "Grant execution functions to the eauth user"},
]


def get_redis_client():
    """Redis from app config. Raises redis RedisError when unreachable."""
    import redis
    from flask import current_app

    return redis.from_url(
        current_app.config["REDIS_URL"],
        socket_connect_timeout=2, socket_timeout=5)


def get_queue():
    from rq import Queue

    return Queue(QUEUE_NAME, connection=get_redis_client())


def queue_or_none(func: Callable, *args: Any,
                  job_timeout: int = JOB_TIMEOUT, **kwargs: Any):
    """Enqueue func; return the Job, or None when Redis is unreachable."""
    import redis
    from rq import Queue

    try:
        queue = Queue(QUEUE_NAME, connection=get_redis_client())
        return queue.enqueue(func, *args, job_timeout=job_timeout,
                             result_ttl=RESULT_TTL, **kwargs)
    except redis.exceptions.RedisError:
        return None


def wait_for(job, wait: float = 8.0) -> tuple[str, Any]:
    """Wait up to `wait` seconds for a queued job.

    Returns (status, value): "ready" with the return value, "pending"
    when still running, "error" with the failure line.
    """
    deadline = time.monotonic() + wait
    while True:
        job.refresh()
        state = job.get_status()
        if state == "finished":
            return ("ready", job.result)
        if state == "failed":
            info = (job.exc_info or "").strip().splitlines()
            return ("error", info[-1] if info else "worker failed")
        if time.monotonic() >= deadline:
            return ("pending", None)
        time.sleep(0.25)


def build_client():
    """Fresh SaltClient from the current app config (view or worker)."""
    from flask import current_app

    from .salt_client import SaltClient

    return SaltClient(
        current_app.config["SALT_API_URL"],
        current_app.config["SALT_EAUTH_USER"],
        current_app.config["SALT_EAUTH_PASSWORD"],
        current_app.config["SALT_EAUTH_TYPE"],
        verify=current_app.config["SALT_API_VERIFY"])


@contextmanager
def app_context():
    """Current app when one is active, else an isolated worker app.

    Inline fallbacks run inside the view's context and must not tear
    it down; worker jobs build and clean up their own.
    """
    from flask import current_app

    try:
        yield current_app._get_current_object()
    except RuntimeError:
        with isolated_app() as app:
            yield app


@contextmanager
def isolated_app():
    """App context for worker jobs. Tears down its own DB session."""
    from . import create_app
    from .db import close_session

    app = create_app()
    ctx = app.app_context()
    ctx.push()
    try:
        yield app
    finally:
        try:
            close_session()
        finally:
            # Re-read the registry late: init_db rebinds it, so an
            # early import would hold a stale (possibly None) value.
            from .db import _Session

            if _Session is not None:
                _Session.remove()
            ctx.pop()


def refresh_now(client) -> int:
    """Fleet grains refresh. Shared by the view fallback and the task."""
    from .inventory import refresh_inventory
    from .minions import live_roster

    statuses, _ = live_roster(client)
    return refresh_inventory(client, statuses)


def refresh_inventory_task() -> dict:
    with isolated_app():
        return {"count": refresh_now(build_client())}


def salt_overview_now(client) -> dict:
    """Key and presence counts. Raises SaltApiError on failure."""
    keys = client.wheel("key.list_all")[0]["data"]["return"]
    accepted = keys.get("minions", [])
    pending = keys.get("minions_pre", [])
    status = client.runner("manage.status")[0]
    return {"reachable": True, "accepted": len(accepted),
            "pending": len(pending), "up": len(status.get("up", [])),
            "down": len(status.get("down", []))}


def salt_overview_task() -> dict:
    with isolated_app():
        return salt_overview_now(build_client())


def normalize_versions(payload) -> dict[str, int]:
    """{version: count} from a manage.versions return. Verified live:
    grouped labels map to {minion: version-string} plus a "Master"
    key ({"Up to date": {"m1": "3008.2"}, "Master": "3008.2"}).
    The flat {minion: version} shape counts too. Anything else
    (offline bools, strings, None) yields {} and the caller falls
    back to grain snapshots."""
    counts: dict[str, int] = {}
    if not isinstance(payload, dict):
        return counts
    for key, value in payload.items():
        if key == "Master":
            continue
        if isinstance(value, dict):
            for version in value.values():
                if isinstance(version, str):
                    counts[version] = counts.get(version, 0) + 1
        elif isinstance(value, str):
            counts[value] = counts.get(value, 0) + 1
    return counts


def fleet_truth_now(client) -> dict:
    """Live versions + master-active JIDs. Never raises: each panel
    falls back independently when its call fails or surprises."""
    out: dict = {"versions": {}, "active_jids": [],
                 "versions_live": False, "active_live": False}
    try:
        versions = normalize_versions(client.runner("manage.versions")[0])
    except (SaltApiError, httpx.HTTPError, KeyError, IndexError, TypeError):
        versions = {}
    if versions:
        out["versions"] = versions
        out["versions_live"] = True
    try:
        active = client.runner("jobs.active")[0]
    except (SaltApiError, httpx.HTTPError, KeyError, IndexError, TypeError):
        active = None
    # Only JID-shaped keys count: anything else is not a jobs.active
    # payload (e.g. an unexpected dict), so fall back to the DB count
    # instead of reporting a false live zero.
    if isinstance(active, dict) and all(
            isinstance(k, str) and k.isdigit() for k in active):
        out["active_jids"] = sorted(active)
        out["active_live"] = True
    return out


def fleet_truth_task() -> dict:
    with isolated_app():
        return fleet_truth_now(build_client())


def list_functions_now(client, minion: str) -> list[str]:
    """Live execution-function index from one minion.

    Raises SaltApiError on failure (caller falls back to presets).
    """
    payload = client.local(minion, "sys.list_functions")[0].get(minion, [])
    names: set[str] = set()
    if isinstance(payload, dict):
        for funs in payload.values():
            if isinstance(funs, list):
                names.update(str(f) for f in funs)
    elif isinstance(payload, list):
        names.update(str(f) for f in payload)
    return sorted(names)


def fun_index_task(minion: str) -> list[str]:
    with isolated_app():
        return list_functions_now(build_client(), minion)


def show_sls_now(client, minion: str, sls_list: list[str],
                 via: str = "local") -> dict:
    """Render SLS files via state.show_sls on one minion.

    Returns {sls: {state-id: ...}}. Raises SaltApiError on failure;
    the view degrades to the unavailable note. Tolerates string,
    dict, and None payloads per file.
    """
    rendered: dict = {}
    for sls in sls_list:
        payload = client.local(minion, "state.show_sls", arg=[sls],
                               timeout=30, via=via)[0].get(minion)
        if isinstance(payload, dict):
            rendered[sls] = payload
    return rendered


def show_sls_task(minion: str, sls_list: list[str],
                  via: str = "local") -> dict:
    with isolated_app():
        return show_sls_now(build_client(), minion, sls_list, via)


def mine_get_now(client, reader: str, tgt: str, fun: str,
                 tgt_type: str = "glob") -> dict:
    """Mine values for a target expression, read through one minion.

    Raises SaltApiError on failure. Non-mapping payloads yield {}
    (caller shows the empty state).
    """
    payload = client.local(reader, "mine.get", arg=[tgt, fun],
                           kwarg={"tgt_type": tgt_type})[0].get(reader, {})
    return payload if isinstance(payload, dict) else {}


def mine_get_task(reader: str, tgt: str, fun: str,
                  tgt_type: str = "glob") -> dict:
    with isolated_app():
        return mine_get_now(build_client(), reader, tgt, fun, tgt_type)


FUN_DOC_LINES = 40


def fun_doc_now(client, minion: str, fun: str) -> str:
    """Trimmed sys.doc text for one function. Empty when Salt says
    nothing (caller renders the no-docs note)."""
    payload = client.local(minion, "sys.doc", arg=[fun])[0].get(minion, {})
    text = payload.get(fun, "") if isinstance(payload, dict) else payload
    return "\n".join(str(text or "").strip().splitlines()[:FUN_DOC_LINES])


def probe_capabilities(client, ping_target: str | None = None) -> dict:
    """One probe per door the UI depends on. Never raises for Salt."""
    out: dict[str, Any] = {c["key"]: False for c in CAPABILITY_CHECKS}
    out["ping_target"] = ping_target
    out["error"] = None
    try:
        client.wheel("key.list_all")
        out["wheel_ok"] = True
        client.runner("manage.status")
        out["runner_ok"] = True
        from .db import get_session
        from .models import SaltReturn

        get_session().query(SaltReturn.jid).limit(1).all()
        out["history_ok"] = True
        if ping_target:
            client.local(ping_target, "test.ping")
            out["ping_ok"] = True
    except (SaltApiError, httpx.HTTPError) as exc:
        out["error"] = str(exc)
    except Exception as exc:  # noqa: BLE001 — DB problems degrade too
        out["error"] = str(exc)
    return out


def read_capability_cache() -> dict | None:
    import redis

    try:
        raw = get_redis_client().get(CAPABILITY_CACHE_KEY)
        return json.loads(raw) if raw else None
    except (redis.exceptions.RedisError, ValueError):
        return None


def write_capability_cache(payload: dict) -> None:
    import redis

    try:
        get_redis_client().set(CAPABILITY_CACHE_KEY, json.dumps(payload),
                               ex=CAPABILITY_TTL)
    except redis.exceptions.RedisError:
        pass


def capabilities_task(ping_target: str | None = None) -> dict:
    with isolated_app():
        out = probe_capabilities(build_client(), ping_target)
        write_capability_cache(out)
        return out


BATCH_CANCEL_TTL = 3600


def split_roster(roster: list[str], mode: str, size: int) -> list[list[str]]:
    """Split an ordered roster into waves. mode is count or percent."""
    if mode == "percent":
        size = max(1, -(-len(roster) * size // 100))
    size = max(1, size)
    return [roster[i:i + size] for i in range(0, len(roster), size)]


def batch_cancel_key(group: str) -> str:
    return f"salt:batch:{group}:cancel"


def request_batch_cancel(group: str) -> bool:
    import redis

    try:
        get_redis_client().set(batch_cancel_key(group), "1",
                               ex=BATCH_CANCEL_TTL)
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


def run_wave_batch(group: str, waves: list[list[str]], fun: str,
                   args: list[str], stop_after: int, user: str,
                   wave_timeout: float = 600.0) -> dict:
    """Run waves with a failure gate. Worker task or inline fallback."""
    import time

    from .audit import log_event
    from .db import get_session
    from .jobs import sync_job
    from .models import Job, JobReturn

    session = get_session()
    client = build_client()
    failures = 0
    status = "complete"
    for num, wave in enumerate(waves, 1):
        if batch_cancelled(group):
            status = "cancelled"
            log_event(user, f"batch-cancelled:{group}")
            break
        targets = sorted(wave)
        try:
            result = client.local(targets, fun, arg=args, tgt_type="list",
                                  asynchronous=True)
            jid = (result[0]["jid"] if isinstance(result, list)
                   else result["jid"])
        except (SaltApiError, httpx.HTTPError, KeyError, IndexError,
                TypeError):
            child_jid = f"batch-{group}-w{num}"
            session.add(Job(jid=child_jid, fun=fun,
                            tgt=",".join(targets), tgt_type="list",
                            user=user, batch_group=group, complete=True))
            session.commit()
            failures += len(targets)
            log_event(user, f"batch-wave-failed:{group}:{num}", jid=child_jid)
            if failures >= stop_after:
                status = "stopped"
                break
            continue
        session.add(Job(jid=jid, fun=fun, tgt=",".join(targets),
                        tgt_type="list", user=user, batch_group=group))
        session.commit()
        deadline = time.monotonic() + wave_timeout
        while True:
            sync_job(jid)
            done = (session.query(JobReturn).filter_by(jid=jid).count()
                    >= len(targets))
            if done or time.monotonic() >= deadline:
                break
            time.sleep(5)
        wave_failures = (session.query(JobReturn)
                         .filter_by(jid=jid, success=False).count())
        failures += wave_failures
        log_event(user, f"batch-wave:{group}:{num}", jid=jid)
        parent, state = _parent_state(session, group)
        state.update(failures=failures, waves_done=num, status="running")
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
        state.update(failures=failures, status=status)
        parent.batch_state = state
        session.commit()
    log_event(user, f"batch-{status}:{group}")
    return {"group": group, "status": status, "failures": failures,
            "waves": len(waves)}


def run_wave_batch_task(group: str, waves: list[list[str]], fun: str,
                        args: list[str], stop_after: int,
                        user: str) -> dict:
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


def run_orchestrate_task(jid: str, mods: str, saltenv: str, test: bool,
                         pillar: dict, user: str) -> dict:
    """Run state.orchestrate and store its output as job returns."""
    from .db import get_session
    from .models import Job, JobReturn

    with app_context():
        client = build_client()
        result = client.runner("state.orchestrate", mods=mods,
                               saltenv=saltenv or "base", pillar=pillar or {},
                               test=test)
        returns = (result[0] if isinstance(result, list) else result)
        if not isinstance(returns, dict):
            returns = {"output": returns}
        session = get_session()
        job = session.get(Job, jid)
        if job is not None:
            job.complete = True
        for mid, payload in returns.items():
            if not isinstance(payload, dict):
                payload = {"output": payload}
            session.add(JobReturn(jid=jid, minion_id=str(mid),
                                  success=_orch_success(payload), retcode=0,
                                  payload=payload))
        session.commit()
        return {"jid": jid, "minions": len(returns)}
