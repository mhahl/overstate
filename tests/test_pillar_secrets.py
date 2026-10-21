"""Encrypted-pillar tests: NaCl sealedbox service, YAML round-trip, editor
routes, verify flow, onboarding copy, and the encryption settings tab.

Encryption is pure PyNaCl, so the crypto tests run with real keys — no
external binary and no skip paths.
"""

import base64
import subprocess

import pytest
from nacl.public import PrivateKey, SealedBox

from overstate_ui import create_app
from overstate_ui.auth import _ph, seed_admin
from overstate_ui.config import TestConfig
from overstate_ui.db import create_all, get_session, init_db
from overstate_ui.models import User

PW = "pw"

# Deterministic stand-in for monkeypatched route tests. is_encrypted only
# checks the envelope, so a literal marker is enough there.
NACL = "NACL[ZmFrZS1jaXBoZXJ0ZXh0]"


def _keypair() -> tuple[str, bytes]:
    """(base64 public key, raw secret key) for real cryptography tests."""
    secret = PrivateKey.generate()
    return base64.b64encode(bytes(secret.public_key)).decode(), bytes(secret)


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


def _store_pk(c):
    """Store a real public key setting (route coverage is separate)."""
    from overstate_ui.settings import set_setting

    pk, _sk = _keypair()
    with c.app.app_context():
        set_setting("pillar_nacl_pk", pk)
    return pk


def _mk_collection(c, name="netbird"):
    c.post("/pillar/secrets/collection/create", data={"name": name})


# --- NaCl service ------------------------------------------------------

def test_encrypt_roundtrip_and_envelope():
    from overstate_ui import pillar_nacl

    pk, sk = _keypair()
    block = pillar_nacl.encrypt_value("hello-world", pk)
    assert block.startswith("NACL[") and block.endswith("]")
    assert pillar_nacl.is_encrypted(block)
    assert not pillar_nacl.is_encrypted("hello-world")
    sealed = base64.b64decode(block[len("NACL[") : -1])
    assert SealedBox(PrivateKey(sk)).decrypt(sealed) == b"hello-world"
    # Sealedbox is randomized: same plaintext, different ciphertext.
    assert pillar_nacl.encrypt_value("hello-world", pk) != block


def test_key_id_is_short_and_stable():
    from overstate_ui import pillar_nacl

    pk, _ = _keypair()
    pk2, _ = _keypair()
    first = pillar_nacl.key_id(pk)
    assert first and len(first) == 16
    assert first == pillar_nacl.key_id(pk)
    assert first != pillar_nacl.key_id(pk2)


def test_bad_public_keys_raise():
    from overstate_ui import pillar_nacl

    for bad in ("", "not-base64!!", base64.b64encode(b"short").decode()):
        with pytest.raises(pillar_nacl.NaclSetupError):
            pillar_nacl.encrypt_value("x", bad)
        assert pillar_nacl.key_id(bad) is None


# --- YAML round-trip ---------------------------------------------------

def test_roundtrip_preserves_comments(secrets_env):
    import overstate_ui.pillar_secrets as psmod

    with secrets_env.app.app_context():
        path = psmod._secrets_dir() / "netbird.sls"
        path.parent.mkdir(parents=True)
        path.write_text("# setup\nnetbird:\n  setup_key: NACL[abc]\n")
        data = psmod._load_rt(path)
        assert data["netbird"]["setup_key"] == "NACL[abc]"
        psmod._set_path(data, "netbird.extra", NACL)
        psmod._dump_rt(data, path)
        text = path.read_text()
        assert "# setup" in text  # comment preserved
        assert "NACL[abc]" in text
        assert "NACL[ZmFrZS1jaXBoZXJ0ZXh0]" in text


def test_del_prunes_empty_parents():
    from overstate_ui import pillar_secrets as psmod

    data = {"a": {"b": {"c": NACL}, "keep": 1}}
    assert psmod._del_path(data, "a.b.c")
    assert data == {"a": {"keep": 1}}


def test_dotted_key_validation():
    from overstate_ui import pillar_secrets as psmod
    from overstate_ui.pillar_nacl import NaclSetupError

    with pytest.raises(NaclSetupError):
        psmod._validate_dotted_key("")
    with pytest.raises(NaclSetupError):
        psmod._validate_dotted_key("a..b")
    with pytest.raises(NaclSetupError):
        psmod._validate_dotted_key("a b")
    psmod._validate_dotted_key("app.db.password")


# --- editor routes -----------------------------------------------------

def test_index_lists_collections_and_keys(secrets_env, monkeypatch):
    import overstate_ui.pillar_secrets as psmod

    monkeypatch.setattr(psmod, "key_id", lambda pk: "f1ce" if pk else None)
    _store_pk(secrets_env)
    _mk_collection(secrets_env, "netbird")
    with secrets_env.app.app_context():
        psmod._collection_path("netbird").write_text(
            "netbird:\n  setup_key: NACL[abc]\n"
        )
    html = secrets_env.get("/pillar/secrets/").data.decode()
    assert "netbird" in html
    assert "netbird.setup_key" in html
    assert "abc" in html  # ciphertext preview renders without a badge
    assert "badge-success\">encrypted" not in html
    # The save dialog is key-aware: static overwrite fallback stays for
    # no-JS, plus a hook the script uses to reword per typed key.
    assert "data-secret-save" in html
    assert "Overwrite '" in html


