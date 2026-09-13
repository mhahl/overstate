"""Audit page tests: render, filters, pagination, role reach."""

from overstate_ui import create_app
from overstate_ui.audit import log_event
from overstate_ui.auth import seed_admin
from overstate_ui.config import TestConfig
from overstate_ui.db import create_all, get_session, init_db
from overstate_ui.models import User


def make_client():
    init_db("sqlite://")
    app = create_app(TestConfig)
    app.config["WTF_CSRF_ENABLED"] = False
    with app.app_context():
        create_all()
        seed_admin(password="pw")
        get_session().add(User(username="vwr", password_hash="x", role="viewer"))
        get_session().commit()
        for i in range(30):
            log_event(
                "admin" if i % 2 else "op",
                f"action-{i:02d}",
                jid=f"jid-{i:02d}" if i % 3 == 0 else None,
            )
    c = app.test_client()
    c.post("/login", data={"username": "admin", "password": "pw"})
    return c


def login_as(client, username, password):
    client.post("/logout")
    client.post("/login", data={"username": username, "password": password})


def test_audit_renders_newest_first():
    html = make_client().get("/audit/").data.decode()
    assert "Audit" in html
    assert html.find("action-29") < html.find("action-05")
    assert "action-00" not in html  # page 2 at default page size
    html = make_client().get("/audit/?page=2").data.decode()
    assert "action-00" in html
    assert "/jobs/jid-00" in html  # JID links to job detail


def test_audit_filters_narrow():
    c = make_client()
    html = c.get("/audit/?user=op").data.decode()
    assert "action-28" in html and "action-29" not in html
    html = c.get("/audit/?action=run").data.decode()
    assert "No events match." in html
    html = c.get("/audit/?action=action-1").data.decode()
    assert "action-19" in html and "action-00" not in html


def test_audit_sorts_by_action_and_user():
    c = make_client()
    html = c.get("/audit/?sort=action&dir=asc").data.decode()
    assert html.find("action-00") < html.find("action-01")
    assert 'aria-sort="asc"' in html
    html = c.get("/audit/?sort=user&dir=asc").data.decode()
    assert html.find(">admin<") < html.find(">op<")


def test_audit_pagination_bounds():
    c = make_client()
    with c.application.app_context():
        from overstate_ui.models import Setting

        get_session().add(Setting(key="page_size", value="10"))
        get_session().commit()
    html = c.get("/audit/").data.decode()
    assert "Page 1 of 3" in html
    assert "action-29" in html and "action-19" not in html
    html = c.get("/audit/?page=99").data.decode()
    assert "Page 3 of 3" in html
    assert "action-00" in html


def test_audit_reachable_for_viewer():
    from overstate_ui import auth as authmod

    c = make_client()
    with c.application.app_context():
        user = get_session().query(User).filter_by(username="vwr").one()
        user.password_hash = authmod._ph.hash("vpw")
        get_session().commit()
    login_as(c, "vwr", "vpw")
    assert c.get("/audit/").status_code == 200
