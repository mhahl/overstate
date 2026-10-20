"""Encrypted-pillar tests: gpg service, YAML round-trip, editor routes,
verify flow, and the encryption settings tab.

Unit tests inject a fake gnupg backend (no gpg binary needed); one
integration test exercises a real gpg round-trip and skips when the
binary is absent (macOS homebrew/CI include it, containers build it).
"""

import shutil
import subprocess

import pytest

from overstate_ui import create_app
from overstate_ui.auth import _ph, seed_admin
from overstate_ui.config import TestConfig
from overstate_ui.db import create_all, get_session, init_db
from overstate_ui.models import User

PW = "pw"

ARMORED = "-----BEGIN PGP MESSAGE-----\nhQIMA3+FAKE\n-----END PGP MESSAGE-----"


def _git(cwd, *args):
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=False
    )


@pytest.fixture()
def secrets_env(tmp_path):
    """Git checkout: salt/ (file roots) + pillar/secrets/. No upstream."""
    work = tmp_path / "work"
    salt = work / "salt"
    salt.mkdir(parents=True)
    (salt / "top.sls").write_text("base:\n  '*': []\n")
    _git(work, "init", "-qb", "main")
    _git(work, "config", "user.email", "t@t")
    _git(work, "config", "user.name", "t")
    _git(work, "add", ".")
    _git(work, "commit", "-qm", "seed")
    init_db("sqlite://")
    app = create_app(TestConfig)
    app.config["WTF_CSRF_ENABLED"] = False
    app.config["FILE_ROOTS"] = str(salt)
    with app.app_context():
        create_all()
        seed_admin(password=PW)
        session = get_session()
        session.add(User(username="op", role="operator", password_hash=_ph.hash(PW)))
        session.add(User(username="vie", role="viewer", password_hash=_ph.hash(PW)))
        session.commit()
    c = app.test_client()
    c.post("/login", data={"username": "admin", "password": PW})
    c.app = app
    c.work = work
    return c


def _login(c, username):
    c.post("/logout")
    c.post("/login", data={"username": username, "password": PW})


def _store_pk(c, armored="-----BEGIN PGP PUBLIC KEY BLOCK-----\nabc\n-----END PGP PUBLIC KEY BLOCK-----"):
    """Store the master public key setting directly (route tested below)."""
    from overstate_ui.settings import set_setting

    with c.app.app_context():
        set_setting("pillar_gpg_pk", armored)


def _mk_collection(c, name="netbird"):
    """Create a collection through the route."""
    c.post("/pillar/secrets/collection/create", data={"name": name})


# --- gpg service -------------------------------------------------------

def test_encrypt_requires_stored_key(monkeypatch, secrets_env):
    import overstate_ui.pillar_secrets as psmod
    from overstate_ui.pillar_gpg import GpgSetupError

    monkeypatch.setattr(psmod, "encrypt_value", lambda v, pk: (_ for _ in ()).throw(GpgSetupError("no key")))
    with secrets_env.app.app_context():
        from overstate_ui.settings import get_setting

        assert get_setting("pillar_gpg_pk") == ""
    with pytest.raises(GpgSetupError):
        psmod.encrypt_value("x", "")


def test_key_id_returns_short_fingerprint(monkeypatch):
    import types

    from overstate_ui import pillar_gpg

    class FakeImport:
        fingerprints = ("0123456789abcdef0123456789abcdef01234567",)

    class FakeGPG:
        def __init__(self, gnupghome=None):
            self.encoding = "utf-8"

        def import_keys(self, armored):
            assert armored
            return FakeImport()

    monkeypatch.setattr(
        pillar_gpg, "gnupg", types.SimpleNamespace(GPG=FakeGPG)
    )
    assert pillar_gpg.key_id("-----BEGIN PGP PUBLIC KEY BLOCK-----\n") == "89abcdef01234567"


def test_encrypt_value_produces_armored_block():
    import tempfile

    import gnupg

    if not shutil.which("gpg"):
        pytest.skip("gpg binary not available")
    from overstate_ui import pillar_gpg

    # Generate a real keypair in a scratch homedir and encrypt to it.
    with tempfile.TemporaryDirectory() as home:
        g = gnupg.GPG(gnupghome=home, options=["--pinentry-mode", "loopback"])
        g.encoding = "utf-8"
        gen = g.gen_key(
            "Key-Type: RSA\nKey-Length: 1024\nName-Real: t\n%no-protection\n%commit\n"
        )
        assert gen.fingerprint
        pub = g.export_keys(gen.fingerprint)
        block = pillar_gpg.encrypt_value("hello-world", pub)
        assert block.strip().startswith(pillar_gpg.ARMOR_HEADER)
        # Decrypt with the private key: proves a real round-trip.
        decrypted = g.decrypt(block)
        assert str(decrypted) == "hello-world"
        assert pillar_gpg.is_encrypted(block)
        assert not pillar_gpg.is_encrypted("hello-world")


