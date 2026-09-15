"""Phase 1 auth tests: login flow, seed-once, CSRF on logout."""

import pytest

from overstate_ui import create_app
from overstate_ui.auth import seed_admin
from overstate_ui.config import TestConfig
from overstate_ui.db import create_all, get_session, init_db
from overstate_ui.models import User


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
    rv = client.post("/login", data={"username": "admin", "password": "test-password"})
    assert rv.status_code == 302
    assert client.get("/").status_code == 200
    assert client.post("/logout").status_code == 302
    assert client.get("/").status_code == 302


def test_login_page_shows_logo(client):
    html = client.get("/login").data.decode()
    assert "overstate.svg" in html
    rv = client.get("/static/overstate.svg")
    assert rv.status_code == 200
    assert rv.data.lstrip().startswith(b"<?xml")


def test_login_page_uses_local_wireframe_build(client):
    html = client.get("/login").data.decode()
    assert 'data-theme="wireframe"' in html
    assert "/static/app.css" in html
    assert "cdn.jsdelivr.net" not in html


def test_authenticated_pages_use_vendored_js_and_security_headers(client):
    import os

    assert os.path.isfile("overstate_ui/static/htmx.min.js")
    assert os.path.isfile("overstate_ui/static/alpine.min.js")
    client.post("/login", data={"username": "admin", "password": "test-password"})
    rv = client.get("/events/")
    html = rv.data.decode()
    assert "cdn.jsdelivr.net" not in html
    assert "/static/htmx.min.js" in html
    assert "/static/alpine.min.js" in html
    assert rv.headers["X-Content-Type-Options"] == "nosniff"
    assert rv.headers["Referrer-Policy"] == "same-origin"
    assert rv.headers["X-Frame-Options"] == "DENY"
    csp = rv.headers["Content-Security-Policy"]
    assert "default-src 'self'" in csp
    assert "cdn.jsdelivr.net" not in csp


def test_oidc_http_issuer_refuses():
    app = _oidc_app(OIDC_ISSUER="http://idp.example.com")
    client = app.test_client()
    rv = client.get("/login/oidc")
    assert rv.status_code == 302
    assert "/login" in rv.headers["Location"]
    rv = client.get("/login/oidc/callback", follow_redirects=True)
    assert "SSO login failed." in rv.data.decode()
    assert "http://idp.example.com" not in rv.data.decode()


def test_oidc_failure_flash_hides_provider_detail(monkeypatch):
    import overstate_ui.auth as authmod

    def boom():
        raise RuntimeError("provider exploded: secret-sauce")

    monkeypatch.setattr(authmod, "_oauth_client", boom)
    app = _oidc_app()
    rv = app.test_client().get("/login/oidc/callback", follow_redirects=True)
    html = rv.data.decode()
    assert "SSO login failed." in html
    assert "secret-sauce" not in html


def test_events_note_is_live_region(client):
    client.post("/login", data={"username": "admin", "password": "test-password"})
    html = client.get("/events/").data.decode()
    assert 'id="event-note"' in html
    assert 'aria-live="polite"' in html


def test_bad_password_rejected(client):
    rv = client.post("/login", data={"username": "admin", "password": "wrong"})
    assert rv.status_code == 200
    assert client.get("/").status_code == 302
    # Categorized: an error looks like an error, not a soft warning.
    assert "alert-error" in rv.data.decode()
    assert "Invalid credentials." in rv.data.decode()


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


def _oidc_app(**overrides):
    init_db("sqlite://")
    app = create_app(TestConfig)
    app.config["WTF_CSRF_ENABLED"] = False
    app.config.update(
        {
            "OIDC_ISSUER": "https://idp.example.com",
            "OIDC_CLIENT_ID": "cid",
            "OIDC_CLIENT_SECRET": "csecret",
        }
    )
    app.config.update(overrides)
    with app.app_context():
        create_all()
    return app


def test_oidc_routes_404_when_disabled(client):
    assert client.get("/login/oidc").status_code == 404
    assert client.get("/login/oidc/callback").status_code == 404
    assert "Log in with SSO" not in client.get("/login").data.decode()


