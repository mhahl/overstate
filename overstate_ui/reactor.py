"""Master reactor mapping: list, inspect SLS, add/delete, export for git.

The mapping (event tag -> SLS) is master-config state, read and changed
through salt-api's ``reactor`` runner. SLS bodies are read-only: the UI
manages the mapping, never file contents. The export renders the live
mapping as a master-config YAML block for committing to git by hand.
"""

import re
from pathlib import Path

from flask import (
    Blueprint,
    Response,
    abort,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)
from flask_login import current_user, login_required

from .audit import log_event
from .auth import roles_required
from .dashboard import get_salt
from .events import TAG_CHOICES
from .files import MAX_BYTES, highlight_yaml
from .salt_client import SaltApiError

bp = Blueprint("reactor", __name__, url_prefix="/reactor")

# Event tags are slash-separated globs; quotes and shell metacharacters
# never belong in one.
EVENT_RE = re.compile(r"^[A-Za-z0-9_./*?\[\]{}|+=:,@-]+$")
SLS_RE = re.compile(r"^[A-Za-z0-9_./:\-]+$")
MAX_EVENT_LEN = 256
MAX_SLS_LEN = 512


def roots() -> Path:
    return Path(current_app.config["REACTOR_ROOTS"]).resolve()


def _safe_join(rel: str) -> Path | None:
    """Resolve ``rel`` inside the reactor roots; None on traversal."""
    base = roots()
    try:
        target = (base / rel).resolve()
    except (OSError, ValueError):
        return None
    if target != base and base not in target.parents:
        return None
    return target


def sls_to_rel(sls: str) -> str | None:
    """Map a reactor SLS reference to a path under the reactor roots.

    Accepts ``salt://…``, absolute paths inside the roots, and bare
    relative paths. Anything else is listed but not browsable.
    """
    ref = sls.strip()
    if ref.startswith("salt://"):
        ref = ref[len("salt://") :]
    elif ref.startswith("/"):
        try:
            return str(Path(ref).resolve().relative_to(roots()))
        except (OSError, ValueError):
            return None
    if not ref or ref.startswith((".", "/")) or "\\" in ref:
        return None
    if _safe_join(ref) is None:
        return None
    return ref


def parse_reactor_list(value) -> tuple[dict[str, list[str]], str]:
    """Normalize ``reactor.list`` output to {event: [sls, …]}.

    The runner returns the master's reactor config: usually a list of
    single-key mappings, sometimes a plain mapping. Anything else is
    returned as raw text for the fallback box.
    """
    items: list = []
    if isinstance(value, dict):
        items = [value]
    elif isinstance(value, list):
        items = [v for v in value if isinstance(v, dict)]
    else:
        return {}, "" if value is None else str(value)
    entries: dict[str, list[str]] = {}
    for item in items:
        for event, sls in item.items():
            if not isinstance(event, str):
                continue
            refs = sls if isinstance(sls, list) else [sls]
            entries.setdefault(event, []).extend(str(r) for r in refs)
    return entries, ""


def _unwrap(value):
    """Unwrap salt-api's single-element runner envelope."""
    if isinstance(value, list) and len(value) == 1:
        return value[0]
    return value


def nearest_tag(event: str) -> str:
    """Event-family deep link for a reactor pattern (events viewer)."""
    for choice in TAG_CHOICES:
        if event.startswith(choice):
            return choice
    return "salt/job"


