"""Overstate app factory."""

from flask import Flask
from flask_wtf import CSRFProtect

from . import auth, audit, dashboard, events, files, groups, jobs, keys, minions, pillar, schedules, settings, states, users
from .config import Config
from .db import close_session, init_db
from .salt_client import SaltClient

csrf = CSRFProtect()


def create_app(config: type[Config] = Config) -> Flask:
    app = Flask(__name__)
    app.config.from_object(config)
    app.config.setdefault("SECRET_KEY", config.SECRET_KEY)

    init_db(config.SQLALCHEMY_DATABASE_URI)
    app.teardown_appcontext(close_session)

    csrf.init_app(app)
    auth.login_manager.init_app(app)
    app.extensions["salt_client"] = SaltClient(
        config.SALT_API_URL, config.SALT_EAUTH_USER, config.SALT_EAUTH_PASSWORD,
        config.SALT_EAUTH_TYPE, verify=config.SALT_API_VERIFY,
    )

    app.register_blueprint(auth.bp)
    app.register_blueprint(dashboard.bp)
    app.register_blueprint(keys.bp)
    app.register_blueprint(minions.bp)
    app.register_blueprint(groups.bp)
    app.register_blueprint(pillar.bp)
    app.register_blueprint(jobs.bp)
    app.register_blueprint(settings.bp)
    app.register_blueprint(states.bp)
    app.register_blueprint(schedules.bp)
    app.register_blueprint(events.bp)
    app.register_blueprint(audit.bp)
    app.register_blueprint(users.bp)
    app.register_blueprint(files.bp)

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
