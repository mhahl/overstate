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
        if body.get("client") == "runner" and body.get("fun") == "manage.status":
            return httpx.Response(200, json={"return": [{"up": ["web-01"], "down": []}]})
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


def test_access_groups_search_and_pages_with_size_dropdown(client):
    from overstate_ui.models import LocalGroup

    with client.app.app_context():
        for i in range(12):
            get_session().add(LocalGroup(name=f"team-{i:02d}"))
        get_session().commit()
    html = client.get("/users/groups?per_page=10").data.decode()
    assert html.count("team-") >= 10
    assert 'name="per_page"' in html
    assert "Page 1 of 2" in html
    page2 = client.get("/users/groups?per_page=10&page=2").data.decode()
    assert "team-11" in page2 and "team-00" not in page2
    searched = client.get("/users/groups?q=team-01").data.decode()
    assert "team-01" in searched and "team-00" not in searched


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


def test_run_form_uses_no_alpine_directives(client):
    # Alpine compiles templates with `new Function`, which the app CSP
    # (no unsafe-eval) blocks: every directive below used to throw on
    # /jobs/new. The transport/batch sync is vanilla JS instead.
    html = client.get("/jobs/new").data.decode()
    for directive in ("x-data", "x-model", "@change", ":disabled"):
        assert directive not in html
    assert "via === 'ssh'" in html
    assert "syncTransport" in html


def test_htmx_errors_keep_last_good_fragment(client):
    html = client.get("/jobs/").data.decode()
    assert "htmx:responseError" in html
    assert "Refresh failed." in html


def test_confirm_snapshots_form_before_close(client):
    html = client.get("/jobs/").data.decode()
    # Scoped to the confirm-dialog script: the page_size_select dropdown
    # also emits requestSubmit (its own form), which appears earlier.
    script = html[html.index("pendingForm = null") :]
    assert "pendingForm = null" in script
    assert script.index("dialog.close()") < script.index("requestSubmit")


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
    assert 'role="tab"' not in orch
    assert 'role="tab"' not in client.get("/jobs/").data.decode()
    assert 'role="tab"' not in client.get("/keys/").data.decode()
    assert 'role="tab"' not in client.get("/minions/web-01").data.decode()
    # Users owns a section tab bar (Users / Access groups): the endpoint-
    # driven active tab renders on the users list page.
    assert 'aria-label="Users views"' in users
    assert 'role="tab"' in users
    assert 'tab-active' in users
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
            q="",
            page=1,
            pages=1,
            per_page=50,
            total=1,
        )
    assert ">60<" in sched
    assert "seconds" in sched


def test_navigation_and_list_controls_are_semantic(client):
    html = client.get("/minions/").data.decode()
    assert 'href="#main-content"' in html
    assert 'id="main-content"' in html
    assert 'aria-current="page"' in html
    assert 'aria-label="Select all minions on this page"' in html
    assert 'max-w-full overflow-x-auto' in html


def test_theme_control_is_available_on_login_and_app_pages(client):
    login = client.get("/login").data.decode()
    app = client.get("/minions/").data.decode()
    assert 'value="dark"' in app
    assert 'aria-label="Dark theme"' in app
    assert 'data-theme="corporate"' in login
    assert 'data-theme="corporate"' in app


def test_mobile_header_hides_secondary_labels(client):
    html = client.get("/minions/").data.decode()
    assert 'class="hidden sm:inline font-semibold">Overstate</span>' in html
    assert 'font-normal hidden md:inline">Jump to…</span>' in html
    assert 'class="ms-auto flex items-center gap-1 sm:gap-2"' in html


def test_mobile_drawer_has_keyboard_operable_controls(client):
    from html.parser import HTMLParser

    class DrawerControls(HTMLParser):
        def __init__(self):
            super().__init__()
            self.controls = {}

        def handle_starttag(self, tag, attrs):
            properties = dict(attrs)
            if properties.get("id") in ("nav-open", "nav-close"):
                self.controls[properties["id"]] = (tag, properties)

    parser = DrawerControls()
    html = client.get("/minions/").data.decode()
    parser.feed(html)
    assert parser.controls["nav-open"][0] == "button"
    assert parser.controls["nav-open"][1]["aria-controls"] == "nav-panel"
    assert parser.controls["nav-open"][1]["aria-expanded"] == "false"
    assert parser.controls["nav-close"][0] == "button"
    assert parser.controls["nav-close"][1]["aria-label"] == "Close navigation"
    assert '/static/nav.js' in html


def test_job_history_exposes_completion_state(client):
    html = client.get("/jobs/?tab=history").data.decode()
    assert "Status" in html
    assert "Done" in html


def test_action_forms_are_not_nested_in_inline_elements(client):
    from html.parser import HTMLParser

    class InlineForms(HTMLParser):
        def __init__(self):
            super().__init__()
            self.open_tags = []
            self.invalid = []

        def handle_starttag(self, tag, attrs):
            if tag in {"form", "dialog"} and "span" in self.open_tags:
                self.invalid.append(tag)
            if tag not in {"input", "img", "br", "meta", "link", "hr", "source"}:
                self.open_tags.append(tag)

        def handle_endtag(self, tag):
            if tag in self.open_tags:
                self.open_tags = self.open_tags[:len(self.open_tags) - 1 - self.open_tags[::-1].index(tag)]

    from overstate_ui.models import LocalGroup

    with client.app.app_context():
        get_session().add(LocalGroup(name="operators"))
        get_session().commit()

    for route in ("/keys/?tab=pending", "/jobs/?tab=saved", "/states/", "/users/", "/users/groups", "/files/"):
        parser = InlineForms()
        response = client.get(route)
        assert response.status_code == 200, route
        parser.feed(response.data.decode())
        assert not parser.invalid, (route, parser.invalid)


def test_event_search_does_not_submit_watch_form(client):
    html = client.get("/events/").data.decode()
    watch_form = html[html.index('aria-label="Event tag filter"'):]
    assert watch_form.index("</form>") < watch_form.index('id="event-filter"')


def test_settings_sections_are_navigation_not_inert_aria_tabs(client):
    html = client.get("/settings/").data.decode()
    assert 'aria-label="Settings sections"' in html
    assert 'role="tablist"' not in html
    assert 'aria-current="page"' in html
