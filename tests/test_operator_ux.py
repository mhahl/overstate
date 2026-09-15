"""Package P4 tests: operator UX forms, lists, and small chrome fixes."""

import datetime as dt
import json

import httpx
import pytest

from overstate_ui import create_app
from overstate_ui.auth import seed_admin
from overstate_ui.config import TestConfig
from overstate_ui.db import create_all, get_session, init_db
from overstate_ui.models import Job, Minion
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
                grains={"osfinger": "Debian"},
                conformity={"status": "ok", "jid": "old-jid"},
            )
        )
        now = dt.datetime.now(dt.UTC)
        session.add(
            Job(
                jid="old-jid",
                fun="test.ping",
                tgt="web-01",
                tgt_type="list",
                user="admin",
                complete=True,
                started_at=now - dt.timedelta(days=30),
            )
        )
        session.commit()
    c = app.test_client()
    c.post("/login", data={"username": "admin", "password": "pw"})
    c.app = app
    return c


def test_fun_combobox_enter_only_picks_when_open_and_selected(client):
    html = client.get("/jobs/new").data.decode()
    assert "funList.classList.contains('hidden')" in html
    assert "aria-selected" in html
    # Enter with a closed list or nothing highlighted falls through to submit.


def test_jobs_filter_form_lives_outside_region(client):
    html = client.get("/jobs/?tab=running").data.decode()
    assert 'id="jobs-filter"' in html
    assert html.index('id="jobs-filter"') < html.index('id="jobs-region"')
    assert "inflight" in html
    assert "input:focus" in html
    partial = client.get(
        "/jobs/?tab=running", headers={"HX-Request": "true"}
    ).data.decode()
    assert 'id="jobs-region"' in partial
    assert "<form" not in partial


def test_states_chips_use_dir(client):
    html = client.get("/states/").data.decode()
    assert "dir=" in html
    assert "direction=" not in html
    assert "data-status-chip" in html
    assert "htmx:afterSwap" in html


def test_minions_filter_keeps_sort(client):
    html = client.get("/minions/").data.decode()
    assert 'name="sort"' in html
    assert 'name="dir"' in html
    assert "location.search" in html


def test_fun_doc_fetch_guards_and_empty_states(client):
    html = client.get("/jobs/new").data.decode()
    assert "r.ok" in html
    assert "AbortController" in html
    assert "Docs unavailable." in html
    assert "No accepted minion available for sys.doc." in html


def test_presence_leaves_checking(client):
    html = client.get("/minions/web-01").data.decode()
    assert "unknown" in html
    assert "presence unavailable" in html


def test_palette_is_dialog_with_stale_guard(client):
    html = client.get("/jobs/").data.decode()
    assert '<dialog id="palette"' in html
    assert 'role="dialog"' not in html
    assert "aria-modal" in html
    assert "searchGen" in html
    assert "showModal" in html
    # Recents writer only keeps same-origin paths.
    assert 'href.charAt(0) === "/"' in html
    assert "sm:inline-flex" not in html


def test_login_honors_safe_next(client):
    client.post("/logout")
    rv = client.post(
        "/login?next=/jobs/",
        data={"username": "admin", "password": "pw"},
    )
    assert rv.status_code == 302
    assert rv.headers["Location"].endswith("/jobs/")
    client.post("/logout")
    rv = client.post(
        "/login?next=https://evil.example.com/",
        data={"username": "admin", "password": "pw"},
    )
    assert rv.headers["Location"].endswith("/")
    assert "evil" not in rv.headers["Location"]
    client.post("/logout")
    rv = client.post(
        "/login?next=/\\evil.com",
        data={"username": "admin", "password": "pw"},
    )
    assert rv.headers["Location"].endswith("/")
    html = client.get("/login?next=/jobs/").data.decode()
    assert 'name="next"' in html


def test_history_search_hits_database(client):
    with client.app.app_context():
        now = dt.datetime.now(dt.UTC)
        for i in range(60):
            get_session().add(
                Job(
                    jid=f"bulk-{i:04d}",
                    fun="test.ping",
                    tgt="*",
                    tgt_type="glob",
                    user="admin",
                    complete=True,
                    started_at=now - dt.timedelta(days=i + 1),
                )
            )
        get_session().commit()
    html = client.get("/jobs/?tab=history&q=old-jid").data.decode()
    assert "old-jid" in html
    assert "bulk-0059" not in html
    page2 = client.get("/jobs/?tab=history&page=2").data.decode()
    assert "Page 2 of" in page2
    rv = client.get("/jobs/?jump=old-jid")
    assert rv.status_code == 302
    assert rv.headers["Location"].endswith("/jobs/old-jid")
    rv = client.get("/jobs/?jump=no-such-jid", follow_redirects=True)
    assert "Unknown job." in rv.data.decode()


def test_run_form_survives_garbage_saved(client):
    assert client.get("/jobs/new?saved=nope").status_code == 200


def test_ssh_forces_sync_and_batch_fields_are_numbers(client):
    html = client.get("/jobs/new").data.decode()
    assert 'type="number"' in html
    assert 'min="1"' in html
    assert "via === 'ssh'" in html


def test_htmx_errors_keep_last_good_fragment(client):
    html = client.get("/jobs/").data.decode()
    assert "htmx:responseError" in html
    assert "Refresh failed." in html


def test_confirm_snapshots_form_before_close(client):
    html = client.get("/jobs/").data.decode()
    assert html.index("pendingForm = null") < html.index("dialog.close()")
    assert html.index("dialog.close()") < html.index("requestSubmit")


def test_loading_links_swallow_double_clicks(client):
    html = client.get("/jobs/").data.decode()
    assert "loadingActive" in html
    assert "preventDefault" in html


def test_chrome_title_flash_tabs_schedule_bulk(client):
    users = client.get("/users/").data.decode()
    assert "Users · Overstate" in users
    # Base owns the only flash region; page templates must not repeat it.
    with open("overstate_ui/templates/base.html") as fh:
        assert "get_flashed_messages" in fh.read()
    for name in ("users.html", "jobs_orchestrate.html"):
        with open(f"overstate_ui/templates/{name}") as fh:
            assert "get_flashed_messages" not in fh.read(), name
    orch = client.get("/jobs/orchestrate").data.decode()
    assert "Orchestrate" in orch
    assert 'role="tab"' not in users + orch
    assert 'role="tab"' not in client.get("/jobs/").data.decode()
    assert 'role="tab"' not in client.get("/keys/").data.decode()
    assert 'role="tab"' not in client.get("/minions/web-01").data.decode()
    minions = client.get("/minions/").data.decode()
    assert 'id="bulk-all"' in minions
    assert "<noscript>" in minions
    assert 'action="/minions/web-01/remove"' in minions
    rv = client.get("/jobs/new?bulk_run=1", follow_redirects=True)
    assert "Select minions first." in rv.data.decode()
    with client.app.test_request_context("/"):
        from flask import render_template

        sched = render_template(
            "schedules.html",
            minions=["web-01"],
            mid="web-01",
            entries={
                "hourly": {
                    "function": "test.ping",
                    "seconds": 60,
                    "enabled": True,
                }
            },
            raw=None,
            sort="name",
            direction="asc",
            error=None,
        )
    assert ">60<" in sched
    assert "seconds" in sched
