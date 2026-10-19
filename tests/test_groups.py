"""Nodegroup tests: ConfigMap-backed CRUD, targeting, RBAC sync."""

import httpx
import pytest
import yaml

from overstate_ui import create_app
from overstate_ui import groups as groupsmod
from overstate_ui.auth import seed_admin
from overstate_ui.config import TestConfig
from overstate_ui.db import create_all, get_session, init_db
from overstate_ui.models import Minion, MinionGroup
from overstate_ui.salt_client import SaltApiError, SaltClient


def fake_transport() -> httpx.MockTransport:
    import json

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/login":
            return httpx.Response(
                200, json={"return": [{"token": "tok", "expire": 99999}]}
            )
        body = json.loads(request.content or b"{}")
        if body.get("fun") == "key.list_all":
            return httpx.Response(
                200,
                json={
                    "return": [
                        {
                            "data": {
                                "return": {
                                    "minions": ["web-01", "web-02", "db-01"],
                                    "minions_pre": [],
                                }
                            }
                        }
                    ]
                },
            )
        if body.get("fun") == "manage.status":
            return httpx.Response(200, json={"return": [{"up": [], "down": []}]})
        return httpx.Response(200, json={"return": [{}]})

    return httpx.MockTransport(handler)


class FakeConfigMapStore:
    """In-memory stand-in for the salt-master ConfigMap.

    Shared across K8sClient instantiations within one test, so a save
    followed by an index read sees the same data.
    """

    def __init__(self, initial: str | None = None):
        self.data = {"master.conf": initial or "nodegroups: {}\n"}
        self.revision = "1"

    def get_configmap(self, name):
        return {"data": dict(self.data), "resourceVersion": self.revision}

    def replace_configmap(self, name, data, revision):
        if revision != self.revision:
            raise groupsmod.K8sConflictError("config changed")
        self.data = data
        self.revision = str(int(self.revision) + 1)
        return self.revision

    def statefulset_rollout(self, name):
        # set_restart_needed() records the generation; a static,
        # converged value keeps the restart badge logic inert here.
        return {
            "generation": 1,
            "observedGeneration": 1,
            "replicas": 1,
            "readyReplicas": 1,
            "updatedReplicas": 1,
        }

    def nodegroups(self) -> dict:
        parsed = yaml.safe_load(self.data["master.conf"]) or {}
        return parsed.get("nodegroups", {})


@pytest.fixture()
def app():
    init_db("sqlite://")
    app = create_app(TestConfig)
    app.config["WTF_CSRF_ENABLED"] = False
    app.extensions["salt_client"] = SaltClient(
        "https://salt:8000", "u", "p", transport=fake_transport()
    )
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


@pytest.fixture()
def configmap(monkeypatch):
    store = FakeConfigMapStore()
    monkeypatch.setattr(groupsmod, "K8sClient", lambda: store)
    return store


def group_named(name):
    return get_session().query(MinionGroup).filter_by(name=name).first()


def test_groups_page_renders_nodegroups(admin, configmap):
    configmap.data["master.conf"] = (
        "nodegroups:\n  web: L@web-01,web-02\n  db: G@os:Debian\n"
    )
    html = admin.get("/groups/").data.decode()
    assert "Node groups" in html
    assert "L@web-01,web-02" in html
    assert "G@os:Debian" in html
    assert "Add node group" in html  # admin may write
    assert "Cannot read nodegroups" not in html


def test_groups_page_error_when_configmap_unreadable(admin, app):
    # No configmap fixture: the real K8sClient is unavailable in tests,
    # so the page renders the error banner instead of a 500.
    html = admin.get("/groups/").data.decode()
    assert "Cannot read nodegroups" in html


def test_create_nodegroup(admin, app, configmap):
    rv = admin.post(
        "/groups",
        data={"name": "web", "prefix": "L@", "value": "web-01, web-02"},
        follow_redirects=True,
    )
    assert "saved as" in rv.data.decode()
    assert "L@web-01, web-02" in rv.data.decode()
    assert configmap.nodegroups()["web"] == "L@web-01, web-02"
    with app.app_context():
        assert group_named("web").members == ["web-01", "web-02"]


def test_create_validates(admin, configmap):
    rv = admin.post(
        "/groups", data={"name": "", "value": "web-01"}, follow_redirects=True
    )
    assert "Enter a group name." in rv.data.decode()
    rv = admin.post(
        "/groups", data={"name": "web", "value": ""}, follow_redirects=True
    )
    assert "Enter an expression value." in rv.data.decode()
    assert configmap.nodegroups() == {}


