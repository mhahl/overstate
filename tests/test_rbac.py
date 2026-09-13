"""v2 unit 1 tests: role matrix, OIDC provisioning, admin user management."""

import pytest

from overstate_ui import auth as authmod
from overstate_ui import create_app
from overstate_ui.auth import provision_oidc_user, seed_admin
from overstate_ui.config import TestConfig
from overstate_ui.db import create_all, get_session, init_db
from overstate_ui.models import User

PW = "test-password"


def _make_user(username: str, role: str) -> None:
    session = get_session()
    session.add(User(username=username, password_hash=authmod._ph.hash(PW), role=role))
    session.commit()


@pytest.fixture()
def client():
    init_db("sqlite://")
    app = create_app(TestConfig)
    app.config["WTF_CSRF_ENABLED"] = False
    with app.app_context():
        create_all()
        seed_admin(password=PW)
        _make_user("op", "operator")
        _make_user("vwr", "viewer")
    c = app.test_client()
    c.app = app
    return c


def login_as(client, username: str):
    client.post("/logout")
    return client.post("/login", data={"username": username, "password": PW})


def test_seed_admin_gets_admin_role(client):
    with client.app.app_context():
        admin = get_session().query(User).filter_by(username="admin").one()
        assert admin.role == "admin"


MUTATIONS = [
    ("post", "/jobs/run", {}),
    ("post", "/minions/refresh", {}),
    ("post", "/states/watch", {"sls": "x"}),
    ("post", "/states/recompute", {}),
    ("post", "/keys/accept", {}),
    ("post", "/schedules/m1/enable", {}),
    ("post", "/schedules/m1/add", {}),
    ("post", "/minions/m1/beacons/enable", {}),
    ("post", "/minions/m1/beacons/disable", {}),
    ("post", "/settings/", {"theme": "dark"}),
    ("get", "/users/", None),
]


@pytest.mark.parametrize("method,path,data", MUTATIONS)
def test_viewer_blocked_from_mutations(client, method, path, data):
    login_as(client, "vwr")
    rv = client.post(path, data=data) if method == "post" else client.get(path)
    assert rv.status_code == 403


def test_viewer_can_still_read(client):
    login_as(client, "vwr")
    assert client.get("/").status_code == 200
    assert client.get("/states/").status_code == 200


def test_operator_can_mutate_but_not_admin_routes(client):
    login_as(client, "op")
    assert client.post("/states/watch", data={"sls": "demo"}).status_code == 302
    assert client.post("/settings/", data={"theme": "dark"}).status_code == 403
    assert client.get("/users/").status_code == 403


def test_admin_users_page_filters_by_role_and_sorts(client):
    login_as(client, "admin")
    html = client.get("/users/?role=viewer").data.decode()
    assert '<td class="font-medium">vwr</td>' in html
    assert '<td class="font-medium">op</td>' not in html
    html = client.get("/users/?sort=username&dir=desc").data.decode()
    assert html.find('<td class="font-medium">vwr</td>') < html.find(
        '<td class="font-medium">admin</td>'
    )
    assert 'aria-sort="desc"' in html


def test_admin_users_page_set_role_and_self_guard(client):
    login_as(client, "admin")
    assert client.get("/users/").status_code == 200
    with client.app.app_context():
        vwr = get_session().query(User).filter_by(username="vwr").one()
        uid, admin_id = (
            vwr.id,
            get_session().query(User).filter_by(username="admin").one().id,
        )
    rv = client.post(f"/users/{uid}/role", data={"role": "operator"})
    assert rv.status_code == 302
    with client.app.app_context():
        assert get_session().get(User, uid).role == "operator"
    # cannot demote yourself
    rv = client.post(f"/users/{admin_id}/role", data={"role": "viewer"})
    assert rv.status_code == 302
    with client.app.app_context():
        assert get_session().get(User, admin_id).role == "admin"


def test_provision_oidc_user_defaults_to_viewer(client):
    with client.app.app_context():
        user = provision_oidc_user(
            {"sub": "s1", "preferred_username": "sso-alice", "email": "a@example.com"},
            "https://idp.example",
        )
        assert user.role == "viewer"
        assert user.password_hash is None
        # group mapping wins at each login, resetting manual edits
        user.role = "operator"
        get_session().commit()
        same = provision_oidc_user(
            {"sub": "s1", "preferred_username": "sso-alice"}, "https://idp.example"
        )
        assert same.id == user.id and same.role == "viewer"


def test_provision_oidc_user_rejects_empty_claims(client):
    with client.app.app_context(), pytest.raises(ValueError):
        provision_oidc_user({}, "https://idp.example")


def test_oidc_routes_404_when_disabled(client):
    assert client.get("/login/oidc").status_code == 404
    assert client.get("/login/oidc/callback").status_code == 404
    assert b"Log in with SSO" not in client.get("/login").data


def test_oidc_callback_provisions_and_logs_in(client, monkeypatch):
    class FakeOidc:
        def authorize_access_token(self):
            return {"userinfo": {"sub": "bob-sub", "preferred_username": "sso-bob"}}

    monkeypatch.setattr(authmod, "_oauth_client", lambda: FakeOidc())
    client.app.config.update(
        OIDC_ISSUER="https://idp.example", OIDC_CLIENT_ID="x", OIDC_CLIENT_SECRET="y"
    )
    assert b"Log in with SSO" in client.get("/login").data
    rv = client.get("/login/oidc/callback")
    assert rv.status_code == 302
    assert client.get("/").status_code == 200
    with client.app.app_context():
        user = get_session().query(User).filter_by(username="sso-bob").one()
        assert user.role == "viewer"
