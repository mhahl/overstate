"""Phase 1 auth tests: login flow, seed-once, CSRF on logout."""

import pytest

from overstate_ui import create_app
from overstate_ui.auth import seed_admin
from overstate_ui.config import TestConfig
from overstate_ui.db import create_all, init_db


@pytest.fixture()
def client(tmp_path):
    init_db("sqlite://")
    app = create_app(TestConfig)
    app.config["WTF_CSRF_ENABLED"] = False
    with app.app_context():
        create_all()
        seed_admin(password="test-password")
    return app.test_client()


def test_login_logout_roundtrip(client):
    assert client.get("/").status_code == 302  # login required
    rv = client.post(
        "/login", data={"username": "admin", "password": "test-password"}
    )
    assert rv.status_code == 302
    assert client.get("/").status_code == 200
    assert client.post("/logout").status_code == 302
    assert client.get("/").status_code == 302


def test_bad_password_rejected(client):
    rv = client.post("/login", data={"username": "admin", "password": "wrong"})
    assert rv.status_code == 200
    assert client.get("/").status_code == 302


def test_successful_login_resets_rate_limit(client):
    def failing(n):
        for _ in range(n):
            client.post("/login", data={"username": "admin", "password": "wrong"})

    def succeeds():
        rv = client.post(
            "/login", data={"username": "admin", "password": "test-password"}
        )
        assert rv.status_code == 302
        client.post("/logout")

    failing(9)
    succeeds()  # clears the bucket; without the reset the next wave locks out
    failing(9)
    succeeds()


def test_seed_admin_only_when_empty():
    import overstate_ui.db as dbmod
    from overstate_ui import create_app as _ca

    app = _ca(TestConfig)
    with app.app_context():
        dbmod.create_all()
        assert seed_admin(password="one") is True
        assert seed_admin(password="two") is False
    dbmod._Session.remove()
