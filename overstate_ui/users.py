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


@bp.route("/rotation")
@roles_required("admin")
def rotation():
    """Step 1: show a fresh password plus the two-sided apply steps."""
    import secrets

    from flask import current_app

    return render_template(
        "users_rotation.html",
        password=secrets.token_urlsafe(18),
        eauth_user=current_app.config["SALT_EAUTH_USER"],
    )


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
    candidate = build_client()
    candidate.password = password
    try:
        candidate.login()
    except SaltApiError as exc:
        flash(f"verification failed: {exc}", "error")
    else:
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
