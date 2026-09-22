"""UX fixes: states + schedules confirms, days rendering, role gates, badges."""

import httpx

from overstate_ui import auth as authmod
from overstate_ui import create_app
from overstate_ui.auth import seed_admin
from overstate_ui.config import TestConfig
from overstate_ui.db import create_all, get_session, init_db
from overstate_ui.models import Job, JobReturn, Minion, User
from overstate_ui.salt_client import SaltClient
from overstate_ui.seed_mock import seed as seed_mock

PW = "test-password"


def _transport(schedule_entries):
    import json

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
            return httpx.Response(
                200, json={"return": [{"up": ["web-01"], "down": []}]}
            )
        if body.get("client") == "local" and body.get("fun") == "schedule.list":
            return httpx.Response(200, json={"return": [{"web-01": schedule_entries}]})
        return httpx.Response(200, json={"return": [{"web-01": {}}]})

    return httpx.MockTransport(handler)


def _app(schedule_entries=None):
    entries = (
        schedule_entries
        if schedule_entries is not None
        else {
            "daily": {
                "function": "state.highstate",
                "seconds": 86400,
                "enabled": True,
            }
        }
    )
    init_db("sqlite://")
    app = create_app(TestConfig)
    app.config["WTF_CSRF_ENABLED"] = False
    app.extensions["salt_client"] = SaltClient(
        "https://salt:8000", "u", "p", transport=_transport(entries)
    )
    with app.app_context():
        create_all()
        seed_admin(password=PW)
        session = get_session()
        session.add(
            User(username="op", password_hash=authmod._ph.hash(PW), role="operator")
        )
        session.add(
            User(username="vwr", password_hash=authmod._ph.hash(PW), role="viewer")
        )
        seed_mock(session)  # seeds WatchedState common + baseline
        session.add(
            Minion(id="ok-01", grains={}, conformity={"status": "ok", "jid": "j1"})
        )
        session.add(
            Minion(
                id="drift-01",
                grains={},
                conformity={"status": "drifted", "jid": "j2"},
            )
        )
        session.add(Minion(id="new-01", grains={}, conformity={"status": "unknown"}))
        session.commit()
    return app


def _login(client, username):
    client.post("/logout")
    client.post("/login", data={"username": username, "password": PW})


def test_unwatch_form_has_data_confirm():
    app = _app()
    client = app.test_client()
    _login(client, "op")
    html = client.get("/states/").data.decode()
    assert "data-confirm=" in html
    assert "Stop watching" in html


def test_schedule_add_rejects_disallowed_function():
    app = _app()
    client = app.test_client()
    _login(client, "op")
    rv = client.post(
        "/schedules/web-01/add",
        data={
            "name": "evil",
            "function": "cmd.run",
            "unit": "seconds",
            "value": "60",
        },
        follow_redirects=True,
    )
    assert "cannot run here" in rv.data.decode()


def test_schedule_add_accepts_allowed_function():
    app = _app()
    client = app.test_client()
    _login(client, "op")
    rv = client.post(
        "/schedules/web-01/add",
        data={
            "name": "pingy",
            "function": "test.ping",
            "unit": "seconds",
            "value": "60",
        },
        follow_redirects=True,
    )
    assert "did not confirm the add" in rv.data.decode()


def test_schedule_delete_disable_forms_have_data_confirm():
    app = _app()
    client = app.test_client()
    _login(client, "op")
    html = client.get("/schedules/").data.decode()
    assert html.count("data-confirm=") >= 2
    assert "Delete job" in html
    assert "Disable job" in html


def test_schedule_days_rendered_instead_of_dash():
    app = _app(
        {
            "weekly": {
                "function": "state.highstate",
                "days": 2,
                "enabled": True,
            }
        }
    )
    client = app.test_client()
    _login(client, "op")
    html = client.get("/schedules/").data.decode()
    assert "weekly" in html
    assert ">2<" in html


def test_viewer_sees_no_action_buttons_states_and_schedules():
    app = _app()
    client = app.test_client()
    _login(client, "vwr")
    states = client.get("/states/").data.decode()
    assert "Unwatch" not in states
    assert "Stop watching" not in states
    assert "Recompute" not in states
    assert "e.g. baseline" not in states
    schedules = client.get("/schedules/").data.decode()
    assert "Add schedule" not in schedules
    # Button text only: the "Enabled" column header stays visible.
    assert ">Enable<" not in schedules
    assert ">Disable<" not in schedules
    assert ">Delete<" not in schedules


def test_operator_sees_action_buttons():
    app = _app()
    client = app.test_client()
    _login(client, "op")
    states = client.get("/states/").data.decode()
    assert "Stop watching" in states  # unwatch × chip, confirm included
    assert "Recompute" in states
    schedules = client.get("/schedules/").data.decode()
    assert "Add schedule" in schedules
    assert "Delete" in schedules


def _seed_stored_state(app, mid="web-01"):
    with app.app_context():
        session = get_session()
        session.add(
            Job(
                jid="202609200000000001",
                fun="state.apply",
                tgt=mid,
                tgt_type="list",
                user="op",
                complete=True,
            )
        )
        session.add(
            JobReturn(
                jid="202609200000000001",
                minion_id=mid,
                success=True,
                payload={"mystate": {"result": True, "comment": "ok"}},
            )
        )
        session.commit()


def test_minion_raw_tab_is_own_panel():
    """Raw JSON lives on the Raw tab, not inside per-tab accordions."""
    app = _app()
    _seed_stored_state(app)
    client = app.test_client()
    _login(client, "op")
    html = client.get("/minions/web-01", query_string={"tab": "raw"}).data.decode()
    assert 'tab-active">Raw' in html
    assert "Advanced diagnostic dump" in html
    assert "mystate" in html  # stored payload, uncollapsed
    assert "state.highstate" in html  # schedule payload, uncollapsed
    assert "Raw JSON (advanced)" not in html
    assert "Raw live data" not in html
    assert "Raw stored return" not in html


def test_minion_tabs_have_no_raw_accordions():
    """States, schedule, and pillar tabs render without raw accordions."""
    app = _app()
    _seed_stored_state(app)
    client = app.test_client()
    _login(client, "op")
    for tab in ("states", "schedule", "pillar"):
        html = client.get("/minions/web-01", query_string={"tab": tab}).data.decode()
        assert "Raw JSON (advanced)" not in html
        assert "Raw live data" not in html
        assert "Raw stored return" not in html
    # ...while the pretty surfaces survive the move.
    states = client.get("/minions/web-01", query_string={"tab": "states"}).data.decode()
    assert "Last stored run" in states


def test_minion_raw_tab_empty_states():
    """A minion with nothing stored gets guidance, not blank JSON."""
    app = _app()
    client = app.test_client()
    _login(client, "op")
    html = client.get("/minions/ok-01", query_string={"tab": "raw"}).data.decode()
    assert "No stored state run" in html
    assert "No pillar data" in html


def test_conformity_badges_have_accessible_labels_without_semantic_change():
    app = _app()
    client = app.test_client()
    _login(client, "op")
    html = client.get("/states/").data.decode()
    assert 'aria-label="Conformity: ok"' in html
    assert 'aria-label="Conformity: drifted"' in html
    assert 'aria-label="Conformity: unknown"' in html
    # semantics preserved: same badge classes per status
    assert "badge-success" in html
    assert "badge-error" in html
    assert "badge-neutral" in html
