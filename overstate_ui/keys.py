"""Key management. Tabs per status; every action is a wheel call + audit row."""

import re

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
from .dashboard import get_salt
from .salt_client import SaltApiError

bp = Blueprint("keys", __name__, url_prefix="/keys")

TABS = [
    ("pending", "minions_pre"),
    ("accepted", "minions"),
    ("rejected", "minions_rejected"),
    ("denied", "minions_denied"),
]

ACTIONS = {"accept": "key.accept", "reject": "key.reject", "delete": "key.delete"}


def _fingerprints(client) -> dict:
    """Merge key.finger output into {minion_id: fingerprint}."""
    fp_re = re.compile(r"^([0-9a-fA-F]{2}:)+[0-9a-fA-F]{2}$")
    try:
        # match is required: key.finger with no match crashes server-side
        # (UnboundLocalError on Salt 3008).
        result = client.wheel("key.finger", match="*")[0]
    except (SaltApiError, KeyError, IndexError, TypeError):
        return {}
    node = result.get("data", {}).get("return", result)
    merged: dict = {}

    def walk(obj) -> None:
        if isinstance(obj, dict):
            for key, value in obj.items():
                if isinstance(value, str) and fp_re.match(value):
                    merged[key] = value
                else:
                    walk(value)
        elif isinstance(obj, list):
            for item in obj:
                walk(item)

    walk(node)
    return merged


def get_key_data(client) -> dict:
    listed = client.wheel("key.list_all")[0]["data"]["return"]
    fingers = _fingerprints(client)
    out = {}
    for tab, field in TABS:
        out[tab] = [
            {"id": mid, "fingerprint": fingers.get(mid, "—")}
            for mid in sorted(listed.get(field, []))
        ]
    return out


@bp.route("/")
@login_required
def index():
    tab = request.args.get("tab", "pending")
    if tab not in dict(TABS):
        tab = "pending"
    try:
        data = get_key_data(get_salt())
    except SaltApiError as exc:
        flash(f"salt-api error: {exc}", "error")
        data = {t: [] for t, _ in TABS}
    counts = {t: len(data[t]) for t, _ in TABS}
    masters = [
        m.strip()
        for m in current_app.config.get("SYNDIC_MASTERS", "").split(",")
        if m.strip()
    ]
    q = request.args.get("q", "").strip()
    ql = q.lower()
    rows = data[tab]
    if ql:
        rows = [
            r for r in rows if ql in r["id"].lower() or ql in r["fingerprint"].lower()
        ]
    sort = request.args.get("sort", "id")
    if sort not in ("id",):
        sort = "id"
    direction = request.args.get("dir", "asc")
    if direction not in ("asc", "desc"):
        direction = "asc"
    rows = sorted(rows, key=lambda r: r["id"], reverse=(direction == "desc"))
    return render_template(
        "keys.html",
        tab=tab,
        rows=rows,
        counts=counts,
        syndic_masters=masters,
        q=q,
        sort=sort,
        direction=direction,
    )


@bp.post("/<action>")
@roles_required("operator")
def act(action: str):
    if action not in ACTIONS:
        flash("Unknown key action.", "error")
        return redirect(url_for("keys.index"))
    mid = request.form.get("id", "").strip()
    tab = request.form.get("tab", "pending")
    keep = {"tab": tab}
    for f in ("q", "sort", "dir"):
        if request.form.get(f):
            keep[f] = request.form[f]
    if not mid:
        flash("Select a minion first: an empty key selection never fires.", "error")
        return redirect(url_for("keys.index", **keep))
    try:
        get_salt().wheel(ACTIONS[action], match=mid)
    except SaltApiError as exc:
        flash(f"salt-api error: {exc}", "error")
    else:
        log_event(current_user.username, f"{action}-key")
        flash(f"{mid}: {action}ed.", "success")
    nxt = request.form.get("next", "")
    if nxt.startswith("/") and not nxt.startswith("//"):
        return redirect(nxt)
    return redirect(url_for("keys.index", **keep))