# --- YAML round-trip ---------------------------------------------------

def test_roundtrip_preserves_comments_and_shebang(secrets_env):
    import overstate_ui.pillar_secrets as psmod

    with secrets_env.app.app_context():
        path = psmod._secrets_dir() / "netbird.sls"
        path.parent.mkdir(parents=True)
        path.write_text(
            "# netbird setup\nnetbird:\n  setup_key: |\n    -----BEGIN PGP MESSAGE-----\n\n    abc\n    -----END PGP MESSAGE-----\n"
        )
        data = psmod._load_rt(path)
        assert data["netbird"]["setup_key"].startswith("-----BEGIN PGP MESSAGE-----")
        psmod._set_path(data, "netbird.extra", ARMORED)
        psmod._dump_rt(data, path)
        text = path.read_text()
        assert "# netbird setup" in text  # comment preserved
        assert "extra:" in text
        assert "-----BEGIN PGP MESSAGE-----" in text


def test_del_prunes_empty_parents():
    from overstate_ui import pillar_secrets as psmod

    data = {"a": {"b": {"c": ARMORED}, "keep": 1}}
    assert psmod._del_path(data, "a.b.c")
    assert data == {"a": {"keep": 1}}


def test_dotted_key_validation():
    from overstate_ui import pillar_secrets as psmod
    from overstate_ui.pillar_gpg import GpgSetupError

    with pytest.raises(GpgSetupError):
        psmod._validate_dotted_key("")
    with pytest.raises(GpgSetupError):
        psmod._validate_dotted_key("a..b")
    with pytest.raises(GpgSetupError):
        psmod._validate_dotted_key("a b")
    psmod._validate_dotted_key("app.db.password")


# --- editor routes -----------------------------------------------------

def test_index_lists_collections_and_keys(secrets_env, monkeypatch):
    import overstate_ui.pillar_secrets as psmod

    monkeypatch.setattr(psmod, "key_id", lambda pk: "f1ce" if pk else None)
    _store_pk(secrets_env)
    _mk_collection(secrets_env, "netbird")
    with secrets_env.app.app_context():
        path = psmod._collection_path("netbird")
        path.write_text("netbird:\n  setup_key: |\n    -----BEGIN PGP MESSAGE-----\n\n    abc\n    -----END PGP MESSAGE-----\n")
    html = secrets_env.get("/pillar/secrets/").data.decode()
    assert "netbird" in html
    assert "netbird.setup_key" in html
    assert "encrypted" in html


def test_collection_create_commits(secrets_env):
    _mk_collection(secrets_env, "netbird")
    path = secrets_env.work / "pillar" / "secrets" / "netbird.sls"
    assert path.is_file()
    proc = _git(secrets_env.work, "status", "--porcelain")
    assert proc.stdout.strip() == ""  # committed, tree clean


def test_add_secret_encrypts_and_commits(secrets_env, monkeypatch):
    import overstate_ui.pillar_secrets as psmod
    from overstate_ui.models import AuditEvent

    calls = {}

    def fake_encrypt(value, pk):
        calls["value"] = value
        return ARMORED

    monkeypatch.setattr(psmod, "encrypt_value", fake_encrypt)
    _store_pk(secrets_env)
    _mk_collection(secrets_env, "netbird")
    rv = secrets_env.post(
        "/pillar/secrets/secret/add",
        data={"collection": "netbird", "key": "netbird.setup_key", "value": "S3cret!"},
        follow_redirects=True,
    )
    assert rv.status_code == 200
    assert "added" in rv.data.decode().lower()
    # Ciphertext lands in the git tree as a literal block; parse it back.
    from ruamel.yaml import YAML

    parsed = YAML(typ="safe").load(
        (secrets_env.work / "pillar" / "secrets" / "netbird.sls").read_text()
    )
    assert parsed["netbird"]["setup_key"] == ARMORED
    text = (secrets_env.work / "pillar" / "secrets" / "netbird.sls").read_text()
    assert "S3cret!" not in text  # plaintext never hits disk
    proc = _git(secrets_env.work, "status", "--porcelain")
    assert proc.stdout.strip() == ""
    with secrets_env.app.app_context():
        assert get_session().query(AuditEvent).filter_by(action="pillar-secret:add:netbird:netbird.setup_key").count() == 1


