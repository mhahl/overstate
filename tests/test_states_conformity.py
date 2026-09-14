"""Conformity must only cite jobs that actually checked the minion.

Regression: recompute_conformity() used to stamp every minion missing
from the latest state job with {"status": "unknown", "jid": latest},
so a minion page claimed "Last checked in <job>" even when that job's
target glob did not cover the minion.
"""

import datetime as dt
import json

import httpx

from overstate_ui import create_app
from overstate_ui.auth import seed_admin
from overstate_ui.config import TestConfig
from overstate_ui.db import create_all, get_session, init_db
from overstate_ui.models import Job, JobReturn, Minion
from overstate_ui.salt_client import SaltClient
from overstate_ui.states import recompute_conformity


def _app():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/login":
            return httpx.Response(
                200, json={"return": [{"token": "tok", "expire": 99}]}
            )
        body = json.loads(request.content or b"{}")
        if body.get("client") == "runner":
            return httpx.Response(200, json={"return": [{"up": [], "down": []}]})
        return httpx.Response(200, json={"return": [{}]})

    init_db("sqlite://")
    app = create_app(TestConfig)
    app.config["WTF_CSRF_ENABLED"] = False
    app.extensions["salt_client"] = SaltClient(
        "https://salt:8000", "u", "p", transport=httpx.MockTransport(handler)
    )
    with app.app_context():
        create_all()
        seed_admin(password="pw")
    return app


def _seed_narrow_glob(app):
    """Old job covered both minions; new job targets web-* only."""
    with app.app_context():
        session = get_session()
        now = dt.datetime.now(dt.UTC)
        session.add(
            Minion(
                id="web-01",
                grains={},
                conformity={"status": "ok", "jid": "old"},
                key_status="accepted",
            )
        )
        session.add(
            Minion(
                id="db-01",
                grains={},
                conformity={"status": "ok", "jid": "old"},
                key_status="accepted",
            )
        )
        session.add(
            Job(
                jid="old",
                fun="state.highstate",
                tgt="*",
                tgt_type="glob",
                user="u",
                started_at=now - dt.timedelta(hours=2),
            )
        )
        session.add(
            Job(
                jid="new",
                fun="state.highstate",
                tgt="web-*",
                tgt_type="glob",
                user="u",
                started_at=now,
            )
        )
        session.add(
            JobReturn(
                jid="old", minion_id="web-01", success=True, retcode=0, payload={}
            )
        )
        session.add(
            JobReturn(
                jid="old", minion_id="db-01", success=True, retcode=0, payload={}
            )
        )
        session.add(
            JobReturn(
                jid="new", minion_id="web-01", success=False, retcode=1, payload={}
            )
        )
        session.commit()


def test_uncovered_minion_keeps_prior_conformity():
    app = _app()
    _seed_narrow_glob(app)
    with app.app_context():
        assert recompute_conformity() == "new"
        covered = get_session().get(Minion, "web-01")
        assert covered.conformity == {"status": "drifted", "jid": "new"}
        uncovered = get_session().get(Minion, "db-01")
        assert uncovered.conformity == {"status": "ok", "jid": "old"}


def test_minion_without_returns_is_not_attributed():
    app = _app()
    with app.app_context():
        session = get_session()
        now = dt.datetime.now(dt.UTC)
        session.add(Minion(id="fresh-01", grains={}, conformity={}, key_status="accepted"))
        session.add(
            Job(
                jid="new",
                fun="state.apply",
                tgt="web-*",
                tgt_type="glob",
                user="u",
                started_at=now,
            )
        )
        session.add(
            JobReturn(
                jid="new", minion_id="web-01", success=True, retcode=0, payload={}
            )
        )
        session.commit()
        recompute_conformity()
        assert get_session().get(Minion, "fresh-01").conformity == {}


def test_unknown_verdict_never_links_a_job():
    app = _app()
    with app.app_context():
        # Legacy row already stamped unknown+jid must not claim a check.
        get_session().add(
            Minion(
                id="legacy-01",
                grains={},
                conformity={"status": "unknown", "jid": "new"},
                key_status="accepted",
            )
        )
        get_session().commit()
    client = app.test_client()
    client.post("/login", data={"username": "admin", "password": "pw"})
    detail = client.get("/minions/legacy-01").data.decode()
    assert "No highstate-style job recorded" in detail
    assert "Last checked in" not in detail
    states = client.get("/states/").data.decode()
    assert ">–<" in states
