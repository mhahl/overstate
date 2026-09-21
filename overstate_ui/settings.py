"""DB-backed settings. Only the OIDC client secret may live here (declared exception); all other credentials stay env-only."""

import secrets
import socket
from urllib.parse import urlsplit

from flask import (
    Blueprint,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)
from flask_login import current_user, login_required

from .audit import log_event
from .auth import roles_required
from .db import get_session
from .models import Setting

bp = Blueprint("settings", __name__, url_prefix="/settings")

OIDC_ENV_FALLBACK = {
    "oidc_issuer": "OIDC_ISSUER",
    "oidc_client_id": "OIDC_CLIENT_ID",
    "oidc_client_secret": "OIDC_CLIENT_SECRET",
    "oidc_groups_claim": "OIDC_GROUPS_CLAIM",
    "oidc_admin_groups": "OIDC_ADMIN_GROUPS",
    "oidc_operator_groups": "OIDC_OPERATOR_GROUPS",
}

DEFS = {
    "master_host": {
        "label": "Master hostname",
        "help": "Minion-facing Salt master hostname shown in the onboarding wizard. Empty resets to the detected default.",
        "default": "",
    },
    "oidc_issuer": {
        "label": "OIDC issuer",
        "help": "Single sign-on provider URL. Empty (and no env) disables SSO.",
        "default": "",
    },
    "oidc_client_id": {
        "label": "OIDC client ID",
        "help": "Client ID registered at the provider.",
        "default": "",
    },
    "oidc_client_secret": {
        "label": "OIDC client secret",
        "help": "Empty defers to OIDC_CLIENT_SECRET.",
        "default": "",
    },
    "oidc_groups_claim": {
        "label": "OIDC groups claim",
        "help": "Claim carrying group names for role mapping. Empty uses the default.",
        "default": "",
    },
    "oidc_admin_groups": {
        "label": "OIDC admin groups",
        "help": "Comma-separated provider groups mapped to the admin role.",
        "default": "",
    },
    "oidc_operator_groups": {
        "label": "OIDC operator groups",
        "help": "Comma-separated provider groups mapped to the operator role.",
        "default": "",
    },
    "default_target": {
        "label": "Default target",
        "help": "Prefilled in the run form when no preset or bulk selection sets one.",
        "default": "*",
    },
    "page_size": {
        "label": "Page size",
        "help": "Rows per page in the minion list.",
        "options": ["10", "25", "50"],
        "default": "25",
    },
    "theme": {
        "label": "Theme",
        "help": "Interface color scheme.",
        "options": ["light", "dark", "wireframe"],
        "default": "wireframe",
    },
}


SECTIONS = [
    {
        "title": "Server",
        "desc": "How minions and browsers reach this installation.",
        "keys": ["master_host"],
    },
    {
        "title": "Single sign-on (OIDC)",
        "desc": "Provider connection and role mapping. Values set here override the environment; clearing a field defers back to it. The client secret may be stored here or via OIDC_CLIENT_SECRET.",
        "keys": [
            "oidc_issuer",
            "oidc_client_id",
            "oidc_client_secret",
            "oidc_groups_claim",
            "oidc_admin_groups",
            "oidc_operator_groups",
        ],
    },
    {
        "title": "Run defaults",
        "desc": "Prefills for the command forms.",
        "keys": ["default_target"],
    },
    {
        "title": "Display",
        "desc": "List density and color scheme.",
        "keys": ["page_size", "theme"],
    },
]


def default_master_host() -> str:
    """Detected default for the minion-facing master hostname: this
    host's FQDN, falling back to the salt-api URL host (this app often
    runs next to the master, but an admin should verify in Settings)."""
    try:
        fqdn = socket.getfqdn()
    except OSError:
        fqdn = ""
    if fqdn and fqdn not in ("localhost",):
        return fqdn
    try:
        return urlsplit(current_app.config["SALT_API_URL"]).hostname or ""
    except (ValueError, KeyError):
        return ""


