"""File-roots browser. States live in git; operators edit text files through
the edit page (one local commit per save, admin push is separate); the
read-only listing and view pages stay viewer-visible."""

import hashlib
import json
import os
import subprocess
from pathlib import Path

import httpx
import yaml
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
from pygments.lexers import JsonLexer, YamlLexer

from .audit import log_event
from .auth import roles_required
from .dashboard import get_salt
from .git_sync import (
    clear_token,
    clone_repo,
    git_commit_file,
    git_fetch_now,
    git_head,
    git_origin,
    git_push_now,
    git_status,
    git_sync_now,
    has_token,
    is_checkout,
    reclone_repo,
    reset_hard,
    reset_preview,
    save_token,
    set_remote_origin,
    validate_repo_url,
)
from .salt_client import SaltApiError

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


def highlight_json(data) -> str:
    """Syntax-highlight a JSON-serializable payload as an HTML fragment.

    Shares the ``codehl`` stylesheet and line numbers with
    :func:`highlight_yaml`; used for structured Salt data (pillar)
    where YAML source is unavailable. Keys are sorted and exotic
    values stringified so the view is stable and never crashes.
    """
    return highlight(
        json.dumps(data, indent=1, sort_keys=True, default=str),
        JsonLexer(),
        HtmlFormatter(linenos="table", cssclass="codehl"),
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


def yaml_advisory(target: Path, data: bytes) -> str | None:
    """Advisory-only YAML parse check for SLS files. Never blocks a save.

    Returns a warning message when the content does not parse, else None.
    Salt stays the arbiter of validity at apply time.
    """
    if target.suffix.lower() not in YAML_SUFFIXES:
        return None
    try:
        yaml.safe_load(data.decode("utf-8"))
    except Exception:  # noqa: BLE001 — any parse failure is advisory
        return (
            "Warning: that YAML does not parse — saved anyway. "
            "Salt is truth at apply time."
        )
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


def _refresh_fileserver() -> str | None:
    """Tell the master to re-read file roots after a changed pull.

    Returns None when the master confirms, otherwise the reason. A
    refresh failure never fails the sync itself — the pull already
    landed, applies just lag until the master updates on its own.
    """
    try:
        get_salt().runner("fileserver.update", http_timeout=30.0)
    except (SaltApiError, httpx.HTTPError, KeyError) as exc:
        return f"salt-api error: {exc}"
    return None


@bp.post("/sync")
@roles_required("operator")
def sync():
    """Pull --ff-only; on a changed pull, refresh the master fileserver."""
    result = git_sync_now()
    if result["ok"]:
        log_event(current_user.username, f"git-sync:{result['new']}")
        if result["changed"]:
            err = _refresh_fileserver()
            if err is None:
                flash(
                    f"Synced {result['old']} → {result['new']}; "
                    "master fileserver refreshed.",
                    "success",
                )
                log_event(current_user.username, "fileserver-update")
            else:
                flash(
                    f"Synced {result['old']} → {result['new']}, but the "
                    f"master refresh failed ({err}) — applies may lag "
                    "until the master updates.",
                    "warning",
                )
                log_event(current_user.username, f"fileserver-update-failed:{err}")
        else:
            flash(f"Already up to date at {result['new']}.", "info")
    else:
        flash(f"Sync refused: {result['reason']}. Nothing changed.", "error")
        log_event(current_user.username, f"git-sync-refused:{result['reason']}")
    return redirect(url_for("files.index"))


@bp.post("/fetch")
@roles_required("operator")
def fetch():
    """Check the remote for updates without touching the working tree.

    Refreshes behind/ahead counts so the status card answers against
    the live remote. Files never change here; only Sync pulls.
    """
    result = git_fetch_now()
    if result["ok"]:
        behind = git_status().get("behind")
        if behind:
            flash(
                f"Fetched: {behind} commit(s) behind — Sync to pull.",
                "info",
            )
        else:
            flash("Fetched: up to date with the remote.", "info")
        log_event(current_user.username, "git-fetch")
    else:
        flash(f"Check failed: {result['reason']}. Nothing changed.", "error")
        log_event(current_user.username, f"git-fetch-refused:{result['reason']}")
    return redirect(url_for("files.index"))


@bp.route("/edit")
@roles_required("operator")
def edit():
    """Edit form for one text file: CodeMirror bundle over a textarea.

    The textarea is the real form field, so saving works when the bundle
    is missing or JS is off. ``base_sha``/``base_hash`` record what the
    form was read at; the save route checks them (stale bases refuse).
    """
    rel = request.args.get("path", "")
    target = safe_join(rel)
    if not target or not target.is_file():
        abort(404)
    content = read_text(target)
    if content is None:
        flash("That file cannot be edited here (too large or not text).", "error")
        return redirect(url_for("files.view", path=rel))
    try:
        raw = target.read_bytes()
    except OSError:
        abort(404)
    lang = "yaml" if target.suffix.lower() in YAML_SUFFIXES else "text"
    return render_template(
        "file_edit.html",
        rel=rel,
        content=content,
        lang=lang,
        base_sha=git_head() or "nogit",
        base_hash=hashlib.sha256(raw).hexdigest(),
        revision=sync_revision(),
    )


@bp.post("/save")
@roles_required("operator")
def save():
    """Write one text file and commit it locally (that file only).

    Identical content commits nothing; binary/oversize results and
    non-checkouts refuse with the file untouched. A successful commit
    triggers the same advisory fileserver refresh as a sync.
    """
    rel = request.form.get("path", "")
    target = safe_join(rel)
    if not target or not target.is_file():
        abort(404)
    try:
        current = target.read_bytes()
    except OSError:
        abort(404)
    try:
        current.decode("utf-8")
        current_editable = len(current) <= MAX_BYTES
    except UnicodeDecodeError:
        current_editable = False
    if not current_editable:
        flash("That file is no longer editable text. Nothing changed.", "error")
        log_event(current_user.username, f"file-save-refused:{rel}:uneditable")
        return redirect(url_for("files.view", path=rel))
    data = request.form.get("content", "").encode("utf-8")
    if len(data) > MAX_BYTES:
        flash(
            f"Too large to save ({len(data)} bytes; limit is {MAX_BYTES}). "
            "Nothing changed.",
            "error",
        )
        log_event(current_user.username, f"file-save-refused:{rel}:oversize")
        return redirect(url_for("files.view", path=rel))
    if data == current:
        flash("No changes — nothing committed.", "info")
        return redirect(url_for("files.view", path=rel))
    if not is_checkout():
        flash("Not a git checkout: files are read-only here. Nothing changed.", "error")
        log_event(current_user.username, f"file-save-refused:{rel}:not-a-checkout")
        return redirect(url_for("files.view", path=rel))
    head = git_head()
    base_ok = (head is not None and request.form.get("base_sha") == head) or (
        head is None and request.form.get("base_sha") == "nogit"
    )
    if (
        not base_ok
        or request.form.get("base_hash") != hashlib.sha256(current).hexdigest()
    ):
        short = head[:7] if head else "unknown"
        flash(
            f"That file changed underneath you (now at {short}). Reload the "
            "edit page and re-apply your change. Nothing was written.",
            "error",
        )
        log_event(current_user.username, f"file-save-refused:{rel}:stale")
        return redirect(url_for("files.edit", path=rel))
    try:
        tmp = target.with_name(target.name + ".overstate-tmp")
        tmp.write_bytes(data)
        os.replace(tmp, target)
    except OSError:
        flash("Could not write the file. Nothing changed.", "error")
        log_event(current_user.username, f"file-save-refused:{rel}:write-failed")
        return redirect(url_for("files.view", path=rel))
    rel_posix = target.relative_to(roots()).as_posix()
    result = git_commit_file(
        rel_posix,
        f"overstate({current_user.username}): {rel_posix}",
        current_user.username,
    )
    if not result["ok"]:
        flash(
            f"Saved on disk but not committed ({result['reason']}). "
            "Resolve it in git; the file itself is updated.",
            "error",
        )
        log_event(
            current_user.username, f"file-save-uncommitted:{rel}:{result['reason']}"
        )
        return redirect(url_for("files.view", path=rel))
    err = _refresh_fileserver()
    if err is None:
        flash(f"Saved {rel} — committed as {result['new']}.", "success")
    else:
        flash(
            f"Saved {rel} — committed as {result['new']}, but the master "
            f"refresh failed ({err}); applies may lag until the master updates.",
            "warning",
        )
    advisory = yaml_advisory(target, data)
    if advisory is not None:
        flash(advisory, "warning")
    log_event(current_user.username, f"file-save:{rel}:{result['new']}")
    return redirect(url_for("files.view", path=rel))


@bp.post("/push")
@roles_required("admin")
def push():
    """Push local commits upstream. Admin-only; refusals change nothing."""
    result = git_push_now()
    if result["ok"]:
        if result["sent"]:
            flash(f"Pushed {result['new']} upstream.", "success")
        else:
            flash(f"Already up to date at {result['new']}.", "info")
        log_event(current_user.username, f"git-push:{result['new']}")
    else:
        flash(f"Push refused: {result['reason']}. Nothing changed.", "error")
        log_event(current_user.username, f"git-push-refused:{result['reason']}")
    return redirect(url_for("files.index"))


def _repo_context(preview=None, pending=None):
    checkout = is_checkout()
    return {
        "checkout": checkout,
        "status": git_status() if checkout else None,
        "origin": git_origin() if checkout else None,
        "token_configured": has_token(),
        "preview": preview,
        "pending": pending,
    }


def _note_refreshed(verb, sha):
    """Fileserver refresh after a tree-changing repo op, sync-style."""
    err = _refresh_fileserver()
    if err is None:
        flash(f"{verb} at {sha}; master fileserver refreshed.", "success")
        log_event(current_user.username, "fileserver-update")
    else:
        flash(
            f"{verb} at {sha}, but the master refresh failed ({err}) — "
            "applies may lag until the master updates.",
            "warning",
        )
        log_event(current_user.username, f"fileserver-update-failed:{err}")


@bp.get("/repo")
@roles_required("admin")
def repo():
    """Repo tab: bootstrap, repoint, and repair the file-roots checkout."""
    return render_template("repo.html", **_repo_context())


@bp.post("/repo/clone")
@roles_required("admin")
def repo_clone():
    """Clone the canonical repo into an empty file roots. Admin-only."""
    url = request.form.get("url", "")
    branch = request.form.get("branch", "")
    token = request.form.get("token", "") or None
    result = clone_repo(url, branch, token)
    if result["ok"]:
        log_event(current_user.username, "git-clone")
        _note_refreshed("Cloned", git_head())
    else:
        flash(f"Clone refused: {result['reason']}. Nothing changed.", "error")
        log_event(current_user.username, f"git-clone-refused:{result['reason']}")
    return redirect(url_for("files.repo"))


@bp.post("/repo/set-remote")
@roles_required("admin")
def repo_set_remote():
    """Repoint origin at a moved canonical repo. Admin-only."""
    result = set_remote_origin(request.form.get("url", ""))
    if result["ok"]:
        log_event(current_user.username, "git-set-remote")
        flash("Origin repointed.", "success")
    else:
        flash(f"Repoint refused: {result['reason']}. Nothing changed.", "error")
        log_event(current_user.username, f"git-set-remote-refused:{result['reason']}")
    return redirect(url_for("files.repo"))


@bp.post("/repo/reset")
@roles_required("admin")
def repo_reset():
    """Two-step reset to the tracked upstream: preview, then confirm."""
    if request.form.get("confirm") != "1":
        preview = reset_preview()
        if not preview["ok"]:
            flash(
                f"Reset refused: {preview['reason']}. Nothing changed.",
                "error",
            )
            return redirect(url_for("files.repo"))
        return render_template("repo.html", **_repo_context(preview=preview))
    clean = request.form.get("clean") == "1"
    result = reset_hard(clean_untracked=clean)
    if result["ok"]:
        log_event(current_user.username, "git-reset")
        _note_refreshed("Reset", result["new"])
    else:
        flash(f"Reset refused: {result['reason']}. Nothing changed.", "error")
        log_event(current_user.username, f"git-reset-refused:{result['reason']}")
    return redirect(url_for("files.repo"))


@bp.post("/repo/reclone")
@roles_required("admin")
def repo_reclone():
    """Two-step full re-clone: preview the destruction, then confirm."""
    url = request.form.get("url", "") or (git_origin() or "")
    branch = request.form.get("branch", "")
    token = request.form.get("token", "") or None
    if request.form.get("confirm") != "1":
        if not validate_repo_url(url)["ok"]:
            flash("Re-clone refused: enter a remote URL first.", "error")
            return redirect(url_for("files.repo"))
        return render_template(
            "repo.html",
            **_repo_context(pending={"url": url, "branch": branch}),
        )
    result = reclone_repo(url, branch, token)
    if result["ok"]:
        log_event(current_user.username, "git-reclone")
        _note_refreshed("Re-cloned", git_head())
    else:
        flash(f"Re-clone refused: {result['reason']}. Nothing changed.", "error")
        log_event(current_user.username, f"git-reclone-refused:{result['reason']}")
    return redirect(url_for("files.repo"))


@bp.post("/repo/token-save")
@roles_required("admin")
def repo_token_save():
    """Store an https token for the current origin's host (0600)."""
    origin = git_origin()
    if not origin or not origin.startswith("https://"):
        flash(
            "Save refused: clone or set an https remote first — "
            "tokens are stored per host.",
            "error",
        )
        return redirect(url_for("files.repo"))
    host = origin.split("/", 3)[2]
    result = save_token(request.form.get("token", ""), host)
    if result["ok"]:
        log_event(current_user.username, "git-token-saved")
        flash(f"Token stored for {host}.", "success")
    else:
        flash(f"Save refused: {result['reason']}. Nothing changed.", "error")
    return redirect(url_for("files.repo"))


@bp.post("/repo/token-clear")
@roles_required("admin")
def repo_token_clear():
    """Forget the stored token. Future remote calls prompt nothing —
    they fail with missing credentials instead."""
    result = clear_token()
    if result["ok"]:
        log_event(current_user.username, "git-token-cleared")
        flash("Stored token cleared.", "info")
    else:
        flash(f"Clear refused: {result['reason']}.", "error")
    return redirect(url_for("files.repo"))


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
