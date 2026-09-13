"""v3.5 tests: live-table fragments, sorting, presence, polling markup."""

import datetime as dt
import json

import httpx
import pytest

from overstate_ui import create_app
from overstate_ui.auth import seed_admin
from overstate_ui.config import TestConfig
from overstate_ui.db import create_all, get_session, init_db
from overstate_ui.jobs import sort_jobs
from overstate_ui.models import Job, JobReturn, Minion
from overstate_ui.salt_client import SaltClient


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
                                    "minions": ["web-01", "web-02"],
                                    "minions_pre": ["db-01"],
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
        session = get_session()
        session.add(
            Minion(
                id="web-01",
                key_status="accepted",
                grains={"osfinger": "Fedora Linux"},
                conformity={"status": "ok", "jid": "1"},
            )
        )
        session.add(
            Minion(
                id="web-02",
                key_status="accepted",
                grains={"osfinger": "Debian"},
                conformity={"status": "drifted", "jid": "1"},
            )
        )
        session.add(Minion(id="db-01", key_status="pending", grains={}, conformity={}))
        session.add(
            Job(
                jid="100",
                fun="state.highstate",
                tgt="*",
                tgt_type="glob",
                user="amy",
                complete=False,
            )
        )
        session.add(
            Job(
                jid="090",
                fun="test.ping",
                tgt="*",
                tgt_type="glob",
                user="zed",
                complete=False,
            )
        )
        session.add(
            JobReturn(
                jid="100", minion_id="web-01", success=True, retcode=0, payload={}
            )
        )
        session.add(
            JobReturn(
                jid="090", minion_id="web-01", success=False, retcode=1, payload={}
            )
        )
        session.commit()
    c = app.test_client()
    c.post("/login", data={"username": "admin", "password": "pw"})
    c.app = app
    return c


def partial(client, path):
    return client.get(path, headers={"HX-Request": "true"}).data.decode()


def test_minions_partial_has_no_shell(client):
    html = partial(client, "/minions/")
    assert 'id="minion-table"' in html
    assert "drawer-side" not in html
    assert "menu-active" not in html


def test_minions_sort_key(client):
    html = partial(client, "/minions/?sort=key&dir=asc")
    assert html.index("web-01") < html.index("db-01")
    html = partial(client, "/minions/?sort=key&dir=desc")
    assert html.index("db-01") < html.index("web-01")


def test_minions_sort_presence_and_os(client):
    html = partial(client, "/minions/?sort=presence&dir=asc")
    assert html.index("web-01") < html.index("db-01")
    html = partial(client, "/minions/?sort=os&dir=asc")
    assert html.index("web-02") < html.index("web-01")


def test_minions_bad_sort_falls_back(client):
    rv = client.get("/minions/?sort=bogus&dir=sideways")
    assert rv.status_code == 200
    assert 'id="minion-table"' in rv.data.decode()


def test_minions_sort_links_push_url(client):
    html = client.get("/minions/").data.decode()
    assert 'hx-push-url="true"' in html
    assert 'hx-target="#minion-table"' in html


def test_jobs_partial_and_sort(client):
    html = partial(client, "/jobs/?tab=running&sort=fun&dir=asc")
    assert 'id="jobs-region"' in html
    assert "drawer-side" not in html
    assert html.index("state.highstate") < html.index("test.ping")
    html = partial(client, "/jobs/?tab=running&sort=user&dir=desc")
    assert html.index("zed") < html.index("amy")


def test_jobs_tabs_are_hx(client):
    html = client.get("/jobs/?tab=history").data.decode()
    assert 'hx-target="#jobs-region"' in html


def test_jobs_running_poll_markup(client):
    html = client.get("/jobs/?tab=running").data.decode()
    assert 'id="live-toggle"' in html
    assert "data-poll-url" in html
    assert "htmx.ajax" in html


def test_conformity_partial_and_sort(client):
    html = partial(client, "/states/?sort=status&dir=asc")
    assert 'id="conformity-table"' in html
    assert "drawer-side" not in html
    assert html.index("web-01") < html.index("web-02")


def test_conformity_full_page_has_hx_wiring(client):
    html = client.get("/states/").data.decode()
    assert 'id="conformity-table"' in html
    assert 'hx-target="#conformity-table"' in html
    assert 'hx-push-url="true"' in html


def test_sort_jobs_started_uses_datetime():
    def job(jid, started):
        return Job(
            jid=jid,
            fun="test.ping",
            tgt="*",
            tgt_type="glob",
            user="u",
            started_at=started,
        )

    old = job("001", dt.datetime(2020, 1, 1, tzinfo=dt.UTC))
    new = job("002", dt.datetime(2024, 1, 1, tzinfo=dt.UTC))
    assert [j.jid for j in sort_jobs([old, new], "started", "desc")] == ["002", "001"]
    assert [j.jid for j in sort_jobs([new, old], "started", "asc")] == ["001", "002"]
    missing = job("000", None)
    assert next(j.jid for j in sort_jobs([missing, old], "started", "desc")) == "001"


def test_conformity_ok_renders_success(client):
    html = client.get("/states/").data.decode()
    assert '<span class="badge badge-sm badge-success">ok</span>' in html


def test_minion_returns_partial_and_sort(client):
    html = partial(client, "/minions/web-01?tab=jobs&sort=status&dir=asc")
    assert 'id="minion-returns"' in html
    assert "drawer-side" not in html
    assert html.index(">100<") < html.index(">090<")


def test_presence_endpoint(client):
    rv = client.get("/minions/presence")
    assert rv.status_code == 200
    assert rv.get_json() == {"web-01": "up", "web-02": "dead", "db-01": "down"}


def test_bulk_selection_survives_swaps_hook(client):
    html = client.get("/minions/").data.decode()
    assert "bulk-selection" in html
    assert "htmx:afterSwap" in html
    assert 'name="bulk"' in html


def test_live_pause_key_shared(client):
    minions_html = client.get("/minions/").data.decode()
    jobs_html = client.get("/jobs/?tab=running").data.decode()
    assert "overstate-live" in minions_html
    assert "overstate-live" in jobs_html


def test_minions_live_markup(client):
    html = client.get("/minions/").data.decode()
    assert 'id="live-toggle"' in html
    assert 'data-mid="web-01"' in html
    assert "/minions/presence" in html
