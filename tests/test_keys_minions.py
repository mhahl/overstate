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
            return httpx.Response(200, json={"return": [{"token": "tok",
                                                         "expire": 99}]})
        body = json.loads(request.content or b"{}")
        if body.get("client") == "wheel":
            if body.get("fun") == "key.list_all":
                return httpx.Response(200, json={"return": [{"data": {"return": {
                    "minions": ["web-01"], "minions_pre": ["new-01"],
                    "minions_rejected": [], "minions_denied": []}}}]})
            if body.get("fun") == "key.finger":
                return httpx.Response(200, json={"return": [{"data": {"return": {
                    "minions": {"web-01": FP},
                    "minions_pre": {"new-01": FP}}}}]})
            if body.get("fun") in ("key.accept", "key.reject", "key.delete"):
                assert body.get("match") in ("web-01", "new-01")
                return httpx.Response(200, json={"return": [{"data": {
                    "return": {}, "success": True}}]})
        if body.get("client") == "runner":
            return httpx.Response(200, json={"return": [{"up": ["web-01"],
                                                         "down": []}]})
        if body.get("client") == "local":
            fun = body.get("fun")
            if fun == "grains.items":
                return httpx.Response(200, json={"return": [{
                    "web-01": {"osfinger": "Fedora Linux 41", "ipv4": ["10.0.0.1"],
                               "num_cpus": 4, "saltversion": "3006.5"}}]})
            if fun in ("schedule.list", "pillar.items", "state.show_highstate",
                       "beacons.list"):
                return httpx.Response(200, json={"return": [{"web-01": {}}]})
        return httpx.Response(200, json={"return": [{}]})

    return httpx.MockTransport(handler)


@pytest.fixture()
def client():
    init_db("sqlite://")
    app = create_app(TestConfig)
    app.config["WTF_CSRF_ENABLED"] = False
    app.extensions["salt_client"] = SaltClient(
        "https://salt:8000", "u", "p", transport=fake_transport())
    with app.app_context():
        create_all()
        seed_admin(password="pw")
    c = app.test_client()
    c.post("/login", data={"username": "admin", "password": "pw"})
    return c


def test_keys_tabs(client):
    html = client.get("/keys/?tab=pending").data.decode()
    assert "new-01" in html and FP in html
    html = client.get("/keys/?tab=accepted").data.decode()
    assert "web-01" in html


def test_key_accept_writes_audit_row():
    init_db("sqlite://")
    app = create_app(TestConfig)
    app.config["WTF_CSRF_ENABLED"] = False
    app.extensions["salt_client"] = SaltClient(
        "https://salt:8000", "u", "p", transport=fake_transport())
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
    html = client.get("/minions/onboard?mid=db-02&distro=fedora"
                      "&master=salt.example.com").data.decode()
    assert "Run this on the new machine" in html
    assert "dnf" in html and "zypper" not in html
    assert "master: salt.example.com" in html
    assert "id: db-02" in html
    assert "onboard/script" in html and "Download" in html


def test_onboard_rejects_bad_input(client):
    html = client.get("/minions/onboard?mid=bad+id%21&distro=fedora"
                      "&master=salt.example.com").data.decode()
    assert "valid hostnames" in html
    assert "Run this on the new machine" not in html


def test_onboard_script_download(client):
    rv = client.get("/minions/onboard/script?mid=db-02&distro=opensuse"
                    "&master=salt.example.com")
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
        "https://salt:8000", "u", "p", transport=fake_transport())
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