def test_update_requires_confirm_gate(secrets_env, monkeypatch):
    import overstate_ui.pillar_secrets as psmod

    def varying(v, pk):
        return f"-----BEGIN PGP MESSAGE-----\n{v}\n-----END PGP MESSAGE-----"

    monkeypatch.setattr(psmod, "encrypt_value", varying)
    _store_pk(secrets_env)
    _mk_collection(secrets_env, "netbird")
    secrets_env.post(
        "/pillar/secrets/secret/add",
        data={"collection": "netbird", "key": "netbird.setup_key", "value": "old"},
    )
    rv = secrets_env.post(
        "/pillar/secrets/secret/update",
        data={"collection": "netbird", "key": "netbird.setup_key", "value": "new"},
        follow_redirects=True,
    )
    assert "Confirm that the old value" in rv.data.decode()
    rv = secrets_env.post(
        "/pillar/secrets/secret/update",
        data={"collection": "netbird", "key": "netbird.setup_key", "value": "new", "confirm_box": "1"},
        follow_redirects=True,
    )
    assert "updated" in rv.data.decode().lower()


def test_add_refuses_existing_key_without_update(secrets_env, monkeypatch):
    import overstate_ui.pillar_secrets as psmod

    monkeypatch.setattr(psmod, "encrypt_value", lambda v, pk: ARMORED)
    _store_pk(secrets_env)
    _mk_collection(secrets_env, "netbird")
    secrets_env.post(
        "/pillar/secrets/secret/add",
        data={"collection": "netbird", "key": "netbird.setup_key", "value": "old"},
    )
    rv = secrets_env.post(
        "/pillar/secrets/secret/add",
        data={"collection": "netbird", "key": "netbird.setup_key", "value": "x"},
        follow_redirects=True,
    )
    assert "already exists" in rv.data.decode()


def test_delete_removes_key_and_commits(secrets_env, monkeypatch):
    import overstate_ui.pillar_secrets as psmod

    monkeypatch.setattr(psmod, "encrypt_value", lambda v, pk: ARMORED)
    _store_pk(secrets_env)
    _mk_collection(secrets_env, "netbird")
    secrets_env.post(
        "/pillar/secrets/secret/add",
        data={"collection": "netbird", "key": "netbird.setup_key", "value": "abc"},
    )
    rv = secrets_env.post(
        "/pillar/secrets/secret/delete",
        data={"collection": "netbird", "key": "netbird.setup_key"},
        follow_redirects=True,
    )
    assert "deleted" in rv.data.decode().lower()
    text = (secrets_env.work / "pillar" / "secrets" / "netbird.sls").read_text()
    assert "setup_key" not in text


def test_wire_top_sls_idempotent(secrets_env):
    _mk_collection(secrets_env, "netbird")
    secrets_env.post("/pillar/secrets/top/wire", data={"collection": "netbird"})
    top = (secrets_env.work / "pillar" / "top.sls").read_text()
    assert "secrets.netbird" in top
    secrets_env.post("/pillar/secrets/top/wire", data={"collection": "netbird"})
    top2 = (secrets_env.work / "pillar" / "top.sls").read_text()
    assert top2.count("secrets.netbird") == 1


# --- verify flow -------------------------------------------------------

def test_verify_roundtrip_success(secrets_env, monkeypatch):
    import overstate_ui.pillar_secrets as psmod
    from overstate_ui.models import AuditEvent

    # Fake encrypt that round-trips through a module dict so the fake
    # read-back can "decrypt" the armored value back to its plaintext.
    store = {}

    def fake_encrypt(value, pk):
        block = f"-----BEGIN PGP MESSAGE-----\n{value}\n-----END PGP MESSAGE-----"
        store[block] = value
        return block

    monkeypatch.setattr(psmod, "encrypt_value", fake_encrypt)

    def fake_read(minion):
        from ruamel.yaml import YAML

        text = (secrets_env.work / "pillar" / "secrets" / "netbird.sls").read_text()
        data = YAML(typ="safe").load(text) or {}
        value = data.get("_overstate_verify")
        if value in store:
            return {"_overstate_verify": store[value]}
        return {}

    monkeypatch.setattr(psmod, "_read_pillar", fake_read)
    monkeypatch.setattr(psmod, "_refresh_pillar", lambda m: None)
    _store_pk(secrets_env)
    _mk_collection(secrets_env, "netbird")
    secrets_env.post("/pillar/secrets/top/wire", data={"collection": "netbird"})
    rv = secrets_env.post(
        "/pillar/secrets/verify",
        data={"collection": "netbird", "minion": "web-01"},
        follow_redirects=True,
    )
    assert "verified end to end" in rv.data.decode()
    # The throwaway key is gone from the tree.
    text = (secrets_env.work / "pillar" / "secrets" / "netbird.sls").read_text()
    assert "_overstate_verify" not in text
    with secrets_env.app.app_context():
        assert get_session().query(AuditEvent).filter_by(action="pillar-secret:verify:netbird").count() == 1


