"""Admin user management. Lists users, changes roles, deletes users."""

from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_login import current_user

from .auth import LEVELS, roles_required
from .db import get_session
from .models import User

bp = Blueprint("users", __name__, url_prefix="/users")


@bp.route("/")
@roles_required("admin")
def index():
    role = request.args.get("role", "")
    if role not in LEVELS:
        role = ""
    sort = request.args.get("sort", "username")
    if sort not in ("username", "role"):
        sort = "username"
    direction = request.args.get("dir", "asc")
    if direction not in ("asc", "desc"):
        direction = "asc"
    query = get_session().query(User)
    if role:
        query = query.filter_by(role=role)
    col = User.username if sort == "username" else User.role
    col = col.desc() if direction == "desc" else col.asc()
    users = query.order_by(col, User.username).all()
    return render_template(
        "users.html",
        users=users,
        roles=list(LEVELS),
        role=role,
        sort=sort,
        direction=direction,
    )


@bp.post("/<int:uid>/role")
@roles_required("admin")
def set_role(uid: int):
    session = get_session()
    user = session.get(User, uid)
    role = request.form.get("role", "")
    if user is None:
        flash("Unknown user.", "error")
    elif role not in LEVELS:
        flash("Unknown role.", "error")
    elif user.id == current_user.id and role != "admin":
        flash("You cannot demote yourself.", "error")
    else:
        user.role = role
        session.commit()
        flash(f"'{user.username}' is now {role}.", "success")
    return redirect(url_for("users.index"))


ROTATION_TTL = 30 * 60


def _rotation_store():
    """Server-side home for the pending rotation password: Redis with a
    30-minute TTL. None when the cache is unreachable, so the wizard
    refuses rather than falling back to the login cookie."""
    try:
        from .tasks_queue import get_redis_client

        client = get_redis_client()
        client.ping()
        return client
    except Exception:  # noqa: BLE001 — Redis down means no wizard
        return None


def _rotation_key() -> str:
    return f"rotation:{current_user.id}"


@bp.route("/rotation")
@roles_required("admin")
def rotation():
    """Step 1: show the pending password plus the two-sided apply steps.

    The password is minted once and kept on the server until it is
    verified or regenerated: re-rendering the page must never silently
    swap the password the admin is mid-applying."""
    import secrets

    from flask import current_app

    store = _rotation_store()
    if store is None:
        flash("Rotation needs the cache.", "error")
        return render_template(
            "users_rotation.html",
            password=None,
            eauth_user=current_app.config["SALT_EAUTH_USER"],
        )
    raw = store.get(_rotation_key())
    password = raw.decode() if isinstance(raw, bytes) else raw
    if not password:
        password = secrets.token_urlsafe(18)
        store.set(_rotation_key(), password, ex=ROTATION_TTL)
    return render_template(
        "users_rotation.html",
        password=password,
        eauth_user=current_app.config["SALT_EAUTH_USER"],
    )


@bp.post("/rotation/regenerate")
@roles_required("admin")
def rotation_regenerate():
    """Mint a fresh pending password, discarding the unapplied one."""
    import secrets

    store = _rotation_store()
    if store is None:
        flash("Rotation needs the cache.", "error")
        return redirect(url_for("users.rotation"))
    store.set(_rotation_key(), secrets.token_urlsafe(18), ex=ROTATION_TTL)
    flash("New password generated. Previous discarded.", "info")
    return redirect(url_for("users.rotation"))


@bp.post("/rotation/verify")
@roles_required("admin")
def rotation_verify():
    """Step 2: try the pasted password against salt-api. Never stored."""
    from .audit import log_event
    from .salt_client import SaltApiError
    from .tasks import build_client

    password = request.form.get("password", "")
    if not password:
        flash("Paste the new password to verify it.", "info")
        return redirect(url_for("users.rotation"))
    store = _rotation_store()
    if store is None:
        flash("Rotation needs the cache.", "error")
        return redirect(url_for("users.rotation"))
    candidate = build_client()
    candidate.password = password
    try:
        candidate.login()
    except SaltApiError as exc:
        flash(f"verification failed: {exc}", "error")
    else:
        store.delete(_rotation_key())
        log_event(current_user.username, "eauth-rotation-verified")
        flash(
            "New eauth password works. Update the app environment to match.", "warning"
        )
    return redirect(url_for("users.rotation"))


@bp.post("/<int:uid>/delete")
@roles_required("admin")
def delete(uid: int):
    session = get_session()
    user = session.get(User, uid)
    if user is None:
        flash("Unknown user.", "error")
    elif user.id == current_user.id:
        flash("You cannot delete yourself.", "error")
    else:
        session.delete(user)
        session.commit()
        flash(f"Deleted '{user.username}'.", "success")
    return redirect(url_for("users.index"))
