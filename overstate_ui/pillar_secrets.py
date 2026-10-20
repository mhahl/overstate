"""Encrypted-pillar editor: per-file collections under ``pillar/secrets/``.

Salt's ``gpg`` renderer decrypts ``-----BEGIN PGP MESSAGE-----`` values
inside pillar files at render time on the master (renderer line set in
master.conf; see docs/guides/pillar-encryption.md). The app never holds
the private key: values are typed as plaintext, encrypted in-memory with
the master's *public* key (a Setting row), and only ciphertext lands in
the git checkout. Decryption and plaintext display stay master-side.

Three surfaces share this module:

* ``/pillar/secrets`` — the editor: collections, per-key add/update/
  delete (blind overwrite), top.sls wiring, and the onboarding wizard.
* ``/settings/encryption`` — the admin tab: store/replace the master
  public key, show its fingerprint and last-verified stamp.

Secrets are ciphertext in git by design: a check of ``git log -p`` must
never show a plaintext value, only PGP blocks.
"""

from __future__ import annotations

import re
import secrets as pysecrets
from pathlib import Path

from flask import (
    Blueprint,
    abort,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)
from flask_login import current_user, login_required
from ruamel.yaml import YAML

from .audit import log_event
from .auth import roles_required
from .git_sync import _toplevel, git_commit_file, is_checkout
from .pillar_gpg import GpgSetupError, encrypt_value, is_encrypted, key_id
from .settings import get_setting, set_setting

bp = Blueprint("pillar_secrets", __name__, url_prefix="/pillar/secrets")
enc_bp = Blueprint("pillar_encryption", __name__, url_prefix="/settings/encryption")

#: Setting keys (public key material + verify stamp; never plaintext).
PK_SETTING = "pillar_gpg_pk"
VERIFIED_SETTING = "pillar_secret_verified_at"

SECRETS_SUBDIR = "secrets"
KEY_RE = re.compile(r"^[A-Za-z0-9_-]+$")
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
RESERVED_SLUGS = frozenset({"top", "pillar"})
MAX_KEY_DEPTH = 8
MAX_VALUE_BYTES = 64 * 1024

_rt = YAML(typ="rt")
_rt.preserve_quotes = True
_rt.width = 4096
_rt.indent(mapping=2, sequence=4, offset=2)


# --- Pillar-tree access -------------------------------------------------

def _pillar_dir() -> Path:
    """Pillar tree at the checkout root (``_toplevel()/pillar``)."""
    top = _toplevel()
    if top is None:
        # Mirrors git_sync._pillar_dir fallback: srv beside file roots.
        from .git_sync import _srv

        return _srv() / "pillar"
    return top / "pillar"


def _secrets_dir() -> Path:
    return _pillar_dir() / SECRETS_SUBDIR


def _collection_path(name: str) -> Path:
    """Resolve a validated collection slug to its file. Name is a slug;
    the file lives under ``pillar/secrets/`` with a ``.sls`` suffix."""
    if not SLUG_RE.match(name) or name in RESERVED_SLUGS:
        abort(404)
    path = _secrets_dir() / f"{name}.sls"
    try:
        path = path.resolve()
    except OSError:
        abort(404)
    root = _secrets_dir().resolve()
    if root not in path.parents:
        abort(404)
    return path


def _collection_rel(name: str) -> str:
    """Checkout-root-relative path for git commits."""
    return f"pillar/{SECRETS_SUBDIR}/{name}.sls"


def list_collections() -> list[str]:
    """Collection slugs: existing ``.sls`` files under pillar/secrets/."""
    base = _secrets_dir()
    try:
        if not base.is_dir():
            return []
        names = [
            p.stem
            for p in sorted(base.iterdir())
            if p.is_file() and p.suffix == ".sls" and not p.name.startswith(".")
        ]
    except OSError:
        return []
    return [n for n in names if SLUG_RE.match(n) and n not in RESERVED_SLUGS]


# --- YAML helpers (comment-preserving round-trip) -----------------------

def _load_rt(path: Path):
    """Round-trip load; missing file yields an empty mapping."""
    try:
        if path.is_file():
            return _rt.load(path.read_text(encoding="utf-8")) or {}
    except (OSError, ValueError) as exc:
        raise GpgSetupError(f"could not read {path.name}") from exc
    return {}


def _dump_rt(data, path: Path) -> None:
    import io

    buf = io.StringIO()
    _rt.dump(data, buf)
    tmp = path.with_name(path.name + ".overstate-tmp")
    tmp.write_text(buf.getvalue(), encoding="utf-8")
    tmp.replace(path)