def get_setting(key: str) -> str:
    row = get_session().get(Setting, key)
    if row is not None:
        return row.value
    if key in OIDC_ENV_FALLBACK:
        return current_app.config.get(OIDC_ENV_FALLBACK[key], "") or ""
    if key == "master_host":
        return default_master_host()
    return DEFS.get(key, {}).get("default", "")


def _settings_context():
    is_admin = current_user.role == "admin"
    values = {key: get_setting(key) for key in DEFS}
    # The stored secret is never echoed: the form posts it back only
    # when the admin types a new one, and viewers see no OIDC values.
    values["oidc_client_secret"] = ""
    if not is_admin:
        for key in OIDC_ENV_FALLBACK:
            values[key] = ""
        sections = [
            {**s, "fields": [(k, DEFS[k]) for k in s["keys"]]}
            for s in SECTIONS
            if s["title"] != "Single sign-on (OIDC)"
        ]
    else:
        sections = [
            {**s, "fields": [(k, DEFS[k]) for k in s["keys"]]} for s in SECTIONS
        ]
    return sections, values, is_admin


@bp.route("/")
@login_required
def index():
    sections, values, is_admin = _settings_context()
    return render_template(
        "settings.html",
        sections=sections,
        values=values,
        is_admin=is_admin,
        rotation_user=current_app.config["SALT_EAUTH_USER"],
        rotation_password=None,
    )


@bp.post("/rotation/generate")
@roles_required("admin")
def rotation_generate():
    """Mint a replacement salt-api password and show it exactly once.
    Nothing is stored anywhere — the audit row records the act, never
    the secret, and the admin pastes it into the Secret by hand."""
    password = secrets.token_urlsafe(24)
    log_event(current_user.username, "rotation-password-generated")
    sections, values, is_admin = _settings_context()
    return render_template(
        "settings.html",
        sections=sections,
        values=values,
        is_admin=is_admin,
        rotation_user=current_app.config["SALT_EAUTH_USER"],
        rotation_password=password,
    )


@bp.post("/rotation/verify")
@roles_required("admin")
def rotation_verify():
    """Try a candidate password against salt-api with an ephemeral
    client. Proves a hand-applied rotation took; stores nothing."""
    import httpx

    from .salt_client import SaltApiError, SaltClient

    candidate = request.form.get("password", "")
    user = current_app.config["SALT_EAUTH_USER"]
    if not candidate:
        flash("Paste the candidate password first — nothing was tried.", "error")
        return redirect(url_for("settings.index"))
    client = SaltClient(
        current_app.config["SALT_API_URL"],
        user,
        candidate,
        current_app.config["SALT_EAUTH_TYPE"],
        verify=current_app.config["SALT_API_VERIFY"],
    )
    try:
        client.login(http_timeout=15.0)
    except (SaltApiError, httpx.HTTPError, KeyError) as exc:
        flash(f"salt-api refused the candidate ({exc}). Nothing changed.", "error")
        log_event(current_user.username, "rotation-verify:failed")
    else:
        flash(f"salt-api accepted a login as {user}.", "success")
        log_event(current_user.username, "rotation-verify:ok")
    return redirect(url_for("settings.index"))


@bp.post("/")
@roles_required("admin")
def save():
    session = get_session()
    for key, meta in DEFS.items():
        value = request.form.get(key, "").strip()
        if key == "oidc_client_secret":
            if request.form.get("clear_oidc_client_secret") == "on":
                # Explicit clear: drop the override so the env applies.
                row = session.get(Setting, key)
                if row is not None:
                    session.delete(row)
                continue
            if not value:
                # Empty means keep the stored secret, not wipe it.
                continue
        elif not value:
            if key in OIDC_ENV_FALLBACK:
                # Clearing defers to the environment: drop any override.
                row = session.get(Setting, key)
                if row is not None:
                    session.delete(row)
                continue
            value = default_master_host() if key == "master_host" else meta["default"]
        if "options" in meta and value not in meta["options"]:
            value = meta["default"]
        row = session.get(Setting, key)
        if row is None:
            session.add(Setting(key=key, value=value))
        else:
            row.value = value
    session.commit()
    flash("Settings saved.", "success")
    return redirect(url_for("settings.index"))
