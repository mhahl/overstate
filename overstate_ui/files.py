"""Read-only file-roots browser. States live in git; deployment syncs the
checkout; the app only reads. There is intentionally no write path here."""

import subprocess
from pathlib import Path

from flask import (
    Blueprint,
    abort,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)
from flask_login import current_user, login_required
from pygments import highlight
from pygments.formatters import HtmlFormatter
from pygments.lexers import YamlLexer

from .audit import log_event
from .auth import roles_required
from .git_sync import git_status, git_sync_now

bp = Blueprint("files", __name__, url_prefix="/files")

MAX_BYTES = 256 * 1024
LIST_LIMIT = 5000
PAGE_SIZES = (25, 50, 100)
DEFAULT_PAGE_SIZE = 50
YAML_SUFFIXES = (".sls", ".yaml", ".yml")


def highlight_yaml(content: str) -> str:
    """Syntax-highlight YAML/SLS source as an HTML fragment.

    The fragment carries its own line numbers (Pygments ``table``
    line numbers) and is theme-aware via the ``codehl`` stylesheet.
    Content is shown verbatim — never reformatted, so comments and
    ordering survive.
    """
    return highlight(
        content, YamlLexer(), HtmlFormatter(linenos="table", cssclass="codehl")
    )


def roots() -> Path:
    return Path(current_app.config["FILE_ROOTS"]).resolve()


def safe_join(rel: str) -> Path | None:
    """Resolve ``rel`` inside roots; return None on traversal/absolute paths."""
    base = roots()
    try:
        target = (base / rel).resolve()
    except (OSError, ValueError):
        return None
    if target != base and base not in target.parents:
        return None
    return target


def list_tree(limit: int | None = None) -> list[dict]:
    """Flat file listing under roots, sorted by path.

    ``limit`` bounds the walk itself: collection stops after
    ``limit`` entries so a huge checkout cannot balloon one request.
    ``None`` walks everything (kept for small-tree callers/tests).
    """
    base = roots()
    entries = []
    if not base.is_dir():
        return entries
    for path in base.rglob("*"):
        if path.is_dir():
            continue
        try:
            size = path.stat().st_size
        except OSError:
            continue
        entries.append({"rel": str(path.relative_to(base)), "size": size})
        if limit is not None and len(entries) >= limit:
            break
    entries.sort(key=lambda e: e["rel"])
    return entries


def group_rows(entries: list[dict]) -> list[dict]:
    """Interleave directory header rows for one page of entries.

    Headers carry ``{"group": name}``; file rows pass through. The
    top level (files at the root) groups under ``/``.
    """
    rows: list[dict] = []
    seen: set[str] = set()
    for entry in entries:
        rel = entry["rel"]
        top = rel.split("/", 1)[0] if "/" in rel else "/"
        if top not in seen:
            seen.add(top)
            rows.append({"group": top})
        rows.append(entry)
    return rows


def read_text(target: Path) -> str | None:
    try:
        data = target.read_bytes()
    except OSError:
        return None
    if len(data) > MAX_BYTES:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def sync_revision() -> str | None:
    """Short git SHA of the checkout backing roots, if it is one."""
    path = roots()
    while True:
        if (path / ".git").exists():
            try:
                out = subprocess.run(
                    ["git", "rev-parse", "--short", "HEAD"],
                    cwd=path,
                    capture_output=True,
                    text=True,
                    timeout=5,
                    check=False,
                )
            except (OSError, subprocess.SubprocessError):
                return None
            return out.stdout.strip() or None if out.returncode == 0 else None
        if path.parent == path:
            return None
        path = path.parent


@bp.route("/")
@login_required
def index():
    q = request.args.get("q", "").strip().lower()
    try:
        page = max(1, int(request.args.get("page", 1)))
    except ValueError:
        page = 1
    try:
        per_page = int(request.args.get("per_page", DEFAULT_PAGE_SIZE))
    except ValueError:
        per_page = DEFAULT_PAGE_SIZE
    if per_page not in PAGE_SIZES:
        per_page = DEFAULT_PAGE_SIZE
    entries = list_tree(limit=LIST_LIMIT + 1)
    truncated = len(entries) > LIST_LIMIT
    entries = entries[:LIST_LIMIT]
    if q:
        entries = [e for e in entries if q in e["rel"].lower()]
    total = len(entries)
    pages = max(1, (total + per_page - 1) // per_page)
    page = min(page, pages)
    rows = group_rows(entries[(page - 1) * per_page : page * per_page])
    has_top = (roots() / "top.sls").is_file()
    return render_template(
        "files.html",
        rows=rows,
        revision=sync_revision(),
        q=request.args.get("q", ""),
        page=page,
        pages=pages,
        per_page=per_page,
        total=total,
        truncated=truncated,
        has_top=has_top,
        git=git_status(),
    )


@bp.post("/sync")
@roles_required("operator")
def sync():
    """Pull --ff-only on the file-roots checkout. Refusals explain."""
    result = git_sync_now()
    if result["ok"]:
        if result["changed"]:
            flash(f"Synced {result['old']} → {result['new']}.", "success")
        else:
            flash(f"Already up to date at {result['new']}.", "info")
        log_event(current_user.username, f"git-sync:{result['new']}")
    else:
        flash(f"Sync refused: {result['reason']}. Nothing changed.", "error")
        log_event(current_user.username, f"git-sync-refused:{result['reason']}")
    return redirect(url_for("files.index"))


@bp.route("/view")
@login_required
def view():
    rel = request.args.get("path", "")
    target = safe_join(rel)
    if not target or not target.is_file():
        abort(404)
    content = read_text(target)
    if content is not None:
        highlighted = (
            highlight_yaml(content) if target.suffix.lower() in YAML_SUFFIXES else None
        )
        return render_template(
            "file_view.html",
            rel=rel,
            content=content,
            lines=content.splitlines(),
            highlighted=highlighted,
            reason=None,
            size=None,
            revision=sync_revision(),
        )
    # The file exists but cannot be shown as text: explain instead of
    # a bare 404 so operators know whether it is big or binary.
    try:
        size = target.stat().st_size
    except OSError:
        abort(404)
    reason = "too large to display" if size > MAX_BYTES else "not readable text"
    return render_template(
        "file_view.html",
        rel=rel,
        content=None,
        lines=[],
        reason=reason,
        size=size,
        max_bytes=MAX_BYTES,
        revision=sync_revision(),
    )
