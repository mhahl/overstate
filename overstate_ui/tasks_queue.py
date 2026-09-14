"""RQ plumbing for background Salt queries.

Queue access, short waits, app contexts, and the capability cache.
Views enqueue with :func:`queue_or_none`. The dashboard never blocks on
jobs: it renders instantly and polls their state with :func:`describe_job`
until each panel resolves to live data or the snapshot fallback.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from contextlib import contextmanager
from typing import Any

QUEUE_NAME = "salt"
JOB_TIMEOUT = 300
RESULT_TTL = 600
CAPABILITY_CACHE_KEY = "salt:capabilities"
CAPABILITY_TTL = 300


def get_redis_client():
    """Redis from app config. Raises redis RedisError when unreachable."""
    import redis
    from flask import current_app

    return redis.from_url(
        current_app.config["REDIS_URL"], socket_connect_timeout=2, socket_timeout=5
    )


def queue_or_none(
    func: Callable, *args: Any, job_timeout: int = JOB_TIMEOUT, **kwargs: Any
):
    """Enqueue func; return the Job, or None when Redis is unreachable."""
    import redis
    from rq import Queue

    try:
        queue = Queue(QUEUE_NAME, connection=get_redis_client())
        return queue.enqueue(
            func, *args, job_timeout=job_timeout, result_ttl=RESULT_TTL, **kwargs
        )
    except redis.exceptions.RedisError:
        return None


def describe_job(jid: str | None) -> tuple[str, Any]:
    """Non-blocking job state for dashboard polling. Never raises and
    never touches Salt: only fast Redis lookups. Returns ("ready",
    result), ("waiting", None), or ("gone", None) for missing, failed,
    expired, or unreachable jobs — "gone" panels fall back to snapshot
    data so polling always terminates."""
    if not jid:
        return ("gone", None)
    import redis
    from rq import Queue

    try:
        job = Queue(QUEUE_NAME, connection=get_redis_client()).fetch_job(jid)
    except redis.exceptions.RedisError:
        return ("gone", None)
    if job is None:
        return ("gone", None)
    try:
        state = job.get_status()
        result = job.result if state == "finished" else None
    except redis.exceptions.RedisError:
        return ("gone", None)
    if state == "finished" and result is not None:
        return ("ready", result)
    if state in ("failed", "stopped", "canceled"):
        return ("gone", None)
    return ("waiting", None)


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
        verify=current_app.config["SALT_API_VERIFY"],
    )


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
        get_redis_client().set(
            CAPABILITY_CACHE_KEY, json.dumps(payload), ex=CAPABILITY_TTL
        )
    except redis.exceptions.RedisError:
        pass
