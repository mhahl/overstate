"""minions_helpers: roster halves fail independently, failures are logged."""

import logging

from overstate_ui.minions_helpers import live_roster
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


def test_roster_keeps_keys_when_presence_fails(caplog):
    client = StubClient(wheel=KEYS, runner=SaltApiError("runner down"))
    with caplog.at_level(logging.WARNING, logger="overstate_ui.minions_helpers"):
        statuses, up = live_roster(client)
    assert statuses == {"web-01": "accepted"}
    assert up == set()
    assert "live presence unavailable" in caplog.text


def test_roster_keeps_presence_when_keys_fail(caplog):
    client = StubClient(wheel=SaltApiError("wheel down"), runner=STATUS)
    with caplog.at_level(logging.WARNING, logger="overstate_ui.minions_helpers"):
        statuses, up = live_roster(client)
    assert statuses == {}
    assert up == {"web-01"}
    assert "live key list unavailable" in caplog.text


def test_roster_happy_path_silent(caplog):
    client = StubClient(wheel=KEYS, runner=STATUS)
    with caplog.at_level(logging.WARNING, logger="overstate_ui.minions_helpers"):
        statuses, up = live_roster(client)
    assert statuses == {"web-01": "accepted"}
    assert up == {"web-01"}
    assert caplog.text == ""
