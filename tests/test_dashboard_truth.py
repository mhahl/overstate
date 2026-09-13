"""Dashboard truth tests: live version skew and master-active
in-flight counts, with snapshot/DB fallbacks when denied."""

import json

import httpx

from overstate_ui import create_app
from overstate_ui.auth import seed_admin
from overstate_ui.config import TestConfig
from overstate_ui.db import create_all, get_session, init_db
from overstate_ui.models import Job, Minion
from overstate_ui.salt_client import SaltClient

DENY_RUNNERS = False
GROUPED_VERSIONS = False


def fake_transport() -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/login":
            return httpx.Response(200, json={"return": [{"token": "tok",
                                                         "expire": 99}]})
        body = json.loads(request.content or b"{}")
        if body.get("client") == "wheel":
            return httpx.Response(200, json={"return": [{"data": {"return": {
                "minions": ["web-01"], "minions_pre": [],
                "minions_rejected": [], "minions_denied": []}}}]})
        if body.get("client") == "runner":
            if DENY_RUNNERS and body.get("fun") in ("manage.versions",
                                                    "jobs.active"):
                return httpx.Response(500, json={})
            if body.get("fun") == "manage.versions":
                if GROUPED_VERSIONS:
                    versions = {"Up to date": ["web-01"]}
                else:
                    versions = {"web-01": "3006.5", "db-01": "3006.5"}
                return httpx.Response(200, json={"return": [versions]})
            if body.get("fun") == "jobs.active":
                return httpx.Response(200, json={"return": [{
                    "12345": {"fun": "state.highstate"}}]})
            return httpx.Response(200, json={"return": [{
                "up": ["web-01"], "down": []}]})
        return httpx.Response(200, json={"return": [{}]})

    return httpx.MockTransport(handler)


def make_client():
    init_db("sqlite://")
    app = create_app(TestConfig)
    app.config["WTF_CSRF_ENABLED"] = False
    app.extensions["salt_client"] = SaltClient(
        "https://salt:8000", "u", "p", transport=fake_transport())
    with app.app_context():
        create_all()
        seed_admin(password="pw")
        get_session().add(Minion(id="web-01", key_status="accepted",
                                 grains={"saltversion": "3006.5"}))
        get_session().add(Job(jid="12345", fun="state.highstate", tgt="*",
                              tgt_type="glob", user="admin", complete=False))
        get_session().add(Job(jid="stale-1", fun="test.ping", tgt="*",
                              tgt_type="glob", user="admin", complete=False))
        get_session().commit()
    c = app.test_client()
    c.post("/login", data={"username": "admin", "password": "pw"})
    c.app = app
    return c


def test_dashboard_shows_live_versions_and_in_flight():
    html = make_client().get("/").data.decode()
    assert "3006.5" in html  # live skew from manage.versions
    assert "Live from master" in html
    assert ">1<" in html  # only the master-active JID counts


def test_denied_runners_fall_back_to_snapshot_and_db():
    global DENY_RUNNERS
    DENY_RUNNERS = True
    try:
        html = make_client().get("/").data.decode()
    finally:
        DENY_RUNNERS = False
    assert "3006.5" in html  # snapshot grains still feed the panel
    assert "Live from master" not in html
    assert ">2<" in html  # both DB-incomplete jobs count


def test_grouped_versions_fall_back_to_snapshot():
    global GROUPED_VERSIONS
    GROUPED_VERSIONS = True
    try:
        html = make_client().get("/").data.decode()
    finally:
        GROUPED_VERSIONS = False
    assert "3006.5" in html  # snapshot, not the unparseable grouping
    assert "Up to date" not in html


def test_normalize_versions_shapes():
    from overstate_ui.tasks import normalize_versions

    assert normalize_versions({"a": "1", "b": "1", "c": "2"}) == \
        {"1": 2, "2": 1}
    assert normalize_versions({"Up to date": {"a": "1", "b": "1"},
                               "Master": "3008.2"}) == {"1": 2}
    assert normalize_versions({"Minion offline": {"a": False}}) == {}
    assert normalize_versions({"Up to date": ["a"]}) == {}
    assert normalize_versions("3006") == {}
    assert normalize_versions(None) == {}


def test_non_jid_active_payload_falls_back():
    from overstate_ui.tasks import fleet_truth_now

    class OddClient:
        def runner(self, fun, **kwargs):
            return [{"up": ["a"], "down": []}]

    out = fleet_truth_now(OddClient())
    assert out["active_live"] is False
    assert out["active_jids"] == []
