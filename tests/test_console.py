"""Console tests: salt-style lines in the browser, same guardrails as Run job."""

import json

import httpx
import pytest

from overstate_ui import auth as authmod
from overstate_ui import create_app
from overstate_ui.auth import seed_admin
from overstate_ui.config import TestConfig
from overstate_ui.db import create_all, get_session, init_db
from overstate_ui.models import AuditEvent, Job, User
from overstate_ui.salt_client import SaltClient

KEY_LISTING = {
    "minions": ["m1"],
    "minions_pre": ["new1"],
    "minions_rejected": [],
    "minions_denied": [],
}


def fake_transport() -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/login":
            return httpx.Response(
                200, json={"return": [{"token": "tok", "expire": 99}]}
            )
        body = json.loads(request.content or b"{}")
        client = body.get("client")
        if client == "local_async":
            return httpx.Response(
                200, json={"return": [{"jid": "1", "minions": ["m1"]}]}
            )
        if client == "local":
            return httpx.Response(200, json={"return": [{"m1": True}]})
        if client == "wheel":
            if body.get("fun") == "key.list_all":
                return httpx.Response(200, json={"return": [dict(KEY_LISTING)]})
            return httpx.Response(200, json={"return": [{"result": True}]})
        if client == "runner":
            if body.get("fun") == "manage.status":
                return httpx.Response(
                    200, json={"return": [{"up": ["m1"], "down": []}]}
                )
            return httpx.Response(200, json={"return": [{}]})
        return httpx.Response(200, json={"return": [{}]})

    return httpx.MockTransport(handler)


@pytest.fixture()
def client():
    init_db("sqlite://")
    app = create_app(TestConfig)
    app.config["WTF_CSRF_ENABLED"] = False
    app.extensions["salt_client"] = SaltClient(
        "https://salt:8000", "u", "p", transport=fake_transport()
    )
    with app.app_context():
        create_all()
        seed_admin(password="pw")
        get_session().add(
            User(
                username="vwr",
                password_hash=authmod._ph.hash("pw"),
                role="viewer",
            )
        )
        get_session().commit()
    c = app.test_client()
    c.post("/login", data={"username": "admin", "password": "pw"})
    c.app = app
    return c


def post_line(client, line: str):
    return client.post("/console/run", json={"line": line})


def test_page_renders_for_operator(client):
    rv = client.get("/console/")
    assert rv.status_code == 200
    html = rv.data.decode()
    assert "Master console" in html
    assert "salt-master $" in html


def test_viewer_can_read_page_but_not_fire(client):
    client.post("/logout")
    client.post("/login", data={"username": "vwr", "password": "pw"})
    rv = client.get("/console/")
    assert rv.status_code == 200
    assert "viewer" in rv.data.decode()
    rv = post_line(client, "salt '*' test.ping")
    assert rv.status_code == 403


def test_help_and_unknown(client):
    rv = post_line(client, "help")
    assert rv.status_code == 200
    assert "salt-key" in rv.get_json()["output"]
    rv = post_line(client, "bash")
    assert rv.status_code == 400
    assert "salt" in rv.get_json()["output"]
    rv = post_line(client, "")
    assert rv.status_code == 400
    rv = post_line(client, "salt 'unclosed")
    assert rv.status_code == 400


def test_salt_async_fires_and_audits(client):
    rv = post_line(client, "salt '*' test.ping")
    assert rv.status_code == 200
    data = rv.get_json()
    assert data["ok"] and data["jid"]
    assert "jobs" in data["job_url"]
    with client.app.app_context():
        job = get_session().get(Job, data["jid"])
        assert job is not None and job.fun == "test.ping"
        audit = (
            get_session()
            .query(AuditEvent)
            .filter(AuditEvent.action == "run:test.ping")
            .one()
        )
        assert audit.jid == data["jid"]


def test_salt_sync_prints_per_minion(client):
    rv = post_line(client, "salt m1 test.ping --sync")
    assert rv.status_code == 200
    data = rv.get_json()
    assert data["ok"]
    assert "m1: ok" in data["output"]


def test_blocked_function_rejected(client):
    rv = post_line(client, "salt '*' cmd.run ls")
    assert rv.status_code == 400
    assert "cannot run here" in rv.get_json()["output"]


def test_confirm_gate_mirrors_run_job(client):
    rv = post_line(client, "salt '*' state.apply")
    assert rv.status_code == 400
    assert "--confirm" in rv.get_json()["output"]
    rv = post_line(client, "salt '*' state.apply --confirm='*'")
    assert rv.status_code == 200
    assert rv.get_json()["ok"]
    # Test-mode runs stay exempt, like the Run job form.
    rv = post_line(client, "salt '*' state.highstate test=True")
    assert rv.status_code == 200


def test_target_type_flag(client):
    rv = post_line(client, "salt -t grain 'os:Debian' test.ping")
    assert rv.status_code == 200
    with client.app.app_context():
        job = get_session().get(Job, rv.get_json()["jid"])
        assert job.tgt_type == "grain"
    rv = post_line(client, "salt '*' test.ping -t bogus")
    assert rv.status_code == 400


def test_key_list_and_accept(client):
    rv = post_line(client, "salt-key -L")
    assert rv.status_code == 200
    out = rv.get_json()["output"]
    assert "new1" in out and "m1" in out
    rv = post_line(client, "salt-key -a new1")
    assert rv.status_code == 200
    with client.app.app_context():
        audit = (
            get_session()
            .query(AuditEvent)
            .filter(AuditEvent.action == "console:key.accept:new1")
            .one()
        )
        assert audit is not None
    rv = post_line(client, "salt-key -a '*'")
    assert rv.status_code == 400
    rv = post_line(client, "salt-key -a ghost")
    assert rv.status_code == 400


def test_runner_allowlist(client):
    rv = post_line(client, "salt-run manage.status")
    assert rv.status_code == 200
    assert "m1" in rv.get_json()["output"]
    rv = post_line(client, "salt-run jobs.list_jobs")
    assert rv.status_code == 200
    rv = post_line(client, "salt-run cmd.run")
    assert rv.status_code == 400
    rv = post_line(client, "salt-run jobs.lookup_jid jid")
    assert rv.status_code == 400