def _flatten(prefix: str, obj, out: dict) -> None:
    """Flatten nested pillar to dotted leaves (values stay as-is)."""
    if isinstance(obj, dict):
        for key, value in obj.items():
            _flatten(f"{prefix}{key}.", value, out)
    else:
        out[prefix.rstrip(".")] = obj


def _set_path(data: dict, dotted: str, value) -> None:
    from ruamel.yaml.scalarstring import LiteralScalarString

    parts = dotted.split(".")
    node = data
    for part in parts[:-1]:
        child = node.get(part)
        if child is None:
            child = {}
            node[part] = child
        elif not isinstance(child, dict):
            raise GpgSetupError(f"'{part}' is not a mapping in the collection")
        node = child
    # Multiline armored blocks dump as literal ``|`` scalars (readable,
    # matches the pillar-encryption guide) instead of quoted strings.
    leaf = LiteralScalarString(value) if isinstance(value, str) and "\n" in value else value
    node[parts[-1]] = leaf


def _del_path(data: dict, dotted: str) -> bool:
    parts = dotted.split(".")
    node = data
    stack: list[tuple[dict, str]] = []
    for part in parts[:-1]:
        child = node.get(part)
        if not isinstance(child, dict):
            return False
        stack.append((node, part))
        node = child
    if parts[-1] not in node:
        return False
    del node[parts[-1]]
    # Prune now-empty parents so deletes leave a tidy file.
    for parent, key in reversed(stack):
        child = parent.get(key)
        if isinstance(child, dict) and not child:
            del parent[key]
        else:
            break
    return True


def _validate_dotted_key(dotted: str) -> None:
    parts = (dotted or "").strip().split(".")
    if not parts or any(not p for p in parts):
        raise GpgSetupError("the key must be dotted (e.g. app.db.password)")
    if len(parts) > MAX_KEY_DEPTH:
        raise GpgSetupError(f"too many segments (max {MAX_KEY_DEPTH})")
    if any(len(p) > 64 or not KEY_RE.match(p) for p in parts):
        raise GpgSetupError("key segments: letters, digits, - and _ only")


# --- Top-file wiring ----------------------------------------------------

_WIRE_COMMENT = "# managed by Overstate (encrypted pillar)"


def top_sls_path() -> Path:
    return _pillar_dir() / "top.sls"


def collection_wired(name: str) -> bool:
    """Whether ``secrets.<name>`` appears in pillar/top.sls (advisory)."""
    try:
        text = top_sls_path().read_text(encoding="utf-8")
    except OSError:
        return False
    return f"secrets.{name}" in text


def wire_collection(name: str, user: str) -> dict:
    """Idempotently ensure ``base: {'*': [secrets.<name>]}`` in
    pillar/top.sls; creates the file when missing. One commit."""
    top = top_sls_path()
    created = not top.is_file()
    data = _load_rt(top)
    base = data.get("base")
    if base is None or not isinstance(base, dict):
        base = {}
        data["base"] = base
    star = base.get("*")
    if star is None or not isinstance(star, list):
        star = []
        base["*"] = star
    entry = f"secrets.{name}"
    if entry not in [str(x) for x in star]:
        star.append(entry)
    top.parent.mkdir(parents=True, exist_ok=True)
    text = _rt_text(data)
    if created:
        text = f"{_WIRE_COMMENT}\n{text}"
    top.write_text(text, encoding="utf-8")
    rel = "pillar/top.sls"
    result = git_commit_file(rel, f"overstate({user}): wire secrets.{name}", user, cwd=_toplevel())
    return {"ok": result["ok"], "reason": result.get("reason")}


def _rt_text(data) -> str:
    import io

    buf = io.StringIO()
    _rt.dump(data, buf)
    return buf.getvalue()


# --- Verification -------------------------------------------------------

def _read_pillar(minion: str) -> dict:
    from .dashboard import get_salt

    payload = get_salt().local(minion, "pillar.items", tgt_type="list")[0].get(minion)
    return payload if isinstance(payload, dict) else {}


def _refresh_pillar(minion: str) -> None:
    from .dashboard import get_salt

    get_salt().local(minion, "saltutil.refresh_pillar", tgt_type="list")


