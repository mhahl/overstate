"""DB-backed settings. Credentials never live here (env only)."""

import socket
from urllib.parse import urlsplit

from flask import Blueprint, current_app, flash, redirect, render_template, request, url_for
from flask_login import login_required
from .auth import roles_required

from .db import get_session
from .models import Setting

bp = Blueprint("settings", __name__, url_prefix="/settings")

DEFS = {
    "master_host": {
        "label": "Master hostname",
        "help": "Minion-facing Salt master hostname shown in the onboarding wizard. Empty resets to the detected default.",
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
        "options": ["light", "dark"],
        "default": "light",
    },
}


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
    if key == "master_host":
        return default_master_host()
    return DEFS.get(key, {}).get("default", "")


@bp.route("/")
@login_required
def index():
    values = {key: get_setting(key) for key in DEFS}
    return render_template("settings.html", defs=DEFS, values=values)


@bp.post("/")
@roles_required("admin")
def save():
    session = get_session()
    for key, meta in DEFS.items():
        value = request.form.get(key, "").strip()
        if not value:
            value = (default_master_host() if key == "master_host"
                     else meta["default"])
        if "options" in meta and value not in meta["options"]:
            value = meta["default"]
        row = session.get(Setting, key)
        if row is None:
            session.add(Setting(key=key, value=value))
        else:
            row.value = value
    session.commit()
    flash("Settings saved.")
    return redirect(url_for("settings.index"))
