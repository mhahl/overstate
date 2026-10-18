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
    checkout_layout,
    clear_token,
    clone_repo,
    formula_in_top,
    git_commit_file,
    git_fetch_now,
    git_head,
    git_origin,
    git_push_now,
    git_status,
    git_sync_now,
    has_token,
    is_checkout,
    list_formula_dirs,
    paths_changed,
    reclone_repo,
    remove_formula_files,
    reset_hard,
    reset_preview,
    roots_nonempty,
    save_token,
    set_remote_origin,
    srv_nonempty,
    update_formula,
    validate_formula_name,
    validate_repo_url,
    vendor_formula,
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
    for path, rel in _iter_files(base):
        try:
            size = path.stat().st_size
        except OSError:
            continue
        entries.append({"rel": rel, "size": size})
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


def dir_crumbs(d: str) -> list[dict]:
    """Breadcrumb segments for a dir prefix: Files plus each level."""
    crumbs = [{"label": "Files", "dir": ""}]
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


@bp.route("/")
@login_required
def index():
    from flask import abort
    from flask_login import current_user

    from .authz import rbac_mode, require

    require("file.read")
    ftab = request.args.get("tab", "files")
    if ftab not in ("files", "updates", "repo", "formulas"):
        ftab = "files"
    if ftab == "repo":
        if rbac_mode() == "scoped":
            require("file.git")
        elif current_user.role != "admin":
            abort(403)
    context = _files_page_context()
    if ftab == "repo":
        context.update(_repo_context())
    # Formulas context rides every tab: the rail badges report tracked /
    # untracked / missing counts wherever the user stands.
    context.update(_formulas_context())
    fsub = request.args.get("fsub", "tracked")
    if fsub not in ("tracked", "untracked"):
        fsub = "tracked"
    return render_template("files.html", **context, ftab=ftab, fsub=fsub)


