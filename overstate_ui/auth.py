"""Auth: local password login plus OIDC SSO with role-based access.

Roles are viewer < operator < admin. ``roles_required`` gates mutating
routes; read-only routes keep bare ``login_required``. OIDC users are
JIT-provisioned as viewers (no local password) and promoted by an admin.
"""

import functools
import time

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
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
from flask_login import (
    LoginManager,
    current_user,
    login_required,
    login_user,
    logout_user,
)
from flask_wtf import FlaskForm
from wtforms import PasswordField, StringField
from wtforms.validators import DataRequired

from .db import get_session
from .models import User

bp = Blueprint("auth", __name__)
login_manager = LoginManager()
login_manager.login_view = "auth.login"

_ph = PasswordHasher()
_attempts: dict[str, list[float]] = {}

LEVELS = {"viewer": 0, "operator": 1, "admin": 2}


def role_level(role: str | None) -> int:
    return LEVELS.get(role or "", 0)


def roles_required(*roles: str):
    """Require one of ``roles``; higher levels imply lower ones."""
    minimum = min(LEVELS[r] for r in roles)

    def decorator(view):
        @functools.wraps(view)
        @login_required
        def guarded(*args, **kwargs):
            if role_level(getattr(current_user, "role", None)) < minimum:
                abort(403)
            return view(*args, **kwargs)

        return guarded

    return decorator


def oidc_enabled() -> bool:
    cfg = current_app.config
    return bool(cfg.get("OIDC_ISSUER") and cfg.get("OIDC_CLIENT_ID")
                and cfg.get("OIDC_CLIENT_SECRET"))


def _oauth_client():
    from authlib.integrations.flask_client import OAuth

    oauth = OAuth()
    oauth.register(
        "oidc",
        server_metadata_url=f"{current_app.config['OIDC_ISSUER']}/"
        ".well-known/openid-configuration",
        client_id=current_app.config["OIDC_CLIENT_ID"],
        client_secret=current_app.config["OIDC_CLIENT_SECRET"],
        client_kwargs={"scope": "openid email profile"},
    )
    return oauth.oidc


def provision_oidc_user(userinfo: dict) -> User:
    """Find-or-create a user from OIDC claims. New users start as viewers."""
    username = (
        userinfo.get("preferred_username") or userinfo.get("email") or userinfo.get("sub")
    )
    if not username:
        raise ValueError("OIDC claims carry no usable username")
    session = get_session()
    user = session.query(User).filter_by(username=username).first()
    if user is None:
        user = User(username=username, password_hash=None, role="viewer")
        session.add(user)
        session.commit()
    return user


def _rate_limited(ip: str, limit: int = 10, window: int = 60) -> bool:
    now = time.monotonic()
    hits = [t for t in _attempts.get(ip, []) if now - t < window]
    if len(hits) >= limit:
        _attempts[ip] = hits
        return True
    hits.append(now)
    _attempts[ip] = hits
    return False


@login_manager.user_loader
def load_user(user_id: str):
    return get_session().get(User, int(user_id))


def seed_admin(username: str = "admin", password: str | None = None) -> bool:
    """Create the initial admin only when the users table is empty."""
    session = get_session()
    if session.query(User).count() > 0:
        return False
    import secrets

    password = password or secrets.token_urlsafe(16)
    session.add(User(username=username, password_hash=_ph.hash(password),
                     role="admin"))
    session.commit()
    print(f"seeded admin '{username}' with password: {password}")
    return True


class LoginForm(FlaskForm):
    username = StringField(validators=[DataRequired()])
    password = PasswordField(validators=[DataRequired()])


@bp.route("/login", methods=["GET", "POST"])
def login():
    form = LoginForm()
    if form.validate_on_submit():
        if _rate_limited(request.remote_addr or "unknown"):
            flash("Too many attempts. Try again later.")
            return render_template("login.html", form=form), 429
        user = get_session().query(User).filter_by(username=form.username.data).first()
        try:
            valid = (
                user is not None
                and user.password_hash is not None
                and _ph.verify(user.password_hash, form.password.data)
            )
        except VerifyMismatchError:
            valid = False
        if valid:
            _attempts.pop(request.remote_addr or "unknown", None)
            login_user(user)
            return redirect(url_for("dashboard.index"))
        flash("Invalid credentials.")
    return render_template("login.html", form=form)


@bp.route("/login/oidc")
def oidc_login():
    if not oidc_enabled():
        abort(404)
    redirect_uri = url_for("auth.oidc_callback", _external=True)
    return _oauth_client().authorize_redirect(redirect_uri)


@bp.route("/login/oidc/callback")
def oidc_callback():
    if not oidc_enabled():
        abort(404)
    token = _oauth_client().authorize_access_token()
    userinfo = token.get("userinfo") or _oauth_client().userinfo(token=token)
    try:
        user = provision_oidc_user(dict(userinfo))
    except ValueError as exc:
        flash(f"SSO login failed: {exc}")
        return redirect(url_for("auth.login"))
    login_user(user)
    return redirect(url_for("dashboard.index"))


@bp.route("/logout", methods=["POST"])
@login_required
def logout():
    logout_user()
    return redirect(url_for("auth.login"))
