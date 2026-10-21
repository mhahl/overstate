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
    git_commit_file,
    git_fetch_now,
    git_head,
    git_push_now,
    git_status,
    git_sync_now,
    is_checkout,
    paths_changed,
)
from .salt_client import SaltApiError

bp = Blueprint("files", __name__, url_prefix="/files")

MAX_BYTES = 256 * 1024
LIST_LIMIT = 5000
# Pagination sizes live in minions.py (single source of truth);
# files.py cannot import it at module level (minions imports files).
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


def browse_root() -> Path:
    """Root the Files browser walks: the whole cloned repo when a checkout
    exists (git toplevel), else the configured file roots.

    ``roots()`` stays the salt tree for git/formulas internals; this is
    the *viewing* root, so the browser shows everything the checkout
    holds — ``salt/``, ``pillar/``, ``reactor/`` and any sibling files.
    """
    from .git_sync import _toplevel

    top = _toplevel()
    return top if top is not None else roots()


def safe_join(rel: str, base: Path | None = None) -> Path | None:
    """Resolve ``rel`` inside ``base`` (or roots); None on traversal."""
    base = base or roots()
    try:
        target = (base / rel).resolve()
    except (OSError, ValueError):
        return None
    if target != base and base not in target.parents:
        return None
    return target


def list_tree(limit: int | None = None, base: Path | None = None) -> list[dict]:
    """Flat file listing under ``base`` (defaults to file roots), sorted
    by path.

    ``limit`` bounds the walk itself: collection stops after
    ``limit`` entries so a huge checkout cannot balloon one request.
    ``None`` walks everything (kept for small-tree callers/tests).
    """
    base = base or roots()
    entries = []
    if not base.is_dir():
        return entries
    for path, rel in _iter_files(base):
        try:
            st = path.stat()
        except OSError:
            continue
        entries.append({"rel": rel, "size": st.st_size, "mtime": st.st_mtime})
        if limit is not None and len(entries) >= limit:
            break
    entries.sort(key=lambda e: e["rel"])
    return entries


def _iter_files(base: Path):
    """File paths under roots, skipping git internals.

    ``.git/`` would otherwise flood the browser with thousands of
    object rows that can never be viewed or edited meaningfully.
    """
    for path in base.rglob("*"):
        if path.is_dir():
            continue
        try:
            rel = path.relative_to(base)
        except ValueError:
            continue
        if ".git" in rel.parts:
            continue
        yield path, str(rel)


def normalize_dir(value: str) -> str:
    """Clean a ``?dir=`` navigation prefix. ``""`` means the whole tree.

    Anything that escapes roots (``..``, backslashes) collapses to
    ``""`` instead of erroring — a mistyped bookmark still shows files.
    """
    parts = [p for p in (value or "").split("/") if p not in ("", ".")]
    if not parts or any(p == ".." or "\\" in p for p in parts):
        return ""
    return "/".join(parts)


def dir_crumbs(d: str, root_label: str = "Files") -> list[dict]:
    """Breadcrumb segments for a dir prefix: root label plus each level."""
    crumbs = [{"label": root_label, "dir": ""}]
    prefix = []
    for seg in d.split("/"):
        if not seg:
            continue
        prefix.append(seg)
        crumbs.append({"label": seg, "dir": "/".join(prefix)})
    return crumbs


