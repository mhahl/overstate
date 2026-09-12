"""Minion inventory: live list merged from Salt, snapshot cache, detail tabs."""

import csv
import io
import re

from flask import Blueprint, Response, flash, jsonify, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from .auth import roles_required

from .dashboard import get_salt
from .db import get_session
from .inventory import GRAIN_COLUMNS, refresh_inventory
from .models import JobReturn, Minion
from .salt_client import SaltApiError

bp = Blueprint("minions", __name__, url_prefix="/minions")

PAGE_SIZES = (10, 25, 50)
DETAIL_TABS = ["overview", "states", "jobs", "schedule", "pillar", "beacons"]
SORT_COLUMNS = ("id", "key", "presence", "os")
PRESENCE_ORDER = {"up": 0, "dead": 1, "down": 2}
ONBOARD_DISTROS = ("opensuse", "fedora")
ONBOARD_INSTALL = {
    "opensuse": "sudo zypper -n install salt-minion",
    "fedora": "sudo dnf -y install salt-minion",
}
HOST_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9.-]{0,253}[A-Za-z0-9])?$")


def presence_of(row: dict) -> str:
    if row["up"]:
        return "up"
    if row["dead"]:
        return "dead"
    return "down"


def live_roster(client) -> tuple[dict[str, str], set[str]]:
    """Return ({minion_id: key_status}, {up minion ids}); offline-safe."""
    statuses: dict[str, str] = {}
    up: set[str] = set()
    try:
        listed = client.wheel("key.list_all")[0]["data"]["return"]
        for mid in listed.get("minions", []):
            statuses[mid] = "accepted"
        for mid in listed.get("minions_pre", []):
            statuses[mid] = "pending"
        for mid in listed.get("minions_rejected", []):
            statuses[mid] = "rejected"
        for mid in listed.get("minions_denied", []):
            statuses[mid] = "denied"
        up = set(client.runner("manage.status")[0].get("up", []))
    except (SaltApiError, KeyError, IndexError, TypeError):
        pass
    return statuses, up


def normalize_grains(grains) -> dict:
    """Coerce snapshot/live grains for display. Proxy minions and older
    releases omit keys or report scalars (e.g. a single ipv4 string)."""
    if not isinstance(grains, dict):
        return {}
    grains = dict(grains)
    ipv4 = grains.get("ipv4")
    if isinstance(ipv4, str):
        grains["ipv4"] = [ipv4]
    elif not isinstance(ipv4, list):
        grains["ipv4"] = []
    return grains


def minion_rows(statuses: dict, up: set, q: str, status_filter: str,
                 sort: str = "id", direction: str = "asc") -> list[dict]:
    session = get_session()
    by_id: dict[str, dict] = {}
    for row in session.query(Minion).order_by(Minion.id).all():
        by_id[row.id] = {
            "id": row.id,
            "key_status": row.key_status,
            "up": False,
            "dead": False,
            "last_seen": row.last_seen,
            "grains": normalize_grains(row.grains),
        }
    for mid, st in statuses.items():
        by_id.setdefault(mid, {"id": mid, "key_status": st, "up": False,
                               "dead": False, "last_seen": None, "grains": {}})
        by_id[mid]["key_status"] = st
    rows = []
    for row in by_id.values():
        row["up"] = row["id"] in up
        row["dead"] = row["key_status"] == "accepted" and not row["up"] and bool(up)
        if q and q not in row["id"]:
            continue
        if status_filter and row["key_status"] != status_filter:
            continue
        rows.append(row)
    if sort == "key":
        key = lambda r: (r["key_status"], r["id"])  # noqa: E731
    elif sort == "presence":
        key = lambda r: (PRESENCE_ORDER[presence_of(r)], r["id"])  # noqa: E731
    elif sort == "os":
        key = lambda r: (r["grains"].get("osfinger", ""), r["id"])  # noqa: E731
    else:
        key = lambda r: r["id"]  # noqa: E731
    rows.sort(key=key, reverse=(direction == "desc"))
    return rows


