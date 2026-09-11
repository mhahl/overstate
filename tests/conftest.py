"""Shared fixtures. The login rate limiter is process-global and keyed by
IP, and every test client posts from 127.0.0.1 — clear it per test so
tests stay isolated from each other's login volume."""

import pytest

from overstate_ui import auth


@pytest.fixture(autouse=True)
def _clear_login_attempts():
    auth._attempts.clear()
    yield
    auth._attempts.clear()
