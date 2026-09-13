"""Phase 5 tests: run, sync from returner rows, history, saved, SSE stream."""

import datetime as dt
import json

import httpx
import pytest

from overstate_ui import create_app
from overstate_ui.auth import seed_admin
from overstate_ui.config import TestConfig
from overstate_ui.db import create_all, get_session, init_db
from overstate_ui.jobs import sync_job
from overstate_ui.models import AuditEvent, Job, JobReturn, SavedJob
from overstate_ui.salt_client import SaltClient
from overstate_ui.seed_mock import seed as seed_mock


def fake_transport() -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/login":
            return httpx.Response(200, json={"return": [{"token": "tok",
                                                         "expire": 99}]})
        body = json.loads(request.content or b"{}")
        if body.get("client") == "local_async":
            assert body["fun"] == "test.ping"
            return httpx.Response(200, json={"return": [{"jid": "99999",
                                                         "minions": ["m1"]}]})
        return httpx.Response(200, json={"return": [{}]})

    return httpx.MockTransport(handler)


@pytest.fixture()
def client():
    init_db("sqlite://")
    app = create_app(TestConfig)
    app.config["WTF_CSRF_ENABLED"] = False
    app.extensions["salt_client"] = SaltClient(
        "https://salt:8000", "u", "p", transport=fake_transport())
    with app.app_context():
        create_all()
        seed_admin(password="pw")
        seed_mock(get_session())
    c = app.test_client()
    c.post("/login", data={"username": "admin", "password": "pw"})
    c.app = app
    return c


def test_run_async_creates_job_and_audit(client):
    rv = client.post("/jobs/run", data={
        "tgt": "*", "tgt_type": "glob", "fun": "test.ping",
        "args": "", "mode": "async", "save_as": "ping all"})
    assert rv.status_code == 302
    assert "/jobs/99999" in rv.headers["Location"]
    with client.app.app_context():
        job = get_session().get(Job, "99999")
        assert job is not None and job.fun == "test.ping"
        assert job.complete is False
        audit = get_session().query(AuditEvent).filter(
            AuditEvent.action == "run:test.ping").one()
        assert audit.jid == "99999"
        saved = get_session().query(SavedJob).filter_by(name="ping all").one()
        assert saved.fun == "test.ping"


def test_run_requires_fun(client):
    rv = client.post("/jobs/run", data={"tgt": "*", "tgt_type": "glob",
                                        "fun": ""})
    assert rv.status_code == 302
    assert "new" in rv.headers["Location"]


def test_sync_copies_returner_rows(client):
    with client.app.app_context():
        job = sync_job("20260910123000000002")
        assert job is not None
        assert job.fun == "state.highstate"
        returns = get_session().query(JobReturn).filter_by(
            jid="20260910123000000002").all()
        assert len(returns) == 2
        failed = [r for r in returns if not r.success]
        assert len(failed) == 1 and failed[0].minion_id == "tw-minion-02"


def test_sync_completes_old_job_without_returns(client):
    with client.app.app_context():
        from overstate_ui.models import Job

        get_session().add(Job(jid="42424242424242424242", fun="test.ping",
                              tgt="*", tgt_type="glob", user="admin",
                              complete=False))
        get_session().add(Job(jid="42424242424242424243", fun="test.ping",
                              tgt="*", tgt_type="glob", user="admin",
                              complete=False))
        get_session().commit()
        old = get_session().get(Job, "42424242424242424242")
        old.started_at = (dt.datetime.now(dt.timezone.utc)
                          - dt.timedelta(hours=2))
        get_session().commit()
        assert sync_job("42424242424242424242").complete is True
        assert sync_job("42424242424242424243").complete is False


def test_detail_and_history_render(client):
    with client.app.app_context():
        sync_job("20260910123000000002")
    html = client.get("/jobs/20260910123000000002").data.decode()
    assert "tw-minion-02" in html and "failed" in html
    assert "Sync results" in html


def test_detail_recovery_links(client):
    from overstate_ui.models import AuditEvent

    with client.app.app_context():
        sync_job("20260910123000000002")
        get_session().add(AuditEvent(user="admin",
                                     action="kill:20260910123000000002",
                                     jid="kk"))
        get_session().commit()
    html = client.get("/jobs/20260910123000000002").data.decode()
    assert html.count("Re-run") == 1
    assert "tgt=tw-minion-02" in html and "tgt_type=list" in html
    assert "Failed states:" in html
    assert "pkg_|-nginx_|-nginx_|-installed" in html
    assert "check presence" in html


def test_new_prefills_fun_and_args(client):
    html = client.get("/jobs/new?tgt=x&tgt_type=glob&fun=test.ping&args=a"
                      ).data.decode()
    assert 'name="fun" value="test.ping"' in html
    assert 'name="args" value="a"' in html


def test_jobs_page_pause_labels_scope(client):
    html = client.get("/jobs/").data.decode()
    assert "Pause live updates" in html
    assert "all pages" in html


def test_stream_completes_for_old_job(client):
    with client.app.app_context():
        from overstate_ui.models import SaltReturn

        job = sync_job("20260910123000000002")
        old = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=2)
        job.started_at = old
        for sr in get_session().query(SaltReturn).all():
            sr.alter_time = old
        get_session().commit()
    rv = client.get("/jobs/20260910123000000002/stream?interval=0.05")
    text = rv.data.decode()
    assert '"complete": true' in text
    assert "event: done" in text


def test_saved_delete(client):
    with client.app.app_context():
        saved_id = get_session().query(SavedJob).first().id
    rv = client.post(f"/jobs/saved/{saved_id}/delete")
    assert rv.status_code == 302
    with client.app.app_context():
        assert get_session().query(SavedJob).count() == 1


def test_new_renders_operation_library(client):
    from overstate_ui.jobs import OPERATION_GROUPS

    html = client.get("/jobs/new").data.decode()
    assert 'id="op-search"' in html
    assert 'id="fun-list"' in html
    assert 'id="blast"' not in html
    assert "os:Debian" in html
    for group, ops in OPERATION_GROUPS:
        assert group.replace("&", "&amp;") in html
        for op in ops:
            assert f"/jobs/new?preset={op['preset']}" in html
            assert op["fun"] in html
            assert op["about"] in html


def test_new_marks_active_preset(client):
    html = client.get("/jobs/new?preset=ping").data.decode()
    assert 'aria-current="true"' in html
    assert 'value="test.ping"' in html


def test_library_collapsed_by_default(client):
    html = client.get("/jobs/new").data.decode()
    assert 'id="op-library-toggle"' in html
    assert "checked" not in html
    html = client.get("/jobs/new?preset=ping").data.decode()
    assert 'id="op-library-toggle"' in html
    assert "checked" in html