@bp.route("/")
@login_required
def index():
    q = request.args.get("q", "").strip()
    status_filter = request.args.get("status", "")
    try:
        page = max(1, int(request.args.get("page", 1)))
    except ValueError:
        page = 1
    from .settings import get_setting

    try:
        default_page_size = int(get_setting("page_size"))
    except ValueError:
        default_page_size = 25
    if default_page_size not in PAGE_SIZES:
        default_page_size = 25
    try:
        per_page = int(request.args.get("per_page", default_page_size))
    except ValueError:
        per_page = default_page_size
    if per_page not in PAGE_SIZES:
        per_page = default_page_size
    sort = request.args.get("sort", "id")
    if sort not in SORT_COLUMNS:
        sort = "id"
    direction = request.args.get("dir", "asc")
    if direction not in ("asc", "desc"):
        direction = "asc"
    statuses, up = live_roster(get_salt())
    rows = minion_rows(statuses, up, q, status_filter, sort, direction)
    total = len(rows)
    pages = max(1, (total + per_page - 1) // per_page)
    page = min(page, pages)
    ctx = dict(rows=rows[(page - 1) * per_page: page * per_page],
               q=q, status=status_filter, page=page, pages=pages,
               per_page=per_page, total=total, grains=GRAIN_COLUMNS,
               reachable=bool(statuses or up), sort=sort, direction=direction)
    if request.headers.get("HX-Request") == "true":
        return render_template("_minion_rows.html", **ctx)
    return render_template("minions.html", **ctx)


@bp.route("/search")
@login_required
def search():
    """Minion id lookup for the command palette. Snapshot cache only."""
    q = request.args.get("q", "").strip()
    query = get_session().query(Minion.id)
    if q:
        query = query.filter(Minion.id.contains(q))
    return jsonify([row[0] for row in
                    query.order_by(Minion.id).limit(20).all()])


@bp.route("/presence")
@login_required
def presence():
    """Lightweight presence map for in-place dot updates. Never re-renders
    the table, so bulk checkbox selections survive polling."""
    statuses, up = live_roster(get_salt())
    rows = minion_rows(statuses, up, "", "")
    return jsonify({r["id"]: presence_of(r) for r in rows})


@bp.route("/export.csv")
@login_required
def export_csv():
    statuses, up = live_roster(get_salt())
    rows = minion_rows(statuses, up, "", "")
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["id", "key_status", "presence", "osfinger", "osrelease",
                     "fqdn", "ipv4", "cpuarch", "num_cpus", "saltversion",
                     "last_seen"])
    for r in rows:
        g = r["grains"]
        writer.writerow([
            r["id"], r["key_status"],
            "up" if r["up"] else ("accepted-but-dead" if r["dead"] else "down"),
            g.get("osfinger", ""), g.get("osrelease", ""), g.get("fqdn", ""),
            ";".join(g.get("ipv4", [])), g.get("cpuarch", ""),
            g.get("num_cpus", ""), g.get("saltversion", ""),
            r["last_seen"].isoformat() if r["last_seen"] else "",
        ])
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition":
                             "attachment; filename=minions.csv"})


@bp.post("/refresh")
@roles_required("operator")
def refresh():
    from .tasks import queue_or_none, refresh_inventory_task, wait_for

    job = queue_or_none(refresh_inventory_task)
    if job is None:
        refresh_sync()
    else:
        status, value = wait_for(job, wait=10.0)
        if status == "ready":
            flash(f"Inventory refreshed: {value['count']} minions.")
        elif status == "pending":
            flash("Refresh queued in the background — reload to see it.")
        else:
            flash(f"refresh failed in the background: {value}")
    return redirect(url_for("minions.index"))


def refresh_sync() -> None:
    """Synchronous refresh. Worker fallback and no-Redis path."""
    client = get_salt()
    try:
        statuses, _ = live_roster(client)
        count = refresh_inventory(client, statuses)
    except SaltApiError as exc:
        flash(f"salt-api error: {exc}")
    else:
        flash(f"Inventory refreshed: {count} minions.")




