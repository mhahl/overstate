"""Serve-path tests (P2): workers, cookies, proxy trust, login budget."""

import pathlib

import pytest

from overstate_ui import create_app
from overstate_ui.auth import seed_admin
from overstate_ui.config import TestConfig
from overstate_ui.db import create_all, init_db

REPO = pathlib.Path(__file__).resolve().parent.parent


def _app(**overrides):
    init_db("sqlite://")
    app = create_app(TestConfig)
    app.config["WTF_CSRF_ENABLED"] = False
    app.config.update(overrides)
    with app.app_context():
        create_all()
        seed_admin(password="pw")
    return app


def test_entrypoint_defaults_to_multiple_workers():
    text = (REPO / "scripts" / "docker-entrypoint.sh").read_text()
    assert "WEB_CONCURRENCY" in text
    assert ":-4}" in text
    assert '"$WORKERS"' in text or "$WORKERS" in text
    assert "-w" in text
    assert "--timeout 90" in text


def test_placeholder_secret_key_refuses_non_testing_boot(monkeypatch):
    from overstate_ui.config import Config

    class ProdConfig(Config):
        TESTING = False
        SECRET_KEY = "change-me"
        SQLALCHEMY_DATABASE_URI = "sqlite://"

    monkeypatch.delenv("TRUST_PROXY", raising=False)
    with pytest.raises(RuntimeError):
        create_app(ProdConfig)


def test_testconfig_still_boots_on_placeholder(monkeypatch):
    monkeypatch.delenv("TRUST_PROXY", raising=False)
    _app()  # must not raise


def test_session_cookie_flags_secure_when_enabled(monkeypatch):
    monkeypatch.setenv("OVERSTATE_SECURE_COOKIES", "1")
    monkeypatch.delenv("TRUST_PROXY", raising=False)
    app = _app()
    assert app.config["SESSION_COOKIE_SECURE"] is True
    assert app.config["SESSION_COOKIE_HTTPONLY"] is True
    assert app.config["SESSION_COOKIE_SAMESITE"] == "Lax"
    client = app.test_client()
    rv = client.post("/login", data={"username": "admin", "password": "pw"})
    assert rv.status_code == 302
    cookie = rv.headers.get("Set-Cookie", "")
    assert "Secure" in cookie
    assert "HttpOnly" in cookie
    assert "SameSite=Lax" in cookie


def test_session_cookie_insecure_on_plain_local_http(monkeypatch):
    monkeypatch.delenv("OVERSTATE_SECURE_COOKIES", raising=False)
    monkeypatch.delenv("TLS_CERT", raising=False)
    monkeypatch.delenv("TRUST_PROXY", raising=False)
    app = _app()
    assert app.config["SESSION_COOKIE_SECURE"] is False


def test_login_rate_limit_blocks_after_ten_failures(monkeypatch):
    monkeypatch.delenv("TRUST_PROXY", raising=False)
    app = _app()
    client = app.test_client()
    for _ in range(10):
        rv = client.post("/login", data={"username": "admin", "password": "bad"})
        assert rv.status_code == 200
    rv = client.post("/login", data={"username": "admin", "password": "bad"})
    assert rv.status_code == 429


def test_login_budget_shared_through_redis(monkeypatch):
    import overstate_ui.auth as authmod

    store = {}

    class FakeRedis:
        def incr(self, key):
            store[key] = store.get(key, 0) + 1
            return store[key]

        def expire(self, key, window):
            store[f"{key}:ttl"] = window

        def delete(self, key):
            store.pop(key, None)

    monkeypatch.setattr("redis.from_url", lambda *a, **k: FakeRedis())
    monkeypatch.delenv("TRUST_PROXY", raising=False)
    app = _app()
    client = app.test_client()
    for _ in range(9):
        client.post("/login", data={"username": "admin", "password": "bad"})
    # Every worker increments the same username+IP key.
    assert store.get("login-attempts:127.0.0.1:admin") == 9
    rv = client.post("/login", data={"username": "admin", "password": "pw"})
    assert rv.status_code == 302
    # A correct login resets the shared budget.
    assert "login-attempts:127.0.0.1:admin" not in store
    assert authmod._attempts.get("127.0.0.1:admin") is None


def test_oidc_redirect_ignores_spoofed_host(monkeypatch):
    import overstate_ui.auth as authmod

    seen = {}

    class FakeOidc:
        def authorize_redirect(self, uri):
            seen["uri"] = uri
            from flask import redirect

            return redirect(uri)

    monkeypatch.setattr(authmod, "_oauth_client", lambda: FakeOidc())
    monkeypatch.delenv("TRUST_PROXY", raising=False)
    monkeypatch.delenv("OIDC_REDIRECT_URI", raising=False)
    monkeypatch.delenv("PUBLIC_URL", raising=False)
    app = _app(
        OIDC_ISSUER="https://idp.example.com",
        OIDC_CLIENT_ID="cid",
        OIDC_CLIENT_SECRET="csecret",
    )
    client = app.test_client()
    rv = client.get("/login/oidc", headers={"X-Forwarded-Host": "evil.example"})
    assert rv.status_code == 302
    assert "evil.example" not in seen["uri"]


def test_oidc_redirect_pinned_to_configured_uri(monkeypatch):
    import overstate_ui.auth as authmod

    seen = {}

    class FakeOidc:
        def authorize_redirect(self, uri):
            seen["uri"] = uri
            from flask import redirect

            return redirect(uri)

    monkeypatch.setattr(authmod, "_oauth_client", lambda: FakeOidc())
    monkeypatch.delenv("TRUST_PROXY", raising=False)
    app = _app(
        OIDC_ISSUER="https://idp.example.com",
        OIDC_CLIENT_ID="cid",
        OIDC_CLIENT_SECRET="csecret",
        OIDC_REDIRECT_URI="https://app.example.com/login/oidc/callback",
    )
    app.test_client().get("/login/oidc", headers={"X-Forwarded-Host": "evil.example"})
    assert seen["uri"] == "https://app.example.com/login/oidc/callback"


def test_quadlet_app_trusts_proxy_behind_caddy():
    text = (REPO / "deploy" / "quadlet" / "overstate-app.container").read_text()
    assert "TRUST_PROXY=1" in text
