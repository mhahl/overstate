"""UI consistency (group 1): shared header, button, pagination, table, and
back-link conventions.

Rendered assertions cover every page that renders without live Salt data.
The Keys table only renders with live roster data, so its classes are
pinned by a static template assertion instead.
"""

import pathlib

from overstate_ui import create_app
from overstate_ui.audit import log_event
from overstate_ui.auth import seed_admin
from overstate_ui.config import TestConfig
from overstate_ui.db import create_all, get_session, init_db
from overstate_ui.models import Job, Minion, MinionGroup

TEMPLATES = (
    pathlib.Path(__file__).resolve().parent.parent / "overstate_ui" / "templates"
)

FILTER_PAGES = ["/audit/", "/groups/", "/jobs/", "/keys/", "/minions/", "/pillar/"]


def make_client():
    init_db("sqlite://")
    app = create_app(TestConfig)
    app.config["WTF_CSRF_ENABLED"] = False
    with app.app_context():
        create_all()
        seed_admin(password="pw")
        session = get_session()
        for i in range(30):
            log_event("admin", f"action-{i:02d}")
            session.add(MinionGroup(name=f"g-{i:02d}", members=["web-01"]))
            session.add(Minion(id=f"web-{i:02d}", key_status="accepted", grains={}))
        for i in range(51):
            session.add(
                Job(
                    jid=f"hist-{i:03d}",
                    fun="test.ping",
                    tgt="*",
                    tgt_type="glob",
                    user="admin",
                    complete=True,
                )
            )
        session.commit()
    c = app.test_client()
    c.post("/login", data={"username": "admin", "password": "pw"})
    return c


def test_filter_submit_is_primary_on_every_filter_page():
    c = make_client()
    for path in FILTER_PAGES:
        html = c.get(path).data.decode()
        assert 'btn-primary mb-1">Filter' in html, path
        assert 'class="btn btn-sm mb-1">Filter' not in html, path


def test_pagination_uses_join_component():
    c = make_client()
    for path in ("/audit/", "/groups/", "/jobs/?tab=history", "/minions/"):
        html = c.get(path).data.decode()
        assert 'class="join' in html, path
        assert "join-item" in html, path


def test_list_tables_pin_headers():
    c = make_client()
    for path in ("/audit/", "/groups/", "/users/", "/pillar/"):
        html = c.get(path).data.decode()
        assert "table-pin-rows" in html, path
    # The Keys table only renders with live roster data.
    keys_src = (TEMPLATES / "keys.html").read_text()
    assert "table-pin-rows" in keys_src


def test_dashboard_heading_matches_page_headings():
    html = make_client().get("/").data.decode()
    assert 'font-bold tracking-tight">Fleet overview' in html


def test_destructive_actions_sit_outside_primary_headers():
    c = make_client()
    users = c.get("/users/").data.decode()
    assert "Danger zone" in users
    assert "Rotate salt-api password" in users
    detail = c.get("/jobs/hist-000").data.decode()
    assert 'btn-primary">Sync results' in detail


def test_primary_actions_live_in_page_headers():
    html = make_client().get("/reactor/").data.decode()
    assert ">Add reactor</a>" in html


def test_onboard_uses_steps_progress():
    html = make_client().get("/minions/onboard").data.decode()
    assert 'class="steps' in html


def test_events_stream_has_status_badge():
    html = make_client().get("/events/").data.decode()
    assert 'id="event-status"' in html


def test_minions_search_filters_live():
    html = make_client().get("/minions/").data.decode()
    assert 'hx-trigger="input changed delay:400ms, submit"' in html


def test_jobs_poll_shows_refreshing_indicator():
    html = make_client().get("/jobs/?tab=history").data.decode()
    assert 'id="jobs-refreshing"' in html


def test_sort_indicators_use_icons():
    html = make_client().get("/audit/?sort=action&dir=asc").data.decode()
    assert "icon-[lucide--arrow-up]" in html
    assert "↑" not in html


def test_confirm_fire_button_states_blast_radius():
    c = make_client()
    rv = c.post(
        "/jobs/run",
        data={
            "tgt": "*",
            "tgt_type": "glob",
            "fun": "state.apply",
            "args": "web",
            "mode": "async",
        },
    )
    html = rv.data.decode()
    assert "Review before firing" in html
    assert "Fire on 30 minions" in html


def test_back_links_share_glyph_and_size():
    for name in TEMPLATES.glob("*.html"):
        src = name.read_text()
        assert "←" not in src, name.name
        for line in src.splitlines():
            # Every "‹ ..." control is a back/prev link: small ghost style.
            if "‹ " in line:
                assert "btn-sm" in line, f"{name.name}: {line.strip()}"
