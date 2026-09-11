"""v2 unit 3 tests: fleet presets prefill, typed-confirm gating, launch flow."""

import json

import httpx
import pytest

from overstate_ui import create_app
from overstate_ui.auth import seed_admin
from overstate_ui.config import TestConfig
from overstate_ui.db import create_all, get_session, init_db
from overstate_ui.jobs import DESTRUCTIVE_FUNS
from overstate_ui.models import Job
from overstate_ui.salt_client import SaltClient


def fake_transport() -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/login":
            return httpx.Response(200, json={"return": [{"token": "tok",
                                                         "expire": 99}]})
        body = json.loads(request.content or b"{}")
        if body.get("client") == "local_async":
            return httpx.Response(200, json={"return": [{"jid": "42424",
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
    c = app.test_client()
    c.post("/login", data={"username": "admin", "password": "pw"})
    c.app = app
    return c


def test_fleet_presets_prefill(client):
    rv = client.get("/jobs/new", query_string={"preset": "service-restart"})
    assert rv.status_code == 200
    assert b"service.restart" in rv.data
    rv = client.get("/jobs/new", query_string={"preset": "process-signal"})
    assert b"ps.kill_pid" in rv.data


def test_destructive_without_confirm_shows_interstitial(client):
    rv = client.post("/jobs/run", data={
        "tgt": "web-*", "tgt_type": "glob", "fun": "service.restart",
        "args": "nginx", "mode": "async"})
    assert rv.status_code == 200
    assert b"Type <code>web-*" in rv.data
    with client.app.app_context():
        assert get_session().query(Job).count() == 0


def test_destructive_with_wrong_confirm_stays(client):
    rv = client.post("/jobs/run", data={
        "tgt": "web-*", "tgt_type": "glob", "fun": "service.restart",
        "args": "nginx", "mode": "async", "confirm": "oops"})
    assert rv.status_code == 200
    with client.app.app_context():
        assert get_session().query(Job).count() == 0


def test_destructive_with_matching_confirm_launches(client):
    rv = client.post("/jobs/run", data={
        "tgt": "web-*", "tgt_type": "glob", "fun": "service.restart",
        "args": "nginx", "mode": "async", "confirm": "web-*"})
    assert rv.status_code == 302
    assert "/jobs/42424" in rv.headers["Location"]
    with client.app.app_context():
        job = get_session().get(Job, "42424")
        assert job is not None and job.fun == "service.restart"


def test_non_destructive_needs_no_confirm(client):
    rv = client.post("/jobs/run", data={
        "tgt": "*", "tgt_type": "glob", "fun": "test.ping",
        "args": "", "mode": "async"})
    assert rv.status_code == 302


def test_destructive_set_covers_package_changes():
    assert {"pkg.install", "pkg.remove"} <= set(DESTRUCTIVE_FUNS)
