"""Read-only file-roots browser. States live in git; deployment syncs the
checkout; the app only reads. There is intentionally no write path here."""

import subprocess
from pathlib import Path

from flask import Blueprint, abort, current_app, render_template, request
from flask_login import login_required

bp = Blueprint("files", __name__, url_prefix="/files")

MAX_BYTES = 256 * 1024


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


def list_tree() -> list[dict]:
    base = roots()
    entries = []
    if not base.is_dir():
        return entries
    for path in sorted(base.rglob("*")):
        if path.is_dir():
            continue
        entries.append({"rel": str(path.relative_to(base)),
                        "size": path.stat().st_size})
    return entries


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
                    ["git", "rev-parse", "--short", "HEAD"], cwd=path,
                    capture_output=True, text=True, timeout=5)
            except (OSError, subprocess.SubprocessError):
                return None
            return out.stdout.strip() or None if out.returncode == 0 else None
        if path.parent == path:
            return None
        path = path.parent


@bp.route("/")
@login_required
def index():
    return render_template("files.html", entries=list_tree(),
                           revision=sync_revision())


@bp.route("/view")
@login_required
def view():
    rel = request.args.get("path", "")
    target = safe_join(rel)
    if not target or not target.is_file():
        abort(404)
    content = read_text(target)
    if content is None:
        abort(404)
    return render_template("file_view.html", rel=rel, content=content,
                           revision=sync_revision())