def test_index_searches_and_paginates_keys(secrets_env):
    """Key tables page independently and the toolbar filters by key."""
    import overstate_ui.pillar_secrets as psmod

    _store_pk(secrets_env)
    _mk_collection(secrets_env, "svc")
    keys = [f"svc.k{i:02d}" for i in range(30)] + ["svc.zebra"]
    with secrets_env.app.app_context():
        psmod._collection_path("svc").write_text(
            "\n".join(f"{k}: NACL[{k}]" for k in keys) + "\n"
        )

    first = secrets_env.get("/pillar/secrets/?per_page=10").data.decode()
    assert "Page 1 of 4" in first
    assert "1–10 of 31 keys" in first
    assert "cp_svc" in first  # per-collection page param in the links
    assert "svc.k00" in first and "svc.k09" in first
    assert "svc.k10" not in first

    last = secrets_env.get("/pillar/secrets/?per_page=10&cp_svc=4").data.decode()
    assert "Page 4 of 4" in last and "svc.zebra" in last

    filtered = secrets_env.get("/pillar/secrets/?q=zebra").data.decode()
    assert "svc.zebra" in filtered and "svc.k00" not in filtered
    assert "No keys match" in secrets_env.get("/pillar/secrets/?q=nope").data.decode()


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
        return NACL

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
    from ruamel.yaml import YAML

    parsed = YAML(typ="safe").load(
        (secrets_env.work / "pillar" / "secrets" / "netbird.sls").read_text()
    )
    assert parsed["netbird"]["setup_key"] == NACL
    text = (secrets_env.work / "pillar" / "secrets" / "netbird.sls").read_text()
    assert "S3cret!" not in text  # plaintext never hits disk
    proc = _git(secrets_env.work, "status", "--porcelain")
    assert proc.stdout.strip() == ""
    with secrets_env.app.app_context():
        assert (
            get_session()
            .query(AuditEvent)
            .filter_by(action="pillar-secret:add:netbird:netbird.setup_key")
            .count()
            == 1
        )


def test_update_requires_confirm_gate(secrets_env, monkeypatch):
    import overstate_ui.pillar_secrets as psmod

    def varying(v, pk):
        return f"NACL[{v}]"

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
        data={
            "collection": "netbird",
            "key": "netbird.setup_key",
            "value": "new",
            "confirm_box": "1",
        },
        follow_redirects=True,
    )
    assert "updated" in rv.data.decode().lower()


def test_add_refuses_existing_key_without_update(secrets_env, monkeypatch):
    import overstate_ui.pillar_secrets as psmod

    monkeypatch.setattr(psmod, "encrypt_value", lambda v, pk: NACL)
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

    monkeypatch.setattr(psmod, "encrypt_value", lambda v, pk: NACL)
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

    store = {}

    def fake_encrypt(value, pk):
        block = f"NACL[{value}]"
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
    text = (secrets_env.work / "pillar" / "secrets" / "netbird.sls").read_text()
    assert "_overstate_verify" not in text
    with secrets_env.app.app_context():
        assert (
            get_session()
            .query(AuditEvent)
            .filter_by(action="pillar-secret:verify:netbird")
            .count()
            == 1
        )


def test_verify_reports_mismatch_and_cleans_up(secrets_env, monkeypatch):
    import overstate_ui.pillar_secrets as psmod

    monkeypatch.setattr(psmod, "encrypt_value", lambda v, pk: NACL)
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
    assert "_overstate_verify" not in text


# --- RBAC --------------------------------------------------------------

def test_viewer_cannot_write(secrets_env, monkeypatch):
    import overstate_ui.pillar_secrets as psmod

    monkeypatch.setattr(psmod, "encrypt_value", lambda v, pk: NACL)
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
    assert secrets_env.get("/pillar/secrets/").status_code == 200


