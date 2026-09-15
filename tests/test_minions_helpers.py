"""minions_helpers: roster halves fail independently, failures are logged."""

import logging

from overstate_ui.minions_helpers import live_roster, summarize_schedule
from overstate_ui.salt_client import SaltApiError


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
