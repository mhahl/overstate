"""v2 unit 4 tests: proxy grain gaps, syndic notes, salt-ssh path."""

import json

import httpx
import pytest

from overstate_ui import create_app
from overstate_ui.auth import seed_admin
from overstate_ui.config import TestConfig
from overstate_ui.db import create_all, get_session, init_db
from overstate_ui.minions import normalize_grains
from overstate_ui.models import Job
from overstate_ui.salt_client import SaltClient


def test_normalize_grains_proxy_gaps():
    assert normalize_grains(None) == {}
    assert normalize_grains("not-a-dict") == {}
    assert normalize_grains({})["ipv4"] == []
    assert normalize_grains({"ipv4": "10.0.0.5"})["ipv4"] == ["10.0.0.5"]
    assert normalize_grains({"ipv4": ["a"]})["ipv4"] == ["a"]
    # proxy markers survive normalization
    assert normalize_grains({"proxytype": "dummy"})["proxytype"] == "dummy"


@pytest.fixture()
def client():
    init_db("sqlite://")
    app = create_app(TestConfig)
    app.config["WTF_CSRF_ENABLED"] = False

    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/login":
            return httpx.Response(200, json={"return": [{"token": "tok",
                                                         "expire": 99}]})
        body = json.loads(request.content or b"{}")
        seen.update(body)
        if body.get("client") == "local_async":
            return httpx.Response(200, json={"return": [{"jid": "1",
                                                         "minions": []}]})
        if body.get("client") == "wheel":
            return httpx.Response(200, json={"return": [{
                "data": {"return": {"minions": [], "minions_pre": [],
                                    "minions_rejected": [], "minions_denied": []}}}]})
        return httpx.Response(200, json={"return": [{"m1": True}]})

    app.extensions["salt_client"] = SaltClient(
        "https://salt:8000", "u", "p", transport=httpx.MockTransport(handler))
    with app.app_context():
        create_all()
        seed_admin(password="pw")
    c = app.test_client()
    c.post("/login", data={"username": "admin", "password": "pw"})
    c.app = app
    c.seen = seen
    return c


def test_proxy_grains_render_in_list_and_csv(client):
    from overstate_ui.models import Minion

    with client.app.app_context():
        get_session().add(Minion(id="prox-1", key_status="accepted",
                                 grains={"proxytype": "dummy",
                                         "ipv4": "10.0.0.5"}))
        get_session().commit()
    rv = client.get("/minions/")
    assert rv.status_code == 200
    assert b"proxy" in rv.data and b"10.0.0.5" in rv.data
    rv = client.get("/minions/export.csv")
    assert rv.status_code == 200
    assert b"prox-1" in rv.data and b"10.0.0.5" in rv.data


def test_syndic_banner_only_when_configured(client):
    assert b"Syndic topology" not in client.get("/keys/").data
    client.app.config["SYNDIC_MASTERS"] = "moa-1, moa-2"
    rv = client.get("/keys/")
    assert b"Syndic topology" in rv.data and b"moa-1" in rv.data


def test_ssh_run_posts_ssh_client_and_synthesizes_jid(client):
    rv = client.post("/jobs/run", data={
        "tgt": "roster-*", "tgt_type": "glob", "fun": "test.ping",
        "args": "", "mode": "async", "via": "ssh"})
    assert rv.status_code == 302
    assert client.seen.get("client") == "ssh"
    assert client.seen.get("ignore_invalid") is True
    jid = rv.headers["Location"].rsplit("/", 1)[-1]
    assert jid.startswith("ssh-")
    with client.app.app_context():
        job = get_session().get(Job, jid)
        assert job is not None and job.complete is True


def test_ssh_forces_sync_mode(client):
    rv = client.post("/jobs/run", data={
        "tgt": "r", "tgt_type": "glob", "fun": "test.ping",
        "args": "", "mode": "async", "via": "ssh"}, follow_redirects=True)
    assert b"runs synchronously" in rv.data


def test_bad_via_falls_back_to_local(client):
    rv = client.post("/jobs/run", data={
        "tgt": "*", "tgt_type": "glob", "fun": "test.ping",
        "args": "", "mode": "async", "via": "telnet"})
    assert rv.status_code == 302
    assert client.seen.get("client") == "local_async"