def test_scoped_operator_without_grant_forbidden(secrets_env, monkeypatch):
    import overstate_ui.pillar_secrets as psmod
    from overstate_ui.models import Setting

    monkeypatch.setattr(psmod, "encrypt_value", lambda v, pk: NACL)
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

    monkeypatch.setattr(psmod, "encrypt_value", lambda v, pk: NACL)
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
    assert "not a valid NaCl public key" in rv.data.decode()
    rv = secrets_env.post(
        "/settings/encryption/store", data={"pk": "GOOD-PUBKEY"}, follow_redirects=True
    )
    assert "stored" in rv.data.decode().lower()
    with secrets_env.app.app_context():
        assert get_setting("pillar_nacl_pk") == "GOOD-PUBKEY"
    # Replace without the checkbox gate is refused.
    rv = secrets_env.post(
        "/settings/encryption/store",
        data={"pk": "GOOD-PUBKEY-2"},
        follow_redirects=True,
    )
    assert "confirm to replace" in rv.data.decode()
    secrets_env.post(
        "/settings/encryption/store",
        data={"pk": "GOOD-PUBKEY-2", "confirm_box": "1"},
        follow_redirects=True,
    )
    with secrets_env.app.app_context():
        assert get_setting("pillar_nacl_pk") == "GOOD-PUBKEY-2"
    # Clear requires the gate too.
    secrets_env.post("/settings/encryption/clear", data={})
    with secrets_env.app.app_context():
        assert get_setting("pillar_nacl_pk") == "GOOD-PUBKEY-2"
    secrets_env.post(
        "/settings/encryption/clear", data={"confirm_box": "1"}, follow_redirects=True
    )
    with secrets_env.app.app_context():
        assert get_setting("pillar_nacl_pk") == ""


# --- setup copy / onboarding ------------------------------------------

def test_unconfigured_page_shows_empty_state_with_setup_button(secrets_env):
    """No stored key => a single empty state, not the old stacked cards."""
    html = secrets_env.get("/pillar/secrets/").data.decode()
    assert "Set up encrypted pillar" in html
    assert "Setup encryption" in html
    assert 'href="/pillar/secrets/setup"' in html
    # The runbook/code blocks moved to the wizard.
    assert "mockup-code" not in html
    assert "salt-call --local nacl.keygen" not in html


def test_configured_page_shows_editor_status(secrets_env, monkeypatch):
    import overstate_ui.pillar_secrets as psmod

    monkeypatch.setattr(psmod, "key_id", lambda pk: "f1ce" if pk else None)
    _store_pk(secrets_env)
    html = secrets_env.get("/pillar/secrets/").data.decode()
    assert "key f1ce" in html  # status strip keeps the key id, no badge
    assert "encryption configured" not in html
    assert "Setup encryption" not in html  # empty state gone
    assert 'href="/pillar/secrets/setup"' in html  # repair link


def test_setup_wizard_has_nacl_runbook(secrets_env):
    """The wizard carries the config check, keygen runbook, key step,
    and verify step in the minion-onboarding style."""
    html = secrets_env.get("/pillar/secrets/setup").data.decode()
    assert 'aria-label="Encryption setup steps"' in html
    assert "Step 1 · Master config" in html
    assert "Step 2 · Keys" in html
    assert "mockup-code" in html
    assert "salt-call --local nacl.keygen" in html
    assert "nacl.config" in html
    assert "Set up NaCl on the masters" in html
    assert "gpg" not in html.lower()
    assert "_overstate_verify" not in html


def test_setup_wizard_verify_step_needs_a_collection(secrets_env):
    html = secrets_env.get("/pillar/secrets/setup").data.decode()
    assert "Create a collection and add a secret first" in html


def test_key_settings_page_uses_nacl(secrets_env):
    html = secrets_env.get("/settings/encryption/").data.decode()
    assert "Generate the master keypair" in html
    assert "mockup-code" in html
    assert "salt-call --local nacl.keygen" in html
    assert "NACL" not in html.replace("nacl", "")  # no armored/uppercase leftovers
    assert "gpg" not in html.lower()
    assert "armor" not in html.lower()


# --- pillar capture honesty -------------------------------------------

def test_pillar_detail_banner_warns_plaintext(secrets_env, monkeypatch):
    import overstate_ui.pillar_secrets as psmod

    monkeypatch.setattr(psmod, "encrypt_value", lambda v, pk: NACL)
    _store_pk(secrets_env)
    _mk_collection(secrets_env, "netbird")
    secrets_env.post(
        "/pillar/secrets/secret/add",
        data={"collection": "netbird", "key": "netbird.setup_key", "value": "x"},
    )
    html = secrets_env.get("/pillar/web-01").data.decode()
    assert "Encrypted pillar values are included" in html
    assert "plaintext" in html
    # The banner warns without dumping internals: no storage backend,
    # no key list.
    assert "Postgres" not in html
    assert "Keys:" not in html


def test_pillar_detail_no_banner_without_secrets(secrets_env):
    html = secrets_env.get("/pillar/web-01").data.decode()
    assert "Encrypted pillar values are included" not in html


# --- Pillar section tabs ----------------------------------------------

def _pillar_tab_bar(html):
    bar = html.split('aria-label="Pillar views"', 1)[1].split("</div>", 1)[0]
    return bar.split('role="tab"')[1:]


def _aside(html):
    return html.split('id="nav-panel"', 1)[1].split("</aside>", 1)[0]


def test_sidebar_has_one_pillar_entry(secrets_env):
    for path in ("/pillar/", "/pillar/secrets/"):
        html = secrets_env.get(path).data.decode()
        aside = _aside(html)
        assert 'href="/pillar/"' in aside
        assert 'href="/pillar/secrets/"' not in aside
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
