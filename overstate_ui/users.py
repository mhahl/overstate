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
    users = get_session().query(User).order_by(User.username).all()
    return render_template("users.html", users=users, roles=list(LEVELS))


@bp.post("/<int:uid>/role")
@roles_required("admin")
def set_role(uid: int):
    session = get_session()
    user = session.get(User, uid)
    role = request.form.get("role", "")
    if user is None:
        flash("Unknown user.")
    elif role not in LEVELS:
        flash("Unknown role.")
    elif user.id == current_user.id and role != "admin":
        flash("You cannot demote yourself.")
    else:
        user.role = role
        session.commit()
        flash(f"'{user.username}' is now {role}.")
    return redirect(url_for("users.index"))


@bp.post("/<int:uid>/delete")
@roles_required("admin")
def delete(uid: int):
    session = get_session()
    user = session.get(User, uid)
    if user is None:
        flash("Unknown user.")
    elif user.id == current_user.id:
        flash("You cannot delete yourself.")
    else:
        session.delete(user)
        session.commit()
        flash(f"Deleted '{user.username}'.")
    return redirect(url_for("users.index"))
