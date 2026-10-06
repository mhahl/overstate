"""Key management. Tabs per status; every action fans out to all masters.

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
from .db import get_session
from .fleet import pod_clients
from .models import Minion
from .salt_client import SaltApiError, SaltClient

bp = Blueprint("keys", __name__, url_prefix="/keys")

TABS = [
    ("pending", "minions_pre"),
    ("accepted", "minions"),
    ("rejected", "minions_rejected"),
    ("denied", "minions_denied"),
]

ACTIONS = {"accept": "key.accept", "reject": "key.reject", "delete": "key.delete"}

PAST = {"accept": "accepted", "reject": "rejected", "delete": "deleted"}

# key.finger section names per roster tab (when the return is sectioned).
_FINGER_SECTIONS = {
    "accepted": "minions",
    "pending": "minions_pre",
    "rejected": "minions_rejected",
    "denied": "minions_denied",
}

_FP_RE = re.compile(r"^([0-9a-fA-F]{2}:)+[0-9a-fA-F]{2}$")


def _fingerprints(client, http_timeout: float | None = 8.0) -> dict:
    """Merge key.finger output into {minion_id: fingerprint}."""
    fp_re = _FP_RE
    try:
        # match is required: key.finger with no match crashes server-side
        # (UnboundLocalError on Salt 3008).
        result = client.wheel("key.finger", match="*", http_timeout=http_timeout)[0]
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
    http_timeout: float | None = 8.0,
) -> tuple[dict, list[str]]:
    """(union roster, unreachable pod names).

    Each row carries per-pod states; a minion appears under a tab when ANY
    pod reports it there. Unreachable pods contribute nothing (their
    absence is flashed by the caller). Reads carry a short HTTP backstop
    so a sick master degrades the page fast instead of pinning a worker.
    """
    states: dict[str, dict] = {}
    order: list[str] = []
    failed: list[str] = []
    for name, client in clients:
        try:
            listed = client.wheel("key.list_all", http_timeout=http_timeout)[0]["data"][
                "return"
            ]
            fingers = _fingerprints(client, http_timeout=http_timeout)
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


def _attribute_fingerprints(node, listed: dict[str, set]) -> dict[str, dict]:
    """{tab: {minion_id: fingerprint}}, failing closed on ambiguity.

    Section-keyed key.finger returns attribute exactly. A flat return
    attributes a fingerprint only to an id sitting in a single status on
    that pod, so a same-id collision across statuses can never read as
    agreement. Anything unresolvable stays absent.
    """
    out: dict[str, dict] = {tab: {} for tab, _ in TABS}
    if not isinstance(node, dict):
        return out
    sections = {tab: node.get(field) for tab, field in _FINGER_SECTIONS.items()}
    if any(isinstance(ids, dict) and ids for ids in sections.values()):
        for tab, ids in sections.items():
            if isinstance(ids, dict):
                for mid, fp in ids.items():
                    if isinstance(fp, str) and _FP_RE.match(fp):
                        out[tab][mid] = fp
        return out
    flat: dict = {}

    def walk(obj) -> None:
        if isinstance(obj, dict):
            for key, value in obj.items():
                if isinstance(value, str) and _FP_RE.match(value):
                    flat[key] = value
                else:
                    walk(value)
        elif isinstance(obj, list):
            for item in obj:
                walk(item)

    walk(node)
    counts: dict = {}
    for ids in listed.values():
        for mid in ids:
            counts[mid] = counts.get(mid, 0) + 1
    for tab, ids in listed.items():
        for mid in ids:
            if counts[mid] == 1 and mid in flat:
                out[tab][mid] = flat[mid]
    return out


def reconcile_keys(clients, execute: bool = True) -> dict:
    """Complete key trust across pods; never create it.

    For each minion pending on a reachable pod, accept it there iff
    another reachable pod reports it accepted with the same fingerprint.
    Globally-pending keys, fingerprint mismatches, and anything
    rejected/denied anywhere stay for a human. With execute=False the
    same plan is returned but no wheel accept fires (preview).

    Returns {"accepted": [(pod, id)], "skipped": [(id, reason)],
    "unreachable": [pod], "errors": [(pod, id)]}.
    """
    per_pod: dict[str, tuple] = {}
    unreachable: list[str] = []
    for name, cli in clients:
        try:
            listed_raw = cli.wheel("key.list_all", http_timeout=8.0)[0]["data"][
                "return"
            ]
            listed = {tab: set(listed_raw.get(field, []) or []) for tab, field in TABS}
        except (SaltApiError, KeyError, IndexError, TypeError):
            unreachable.append(name)
            continue
        try:
            node = cli.wheel("key.finger", match="*", http_timeout=8.0)[0]
            node = node.get("data", {}).get("return", node)
        except (SaltApiError, KeyError, IndexError, TypeError):
            node = None
        per_pod[name] = (listed, _attribute_fingerprints(node, listed))
    by_name = dict(clients)
    accepted: list[tuple[str, str]] = []
    skipped: list[tuple[str, str]] = []
    errors: list[tuple[str, str]] = []
    ids = sorted(
        {
            mid
            for listed, _ in per_pod.values()
            for ids in listed.values()
            for mid in ids
        }
    )
    for mid in ids:
        state = {
            name: next((tab for tab, ids in listed.items() if mid in ids), None)
            for name, (listed, _) in per_pod.items()
        }
        deciders = [
            (name, tab) for name, tab in state.items() if tab in ("rejected", "denied")
        ]
        if deciders:
            name, tab = deciders[0]
            skipped.append((mid, f"explicit {tab} on {name} — human decides"))
            continue
        pending_on = [name for name, tab in state.items() if tab == "pending"]
        if not pending_on:
            continue  # absent elsewhere or already accepted everywhere
        trusted = [
            (name, fingers["accepted"][mid])
            for name, (_, fingers) in per_pod.items()
            if state[name] == "accepted" and mid in fingers["accepted"]
        ]
        if not trusted:
            skipped.append((mid, "trusted nowhere — accept by hand"))
            continue
        for pod in pending_on:
            here = per_pod[pod][1]["pending"].get(mid)
            if here and any(here == fp for _, fp in trusted):
                if execute:
                    try:
                        by_name[pod].wheel("key.accept", match=mid)
                    except SaltApiError:
                        errors.append((pod, mid))
                        continue
                accepted.append((pod, mid))
            else:
                skipped.append((mid, f"{pod}: fingerprint mismatch or unavailable"))
    return {
        "accepted": accepted,
        "skipped": skipped,
        "unreachable": unreachable,
        "errors": errors,
    }


@bp.route("/")
@login_required
def index():
    from flask_login import current_user

    from .authz import has_fleet, minions_with, rbac_mode, require

    require("key.read")
    tab = request.args.get("tab", "pending")
    if tab not in dict(TABS):
        tab = "pending"
    from .tasks_queue import read_keys_cache, write_keys_cache

    clients = pod_clients(get_salt())
    # Merged roster + fingerprints, cached briefly: rebuilding it costs
    # two wheel calls per pod on every view. Mutations clear it below.
    cached = read_keys_cache()
    if (
        isinstance(cached, dict)
        and isinstance(cached.get("data"), dict)
        and isinstance(cached.get("failed"), list)
    ):
        data, failed = cached["data"], cached["failed"]
    else:
        data, failed = merged_key_data(clients)
        if len(failed) < len(clients):
            write_keys_cache(data, failed)
    if len(failed) == len(clients):
        flash("Salt API error: no master is reachable.", "error")
        data = {t: [] for t, _ in TABS}
    if rbac_mode() == "scoped" and not has_fleet(current_user, "key.read"):
        # Accepted ids in scope only; the pending, rejected, and denied
        # piles stay fleet key.read.
        allowed = minions_with(current_user, "key.read")
        data = {
            t: [r for r in rows if r["id"] in allowed] if t == "accepted" else []
            for t, rows in data.items()
        }
    for name in failed:
        flash(f"{name} is unreachable. Key states are partial.", "warning")
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
    # Scale-up drift signal: ids pending on one pod but accepted on
    # another are one Reconcile click from converging.
    by_id: dict[str, dict] = {}
    for tab_rows in data.values():
        for row in tab_rows:
            by_id.setdefault(row["id"], {}).update(row.get("states") or {})
    completable = sum(
        1
        for states in by_id.values()
        if {p for p, s in states.items() if s == "pending"}
        - {p for p, s in states.items() if s == "accepted"}
        and "accepted" in states.values()
    )
    return render_template(
        "keys.html",
        tab=tab,
        rows=rows,
        counts=counts,
        syndic_masters=masters,
        completable=completable,
        q=q,
        sort=sort,
        direction=direction,
    )


@bp.post("/reconcile")
@roles_required("operator")
def reconcile():
    from .authz import rbac_mode, require

    if rbac_mode() == "scoped":
        # Accept admits pending keys on every master: fleet-only, since
        # a pending key has no trustworthy grain.
        require("key.accept")
    clients = pod_clients(get_salt())
    if request.form.get("confirm") == "1":
        report = reconcile_keys(clients)
        if len(report["unreachable"]) == len(clients):
            flash("Salt API error: no master is reachable. Nothing changed.", "error")
        else:
            from .tasks_queue import clear_key_caches

            clear_key_caches()
            n = len(report["accepted"])
            log_event(current_user.username, f"reconcile-keys:{n}")
            detail = "; ".join(
                [f"{mid}: {reason}" for mid, reason in report["skipped"]]
                + [f"{pod}/{mid}: accept failed" for pod, mid in report["errors"]]
                + (
                    [f"unreachable: {', '.join(report['unreachable'])}"]
                    if report["unreachable"]
                    else []
                )
            )
            flash(
                f"Reconciled {n} key(s)." + (f" {detail}" if detail else ""),
                "success" if n and not detail else "warning" if n else "info",
            )
        return redirect(url_for("keys.index"))
    report = reconcile_keys(clients, execute=False)
    if len(report["unreachable"]) == len(clients):
        flash("Salt API error: no master is reachable. Nothing changed.", "error")
        return redirect(url_for("keys.index"))
    return render_template("keys_reconcile.html", report=report)


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
        flash("Select a minion first. An empty key selection does nothing.", "error")
        return redirect(url_for("keys.index", **keep))
    # match= is a Salt glob: a forged "*" would accept or delete every
    # key, so only exact ids from the roster may pass.
    if any(c in mid for c in "*?[]"):
        flash("You cannot use wildcards in key ids.", "error")
        return redirect(url_for("keys.index", **keep))
    from .authz import rbac_mode, require

    if rbac_mode() == "scoped":
        # Gate before the roster read: a denied caller publishes nothing.
        # Unknown ids still redirect below, but only for gate-passers.
        if action == "delete":
            require("key.delete", minion=mid)
        else:
            require("key.accept")
    clients = pod_clients(get_salt())
    roster, _ = merged_key_data(clients)
    current_ids = {row["id"] for rows in roster.values() for row in rows}
    if mid not in current_ids:
        # An empty union means no master answered: say so plainly.
        flash("That key is missing from the current list.", "error")
        return redirect(url_for("keys.index", **keep))
    failed = []
    for name, cli in clients:
        try:
            cli.wheel(ACTIONS[action], match=mid)
        except SaltApiError:
            failed.append(name)
    if len(failed) == len(clients):
        flash("Salt API error: no master is reachable. Nothing changed.", "error")
        return redirect(url_for("keys.index", **keep))
    from .tasks_queue import clear_key_caches

    clear_key_caches()
    past = PAST[action]
    # A delete must also clear the inventory snapshot: the minion list
    # unions the snapshot cache with the live roster, so a key delete
    # that leaves the snapshot behind never removes the minion. Job
    # history is kept. Accept/reject only change trust, so the snapshot
    # (grains, history) stays put.
    removed = False
    if action == "delete":
        session = get_session()
        row = session.get(Minion, mid)
        if row is not None:
            session.delete(row)
            session.commit()
            removed = True
            log_event(
                current_user.username,
                f"minion-remove:{mid}",
                permission="minion.remove",
                minion_id=mid,
            )
    suffix = " Inventory row removed." if removed else ""
    if failed:
        flash(
            f"{mid}: {past} on the reachable masters. "
            f"{', '.join(failed)} did not answer. Retry to converge.{suffix}",
            "warning",
        )
        log_event(
            current_user.username,
            f"{action}-key-partial:{mid}:{','.join(failed)}",
            permission="key.delete" if action == "delete" else "key.accept",
            minion_id=mid,
        )
    else:
        log_event(
            current_user.username,
            f"{action}-key",
            permission="key.delete" if action == "delete" else "key.accept",
            minion_id=mid,
        )
        flash(f"{mid}: {past}.{suffix}", "success")
    return redirect(url_for("keys.index", **keep))
