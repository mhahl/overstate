"""States page + minion detail: stored-first rendering, filter, refresh."""

import datetime as dt
import json

import httpx

from overstate_ui import auth as authmod
from overstate_ui import create_app
from overstate_ui.auth import seed_admin
from overstate_ui.config import TestConfig
from overstate_ui.db import create_all, get_session, init_db
from overstate_ui.models import Job, JobReturn, Minion, User
from overstate_ui.salt_client import SaltClient

FAIL_SHOW_HIGHSTATE = False

STORED = {
    "nginx": {
        "result": True,
        "__sls__": "web",
        "comment": "already managed",
        "changes": {},
    },
    "broken": {
        "result": False,
        "__sls__": "web",
        "comment": "failed to apply",
        "changes": {},
    },
    "cmd_|-trivy_binary_|-tar -xzf x_|-run": {
        "__id__": "trivy_binary",
        "result": True,
        "__sls__": "baseline.trivy",
        "comment": "already fetched",
        "name": "tar -xzf x",
        "changes": {},
    },
}

LIVE = {
    "live_state": {
        "result": True,
        "__sls__": "live",
        "comment": "fresh from minion",
        "changes": {},
    }
}


def fake_transport() -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/login":
            return httpx.Response(
                200, json={"return": [{"token": "tok", "expire": 99}]}
            )
        body = json.loads(request.content or b"{}")
        if body.get("fun") == "state.show_highstate":
            if FAIL_SHOW_HIGHSTATE:
                return httpx.Response(500, json={})
            return httpx.Response(200, json={"return": [{"w-01": LIVE}]})
        return httpx.Response(200, json={"return": [{}]})

    return httpx.MockTransport(handler)


def make_client():
    init_db("sqlite://")
    app = create_app(TestConfig)
    app.config["WTF_CSRF_ENABLED"] = False
    app.extensions["salt_client"] = SaltClient(
        "https://salt:8000", "u", "p", transport=fake_transport()
    )
    with app.app_context():
        create_all()
        seed_admin(password="pw")
        session = get_session()
        session.add(
            User(username="vie", password_hash=authmod._ph.hash("pw"), role="viewer")
        )
        now = dt.datetime.now(dt.UTC)
        session.add(
            Minion(
                id="w-01",
                grains={},
                conformity={
                    "status": "drifted",
                    "jid": "J1",
                    "checked_at": now.isoformat(),
                    "targeted": True,
                },
                key_status="accepted",
            )
        )
        session.add(
            Job(
                jid="J1",
                fun="state.highstate",
                tgt="w-01",
                tgt_type="list",
                user="u",
                started_at=now,
            )
        )
        session.add(
            JobReturn(
                jid="J1", minion_id="w-01", success=False, retcode=1, payload=STORED
            )
        )
        session.commit()
    c = app.test_client()
    c.post("/login", data={"username": "admin", "password": "pw"})
    c.app = app
    return c


def test_index_shows_checked_and_history_and_filters():
    html = make_client().get("/states/").data.decode()
    assert "drifted" in html
    assert "J1" in html  # source link + history dot title
    assert "partial" not in html
    filtered = make_client().get("/states/", query_string={"status": "ok"})
    assert "w-01" not in filtered.data.decode()
    kept = make_client().get("/states/", query_string={"status": "drifted"})
    assert "w-01" in kept.data.decode()


def test_states_page_uses_a_conformity_control_room_layout():
    html = make_client().get("/states/").data.decode()
    assert 'aria-label="Fleet conformity summary"' in html
    assert 'aria-label="Conformity ledger"' in html
    assert 'aria-label="Watched SLS coverage"' in html
    assert "How verdicts work" in html
    assert "Recompute verdicts" in html


def test_conformity_summary_counts_statuses_and_partial():
    from types import SimpleNamespace

    from overstate_ui.states import summarize_conformity

    rows = [
        SimpleNamespace(conformity={"status": "ok"}),
        SimpleNamespace(conformity={"status": "drifted", "partial": True}),
        SimpleNamespace(conformity={"status": "failed"}),
        SimpleNamespace(conformity={"status": "unreachable"}),
        SimpleNamespace(conformity={}),
    ]
    assert summarize_conformity(rows) == {
        "total": 5,
        "ok": 1,
        "drifted": 2,
        "unreachable": 1,
        "unknown": 1,
        "partial": 1,
    }


