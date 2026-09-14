"""Phase 4 tests: keys tabs/actions + audit, minions list/detail/refresh."""

import json

import httpx
import pytest

from overstate_ui import create_app
from overstate_ui.auth import seed_admin
from overstate_ui.config import TestConfig
from overstate_ui.db import create_all, get_session, init_db
from overstate_ui.models import AuditEvent, Minion
from overstate_ui.salt_client import SaltClient

FP = "aa:bb:cc:dd:ee:ff:00:11:22:33:44:55:66:77:88:99"


def fake_transport() -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/login":
            return httpx.Response(
                200, json={"return": [{"token": "tok", "expire": 99}]}
            )
        body = json.loads(request.content or b"{}")
        if body.get("client") == "wheel":
            if body.get("fun") == "key.list_all":
                return httpx.Response(
                    200,
                    json={
                        "return": [
                            {
                                "data": {
                                    "return": {
                                        "minions": ["web-01"],
                                        "minions_pre": ["new-01"],
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
                    200,
                    json={
                        "return": [
                            {
                                "data": {
                                    "return": {
                                        "minions": {"web-01": FP},
                                        "minions_pre": {"new-01": FP},
                                    }
                                }
                            }
                        ]
                    },
                )
            if body.get("fun") in ("key.accept", "key.reject", "key.delete"):
                assert body.get("match") in ("web-01", "new-01")
                return httpx.Response(
                    200, json={"return": [{"data": {"return": {}, "success": True}}]}
                )
        if body.get("client") == "runner":
            return httpx.Response(
                200, json={"return": [{"up": ["web-01"], "down": []}]}
            )
        if body.get("client") == "local":
            fun = body.get("fun")
            if fun == "grains.items":
                return httpx.Response(
                    200,
                    json={
                        "return": [
                            {
                                "web-01": {
                                    "osfinger": "Fedora Linux 41",
                                    "ipv4": ["10.0.0.1"],
                                    "num_cpus": 4,
                                    "saltversion": "3006.5",
                                }
                            }
                        ]
                    },
                )
            if fun in (
                "schedule.list",
                "pillar.items",
                "state.show_highstate",
                "beacons.list",
            ):
                value = {}
                if (
                    fun == "schedule.list"
                    and (body.get("kwarg") or {}).get("return_yaml") is not False
                ):
                    # Older renders arrive as YAML text, not a mapping.
                    value = "schedule: {}\n"
                return httpx.Response(200, json={"return": [{"web-01": value}]})
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
    c = app.test_client()
    c.post("/login", data={"username": "admin", "password": "pw"})
    c.app = app
    return c


def test_keys_tabs(client):
    html = client.get("/keys/?tab=pending").data.decode()
    assert "new-01" in html and FP in html
    html = client.get("/keys/?tab=accepted").data.decode()
    assert "web-01" in html


def test_keys_search_filters_within_tab(client):
    html = client.get("/keys/?tab=accepted&q=zzz-nope").data.decode()
    assert "No accepted keys" in html
    html = client.get("/keys/?tab=accepted&q=web").data.decode()
    assert "web-01" in html


def test_keys_sort_direction_flips(client):
    asc = client.get("/keys/?tab=accepted&sort=id&dir=asc").data.decode()
    desc = client.get("/keys/?tab=accepted&sort=id&dir=desc").data.decode()
    assert 'aria-sort="asc"' in asc and 'aria-sort="desc"' in desc


def test_key_accept_writes_audit_row():
    init_db("sqlite://")
    app = create_app(TestConfig)
    app.config["WTF_CSRF_ENABLED"] = False
    app.extensions["salt_client"] = SaltClient(
        "https://salt:8000", "u", "p", transport=fake_transport()
    )
    with app.app_context():
        create_all()
        seed_admin(password="pw")
    c = app.test_client()
    c.post("/login", data={"username": "admin", "password": "pw"})
    c.post("/keys/accept", data={"id": "new-01", "tab": "pending"})
    with app.app_context():
        row = get_session().query(AuditEvent).one()
        assert (row.user, row.action) == ("admin", "accept-key")


def test_minions_list_search_paginate(client):
    html = client.get("/minions/").data.decode()
    assert "web-01" in html and "new-01" in html
    assert "Fedora" not in html  # snapshot empty before refresh
    html = client.get("/minions/?q=web").data.decode()
    assert "web-01" in html and "new-01" not in html
    html = client.get("/minions/?status=pending").data.decode()
    assert "new-01" in html and "web-01" not in html


def test_minions_row_kebab_menu_for_operator(client):
    html = client.get("/minions/").data.decode()
    assert "ellipsis-vertical" in html
    assert "data-kebab" in html
    body = html.split("<tbody>", 1)[1].split("</tbody>", 1)[0]
    assert 'action="/minions/web-01/refresh"' in body
    # Both items are direct li children, so daisyUI styles them as menu
    # items (no display:contents wrapper swallowing the item box).
    assert 'class="contents"' not in body
    assert "openRemoveModal" in body
    # Remove goes through the warning dialog, not a direct POST.
    assert 'action="/minions/web-01/remove"' not in body
    assert 'id="remove-modal"' in html
    assert 'name="delete_key"' in html
    # Flat two-item menu: no headings, no job links, no key ops.
    assert "menu-title" not in body
    assert "jobs/new" not in body
    assert "/keys/" not in body
    # Placement is decided at click time (the page script floats the menu
    # above the scroll wrapper), so no static open direction is baked in.
    assert "dropdown-top" not in body
    assert 'id="remove-modal-cancel"' in html


def test_minions_row_menu_hidden_for_viewer(client):
    from overstate_ui.auth import _ph
    from overstate_ui.models import User

    with client.app.app_context():
        get_session().add(
            User(username="v", password_hash=_ph.hash("pw"), role="viewer")
        )
        get_session().commit()
    viewer = client.app.test_client()
    viewer.post("/login", data={"username": "v", "password": "pw"})
    html = viewer.get("/minions/").data.decode()
    assert "web-01" in html  # list itself stays visible
    assert "ellipsis-vertical" not in html
    assert 'id="remove-modal"' not in html


def test_key_act_empty_id_rejected(client):
    rv = client.post("/keys/delete", data={"id": "", "tab": "accepted"})
    assert rv.status_code == 302
    assert "Select a minion first" in client.get(rv.headers["Location"]).data.decode()


def test_key_act_honors_next(client):
    rv = client.post("/keys/delete", data={"id": "new-01", "next": "/minions/"})
    assert rv.status_code == 302
    assert rv.headers["Location"] == "/minions/"
    rv = client.post("/keys/delete", data={"id": "new-01", "next": "https://evil/"})
    assert rv.headers["Location"].startswith("/keys/")


def test_minion_row_refresh_updates_snapshot(client):
    with client.app.app_context():
        get_session().add(
            Minion(id="web-01", grains={}, conformity={}, key_status="accepted")
        )
        get_session().commit()
    rv = client.post("/minions/web-01/refresh")
    assert rv.status_code == 302
    assert rv.headers["Location"] == "/minions/"
    with client.app.app_context():
        row = get_session().get(Minion, "web-01")
        assert row.grains["osfinger"] == "Fedora Linux 41"
        assert row.last_seen is not None
    assert "refreshed" in client.get(rv.headers["Location"]).data.decode()


def test_minion_row_refresh_without_grains_keeps_snapshot(client):
    with client.app.app_context():
        get_session().add(
            Minion(
                id="ghost-01",
                grains={"os": "X"},
                conformity={},
                key_status="accepted",
            )
        )
        get_session().commit()
    rv = client.post("/minions/ghost-01/refresh")
    assert rv.status_code == 302
    with client.app.app_context():
        assert get_session().get(Minion, "ghost-01").grains == {"os": "X"}
    assert "no grain data" in client.get(rv.headers["Location"]).data.decode()


def test_minion_row_remove_deletes_snapshot_keeps_history(client):
    from overstate_ui.models import Job, JobReturn

    with client.app.app_context():
        session = get_session()
        session.add(
            Minion(id="web-01", grains={}, conformity={}, key_status="accepted")
        )
        session.add(
            Job(
                jid="j1",
                fun="test.ping",
                tgt="*",
                tgt_type="glob",
                user="admin",
                complete=True,
            )
        )
        session.add(
            JobReturn(jid="j1", minion_id="web-01", success=True, retcode=0, payload={})
        )
        session.commit()
    rv = client.post("/minions/web-01/remove")
    assert rv.status_code == 302
    assert rv.headers["Location"] == "/minions/"
    with client.app.app_context():
        assert get_session().get(Minion, "web-01") is None
        assert (
            get_session().query(JobReturn).filter_by(minion_id="web-01").count() == 1
        )
    assert "removed" in client.get("/minions/").data.decode().lower()


def test_minion_row_remove_with_key_deletes_both(client, monkeypatch):
    with client.app.app_context():
        get_session().add(
            Minion(id="web-01", grains={}, conformity={}, key_status="accepted")
        )
        get_session().commit()
    calls = []
    salt = client.app.extensions["salt_client"]
    orig_wheel = salt.wheel

    def rec(fun, **kwargs):
        calls.append((fun, kwargs))
        return orig_wheel(fun, **kwargs)

    monkeypatch.setattr(salt, "wheel", rec)
    rv = client.post("/minions/web-01/remove", data={"delete_key": "yes"})
    assert rv.status_code == 302
    assert ("key.delete", {"match": "web-01"}) in calls
    with client.app.app_context():
        assert get_session().get(Minion, "web-01") is None
    assert "Salt key deleted" in client.get(rv.headers["Location"]).data.decode()


def test_minion_row_remove_without_key_leaves_key_alone(client, monkeypatch):
    with client.app.app_context():
        get_session().add(
            Minion(id="web-01", grains={}, conformity={}, key_status="accepted")
        )
        get_session().commit()
    calls = []
    salt = client.app.extensions["salt_client"]
    orig_wheel = salt.wheel

    def rec(fun, **kwargs):
        calls.append((fun, kwargs))
        return orig_wheel(fun, **kwargs)

    monkeypatch.setattr(salt, "wheel", rec)
    rv = client.post("/minions/web-01/remove")
    assert rv.status_code == 302
    assert [fun for fun, _ in calls if fun == "key.delete"] == []
    with client.app.app_context():
        assert get_session().get(Minion, "web-01") is None


def test_minion_row_remove_key_failure_keeps_snapshot(client, monkeypatch):
    from overstate_ui.salt_client import SaltApiError

    with client.app.app_context():
        get_session().add(
            Minion(id="web-01", grains={}, conformity={}, key_status="accepted")
        )
        get_session().commit()
    salt = client.app.extensions["salt_client"]
    orig_wheel = salt.wheel

    def boom(fun, **kwargs):
        if fun == "key.delete":
            raise SaltApiError("key busy")
        return orig_wheel(fun, **kwargs)

    monkeypatch.setattr(salt, "wheel", boom)
    rv = client.post("/minions/web-01/remove", data={"delete_key": "yes"})
    assert rv.status_code == 302
    with client.app.app_context():
        assert get_session().get(Minion, "web-01") is not None
    assert "salt-api error" in client.get(rv.headers["Location"]).data.decode()


def test_minion_row_remove_unknown_minion(client):
    rv = client.post("/minions/nope-01/remove")
    assert rv.status_code == 302
    assert "Unknown minion" in client.get(rv.headers["Location"]).data.decode()


def test_minion_row_actions_forbidden_for_viewer(client):
    from overstate_ui.auth import _ph
    from overstate_ui.models import User

    with client.app.app_context():
        get_session().add(
            User(username="v", password_hash=_ph.hash("pw"), role="viewer")
        )
        get_session().commit()
    viewer = client.app.test_client()
    viewer.post("/login", data={"username": "v", "password": "pw"})
    assert viewer.post("/minions/web-01/refresh").status_code == 403
    assert viewer.post("/minions/web-01/remove").status_code == 403


def test_minions_refresh_caches_grains(client):
    rv = client.post("/minions/refresh")
    assert rv.status_code == 302
    html = client.get("/minions/").data.decode()
    assert "Fedora Linux 41" in html


def test_onboard_renders_form_and_pending(client):
    html = client.get("/minions/onboard").data.decode()
    assert "Onboard a minion" in html
    assert 'name="mid"' in html and 'name="distro"' in html
    assert 'name="master"' in html
    assert "new-01" in html  # pending key from the fixture roster
    assert "Run this on the new machine" not in html
    assert "Onboard minion" in client.get("/minions/").data.decode()


def test_onboard_generates_script(client):
    html = client.get(
        "/minions/onboard?mid=db-02&distro=fedora&master=salt.example.com"
    ).data.decode()
    assert "Run this on the new machine" in html
    assert "dnf" in html and "zypper" not in html
    assert "master: salt.example.com" in html
    assert "id: db-02" in html
    assert "onboard/script" in html and "Download" in html
    assert 'data-copy="onboard-script"' in html
    assert 'id="onboard-script"' in html


def test_onboard_rejects_bad_input(client):
    html = client.get(
        "/minions/onboard?mid=bad+id%21&distro=fedora&master=salt.example.com"
    ).data.decode()
    assert "valid hostnames" in html
    assert "Run this on the new machine" not in html


def test_onboard_script_download(client):
    rv = client.get(
        "/minions/onboard/script?mid=db-02&distro=opensuse&master=salt.example.com"
    )
    assert rv.status_code == 200
    assert "attachment" in rv.headers["Content-Disposition"]
    assert "onboard-db-02.sh" in rv.headers["Content-Disposition"]
    text = rv.data.decode()
    assert text.startswith("#!/bin/sh")
    assert "zypper" in text and "systemctl enable --now salt-minion" in text
    rv = client.get("/minions/onboard/script?mid=nope%21")
    assert rv.status_code == 302


def test_build_onboard_script_distros():
    from overstate_ui.minions import build_onboard_script

    assert "zypper" in build_onboard_script("opensuse", "m", "h")
    assert "dnf" in build_onboard_script("fedora", "m", "h")


def test_master_host_setting_saved_and_used():
    from overstate_ui.settings import default_master_host, get_setting

    init_db("sqlite://")
    app = create_app(TestConfig)
    app.config["WTF_CSRF_ENABLED"] = False
    app.extensions["salt_client"] = SaltClient(
        "https://salt:8000", "u", "p", transport=fake_transport()
    )
    with app.app_context():
        create_all()
        seed_admin(password="pw")
        assert get_setting("master_host") == default_master_host()
        assert get_setting("master_host") != ""
    c = app.test_client()
    c.post("/login", data={"username": "admin", "password": "pw"})
    c.post("/settings/", data={"master_host": "salt.example.com"})
    with app.app_context():
        assert get_setting("master_host") == "salt.example.com"
    assert "salt.example.com" in c.get("/minions/onboard").data.decode()
    c.post("/settings/", data={"master_host": ""})
    with app.app_context():
        assert get_setting("master_host") == default_master_host()


def test_master_host_falls_back_to_api_host(monkeypatch):
    from overstate_ui import settings as settings_mod

    monkeypatch.setattr(settings_mod.socket, "getfqdn", lambda: "localhost")
    init_db("sqlite://")
    app = create_app(TestConfig)
    with app.app_context():
        create_all()
        assert settings_mod.get_setting("master_host") == "salt-master"


def test_minion_detail_tabs(client):
    for tab in ("overview", "states", "jobs", "schedule", "pillar", "beacons"):
        rv = client.get(f"/minions/web-01?tab={tab}")
        assert rv.status_code == 200, tab
    assert "osfinger" in client.get("/minions/web-01").data.decode()


def test_schedule_pillar_empty_states_link_docs_no_raw(client):
    html = client.get("/minions/web-01?tab=schedule").data.decode()
    assert "No schedules on web-01" in html
    assert "salt.modules.schedule" in html
    assert "mockup-code" not in html  # no raw dump below the empty state
    html = client.get("/minions/web-01?tab=pillar").data.decode()
    assert "No pillar data for web-01" in html
    assert "topics/pillar" in html
    assert "mockup-code" not in html


def test_minion_overview_dashboard(client):
    html = client.get("/minions/web-01").data.decode()
    for heading in (
        "Presence",
        "Key",
        "Conformity",
        "Last seen",
        "Machine",
        "Recent activity",
        "All grain facts",
    ):
        assert heading in html
    assert "Fedora Linux 41" in html and "10.0.0.1" in html
    assert "icon-[simple-icons--fedora]" in html
    assert "Run job on this minion" in html
    assert "not in snapshot cache" in html
    assert "No highstate-style job has reported" in html


def test_minions_page_pause_labels_scope(client):
    html = client.get("/minions/").data.decode()
    assert "Pause live updates" in html
    assert "all pages" in html


def test_os_icon_slug():
    from overstate_ui.minions import os_icon_slug

    assert os_icon_slug({"os": "Fedora"}) == "fedora"
    assert os_icon_slug({"osfinger": "openSUSE Leap 15.6"}) == "opensuse"
    assert os_icon_slug({"os": "SUSE"}) == "opensuse"
    assert os_icon_slug({"os": "Red Hat Enterprise Linux"}) == "redhat"
    assert os_icon_slug({"os": "Windows"}) == "windows"
    assert os_icon_slug({}) == "linux"
    assert os_icon_slug({"osfinger": "Something Else"}) == "linux"


def test_minion_overview_snapshot_and_activity(client):
    import datetime as dt

    from overstate_ui.models import Job, JobReturn

    with client.app.app_context():
        get_session().add(
            Minion(
                id="web-01",
                grains={},
                conformity={"status": "ok", "jid": "j1"},
                key_status="accepted",
                last_seen=dt.datetime(2026, 9, 1, 12, 0, tzinfo=dt.UTC),
            )
        )
        get_session().add(
            Job(
                jid="j1",
                fun="test.ping",
                tgt="*",
                tgt_type="glob",
                user="admin",
                complete=True,
            )
        )
        get_session().add(
            JobReturn(jid="j1", minion_id="web-01", success=True, retcode=0)
        )
        get_session().commit()
    html = client.get("/minions/web-01").data.decode()
    assert "not in snapshot cache" not in html
    assert ">accepted<" in html
    assert "2026-09-01 12:00" in html
    assert ">ok<" in html and "j1" in html
    assert "test.ping" in html
    assert "Nothing has run" not in html
    jobs_html = client.get("/minions/web-01?tab=jobs").data.decode()
    assert 'href="/jobs/j1"' in jobs_html


def test_minion_overview_hides_run_for_viewer(client):
    from overstate_ui.auth import _ph
    from overstate_ui.models import User

    with client.app.app_context():
        get_session().add(
            User(username="v", password_hash=_ph.hash("pw"), role="viewer")
        )
        get_session().commit()
    viewer = client.app.test_client()
    viewer.post("/login", data={"username": "v", "password": "pw"})
    html = viewer.get("/minions/web-01").data.decode()
    assert "Run job on this minion" not in html
    assert "Recent activity" in html
