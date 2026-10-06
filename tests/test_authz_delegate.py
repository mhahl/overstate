"""Delegated admin: team leads grant inside their scope (PR9).

A lead holding ``grant.delegate`` on a scope may create and delete
grants whose permission set and minion set sit inside it — never fleet
scopes, never ``grant.admin``, never their own grants, never
``source=backfill`` fleet ladder rows, and never ``secrets-reader``
without holding ``pillar.read`` + ``mine.read`` there. ``GET /users/``
for minion-scoped ``user.read`` is 200 and filtered, not 403.
"""

import pytest

from overstate_ui import auth as authmod
from overstate_ui import authz, create_app
from overstate_ui.auth import seed_admin
from overstate_ui.config import TestConfig
from overstate_ui.db import create_all, get_session, init_db
from overstate_ui.models import (
    AuditEvent,
    Grant,
    LocalGroup,
    LocalGroupMember,
    Minion,
    MinionGroup,
    Setting,
    User,
)

PW = "test-password"


def _grant(session, user_id, role, kind, value, source="manual"):
    session.add(
        Grant(
            subject_kind="user",
            subject_user_id=user_id,
            subject_group_id=None,
            role=role,
            scope_kind=kind,
            scope_value=value,
            source=source,
        )
    )


@pytest.fixture()
def env(monkeypatch):
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
                Minion(id="web-02", grains={"os": "Ubuntu"}, conformity={}),
                Minion(id="db-01", grains={"os": "Debian"}, conformity={}),
            ]
        )
        session.add(MinionGroup(id=7, name="web", members=["web-01", "web-02"]))
        lead = User(
            username="lead", password_hash=authmod._ph.hash(PW), role="scoped"
        )
        op = User(username="op", password_hash=authmod._ph.hash(PW), role="none")
        new = User(username="new", password_hash=authmod._ph.hash(PW), role="none")
        far = User(
            username="far", password_hash=authmod._ph.hash(PW), role="scoped"
        )
        session.add_all([lead, op, new, far])
        session.flush()
        _grant(session, lead.id, "team-lead", "glob", "web-*")
        _grant(session, op.id, "team-operator", "glob", "web-*")
        _grant(session, far.id, "team-viewer", "list", '["db-01"]')
        webteam = LocalGroup(name="webteam")
        fleeted = LocalGroup(name="fleeted")
        session.add_all([webteam, fleeted])
        session.flush()
        session.add(LocalGroupMember(group_id=webteam.id, user_id=op.id))
        session.add(
            Grant(
                subject_kind="local_group",
                subject_user_id=None,
                subject_group_id=webteam.id,
                role="team-operator",
                scope_kind="glob",
                scope_value="web-*",
                source="manual",
            )
        )
        session.add(
            Grant(
                subject_kind="local_group",
                subject_user_id=None,
                subject_group_id=fleeted.id,
                role="team-operator",
                scope_kind="fleet",
                scope_value="*",
                source="manual",
            )
        )
        session.commit()
        ids = {
            "lead": lead.id,
            "op": op.id,
            "new": new.id,
            "far": far.id,
            "webteam": webteam.id,
            "fleeted": fleeted.id,
        }
    monkeypatch.setattr(authz, "ENFORCEMENT_COMPLETE", True)
    client = app.test_client()
    client.app = app
    client.ids = ids
    return client


def login_as(client, username: str):
    client.post("/logout")
    return client.post("/login", data={"username": username, "password": PW})


def test_filtered_users_page(env):
    login_as(env, "lead")
    rv = env.get("/users/")
    assert rv.status_code == 200
    html = rv.data.decode()
    # In-scope users and groups are listed with their in-scope rows.
    assert "op" in html
    assert "webteam" in html
    assert "team-operator · glob:web-*" in html
    # The out-of-scope user and fleet-only surfaces are absent.
    assert "far" not in html
    assert "db-01" not in html
    assert "Danger zone" not in html
    assert "Service accounts" not in html
    assert "Fleet role" not in html
    # The lead's own grants show, without an edit button for them.
    assert "team-lead · glob:web-*" in html
    assert f"grants-user-{env.ids['lead']}" not in html


