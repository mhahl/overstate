"""minions_helpers: roster halves fail independently, failures are logged."""

import logging

import pytest

from overstate_ui import create_app
from overstate_ui.auth import seed_admin
from overstate_ui.config import TestConfig
from overstate_ui.db import create_all, get_session, init_db
from overstate_ui.minions_helpers import (
    hydrate_entries,
    live_roster,
    minion_entries,
    minion_rows,
    summarize_schedule,
)
from overstate_ui.models import Minion
from overstate_ui.salt_client import SaltApiError


@pytest.fixture()
def app():
    init_db("sqlite://")
    app = create_app(TestConfig)
    app.config["WTF_CSRF_ENABLED"] = False
    with app.app_context():
        create_all()
        seed_admin(password="pw")
    return app


def _seed(app):
    with app.app_context():
        get_session().add_all(
            [
                Minion(
                    id="web-01",
                    grains={"osfinger": "Fedora 41", "ipv4": ["10.0.0.1"]},
                    conformity={},
                    key_status="accepted",
                ),
                Minion(
                    id="web-02",
                    grains={"osfinger": "Debian 12", "ipv4": "10.0.0.2"},
                    conformity={},
                    key_status="accepted",
                ),
                Minion(
                    id="new-01",
                    grains={},
                    conformity={},
                    key_status="pending",
                ),
            ]
        )
        get_session().commit()


class StubClient:
    def __init__(self, wheel=None, runner=None):
        self._wheel = wheel
        self._runner = runner

    def wheel(self, fun, **kwargs):
        if isinstance(self._wheel, Exception):
            raise self._wheel
        return self._wheel

    def runner(self, fun, **kwargs):
        if isinstance(self._runner, Exception):
            raise self._runner
        return self._runner


KEYS = [{"data": {"return": {"minions": ["web-01"], "minions_pre": []}}}]
STATUS = [{"up": ["web-01"], "down": []}]
EMPTY_KEYS = [{"data": {"return": {}}}]
EMPTY_STATUS = [{"up": [], "down": []}]


def test_roster_keeps_keys_when_presence_fails(caplog):
    client = StubClient(wheel=KEYS, runner=SaltApiError("runner down"))
    with caplog.at_level(logging.WARNING, logger="overstate_ui.minions_helpers"):
        statuses, up, reachable = live_roster(client)
    assert statuses == {"web-01": "accepted"}
    assert up == set()
    assert reachable is True
    assert "live presence unavailable" in caplog.text


def test_roster_keeps_presence_when_keys_fail(caplog):
    client = StubClient(wheel=SaltApiError("wheel down"), runner=STATUS)
    with caplog.at_level(logging.WARNING, logger="overstate_ui.minions_helpers"):
        statuses, up, reachable = live_roster(client)
    assert statuses == {}
    assert up == {"web-01"}
    assert reachable is True
    assert "live key list unavailable" in caplog.text


def test_roster_unreachable_when_both_fail(caplog):
    client = StubClient(
        wheel=SaltApiError("wheel down"), runner=SaltApiError("runner down")
    )
    with caplog.at_level(logging.WARNING, logger="overstate_ui.minions_helpers"):
        statuses, up, reachable = live_roster(client)
    assert statuses == {}
    assert up == set()
    assert reachable is False


def test_empty_fleet_is_reachable_not_an_outage(caplog):
    client = StubClient(wheel=EMPTY_KEYS, runner=EMPTY_STATUS)
    with caplog.at_level(logging.WARNING, logger="overstate_ui.minions_helpers"):
        statuses, up, reachable = live_roster(client)
    assert statuses == {}
    assert up == set()
    assert reachable is True
    assert caplog.text == ""


def test_roster_happy_path_silent(caplog):
    client = StubClient(wheel=KEYS, runner=STATUS)
    with caplog.at_level(logging.WARNING, logger="overstate_ui.minions_helpers"):
        statuses, up, reachable = live_roster(client)
    assert statuses == {"web-01": "accepted"}
    assert up == {"web-01"}
    assert reachable is True
    assert caplog.text == ""


