"""Tracked Salt formulas vendored under file roots, as their own page.

DB rows are the index; ``salt/<name>/`` on disk plus git are truth.
Operators vendor/update/remove (one local commit each); the Formauls
push sends it upstream via the Files checkout. Secrets ride the shared
0600 helper or an ephemeral per-clone token, never this module.
"""

from flask import (
    Blueprint,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)
from flask_login import current_user, login_required

from .audit import log_event
from .auth import roles_required
from .files import _refresh_fileserver, roots, safe_join
from .git_sync import (
    formula_committed,
    formula_in_top,
    has_token,
    list_formula_dirs,
    remove_formula_files,
    save_token,
    update_formula,
    validate_formula_name,
    validate_repo_url,
    vendor_formula,
)

bp = Blueprint("formulas", __name__, url_prefix="/formulas")


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
            "in_git": formula_committed(name),
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
            "from_git": sum(1 for u in untracked if u["in_git"]),
        },
        "token_configured": has_token(),
        "remove_pending": remove_pending,
    }


def _render(**overrides):
    context = _formulas_context(**overrides)
    fsub = request.args.get("fsub", "tracked")
    if fsub not in ("tracked", "untracked"):
        fsub = "tracked"
    return render_template("formulas.html", **context, fsub=fsub)


def _formula_files_preview(name: str, limit: int = 12) -> list[str]:
    """Relative file names inside ``salt/<name>/`` for remove previews."""
    import os
    from pathlib import Path

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


def _note_refreshed(verb: str, name: str) -> None:
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
    from .authz import rbac_mode

    if not requested or not (token or "").strip():
        return
    if not url.startswith("https://"):
        return
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


@bp.route("/")
@login_required
def index():
    from .authz import rbac_mode, require

    if rbac_mode() == "scoped":
        require("file.read")
    return _render()


@bp.post("/add")
@roles_required("operator")
def add():
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
        return redirect(url_for("formulas.index"))
    session = get_session()
    if session.query(Formula).filter_by(name=slug["name"]).first() is not None:
        flash(f"Add refused: '{slug['name']}' is already tracked.", "error")
        return redirect(url_for("formulas.index"))
    result = vendor_formula(slug["name"], url, branch or None, auth, token)
    if not result["ok"]:
        flash(f"Add refused: {result['reason']}. Nothing changed.", "error")
        log_event(current_user.username, f"formula-add-refused:{slug['name']}")
        return redirect(url_for("formulas.index"))
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
    _note_refreshed("Vendored", result["name"])
    sha = (result.get("sha") or "")[:12] or "unknown"
    flash(
        f"Vendored formula '{result['name']}' at {sha}. Push sends it upstream.",
        "success",
    )
    log_event(current_user.username, f"formula-add:{result['name']}:{sha}")
    return redirect(url_for("formulas.index"))


@bp.post("/track")
@roles_required("operator")
def track():
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
        return redirect(url_for("formulas.index"))
    valid = validate_repo_url(url)
    if not valid["ok"]:
        flash(f"Track refused: {valid['reason']}.", "error")
        return redirect(url_for("formulas.index"))
    session = get_session()
    if session.query(Formula).filter_by(name=slug["name"]).first() is not None:
        flash(f"'{slug['name']}' is already tracked.", "info")
        return redirect(url_for("formulas.index"))
    if slug["name"] not in {d["name"] for d in list_formula_dirs()}:
        flash(f"Track refused: '{slug['name']}/' is not installed.", "error")
        return redirect(url_for("formulas.index"))
    if formula_committed(slug["name"]):
        # Committed in the states repo: the source is the repo itself, so
        # there is no external URL/ref to track. Mirror the disabled UI
        # server-side so a forged POST cannot register it anyway.
        flash(
            f"Track refused: '{slug['name']}/' comes from the states repo.",
            "error",
        )
        log_event(current_user.username, f"formula-track-refused:{slug['name']}:from-repo")
        return redirect(url_for("formulas.index"))
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
    return redirect(url_for("formulas.index"))


@bp.post("/update/<name>")
@roles_required("operator")
def update(name: str):
    from .authz import rbac_mode, require
    from .db import get_session
    from .models import Formula

    slug = validate_formula_name(name)
    if not slug["ok"]:
        flash(f"Update refused: {slug['reason']}.", "error")
        return redirect(url_for("formulas.index"))
    if rbac_mode() == "scoped":
        require("file.write", prefix=slug["name"])
    session = get_session()
    row = session.query(Formula).filter_by(name=slug["name"]).first()
    if row is None:
        flash(f"Update refused: '{slug['name']}' is not tracked.", "error")
        return redirect(url_for("formulas.index"))
    auth = (request.form.get("auth") or "shared").strip()
    token = request.form.get("token", "") or None
    result = update_formula(row.name, row.url, row.branch, auth, token)
    if not result["ok"]:
        flash(f"Update refused: {result['reason']}. Nothing changed.", "error")
        log_event(current_user.username, f"formula-update-refused:{row.name}")
        return redirect(url_for("formulas.index"))
    if result.get("unchanged"):
        flash(f"'{row.name}' is already up to date.", "info")
        log_event(current_user.username, f"formula-update-unchanged:{row.name}")
        return redirect(url_for("formulas.index"))
    row.installed_sha = result.get("sha") or row.installed_sha
    session.commit()
    _maybe_persist_shared_token(
        token or "", row.url, bool(request.form.get("save_shared"))
    )
    _note_refreshed("Updated", row.name)
    sha = (result.get("sha") or "")[:12] or "unknown"
    flash(f"Updated formula '{row.name}' to {sha}. Push sends it upstream.", "success")
    log_event(current_user.username, f"formula-update:{row.name}:{sha}")
    return redirect(url_for("formulas.index"))


@bp.post("/remove/<name>")
@roles_required("operator")
def remove(name: str):
    from .authz import rbac_mode, require
    from .db import get_session
    from .models import Formula

    slug = validate_formula_name(name)
    if not slug["ok"]:
        flash(f"Remove refused: {slug['reason']}.", "error")
        return redirect(url_for("formulas.index"))
    if rbac_mode() == "scoped":
        require("file.write", prefix=slug["name"])
    session = get_session()
    row = session.query(Formula).filter_by(name=slug["name"]).first()
    if row is None:
        flash(f"Remove refused: '{slug['name']}' is not tracked.", "error")
        return redirect(url_for("formulas.index"))
    if request.form.get("confirm") != "1":
        files = _formula_files_preview(slug["name"])
        log_event(current_user.username, f"formula-remove-preview:{slug['name']}")
        return _render(remove_pending={"name": slug["name"], "files": files})
    present = (roots() / slug["name"]).is_dir()
    if present:
        result = remove_formula_files(slug["name"])
        if not result["ok"]:
            flash(f"Remove refused: {result['reason']}. Nothing changed.", "error")
            log_event(
                current_user.username, f"formula-remove-refused:{slug['name']}"
            )
            return redirect(url_for("formulas.index"))
        _note_refreshed("Removed", slug["name"])
    session.delete(row)
    session.commit()
    flash(
        f"Removed formula '{slug['name']}'. Push sends the removal upstream.",
        "success",
    )
    log_event(current_user.username, f"formula-remove:{slug['name']}")
    return redirect(url_for("formulas.index"))