def test_viewer_without_user_read_is_403(env):
    login_as(env, "far")
    assert env.get("/users/").status_code == 403


def test_delegate_grant_inside_scope(env):
    login_as(env, "lead")
    rv = env.post(
        f"/users/{env.ids['new']}/grants",
        data={"role": "team-operator", "scope_kind": "glob", "scope_value": "web-*"},
    )
    assert rv.status_code == 302
    with env.app.app_context():
        row = (
            get_session()
            .query(Grant)
            .filter_by(subject_user_id=env.ids["new"], role="team-operator")
            .one()
        )
        assert row.source == "manual"
        assert get_session().get(User, env.ids["new"]).role == "scoped"


def test_delegate_cannot_grant_fleet(env):
    login_as(env, "lead")
    for role in ("operator", "team-operator"):
        rv = env.post(
            f"/users/{env.ids['new']}/grants",
            data={"role": role, "scope_kind": "fleet", "scope_value": "*"},
        )
        assert rv.status_code == 403
    with env.app.app_context():
        assert (
            get_session().query(Grant).filter_by(subject_user_id=env.ids["new"]).count()
            == 0
        )


def test_delegate_cannot_grant_outside_scope(env):
    login_as(env, "lead")
    rv = env.post(
        f"/users/{env.ids['new']}/grants",
        data={"role": "team-operator", "scope_kind": "glob", "scope_value": "db-*"},
    )
    assert rv.status_code == 403
    with env.app.app_context():
        assert (
            get_session().query(Grant).filter_by(subject_user_id=env.ids["new"]).count()
            == 0
        )


def test_delegate_cannot_self_grant(env):
    login_as(env, "lead")
    rv = env.post(
        f"/users/{env.ids['lead']}/grants",
        data={"role": "team-viewer", "scope_kind": "glob", "scope_value": "web-*"},
    )
    assert rv.status_code == 403
    with env.app.app_context():
        assert (
            get_session()
            .query(Grant)
            .filter_by(subject_user_id=env.ids["lead"], role="team-viewer")
            .count()
            == 0
        )


def test_delegate_cannot_delete_backfill_ladder(env):
    with env.app.app_context():
        gid = (
            get_session()
            .query(Grant)
            .filter_by(source="backfill", role="admin", scope_kind="fleet")
            .one()
            .id
        )
        admin_id = get_session().query(User).filter_by(username="admin").one().id
    login_as(env, "lead")
    rv = env.post(f"/users/{admin_id}/grants/{gid}/delete")
    assert rv.status_code == 403
    with env.app.app_context():
        assert get_session().get(Grant, gid) is not None


def test_delegate_can_delete_inside_scope(env):
    login_as(env, "lead")
    with env.app.app_context():
        gid = (
            get_session()
            .query(Grant)
            .filter_by(subject_user_id=env.ids["op"], role="team-operator")
            .one()
            .id
        )
    rv = env.post(f"/users/{env.ids['op']}/grants/{gid}/delete")
    assert rv.status_code == 302
    with env.app.app_context():
        assert get_session().get(Grant, gid) is None
        # op is still in webteam, which holds its own team-operator
        # grant: the cache recomputes to scoped, not none.
        assert get_session().get(User, env.ids["op"]).role == "scoped"


def test_secrets_reader_brake(env):
    login_as(env, "lead")
    rv = env.post(
        f"/users/{env.ids['new']}/grants",
        data={"role": "secrets-reader", "scope_kind": "glob", "scope_value": "web-*"},
    )
    assert rv.status_code == 403
    # An admin who hands the lead pillar+mine on that scope lifts the brake.
    login_as(env, "admin")
    rv = env.post(
        f"/users/{env.ids['lead']}/grants",
        data={"role": "secrets-reader", "scope_kind": "glob", "scope_value": "web-*"},
    )
    assert rv.status_code == 302
    login_as(env, "lead")
    rv = env.post(
        f"/users/{env.ids['new']}/grants",
        data={"role": "secrets-reader", "scope_kind": "glob", "scope_value": "web-*"},
    )
    assert rv.status_code == 302
    with env.app.app_context():
        assert (
            get_session()
            .query(Grant)
            .filter_by(subject_user_id=env.ids["new"], role="secrets-reader")
            .count()
            == 1
        )