def test_summarize_schedule_humanizes_intervals():
    enabled, rows = summarize_schedule(
        {
            "enabled": True,
            "nightly": {"function": "state.sls", "job_args": ["base"], "minutes": 5},
            "hourly": {"function": "mine.update", "hours": 1, "minutes": 30},
            "solo": {"function": "test.ping", "seconds": 1},
        }
    )
    assert enabled is True
    by_name = {r["name"]: r for r in rows}
    assert by_name["nightly"]["every"] == "every 5 minutes"
    assert by_name["nightly"]["arguments"] == '["base"]'
    assert by_name["hourly"]["every"] == "every 1 hour, 30 minutes"
    assert by_name["solo"]["every"] == "every 1 second"
    assert [r["name"] for r in rows] == ["hourly", "nightly", "solo"]


def test_summarize_schedule_cron_when_once_and_splay():
    enabled, rows = summarize_schedule(
        {
            "enabled": False,
            "cronjob": {"function": "state.sls", "cron": "0 2 * * *"},
            "lunch": {"function": "test.ping", "when": "12:00", "splay": 10},
            "oneoff": {"function": "test.ping", "once_fmt": "2026-01-01 00:00:00"},
            "plain": {"function": "test.ping"},
            "quiet": {"function": "test.ping", "minutes": 5, "enabled": False},
        }
    )
    assert enabled is False
    by_name = {r["name"]: r for r in rows}
    assert "enabled" not in by_name
    assert by_name["cronjob"]["every"] == "cron 0 2 * * *"
    assert by_name["lunch"]["every"] == "at 12:00 (+10s splay)"
    assert by_name["oneoff"]["every"] == "once 2026-01-01 00:00:00"
    assert by_name["plain"]["every"] == "no schedule set"
    assert by_name["quiet"]["enabled"] is False


def test_summarize_schedule_non_mapping_yields_empty():
    assert summarize_schedule("schedule: {}\n") == (None, [])
    assert summarize_schedule(None) == (None, [])


def test_entries_plus_hydrate_matches_minion_rows(app):
    """The split path must render exactly what minion_rows rendered:
    same ids, order, flags, and grains — including a live-only id with
    no snapshot row and a scalar-ipv4 snapshot."""
    _seed(app)
    statuses = {"web-01": "accepted", "web-02": "denied", "ghost-01": "pending"}
    up = {"web-01"}
    with app.app_context():
        for sort in ("id", "key", "presence", "os"):
            for direction in ("asc", "desc"):
                for q, status_filter in (("", ""), ("web", ""), ("", "pending")):
                    expected = minion_rows(
                        statuses, up, q, status_filter, sort, direction
                    )
                    got = hydrate_entries(
                        minion_entries(statuses, up, q, status_filter, sort, direction)
                    )
                    assert got == expected, (sort, direction, q, status_filter)


def test_entries_carry_no_grains_until_hydrated(app):
    """Only the os sort needs grains pre-hydration; every other sort
    must leave entries light so a page hydrates a page, not the fleet."""
    _seed(app)
    with app.app_context():
        plain = minion_entries({"web-01": "accepted"}, set(), "", "")
        assert [e["id"] for e in plain] == ["new-01", "web-01", "web-02"]
        assert all("grains" not in e for e in plain)
        page = hydrate_entries(plain[:2])
        assert [r["id"] for r in page] == ["new-01", "web-01"]
        assert page[1]["grains"]["osfinger"] == "Fedora 41"
        # Scalar ipv4 normalizes on hydration, exactly as before.
        web2 = hydrate_entries([e for e in plain if e["id"] == "web-02"])
        assert web2[0]["grains"]["ipv4"] == ["10.0.0.2"]
        # Live-only ids hydrate to a bare {}.
        ghost = hydrate_entries(minion_entries({"ghost-01": "pending"}, set(), "", ""))
        assert ghost[0]["grains"] == {}
