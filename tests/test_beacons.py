"""Beacon tab tests: pillar-beacon list with source badges, runtime
toggles with audit, viewer gating, denial flash."""

import json

import httpx

from overstate_ui import create_app
from overstate_ui.auth import seed_admin
from overstate_ui.config import TestConfig
from overstate_ui.db import create_all, get_session, init_db
from overstate_ui.models import AuditEvent
from overstate_ui.salt_client import SaltClient

CALLS: list = []


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
            return httpx.Response(
                200, json={"return": [{"up": ["web-01"], "down": []}]}
            )
        if body.get("client") == "local":
            fun = body.get("fun")
            if fun == "beacons.list":
                mid = body.get("tgt")
                if (body.get("kwarg") or {}).get("include_pillar") is False:
                    beacons = {"local": [{"interval": 60}]}
                else:
                    beacons = {
                        "ps": [{"processes": {"salt-master": "stopped"}}],
                        "local": [{"interval": 60}],
                    }
                if mid == "bare-01":
                    beacons = {}
                return httpx.Response(
                    200, json={"return": [{mid or "web-01": beacons}]}
                )
            if fun in ("beacons.enable_beacon", "beacons.disable_beacon"):
                CALLS.append(body)
                if body.get("arg") == ["gone"]:
                    return httpx.Response(500, json={})
                if body.get("arg") == ["refused"]:
                    return httpx.Response(
                        200,
                        json={
                            "return": [
                                {
                                    "web-01": {
                                        "comment": "Cannot disable beacon item "
                                        "refused, it is configured "
                                        "in pillar.",
                                        "result": False,
                                    }
                                }
                            ]
                        },
                    )
                return httpx.Response(200, json={"return": [{"web-01": True}]})
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
    c = app.test_client()
    c.post("/login", data={"username": "admin", "password": "pw"})
    c.app = app
    return c


def test_beacons_tab_lists_entries_with_source_badges():
    c = make_client()
    html = c.get("/minions/web-01?tab=beacons").data.decode()
    assert "ps" in html and "local" in html
    assert ">pillar</span>" in html  # ps is pillar-only in the stub
    assert ">minion</span>" in html  # local survives include_pillar=False
    assert "beacons/enable" in html  # operator sees toggle controls
    assert "No beacons" not in html


def test_beacons_empty_state_links_docs():
    c = make_client()
    html = c.get("/minions/bare-01?tab=beacons").data.decode()
    assert "No beacons on bare-01" in html
    assert "docs.saltproject.io/en/3006/topics/beacons" in html


def test_beacon_enable_disable_audit():
    c = make_client()
    CALLS.clear()
    for action in ("enable", "disable"):
        rv = c.post(f"/minions/web-01/beacons/{action}", data={"beacon": "ps"})
        assert rv.status_code == 302
    assert [b["fun"] for b in CALLS] == [
        "beacons.enable_beacon",
        "beacons.disable_beacon",
    ]
    assert all(b["arg"] == ["ps"] for b in CALLS)
    with c.app.app_context():
        actions = sorted(
            r.action
            for r in get_session()
            .query(AuditEvent)
            .filter(AuditEvent.action.like("beacon-%"))
            .all()
        )
    assert actions == ["beacon-disable:ps", "beacon-enable:ps"]


def test_beacon_toggle_rejects_unknown_action():
    c = make_client()
    rv = c.post("/minions/web-01/beacons/bogus", data={"beacon": "ps"})
    assert rv.status_code == 302
    with c.app.app_context():
        assert (
            get_session()
            .query(AuditEvent)
            .filter(AuditEvent.action.like("beacon-%"))
            .count()
            == 0
        )


def test_beacon_toggle_denied_flashes_error():
    c = make_client()
    rv = c.post(
        "/minions/web-01/beacons/disable",
        data={"beacon": "gone"},
        follow_redirects=True,
    )
    assert rv.status_code == 200
    assert "salt-api error" in rv.data.decode()
    with c.app.app_context():
        assert (
            get_session()
            .query(AuditEvent)
            .filter(AuditEvent.action.like("beacon-%"))
            .count()
            == 0
        )


def test_beacon_toggle_refusal_flashes_error_not_success():
    c = make_client()
    rv = c.post(
        "/minions/web-01/beacons/disable",
        data={"beacon": "refused"},
        follow_redirects=True,
    )
    html = rv.data.decode()
    assert rv.status_code == 200
    assert "it is configured in pillar" in html
    assert "alert-error" in html
    assert "disabled." not in html
    with c.app.app_context():
        assert (
            get_session()
            .query(AuditEvent)
            .filter(AuditEvent.action.like("beacon-%"))
            .count()
            == 0
        )


def test_viewer_blocked_from_beacon_toggles():
    from overstate_ui.auth import _ph
    from overstate_ui.models import User

    c = make_client()
    with c.app.app_context():
        get_session().add(
            User(username="v", password_hash=_ph.hash("pw"), role="viewer")
        )
        get_session().commit()
    viewer = c.app.test_client()
    viewer.post("/login", data={"username": "v", "password": "pw"})
    assert (
        viewer.post("/minions/web-01/beacons/enable", data={"beacon": "ps"}).status_code
        == 403
    )
    html = viewer.get("/minions/web-01?tab=beacons").data.decode()
    assert "ps" in html  # reads fine
    assert "beacons/enable" not in html  # no toggle controls


def test_parse_beacon_list():
    from overstate_ui.minions import parse_beacon_list

    assert parse_beacon_list({"ps": [{"a": 1}]}) == {"ps": [{"a": 1}]}
    assert parse_beacon_list("beacons:\n  ps: []") == {}
    assert parse_beacon_list(None) == {}
