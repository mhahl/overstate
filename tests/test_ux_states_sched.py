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


def test_schedules_search_and_pages_with_size_dropdown():
    entries = {
        f"job-{i:02d}": {
            "function": "test.ping" if i % 2 else "state.highstate",
            "seconds": 60,
            "enabled": True,
        }
        for i in range(60)
    }
    app = _app(entries)
    client = app.test_client()
    _login(client, "op")
    html = client.get("/schedules/?per_page=10").data.decode()
    assert html.count('class="font-mono text-xs">job-') == 10
    assert 'name="per_page"' in html
    page2 = client.get("/schedules/?per_page=10&page=2").data.decode()
    assert "job-10" in page2 and "job-00" not in page2
    searched = client.get("/schedules/?q=highstate").data.decode()
    assert "job-00" in searched and "job-01" not in searched


def test_states_pages_with_size_dropdown():
    from overstate_ui.db import get_session
    from overstate_ui.models import Minion

    app = _app()
    client = app.test_client()
    _login(client, "op")
    with app.app_context():
        session = get_session()
        for i in range(8):
            session.add(Minion(id=f"extra-{i:02d}", grains={}))
        session.commit()
    html = client.get("/states/?per_page=10").data.decode()
    assert 'name="per_page"' in html
    page2 = client.get("/states/?per_page=10&page=2").data.decode()
    assert "Page 2 of 2" in page2


def test_watch_unknown_sls_warns_and_marks_missing():
    app = _app()
    client = app.test_client()
    _login(client, "op")
    rv = client.post(
        "/states/watch", data={"sls": "no-such-state"}, follow_redirects=True
    )
    html = rv.data.decode()
    assert "no state file matches it" in html
    assert "missing" in html  # the missing badge on the watch list
    with app.app_context():
        from overstate_ui.db import get_session
        from overstate_ui.models import WatchedState

        assert get_session().query(WatchedState).filter_by(sls="no-such-state").first()


def test_watch_known_sls_no_warning():
    app = _app()
    client = app.test_client()
    _login(client, "op")
    rv = client.post("/states/watch", data={"sls": "top"}, follow_redirects=True)
    html = rv.data.decode()
    assert "no state file matches it" not in html
    assert "Watching" in html and "top" in html


def test_conformity_missed_when_up_but_silent(fake_redis):
    from overstate_ui.tasks_queue import write_merged_roster_cache

    app = _app()
    with app.app_context():
        get_session().add(
            Minion(
                id="silent-01",
                grains={},
                conformity={"status": "unreachable", "jid": "j9"},
            )
        )
        get_session().commit()
    # Roster cache says silent-01 is up: the verdict renders as missed
    # (seed_mock also seeds an unreachable minion, so only the missed
    # badge proves the split).
    write_merged_roster_cache({"silent-01": "accepted"}, ["silent-01"])
    client = app.test_client()
    _login(client, "op")
    html = client.get("/states/").data.decode()
    assert 'aria-label="Conformity: missed"' in html


def test_conformity_unreachable_when_down(fake_redis):
    from overstate_ui.tasks_queue import write_merged_roster_cache

    app = _app()
    with app.app_context():
        get_session().add(
            Minion(
                id="down-01",
                grains={},
                conformity={"status": "unreachable", "jid": "j9"},
            )
        )
        get_session().commit()
    write_merged_roster_cache({"down-01": "accepted"}, [])
    client = app.test_client()
    _login(client, "op")
    html = client.get("/states/").data.decode()
    assert 'aria-label="Conformity: unreachable"' in html


def test_conformity_stale_badge_when_verdict_old():
    import datetime as dt

    app = _app()
    with app.app_context():
        get_session().add(
            Minion(
                id="old-01",
                grains={},
                conformity={
                    "status": "ok",
                    "jid": "j0",
                    "checked_at": (
                        dt.datetime.now(dt.UTC) - dt.timedelta(days=2)
                    ).isoformat(),
                },
            )
        )
        get_session().commit()
    client = app.test_client()
    _login(client, "op")
    html = client.get("/states/").data.decode()
    assert "stale" in html  # verdict older than a day is flagged


def test_drifted_row_offers_rerun_link():
    app = _app()
    client = app.test_client()
    _login(client, "op")
    html = client.get("/states/").data.decode()
    assert "Re-run" in html  # drifted rows prefill a state.apply on the minion


