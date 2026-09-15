"""Phase 6 tests: settings, states, schedules, events, CSV export."""

import httpx
import pytest

from overstate_ui import create_app
from overstate_ui.auth import seed_admin
from overstate_ui.config import TestConfig
from overstate_ui.db import create_all, get_session, init_db
from overstate_ui.events import match_prefixes
from overstate_ui.models import WatchedState
from overstate_ui.salt_client import SaltClient
from overstate_ui.seed_mock import seed as seed_mock
from overstate_ui.settings import get_setting

_ADD_CALLS: list = []


def fake_transport() -> httpx.MockTransport:
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
        if body.get("client") == "local":
            if body.get("fun") == "schedule.list":
                return httpx.Response(
                    200,
                    json={
                        "return": [
                            {
                                "web-01": {
                                    "daily": {
                                        "function": "state.highstate",
                                        "seconds": 86400,
                                        "enabled": True,
                                    }
                                }
                            }
                        ]
                    },
                )
            if body.get("fun") == "schedule.add":
                _ADD_CALLS.append(body)
                return httpx.Response(200, json={"return": [{"web-01": True}]})
            if body.get("fun") == "schedule.delete":
                return httpx.Response(200, json={"return": [{"web-01": {}}]})
            return httpx.Response(200, json={"return": [{"web-01": {}}]})
        return httpx.Response(200, json={"return": [{}]})

    return httpx.MockTransport(handler)


@pytest.fixture()
def client():
    init_db("sqlite://")
    app = create_app(TestConfig)
    app.config["WTF_CSRF_ENABLED"] = False
    app.extensions["salt_client"] = SaltClient(
        "https://salt:8000", "u", "p", transport=fake_transport()
    )
    with app.app_context():
        create_all()
        seed_admin(password="pw")
        seed_mock(get_session())
    c = app.test_client()
    c.post("/login", data={"username": "admin", "password": "pw"})
    c.app = app
    return c


def test_settings_save_and_default(client):
    with client.app.app_context():
        assert get_setting("theme") == "wireframe"
    rv = client.post(
        "/settings/",
        data={"default_target": "web-*", "page_size": "10", "theme": "dark"},
    )
    assert rv.status_code == 302
    with client.app.app_context():
        assert get_setting("theme") == "dark"
        assert get_setting("page_size") == "10"
    assert 'data-theme="dark"' in client.get("/").data.decode()


def test_settings_invalid_options_fall_back_to_default(client):
    rv = client.post(
        "/settings/",
        data={"default_target": "*", "page_size": "999", "theme": "midnight"},
    )
    assert rv.status_code == 302
    with client.app.app_context():
        assert get_setting("theme") == "wireframe"
        assert get_setting("page_size") == "25"


def test_settings_page_read_only_for_viewers(client):
    from overstate_ui.auth import _ph
    from overstate_ui.models import User

    with client.app.app_context():
        get_session().add(
            User(username="viewer", role="viewer", password_hash=_ph.hash("pw"))
        )
        get_session().commit()
    viewer = client.app.test_client()
    viewer.post("/login", data={"username": "viewer", "password": "pw"})
    html = viewer.get("/settings/").data.decode()
    assert "Only admins can change settings." in html
    assert "disabled" in html
    assert viewer.post("/settings/", data={}).status_code == 403


def test_unwatch_flashes_confirmation(client):
    from overstate_ui.models import WatchedState

    with client.app.app_context():
        wid = get_session().query(WatchedState).filter_by(sls="common").one().id
    rv = client.post(f"/states/unwatch/{wid}")
    assert rv.status_code == 302
    assert "Stopped watching" in client.get(rv.headers["Location"]).data.decode()
    rv = client.post("/states/unwatch/999999")
    assert "Nothing to unwatch" in client.get(rv.headers["Location"]).data.decode()


def test_watch_duplicate_flashes_info(client):
    rv = client.post("/states/watch", data={"sls": "common"})
    assert rv.status_code == 302
    assert "Already watching" in client.get(rv.headers["Location"]).data.decode()


def test_every_submit_shows_pending_state(client):
    html = client.get("/states/").data.decode()
    assert "data-pending-spinner" in html
    assert "htmx:afterRequest" in html
    assert "getAttribute('method') === 'dialog'" in html
    assert "data-download" in client.get("/minions/").data.decode()


def test_states_watch_recompute(client):
    rv = client.post("/states/watch", data={"sls": "webserver"})
    assert rv.status_code == 302
    with client.app.app_context():
        assert get_session().query(WatchedState).count() == 3  # 2 seeded + 1
    rv = client.post("/states/recompute")
    assert rv.status_code == 302
    html = client.get("/states/").data.decode()
    assert "drifted" in html  # seeded highstate had a failure
    assert "webserver" in html