def _yaml_quote(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


def render_export(entries: dict[str, list[str]]) -> str:
    """Render the live mapping as a master-config YAML block for git."""
    lines = ["reactor:"]
    for event in sorted(entries):
        lines.append(f"  - {_yaml_quote(event)}:")
        for sls in entries[event]:
            lines.append(f"    - {_yaml_quote(sls)}")
    return "\n".join(lines) + "\n"


@bp.route("/")
@login_required
def index():
    q = request.args.get("q", "").strip().lower()
    sort = request.args.get("sort", "event")
    if sort not in ("event", "sls"):
        sort = "event"
    direction = request.args.get("dir", "asc")
    if direction not in ("asc", "desc"):
        direction = "asc"
    entries: dict[str, list[str]] = {}
    raw = ""
    error = None
    try:
        value = get_salt().runner("reactor.list", http_timeout=30.0)
        value = _unwrap(value)
        entries, raw = parse_reactor_list(value)
    except SaltApiError as exc:
        error = f"salt-api error: {exc}"
    rows = [
        {
            "event": event,
            "refs": refs,
            "rels": [sls_to_rel(r) for r in refs],
            "tag": nearest_tag(event),
        }
        for event, refs in entries.items()
        if not q or q in event.lower() or any(q in r.lower() for r in refs)
    ]
    key = (
        (lambda r: r["event"].lower())
        if sort == "event"
        else (lambda r: ",".join(r["refs"]).lower())
    )
    rows.sort(key=key, reverse=(direction == "desc"))
    return render_template(
        "reactor.html",
        rows=rows,
        raw=raw,
        error=error,
        q=request.args.get("q", ""),
        sort=sort,
        direction=direction,
        total=len(entries),
    )


@bp.route("/view")
@login_required
def view():
    rel = request.args.get("sls", "")
    target = _safe_join(rel) if rel and sls_to_rel(rel) == rel else None
    if not target or not target.is_file():
        abort(404)
    try:
        data = target.read_bytes()
    except OSError:
        abort(404)
    if len(data) > MAX_BYTES:
        return render_template(
            "reactor_view.html",
            rel=rel,
            content=None,
            reason="too large to display",
            size=len(data),
            max_bytes=MAX_BYTES,
        )
    try:
        content = data.decode("utf-8")
    except UnicodeDecodeError:
        return render_template(
            "reactor_view.html",
            rel=rel,
            content=None,
            reason="not readable text",
            size=len(data),
            max_bytes=MAX_BYTES,
        )
    return render_template(
        "reactor_view.html",
        rel=rel,
        content=content,
        highlighted=highlight_yaml(content),
        reason=None,
        size=None,
    )


@bp.post("/add")
@roles_required("operator")
def add():
    event = request.form.get("event", "").strip()
    sls = request.form.get("sls", "").strip()
    if (
        not event
        or not sls
        or len(event) > MAX_EVENT_LEN
        or len(sls) > MAX_SLS_LEN
        or not EVENT_RE.match(event)
        or not SLS_RE.match(sls)
    ):
        flash("An event pattern and one SLS reference are required.", "error")
        return redirect(url_for("reactor.index"))
    try:
        result = get_salt().runner(
            "reactor.add", event=event, reactors=sls, http_timeout=30.0
        )
    except SaltApiError as exc:
        flash(f"salt-api error: {exc}", "error")
        return redirect(url_for("reactor.index"))
    if isinstance(result, dict) and result.get("result") is False:
        flash(f"{event}: salt did not confirm the add.", "warning")
    else:
        log_event(current_user.username, f"reactor-add:{event}")
        flash(f"{event}: reactor added.", "success")
    return redirect(url_for("reactor.index"))


@bp.route("/delete")
@login_required
def delete_confirm():
    event = request.args.get("event", "")
    if not event:
        flash("Pick a reactor first: an empty selection never fires.", "error")
        return redirect(url_for("reactor.index"))
    refs: list[str] = []
    try:
        value = get_salt().runner("reactor.list", http_timeout=30.0)
        value = _unwrap(value)
        entries, _ = parse_reactor_list(value)
        refs = entries.get(event, [])
    except SaltApiError:
        refs = []
    return render_template("reactor_confirm.html", event=event, refs=refs)


@bp.post("/delete")
@roles_required("operator")
def delete():
    event = request.form.get("event", "").strip()
    if not event:
        flash("Pick a reactor first: an empty selection never fires.", "error")
        return redirect(url_for("reactor.index"))
    try:
        get_salt().runner("reactor.delete", event=event, http_timeout=30.0)
    except SaltApiError as exc:
        flash(f"salt-api error: {exc}", "error")
        return redirect(url_for("reactor.index"))
    log_event(current_user.username, f"reactor-delete:{event}")
    flash(f"{event}: reactor deleted.", "success")
    return redirect(url_for("reactor.index"))


@bp.route("/export")
@login_required
def export():
    """Render the live mapping as a YAML block for committing to git."""
    entries: dict[str, list[str]] = {}
    error = None
    try:
        value = get_salt().runner("reactor.list", http_timeout=30.0)
        value = _unwrap(value)
        entries, _ = parse_reactor_list(value)
    except SaltApiError as exc:
        error = f"salt-api error: {exc}"
    body = render_export(entries) if not error else ""
    if request.args.get("download") == "1" and not error:
        return Response(
            body,
            mimetype="text/plain",
            headers={"Content-Disposition": "attachment; filename=reactor.conf"},
        )
    return render_template(
        "reactor_export.html", body=body, error=error, total=len(entries)
    )
