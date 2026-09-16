"""Overstate app factory."""

import os

from flask import Flask
from flask_wtf import CSRFProtect
from werkzeug.middleware.proxy_fix import ProxyFix

from . import (
    audit,
    auth,
    dashboard,
    events,
    files,
    groups,
    jobs,
    keys,
    masterconfig,
    mine,
    minions,
    pillar,
    reactor,
    schedules,
    settings,
    states,
    users,
)
from .config import Config
from .db import close_session, init_db
from .salt_client import SaltClient

csrf = CSRFProtect()


PLACEHOLDER_SECRET_KEYS = (None, "", "dev-only-change-me", "change-me")


def create_app(config: type[Config] = Config) -> Flask:
    app = Flask(__name__)
    app.config.from_object(config)
    app.config.setdefault("SECRET_KEY", config.SECRET_KEY)
    # Placeholders are dev-only; production must set a real SECRET_KEY.
    if not app.config.get("TESTING") and app.config.get("SECRET_KEY") in (
        PLACEHOLDER_SECRET_KEYS
    ):
        raise RuntimeError("SECRET_KEY is a placeholder; set a real value")
    # Cookie flags: HttpOnly + Lax always; Secure only behind TLS so
    # local HTTP compose logins keep working.
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    app.config["SESSION_COOKIE_SECURE"] = os.environ.get(
        "OVERSTATE_SECURE_COOKIES"
    ) == "1" or bool(os.environ.get("TLS_CERT"))
    # Behind Caddy the app sees the internal address (overstate-app:8000);
    # trust the forwarded host/port/scheme only there so direct gunicorn
    # access cannot spoof X-Forwarded-Host into OIDC redirects.
    if os.environ.get("TRUST_PROXY") == "1":
        app.wsgi_app = ProxyFix(  # type: ignore[method-assign]
            app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_port=1
        )

    init_db(config.SQLALCHEMY_DATABASE_URI)
    app.teardown_appcontext(close_session)

    csrf.init_app(app)
    auth.login_manager.init_app(app)
    app.extensions["salt_client"] = SaltClient(
        config.SALT_API_URL,
        config.SALT_EAUTH_USER,
        config.SALT_EAUTH_PASSWORD,
        config.SALT_EAUTH_TYPE,
        verify=config.SALT_API_VERIFY,
    )

    app.register_blueprint(auth.bp)
    app.register_blueprint(dashboard.bp)
    app.register_blueprint(keys.bp)
    app.register_blueprint(masterconfig.bp)
    app.register_blueprint(minions.bp)
    app.register_blueprint(mine.bp)
    app.register_blueprint(groups.bp)
    app.register_blueprint(pillar.bp)
    app.register_blueprint(reactor.bp)
    app.register_blueprint(jobs.bp)
    app.register_blueprint(settings.bp)
    app.register_blueprint(states.bp)
    app.register_blueprint(schedules.bp)
    app.register_blueprint(events.bp)
    app.register_blueprint(audit.bp)
    app.register_blueprint(users.bp)
    app.register_blueprint(files.bp)

    @app.after_request
    def _security_headers(resp):
        # Same-origin scripts only (HTMX/Alpine are vendored); inline
        # scripts stay working via 'unsafe-inline' for this pass.
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("Referrer-Policy", "same-origin")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self' 'unsafe-inline'; "
            "style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
            "connect-src 'self'",
        )
        return resp

    @app.context_processor
    def _theme():
        from .settings import get_setting

        try:
            theme = get_setting("theme")
        except Exception:  # noqa: BLE001 — DB may not exist yet
            theme = "wireframe"
        allowed = ("light", "dark", "wireframe")
        return {"app_theme": theme if theme in allowed else "wireframe"}

    return app
