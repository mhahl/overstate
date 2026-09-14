"""v3 tests: pillar explorer, presets, palette search, bulk glob, migration."""

import json
import os
import subprocess

import httpx
import pytest

from overstate_ui import create_app
from overstate_ui.auth import seed_admin
from overstate_ui.config import TestConfig
from overstate_ui.db import create_all, get_session, init_db
from overstate_ui.jobs import FLEET_PRESETS, suggest_glob
from overstate_ui.models import Minion, PillarSnapshot
from overstate_ui.pillar import SNAPSHOT_LIMIT, diff_pillars
from overstate_ui.salt_client import SaltClient

PILLAR_A = {"ntp": {"servers": ["10.0.0.1"]}, "role": "web", "gone": True}
PILLAR_B = {"ntp": {"servers": ["10.0.0.2"]}, "role": "web", "added": 1}


def fake_transport() -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/login":
            return httpx.Response(
                200, json={"return": [{"token": "tok", "expire": 99}]}
            )
        body = json.loads(request.content or b"{}")
        if body.get("fun") == "pillar.items":
            tgt = body.get("tgt", "web-01")
            payload = PILLAR_B if tgt == "web-02" else PILLAR_A
            return httpx.Response(200, json={"return": [{tgt: payload}]})
        if body.get("client") == "wheel":
            return httpx.Response(
                200,
                json={
                    "return": [
                        {
                            "data": {
                                "return": {
                                    "minions": ["web-01", "web-02"],
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
        session.add(Minion(id="web-01", key_status="accepted", grains={}))
        session.add(Minion(id="web-02", key_status="accepted", grains={}))
        session.add(Minion(id="db-01", key_status="accepted", grains={}))
        session.commit()
    c = app.test_client()
    c.post("/login", data={"username": "admin", "password": "pw"})
    c.app = app
    return c


def test_pillar_index_lists_minions(client):
    html = client.get("/pillar/").data.decode()
    assert "web-01" in html and "Compare two minions" in html


def test_pillar_index_search_and_snapshot_sort(client):
    html = client.get("/pillar/?q=web-02").data.decode()
    assert 'href="/pillar/web-02"' in html
    assert 'href="/pillar/web-01"' not in html
    html = client.get("/pillar/?sort=snapshots&dir=desc").data.decode()
    assert 'aria-sort="desc"' in html


def test_capture_stores_and_prunes(client):
    for _ in range(SNAPSHOT_LIMIT + 3):
        rv = client.post("/pillar/web-01/capture")
        assert rv.status_code == 302
    with client.app.app_context():
        rows = get_session().query(PillarSnapshot).filter_by(minion_id="web-01").all()
        assert len(rows) == SNAPSHOT_LIMIT
        assert rows[0].payload["role"] == "web"
    html = client.get("/pillar/web-01").data.decode()
    assert "Live rendered pillar" in html
    assert "may contain secrets" not in html


def test_diff_shows_changed_added_removed(client):
    client.post("/pillar/web-01/capture")
    client.post("/pillar/web-02/capture")
    html = client.get("/pillar/diff?a=web-01&b=web-02").data.decode()
    assert "ntp.servers" in html
    assert "changed" in html and "added" in html and "removed" in html


def test_diff_with_no_snapshots_shows_no_data(client):
    html = client.get("/pillar/diff?a=web-01&b=web-02").data.decode()
    assert "Identical pillar" not in html
    assert "No pillar data to compare" in html


def test_pillar_detail_dead_master_renders_fallback(client):
    def dead(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("master down")

    client.app.extensions["salt_client"] = SaltClient(
        "https://salt:8000", "u", "p", transport=httpx.MockTransport(dead)
    )
    rv = client.get("/pillar/web-01")
    assert rv.status_code == 200
    assert "unreachable" in rv.data.decode()


def test_diff_unit():
    rows = diff_pillars({"a": 1, "b": {"c": 2}}, {"a": 1, "b": {"c": 3}})
    assert rows == [{"path": "b.c", "kind": "changed", "old": 2, "new": 3}]


def test_suggest_glob_unit():
    glob, selected, covered = suggest_glob(
        ["web-02", "web-01"], ["web-01", "web-02", "db-01"]
    )
    assert glob == "web-0*"
    assert selected == ["web-01", "web-02"]
    assert covered == ["web-01", "web-02"]
    glob_all, _, _ = suggest_glob(["web-01", "db-01"], ["web-01", "db-01"])
    assert glob_all == "*"


def test_suggest_glob_never_defaults_to_fleet():
    # Empty selection: no target, not "*".
    assert suggest_glob([], ["web-01", "db-01"]) == ("", [], [])
    assert suggest_glob([" ", ""], ["web-01"]) == ("", [], [])
    # No shared prefix: no target rather than a fleet-wide glob.
    glob, selected, covered = suggest_glob(
        ["web-01", "db-01"], ["web-01", "db-01", "other-01"]
    )
    assert (glob, covered) == ("", [])
    assert selected == ["db-01", "web-01"]


def test_suggest_glob_single_host_is_exact():
    glob, selected, covered = suggest_glob(["web-01"], ["web-01", "web-02", "web-010"])
    assert glob == "web-01"
    assert selected == ["web-01"]
    assert covered == ["web-01"]


def test_bulk_ignored_by_preset_shows_note(client):
    html = client.get("/jobs/new?bulk=web-01,web-02&preset=ping").data.decode()
    assert "Bulk selection ignored" in html
    assert "Start over" in html
    assert "Bulk selection (" not in html


def test_refresh_pillar_preset_prefills_form(client):
    assert FLEET_PRESETS["refresh-pillar"]["fun"] == "saltutil.refresh_pillar"
    assert FLEET_PRESETS["sync-all"]["fun"] == "saltutil.sync_all"
    html = client.get("/jobs/new?preset=refresh-pillar").data.decode()
    assert "saltutil.refresh_pillar" in html


def test_bulk_prefills_glob_with_note(client):
    html = client.get("/jobs/new?bulk=web-01,web-02").data.decode()
    assert "Bulk selection (2)" in html
    assert "web-0*" in html


def test_minion_search_json(client):
    rv = client.get("/minions/search?q=web-0")
    assert rv.status_code == 200
    assert rv.get_json() == ["web-01", "web-02"]


def test_palette_markup_present(client):
    html = client.get("/").data.decode()
    assert 'id="palette-btn"' in html and "Ctrl K" in html
    assert "/minions/search" in html


def test_palette_results_are_real_links(client):
    """Regression: results are native anchors so clicks always navigate."""
    html = client.get("/").data.decode()
    assert 'id="palette-list"' in html
    assert "<li data-palette-entry" in html
    assert 'href="/jobs/new?preset=refresh-pillar"' in html
    assert "commandPalette" not in html


def test_palette_minion_links_have_no_double_slash(client):
    html = client.get("/").data.decode()
    assert "minionBase + id" in html
    assert 'minionBase + "/" + id' not in html


def test_migration_head_applies(tmp_path):
    db = tmp_path / "mig.sqlite"
    env = dict(os.environ, DATABASE_URL=f"sqlite:///{db}")
    subprocess.run(
        [".venv/bin/alembic", "upgrade", "head"],
        check=True,
        capture_output=True,
        env=env,
        timeout=120,
    )
    subprocess.run(
        [".venv/bin/alembic", "downgrade", "-1"],
        check=True,
        capture_output=True,
        env=env,
        timeout=120,
    )
    subprocess.run(
        [".venv/bin/alembic", "upgrade", "head"],
        check=True,
        capture_output=True,
        env=env,
        timeout=120,
    )
