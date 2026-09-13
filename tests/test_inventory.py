"""Inventory snapshot tests: upserts, defaults, bad payloads."""

import pytest

from overstate_ui import create_app
from overstate_ui.auth import seed_admin
from overstate_ui.config import TestConfig
from overstate_ui.db import create_all, get_session, init_db
from overstate_ui.inventory import refresh_inventory
from overstate_ui.models import Minion


@pytest.fixture()
def app():
    init_db("sqlite://")
    app = create_app(TestConfig)
    app.config["WTF_CSRF_ENABLED"] = False
    with app.app_context():
        create_all()
        seed_admin(password="pw")
    return app


class StubClient:
    def __init__(self, grains):
        self._grains = grains

    def local(self, tgt, fun, **kwargs):
        assert tgt == "*"
        assert fun == "grains.items"
        return [self._grains]


def test_refresh_upserts_new_and_existing(app):
    with app.app_context():
        get_session().add(
            Minion(id="web-01", grains={}, conformity={}, key_status="pending")
        )
        get_session().commit()
        client = StubClient(
            {
                "web-01": {"os": "Fedora", "ipv4": ["10.0.0.1"]},
                "db-01": {"os": "Debian", "ipv4": "10.0.0.2"},
            }
        )
        count = refresh_inventory(client, {"web-01": "accepted"})
        assert count == 2
        web = get_session().get(Minion, "web-01")
        assert web.grains == {"os": "Fedora", "ipv4": ["10.0.0.1"]}
        assert web.key_status == "accepted"  # live map wins
        assert web.last_seen is not None
        db = get_session().get(Minion, "db-01")
        assert db.key_status == "accepted"  # default for new rows
        assert db.last_seen is not None


def test_refresh_skips_non_dict_grains_and_keeps_status(app):
    with app.app_context():
        get_session().add(
            Minion(
                id="web-01", grains={"os": "X"}, conformity={}, key_status="rejected"
            )
        )
        get_session().commit()
        client = StubClient(
            {
                "web-01": "not-a-mapping",
                "db-01": None,
            }
        )
        assert refresh_inventory(client, {}) == 0
        web = get_session().get(Minion, "web-01")
        assert web.grains == {"os": "X"}  # untouched
        assert web.key_status == "rejected"
        assert get_session().get(Minion, "db-01") is None
