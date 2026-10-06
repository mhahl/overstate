"""Token API for service accounts: ``POST /api/jobs/run``.

Bearer-only. Scoped mode only: the endpoint 403s unless ``rbac_mode``
is ``scoped``, so a token can never ride the legacy ladder (a service
account's ``role`` is ``none``). No sessions, no cookies, no CSRF — the
single view is CSRF-exempt because a Bearer [REDACTED] not a browser
credential.

Body forms::

    {"saved_id": 7}                                  # pinned: stored target+fun
    {"fun": "test.ping", "tgt": "web-*", ...}        # unpinned: read class only

Both forms fire through :func:`jobs_service.launch` as the token owner,
so ``constrain_target`` and audit attribution apply unchanged. Batch
saved jobs are interactive-only and rejected here.
"""

import datetime as dt
import time

from flask import Blueprint, jsonify, request

from .audit import log_event
from .authz import API_READ_FUNS, AuthzDenied, audit_deny, rbac_mode
from .db import get_session
from .models import ApiToken, SavedJob, User

bp = Blueprint("api", __name__, url_prefix="/api")

#: Token fire budget: 60 requests per minute per token id.
TOKEN_RATE_LIMIT = 60
TOKEN_RATE_WINDOW = 60

_attempts: dict[str, list[float]] = {}


def _redis_client_or_none():
    """Redis for the token budget; None when unreachable."""
    try:
        import redis
        from flask import current_app, has_app_context

        if not has_app_context():
            return None
        return redis.from_url(
            current_app.config["REDIS_URL"],
            socket_connect_timeout=2,
            socket_timeout=5,
        )
    except Exception:  # noqa: BLE001 — fall back to process memory below
        return None


def _memory_limited(key: str, limit: int, window: int) -> bool:
    now = time.monotonic()
    hits = [t for t in _attempts.get(key, []) if now - t < window]
    if len(hits) >= limit:
        _attempts[key] = hits
        return True
    hits.append(now)
    _attempts[key] = hits
    return False


def _token_limited(token_id: int) -> bool:
    key = f"api-token:{token_id}"
    client = _redis_client_or_none()
    if client is not None:
        try:
            count = client.incr(key)
            if count == 1:
                client.expire(key, TOKEN_RATE_WINDOW)
            return count > TOKEN_RATE_LIMIT
        except Exception:
            pass
    return _memory_limited(key, TOKEN_RATE_LIMIT, TOKEN_RATE_WINDOW)


def _parse_bearer(value: str | None) -> tuple[str, str] | None:
    """Split ``<prefix>_<secret>`` from an Authorization header value."""
    if not value or not value.startswith("Bearer "):
        return None
    raw = value[len("Bearer "):].strip()
    prefix, sep, secret = raw.partition("_")
    if not sep or not prefix or not secret:
        return None
    return prefix, secret


def _authenticate() -> tuple:
    """Verify the Bearer [REDACTED] all prefix rows; (user, row, error).

    Every candidate row with a matching prefix is verified — evaluation
    does not stop at the first row, so a revoked or expired token sharing
    a prefix cannot shadow the live one. Returns the token owner and row
    on success, else an error payload with its status code.
    """
    from .auth import _ph

    parsed = _parse_bearer(request.headers.get("Authorization"))
    if parsed is None:
        return None, None, ({"ok": False, "error": "bearer-required"}, 401)
    prefix, secret = parsed
    # The stored hash covers the whole raw token, prefix included: the
    # prefix is a lookup accelerator, not a second factor.
    raw = f"{prefix}_{secret}"
    session = get_session()
    rows = session.query(ApiToken).filter_by(token_prefix=prefix).all()
    live = None
    for row in rows:
        try:
            valid = _ph.verify(row.token_hash, raw)
        except Exception:
            valid = False
        if not valid:
            continue
        if row.revoked_at is not None:
            continue
        if row.expires_at is not None and row.expires_at.replace(
            tzinfo=dt.UTC
        ) <= dt.datetime.now(dt.UTC):
            continue
        if live is None:
            live = row
    if live is None:
        return None, None, ({"ok": False, "error": "invalid-token"}, 401)
    user = session.get(User, live.user_id)
    if user is None:
        return None, None, ({"ok": False, "error": "invalid-token"}, 401)
    if _token_limited(live.id):
        return None, None, ({"ok": False, "error": "rate-limited"}, 429)
    live.last_used_at = dt.datetime.now(dt.UTC).replace(tzinfo=None)
    session.commit()
    return user, live, None


