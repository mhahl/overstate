"""Grants dialog, role cache, local groups, mappings (PR6).

Scoped mode: the Users page edits grants through a dialog, the role
column is a recomputed cache, local groups carry grants, and IdP
mappings are edited on Settings. Forged writes 403.
"""

import pytest

from overstate_ui import auth as authmod
from overstate_ui import create_app
from overstate_ui.auth import seed_admin
from overstate_ui.config import TestConfig
from overstate_ui.db import create_all, get_session, init_db
from overstate_ui.models import (
    Grant,
    IdpRoleMapping,
    LocalGroup,
    Minion,
    MinionGroup,
    Setting,
    User,
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


def _user(username, role="viewer"):
    session = get_session()
    user = User(username=username, password_hash=authmod._ph.hash(PW), role=role)
    session.add(user)
    session.commit()
    return user.id


def test_grant_create_and_cache(env):
    with env.app.app_context():
        bob = _user("bob")
    login_as(env, "admin")
    rv = env.post(
        f"/users/{bob}/grants",
        data={"role": "team-operator", "scope_kind": "glob", "scope_value": "web-*"},
    )
    assert rv.status_code == 302
    with env.app.app_context():
        session = get_session()
        assert (
            session.query(Grant)
            .filter_by(subject_user_id=bob, role="team-operator")
            .count()
            == 1
        )
        assert session.get(User, bob).role == "scoped"
        row = (
            session.query(Grant).filter_by(subject_user_id=bob).one()
        )
        assert row.source == "manual"


def test_grant_duplicate_is_rejected(env):
    with env.app.app_context():
        bob = _user("bob")
    login_as(env, "admin")
    data = {"role": "team-operator", "scope_kind": "glob", "scope_value": "web-*"}
    assert env.post(f"/users/{bob}/grants", data=data).status_code == 302
    assert env.post(f"/users/{bob}/grants", data=data).status_code == 302
    with env.app.app_context():
        assert get_session().query(Grant).filter_by(subject_user_id=bob).count() == 1


def test_fleet_only_role_rejected_off_fleet(env):
    with env.app.app_context():
        bob = _user("bob")
    login_as(env, "admin")
    rv = env.post(
        f"/users/{bob}/grants",
        data={"role": "admin", "scope_kind": "glob", "scope_value": "web-*"},
    )
    assert rv.status_code == 302
    with env.app.app_context():
        assert get_session().query(Grant).filter_by(subject_user_id=bob).count() == 0


def test_forged_grant_post_403(env):
    with env.app.app_context():
        bob = _user("bob")
        mallory = _user("mallory")
        session = get_session()
        session.add(
            Grant(
                subject_kind="user",
                subject_user_id=mallory,
                subject_group_id=None,
                role="team-viewer",
                scope_kind="glob",
                scope_value="web-*",
                source="manual",
            )
        )
        session.commit()
    login_as(env, "mallory")
    rv = env.post(
        f"/users/{bob}/grants",
        data={"role": "team-operator", "scope_kind": "glob", "scope_value": "web-*"},
    )
    assert rv.status_code == 403
    with env.app.app_context():
        assert get_session().query(Grant).filter_by(subject_user_id=bob).count() == 0


def test_cannot_delete_own_last_fleet_admin(env):
    with env.app.app_context():
        admin = get_session().query(User).filter_by(username="admin").one().id
        grant = (
            get_session().query(Grant).filter_by(subject_user_id=admin).one().id
        )
    login_as(env, "admin")
    rv = env.post(f"/users/{admin}/grants/{grant}/delete")
    assert rv.status_code == 302
    with env.app.app_context():
        assert get_session().get(Grant, grant) is not None


def test_grant_delete_recomputes_cache(env):
    with env.app.app_context():
        bob = _user("bob")
        session = get_session()
        session.add(
            Grant(
                subject_kind="user",
                subject_user_id=bob,
                subject_group_id=None,
                role="team-viewer",
                scope_kind="glob",
                scope_value="web-*",
                source="manual",
            )
        )
        session.commit()
        grant = session.query(Grant).filter_by(subject_user_id=bob).one().id
    login_as(env, "admin")
    assert env.post(f"/users/{bob}/grants/{grant}/delete").status_code == 302
    with env.app.app_context():
        assert get_session().query(Grant).filter_by(subject_user_id=bob).count() == 0
        assert get_session().get(User, bob).role == "none"


def test_fleet_team_role_caches_scoped(env):
    with env.app.app_context():
        bob = _user("bob")
        session = get_session()
        session.add(
            Grant(
                subject_kind="user",
                subject_user_id=bob,
                subject_group_id=None,
                role="team-operator",
                scope_kind="fleet",
                scope_value="*",
                source="manual",
            )
        )
        session.commit()
        from overstate_ui.authz import refresh_role_cache

        assert refresh_role_cache(session.get(User, bob)) == "scoped"


def test_two_mappings_at_different_scopes_both_apply(env):
    from overstate_ui.authz import authorize

    with env.app.app_context():
        session = get_session()
        session.add_all(
            [
                IdpRoleMapping(
                    idp_group="g1",
                    role="team-viewer",
                    scope_kind="glob",
                    scope_value="web-*",
                    origin="manual",
                ),
                IdpRoleMapping(
                    idp_group="g2",
                    role="team-operator",
                    scope_kind="glob",
                    scope_value="db-*",
                    origin="manual",
                ),
            ]
        )
        bob = _user("bob")
        from overstate_ui.models import UserIdpGroup

        session.add_all(
            [
                UserIdpGroup(user_id=bob, group_name="g1"),
                UserIdpGroup(user_id=bob, group_name="g2"),
            ]
        )
        session.commit()
        bob_row = session.get(User, bob)
        assert authorize(bob_row, "job.read", minion="web-01")
        assert authorize(bob_row, "job.run.read", minion="db-01")
        assert not authorize(bob_row, "job.run.read", minion="web-01")


def test_set_role_writes_fleet_grant(env):
    with env.app.app_context():
        bob = _user("bob")
    login_as(env, "admin")
    rv = env.post(f"/users/{bob}/role", data={"role": "operator"})
    assert rv.status_code == 302
    with env.app.app_context():
        session = get_session()
        rows = session.query(Grant).filter_by(subject_user_id=bob).all()
        assert [(r.role, r.scope_kind) for r in rows] == [("operator", "fleet")]
        assert session.get(User, bob).role == "operator"


def test_local_group_membership_refreshes_cache(env):
    with env.app.app_context():
        bob = _user("bob")
    login_as(env, "admin")
    assert env.post("/users/local-groups", data={"name": "webops"}).status_code == 302
    with env.app.app_context():
        gid = get_session().query(LocalGroup).filter_by(name="webops").one().id
    login_as(env, "admin")
    assert (
        env.post(f"/users/local-groups/{gid}/grants",
                 data={"role": "team-viewer", "scope_kind": "glob", "scope_value": "web-*"}).status_code
        == 302
    )
    with env.app.app_context():
        bob = get_session().query(User).filter_by(username="bob").one().id
    login_as(env, "admin")
    assert (
        env.post(f"/users/local-groups/{gid}/members", data={"user_ids": str(bob)}).status_code
        == 302
    )
    with env.app.app_context():
        assert get_session().get(User, bob).role == "scoped"


def test_group_freeze_blocks_rename_and_delete(env):
    with env.app.app_context():
        bob = _user("bob")
        session = get_session()
        session.add(
            Grant(
                subject_kind="user",
                subject_user_id=bob,
                subject_group_id=None,
                role="team-operator",
                scope_kind="group",
                scope_value="7",
                source="manual",
            )
        )
        session.commit()
    login_as(env, "admin")
    # grant.admin may rename: the id scope_value keeps resolving.
    assert env.post("/groups/7/rename", data={"name": "web2"}).status_code == 302
    with env.app.app_context():
        from overstate_ui.authz import scope_group_members

        assert scope_group_members(7) == {"web-01"}
    # Delete of a referenced group is rejected, even for grant.admin.
    assert env.post("/groups/7/delete").status_code == 403
    with env.app.app_context():
        assert get_session().get(MinionGroup, 7) is not None


def test_mapping_crud_refreshes_cache(env):
    with env.app.app_context():
        bob = _user("sso-bob", role="none")
        from overstate_ui.models import UserIdpGroup

        get_session().add(UserIdpGroup(user_id=bob, group_name="payments"))
        get_session().commit()
    login_as(env, "admin")
    rv = env.post(
        "/settings/idp-mappings",
        data={
            "idp_group": "payments",
            "role": "team-viewer",
            "scope_kind": "group",
            "scope_value": "7",
            "scope_value_group": "7",
        },
    )
    assert rv.status_code == 302
    with env.app.app_context():
        assert get_session().get(User, bob).role == "scoped"
        mid = get_session().query(IdpRoleMapping).one().id
    login_as(env, "admin")
    assert env.post(f"/settings/idp-mappings/{mid}/delete").status_code == 302
    with env.app.app_context():
        assert get_session().get(User, bob).role == "none"


def test_role_filter_accepts_scoped_and_none(env):
    login_as(env, "admin")
    assert env.get("/users/?role=scoped").status_code == 200
    assert env.get("/users/?role=none").status_code == 200
    env.get("/users/?role=bogus")
    assert env.get("/users/").status_code == 200


def test_settings_read_shows_display_keys_only(env):
    with env.app.app_context():
        bob = _user("bob")
        session = get_session()
        session.add(
            Grant(
                subject_kind="user",
                subject_user_id=bob,
                subject_group_id=None,
                role="viewer",
                scope_kind="fleet",
                scope_value="*",
                source="manual",
            )
        )
        session.commit()
    login_as(env, "bob")
    html = env.get("/settings/").data.decode()
    assert "Master hostname" in html
    assert "OIDC issuer" not in html
    assert "IdP group mappings" not in html