def test_delete_nodegroup(admin, app, configmap):
    admin.post("/groups", data={"name": "web", "prefix": "L@", "value": "web-01"})
    rv = admin.post(
        "/groups", data={"delete": "web"}, follow_redirects=True
    )
    assert "deleted" in rv.data.decode()
    assert "web" not in configmap.nodegroups()
    with app.app_context():
        assert group_named("web") is None


def test_delete_unknown_nodegroup(admin, configmap):
    rv = admin.post(
        "/groups", data={"delete": "missing"}, follow_redirects=True
    )
    assert "does not exist" in rv.data.decode()


def test_delete_requires_confirm(admin, configmap):
    admin.post("/groups", data={"name": "doomed", "prefix": "L@", "value": "web-01"})
    html = admin.get("/groups/").data.decode()
    assert 'data-confirm="Delete node group \'doomed\'?"' in html


def test_success_flash_renders_success(admin, configmap):
    rv = admin.post(
        "/groups",
        data={"name": "ok", "prefix": "L@", "value": "web-01"},
        follow_redirects=True,
    )
    html = rv.data.decode()
    assert "alert-success" in html
    assert "alert-error" not in html


def test_error_flash_renders_error(admin, configmap):
    rv = admin.post(
        "/groups", data={"name": "", "value": "web-01"}, follow_redirects=True
    )
    html = rv.data.decode()
    assert "alert-error" in html
    assert "alert-success" not in html


def test_groups_forbidden_for_viewer(app):
    from overstate_ui import auth as authmod
    from overstate_ui.models import User

    with app.app_context():
        get_session().add(
            User(username="vwr", password_hash=authmod._ph.hash("vpw"), role="viewer")
        )
        get_session().commit()
    client = app.test_client()
    client.post("/login", data={"username": "vwr", "password": "vpw"})
    assert client.post("/groups").status_code == 403


def test_nodegroup_target_fires_job(app, admin, monkeypatch):
    seen = []

    def fake_local(*a, **k):
        seen.append(k)
        return [{"jid": "j9"}]

    monkeypatch.setattr(app.extensions["salt_client"], "local", fake_local)
    rv = admin.post(
        "/jobs/run",
        data={
            "tgt": "web",
            "tgt_type": "nodegroup",
            "fun": "test.ping",
            "mode": "async",
            "via": "local",
        },
    )
    assert rv.status_code == 302
    jid = rv.headers["Location"].rsplit("/", 1)[1]
    assert len(jid) == 20 and jid.isdigit()  # app-side shared JID (D12)
    assert seen and seen[0].get("jid") == jid
    assert seen[0].get("tgt_type") == "nodegroup"
    with app.app_context():
        from overstate_ui.models import Job

        job = get_session().get(Job, jid)
        assert job is not None and job.tgt_type == "nodegroup"
        assert job.tgt == "web"  # Salt resolves nodegroups natively


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
        get_session().add(MinionGroup(name="mix", members=["web-01", "ghost"]))
        get_session().commit()
        targets, stale = resolve_group_target("mix")
        assert targets == ["web-01"] and stale == 1


def test_minions_page_has_no_groups(admin):
    html = admin.get("/minions/").data.decode()
    assert 'id="group-modal"' not in html
    assert "Use in new job" not in html


def test_nav_links_groups_page(admin):
    html = admin.get("/minions/").data.decode()
    assert 'href="/groups/"' in html
    assert "Groups</span>" in html


def test_viewer_sees_readonly_ui(app, configmap):
    from overstate_ui.auth import _ph
    from overstate_ui.models import User

    configmap.data["master.conf"] = "nodegroups:\n  web: L@web-01\n"
    with app.app_context():
        get_session().add(
            User(username="v", password_hash=_ph.hash("pw"), role="viewer")
        )
        get_session().commit()
    client = app.test_client()
    client.post("/login", data={"username": "v", "password": "pw"})
    html = client.get("/groups/").data.decode()
    assert "L@web-01" in html  # can read
    assert "Add node group" not in html  # but not write
    assert "Delete node group" not in html
    assert client.post("/groups", data={"name": "x", "value": "y"}).status_code == 403


def test_batch_resolves_group(app):
    from overstate_ui.jobs import resolve_batch_roster

    with app.app_context():
        get_session().add(MinionGroup(name="web", members=["web-02"]))
        get_session().commit()
        assert resolve_batch_roster("web", "group") == ["web-02"]
        assert resolve_batch_roster("missing", "group") == []