def build_tree(entries: list[dict], active_dir: str = "") -> list[dict]:
    """Nested Wunderbaum source: folders only, from flat entries.

    Each node is a folder (``key`` = dir path); files stay in the list
    pane so search and pagination keep their contracts — the tree never
    shows a name the listing filtered out. Children sort
    alphabetically; nodes on the ``active_dir`` path come back
    ``expanded`` with the dir itself ``active``. Pure data — no
    ``url_for`` so unit tests need no request context.
    """
    root: dict = {}
    for entry in entries:
        node = root
        for seg in entry["rel"].split("/")[:-1]:
            node = node.setdefault(seg, {})
    active_parts = active_dir.split("/") if active_dir else []

    def build(node: dict, prefix: list[str], depth: int) -> list[dict]:
        kids = []
        for name in sorted(node):
            path = prefix + [name]
            dirpath = "/".join(path)
            on_path = active_parts[: len(path)] == path
            kids.append(
                {
                    "key": dirpath,
                    "title": name,
                    "folder": True,
                    "expanded": on_path and depth < len(active_parts),
                    "active": dirpath == active_dir,
                    "children": build(node[name], path, depth + 1),
                }
            )
        kids.sort(key=lambda k: k["title"].lower())
        return kids

    return build(root, [], 0)


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
            "Warning: that YAML does not parse. Saved anyway. "
            "Salt parses it at apply time."
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