def test_states_summary_stays_scope_wide_and_failed_rows_filter_as_drifted():
    from overstate_ui.models import WatchedState

    client = make_client()
    with client.app.app_context():
        session = get_session()
        current = session.get(Minion, "w-01")
        current.conformity = {**current.conformity, "partial": True}
        session.add_all(
            [
                Minion(id="ok-01", grains={}, conformity={"status": "ok"}),
                Minion(
                    id="down-01", grains={}, conformity={"status": "unreachable"}
                ),
                Minion(id="new-01", grains={}, conformity={}),
                Minion(id="legacy-01", grains={}, conformity={"status": "failed"}),
                WatchedState(sls="baseline.nginx"),
            ]
        )
        session.commit()

    html = client.get("/states/?status=drifted").data.decode()
    assert 'data-summary="total">5<' in html
    assert 'data-summary="ok">1<' in html
    assert 'data-summary="drifted">2<' in html
    assert 'data-summary="unreachable">1<' in html
    assert 'data-summary="unknown">1<' in html
    assert 'data-summary="partial">1<' in html
    assert "w-01" in html and "legacy-01" in html
    assert "ok-01" not in html and "down-01" not in html and "new-01" not in html


def test_states_page_uses_a_conformity_control_room_layout():
    html = make_client().get("/states/").data.decode()
    assert 'aria-label="Fleet conformity summary"' in html
    assert 'aria-label="Conformity ledger"' in html
    assert 'aria-label="Watched SLS coverage"' in html
    assert "How verdicts work" in html
    assert "Recompute verdicts" in html


def test_conformity_summary_groups_legacy_failures_and_counts_partial():
    from types import SimpleNamespace

    from overstate_ui.states import summarize_conformity

    rows = [
        SimpleNamespace(conformity={"status": "ok"}),
        SimpleNamespace(conformity={"status": "drifted", "partial": True}),
        SimpleNamespace(conformity={"status": "failed"}),
        SimpleNamespace(conformity={"status": "unreachable"}),
        SimpleNamespace(conformity={}),
    ]
    assert summarize_conformity(rows) == {
        "total": 5,
        "ok": 1,
        "drifted": 2,
        "unreachable": 1,
        "unknown": 1,
        "partial": 1,
    }


def test_minion_states_tab_renders_stored_without_live_call():
    global FAIL_SHOW_HIGHSTATE
    FAIL_SHOW_HIGHSTATE = True  # any live fan-out would 500; page must not call
    try:
        html = make_client().get("/minions/w-01", query_string={"tab": "states"})
    finally:
        FAIL_SHOW_HIGHSTATE = False
    body = html.data.decode()
    assert html.status_code == 200
    assert "nginx" in body and "broken" in body
    assert "trivy_binary: tar -xzf x" in body  # human label, not the raw tag
    assert "Refresh from minion" in body
    assert "J1" in body


def test_states_refresh_success_shows_live_not_stored_marker():
    c = make_client()
    rv = c.post("/minions/w-01/states/refresh")
    body = rv.data.decode()
    assert rv.status_code == 200
    assert "live_state" in body
    assert "not stored" in body
    assert "nginx" in body  # stored section still rendered


def test_states_refresh_failure_degrades_to_stored_with_note():
    global FAIL_SHOW_HIGHSTATE
    FAIL_SHOW_HIGHSTATE = True
    try:
        rv = make_client().post("/minions/w-01/states/refresh")
    finally:
        FAIL_SHOW_HIGHSTATE = False
    body = rv.data.decode()
    assert rv.status_code == 200
    assert "Live refresh unavailable" in body
    assert "nginx" in body  # stored data survives


def test_states_refresh_viewer_forbidden():
    c = make_client()
    c.post("/logout")
    c.post("/login", data={"username": "vie", "password": "pw"})
    assert c.post("/minions/w-01/states/refresh").status_code == 403


def test_state_mutations_leave_audit_rows():
    from overstate_ui.models import AuditEvent

    c = make_client()
    c.post("/states/watch", data={"sls": "web"})
    c.post("/states/recompute")
    c.post("/minions/w-01/states/refresh")
    with c.app.app_context():
        actions = [row.action for row in get_session().query(AuditEvent).all()]
    assert "watch:web" in actions
    assert any(a.startswith("states-recompute:") for a in actions)
    assert "states-refresh:w-01" in actions