def build_onboard_script(distro: str, master: str, mid: str) -> str:
    """Render a join script for a new minion. Inputs must already match
    HOST_RE / ONBOARD_DISTROS; values are shell-safe by construction."""
    install = ONBOARD_INSTALL[distro]
    return (
        "#!/bin/sh\n"
        f"# Generated by Overstate: joins this machine to '{master}' as '{mid}'.\n"
        "# Review, then run as root on the new minion.\n"
        "set -eu\n"
        f"{install}\n"
        f"printf 'master: {master}\\nid: {mid}\\n'"
        " | sudo tee /etc/salt/minion > /dev/null\n"
        "sudo systemctl enable --now salt-minion\n"
        f"echo \"Minion '{mid}' started. Accept its key in Overstate > Keys, then run test.ping.\"\n"
    )


def onboard_inputs(args) -> tuple[str, str, str] | None:
    """Validated (distro, master, mid) from the wizard form, or None when
    the form was not submitted or failed validation (caller flashes)."""
    if "mid" not in args and "master" not in args:
        return None
    from .settings import get_setting

    mid = args.get("mid", "").strip()
    distro = args.get("distro", "opensuse")
    master = args.get("master", get_setting("master_host")).strip()
    if (not mid or distro not in ONBOARD_DISTROS or not HOST_RE.match(mid)
            or not master or not HOST_RE.match(master)):
        flash("Minion id and master must be valid hostnames; pick a distribution.")
        return None
    return (distro, master, mid)


@bp.route("/onboard")
@login_required
def onboard():
    """Guided enrollment for a new minion: describe it in the form to
    get a join script, then accept the key and verify. Acceptance
    itself stays on Keys."""
    from .settings import get_setting

    statuses, _ = live_roster(get_salt())
    pending = sorted(m for m, st in statuses.items() if st == "pending")
    master_host = get_setting("master_host")
    inputs = onboard_inputs(request.args)
    script = build_onboard_script(*inputs) if inputs else None
    return render_template("onboard.html", master_host=master_host,
                           pending=pending, reachable=bool(statuses),
                           script=script)


@bp.route("/onboard/script")
@login_required
def onboard_script():
    """Download the generated join script. Same validation as the form."""
    inputs = onboard_inputs(request.args)
    if inputs is None:
        return redirect(url_for("minions.onboard"))
    distro, master, mid = inputs
    return Response(build_onboard_script(distro, master, mid),
                    mimetype="text/x-shellscript",
                    headers={"Content-Disposition":
                             f"attachment; filename=onboard-{mid}.sh"})


@bp.route("/<mid>")
@login_required
def detail(mid: str):
    tab = request.args.get("tab", "overview")
    if tab not in DETAIL_TABS:
        tab = "overview"
    client = get_salt()
    row = get_session().get(Minion, mid)
    data: dict = {"grains": normalize_grains(row.grains if row else {})}
    error = None
    try:
        if tab == "overview":
            live = client.local(mid, "grains.items")[0].get(mid)
            if isinstance(live, dict):
                data["grains"] = normalize_grains(live)
        elif tab == "states":
            data["highstate"] = client.local(mid, "state.show_highstate")[0].get(mid)
        elif tab == "schedule":
            data["schedule"] = client.local(mid, "schedule.list")[0].get(mid)
        elif tab == "pillar":
            data["pillar"] = client.local(mid, "pillar.items")[0].get(mid)
        elif tab == "beacons":
            data["beacons"] = client.local(mid, "beacons.list")[0].get(mid)
    except SaltApiError as exc:
        error = str(exc)
    sort = request.args.get("sort", "jid")
    if sort not in ("jid", "status"):
        sort = "jid"
    direction = request.args.get("dir", "desc")
    if direction not in ("asc", "desc"):
        direction = "desc"
    if tab == "jobs":
        returns = (
            get_session().query(JobReturn)
            .filter_by(minion_id=mid)
            .order_by(JobReturn.jid.desc())
            .limit(20)
            .all()
        )

        def ret_key(r):
            if sort == "status":
                return (0 if r.success else 1, r.jid)
            return r.jid

        data["returns"] = sorted(returns, key=ret_key,
                                 reverse=(direction == "desc"))
    ctx = dict(mid=mid, tab=tab, tabs=DETAIL_TABS, data=data, error=error,
               snapshot=bool(row), sort=sort, direction=direction)
    if tab == "jobs" and request.headers.get("HX-Request") == "true":
        return render_template("_minion_returns.html", **ctx)
    return render_template("minion_detail.html", **ctx)