def test_oidc_login_button_shown_when_configured():
    app = _oidc_app()
    html = app.test_client().get("/login").data.decode()
    assert "Log in with SSO" in html


def test_provision_creates_viewer_with_identity_key():
    from overstate_ui.auth import provision_oidc_user

    app = _oidc_app()
    with app.app_context():
        user = provision_oidc_user(
            {"sub": "s1", "preferred_username": "alice"}, "https://idp.example.com"
        )
        assert user.role == "viewer"
        assert user.oidc_issuer == "https://idp.example.com"
        assert user.oidc_sub == "s1"
        assert user.password_hash is None
        again = provision_oidc_user(
            {"sub": "s1", "preferred_username": "alice"}, "https://idp.example.com"
        )
        assert again.id == user.id
        assert get_session().query(User).count() == 1


def test_provision_never_merges_into_local_account():
    from overstate_ui.auth import provision_oidc_user

    app = _oidc_app()
    with app.app_context():
        get_session().add(
            User(username="bob", password_hash="local-hash", role="operator")
        )
        get_session().commit()
        sso = provision_oidc_user(
            {"sub": "s2", "preferred_username": "bob"}, "https://idp.example.com"
        )
        assert sso.username != "bob"
        assert sso.role == "viewer"
        local = get_session().query(User).filter_by(username="bob").one()
        assert local.password_hash == "local-hash"
        assert local.role == "operator"
        assert local.oidc_sub is None


def test_group_mapping_assigns_roles():
    from overstate_ui.auth import provision_oidc_user

    app = _oidc_app(OIDC_ADMIN_GROUPS="sre", OIDC_OPERATOR_GROUPS="oncall,support")
    with app.app_context():
        admin = provision_oidc_user(
            {
                "sub": "a",
                "preferred_username": "root-sso",
                "groups": ["everyone", "sre"],
            },
            "https://idp.example.com",
        )
        assert admin.role == "admin"
        op = provision_oidc_user(
            {"sub": "o", "preferred_username": "op-sso", "groups": ["support"]},
            "https://idp.example.com",
        )
        assert op.role == "operator"
        viewer = provision_oidc_user(
            {"sub": "v", "preferred_username": "v-sso", "groups": ["everyone"]},
            "https://idp.example.com",
        )
        assert viewer.role == "viewer"
        # Mapping wins at each login, including over manual edits.
        viewer.role = "operator"
        relogin = provision_oidc_user(
            {"sub": "v", "preferred_username": "v-sso", "groups": ["everyone"]},
            "https://idp.example.com",
        )
        assert relogin.id == viewer.id
        assert relogin.role == "viewer"


def test_provision_rejects_missing_subject():
    import pytest

    from overstate_ui.auth import provision_oidc_user

    app = _oidc_app()
    with app.app_context(), pytest.raises(ValueError):
        provision_oidc_user({"preferred_username": "nobody"}, "https://idp.example.com")


def test_oidc_settings_page_enables_sso():
    from overstate_ui.auth import seed_admin as _seed

    init_db("sqlite://")
    app = create_app(TestConfig)
    app.config["WTF_CSRF_ENABLED"] = False
    app.config["OIDC_CLIENT_SECRET"] = "s3cret"
    with app.app_context():
        create_all()
        _seed(password="pw")
    c = app.test_client()
    assert "Log in with SSO" not in c.get("/login").data.decode()
    c.post("/login", data={"username": "admin", "password": "pw"})
    c.post(
        "/settings/",
        data={"oidc_issuer": "https://idp.example.com", "oidc_client_id": "cid"},
    )
    assert "Log in with SSO" in c.get("/login").data.decode()


def test_oidc_db_overrides_and_clears_to_env():
    from overstate_ui.db import get_session as _session
    from overstate_ui.models import Setting
    from overstate_ui.settings import get_setting

    init_db("sqlite://")
    app = create_app(TestConfig)
    app.config["OIDC_ISSUER"] = "https://env.example.com"
    with app.app_context():
        create_all()
        assert get_setting("oidc_issuer") == "https://env.example.com"
        _session().add(Setting(key="oidc_issuer", value="https://db.example.com"))
        _session().commit()
        assert get_setting("oidc_issuer") == "https://db.example.com"


