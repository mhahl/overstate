"""State repo bootstrap + repair, under Settings.

Rare, admin-only, config-flavored: clone a missing checkout, repoint a
moved origin, reset to the tracked upstream, re-clone a corrupt
checkout, and manage the one https token slot. Lives beside Master
settings following the same pattern — a standalone page reached from
the Settings sidebar's Infrastructure group.
"""

from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_login import current_user

from .audit import log_event
from .auth import roles_required
from .files import _refresh_fileserver
from .git_sync import (
    checkout_layout,
    clear_token,
    clone_repo,
    git_head,
    git_origin,
    git_status,
    has_token,
    is_checkout,
    reclone_repo,
    reset_hard,
    reset_preview,
    roots_nonempty,
    save_token,
    set_remote_origin,
    srv_nonempty,
    validate_repo_url,
)

bp = Blueprint("state_repo", __name__, url_prefix="/settings/repo")


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


def _render(**overrides):
    return render_template("state_repo.html", **_repo_context(**overrides))


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


def _short_list(names: list[str], limit: int = 12) -> str:
    """Human-sized file list for flashes: first names, then a count."""
    shown = ", ".join(names[:limit])
    if len(names) > limit:
        shown += f", and {len(names) - limit} more"
    return shown


@bp.route("/")
@roles_required("admin")
def index():
    from .authz import rbac_mode, require

    if rbac_mode() == "scoped":
        require("file.git")
    return _render()


@bp.post("/clone")
@roles_required("admin")
def clone():
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
        return _render(
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
            return _render(pending={"url": url, "branch": branch})
    return redirect(url_for("state_repo.index"))


@bp.post("/set-remote")
@roles_required("admin")
def set_remote():
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
    return redirect(url_for("state_repo.index"))


@bp.post("/reset")
@roles_required("admin")
def reset():
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
            return redirect(url_for("state_repo.index"))
        return _render(preview=preview)
    clean = request.form.get("clean") == "1"
    result = reset_hard(clean_untracked=clean)
    if result["ok"]:
        log_event(current_user.username, "git-reset")
        _note_refreshed("Reset", result["new"])
    else:
        flash(f"Reset refused: {result['reason']}. Nothing changed.", "error")
        log_event(current_user.username, f"git-reset-refused:{result['reason']}")
    return redirect(url_for("state_repo.index"))


@bp.post("/reclone")
@roles_required("admin")
def reclone():
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
            return redirect(url_for("state_repo.index"))
        return _render(pending={"url": url, "branch": branch})
    result = reclone_repo(url, branch, token)
    if result["ok"]:
        log_event(current_user.username, "git-reclone")
        _note_refreshed("Re-cloned", git_head())
    else:
        flash(f"Re-clone refused: {result['reason']}. Nothing changed.", "error")
        log_event(current_user.username, f"git-reclone-refused:{result['reason']}")
    return redirect(url_for("state_repo.index"))


@bp.post("/token-save")
@roles_required("admin")
def token_save():
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
        return redirect(url_for("state_repo.index"))
    host = origin.split("/", 3)[2]
    result = save_token(request.form.get("token", ""), host)
    if result["ok"]:
        log_event(current_user.username, "git-token-saved")
        flash(f"Token stored for {host}.", "success")
    else:
        flash(f"Save refused: {result['reason']}. Nothing changed.", "error")
    return redirect(url_for("state_repo.index"))


@bp.post("/token-clear")
@roles_required("admin")
def token_clear():
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
    return redirect(url_for("state_repo.index"))