def test_delegate_group_members_inside_scope(env):
    login_as(env, "lead")
    rv = env.post(
        f"/users/local-groups/{env.ids['webteam']}/members",
        data={"user_ids": [str(env.ids["op"]), str(env.ids["new"])]},
    )
    assert rv.status_code == 302
    with env.app.app_context():
        members = {
            row.user_id
            for row in get_session()
            .query(LocalGroupMember)
            .filter_by(group_id=env.ids["webteam"])
            .all()
        }
        assert members == {env.ids["op"], env.ids["new"]}


def test_delegate_group_members_fleet_grant_is_403(env):
    login_as(env, "lead")
    rv = env.post(
        f"/users/local-groups/{env.ids['fleeted']}/members",
        data={"user_ids": [str(env.ids["op"])]},
    )
    assert rv.status_code == 403
    with env.app.app_context():
        assert (
            get_session()
            .query(LocalGroupMember)
            .filter_by(group_id=env.ids["fleeted"])
            .count()
            == 0
        )


def test_delegate_group_grant_delete_outside_scope_is_403(env):
    login_as(env, "lead")
    with env.app.app_context():
        gid = (
            get_session()
            .query(Grant)
            .filter_by(subject_group_id=env.ids["fleeted"])
            .one()
            .id
        )
    rv = env.post(f"/users/local-groups/{env.ids['fleeted']}/grants/{gid}/delete")
    assert rv.status_code == 403
    with env.app.app_context():
        assert get_session().get(Grant, gid) is not None


def test_lead_audit_detail_redacted(env):
    with env.app.app_context():
        session = get_session()
        session.add(
            AuditEvent(
                user="lead",
                action="job-constrained",
                outcome="allow",
                permission="job.run.read",
                detail="db-01",
            )
        )
        session.commit()
    login_as(env, "lead")
    rv = env.get("/audit/")
    assert rv.status_code == 200
    html = rv.data.decode()
    assert "job-constrained" in html
    assert "db-01" not in html


def test_delegate_writes_are_audited(env):
    login_as(env, "lead")
    env.post(
        f"/users/{env.ids['new']}/grants",
        data={"role": "team-viewer", "scope_kind": "glob", "scope_value": "web-*"},
    )
    env.post(
        f"/users/{env.ids['new']}/grants",
        data={"role": "team-operator", "scope_kind": "glob", "scope_value": "db-*"},
    )
    with env.app.app_context():
        rows = (
            get_session()
            .query(AuditEvent)
            .filter(AuditEvent.action.in_(["grant-create", "deny"]))
            .all()
        )
        by_action = {row.action: row for row in rows}
        assert by_action["grant-create"].permission == "grant.delegate"
        assert by_action["grant-create"].user == "lead"
        assert by_action["deny"].outcome == "deny"
        assert by_action["deny"].user == "lead"


def test_groups_page_lead_sees_manageable_only(env):
    login_as(env, "lead")
    rv = env.get("/users/groups")
    assert rv.status_code == 200
    html = rv.data.decode()
    assert "webteam" in html
    assert "fleeted" not in html
    assert "IdP groups" not in html


def test_groups_page_admin_sees_local_and_idp(env):
    from overstate_ui.models import IdpRoleMapping, UserIdpGroup

    with env.app.app_context():
        session = get_session()
        op = session.query(User).filter_by(username="op").one()
        session.add(UserIdpGroup(user_id=op.id, group_name="oncall"))
        session.add(
            IdpRoleMapping(
                idp_group="oncall",
                role="team-viewer",
                scope_kind="glob",
                scope_value="web-*",
                origin="manual",
            )
        )
        session.commit()
    login_as(env, "admin")
    html = env.get("/users/groups").data.decode()
    assert "webteam" in html and "fleeted" in html
    assert "oncall" in html and "op" in html


def test_local_group_create_redirects_to_groups(env):
    login_as(env, "admin")
    rv = env.post("/users/local-groups", data={"name": "fresh"})
    assert rv.status_code == 302
    assert rv.headers["Location"].endswith("/users/groups")
    html = env.get("/users/groups").data.decode()
    assert "fresh" in html
