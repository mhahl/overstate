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
from overstate_ui.models import (
    Job,
    JobReturn,
    Minion,
    SaltReturn,
    StateConformityHistory,
    WatchedState,
)
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
            JobReturn(jid="old", minion_id="db-01", success=True, retcode=0, payload={})
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
        session.add(
            Minion(id="fresh-01", grains={}, conformity={}, key_status="accepted")
        )
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


def _seed_sync_scenario(app, *, job_started_ago_min=10, ret_ago_min=5):
    """A completed state job targeting web-* with one return (web-01).

    web-02 is targeted-but-silent; db-01 is outside the target."""
    from overstate_ui.jobs_service import sync_job  # noqa: F401 (re-export check)

    with app.app_context():
        session = get_session()
        now = dt.datetime.now(dt.UTC)
        old = now - dt.timedelta(days=1)
        session.add(
            Minion(
                id="web-01",
                grains={},
                conformity={"status": "ok", "jid": "oldjob"},
                key_status="accepted",
            )
        )
        session.add(
            Minion(
                id="web-02",
                grains={},
                conformity={"status": "ok", "jid": "oldjob"},
                key_status="accepted",
            )
        )
        session.add(
            Minion(
                id="db-01",
                grains={},
                conformity={"status": "ok", "jid": "oldjob"},
                key_status="accepted",
            )
        )
        session.add(
            Job(
                jid="oldjob",
                fun="state.highstate",
                tgt="*",
                tgt_type="glob",
                user="u",
                started_at=old,
            )
        )
        session.add(
            Job(
                jid="new",
                fun="state.highstate",
                tgt="web-*",
                tgt_type="glob",
                user="u",
                started_at=now - dt.timedelta(minutes=job_started_ago_min),
            )
        )
        session.add(
            SaltReturn(
                fun="state.highstate",
                jid="new",
                minion_id="web-01",
                success="True",
                payload={},
                full_ret={},
                alter_time=now - dt.timedelta(minutes=ret_ago_min),
            )
        )
        session.commit()


def test_sync_stamps_returns_and_marks_targeted_silent_unreachable():
    from overstate_ui.jobs_service import sync_job

    app = _app()
    _seed_sync_scenario(app)
    with app.app_context():
        sync_job("new")
        covered = get_session().get(Minion, "web-01")
        assert covered.conformity["status"] == "ok"
        assert covered.conformity["jid"] == "new"
        assert covered.conformity["targeted"] is True
        assert "checked_at" in covered.conformity
        silent = get_session().get(Minion, "web-02")
        assert silent.conformity["status"] == "unreachable"
        assert silent.conformity["jid"] == "new"
        outside = get_session().get(Minion, "db-01")
        assert outside.conformity == {"status": "ok", "jid": "oldjob"}
        trail = (
            get_session()
            .query(StateConformityHistory)
            .filter_by(minion_id="web-02")
            .all()
        )
        assert len(trail) == 1 and trail[0].status == "unreachable"


def test_history_trail_capped_per_minion():
    from overstate_ui.states import HISTORY_LIMIT, _record_history

    app = _app()
    with app.app_context():
        session = get_session()
        now = dt.datetime.now(dt.UTC)
        for i in range(HISTORY_LIMIT + 2):
            _record_history(session, "m-01", f"jid-{i:02d}", "ok", now)
        session.commit()
        rows = (
            session.query(StateConformityHistory)
            .filter_by(minion_id="m-01")
            .order_by(StateConformityHistory.id)
            .all()
        )
        assert len(rows) == HISTORY_LIMIT
        assert rows[0].jid == "jid-02"  # oldest pruned, newest kept


def test_sync_young_job_never_marks_unreachable():
    from overstate_ui.jobs_service import sync_job

    app = _app()
    _seed_sync_scenario(app, job_started_ago_min=0, ret_ago_min=0)
    with app.app_context():
        sync_job("new")
        silent = get_session().get(Minion, "web-02")
        assert silent.conformity == {"status": "ok", "jid": "oldjob"}


