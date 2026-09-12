"""Group tests: CRUD, resolution, group targeting."""

import httpx
import pytest

from overstate_ui import create_app
from overstate_ui.auth import seed_admin
from overstate_ui.config import TestConfig
from overstate_ui.db import create_all, get_session, init_db
from overstate_ui.models import Minion, MinionGroup
from overstate_ui.salt_client import SaltApiError, SaltClient


def fake_transport() -> httpx.MockTransport:
    import json

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/login":
            return httpx.Response(200, json={
                "return": [{"token": "tok", "expire": 99999}]})
        body = json.loads(request.content or b"{}")
        if body.get("fun") == "key.list_all":
            return httpx.Response(200, json={
                "return": [{"data": {"return": {
                    "minions": ["web-01", "web-02", "db-01"],
                    "minions_pre": []}}}]})
        if body.get("fun") == "manage.status":
            return httpx.Response(200, json={
                "return": [{"up": [], "down": []}]})
        return httpx.Response(200, json={"return": [{}]})

    return httpx.MockTransport(handler)


@pytest.fixture()
def app():
    init_db("sqlite://")
    app = create_app(TestConfig)
    app.config["WTF_CSRF_ENABLED"] = False
    app.extensions["salt_client"] = SaltClient(
        "https://salt:8000", "u", "p", transport=fake_transport())
    with app.app_context():
        create_all()
        seed_admin(password="pw")
        for mid in ("web-01", "web-02", "db-01"):
            get_session().add(Minion(id=mid, grains={}, conformity={}))
        get_session().commit()
    return app


@pytest.fixture()
def admin(app):
    client = app.test_client()
    client.post("/login", data={"username": "admin", "password": "pw"})
    return client


def group_named(name):
    return get_session().query(MinionGroup).filter_by(name=name).first()


def test_create_rename_edit_delete(admin, app):
    rv = admin.post("/minions/groups",
                    data={"name": "web", "members": "web-01, web-02 web-01"},
                    follow_redirects=True)
    assert "saved with 2 members" in rv.data.decode()
    with app.app_context():
        assert group_named("web").members == ["web-01", "web-02"]
        gid = group_named("web").id
    rv = admin.post(f"/minions/groups/{gid}/rename", data={"name": "web2"},
                    follow_redirects=True)
    assert "renamed to" in rv.data.decode()
    rv = admin.post(f"/minions/groups/{gid}/members",
                    data={"members": "web-01\ndb-01"},
                    follow_redirects=True)
    assert "now has 2 members" in rv.data.decode()
    with app.app_context():
        assert group_named("web2").members == ["web-01", "db-01"]
    rv = admin.post(f"/minions/groups/{gid}/delete", follow_redirects=True)
    assert "deleted" in rv.data.decode()
    with app.app_context():
        assert group_named("web2") is None


def test_create_validates(admin):
    rv = admin.post("/minions/groups", data={"name": "", "members": "m1"},
                    follow_redirects=True)
    assert "needs a name" in rv.data.decode()
    admin.post("/minions/groups", data={"name": "dup", "members": ""})
    rv = admin.post("/minions/groups", data={"name": "dup", "members": ""},
                    follow_redirects=True)
    assert "already exists" in rv.data.decode()


def test_groups_forbidden_for_viewer(app):
    from overstate_ui import auth as authmod
    from overstate_ui.models import User

    with app.app_context():
        get_session().add(User(username="vwr",
                               password_hash=authmod._ph.hash("vpw"),
                               role="viewer"))
        get_session().commit()
    client = app.test_client()
    client.post("/login", data={"username": "vwr", "password": "vpw"})
    assert client.post("/minions/groups").status_code == 403


def test_group_target_fires_list_job(app, admin, monkeypatch):
    with app.app_context():
        get_session().add(MinionGroup(name="web",
                                      members=["web-01", "ghost-99"]))
        get_session().commit()
    monkeypatch.setattr(app.extensions["salt_client"], "local",
                        lambda *a, **k: [{"jid": "j9"}])
    rv = admin.post("/jobs/run", data={
        "tgt": "web", "tgt_type": "group", "fun": "test.ping",
        "mode": "async", "via": "local"})
    assert rv.status_code == 302
    with app.app_context():
        from overstate_ui.models import Job
        job = get_session().get(Job, "j9")
        assert job is not None and job.tgt_type == "list"
        assert job.tgt == "web-01"  # stale ghost-99 resolved out


def test_resolve_group_target_errors(app):
    from overstate_ui.jobs import resolve_group_target

    with app.app_context():
        with pytest.raises(SaltApiError):
            resolve_group_target("missing")
        get_session().add(MinionGroup(name="empty", members=["ghost"]))
        get_session().commit()
        with pytest.raises(SaltApiError):
            resolve_group_target("empty")
    with app.app_context():
        get_session().add(MinionGroup(name="mix",
                                      members=["web-01", "ghost"]))
        get_session().commit()
        targets, stale = resolve_group_target("mix")
        assert targets == ["web-01"] and stale == 1


def test_batch_resolves_group(app):
    from overstate_ui.jobs import resolve_batch_roster

    with app.app_context():
        get_session().add(MinionGroup(name="web", members=["web-02"]))
        get_session().commit()
        assert resolve_batch_roster("web", "group") == ["web-02"]
        assert resolve_batch_roster("missing", "group") == []
