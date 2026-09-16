"""Unit 9: failover-pair fan-out — shared-JID publishes, keys accept-on-both.

Two stub salt-api pods (httpx MockTransports) stand in for the pair;
pod_clients is monkeypatched so no cluster is needed.
"""

import json

import httpx
import pytest

import overstate_ui.jobs_service as jobs_service_mod
import overstate_ui.keys as keys_mod
from overstate_ui import create_app
from overstate_ui.auth import seed_admin
from overstate_ui.config import TestConfig
from overstate_ui.db import create_all, get_session, init_db
from overstate_ui.fleet import pod_api_urls
from overstate_ui.models import AuditEvent, Job, JobReturn
from overstate_ui.salt_client import SaltClient


def _pod_client(seen, behavior):
    """SaltClient stub recording every local()/wheel() body it receives."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/login":
            return httpx.Response(
                200, json={"return": [{"token": "tok", "expire": 99}]}
            )
        body = json.loads(request.content or b"{}")
        seen.append(body)
        return behavior(body)

    return SaltClient(
        "https://pod:8000", "u", "p", transport=httpx.MockTransport(handler)
    )


@pytest.fixture()
def app_pair(monkeypatch):
    init_db("sqlite://")
    app = create_app(TestConfig)
    app.config["WTF_CSRF_ENABLED"] = False
    with app.app_context():
        create_all()
        seed_admin(password="pw")

    def login(username="admin"):
        client = app.test_client()
        client.post("/login", data={"username": username, "password": "pw"})
        client.app = app
        return client

    return {"app": app, "login": login}


def _actions(client):
    with client.app.app_context():
        session = get_session()
        try:
            return [(row.action, row.jid) for row in session.query(AuditEvent).all()]
        finally:
            session.close()


def test_local_forwards_jid_only_when_set():
    seen = []
    client = _pod_client(seen, lambda body: httpx.Response(200, json={"return": [{}]}))
    client.local("m1", "test.ping", asynchronous=True, jid="20240101000000000001")
    assert seen[-1]["jid"] == "20240101000000000001"
    client.local("m1", "test.ping", asynchronous=True)
    assert "jid" not in seen[-1]
    client.local("m1", "test.ping", via="ssh", jid="20240101000000000001")
    assert "jid" not in seen[-1]


def test_pod_api_urls_empty_outside_cluster(monkeypatch):
    import overstate_ui.fleet as fleet_mod

    class Offline:
        config = type("C", (), {"available": False})()

    monkeypatch.setattr(fleet_mod, "K8sClient", lambda: Offline())
    assert pod_api_urls() == []


def test_async_run_publishes_shared_jid_on_both_pods(app_pair, monkeypatch):
    seen0, seen1 = [], []
    c0 = _pod_client(
        seen0, lambda body: httpx.Response(200, json={"return": [{"jid": "x"}]})
    )
    c1 = _pod_client(
        seen1, lambda body: httpx.Response(200, json={"return": [{"jid": "x"}]})
    )
    monkeypatch.setattr(
        jobs_service_mod, "pod_clients", lambda default: [("pod-0", c0), ("pod-1", c1)]
    )
    client = app_pair["login"]()
    rv = client.post(
        "/jobs/run",
        data={
            "tgt": "*",
            "tgt_type": "glob",
            "fun": "test.ping",
            "args": "",
            "mode": "async",
        },
    )
    assert rv.status_code == 302
    jid0 = next(b["jid"] for b in seen0 if b.get("client") == "local_async")
    jid1 = next(b["jid"] for b in seen1 if b.get("client") == "local_async")
    assert jid0 == jid1 and len(jid0) == 20
    assert rv.headers["Location"].endswith(f"/jobs/{jid0}")
    with client.app.app_context():
        assert get_session().get(Job, jid0) is not None
    assert any(a == ("run:test.ping", jid0) for a in _actions(client))


def test_sync_run_merges_returns_under_one_jid(app_pair, monkeypatch):
    def pod0(body):
        return httpx.Response(200, json={"return": [{"m1": {"result": True}}]})

    def pod1(body):
        return httpx.Response(200, json={"return": [{"m2": {"result": True}}]})

    c0 = _pod_client([], pod0)
    c1 = _pod_client([], pod1)
    monkeypatch.setattr(
        jobs_service_mod, "pod_clients", lambda default: [("pod-0", c0), ("pod-1", c1)]
    )
    client = app_pair["login"]()
    rv = client.post(
        "/jobs/run",
        data={
            "tgt": "*",
            "tgt_type": "glob",
            "fun": "test.ping",
            "args": "",
            "mode": "sync",
        },
        follow_redirects=True,
    )
    assert rv.status_code == 200
    with client.app.app_context():
        session = get_session()
        jobs = session.query(Job).filter_by(fun="test.ping").all()
        assert len(jobs) == 1
        assert jobs[0].complete is True
        mids = {
            r.minion_id
            for r in session.query(JobReturn).filter_by(jid=jobs[0].jid).all()
        }
        assert mids == {"m1", "m2"}


def test_partial_publish_warns_and_audits(app_pair, monkeypatch):
    seen = []
    c0 = _pod_client(
        seen, lambda body: httpx.Response(200, json={"return": [{"jid": "x"}]})
    )
    c1 = _pod_client([], lambda body: httpx.Response(500, json={"error": "down"}))
    monkeypatch.setattr(
        jobs_service_mod, "pod_clients", lambda default: [("pod-0", c0), ("pod-1", c1)]
    )
    client = app_pair["login"]()
    rv = client.post(
        "/jobs/run",
        data={
            "tgt": "*",
            "tgt_type": "glob",
            "fun": "test.ping",
            "args": "",
            "mode": "async",
        },
        follow_redirects=True,
    )
    assert b"pod-1 unreachable" in rv.data
    actions = [a for a, _ in _actions(client)]
    assert "run:test.ping" in actions
    assert "run-partial:test.ping:pod-1" in actions


def _wheel_ok(funs, roster):
    def behavior(body):
        funs.append((body.get("fun"), body.get("match")))
        if body.get("fun") == "key.list_all":
            return httpx.Response(
                200,
                json={
                    "return": [
                        {
                            "data": {
                                "return": {
                                    "minions": [],
                                    "minions_pre": roster,
                                    "minions_rejected": [],
                                    "minions_denied": [],
                                }
                            }
                        }
                    ]
                },
            )
        if body.get("fun") == "key.finger":
            return httpx.Response(
                200, json={"return": [{"data": {"return": {"m1": "aa:bb"}}}]}
            )
        return httpx.Response(200, json={"return": [True]})

    return behavior


def test_keys_accept_fans_out_to_both_pods(app_pair, monkeypatch):
    funs0, funs1 = [], []
    c0 = _pod_client([], _wheel_ok(funs0, ["m1"]))
    c1 = _pod_client([], _wheel_ok(funs1, ["m1"]))
    monkeypatch.setattr(
        keys_mod, "pod_clients", lambda default: [("pod-0", c0), ("pod-1", c1)]
    )
    client = app_pair["login"]()
    rv = client.post(
        "/keys/accept", data={"id": "m1", "tab": "pending"}, follow_redirects=True
    )
    assert b"m1: accepted." in rv.data
    assert ("key.accept", "m1") in funs0
    assert ("key.accept", "m1") in funs1
    assert ("accept-key", None) in _actions(client)


def test_keys_partial_accept_warns(app_pair, monkeypatch):
    funs0 = []
    c0 = _pod_client([], _wheel_ok(funs0, ["m1"]))
    c1 = _pod_client([], lambda body: httpx.Response(500, json={"error": "down"}))
    monkeypatch.setattr(
        keys_mod, "pod_clients", lambda default: [("pod-0", c0), ("pod-1", c1)]
    )
    client = app_pair["login"]()
    rv = client.post(
        "/keys/accept", data={"id": "m1", "tab": "pending"}, follow_redirects=True
    )
    assert b"pod-1 unreachable" in rv.data
    assert ("key.accept", "m1") in funs0
    actions = [a for a, _ in _actions(client)]
    assert "accept-key-partial:m1:pod-1" in actions


def test_roster_merges_per_pod_states(app_pair, monkeypatch):
    c0 = _pod_client([], _wheel_ok([], ["m1"]))
    c1 = _pod_client([], _wheel_ok([], []))
    monkeypatch.setattr(
        keys_mod, "pod_clients", lambda default: [("pod-0", c0), ("pod-1", c1)]
    )
    client = app_pair["login"]()
    pending = client.get("/keys/?tab=pending").data.decode()
    assert "m1" in pending and "pod-0: pending" in pending
    accepted = client.get("/keys/?tab=accepted").data.decode()
    assert "m1" not in accepted