def test_sync_older_job_never_clobbers_newer_verdict():
    from overstate_ui.jobs_service import sync_job

    app = _app()
    with app.app_context():
        session = get_session()
        now = dt.datetime.now(dt.UTC)
        session.add(
            Minion(
                id="m-01",
                grains={},
                conformity={"status": "ok", "jid": "newer"},
                key_status="accepted",
            )
        )
        session.add(
            Job(
                jid="newer",
                fun="state.highstate",
                tgt="m-01",
                tgt_type="list",
                user="u",
                started_at=now - dt.timedelta(minutes=1),
            )
        )
        session.add(
            Job(
                jid="older",
                fun="state.highstate",
                tgt="m-*",
                tgt_type="glob",
                user="u",
                started_at=now - dt.timedelta(hours=1),
            )
        )
        session.add(
            SaltReturn(
                fun="state.highstate",
                jid="older",
                minion_id="m-01",
                success="False",
                payload={},
                full_ret={},
                alter_time=now - dt.timedelta(minutes=30),
            )
        )
        session.commit()
        sync_job("older")
        assert get_session().get(Minion, "m-01").conformity == {
            "status": "ok",
            "jid": "newer",
        }


def _seed_watched_job(app, watched, payload, success=True):
    with app.app_context():
        session = get_session()
        now = dt.datetime.now(dt.UTC)
        for sls in watched:
            session.add(WatchedState(sls=sls))
        session.add(Minion(id="w-01", grains={}, conformity={}, key_status="accepted"))
        session.add(
            Job(
                jid="wj",
                fun="state.highstate",
                tgt="w-01",
                tgt_type="list",
                user="u",
                started_at=now,
            )
        )
        session.add(
            JobReturn(
                jid="wj",
                minion_id="w-01",
                success=success,
                retcode=0,
                payload=payload,
            )
        )
        session.commit()


def test_watched_failure_narrows_whole_success_to_drifted():
    app = _app()
    _seed_watched_job(
        app,
        ["web"],
        {
            "web_nginx": {"result": False, "__sls__": "web", "changes": {}},
            "other_base": {"result": True, "__sls__": "other", "changes": {}},
        },
        success=True,
    )
    with app.app_context():
        assert recompute_conformity() == "wj"
        assert get_session().get(Minion, "w-01").conformity == {
            "status": "drifted",
            "jid": "wj",
        }


def test_watched_clean_stays_ok():
    app = _app()
    _seed_watched_job(
        app,
        ["web"],
        {"web_nginx": {"result": True, "__sls__": "web", "changes": {}}},
        success=True,
    )
    with app.app_context():
        assert recompute_conformity() == "wj"
        assert get_session().get(Minion, "w-01").conformity == {
            "status": "ok",
            "jid": "wj",
        }


def test_unknown_watch_name_falls_back_to_whole_job():
    app = _app()
    _seed_watched_job(
        app,
        ["nope"],
        {"web_nginx": {"result": True, "__sls__": "web", "changes": {}}},
        success=False,
    )
    with app.app_context():
        assert recompute_conformity() == "wj"
        assert get_session().get(Minion, "w-01").conformity == {
            "status": "drifted",
            "jid": "wj",
        }


def test_sync_partial_flag_when_payload_cannot_narrow():
    from overstate_ui.jobs_service import sync_job

    app = _app()
    with app.app_context():
        session = get_session()
        now = dt.datetime.now(dt.UTC)
        session.add(WatchedState(sls="web"))
        session.add(Minion(id="w-01", grains={}, conformity={}, key_status="accepted"))
        session.add(
            Job(
                jid="wj",
                fun="state.highstate",
                tgt="w-01",
                tgt_type="list",
                user="u",
                started_at=now,
            )
        )
        session.add(
            SaltReturn(
                fun="state.highstate",
                jid="wj",
                minion_id="w-01",
                success="True",
                payload={},
                full_ret={},
                alter_time=now,
            )
        )
        session.commit()
        sync_job("wj")
        conformity = get_session().get(Minion, "w-01").conformity
        assert conformity["status"] == "ok"
        assert conformity["partial"] is True


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


def test_conformity_minion_links_to_detail():
    app = _app()
    with app.app_context():
        get_session().add(
            Minion(
                id="web-01",
                grains={},
                conformity={"status": "ok", "jid": "old"},
                key_status="accepted",
            )
        )
        get_session().commit()
    client = app.test_client()
    client.post("/login", data={"username": "admin", "password": "pw"})
    states = client.get("/states/").data.decode()
    assert 'href="/minions/web-01"' in states
