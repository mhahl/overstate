"""OIDC scoped reconciliation and the flag control (PR7).

Scoped mode: the groups claim is membership, unmatched users cache to
none, manual grants survive login, and entering scoped mode rebuilds
backfill ladder mappings. Legacy provisioning still overwrites the
role column.
"""

import pytest

from overstate_ui import create_app
from overstate_ui.auth import provision_oidc_user, role_for_groups, seed_admin
from overstate_ui.config import TestConfig
from overstate_ui.db import create_all, get_session, init_db
from overstate_ui.models import (
    Grant,
    IdpRoleMapping,
    Minion,
    MinionGroup,
    Setting,
    UserIdpGroup,
)

PW = "test-password"


@pytest.fixture()
def env():
    init_db("sqlite://")
    app = create_app(TestConfig)
    app.config["WTF_CSRF_ENABLED"] = False
    with app.app_context():
        create_all()
        seed_admin(password=PW)
        session = get_session()
        session.add_all(
            [
                Setting(key="rbac_mode", value="scoped"),
                Setting(key="rbac_role_fallback", value="off"),
            ]
        )
        session.add_all(
            [
                Minion(id="web-01", grains={"os": "Ubuntu"}, conformity={}),
                Minion(id="db-01", grains={"os": "Debian"}, conformity={}),
            ]
        )
        session.add(MinionGroup(id=7, name="web", members=["web-01"]))
        session.commit()
    client = app.test_client()
    client.app = app
    return client


def login_as(client, username: str):
    client.post("/logout")
    return client.post("/login", data={"username": username, "password": PW})


def _userinfo(sub, groups):
    return {"sub": sub, "preferred_username": f"sso-{sub}", "groups": groups}


def test_idp_mapping_login_and_revoke(env):
    from overstate_ui.authz import authorize

    with env.app.app_context():
        session = get_session()
        session.add(
            IdpRoleMapping(
                idp_group="payments",
                role="team-viewer",
                scope_kind="group",
                scope_value="7",
                origin="manual",
            )
        )
        session.commit()
        user = provision_oidc_user(_userinfo("u1", ["payments"]), "https://idp.example")
        uid = user.id
        assert user.role == "scoped"
        assert authorize(user, "job.read", minion="web-01")
        assert not authorize(user, "job.read", minion="db-01")
        # Second login without the group: access drops, no viewer grant.
        user = provision_oidc_user(_userinfo("u1", []), "https://idp.example")
        assert user.id == uid
        assert user.role == "none"
        assert not authorize(user, "job.read", minion="web-01")
        assert session.query(Grant).filter_by(subject_user_id=uid).count() == 0


def test_manual_grant_survives_login(env):
    from overstate_ui.authz import authorize

    with env.app.app_context():
        session = get_session()
        user = provision_oidc_user(_userinfo("u2", []), "https://idp.example")
        uid = user.id
        assert user.role == "none"
        session.add(
            Grant(
                subject_kind="user",
                subject_user_id=uid,
                subject_group_id=None,
                role="team-viewer",
                scope_kind="glob",
                scope_value="web-*",
                source="manual",
            )
        )
        session.commit()
        user = provision_oidc_user(_userinfo("u2", []), "https://idp.example")
        assert authorize(user, "job.read", minion="web-01")
        assert user.role == "scoped"


def test_string_claim_stores_one_group(env):
    with env.app.app_context():
        user = provision_oidc_user(
            {"sub": "u3", "preferred_username": "sso-u3", "groups": "payments"},
            "https://idp.example",
        )
        names = [
            r.group_name
            for r in get_session().query(UserIdpGroup).filter_by(user_id=user.id).all()
        ]
        assert names == ["payments"]


