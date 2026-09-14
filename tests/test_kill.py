"""Kill tests: gating, publish shape, reporting."""

import pytest

from overstate_ui import create_app
from overstate_ui.auth import seed_admin
from overstate_ui.config import TestConfig
from overstate_ui.db import create_all, get_session, init_db
from overstate_ui.models import AuditEvent, Job, JobReturn, Minion
from overstate_ui.salt_client import SaltApiError


@pytest.fixture()
def app():
    init_db("sqlite://")
    app = create_app(TestConfig)
    app.config["WTF_CSRF_ENABLED"] = False
    with app.app_context():
        create_all()
        seed_admin(password="pw")
        get_session().add(Minion(id="web-01", grains={}, conformity={}))
        get_session().add(
            Job(
                jid="abc123",
                fun="state.apply",
                tgt="web-*",
                tgt_type="glob",
                user="admin",
                complete=False,
            )
        )
        get_session().commit()
    return app


@pytest.fixture()
def admin(app):
    client = app.test_client()
    client.post("/login", data={"username": "admin", "password": "pw"})
    return client


class StubSalt:
    def __init__(self, fail=False):
        self.fail = fail
        self.calls = []

    def local(self, tgt, fun, **kwargs):
        self.calls.append((tgt, fun, kwargs))
        if self.fail:
            raise SaltApiError("denied")
        return [{"jid": "kj1"}]


def test_running_job_detail_handles_stream_drop(admin):
    html = admin.get("/jobs/abc123").data.decode()
    assert "onerror" in html
    assert "Connection lost" in html
    assert 'aria-live="polite"' in html


def test_killable_matrix(app):
    from overstate_ui.jobs import killable

    with app.app_context():
        session = get_session()
        running = session.get(Job, "abc123")
        assert killable(running) is True
        running.complete = True
        assert killable(running) is False
        for prefix in ("batch-", "ssh-", "sync-"):
            running.complete = False
            running.jid = prefix + "x"
            assert killable(running) is False


def test_kill_publishes_and_reports(app, admin, monkeypatch):
    stub = StubSalt()
    monkeypatch.setattr(app.extensions["salt_client"], "local", stub.local)
    rv = admin.post("/jobs/abc123/kill", follow_redirects=True)
    assert "Kill published" in rv.data.decode()
    assert stub.calls == [
        (
            "web-*",
            "saltutil.kill_job",
            {"arg": ["abc123"], "tgt_type": "glob", "asynchronous": True},
        )
    ]
    with app.app_context():
        kill_job = get_session().get(Job, "kj1")
        assert kill_job is not None
        assert kill_job.fun == "saltutil.kill_job"
        event = get_session().query(AuditEvent).filter_by(action="kill:abc123").one()
        assert event.jid == "kj1"
        get_session().add(
            JobReturn(
                jid="kj1", minion_id="web-01", success=True, retcode=0, payload={}
            )
        )
        get_session().commit()
    html = admin.get("/jobs/abc123").data.decode()
    assert "Kill kj1" in html and "web-01" in html


def test_kill_denial_flashes(app, admin, monkeypatch):
    stub = StubSalt(fail=True)
    monkeypatch.setattr(app.extensions["salt_client"], "local", stub.local)
    rv = admin.post("/jobs/abc123/kill", follow_redirects=True)
    assert "kill failed" in rv.data.decode()


def test_kill_guards(app, admin):
    with app.app_context():
        get_session().add(
            Job(
                jid="done1",
                fun="test.ping",
                tgt="*",
                tgt_type="glob",
                user="admin",
                complete=True,
            )
        )
        get_session().commit()
    rv = admin.post("/jobs/done1/kill", follow_redirects=True)
    assert "Only running Salt jobs" in rv.data.decode()
    rv = admin.post("/jobs/nope/kill", follow_redirects=True)
    assert "Unknown job." in rv.data.decode()


def test_kill_forbidden_for_viewer(app):
    from overstate_ui import auth as authmod
    from overstate_ui.models import User

    with app.app_context():
        get_session().add(
            User(username="vwr", password_hash=authmod._ph.hash("vpw"), role="viewer")
        )
        get_session().commit()
    client = app.test_client()
    client.post("/login", data={"username": "vwr", "password": "vpw"})
    assert client.post("/jobs/abc123/kill").status_code == 403