def test_verify_reports_mismatch(secrets_env, monkeypatch):
    import overstate_ui.pillar_secrets as psmod

    monkeypatch.setattr(psmod, "encrypt_value", lambda v, pk: ARMORED)
    monkeypatch.setattr(psmod, "_read_pillar", lambda m: {})
    monkeypatch.setattr(psmod, "_refresh_pillar", lambda m: None)
    _store_pk(secrets_env)
    _mk_collection(secrets_env, "netbird")
    rv = secrets_env.post(
        "/pillar/secrets/verify",
        data={"collection": "netbird", "minion": "web-01"},
        follow_redirects=True,
    )
    assert "Verification failed" in rv.data.decode()
    text = (secrets_env.work / "pillar" / "secrets" / "netbird.sls").read_text()
    assert "_overstate_verify" not in text  # cleaned up even on failure


# --- RBAC --------------------------------------------------------------

def test_viewer_cannot_write(secrets_env, monkeypatch):
    import overstate_ui.pillar_secrets as psmod

    monkeypatch.setattr(psmod, "encrypt_value", lambda v, pk: ARMORED)
    _store_pk(secrets_env)
    _mk_collection(secrets_env, "netbird")
    _login(secrets_env, "vie")
    rv = secrets_env.post(
        "/pillar/secrets/secret/add",
        data={"collection": "netbird", "key": "x", "value": "y"},
        follow_redirects=True,
    )
    assert rv.status_code == 403


def test_viewer_can_read_editor(secrets_env):
    _login(secrets_env, "vie")
    rv = secrets_env.get("/pillar/secrets/")
    assert rv.status_code == 200


def test_scoped_operator_without_grant_forbidden(secrets_env, monkeypatch):
    import overstate_ui.pillar_secrets as psmod
    from overstate_ui.models import Setting

    monkeypatch.setattr(psmod, "encrypt_value", lambda v, pk: ARMORED)
    with secrets_env.app.app_context():
        get_session().add(Setting(key="rbac_mode", value="scoped"))
        get_session().add(
            User(username="teamop", role="scoped", password_hash=_ph.hash(PW))
        )
        get_session().commit()
    _store_pk(secrets_env)
    _mk_collection(secrets_env, "netbird")
    _login(secrets_env, "teamop")
    rv = secrets_env.post(
        "/pillar/secrets/secret/add",
        data={"collection": "netbird", "key": "x", "value": "y"},
        follow_redirects=True,
    )
    assert rv.status_code == 403


def test_scoped_operator_with_grant_can_write(secrets_env, monkeypatch):
    import overstate_ui.pillar_secrets as psmod
    from overstate_ui.models import Grant, Setting

    monkeypatch.setattr(psmod, "encrypt_value", lambda v, pk: ARMORED)
    with secrets_env.app.app_context():
        get_session().add(Setting(key="rbac_mode", value="scoped"))
        teamop = User(username="teamop", role="scoped", password_hash=_ph.hash(PW))
        get_session().add(teamop)
        get_session().flush()
        get_session().add(
            Grant(
                subject_kind="user",
                subject_user_id=teamop.id,
                subject_group_id=None,
                role="team-operator",
                scope_kind="fleet",
                scope_value="*",
                source="manual",
            )
        )
        get_session().commit()
    _store_pk(secrets_env)
    _mk_collection(secrets_env, "netbird")
    _login(secrets_env, "teamop")
    rv = secrets_env.post(
        "/pillar/secrets/secret/add",
        data={"collection": "netbird", "key": "x", "value": "y"},
        follow_redirects=True,
    )
    assert rv.status_code == 200


# --- encryption settings tab -------------------------------------------

def test_settings_tab_requires_admin(secrets_env):
    _login(secrets_env, "op")
    assert secrets_env.get("/settings/encryption/").status_code == 403
    _login(secrets_env, "admin")
    assert secrets_env.get("/settings/encryption/").status_code == 200