def _files_page_context():
    from flask_login import current_user

    from .authz import prefix_visible, rbac_mode, visible_prefixes

    prefixes = (
        visible_prefixes(current_user) if rbac_mode() == "scoped" else None
    )
    q = request.args.get("q", "").strip().lower()
    from .minions import resolve_page_size

    try:
        page = max(1, int(request.args.get("page", 1)))
    except ValueError:
        page = 1
    per_page = resolve_page_size(request.args.get("per_page"))
    d = normalize_dir(request.args.get("dir", ""))
    entries = list_tree(limit=LIST_LIMIT + 1)
    if prefixes is not None:
        # Listing drops entries outside every granted prefix.
        entries = [e for e in entries if prefix_visible(prefixes, e["rel"])]
    truncated = len(entries) > LIST_LIMIT
    entries = entries[:LIST_LIMIT]
    tree_nodes = build_tree(entries, active_dir=d)
    if d:
        entries = [
            e for e in entries if e["rel"] == d or e["rel"].startswith(d + "/")
        ]
    if q:
        entries = [e for e in entries if q in e["rel"].lower()]
    total = len(entries)
    pages = max(1, (total + per_page - 1) // per_page)
    page = min(page, pages)
    rows = group_rows(entries[(page - 1) * per_page : page * per_page])
    has_top = (roots() / "top.sls").is_file()
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
    target = safe_join(rel)
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
    rel_posix = target.relative_to(roots()).as_posix()
    result = git_commit_file(
        rel_posix,
        f"overstate({current_user.username}): {rel_posix}",
        current_user.username,
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


# --- Formulas --------------------------------------------------------

def _formulas_context(remove_pending=None):
    """Tracked formulas joined to on-disk ``salt/<name>/`` trees.

    DB rows are the index; the filesystem is truth. Rows without a dir
    report ``missing``; dirs without a row report ``untracked``.
    """
    from .db import get_session
    from .models import Formula

    session = get_session()
    rows = session.query(Formula).order_by(Formula.name).all()
    by_name = {r.name: r for r in rows}
    dirs = {d["name"] for d in list_formula_dirs()}
    formulas = []
    for row in rows:
        present = row.name in dirs
        formulas.append(
            {
                "name": row.name,
                "url": row.url,
                "branch": row.branch,
                "installed_sha": row.installed_sha,
                "installed_short": (row.installed_sha or "")[:12] or None,
                "present": present,
                "tracked": True,
                "status": "tracked" if present else "missing",
                "in_top": formula_in_top(row.name) if present else False,
            }
        )
    untracked = [
        {
            "name": name,
            "present": True,
            "tracked": False,
            "status": "untracked",
            "in_top": formula_in_top(name),
        }
        for name in sorted(dirs - set(by_name))
    ]
    return {
        "formulas": formulas,
        "untracked": untracked,
        "formula_summary": {
            "tracked": len(formulas),
            "present": sum(1 for f in formulas if f["present"]),
            "missing": sum(1 for f in formulas if not f["present"]),
            "untracked": len(untracked),
        },
        "token_configured": has_token(),
        "remove_pending": remove_pending,
    }


def _render_formulas_tab(**overrides):
    context = _files_page_context()
    context.update(_formulas_context(**overrides))
    fsub = request.args.get("fsub", "tracked")
    if fsub not in ("tracked", "untracked"):
        fsub = "tracked"
    return render_template("files.html", **context, ftab="formulas", fsub=fsub)


def _formula_files_preview(name: str, limit: int = 12) -> list[str]:
    """Relative file names inside ``salt/<name>/`` for remove previews."""
    target = safe_join(name)
    if target is None or not target.is_dir():
        return []
    names: list[str] = []
    for base, _dirs, files in os.walk(target, followlinks=False):
        for fname in files:
            try:
                rel = str((Path(base) / fname).relative_to(target))
            except (OSError, ValueError):
                continue
            names.append(f"{name}/{rel}")
            if len(names) >= 200:
                return sorted(names)
    return sorted(names)


def _note_formula_refreshed(verb: str, name: str) -> None:
    err = _refresh_fileserver()
    if err is None:
        log_event(current_user.username, "fileserver-update")
    else:
        flash(
            f"{verb} {name}, but the master refresh failed ({err}); "
            "applies may lag.",
            "warning",
        )
        log_event(current_user.username, f"fileserver-update-failed:{err}")
    dest = roots() / name
    if (dest / "_modules").is_dir():
        flash("Custom modules changed. Run Sync modules.", "info")


def _maybe_persist_shared_token(token: str, url: str, requested: bool) -> None:
    """Persist an https token into the shared slot. Admin-only by design:
    operators vendor with the one-time token and leave shared creds alone.
    """
    if not requested or not (token or "").strip():
        return
    if not url.startswith("https://"):
        return
    from .authz import rbac_mode

    if rbac_mode() == "scoped":
        from .authz import authorize

        if not authorize(current_user, "file.git"):
            flash(
                "Cloned with the one-time token. Only a file.git grant "
                "can store shared credentials.",
                "warning",
            )
            return
    elif current_user.role != "admin":
        flash(
            "Cloned with the one-time token. Only admins store "
            "shared credentials.",
            "warning",
        )
        return
    host = url.split("/", 3)[2] if "/" in url[8:] else ""
    result = save_token(token.strip(), host)
    if result["ok"]:
        log_event(current_user.username, "git-token-saved")
        flash(f"Token stored for {host}.", "success")
    else:
        flash(f"Shared token not stored ({result['reason']}).", "warning")


@bp.post("/formulas/add")
@roles_required("operator")
def formulas_add():
    from .authz import rbac_mode, require
    from .db import get_session
    from .models import Formula

    name = (request.form.get("name") or "").strip().lower()
    url = (request.form.get("url") or "").strip()
    branch = (request.form.get("branch") or "").strip()
    auth = (request.form.get("auth") or "shared").strip()
    token = request.form.get("token", "") or None
    if rbac_mode() == "scoped":
        require("file.write", prefix=name or None)
    slug = validate_formula_name(name)
    if not slug["ok"]:
        flash(f"Add refused: {slug['reason']}. Nothing changed.", "error")
        return redirect(url_for("files.index", tab="formulas"))
    session = get_session()
    if session.query(Formula).filter_by(name=slug["name"]).first() is not None:
        flash(f"Add refused: '{slug['name']}' is already tracked.", "error")
        return redirect(url_for("files.index", tab="formulas"))
    result = vendor_formula(slug["name"], url, branch or None, auth, token)
    if not result["ok"]:
        flash(f"Add refused: {result['reason']}. Nothing changed.", "error")
        log_event(current_user.username, f"formula-add-refused:{slug['name']}")
        return redirect(url_for("files.index", tab="formulas"))
    session.add(
        Formula(
            name=result["name"],
            url=url.strip(),
            branch=(branch.strip() or None),
            installed_sha=result.get("sha"),
            created_by=current_user.username,
        )
    )
    session.commit()
    _maybe_persist_shared_token(
        token or "", url.strip(), bool(request.form.get("save_shared"))
    )
    _note_formula_refreshed("Vendored", result["name"])
    sha = (result.get("sha") or "")[:12] or "unknown"
    flash(f"Vendored formula '{result['name']}' at {sha}. Push sends it upstream.", "success")
    log_event(current_user.username, f"formula-add:{result['name']}:{sha}")
    return redirect(url_for("files.index", tab="formulas"))


@bp.post("/formulas/track")
@roles_required("operator")
def formulas_track():
    from .authz import rbac_mode, require
    from .db import get_session
    from .models import Formula

    name = (request.form.get("name") or "").strip().lower()
    url = (request.form.get("url") or "").strip()
    branch = (request.form.get("branch") or "").strip()
    if rbac_mode() == "scoped":
        require("file.write", prefix=name or None)
    slug = validate_formula_name(name)
    if not slug["ok"]:
        flash(f"Track refused: {slug['reason']}.", "error")
        return redirect(url_for("files.index", tab="formulas"))
    valid = validate_repo_url(url)
    if not valid["ok"]:
        flash(f"Track refused: {valid['reason']}.", "error")
        return redirect(url_for("files.index", tab="formulas"))
    session = get_session()
    if session.query(Formula).filter_by(name=slug["name"]).first() is not None:
        flash(f"'{slug['name']}' is already tracked.", "info")
        return redirect(url_for("files.index", tab="formulas"))
    if slug["name"] not in {d["name"] for d in list_formula_dirs()}:
        flash(f"Track refused: '{slug['name']}/' is not installed.", "error")
        return redirect(url_for("files.index", tab="formulas"))
    session.add(
        Formula(
            name=slug["name"],
            url=url.strip(),
            branch=(branch.strip() or None),
            installed_sha=None,
            created_by=current_user.username,
        )
    )
    session.commit()
    flash(f"Tracking '{slug['name']}'. Update pins its upstream commit.", "success")
    log_event(current_user.username, f"formula-track:{slug['name']}")
    return redirect(url_for("files.index", tab="formulas"))


@bp.post("/formulas/update/<name>")
@roles_required("operator")
def formulas_update(name: str):
    from .authz import rbac_mode, require
    from .db import get_session
    from .models import Formula

    slug = validate_formula_name(name)
    if not slug["ok"]:
        flash(f"Update refused: {slug['reason']}.", "error")
        return redirect(url_for("files.index", tab="formulas"))
    if rbac_mode() == "scoped":
        require("file.write", prefix=slug["name"])
    session = get_session()
    row = session.query(Formula).filter_by(name=slug["name"]).first()
    if row is None:
        flash(f"Update refused: '{slug['name']}' is not tracked.", "error")
        return redirect(url_for("files.index", tab="formulas"))
    auth = (request.form.get("auth") or "shared").strip()
    token = request.form.get("token", "") or None
    result = update_formula(row.name, row.url, row.branch, auth, token)
    if not result["ok"]:
        flash(f"Update refused: {result['reason']}. Nothing changed.", "error")
        log_event(current_user.username, f"formula-update-refused:{row.name}")
        return redirect(url_for("files.index", tab="formulas"))
    if result.get("unchanged"):
        flash(f"'{row.name}' is already up to date.", "info")
        log_event(current_user.username, f"formula-update-unchanged:{row.name}")
        return redirect(url_for("files.index", tab="formulas"))
    row.installed_sha = result.get("sha") or row.installed_sha
    session.commit()
    _maybe_persist_shared_token(
        token or "", row.url, bool(request.form.get("save_shared"))
    )
    _note_formula_refreshed("Updated", row.name)
    sha = (result.get("sha") or "")[:12] or "unknown"
    flash(f"Updated formula '{row.name}' to {sha}. Push sends it upstream.", "success")
    log_event(current_user.username, f"formula-update:{row.name}:{sha}")
    return redirect(url_for("files.index", tab="formulas"))


@bp.post("/formulas/remove/<name>")
@roles_required("operator")
def formulas_remove(name: str):
    from .authz import rbac_mode, require
    from .db import get_session
    from .models import Formula

    slug = validate_formula_name(name)
    if not slug["ok"]:
        flash(f"Remove refused: {slug['reason']}.", "error")
        return redirect(url_for("files.index", tab="formulas"))
    if rbac_mode() == "scoped":
        require("file.write", prefix=slug["name"])
    session = get_session()
    row = session.query(Formula).filter_by(name=slug["name"]).first()
    if row is None:
        flash(f"Remove refused: '{slug['name']}' is not tracked.", "error")
        return redirect(url_for("files.index", tab="formulas"))
    if request.form.get("confirm") != "1":
        files = _formula_files_preview(slug["name"])
        log_event(current_user.username, f"formula-remove-preview:{slug['name']}")
        return _render_formulas_tab(
            remove_pending={"name": slug["name"], "files": files}
        )
    present = (roots() / slug["name"]).is_dir()
    if present:
        result = remove_formula_files(slug["name"])
        if not result["ok"]:
            flash(f"Remove refused: {result['reason']}. Nothing changed.", "error")
            log_event(
                current_user.username, f"formula-remove-refused:{slug['name']}"
            )
            return redirect(url_for("files.index", tab="formulas"))
        _note_formula_refreshed("Removed", slug["name"])
    session.delete(row)
    session.commit()
    flash(
        f"Removed formula '{slug['name']}'. Push sends the removal upstream.",
        "success",
    )
    log_event(current_user.username, f"formula-remove:{slug['name']}")
    return redirect(url_for("files.index", tab="formulas"))


def _repo_context(preview=None, pending=None, replace_pending=None):
    checkout = is_checkout()
    return {
        "checkout": checkout,
        "status": git_status() if checkout else None,
        "origin": git_origin() if checkout else None,
        "token_configured": has_token(),
        "preview": preview,
        "pending": pending,
        "replace_pending": replace_pending,
        "roots_nonempty": roots_nonempty(),
        "srv_nonempty": srv_nonempty(),
        "layout": checkout_layout(),
    }


def _render_repo_tab(**overrides):
    context = _files_page_context()
    context.update(_repo_context(**overrides))
    context.update(_formulas_context())
    return render_template("files.html", **context, ftab="repo")


def _note_refreshed(verb, sha):
    """Fileserver refresh after a tree-changing repo op, sync-style."""
    err = _refresh_fileserver()
    if err is None:
        flash(f"{verb} at {sha}; master fileserver refreshed.", "success")
        log_event(current_user.username, "fileserver-update")
    else:
        flash(
            f"{verb} at {sha}. Master refresh failed ({err}); applies may lag.",
            "warning",
        )
        log_event(current_user.username, f"fileserver-update-failed:{err}")


@bp.get("/repo")
@roles_required("admin")
def repo():
    from .authz import rbac_mode, require

    if rbac_mode() == "scoped":
        require("file.git")
    """Repo tab: bootstrap, repoint, and repair the shared-states checkout."""
    return redirect(url_for("files.index", tab="repo"))


def _short_list(names: list[str], limit: int = 12) -> str:
    """Human-sized file list for flashes: first names, then a count."""
    shown = ", ".join(names[:limit])
    if len(names) > limit:
        shown += f", and {len(names) - limit} more"
    return shown


@bp.post("/repo/clone")
@roles_required("admin")
def repo_clone():
    from .authz import rbac_mode, require

    if rbac_mode() == "scoped":
        require("file.git")
    """Clone the canonical states repo at the srv roots. Admin-only."""
    url = request.form.get("url", "")
    branch = request.form.get("branch", "")
    token = request.form.get("token", "") or None
    replace = request.form.get("confirm") == "1"
    result = clone_repo(url, branch, token, replace=replace)
    if result["ok"]:
        log_event(current_user.username, "git-clone")
        _note_refreshed("Cloned", git_head())
        if result.get("replaced"):
            flash(
                f"Replaced the existing salt/ tree: {_short_list(result['replaced'])}.",
                "info",
            )
    elif result.get("confirm_replace") is not None:
        # An existing salt/ tree (the deploy seed) is only ever replaced
        # on a second, explicit confirm — stay on the page naming it.
        log_event(current_user.username, "git-clone-needs-confirm")
        return _render_repo_tab(
            replace_pending={
                "url": url,
                "branch": branch,
                "files": result["confirm_replace"],
            }
        )
    else:
        flash(f"Clone refused: {result['reason']}. Nothing changed.", "error")
        log_event(current_user.username, f"git-clone-refused:{result['reason']}")
        if result["reason"].startswith("directory not empty"):
            # Plain clone can never succeed here (unexpected content needs
            # the destructive path); stay on the page with the re-clone
            # form prefilled so the advised path is one click away.
            return _render_repo_tab(pending={"url": url, "branch": branch})
    return redirect(url_for("files.repo"))


@bp.post("/repo/set-remote")
@roles_required("admin")
def repo_set_remote():
    from .authz import rbac_mode, require

    if rbac_mode() == "scoped":
        require("file.git")
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
    from .authz import rbac_mode, require

    if rbac_mode() == "scoped":
        require("file.git")
    """Two-step reset to the tracked upstream: preview, then confirm."""
    if request.form.get("confirm") != "1":
        preview = reset_preview()
        if not preview["ok"]:
            flash(
                f"Reset refused: {preview['reason']}. Nothing changed.",
                "error",
            )
            return redirect(url_for("files.repo"))
        return _render_repo_tab(preview=preview)
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
    from .authz import rbac_mode, require

    if rbac_mode() == "scoped":
        require("file.git")
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
    from .authz import rbac_mode, require

    if rbac_mode() == "scoped":
        require("file.git")
    """Store an https token for the current origin's host (0600)."""
    origin = git_origin()
    if not origin or not origin.startswith("https://"):
        flash(
            "Save refused: clone or set an https remote first. "
            "Tokens are stored per host.",
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
    from .authz import rbac_mode, require

    if rbac_mode() == "scoped":
        require("file.git")
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
    from .authz import rbac_mode, require

    rel = request.args.get("path", "")
    target = safe_join(rel)
    if not target or not target.is_file():
        abort(404)
    if rbac_mode() == "scoped":
        require("file.read", prefix=rel)
    parent = rel.rpartition("/")[0] if "/" in rel else ""
    crumbs = dir_crumbs(parent)
    content = read_text(target)
    if content is not None:
        highlighted = (
            highlight_yaml(content) if target.suffix.lower() in YAML_SUFFIXES else None
        )
        return render_template(
            "file_view.html",
            rel=rel,
            crumbs=crumbs,
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
        crumbs=crumbs,
        content=None,
        lines=[],
        reason=reason,
        size=size,
        max_bytes=MAX_BYTES,
        revision=sync_revision(),
    )