def verify_collection(name: str, minion: str, user: str) -> dict:
    """End-to-end proof: write a throwaway encrypted key into the wired
    collection, refresh pillar on the test minion, read it back, then
    remove the key. Returns per-step status; never leaves the key."""
    steps: list[dict] = []
    nonce = pysecrets.token_hex(8)
    path = _collection_path(name)
    try:
        armored = encrypt_value(nonce, get_setting(PK_SETTING))
    except GpgSetupError as exc:
        return {"ok": False, "steps": [{"step": "encrypt", "ok": False, "detail": str(exc)}]}
    steps.append({"step": "encrypt", "ok": True, "detail": None})
    # Write the throwaway key.
    data = _load_rt(path)
    _set_path(data, "_overstate_verify", armored)
    path.parent.mkdir(parents=True, exist_ok=True)
    _dump_rt(data, path)
    commit = git_commit_file(
        _collection_rel(name), f"overstate({user}): verify pillar encryption", user, cwd=_toplevel()
    )
    if not commit["ok"]:
        return {"ok": False, "steps": steps + [{"step": "write", "ok": False, "detail": commit.get("reason")}]}
    steps.append({"step": "write", "ok": True, "detail": commit.get("new")})
    try:
        _refresh_pillar(minion)
        steps.append({"step": "refresh", "ok": True, "detail": None})
    except Exception as exc:  # noqa: BLE001 — report, then clean up
        steps.append({"step": "refresh", "ok": False, "detail": str(exc)[:200]})
    read = _read_pillar(minion)
    matched = read.get("_overstate_verify") == nonce
    steps.append(
        {"step": "read", "ok": matched, "detail": "plaintext matches" if matched else "value did not decrypt to the nonce"}
    )
    # Remove the throwaway key (always).
    data = _load_rt(path)
    _del_path(data, "_overstate_verify")
    _dump_rt(data, path)
    git_commit_file(
        _collection_rel(name), f"overstate({user}): remove pillar verify key", user, cwd=_toplevel()
    )
    ok = all(s["ok"] for s in steps)
    if ok:
        import datetime as dt

        set_setting(
            VERIFIED_SETTING,
            dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        )
    log_event(user, f"pillar-secret:verify:{name}", minion_id=minion, detail="ok" if ok else "failed")
    return {"ok": ok, "steps": steps}


# --- Renderer probe -----------------------------------------------------

def renderer_status() -> dict | None:
    """Whether master.conf enables the gpg renderer + keydir. None when
    the ConfigMap cannot be read (app outside k8s, or not deployed)."""
    try:
        from .k8s import K8sClient

        cm = K8sClient().get_configmap(current_app.config["MASTER_CONFIGMAP"])
        conf = cm.get("data", {}).get("master.conf", "") or ""
    except Exception:  # noqa: BLE001 — probe is advisory
        return None
    return {
        "renderer": bool(re.search(r"(?m)^\s*renderer\s*:.*\bgpg\b", conf)),
        "keydir": bool(re.search(r"(?m)^\s*gpg_keydir\s*:", conf)),
    }


# --- Secrets editor routes ---------------------------------------------

def _may_write() -> bool:
    from .authz import rbac_mode, require

    if rbac_mode() == "scoped":
        require("pillar.secret.write")
    return True


@bp.route("/")
@login_required
def index():
    from .authz import authorize, rbac_mode, require

    if rbac_mode() == "scoped" and not authorize(
        current_user, "pillar.secret.write"
    ):
        # Writers may view the page they can edit; everyone else needs
        # the ordinary pillar read grant.
        require("pillar.read")
    pk = get_setting(PK_SETTING)
    collections = []
    for name in list_collections():
        path = _collection_path(name)
        data = _load_rt(path)
        leaves: dict[str, object] = {}
        _flatten("", data, leaves)
        keys = [
            {
                "key": dotted,
                "encrypted": is_encrypted(value),
                "preview": _preview(value) if is_encrypted(value) else "",
            }
            for dotted, value in sorted(leaves.items())
        ]
        collections.append(
            {
                "name": name,
                "wired": collection_wired(name),
                "entries": keys,
                "rel": _collection_rel(name),
            }
        )
    from .dashboard import get_salt
    from .minions import merged_cached_roster

    statuses, _up, _reachable = merged_cached_roster(get_salt())
    minions = sorted(m for m, st in statuses.items() if st == "accepted")
    renderer = renderer_status()
    return render_template(
        "pillar_secrets.html",
        collections=collections,
        minions=minions,
        renderer=renderer,
        key_id=key_id(pk) if pk else None,
        verified_at=get_setting(VERIFIED_SETTING),
        pk_stored=bool(pk),
        checkout=is_checkout(),
    )