def test_settings_tab_store_and_replace_gate(secrets_env, monkeypatch):
    import overstate_ui.pillar_secrets as psmod
    from overstate_ui.settings import get_setting

    monkeypatch.setattr(psmod, "key_id", lambda pk: "f1ce" if "GOOD" in pk else None)
    rv = secrets_env.post(
        "/settings/encryption/store", data={"pk": "BAD"}, follow_redirects=True
    )
    assert "does not look like a usable" in rv.data.decode()
    rv = secrets_env.post(
        "/settings/encryption/store",
        data={"pk": "GOOD-PUBKEY"},
        follow_redirects=True,
    )
    assert "stored" in rv.data.decode().lower()
    with secrets_env.app.app_context():
        assert get_setting("pillar_gpg_pk") == "GOOD-PUBKEY"
    # Replace without the checkbox gate is refused.
    rv = secrets_env.post(
        "/settings/encryption/store", data={"pk": "GOOD-PUBKEY-2"}, follow_redirects=True
    )
    assert "confirm to replace" in rv.data.decode()
    rv = secrets_env.post(
        "/settings/encryption/store",
        data={"pk": "GOOD-PUBKEY-2", "confirm_box": "1"},
        follow_redirects=True,
    )
    assert "stored" in rv.data.decode().lower()
    with secrets_env.app.app_context():
        assert get_setting("pillar_gpg_pk") == "GOOD-PUBKEY-2"
    # Clear requires the gate too.
    secrets_env.post("/settings/encryption/clear", data={})
    with secrets_env.app.app_context():
        assert get_setting("pillar_gpg_pk") == "GOOD-PUBKEY-2"
    secrets_env.post(
        "/settings/encryption/clear", data={"confirm_box": "1"}, follow_redirects=True
    )
    with secrets_env.app.app_context():
        assert get_setting("pillar_gpg_pk") == ""


# --- Pillar capture honesty -------------------------------------------

def test_pillar_detail_banner_lists_encrypted_keys(secrets_env, monkeypatch):
    import overstate_ui.pillar_secrets as psmod

    monkeypatch.setattr(psmod, "encrypt_value", lambda v, pk: ARMORED)
    _store_pk(secrets_env)
    _mk_collection(secrets_env, "netbird")
    secrets_env.post(
        "/pillar/secrets/secret/add",
        data={"collection": "netbird", "key": "netbird.setup_key", "value": "x"},
    )
    html = secrets_env.get("/pillar/web-01").data.decode()
    assert "Encrypted pillar values are included" in html
    assert "netbird.setup_key" in html  # key name, never the value
    assert "plaintext" in html


def test_pillar_detail_no_banner_without_secrets(secrets_env):
    html = secrets_env.get("/pillar/web-01").data.decode()
    assert "Encrypted pillar values are included" not in html


# --- Pillar section tabs ----------------------------------------------

def _pillar_tab_bar(html):
    """The tab bar markup, split into one segment per tab link."""
    bar = html.split('aria-label="Pillar views"', 1)[1].split("</div>", 1)[0]
    return bar.split('role="tab"')[1:]


def _aside(html):
    return html.split('id="nav-panel"', 1)[1].split("</aside>", 1)[0]


def test_sidebar_has_one_pillar_entry(secrets_env):
    for path in ("/pillar/", "/pillar/secrets/"):
        html = secrets_env.get(path).data.decode()
        aside = _aside(html)
        assert 'href="/pillar/"' in aside
        # The editor is a tab now, not a second sidebar item.
        assert 'href="/pillar/secrets/"' not in aside
        # Exactly one nav item highlights for both pillar routes.
        assert aside.count("menu-active") == 1


def test_tabs_mark_exactly_one_active(secrets_env):
    explorer = _pillar_tab_bar(secrets_env.get("/pillar/").data.decode())
    assert len(explorer) == 2
    assert "tab-active" in explorer[0] and "pillar/secrets" not in explorer[0]
    assert "tab-active" not in explorer[1] and "pillar/secrets" in explorer[1]
    assert 'aria-current="page"' in explorer[0]

    editor = _pillar_tab_bar(secrets_env.get("/pillar/secrets/").data.decode())
    assert len(editor) == 2
    assert "tab-active" not in editor[0] and "pillar/secrets" not in editor[0]
    assert "tab-active" in editor[1] and "pillar/secrets" in editor[1]
    assert 'aria-current="page"' in editor[1]


def test_detail_keeps_explorer_tab_active(secrets_env):
    tabs = _pillar_tab_bar(secrets_env.get("/pillar/web-01").data.decode())
    assert "tab-active" in tabs[0]
    assert "tab-active" not in tabs[1]
