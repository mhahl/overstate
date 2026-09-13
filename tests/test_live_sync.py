"""Live job sync tests: jobs.lookup_jid merges display-only live
rows into the detail page; expired JIDs fall back cleanly."""

import json

import httpx

from overstate_ui import create_app
from overstate_ui.auth import seed_admin
from overstate_ui.config import TestConfig
from overstate_ui.db import create_all, get_session, init_db
from overstate_ui.models import Job, JobReturn
from overstate_ui.salt_client import SaltClient

LIVE_BADGE = '<span class="badge badge-xs badge-info">live</span>'


def fake_transport() -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/login":
            return httpx.Response(
                200, json={"return": [{"token": "tok", "expire": 99}]}
            )
        body = json.loads(request.content or b"{}")
        if body.get("client") == "wheel":
            return httpx.Response(
                200,
                json={
                    "return": [
                        {
                            "data": {
                                "return": {
                                    "minions": ["web-01"],
                                    "minions_pre": [],
                                    "minions_rejected": [],
                                    "minions_denied": [],
                                }
                            }
                        }
                    ]
                },
            )
        if body.get("client") == "runner":
            if body.get("fun") == "jobs.lookup_jid":
                data = {}
                if body.get("jid") == "j-live":
                    data = {
                        "j-live": {
                            "web-01": {
                                "success": True,
                                "retcode": 0,
                                "return": {"ping": True},
                            }
                        }
                    }
                return httpx.Response(200, json={"return": [data]})
            return httpx.Response(
                200, json={"return": [{"up": ["web-01"], "down": []}]}
            )
        return httpx.Response(200, json={"return": [{}]})

    return httpx.MockTransport(handler)


def make_client(with_db_return=False):
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
            Job(
                jid="j-live",
                fun="test.ping",
                tgt="*",
                tgt_type="glob",
                user="admin",
                complete=False,
            )
        )
        get_session().add(
            Job(
                jid="j-old",
                fun="test.ping",
                tgt="*",
                tgt_type="glob",
                user="admin",
                complete=False,
            )
        )
        if with_db_return:
            get_session().add(
                JobReturn(
                    jid="j-live",
                    minion_id="web-01",
                    success=True,
                    retcode=0,
                    payload={"ping": True},
                )
            )
        get_session().commit()
    c = app.test_client()
    c.post("/login", data={"username": "admin", "password": "pw"})
    c.app = app
    return c


def test_live_rows_merge_marked_live():
    html = make_client().get("/jobs/j-live").data.decode()
    assert "web-01" in html
    assert LIVE_BADGE in html


def test_expired_jid_falls_back_without_live_rows():
    html = make_client().get("/jobs/j-old").data.decode()
    assert "No returns yet" in html
    assert LIVE_BADGE not in html


def test_db_rows_win_no_duplicates():
    html = make_client(with_db_return=True).get("/jobs/j-live").data.decode()
    assert LIVE_BADGE not in html  # covered by the DB row, not merged
    assert html.count("web-01 — ok") == 1


def test_live_returns_handles_scalar():
    from overstate_ui.jobs import live_returns_now

    class ScalarClient:
        def runner(self, fun, **kwargs):
            return [{"m1": True, "m2": False}]

    rows = live_returns_now(ScalarClient(), "9")
    assert [(r.minion_id, r.success) for r in rows] == [("m1", True), ("m2", False)]
    assert all(r.live for r in rows)