def _preview(value: str) -> str:
    """Head/tail of an armored block for the table (never plaintext)."""
    lines = [ln for ln in value.splitlines() if ln.strip()]
    head = next((ln for ln in lines if ln.startswith("hQ") or len(ln) > 20), "")
    return head[:24] + "…" if head else "armored"


@bp.post("/collection/create")
@roles_required("operator")
def collection_create():
    _may_write()
    name = request.form.get("name", "").strip()
    if not SLUG_RE.match(name) or name in RESERVED_SLUGS:
        flash("Use a short lowercase name: a-z, 0-9, - and _", "error")
        return redirect(url_for("pillar_secrets.index"))
    path = _collection_path(name)
    if path.exists():
        flash(f"Collection '{name}' already exists.", "info")
        return redirect(url_for("pillar_secrets.index"))
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        _dump_rt({}, path)
        rel = _collection_rel(name)
        result = git_commit_file(
            rel, f"overstate({current_user.username}): create pillar secrets {name}",
            current_user.username, cwd=_toplevel(),
        )
    except (OSError, ValueError) as exc:
        flash(f"Could not create the collection ({exc}).", "error")
        return redirect(url_for("pillar_secrets.index"))
    if not result["ok"]:
        flash(f"Created on disk but not committed ({result['reason']}).", "warning")
    else:
        flash(f"Collection '{name}' created as {result['new']}.", "success")
        log_event(current_user.username, f"pillar-secret:create:{name}")
    return redirect(url_for("pillar_secrets.index"))


@bp.post("/secret/add")
@roles_required("operator")
def secret_add():
    _may_write()
    return _upsert_secret(blind=False)


@bp.post("/secret/update")
@roles_required("operator")
def secret_update():
    _may_write()
    if request.form.get("confirm_box") != "1":
        flash("Confirm that the old value will be overwritten (it cannot be shown).", "error")
        return redirect(url_for("pillar_secrets.index"))
    return _upsert_secret(blind=True)


def _upsert_secret(blind: bool) -> tuple:
    name = request.form.get("collection", "").strip()
    dotted = request.form.get("key", "").strip()
    value = request.form.get("value", "")
    try:
        _validate_dotted_key(dotted)
        if len(value.encode("utf-8")) > MAX_VALUE_BYTES:
            raise GpgSetupError(f"value too large (limit {MAX_VALUE_BYTES} bytes)")
        armored = encrypt_value(value, get_setting(PK_SETTING))
    except GpgSetupError as exc:
        flash(str(exc), "error")
        return redirect(url_for("pillar_secrets.index"))
    path = _collection_path(name)
    if not path.is_file():
        flash(f"Collection '{name}' does not exist.", "error")
        return redirect(url_for("pillar_secrets.index"))
    data = _load_rt(path)
    existing = _flatten_copy(data).get(dotted)
    if existing is not None and not blind:
        flash(f"'{dotted}' already exists — use Update to overwrite it blind.", "error")
        return redirect(url_for("pillar_secrets.index"))
    try:
        _set_path(data, dotted, armored)
        _dump_rt(data, path)
    except (OSError, ValueError, GpgSetupError) as exc:
        flash(str(exc), "error")
        return redirect(url_for("pillar_secrets.index"))
    result = git_commit_file(
        _collection_rel(name),
        f"overstate({current_user.username}): {'update' if existing is not None else 'add'} pillar secret {dotted}",
        current_user.username,
        cwd=_toplevel(),
    )
    if not result["ok"]:
        flash(f"Saved on disk but not committed ({result['reason']}).", "warning")
    else:
        flash(f"Secret '{dotted}' {'updated' if existing is not None else 'added'} ({result['new']}).", "success")
        log_event(current_user.username, f"pillar-secret:{'update' if existing is not None else 'add'}:{name}:{dotted}")
    return redirect(url_for("pillar_secrets.index"))


def _flatten_copy(data: dict) -> dict:
    out: dict = {}
    _flatten("", data, out)
    return out