def test_schedules_action_empty_job_rejected(client):
    with client.app.app_context():
        from overstate_ui.models import AuditEvent

        before = (
            get_session()
            .query(AuditEvent)
            .filter(AuditEvent.action.like("schedule-%"))
            .count()
        )
    rv = client.post("/schedules/web-01/delete", data={"job": ""})
    assert rv.status_code == 302
    assert "Pick a scheduled job" in client.get(rv.headers["Location"]).data.decode()
    with client.app.app_context():
        assert (
            get_session()
            .query(AuditEvent)
            .filter(AuditEvent.action.like("schedule-%"))
            .count()
            == before
        )


def test_schedules_action_failed_return_flashes_error(client, monkeypatch):
    salt = client.app.extensions["salt_client"]
    monkeypatch.setattr(
        salt,
        "local",
        lambda *a, **k: [{"web-01": {"result": False, "comment": "nope"}}],
    )
    rv = client.post("/schedules/web-01/delete", data={"job": "daily"})
    assert rv.status_code == 302
    html = client.get(rv.headers["Location"]).data.decode()
    assert "nope" in html


def test_schedules_index_and_actions(client):
    html = client.get("/schedules/").data.decode()
    assert "daily" in html and "state.highstate" in html
    for action in ("enable", "disable", "delete"):
        rv = client.post(f"/schedules/web-01/{action}", data={"job": "daily"})
        assert rv.status_code == 302
    with client.app.app_context():
        from overstate_ui.models import AuditEvent

        assert (
            get_session()
            .query(AuditEvent)
            .filter(AuditEvent.action.like("schedule-%"))
            .count()
            == 3
        )


def test_schedules_sort_headers_carry_minion(client):
    html = client.get("/schedules/").data.decode()
    assert 'aria-sort="asc"' in html
    assert "sort=function" in html and "minion=web-01" in html
    html = client.get("/schedules/?sort=bogus").data.decode()
    assert 'aria-sort="asc"' in html  # invalid falls back to name


def test_schedules_add_form_visible(client):
    html = client.get("/schedules/").data.decode()
    assert "Add schedule" in html
    assert 'name="function"' in html and 'name="unit"' in html


def test_schedules_add_success(client):
    _ADD_CALLS.clear()
    rv = client.post(
        "/schedules/web-01/add",
        data={
            "name": "hourly",
            "function": "test.ping",
            "value": "60",
            "unit": "minutes",
            "enabled": "on",
        },
    )
    assert rv.status_code == 302
    html = client.get(rv.headers["Location"]).data.decode()
    assert "hourly" in html and "added." in html
    assert len(_ADD_CALLS) == 1
    assert _ADD_CALLS[0]["arg"] == ["hourly"]
    assert _ADD_CALLS[0]["kwarg"] == {
        "function": "test.ping",
        "minutes": 60,
        "enabled": True,
    }
    with client.app.app_context():
        from overstate_ui.models import AuditEvent

        assert (
            get_session()
            .query(AuditEvent)
            .filter(AuditEvent.action == "schedule-add:hourly")
            .count()
            == 1
        )


def test_schedules_add_rejects_duplicates_and_bad_input(client):
    _ADD_CALLS.clear()
    rv = client.post(
        "/schedules/web-01/add",
        data={
            "name": "daily",
            "function": "test.ping",
            "value": "60",
            "unit": "seconds",
            "enabled": "on",
        },
    )
    assert rv.status_code == 302
    assert "already has" in client.get(rv.headers["Location"]).data.decode()
    rv = client.post(
        "/schedules/web-01/add",
        data={
            "name": "hourly",
            "function": "test.ping",
            "value": "0",
            "unit": "seconds",
        },
    )
    assert rv.status_code == 302
    assert "required" in client.get(rv.headers["Location"]).data.decode()
    assert _ADD_CALLS == []
    with client.app.app_context():
        from overstate_ui.models import AuditEvent

        assert (
            get_session()
            .query(AuditEvent)
            .filter(AuditEvent.action.like("schedule-add:%"))
            .count()
            == 0
        )


def test_add_succeeded_shapes():
    from overstate_ui.schedules import add_succeeded

    assert add_succeeded([{"web-01": True}], "web-01") is True
    assert add_succeeded([{"web-01": False}], "web-01") is False
    assert add_succeeded([{"web-01": {}}], "web-01") is False
    assert add_succeeded([{"web-01": {"result": True}}], "web-01") is True


def test_events_match_and_page(client):
    assert match_prefixes("salt/job/123/new", ["salt/job"])
    assert not match_prefixes("salt/auth", ["salt/job"])
    html = client.get("/events/?tag=salt/job&tag=salt/auth").data.decode()
    assert "salt/job" in html and "EventSource" in html


def test_events_stream_filters(monkeypatch):

    seen = [
        {"tag": "salt/job/1/new", "data": {"_stamp": "t1"}},
        {"tag": "salt/auth", "data": {"_stamp": "t2"}},
    ]
    init_db("sqlite://")
    app = create_app(TestConfig)
    app.config["WTF_CSRF_ENABLED"] = False
    monkeypatch.setattr(SaltClient, "event_stream", lambda self: iter(seen))
    app.extensions["salt_client"] = SaltClient("https://salt:8000", "u", "p")
    with app.app_context():
        create_all()
        seed_admin(password="pw")
    c = app.test_client()
    c.post("/login", data={"username": "admin", "password": "pw"})
    rv = c.get("/events/stream?tag=salt/job")
    text = rv.data.decode()
    assert "salt/job/1/new" in text
    assert "salt/auth" not in text


