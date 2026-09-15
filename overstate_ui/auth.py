"""Auth: local password login plus OIDC SSO with role-based access.

Roles are viewer < operator < admin. ``roles_required`` gates mutating
routes; read-only routes keep bare ``login_required``. OIDC users are
JIT-provisioned as viewers (no local password) and promoted by an admin.
"""

import functools
import logging
import time

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from flask import (
    Blueprint,
    abort,
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
logger = logging.getLogger(__name__)

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
    from .settings import get_setting

    return bool(
        get_setting("oidc_issuer")
        and get_setting("oidc_client_id")
        and get_setting("oidc_client_secret")
    )


def _oauth_client():
    """Shared OIDC client, registered once per app and refreshed when the
    settings change. The issuer must be https (an http issuer would hand
    the code flow to a network attacker); discovery is fetched with a
    timeout so a bad issuer fails fast instead of hanging a worker."""
    import httpx
    from authlib.integrations.flask_client import OAuth
    from flask import current_app

    from .settings import get_setting

    issuer = get_setting("oidc_issuer").rstrip("/")
    if not issuer.startswith("https://"):
        raise ValueError("OIDC issuer must be an https URL")
    try:
        resp = httpx.get(f"{issuer}/.well-known/openid-configuration", timeout=10.0)
        resp.raise_for_status()
        doc = resp.json()
        endpoints = {
            "authorize_url": doc["authorization_endpoint"],
            "access_token_url": doc["token_endpoint"],
        }
        for optional in ("userinfo_endpoint", "jwks_uri"):
            if doc.get(optional):
                endpoints[optional] = doc[optional]
    except (KeyError, ValueError) as exc:
        raise ValueError(f"cannot read OIDC discovery: {exc}") from exc
    except Exception as exc:
        raise ValueError(f"cannot reach OIDC issuer: {exc}") from exc
    oauth = current_app.extensions.get("oauth")
    if oauth is None:
        oauth = OAuth()
        oauth.init_app(current_app)
        current_app.extensions["oauth"] = oauth
    return oauth.register(
        "oidc",
        overwrite=True,
        client_id=get_setting("oidc_client_id"),
        client_secret=get_setting("oidc_client_secret"),
        client_kwargs={
            "scope": "openid email profile",
            "code_challenge_method": "S256",
        },
        **endpoints,
    )


def _configured_groups(kind: str) -> set[str]:
    from .settings import get_setting

    raw = get_setting(f"oidc_{kind}_groups")
    return {g.strip() for g in raw.split(",") if g.strip()}


def role_for_groups(groups) -> str:
    """Map IdP group membership to a role. No match means viewer."""
    membership = set(groups or [])
    if membership & _configured_groups("admin"):
        return "admin"
    if membership & _configured_groups("operator"):
        return "operator"
    return "viewer"


def provision_oidc_user(userinfo: dict, issuer: str) -> User:
    """Find-or-create a user from OIDC claims, keyed on (issuer, sub).

    Never merges into a same-named local account: on username collision a
    short-sub suffix disambiguates. Group mapping applies at each login
    and wins over manual role edits.
    """
    sub = userinfo.get("sub")
    if not sub:
        raise ValueError("OIDC claims carry no subject")
    session = get_session()
    user = session.query(User).filter_by(oidc_issuer=issuer, oidc_sub=sub).first()
    if user is None:
        base = userinfo.get("preferred_username") or userinfo.get("email") or sub
        if not base:
            raise ValueError("OIDC claims carry no usable username")
        username = base[:64]
        if session.query(User).filter_by(username=username).first() is not None:
            username = f"{base[:55]}#{str(sub)[:8]}"
        user = User(
            username=username,
            password_hash=None,
            role="viewer",
            oidc_issuer=issuer,
            oidc_sub=sub,
        )
        session.add(user)
    from .settings import get_setting

    claim = get_setting("oidc_groups_claim") or "groups"
    user.role = role_for_groups(userinfo.get(claim))
    session.commit()
    return user


RATE_LIMIT = 10
RATE_WINDOW = 60


def _redis_client_or_none():
    """Redis for the shared login budget; None when unreachable."""
    try:
        import redis
        from flask import current_app, has_app_context

        if not has_app_context():
            return None
        return redis.from_url(
            current_app.config["REDIS_URL"],
            socket_connect_timeout=2,
            socket_timeout=5,
        )
    except Exception:  # noqa: BLE001 — fall back to process memory below
        return None


def _memory_limited(key: str, limit: int, window: int) -> bool:
    now = time.monotonic()
    hits = [t for t in _attempts.get(key, []) if now - t < window]
    if len(hits) >= limit:
        _attempts[key] = hits
        return True
    hits.append(now)
    _attempts[key] = hits
    return False


def _rate_limited(
    ip: str, username: str = "", limit: int = RATE_LIMIT, window: int = RATE_WINDOW
) -> bool:
    # One budget shared by every gunicorn worker, keyed on username+IP.
    key = f"{ip}:{(username or '').strip().lower()}"
    client = _redis_client_or_none()
    if client is not None:
        try:
            count = client.incr(f"login-attempts:{key}")
            if count == 1:
                client.expire(f"login-attempts:{key}", window)
            return count > limit
        except Exception:
            # Redis down: fall back to process memory below.
            logger.debug("login budget Redis unreachable; using memory", exc_info=True)
    return _memory_limited(key, limit, window)


def _rate_reset(ip: str, username: str = "") -> None:
    key = f"{ip}:{(username or '').strip().lower()}"
    _attempts.pop(key, None)
    client = _redis_client_or_none()
    if client is not None:
        try:
            client.delete(f"login-attempts:{key}")
        except Exception:
            # Best effort only; the memory entry is already cleared.
            logger.debug("login budget reset missed Redis", exc_info=True)


@login_manager.user_loader
def load_user(user_id: str):
    return get_session().get(User, int(user_id))


def seed_admin(username: str = "admin", password: str | None = None) -> bool:
    """Create the initial admin only when the users table is empty."""
    import os

    session = get_session()
    if session.query(User).count() > 0:
        return False
    if password is None and (
        os.environ.get("OVERSTATE_ENV") == "prod" or os.environ.get("TLS_CERT")
    ):
        raise RuntimeError(
            "refusing to invent an admin password in production: "
            "set ADMIN_PASSWORD and restart"
        )
    import secrets

    password = password or secrets.token_urlsafe(16)
    session.add(User(username=username, password_hash=_ph.hash(password), role="admin"))
    session.commit()
    print(f"seeded admin '{username}' with password: {password}")
    return True


class LoginForm(FlaskForm):
    username = StringField(validators=[DataRequired()])
    password = PasswordField(validators=[DataRequired()])


def _safe_next(value: str | None) -> str | None:
    """A post-login target is only honored when it is a same-origin path."""
    if not value or not value.startswith("/") or value.startswith("//"):
        return None
    if value.startswith("/\\") or "\\" in value:
        return None
    return value


@bp.route("/login", methods=["GET", "POST"])
def login():
    form = LoginForm()
    pending_next = request.args.get("next", "")
    if form.validate_on_submit():
        ip = request.remote_addr or "unknown"
        if _rate_limited(ip, form.username.data or ""):
            flash("Too many attempts. Try again later.", "error")
            return (
                render_template(
                    "login.html", form=form, sso=oidc_enabled(), next=pending_next
                ),
                429,
            )
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
            _rate_reset(request.remote_addr or "unknown", form.username.data or "")
            login_user(user)
            target = _safe_next(
                request.form.get("next", "") or request.args.get("next", "")
            )
            return redirect(target or url_for("dashboard.index"))
        flash("Invalid credentials.", "error")
    return render_template(
        "login.html", form=form, sso=oidc_enabled(), next=pending_next
    )


def oidc_redirect_uri() -> str:
    """Pinned callback URL: explicit redirect URI or PUBLIC_URL wins over
    the request host so a spoofed X-Forwarded-Host cannot mint one."""
    from flask import current_app

    pinned = current_app.config.get("OIDC_REDIRECT_URI") or ""
    if pinned:
        return pinned
    base = (current_app.config.get("PUBLIC_URL") or "").rstrip("/")
    if base:
        return f"{base}{url_for('auth.oidc_callback')}"
    return url_for("auth.oidc_callback", _external=True)


@bp.route("/login/oidc")
def oidc_login():
    if not oidc_enabled():
        abort(404)
    try:
        client = _oauth_client()
    except ValueError as exc:
        logger.error("SSO misconfigured: %s", exc)
        flash("Single sign-on is misconfigured.", "error")
        return redirect(url_for("auth.login"))
    # The deep link rides the OIDC state param so the callback can
    # honor it without trusting a cookie or the request host.
    state = _safe_next(request.args.get("next", ""))
    if state:
        return client.authorize_redirect(oidc_redirect_uri(), state=state)
    return client.authorize_redirect(oidc_redirect_uri())


@bp.route("/login/oidc/callback")
def oidc_callback():
    if not oidc_enabled():
        abort(404)
    try:
        client = _oauth_client()
        token = client.authorize_access_token()
        userinfo = token.get("userinfo") or client.userinfo(token=token)
    except Exception as exc:  # noqa: BLE001 — provider/network failures
        logger.error("SSO login failed: %s", exc)
        flash("SSO login failed.", "error")
        return redirect(url_for("auth.login"))
    from .settings import get_setting

    try:
        user = provision_oidc_user(
            dict(userinfo),
            get_setting("oidc_issuer").rstrip("/"),
        )
    except ValueError as exc:
        logger.error("SSO login failed: %s", exc)
        flash("SSO login failed.", "error")
        return redirect(url_for("auth.login"))
    login_user(user)
    target = _safe_next(request.args.get("state", ""))
    return redirect(target or url_for("dashboard.index"))


@bp.route("/logout", methods=["POST"])
@login_required
def logout():
    logout_user()
    return redirect(url_for("auth.login"))
