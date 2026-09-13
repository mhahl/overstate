"""Audit helper tests: rows written with user, action, and optional JID."""

import pytest

from overstate_ui import create_app
from overstate_ui.audit import log_event
from overstate_ui.config import TestConfig
from overstate_ui.db import create_all, get_session, init_db
from overstate_ui.models import AuditEvent


@pytest.fixture()
def app_ctx():
    init_db("sqlite://")
    app = create_app(TestConfig)
    with app.app_context():
        create_all()
        yield


def test_log_event_with_jid(app_ctx):
    event = log_event("admin", "state.highstate", jid="20260910000000000001")
    assert event.id is not None
    row = get_session().query(AuditEvent).one()
    assert (row.user, row.action, row.jid) == (
        "admin",
        "state.highstate",
        "20260910000000000001",
    )


def test_log_event_without_jid(app_ctx):
    log_event("admin", "accept-key")
    row = get_session().query(AuditEvent).one()
    assert row.jid is None