def _pinned_fire(user, token_row, saved_id: object):
    from .jobs_service import launch

    session = get_session()
    try:
        pin = int(saved_id)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "unknown-saved-job"}), 404
    saved = session.get(SavedJob, pin)
    if saved is None or token_row.saved_job_id != saved.id:
        # Unknown ids and other tokens' pins look identical: a scanner
        # learns nothing about which saved jobs exist.
        audit_deny("job.run", detail="pin-mismatch", actor=user.username)
        return jsonify({"ok": False, "error": "unknown-saved-job"}), 404
    if saved.batch:
        return (
            jsonify({"ok": False, "error": "batch-saved-jobs-are-interactive-only"}),
            400,
        )
    try:
        jid = launch(
            saved.tgt,
            saved.tgt_type,
            saved.fun,
            list(saved.args or []),
            True,
            tgt_requested=saved.tgt,
            actor_user=user,
        )
    except AuthzDenied:
        # launch already wrote the deny row with the actor name.
        return jsonify({"ok": False, "error": "forbidden"}), 403
    return jsonify({"ok": True, "jid": jid}), 200


def _unpinned_fire(user, body: dict):
    from .jobs_service import launch

    fun = body.get("fun")
    if not isinstance(fun, str) or fun not in API_READ_FUNS:
        audit_deny(str(fun), detail="not-api-readable", actor=user.username)
        return jsonify({"ok": False, "error": "function-not-allowed"}), 403
    tgt = body.get("tgt", "")
    tgt_type = body.get("tgt_type", "glob")
    args = body.get("args", [])
    kwarg = body.get("kwarg")
    if not isinstance(tgt, str) or tgt_type not in ("glob", "list", "group"):
        return jsonify({"ok": False, "error": "bad-target"}), 400
    if not isinstance(args, list) or (kwarg is not None and not isinstance(kwarg, dict)):
        return jsonify({"ok": False, "error": "bad-args"}), 400
    try:
        jid = launch(
            tgt, tgt_type, fun, args, True, kwarg=kwarg, actor_user=user
        )
    except AuthzDenied:
        # launch already wrote the deny row with the actor name.
        return jsonify({"ok": False, "error": "forbidden"}), 403
    return jsonify({"ok": True, "jid": jid}), 200


@bp.post("/jobs/run")
def run_job():
    """Fire a job as the Bearer token's service-account owner."""
    if rbac_mode() != "scoped":
        return jsonify({"ok": False, "error": "api-requires-scoped-mode"}), 403
    # Sessions are never accepted here: a logged-in browser that forgets
    # the Bearer [REDACTED] an anonymous caller, not a user.
    user, token_row, error = _authenticate()
    if error is not None:
        payload, status = error
        return jsonify(payload), status
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify({"ok": False, "error": "json-body-required"}), 400
    if "saved_id" in body:
        return _pinned_fire(user, token_row, body.get("saved_id"))
    if token_row.saved_job_id is not None:
        # A pinned token runs only its saved job: a free-form body,
        # even a read-class one, is not that job.
        audit_deny("job.run", detail="pin-mismatch", actor=user.username)
        return jsonify({"ok": False, "error": "unknown-saved-job"}), 404
    return _unpinned_fire(user, body)


def log_token_event(actor: str, action: str, prefix: str, name: str) -> None:
    """Audit a token lifecycle event without ever storing the secret."""
    log_event(actor, action, outcome="allow", detail=f"{prefix}:{name}")