def test_legacy_provision_still_overwrites_viewer(env):
    with env.app.app_context():
        session = get_session()
        session.query(Setting).filter_by(key="rbac_mode").delete()
        session.commit()
        user = provision_oidc_user(_userinfo("u4", ["whatever"]), "https://idp.example")
        assert user.role == "viewer"
        # role_for_groups is still if/else, not a union.
        assert role_for_groups(["a", "b"]) == "viewer"


def test_entering_scoped_rebuilds_backfill_mappings(env):
    with env.app.app_context():
        session = get_session()
        session.query(Setting).filter_by(key="rbac_mode").delete()
        session.add_all(
            [
                Setting(key="oidc_admin_groups", value="sre-leads, ops"),
                Setting(key="oidc_operator_groups", value="devs"),
                IdpRoleMapping(
                    idp_group="stale",
                    role="admin",
                    scope_kind="fleet",
                    scope_value="*",
                    origin="backfill",
                ),
                IdpRoleMapping(
                    idp_group="handmade",
                    role="team-viewer",
                    scope_kind="glob",
                    scope_value="web-*",
                    origin="manual",
                ),
            ]
        )
        session.commit()
    login_as(env, "admin")
    rv = env.post(
        "/settings/",
        data={
            "oidc_admin_groups": "sre-leads, ops",
            "oidc_operator_groups": "devs",
            "rbac_mode": "scoped",
            "rbac_role_fallback": "on",
        },
    )
    assert rv.status_code == 302
    with env.app.app_context():
        session = get_session()
        groups = {r.idp_group: r for r in session.query(IdpRoleMapping).all()}
        assert set(groups) == {"sre-leads", "ops", "devs", "handmade"}
        assert groups["sre-leads"].origin == "backfill"
        assert groups["sre-leads"].role == "admin"
        assert groups["devs"].role == "operator"
        assert groups["handmade"].origin == "manual"


def test_fallback_banner_and_mirror(env):
    login_as(env, "admin")
    html = env.get("/settings/access").data.decode()
    assert "Role fallback is on" not in html
    with env.app.app_context():
        session = get_session()
        session.query(Setting).filter_by(key="rbac_role_fallback").delete()
        session.add(Setting(key="rbac_role_fallback", value="on"))
        session.commit()
    html = env.get("/settings/access").data.decode()
    assert "Role fallback is on" in html


def test_entering_scoped_keeps_colliding_manual_row(env):
    with env.app.app_context():
        session = get_session()
        session.query(Setting).filter_by(key="rbac_mode").delete()
        session.add_all(
            [
                Setting(key="oidc_admin_groups", value="ops"),
                Setting(key="oidc_operator_groups", value=""),
                IdpRoleMapping(
                    idp_group="ops",
                    role="admin",
                    scope_kind="fleet",
                    scope_value="*",
                    origin="manual",
                ),
            ]
        )
        session.commit()
    login_as(env, "admin")
    rv = env.post(
        "/settings/",
        data={
            "oidc_admin_groups": "ops",
            "oidc_operator_groups": "",
            "rbac_mode": "scoped",
            "rbac_role_fallback": "on",
        },
    )
    assert rv.status_code == 302
    with env.app.app_context():
        session = get_session()
        assert session.get(Setting, "rbac_mode").value == "scoped"
        rows = (
            session.query(IdpRoleMapping)
            .filter_by(
                idp_group="ops",
                role="admin",
                scope_kind="fleet",
                scope_value="*",
            )
            .all()
        )
        assert len(rows) == 1
        assert rows[0].origin == "manual"


def test_legacy_provision_records_claim_groups(env):
    with env.app.app_context():
        session = get_session()
        session.query(Setting).filter_by(key="rbac_mode").delete()
        session.commit()
        user = provision_oidc_user(
            {"sub": "u9", "preferred_username": "sso-u9", "groups": ["g1", "g2"]},
            "https://idp.example",
        )
        assert user.role == "viewer"
        names = sorted(
            r.group_name
            for r in get_session().query(UserIdpGroup).filter_by(user_id=user.id).all()
        )
        assert names == ["g1", "g2"]