@bp.post("/secret/delete")
@roles_required("operator")
def secret_delete():
    _may_write()
    name = request.form.get("collection", "").strip()
    dotted = request.form.get("key", "").strip()
    path = _collection_path(name)
    if not path.is_file():
        flash(f"Collection '{name}' does not exist.", "error")
        return redirect(url_for("pillar_secrets.index"))
    data = _load_rt(path)
    if not _del_path(data, dotted):
        flash(f"Key '{dotted}' not found in '{name}'.", "error")
        return redirect(url_for("pillar_secrets.index"))
    try:
        _dump_rt(data, path)
    except (OSError, ValueError) as exc:
        flash(f"Could not write the file ({exc}).", "error")
        return redirect(url_for("pillar_secrets.index"))
    result = git_commit_file(
        _collection_rel(name),
        f"overstate({current_user.username}): delete pillar secret {dotted}",
        current_user.username,
        cwd=_toplevel(),
    )
    if not result["ok"]:
        flash(f"Saved on disk but not committed ({result['reason']}).", "warning")
    else:
        flash(f"Secret '{dotted}' deleted ({result['new']}).", "success")
        log_event(current_user.username, f"pillar-secret:delete:{name}:{dotted}")
    return redirect(url_for("pillar_secrets.index"))


@bp.post("/top/wire")
@roles_required("operator")
def top_wire():
    _may_write()
    name = request.form.get("collection", "").strip()
    if name not in list_collections():
        flash(f"Collection '{name}' does not exist.", "error")
        return redirect(url_for("pillar_secrets.index"))
    result = wire_collection(name, current_user.username)
    if result["ok"]:
        flash(f"Wired secrets.{name} into pillar/top.sls.", "success")
        log_event(current_user.username, f"pillar-secret:wire:{name}")
    else:
        flash(f"Could not wire top.sls ({result['reason']}).", "error")
    return redirect(url_for("pillar_secrets.index"))


@bp.post("/verify")
@roles_required("operator")
def verify():
    _may_write()
    name = request.form.get("collection", "").strip()
    minion = request.form.get("minion", "").strip()
    if name not in list_collections():
        flash("Pick a collection first.", "error")
        return redirect(url_for("pillar_secrets.index"))
    if not minion or any(c in minion for c in "*?[]"):
        flash("Pick one exact test minion.", "error")
        return redirect(url_for("pillar_secrets.index"))
    if not get_setting(PK_SETTING):
        flash("Store the master public key first (Settings → Encrypted pillar).", "error")
        return redirect(url_for("pillar_secrets.index"))
    result = verify_collection(name, minion, current_user.username)
    if result["ok"]:
        flash("Encryption verified end to end: the master decrypted the test value.", "success")
    else:
        failed = [s for s in result["steps"] if not s["ok"]]
        detail = "; ".join(f"{s['step']}: {s['detail']}" for s in failed) or "unknown"
        flash(f"Verification failed — {detail}", "error")
    return redirect(url_for("pillar_secrets.index"))


# --- Encryption settings tab (admin) -----------------------------------

def _require_settings_admin() -> None:
    from .authz import rbac_mode, require

    if rbac_mode() == "scoped":
        require("settings.write")


@enc_bp.route("/")
@roles_required("admin")
def enc_index():
    _require_settings_admin()
    pk = get_setting(PK_SETTING)
    return render_template(
        "encryption_settings.html",
        pk_stored=bool(pk),
        key_id=key_id(pk) if pk else None,
        verified_at=get_setting(VERIFIED_SETTING),
        renderer=renderer_status(),
    )


@enc_bp.post("/store")
@roles_required("admin")
def store():
    _require_settings_admin()
    armored = request.form.get("pk", "").strip()
    if not armored:
        flash("Paste the master public key (armored block).", "error")
        return redirect(url_for("pillar_encryption.enc_index"))
    if key_id(armored) is None:
        flash("That does not look like a usable public key — nothing stored.", "error")
        return redirect(url_for("pillar_encryption.enc_index"))
    existing = get_setting(PK_SETTING)
    if existing and existing != armored and request.form.get("confirm_box") != "1":
        flash("A key is already stored — replacing it orphans existing values; confirm to replace.", "error")
        return redirect(url_for("pillar_encryption.enc_index"))
    set_setting(PK_SETTING, armored)
    log_event(current_user.username, "pillar-secret:pk-stored", detail=f"keyid:{key_id(armored)}")
    flash("Master public key stored.", "success")
    return redirect(url_for("pillar_encryption.enc_index"))


@enc_bp.post("/clear")
@roles_required("admin")
def clear():
    _require_settings_admin()
    if request.form.get("confirm_box") != "1":
        flash("Confirm to clear the stored public key.", "error")
        return redirect(url_for("pillar_encryption.enc_index"))
    set_setting(PK_SETTING, "")
    log_event(current_user.username, "pillar-secret:pk-cleared")
    flash("Stored public key cleared. Existing ciphertext stays in git until re-encrypted.", "info")
    return redirect(url_for("pillar_encryption.enc_index"))