def last_commit(rel: str) -> dict | None:
    """Provenance of the last commit touching ``rel`` (short sha, author,
    relative date, subject), or None when it is not a tracked checkout
    file. Read-only; used by the file view page."""
    top = browse_root()
    if not (top / ".git").is_dir():
        return None
    try:
        out = subprocess.run(
            ["git", "log", "-1", "--format=%h%x1f%an%x1f%ar%x1f%s", "--", rel],
            cwd=top,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0 or not out.stdout.strip():
        return None
    sha, author, when, subject = out.stdout.strip().split("\x1f", 3)
    return {"sha": sha, "author": author, "when": when, "subject": subject}


@bp.route("/")
@login_required
def index():
    from .authz import require

    require("file.read")
    # Legacy deep links from before the Files split: tab=formulas moved to
    # its own page, tab=repo moved to Settings. Redirect once, keep the
    # bookmarks working.
    tab = request.args.get("tab", "files")
    if tab == "formulas":
        return redirect(url_for("formulas.index"))
    if tab == "repo":
        return redirect(url_for("state_repo.index"))
    return render_template("files.html", **_files_page_context())


def _files_page_context():
    from flask_login import current_user

    from .authz import prefix_visible, rbac_mode, visible_prefixes

    prefixes = (
        visible_prefixes(current_user) if rbac_mode() == "scoped" else None
    )
    q = request.args.get("q", "").strip().lower()
    sort = request.args.get("sort", "name")
    if sort not in ("name", "size", "mtime"):
        sort = "name"
    # ``sd`` (sort direction) avoids clashing with ``dir`` (folder path).
    direction = request.args.get("sd", "asc")
    if direction not in ("asc", "desc"):
        direction = "asc"
    from .minions import resolve_page_size

    try:
        page = max(1, int(request.args.get("page", 1)))
    except ValueError:
        page = 1
    per_page = resolve_page_size(request.args.get("per_page"))
    d = normalize_dir(request.args.get("dir", ""))
    base = browse_root()
    full = list_tree(limit=LIST_LIMIT + 1, base=base)
    if prefixes is not None:
        # Listing drops entries outside every granted prefix.
        full = [e for e in full if prefix_visible(prefixes, e["rel"])]
    tree_nodes = build_tree(full, active_dir=d)
    if d:
        # Walk only the requested subtree so the listing cap applies to
        # this folder, not the whole checkout — a deep folder that sorts
        # past the cap must not appear empty.
        sub = base / d
        if sub.is_dir():
            scoped = list_tree(limit=LIST_LIMIT + 1, base=sub)
            entries = [
                {"rel": f"{d}/{e['rel']}", "size": e["size"], "mtime": e["mtime"]}
                for e in scoped
            ]
        else:
            entries = []
        if prefixes is not None:
            entries = [e for e in entries if prefix_visible(prefixes, e["rel"])]
    else:
        entries = full
    truncated = len(entries) > LIST_LIMIT
    entries = entries[:LIST_LIMIT]
    if q:
        entries = [e for e in entries if q in e["rel"].lower()]
    if sort == "size":
        entries.sort(key=lambda e: e["size"], reverse=(direction == "desc"))
    elif sort == "mtime":
        entries.sort(key=lambda e: e["mtime"], reverse=(direction == "desc"))
    else:
        entries.sort(key=lambda e: e["rel"].lower(), reverse=(direction == "desc"))
    total = len(entries)
    pages = max(1, (total + per_page - 1) // per_page)
    page = min(page, pages)
    rows = group_rows(entries[(page - 1) * per_page : page * per_page])
    # The state top.sls lives under salt/ in the srv layout, or at the
    # browsed root when the checkout is at file roots (legacy layout).
    has_top = (browse_root() / "salt" / "top.sls").is_file() or (
        roots() / "top.sls"
    ).is_file()
    from .file_sync_schedule import (
        FILE_SYNC_CRON_DEFAULT,
        LAST_MESSAGE_SETTING,
        LAST_RUN_SETTING,
        LAST_STATUS_SETTING,
    )
    from .settings import get_setting

    file_sync = {
        "schedule": get_setting("file_sync_cron") or FILE_SYNC_CRON_DEFAULT,
        "last_run": get_setting(LAST_RUN_SETTING),
        "last_status": get_setting(LAST_STATUS_SETTING),
        "last_message": get_setting(LAST_MESSAGE_SETTING),
    }
    return {
        "rows": rows,
        "revision": sync_revision(),
        "q": request.args.get("q", ""),
        "d": d,
        "sort": sort,
        "direction": direction,
        "crumbs": dir_crumbs(d),
        "tree_nodes": tree_nodes,
        "page": page,
        "pages": pages,
        "per_page": per_page,
        "total": total,
        "truncated": truncated,
        "has_top": has_top,
        "git": git_status(),
        "file_sync": file_sync,
    }


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
    from .authz import rbac_mode, require

    if rbac_mode() == "scoped":
        require("file.sync")
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
                    f"Synced {result['old']} → {result['new']}. "
                    f"Master refresh failed ({err}); applies may lag.",
                    "warning",
                )
                log_event(current_user.username, f"fileserver-update-failed:{err}")
            if paths_changed(result["old"], result["new"], "_modules"):
                flash("Custom modules changed. Run Sync modules.", "info")
        else:
            flash(f"Already up to date at {result['new']}.", "info")
    else:
        flash(f"Sync refused: {result['reason']}. Nothing changed.", "error")
        log_event(current_user.username, f"git-sync-refused:{result['reason']}")
    return redirect(url_for("files.index"))


@bp.post("/sync-now")
@roles_required("admin")
def sync_now():
    """Run the scheduled file sync immediately, bypassing the cron gate.

    Shares the scheduled path's sync + fileserver refresh + status
    recording, so the Files page's schedule/status line reflects it.
    """
    from .file_sync_schedule import run_manual_sync

    result = run_manual_sync()
    if result.get("ok"):
        flash(result["message"], "success" if result.get("status") != "warning" else "warning")
        log_event(current_user.username, "file-sync:manual")
    else:
        flash(result["message"], "error")
    return redirect(url_for("files.index"))


@bp.post("/sync-modules")
@roles_required("operator")
def sync_modules():
    from .authz import rbac_mode, require

    if rbac_mode() == "scoped":
        require("file.sync")
    """Publish custom modules fleet-wide: ``saltutil.sync_all`` on the
    master. Operator+. Salt stays the arbiter — this only distributes
    files, it applies nothing."""
    try:
        get_salt().runner("saltutil.sync_all", http_timeout=120.0)
    except (SaltApiError, httpx.HTTPError, KeyError) as exc:
        flash(f"Module sync failed (salt-api error: {exc}). Nothing sent.", "error")
        log_event(current_user.username, "modules-sync-failed")
        return redirect(url_for("files.index"))
    flash("Custom modules synced to the master.", "success")
    log_event(current_user.username, "modules-sync")
    return redirect(url_for("files.index"))


@bp.post("/fetch")
@roles_required("operator")
def fetch():
    from .authz import rbac_mode, require

    if rbac_mode() == "scoped":
        require("file.sync")
    """Check the remote for updates without touching the working tree.

    Refreshes behind/ahead counts so the status card answers against
    the live remote. Files never change here; only Sync pulls.
    """
    result = git_fetch_now()
    if result["ok"]:
        behind = git_status().get("behind")
        if behind:
            flash(
                f"Fetched: {behind} behind. Sync to pull.",
                "info",
            )
        else:
            flash("Already up to date.", "info")
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
    target = safe_join(rel, base=browse_root())
    if not target or not target.is_file():
        abort(404)
    from .authz import rbac_mode, require

    if rbac_mode() == "scoped":
        require("file.write", prefix=rel)
    content = read_text(target)
    if content is None:
        flash("That file cannot be edited here (too large or not text).", "error")
        return redirect(url_for("files.view", path=rel))
    try:
        raw = target.read_bytes()
    except OSError:
        abort(404)
    lang = "yaml" if target.suffix.lower() in YAML_SUFFIXES else "text"
    parent = rel.rpartition("/")[0] if "/" in rel else ""
    return render_template(
        "file_edit.html",
        rel=rel,
        crumbs=dir_crumbs(parent),
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
    target = safe_join(rel, base=browse_root())
    if not target or not target.is_file():
        abort(404)
    from .authz import rbac_mode, require

    if rbac_mode() == "scoped":
        require("file.write", prefix=rel)
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
        flash("No changes. Nothing committed.", "info")
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
            f"That file changed (now {short}). Reload and "
            "re-apply your edits. Nothing was written.",
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
    rel_posix = target.relative_to(browse_root()).as_posix()
    result = git_commit_file(
        rel_posix,
        f"overstate({current_user.username}): {rel_posix}",
        current_user.username,
        cwd=browse_root(),
    )
    if not result["ok"]:
        flash(
            f"Saved on disk but not committed ({result['reason']}). "
            "Fix it in git.",
            "error",
        )
        log_event(
            current_user.username, f"file-save-uncommitted:{rel}:{result['reason']}"
        )
        return redirect(url_for("files.view", path=rel))
    err = _refresh_fileserver()
    if err is None:
        flash(f"Saved {rel} as {result['new']}.", "success")
    else:
        flash(
            f"Saved {rel} as {result['new']}. "
            f"Master refresh failed ({err}); applies may lag.",
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
    from .authz import rbac_mode, require

    if rbac_mode() == "scoped":
        require("file.git")
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


@bp.route("/view")
@login_required
def view():
    from .authz import rbac_mode, require

    rel = request.args.get("path", "")
    target = safe_join(rel, base=browse_root())
    if not target or not target.is_file():
        abort(404)
    if rbac_mode() == "scoped":
        require("file.read", prefix=rel)
    parent = rel.rpartition("/")[0] if "/" in rel else ""
    crumbs = dir_crumbs(parent) + [{"label": rel.rsplit("/", 1)[-1], "dir": None}]
    content = read_text(target)
    if content is not None:
        highlighted = (
            highlight_yaml(content) if target.suffix.lower() in YAML_SUFFIXES else None
        )
        return render_template(
            "file_view.html",
            rel=rel,
            crumbs=crumbs,
            parent=parent,
            content=content,
            lines=content.splitlines(),
            highlighted=highlighted,
            reason=None,
            size=None,
            revision=sync_revision(),
            last_commit=last_commit(rel),
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
        crumbs=crumbs,
        parent=parent,
        content=None,
        lines=[],
        reason=reason,
        size=size,
        max_bytes=MAX_BYTES,
        revision=sync_revision(),
        last_commit=last_commit(rel),
    )
