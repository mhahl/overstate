"""One-click NaCl master setup: exec orchestration, safety, and the route."""

import base64

import pytest
from nacl.public import PrivateKey

from overstate_ui import create_app, pillar_setup
from overstate_ui.auth import _ph, seed_admin
from overstate_ui.config import TestConfig
from overstate_ui.db import create_all, get_session, init_db
from overstate_ui.models import User

PW = "pw"


def _pk() -> str:
    return base64.b64encode(bytes(PrivateKey.generate().public_key)).decode()


class FakeK8s:
    """Routes exec calls by the script they carry; records every call."""

    def __init__(self, pods, *, key_present=False, keygen_ok=True, digest_mismatch=False):
        self.pods = pods
        self.key_present = key_present
        self.keygen_ok = keygen_ok
        self.digest_mismatch = digest_mismatch
        self.calls = []
        self.public_key = _pk()
        self.secret_key = base64.b64encode(bytes(PrivateKey.generate())).decode()

    def list_pods(self, label):
        return [{"name": n} for n in self.pods]

    def exec_command(self, pod, command, container=None, timeout=60.0):
        script = command[-1] if command else ""
        self.calls.append((pod, script))
        if "test -s" in script:
            return {"ok": True, "exit_code": 0,
                    "stdout": "present\n" if self.key_present else "absent\n", "stderr": ""}
        if "nacl.keygen" in script:
            return {"ok": self.keygen_ok, "exit_code": 0 if self.keygen_ok else 1,
                    "stdout": "", "stderr": "" if self.keygen_ok else "keygen failed"}
        if command[:2] == ["/bin/cat", pillar_setup.PUB_FILE]:
            return {"ok": True, "exit_code": 0, "stdout": self.public_key + "\n", "stderr": ""}
        if command[:2] == ["/bin/cat", pillar_setup.KEY_FILE]:
            return {"ok": True, "exit_code": 0, "stdout": self.secret_key + "\n", "stderr": ""}
        if "sha256sum" in script and "printf" not in script:
            return {"ok": True, "exit_code": 0, "stdout": "abc123\n", "stderr": ""}
        if "printf" in script:
            out = "different\n" if self.digest_mismatch else "abc123\n"
            return {"ok": True, "exit_code": 0, "stdout": out, "stderr": ""}
        return {"ok": True, "exit_code": 0, "stdout": "", "stderr": ""}


@pytest.fixture()
def db_app():
    init_db("sqlite://")
    app = create_app(TestConfig)
    app.config["WTF_CSRF_ENABLED"] = False
    with app.app_context():
        create_all()
        seed_admin(password=PW)
        get_session().add(
            User(username="op", role="operator", password_hash=_ph.hash(PW))
        )
        get_session().commit()
    c = app.test_client()
    c.post("/login", data={"username": "admin", "password": PW})
    c.app = app
    return c


def _install(monkeypatch, fake):
    monkeypatch.setattr(pillar_setup, "K8sClient", lambda: fake)


def test_setup_generates_copies_and_stores(monkeypatch, db_app):
    fake = FakeK8s(["salt-master-0", "salt-master-1", "salt-master-2"])
    _install(monkeypatch, fake)
    with db_app.app.app_context():
        out = pillar_setup.setup_nacl("admin")
        from overstate_ui.settings import get_setting

        assert out["ok"] is True
        assert get_setting("pillar_nacl_pk") == fake.public_key
    pods_called = [pod for pod, _ in fake.calls]
    assert "salt-master-0" in pods_called
    assert "salt-master-1" in pods_called and "salt-master-2" in pods_called
    # Keygen ran exactly once, on the first master.
    keygens = [script for _, script in fake.calls if "nacl.keygen" in script]
    assert len(keygens) == 1
    steps = {s["step"]: s for s in out["steps"]}
    assert steps["keygen"]["ok"] and steps["store-public-key"]["ok"]


def test_setup_reuses_existing_key_instead_of_rotating(monkeypatch, db_app):
    fake = FakeK8s(["salt-master-0", "salt-master-1"], key_present=True)
    _install(monkeypatch, fake)
    with db_app.app.app_context():
        out = pillar_setup.setup_nacl("admin")
    assert out["ok"] is True
    assert not [s for _, s in fake.calls if "nacl.keygen" in s]
    assert any(s["step"] == "keygen" and "reused" in (s["detail"] or "") for s in out["steps"])


def test_setup_keygen_failure_reports_and_stops(monkeypatch, db_app):
    fake = FakeK8s(["salt-master-0", "salt-master-1"], keygen_ok=False)
    _install(monkeypatch, fake)
    with db_app.app.app_context():
        out = pillar_setup.setup_nacl("admin")
        from overstate_ui.settings import get_setting

        assert out["ok"] is False
        assert get_setting("pillar_nacl_pk") == ""
    assert not [pod for pod, _ in fake.calls if pod != "salt-master-0"]


def test_setup_digest_mismatch_fails(monkeypatch, db_app):
    fake = FakeK8s(
        ["salt-master-0", "salt-master-1"], key_present=True, digest_mismatch=True
    )
    _install(monkeypatch, fake)
    with db_app.app.app_context():
        out = pillar_setup.setup_nacl("admin")
    assert out["ok"] is False
    failed = [s for s in out["steps"] if not s["ok"]]
    assert failed and failed[0]["step"] == "install:salt-master-1"


def test_setup_requires_admin_route(monkeypatch, db_app, tmp_path):
    fake = FakeK8s(["salt-master-0"])
    _install(monkeypatch, fake)
    rv = db_app.post("/pillar/secrets/setup", follow_redirects=True)
    assert rv.status_code == 200
    assert "NaCl keypair installed" in rv.data.decode()
    db_app.post("/logout")
    db_app.post("/login", data={"username": "op", "password": PW})
    assert db_app.post("/pillar/secrets/setup").status_code == 403