def test_oidc_save_cleared_defers_to_env():
    from overstate_ui.auth import seed_admin as _seed
    from overstate_ui.settings import get_setting

    init_db("sqlite://")
    app = create_app(TestConfig)
    app.config["WTF_CSRF_ENABLED"] = False
    app.config["OIDC_ISSUER"] = "https://env.example.com"
    app.config["OIDC_CLIENT_ID"] = "cid"
    app.config["OIDC_CLIENT_SECRET"] = "s3cret"
    with app.app_context():
        create_all()
        _seed(password="pw")
    c = app.test_client()
    c.post("/login", data={"username": "admin", "password": "pw"})
    c.post("/settings/", data={"oidc_issuer": "https://db.example.com"})
    with app.app_context():
        assert get_setting("oidc_issuer") == "https://db.example.com"
    c.post("/settings/", data={"oidc_issuer": ""})
    with app.app_context():
        assert get_setting("oidc_issuer") == "https://env.example.com"


def test_oidc_client_secret_from_db_enables_sso():
    from overstate_ui.auth import seed_admin as _seed
    from overstate_ui.settings import get_setting

    init_db("sqlite://")
    app = create_app(TestConfig)
    app.config["WTF_CSRF_ENABLED"] = False
    app.config["OIDC_ISSUER"] = "https://idp.example.com"
    app.config["OIDC_CLIENT_ID"] = "cid"
    app.config["OIDC_CLIENT_SECRET"] = ""
    with app.app_context():
        create_all()
        _seed(password="pw")
    c = app.test_client()
    assert "Log in with SSO" not in c.get("/login").data.decode()
    c.post("/login", data={"username": "admin", "password": "pw"})
    assert 'type="password"' in c.get("/settings/").data.decode()
    c.post("/settings/", data={"oidc_client_secret": "db-secret"})
    with app.app_context():
        assert get_setting("oidc_client_secret") == "db-secret"
    assert "Log in with SSO" in c.get("/login").data.decode()
    c.post("/settings/", data={"oidc_client_secret": ""})
    assert "Log in with SSO" in c.get("/login").data.decode()
    c.post(
        "/settings/",
        data={"oidc_client_secret": "", "clear_oidc_client_secret": "on"},
    )
    assert "Log in with SSO" not in c.get("/login").data.decode()


def test_settings_page_groups_every_setting_into_panels():
    from overstate_ui.auth import seed_admin as _seed
    from overstate_ui.settings import DEFS, SECTIONS

    init_db("sqlite://")
    app = create_app(TestConfig)
    app.config["WTF_CSRF_ENABLED"] = False
    with app.app_context():
        create_all()
        _seed(password="pw")
    c = app.test_client()
    c.post("/login", data={"username": "admin", "password": "pw"})
    html = c.get("/settings/").data.decode()
    for section in SECTIONS:
        assert section["title"] in html
    covered = [k for s in SECTIONS for k in s["keys"]]
    assert sorted(covered) == sorted(DEFS)
    for key in DEFS:
        assert f'name="{key}"' in html


def test_callback_exchange_failure_redirects(monkeypatch):
    import overstate_ui.auth as authmod

    def boom():
        raise RuntimeError("provider down")

    monkeypatch.setattr(authmod, "_oauth_client", boom)
    app = _oidc_app()
    rv = app.test_client().get("/login/oidc/callback")
    assert rv.status_code == 302
    assert "/login" in rv.headers["Location"]


def test_seed_admin_only_when_empty():
    import overstate_ui.db as dbmod
    from overstate_ui import create_app as _ca

    app = _ca(TestConfig)
    with app.app_context():
        dbmod.create_all()
        assert seed_admin(password="one") is True
        assert seed_admin(password="two") is False
    dbmod._Session.remove()
