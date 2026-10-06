"""Scoped RBAC backfill and flag resolver (PR3).

PR5 activated enforcement: ENFORCEMENT_COMPLETE is true in production
source, so a ``rbac_mode=scoped`` settings row (or RBAC_MODE env) takes
effect. Rollback is the flag, not the constant: clearing it to legacy
restores the ladder.
"""

import pytest

from overstate_ui import auth as authmod
from overstate_ui import authz, create_app
from overstate_ui.authz import backfill_rbac, rbac_flag, rbac_mode
from overstate_ui.config import TestConfig
from overstate_ui.db import create_all, get_session, init_db
from overstate_ui.models import Grant, IdpRoleMapping, Setting, User

PW = "test-password"


@pytest.fixture()
def app():
    init_db("sqlite://")
    app = create_app(TestConfig)
    app.config["WTF_CSRF_ENABLED"] = False
    with app.app_context():
        create_all()
        yield app


def _user(username: str, role: str) -> User:
    session = get_session()
    user = User(username=username, password_hash=authmod._ph.hash(PW), role=role)
    session.add(user)
    session.commit()
    return user


def test_backfill_grants_and_mappings(app):
    with app.app_context():
        _user("viewer1", "viewer")
        _user("op1", "operator")
        counts = backfill_rbac(
            get_session(), admin_groups="idp-admins, other", operator_groups="idp-ops"
        )
        assert counts == {"grants": 2, "mappings": 3}
        get_session().commit()
        grants = get_session().query(Grant).all()
        assert {(g.role, g.scope_kind, g.scope_value, g.source) for g in grants} == {
            ("viewer", "fleet", "*", "backfill"),
            ("operator", "fleet", "*", "backfill"),
        }
        mappings = get_session().query(IdpRoleMapping).all()
        assert {(m.idp_group, m.role, m.origin) for m in mappings} == {
            ("idp-admins", "admin", "backfill"),
            ("other", "admin", "backfill"),
            ("idp-ops", "operator", "backfill"),
        }
        # Idempotent: a second run inserts nothing.
        assert backfill_rbac(get_session()) == {"grants": 0, "mappings": 0}
        get_session().commit()
        assert get_session().query(Grant).count() == 2
        # No settings rows were inserted.
        assert get_session().query(Setting).count() == 0


def test_backfill_skips_unknown_roles(app):
    with app.app_context():
        _user("odd", "security-reader")
        counts = backfill_rbac(get_session())
        assert counts == {"grants": 0, "mappings": 0}
        get_session().commit()
        assert get_session().query(Grant).count() == 0


def test_seed_admin_writes_fleet_admin_grant(app):
    with app.app_context():
        assert authmod.seed_admin(password=PW) is True
        admin = get_session().query(User).filter_by(username="admin").one()
        grant = (
            get_session()
            .query(Grant)
            .filter_by(subject_user_id=admin.id, role="admin")
            .one()
        )
        assert (grant.scope_kind, grant.scope_value, grant.source) == (
            "fleet",
            "*",
            "backfill",
        )
        # A database that already has users is not re-seeded.
        assert authmod.seed_admin(password=PW) is False


def test_flag_resolver(app, monkeypatch):
    with app.app_context():
        # Defaults with no row and no env.
        monkeypatch.delenv("RBAC_MODE", raising=False)
        monkeypatch.delenv("RBAC_ROLE_FALLBACK", raising=False)
        assert rbac_flag("rbac_mode", "RBAC_MODE", "legacy") == "legacy"
        assert rbac_flag("rbac_role_fallback", "RBAC_ROLE_FALLBACK", "on") == "on"

        # Non-empty env applies, empty string counts as absent.
        monkeypatch.setenv("RBAC_MODE", "scoped")
        assert rbac_flag("rbac_mode", "RBAC_MODE", "legacy") == "scoped"
        monkeypatch.setenv("RBAC_MODE", "  ")
        assert rbac_flag("rbac_mode", "RBAC_MODE", "legacy") == "legacy"

        # DB row wins over env, and no row is ever inserted.
        get_session().add(Setting(key="rbac_mode", value="scoped"))
        get_session().commit()
        monkeypatch.setenv("RBAC_MODE", "legacy")
        assert rbac_flag("rbac_mode", "RBAC_MODE", "legacy") == "scoped"
        get_session().query(Setting).filter_by(key="rbac_role_fallback").delete()
        get_session().commit()
        assert get_session().query(Setting).count() == 1


def test_enforcement_constant_gates_scoped_row(app, monkeypatch):
    with app.app_context():
        get_session().add(Setting(key="rbac_mode", value="scoped"))
        get_session().commit()
        # Production source leaves the constant true: the row applies.
        assert authz.ENFORCEMENT_COMPLETE is True
        assert rbac_mode() == "scoped"
        monkeypatch.setattr(authz, "ENFORCEMENT_COMPLETE", False)
        assert rbac_mode() == "legacy"