def test_schedule_inventory_lists_entries_with_owner(fake_redis):
    from overstate_ui.models import AuditEvent
    from overstate_ui.tasks_queue import write_merged_roster_cache

    write_merged_roster_cache({"web-01": "accepted"}, ["web-01"])
    app = _app()
    with app.app_context():
        get_session().add(
            AuditEvent(user="alice", action="schedule-add:daily", minion_id="web-01")
        )
        get_session().commit()
    client = app.test_client()
    _login(client, "op")
    html = client.get("/schedules/all").data.decode()
    assert "web-01" in html
    assert "state.highstate" in html
    assert "alice" in html
    # Delete from the inventory uses the shared per-minion act route.
    rv = client.post(
        "/schedules/web-01/delete", data={"job": "daily"}, follow_redirects=True
    )
    assert "deleted" in rv.data.decode().lower()


def test_schedule_inventory_fleet_gated(fake_redis):
    """Scoped schedule.read without a fleet grant cannot enumerate."""
    from overstate_ui import auth as authmod
    from overstate_ui.models import Setting, User
    from overstate_ui.tasks_queue import write_merged_roster_cache

    write_merged_roster_cache({"web-01": "accepted"}, ["web-01"])
    app = _app()
    with app.app_context():
        get_session().add(Setting(key="rbac_mode", value="scoped"))
        get_session().add(
            User(
                username="teamop",
                password_hash=authmod._ph.hash(PW),
                role="scoped",
            )
        )
        get_session().commit()
    client = app.test_client()
    _login(client, "teamop")
    assert client.get("/schedules/all").status_code == 403


def test_schedule_add_cron_mode():
    """The add form accepts a five-field cron expression and ships it
    as the schedule's cron kwarg instead of an interval."""
    import json

    calls = []

    def transport():
        import httpx

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/login":
                return httpx.Response(200, json={"return": [{"token": "tok", "expire": 99}]})
            body = json.loads(request.content or b"{}")
            if body.get("client") == "wheel":
                return httpx.Response(
                    200,
                    json={"return": [{"data": {"return": {"minions": ["web-01"], "minions_pre": []}}}]},
                )
            if body.get("client") == "local" and body.get("fun") == "schedule.add":
                calls.append(body.get("kwarg"))
                return httpx.Response(200, json={"return": [{"web-01": True}]})
            return httpx.Response(200, json={"return": [{"web-01": {}}]})

        return httpx.MockTransport(handler)

    init_db("sqlite://")
    app = create_app(TestConfig)
    app.config["WTF_CSRF_ENABLED"] = False
    app.extensions["salt_client"] = SaltClient("https://salt:8000", "u", "p", transport=transport())
    with app.app_context():
        create_all()
        seed_admin(password=PW)
        get_session().add(
            User(username="op", password_hash=authmod._ph.hash(PW), role="operator")
        )
        get_session().add(Minion(id="web-01", grains={}, conformity={}))
        get_session().commit()
    client = app.test_client()
    _login(client, "op")
    rv = client.post(
        "/schedules/web-01/add",
        data={"name": "daily-3am", "function": "state.highstate", "mode": "cron", "cron": "0 3 * * *"},
        follow_redirects=True,
    )
    assert calls and calls[-1]["cron"] == ["0 3 * * *"]
    assert "added" in rv.data.decode().lower()
    # Invalid cron is rejected without touching salt.
    before = len(calls)
    rv = client.post(
        "/schedules/web-01/add",
        data={"name": "bad", "function": "test.ping", "mode": "cron", "cron": "61 * * * *"},
        follow_redirects=True,
    )
    assert "Invalid cron expression" in rv.data.decode()
    assert len(calls) == before


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


def test_schedule_act_rejects_glob_mid():
    """A forged wildcard id must 404, not fan schedule.delete out to the
    fleet. Every other single-minion call targets tgt_type list + exact
    id; schedules.act was the outlier on both."""
    app = _app()
    client = app.test_client()
    _login(client, "op")
    assert client.post("/schedules/*/delete", data={"job": "daily"}).status_code == 404
    assert client.post("/schedules/*/add").status_code == 404


def test_schedule_act_without_minion_response_warns():
    """No entry for the minion in the return means Salt did not confirm
    anything: warn instead of flashing success."""
    app = _app()
    client = app.test_client()
    _login(client, "op")
    rv = client.post(
        "/schedules/ghost-01/delete", data={"job": "daily"}, follow_redirects=True
    )
    html = rv.data.decode()
    assert "no response" in html
    assert "deleted." not in html


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


def test_schedules_index_skips_not_up_minion(monkeypatch, fake_redis):
    from overstate_ui.salt_client import SaltApiError
    from overstate_ui.tasks_queue import write_roster_cache

    app = _app()
    client = app.test_client()
    _login(client, "op")
    write_roster_cache({"web-01": "accepted"}, set())

    def _boom(*args, **kwargs):
        raise SaltApiError("live call must be skipped")

    monkeypatch.setattr(app.extensions["salt_client"], "local", _boom)
    html = client.get("/schedules/?minion=web-01").data.decode()
    assert "not responding" in html


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
    assert "Diagnostic dump" in html
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
