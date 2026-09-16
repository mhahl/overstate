"""Key management. Tabs per status; every action fans out to both masters.

A minion must be accepted on the pod it lands on, so accept/reject/delete
run on every reachable master pod (idempotent wheel calls). The roster
merges all pods with per-pod state chips; an unreachable pod degrades to
a warning, never a silent split.
"""

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
from .fleet import pod_clients
from .salt_client import SaltApiError, SaltClient

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


def merged_key_data(
    clients: list[tuple[str, SaltClient]],
) -> tuple[dict, list[str]]:
    """(union roster, unreachable pod names).

    Each row carries per-pod states; a minion appears under a tab when ANY
    pod reports it there. Unreachable pods contribute nothing (their
    absence is flashed by the caller).
    """
    states: dict[str, dict] = {}
    order: list[str] = []
    failed: list[str] = []
    for name, client in clients:
        try:
            listed = client.wheel("key.list_all")[0]["data"]["return"]
            fingers = _fingerprints(client)
        except (SaltApiError, KeyError, IndexError, TypeError):
            failed.append(name)
            continue
        for tab, field in TABS:
            for mid in listed.get(field, []) or []:
                entry = states.setdefault(
                    mid, {"id": mid, "fingerprint": "—", "states": {}}
                )
                if mid not in order:
                    order.append(mid)
                entry["states"][name] = tab
                if entry["fingerprint"] == "—":
                    entry["fingerprint"] = fingers.get(mid, "—")
    out: dict = {}
    for tab, _ in TABS:
        out[tab] = [
            states[mid]
            for mid in sorted(order)
            if tab in states[mid]["states"].values()
        ]
    return out, failed


@bp.route("/")
@login_required
def index():
    tab = request.args.get("tab", "pending")
    if tab not in dict(TABS):
        tab = "pending"
    clients = pod_clients(get_salt())
    data, failed = merged_key_data(clients)
    if len(failed) == len(clients):
        flash("salt-api error: no master reachable.", "error")
        data = {t: [] for t, _ in TABS}
    for name in failed:
        flash(f"{name} unreachable: key states may be partial.", "warning")
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
    # match= is a Salt glob: a forged "*" would accept or delete every
    # key, so only exact ids from the roster may pass.
    if any(c in mid for c in "*?[]"):
        flash("Key ids with wildcards are never accepted.", "error")
        return redirect(url_for("keys.index", **keep))
    clients = pod_clients(get_salt())
    roster, _ = merged_key_data(clients)
    current_ids = {row["id"] for rows in roster.values() for row in rows}
    if mid not in current_ids:
        # An empty union means no master answered: say so plainly.
        flash("Unknown key: it is not on the current list.", "error")
        return redirect(url_for("keys.index", **keep))
    failed = []
    for name, cli in clients:
        try:
            cli.wheel(ACTIONS[action], match=mid)
        except SaltApiError:
            failed.append(name)
    if len(failed) == len(clients):
        flash("salt-api error: no master reachable. Nothing changed.", "error")
    elif failed:
        flash(
            f"{mid}: {action}ed on the reachable masters; "
            f"{', '.join(failed)} unreachable — retry to converge.",
            "warning",
        )
        log_event(
            current_user.username,
            f"{action}-key-partial:{mid}:{','.join(failed)}"[:64],
        )
    else:
        log_event(current_user.username, f"{action}-key")
        flash(f"{mid}: {action}ed.", "success")
    return redirect(url_for("keys.index", **keep))
