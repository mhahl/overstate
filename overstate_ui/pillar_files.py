"""Pillar-source browser: the ``pillar/`` tree the master renders.

The Files page shows the state roots (``salt/`` only). Pillar source
lives a level up at the checkout root — ``pillar/top.sls``, plaintext
pillar files, and the ``secrets/`` tree — and was previously
invisible in the UI. This blueprint reuses the Files file-browser
machinery against that root.

Pillar source is browseable and *viewable* by anyone with ``file.read``
(the ``secrets/`` files hold only ``NACL[...]`` ciphertext, so nothing
leaks). Editing is ``file.write`` (operator+), with one hard rule:
``secrets/`` is read-only here — the Encrypted pillar editor is the
only writer, because a generic editor would risk plaintext or a
hand-mangled ``NACL[...]`` envelope landing in git.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from .audit import log_event
from .auth import roles_required
from .files import (
    MAX_BYTES,
    YAML_SUFFIXES,
    _refresh_fileserver,
    build_tree,
    dir_crumbs,
    group_rows,
    highlight_yaml,
    list_tree,
    normalize_dir,
    read_text,
    safe_join,
    sync_revision,
    yaml_advisory,
)
from .git_sync import _toplevel, git_commit_file, git_head, is_checkout

bp = Blueprint("pillar_files", __name__, url_prefix="/pillar/files")

LIST_LIMIT = 5000
SECRETS_PREFIX = "secrets/"


def pillar_root() -> Path:
    """The ``pillar/`` source tree: checkout root, or srv beside roots."""
    from .pillar_secrets import _pillar_dir

    return _pillar_dir()


def _is_secret(rel: str) -> bool:
    """Whether ``rel`` lives under the encrypted ``secrets/`` tree."""
    return rel == "secrets" or rel.startswith(SECRETS_PREFIX)


def _relative_commit(rel: str) -> str:
    """Checkout-root-relative path for git commits (``pillar/<rel>``)."""
    return f"pillar/{rel}"


@bp.route("/")
@login_required
def index():
    from .authz import require

    require("file.read")
    q = request.args.get("q", "").strip().lower()
    from .minions import resolve_page_size

    try:
        page = max(1, int(request.args.get("page", 1)))
    except ValueError:
        page = 1
    per_page = resolve_page_size(request.args.get("per_page"))
    d = normalize_dir(request.args.get("dir", ""))
    root = pillar_root()
    entries = list_tree(limit=LIST_LIMIT + 1, base=root)
    truncated = len(entries) > LIST_LIMIT
    entries = entries[:LIST_LIMIT]
    tree_nodes = build_tree(entries, active_dir=d)
    if d:
        entries = [e for e in entries if e["rel"] == d or e["rel"].startswith(d + "/")]
    if q:
        entries = [e for e in entries if q in e["rel"].lower()]
    total = len(entries)
    pages = max(1, (total + per_page - 1) // per_page)
    page = min(page, pages)
    rows = group_rows(entries[(page - 1) * per_page : page * per_page])
    crumbs = dir_crumbs(d, root_label="Pillar")
    has_top = (root / "top.sls").is_file()
    return render_template(
        "pillar_files.html",
        rows=rows,
        total=total,
        page=page,
        pages=pages,
        per_page=per_page,
        q=q,
        d=d,
        crumbs=crumbs,
        tree_nodes=tree_nodes,
        truncated=truncated,
        has_top=has_top,
        checkout=is_checkout(),
        revision=sync_revision(),
    )


@bp.route("/view")
@login_required
def view():
    from .authz import require

    rel = request.args.get("path", "")
    root = pillar_root()
    target = safe_join(rel, base=root)
    if not target or not target.is_file():
        abort(404)
    require("file.read", prefix=rel)
    parent = rel.rpartition("/")[0] if "/" in rel else ""
    crumbs = dir_crumbs(parent, root_label="Pillar")
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
            is_secret=_is_secret(rel),
        )
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
        is_secret=_is_secret(rel),
    )


@bp.route("/edit")
@roles_required("operator")
def edit():
    from .authz import require

    rel = request.args.get("path", "")
    root = pillar_root()
    target = safe_join(rel, base=root)
    if not target or not target.is_file():
        abort(404)
    require("file.write", prefix=rel)
    if _is_secret(rel):
        flash("Encrypted pillar files live in the Encrypted pillar editor — edit them there, not here.", "error")
        return redirect(url_for("pillar_files.view", path=rel))
    content = read_text(target)
    if content is None:
        flash("That file cannot be edited here (too large or not text).", "error")
        return redirect(url_for("pillar_files.view", path=rel))
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
    from .authz import require

    rel = request.form.get("path", "")
    root = pillar_root()
    target = safe_join(rel, base=root)
    if not target or not target.is_file():
        abort(404)
    require("file.write", prefix=rel)
    if _is_secret(rel):
        flash("Encrypted pillar files are read-only in the file browser — use the Encrypted pillar editor.", "error")
        log_event(current_user.username, f"pillar-file-save-refused:{rel}:secret")
        return redirect(url_for("pillar_files.view", path=rel))
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
        log_event(current_user.username, f"pillar-file-save-refused:{rel}:uneditable")
        return redirect(url_for("pillar_files.view", path=rel))
    data = request.form.get("content", "").encode("utf-8")
    if len(data) > MAX_BYTES:
        flash(f"Too large to save ({len(data)} bytes; limit is {MAX_BYTES}). Nothing changed.", "error")
        log_event(current_user.username, f"pillar-file-save-refused:{rel}:oversize")
        return redirect(url_for("pillar_files.view", path=rel))
    if data == current:
        flash("No changes. Nothing committed.", "info")
        return redirect(url_for("pillar_files.view", path=rel))
    if not is_checkout():
        flash("Not a git checkout: pillar files are read-only here. Nothing changed.", "error")
        log_event(current_user.username, f"pillar-file-save-refused:{rel}:not-a-checkout")
        return redirect(url_for("pillar_files.view", path=rel))
    head = git_head()
    base_ok = (head is not None and request.form.get("base_sha") == head) or (
        head is None and request.form.get("base_sha") == "nogit"
    )
    if not base_ok or request.form.get("base_hash") != hashlib.sha256(current).hexdigest():
        short = head[:7] if head else "unknown"
        flash(f"That file changed (now {short}). Reload and re-apply your edits. Nothing was written.", "error")
        log_event(current_user.username, f"pillar-file-save-refused:{rel}:stale")
        return redirect(url_for("pillar_files.edit", path=rel))
    try:
        tmp = target.with_name(target.name + ".overstate-tmp")
        tmp.write_bytes(data)
        os.replace(tmp, target)
    except OSError:
        flash("Could not write the file. Nothing changed.", "error")
        log_event(current_user.username, f"pillar-file-save-refused:{rel}:write-failed")
        return redirect(url_for("pillar_files.view", path=rel))
    commit_rel = _relative_commit(rel)
    result = git_commit_file(commit_rel, f"overstate({current_user.username}): {commit_rel}", current_user.username, cwd=_toplevel())
    if not result["ok"]:
        flash(f"Saved on disk but not committed ({result['reason']}). Fix it in git.", "error")
        log_event(current_user.username, f"pillar-file-save-uncommitted:{rel}:{result['reason']}")
        return redirect(url_for("pillar_files.view", path=rel))
    err = _refresh_fileserver()
    if err is None:
        flash(f"Saved {rel} as {result['new']}.", "success")
    else:
        flash(f"Saved {rel} as {result['new']}. Master refresh failed ({err}); applies may lag.", "warning")
    advisory = yaml_advisory(target, data)
    if advisory is not None:
        flash(advisory, "warning")
    log_event(current_user.username, f"pillar-file-save:{rel}:{result['new']}")
    return redirect(url_for("pillar_files.view", path=rel))