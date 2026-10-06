"""Mine browser: read Salt mine data by target and function.

Read-only: mine.get through one minion. Refreshing stays on the
existing mine-update run preset; mine.delete/flush have no UI.
"""

import httpx
from flask import Blueprint, render_template, request
from flask_login import login_required

from .dashboard import get_salt, ping_target
from .jobs import TGT_TYPES, resolve_group_target
from .jobs_helpers import FUN_RE
from .salt_client import SaltApiError

bp = Blueprint("mine", __name__, url_prefix="/mine")


@bp.route("/")
@login_required
def index():
    tgt = request.args.get("tgt", "").strip()
    tgt_type = request.args.get("tgt_type", "glob")
    if tgt_type not in TGT_TYPES:
        tgt_type = "glob"
    fun = request.args.get("fun", "").strip()
    direction = request.args.get("dir", "asc")
    if direction not in ("asc", "desc"):
        direction = "asc"
    from flask import abort
    from flask_login import current_user

    from .authz import (
        AuthzDenied,
        audit_deny,
        constrain_target_verbose,
        rbac_mode,
        require,
    )

    scoped = rbac_mode() == "scoped"
    entries: dict = {}
    error = None
    if fun and not FUN_RE.match(fun):
        error = "Invalid function name."
    if tgt and fun and error is None:
        query_tgt, query_type = tgt, tgt_type
        hit: set[str] | None = None
        if scoped:
            # Intersect before queue and before the inline fallback; the
            # worker receives the already-constrained target and the
            # reader is an in-scope id, never ping_target().
            require("mine.read")
            try:
                query_tgt, query_type, _, hit = constrain_target_verbose(
                    current_user, "mine.read", tgt, tgt_type
                )
            except AuthzDenied as exc:
                audit_deny(exc.perm, detail=exc.detail)
                abort(403)
            if hit is None:
                # Fleet grant: the target publishes unchanged; read
                # through the snapshot head, as the legacy path does.
                reader = ping_target()
                if reader is None:
                    error = "No minions in the snapshot cache."
            else:
                reader = min(hit)
        elif tgt_type == "group":
            try:
                targets, _stale = resolve_group_target(tgt)
            except SaltApiError as exc:
                error = str(exc)
            else:
                query_tgt, query_type = ",".join(targets), "list"
        if error is None:
            if not query_tgt:
                error = f"group '{tgt}' matches no known minions"
            elif not scoped:
                reader = ping_target()
                if reader is None:
                    error = "No minions in the snapshot cache."
        if error is None:
            from .tasks import mine_get_task, queue_or_none, wait_for

            try:
                queued = queue_or_none(
                    mine_get_task, reader, query_tgt, fun, query_type
                )
                if queued is None:
                    from .tasks import mine_get_now

                    entries = mine_get_now(
                        get_salt(), reader, query_tgt, fun, query_type
                    )
                else:
                    status, value = wait_for(queued, wait=6.0)
                    if status == "ready":
                        entries = value
                    elif status == "pending":
                        error = "Mine query still running. reload to retry."
                    else:
                        error = f"Mine query failed: {value}"
            except (SaltApiError, httpx.HTTPError) as exc:
                error = f"salt-api error: {exc}"
        if scoped and isinstance(entries, dict) and hit is not None:
            # A stale worker argument cannot add db-01 back.
            entries = {k: v for k, v in entries.items() if k in hit}
    return render_template(
        "mine.html",
        tgt=tgt,
        tgt_type=tgt_type,
        tgt_types=TGT_TYPES,
        fun=fun,
        entries=entries,
        error=error,
        direction=direction,
    )
