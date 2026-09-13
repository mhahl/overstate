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


def test_groups_sort_by_members(app, admin):
    with app.app_context():
        get_session().add(MinionGroup(name="big",
                                      members=["web-01", "web-02", "db-01"]))
        get_session().add(MinionGroup(name="small", members=["web-01"]))
        get_session().commit()
    html = admin.get("/groups/?sort=members&dir=desc").data.decode()
    assert (html.index('<td class="font-medium">big</td>')
            < html.index('<td class="font-medium">small</td>'))
    html = admin.get("/groups/?sort=members&dir=asc").data.decode()
    assert (html.index('<td class="font-medium">small</td>')
            < html.index('<td class="font-medium">big</td>'))


def test_create_rename_edit_delete(admin, app):
    rv = admin.post("/groups",
                    data={"name": "web", "members": "web-01, web-02 web-01"},
                    follow_redirects=True)
    assert "saved with 2 members" in rv.data.decode()
    with app.app_context():
        assert group_named("web").members == ["web-01", "web-02"]
        gid = group_named("web").id
    rv = admin.post(f"/groups/{gid}/rename", data={"name": "web2"},
                    follow_redirects=True)
    assert "renamed to" in rv.data.decode()
    rv = admin.post(f"/groups/{gid}/members",
                    data={"members": "web-01\ndb-01"},
                    follow_redirects=True)
    assert "now has 2 members" in rv.data.decode()
    with app.app_context():
        assert group_named("web2").members == ["web-01", "db-01"]
    rv = admin.post(f"/groups/{gid}/delete", follow_redirects=True)
    assert "deleted" in rv.data.decode()
    with app.app_context():
        assert group_named("web2") is None


def test_create_validates(admin):
    rv = admin.post("/groups", data={"name": "", "members": "m1"},
                    follow_redirects=True)
    assert "needs a name" in rv.data.decode()
    admin.post("/groups", data={"name": "dup", "members": ""})
    rv = admin.post("/groups", data={"name": "dup", "members": ""},
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
    assert client.post("/groups").status_code == 403


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


def test_success_flash_renders_success(admin):
    rv = admin.post("/groups", data={"name": "ok", "members": ["web-01"]},
                    follow_redirects=True)
    html = rv.data.decode()
    assert "alert-success" in html
    assert "alert-warning" not in html


def test_error_flash_renders_error(admin):
    rv = admin.post("/groups", data={"name": "", "members": ["m1"]},
                    follow_redirects=True)
    html = rv.data.decode()
    assert "alert-error" in html
    assert "alert-warning" not in html


def test_groups_page_renders_table_and_modal(admin):
    html = admin.get("/groups/").data.decode()
    assert "Groups" in html
    assert 'id="group-modal"' in html
    assert 'aria-labelledby="group-modal-title"' in html
    assert 'id="group-modal-search"' in html
    assert 'id="group-modal-count"' in html
    assert '<tbody id="group-modal-list">' in html
    assert "<th>Minion</th>" in html
    for mid in ("web-01", "web-02", "db-01"):
        assert f'type="checkbox" name="members" value="{mid}"' in html
    assert "New group" in html


def test_minions_page_has_no_groups(admin):
    html = admin.get("/minions/").data.decode()
    assert 'id="group-modal"' not in html
    assert "Use in new job" not in html


def test_nav_links_groups_page(admin):
    html = admin.get("/minions/").data.decode()
    assert 'href="/groups/"' in html
    assert "Groups</a>" in html


def table_html(html):
    # The modal embeds the full roster and every group name for
    # client-side filtering/validation, so table assertions scope to
    # the markup before it.
    return html.split('id="group-modal"')[0]


def test_groups_search_filters_names_and_members(admin):
    admin.post("/groups", data={"name": "webfleet",
                                "members": ["web-01"]})
    admin.post("/groups", data={"name": "dbfleet", "members": ["db-01"]})
    admin.get("/groups/")  # flush setup flash messages
    html = table_html(admin.get("/groups/?q=webfleet").data.decode())
    assert "webfleet" in html and "dbfleet" not in html
    html = table_html(admin.get("/groups/?q=db-01").data.decode())
    assert "dbfleet" in html and "webfleet" not in html
    html = table_html(admin.get("/groups/?q=nomatch").data.decode())
    assert "No groups match" in html


def test_groups_paginate(admin):
    for i in range(12):
        admin.post("/groups", data={"name": f"g-{i:02d}", "members": []})
    admin.get("/groups/")  # flush setup flash messages
    html = table_html(admin.get("/groups/?per_page=10").data.decode())
    assert "Page 1 of 2" in html
    assert "g-00" in html and "g-10" not in html
    html = table_html(admin.get("/groups/?per_page=10&page=2").data.decode())
    assert "Page 2 of 2" in html
    assert "g-10" in html and "g-00" not in html


def test_group_row_links_prefilled_job_form(admin):
    admin.post("/groups",
               data={"name": "fleet", "members": ["web-01"]})
    html = admin.get("/groups/").data.decode()
    assert "Use in new job" in html
    assert "/jobs/new?tgt_type=group&amp;tgt=fleet" in html
    body = admin.get("/jobs/new?tgt_type=group&tgt=fleet").data.decode()
    assert 'value="group" selected' in body
    assert 'name="tgt" value="fleet"' in body
    assert "manage groups" in body


def test_delete_requires_confirm(admin):
    admin.post("/groups",
               data={"name": "doomed",
                     "members": ["web-01", "web-02"]})
    html = admin.get("/groups/").data.decode()
    assert "Delete \u201cdoomed\u201d (2 members)?" in html
    assert ">Confirm</button>" in html


def test_viewer_sees_readonly_hint(app):
    from overstate_ui.auth import _ph
    from overstate_ui.models import User

    with app.app_context():
        get_session().add(User(username="v", password_hash=_ph.hash("pw"),
                               role="viewer"))
        get_session().commit()
    client = app.test_client()
    client.post("/login", data={"username": "v", "password": "pw"})
    html = client.get("/groups/").data.decode()
    assert "Only operators can create or change groups" in html
    assert 'id="group-modal"' not in html


def test_create_from_multiselect_and_edit(admin, app):
    rv = admin.post("/groups",
                    data={"name": "fleet",
                          "members": ["web-01", "web-02"]},
                    follow_redirects=True)
    assert "saved with 2 members" in rv.data.decode()
    with app.app_context():
        group = group_named("fleet")
        assert group.members == ["web-01", "web-02"]
        gid = group.id
    rv = admin.post(f"/groups/{gid}/edit",
                    data={"name": "fleet2", "members": ["db-01"]},
                    follow_redirects=True)
    assert "saved" in rv.data.decode()
    with app.app_context():
        assert group_named("fleet2").members == ["db-01"]
        assert group_named("fleet") is None


def test_edit_rejects_duplicate_name(admin, app):
    admin.post("/groups", data={"name": "a", "members": []})
    admin.post("/groups", data={"name": "b", "members": []})
    with app.app_context():
        gid = group_named("a").id
    rv = admin.post(f"/groups/{gid}/edit", data={"name": "b"},
                    follow_redirects=True)
    assert "already exists" in rv.data.decode()


def test_batch_resolves_group(app):
    from overstate_ui.jobs import resolve_batch_roster

    with app.app_context():
        get_session().add(MinionGroup(name="web", members=["web-02"]))
        get_session().commit()
        assert resolve_batch_roster("web", "group") == ["web-02"]
        assert resolve_batch_roster("missing", "group") == []