def test_schedules_empty_string_payload(client):
    import json as _json

    def empty_schedule(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/login":
            return httpx.Response(
                200, json={"return": [{"token": "tok", "expire": 99}]}
            )
        body = _json.loads(request.content or b"{}")
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
        # schedule.list renders as an indented string on some Salt versions
        return httpx.Response(
            200,
            json={
                "return": [
                    {
                        "web-01": "schedule:\n  daily:\n    enabled: true\n    function: test.ping\n"
                        "    seconds: 3600\n"
                    }
                ]
            },
        )

    init_db("sqlite://")
    app = create_app(TestConfig)
    app.config["WTF_CSRF_ENABLED"] = False
    app.extensions["salt_client"] = SaltClient(
        "https://salt:8000", "u", "p", transport=httpx.MockTransport(empty_schedule)
    )
    with app.app_context():
        create_all()
        seed_admin(password="pw")
    c = app.test_client()
    c.post("/login", data={"username": "admin", "password": "pw"})
    rv = c.get("/schedules/")
    assert rv.status_code == 200
    html = rv.data.decode()
    assert "daily" in html and "test.ping" in html and "Disable" in html


def test_schedules_blank_yaml_shows_empty_state():
    import json as _json

    seen: list = []

    def blank_schedule(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/login":
            return httpx.Response(
                200, json={"return": [{"token": "tok", "expire": 99}]}
            )
        body = _json.loads(request.content or b"{}")
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
        seen.append(body)
        if body.get("kwarg", {}).get("return_yaml") is False:
            # Structured form: an empty schedule arrives as {}.
            return httpx.Response(200, json={"return": [{"web-01": {}}]})
        # Default Salt form: blank YAML text for an empty schedule.
        return httpx.Response(200, json={"return": [{"web-01": "schedule: {}\n"}]})

    init_db("sqlite://")
    app = create_app(TestConfig)
    app.config["WTF_CSRF_ENABLED"] = False
    app.extensions["salt_client"] = SaltClient(
        "https://salt:8000", "u", "p", transport=httpx.MockTransport(blank_schedule)
    )
    with app.app_context():
        create_all()
        seed_admin(password="pw")
    c = app.test_client()
    c.post("/login", data={"username": "admin", "password": "pw"})
    html = c.get("/schedules/").data.decode()
    assert "No schedules on" in html
    assert "schedule: {}" not in html
    assert any(b.get("kwarg", {}).get("return_yaml") is False for b in seen)


def test_parse_schedule_list_shapes():
    from overstate_ui.schedules import parse_schedule_list

    assert parse_schedule_list("schedule: {}\n") == {}
    assert parse_schedule_list({}) == {}
    assert parse_schedule_list(None) == {}
    parsed = parse_schedule_list(
        "schedule:\n  acc-job:\n    enabled: true\n    function: test.ping\n"
        "    seconds: 3600\n"
    )
    assert parsed == {
        "acc-job": {"enabled": True, "function": "test.ping", "seconds": 3600}
    }


def test_events_stream_idle_timeout(monkeypatch):
    import httpx as _httpx

    def slow_stream(self, idle_timeout=65.0):
        raise _httpx.ReadTimeout("idle")
        yield {}

    init_db("sqlite://")
    app = create_app(TestConfig)
    app.config["WTF_CSRF_ENABLED"] = False
    monkeypatch.setattr(SaltClient, "event_stream", slow_stream)
    app.extensions["salt_client"] = SaltClient("https://salt:8000", "u", "p")
    with app.app_context():
        create_all()
        seed_admin(password="pw")
    c = app.test_client()
    c.post("/login", data={"username": "admin", "password": "pw"})
    rv = c.get("/events/stream?tag=salt/job")
    text = rv.data.decode()
    assert "event stream idle timeout" in text
    assert "event: done" in text


def test_minions_csv_export(client):
    rv = client.get("/minions/export.csv")
    assert rv.status_code == 200
    assert "text/csv" in rv.content_type
    text = rv.data.decode()
    assert text.splitlines()[0].startswith("id,key_status,presence")
    assert "fedora-web-01" in text


def test_minions_csv_export_honors_filter(client):
    full = client.get("/minions/export.csv").data.decode()
    assert "fedora-web-01" in full
    filtered = client.get("/minions/export.csv?q=z-nothing-matches").data.decode()
    assert "fedora-web-01" not in filtered
    assert filtered.splitlines()[0].startswith("id,key_status,presence")
    bogus = client.get("/minions/export.csv?status=bogus").data.decode()
    assert "fedora-web-01" in bogus  # invalid status falls back to all
