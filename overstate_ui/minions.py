"""Minion inventory: live list merged from Salt, snapshot cache, detail tabs.

Routes only: helpers live in :mod:`overstate_ui.minions_helpers` and
are re-exported here so existing ``overstate_ui.minions.*`` import
paths keep working.
"""

import csv
import io

from flask import (
    Blueprint,
    Response,
    flash,
    jsonify,
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
from .inventory import GRAIN_COLUMNS
from .minions_helpers import (
    HOST_RE,
    ONBOARD_DISTROS,
    ONBOARD_INSTALL,
    OS_ICONS,
    PRESENCE_ORDER,
    _beacon_refusal,
    build_onboard_script,
    live_roster,
    minion_rows,
    normalize_grains,
    onboard_inputs,
    os_icon_slug,
    parse_beacon_list,
    presence_of,
    refresh_sync,
)
from .models import Job, JobReturn, Minion
from .salt_client import SaltApiError

bp = Blueprint("minions", __name__, url_prefix="/minions")

PAGE_SIZES = (10, 25, 50)
DETAIL_TABS = ["overview", "states", "jobs", "schedule", "pillar", "beacons", "mine"]
BEACON_ACTIONS = {
    "enable": "beacons.enable_beacon",
    "disable": "beacons.disable_beacon",
}
SORT_COLUMNS = ("id", "key", "presence", "os")

__all__ = [
    "BEACON_ACTIONS",
    "DETAIL_TABS",
    "HOST_RE",
    "ONBOARD_DISTROS",
    "ONBOARD_INSTALL",
    "OS_ICONS",
    "PAGE_SIZES",
    "PRESENCE_ORDER",
    "SORT_COLUMNS",
    "_beacon_refusal",
    "bp",
    "build_onboard_script",
    "live_roster",
    "minion_rows",
    "normalize_grains",
    "onboard_inputs",
    "os_icon_slug",
    "parse_beacon_list",
    "presence_of",
    "refresh_sync",
]


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
    statuses, up, reachable = live_roster(get_salt())
    rows = minion_rows(statuses, up, q, status_filter, sort, direction)
    total = len(rows)
    pages = max(1, (total + per_page - 1) // per_page)
    page = min(page, pages)
    ctx = {
        "rows": rows[(page - 1) * per_page : page * per_page],
        "q": q,
        "status": status_filter,
        "page": page,
        "pages": pages,
        "per_page": per_page,
        "total": total,
        "grains": GRAIN_COLUMNS,
        "reachable": reachable,
        "sort": sort,
        "direction": direction,
    }
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
    return jsonify([row[0] for row in query.order_by(Minion.id).limit(20).all()])


@bp.route("/presence")
@login_required
def presence():
    """Lightweight presence map for in-place dot updates. Never re-renders
    the table, so bulk checkbox selections survive polling."""
    statuses, up, _ = live_roster(get_salt())
    rows = minion_rows(statuses, up, "", "")
    return jsonify({r["id"]: presence_of(r) for r in rows})


@bp.route("/export.csv")
@login_required
def export_csv():
    statuses, up, _ = live_roster(get_salt())
    rows = minion_rows(statuses, up, "", "")
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(
        [
            "id",
            "key_status",
            "presence",
            "osfinger",
            "osrelease",
            "fqdn",
            "ipv4",
            "cpuarch",
            "num_cpus",
            "saltversion",
            "last_seen",
        ]
    )
    for r in rows:
        g = r["grains"]
        writer.writerow(
            [
                r["id"],
                r["key_status"],
                "up" if r["up"] else ("accepted-but-dead" if r["dead"] else "down"),
                g.get("osfinger", ""),
                g.get("osrelease", ""),
                g.get("fqdn", ""),
                ";".join(g.get("ipv4", [])),
                g.get("cpuarch", ""),
                g.get("num_cpus", ""),
                g.get("saltversion", ""),
                r["last_seen"].isoformat() if r["last_seen"] else "",
            ]
        )
    return Response(
        buf.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=minions.csv"},
    )


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
            flash(f"Inventory refreshed: {value['count']} minions.", "success")
        elif status == "pending":
            flash("Refresh queued in the background — reload to see it.", "info")
        else:
            flash(f"refresh failed in the background: {value}", "error")
    return redirect(url_for("minions.index"))


@bp.route("/onboard")
@login_required
def onboard():
    """Guided enrollment for a new minion: describe it in the form to
    get a join script, then accept the key and verify. Acceptance
    itself stays on Keys."""
    from .settings import get_setting

    statuses, _, reachable = live_roster(get_salt())
    pending = sorted(m for m, st in statuses.items() if st == "pending")
    master_host = get_setting("master_host")
    inputs = onboard_inputs(request.args)
    script = build_onboard_script(*inputs) if inputs else None
    return render_template(
        "onboard.html",
        master_host=master_host,
        pending=pending,
        reachable=reachable,
        script=script,
    )


@bp.route("/onboard/script")
@login_required
def onboard_script():
    """Download the generated join script. Same validation as the form."""
    inputs = onboard_inputs(request.args)
    if inputs is None:
        return redirect(url_for("minions.onboard"))
    distro, master, mid = inputs
    return Response(
        build_onboard_script(distro, master, mid),
        mimetype="text/x-shellscript",
        headers={"Content-Disposition": f"attachment; filename=onboard-{mid}.sh"},
    )


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
            # return_yaml=False keeps this a real mapping: an empty
            # schedule arrives as {} so the empty state triggers,
            # not as the string "schedule: {}\n" (see schedules.index).
            data["schedule"] = client.local(
                mid, "schedule.list", kwarg={"return_yaml": False}
            )[0].get(mid, {})
        elif tab == "pillar":
            data["pillar"] = client.local(mid, "pillar.items")[0].get(mid)
        elif tab == "mine":
            # This minion's stored mine values for one function:
            # mine.get run on the minion answers {mid: value}.
            # Missing/None means nothing stored (empty state).
            mine_fun = request.args.get("mine_fun", "").strip()
            data["mine_fun"] = mine_fun
            data["mine_found"] = False
            data["mine_value"] = None
            if mine_fun:
                stored = client.local(mid, "mine.get", arg=[mid, mine_fun])[0].get(
                    mid, {}
                )
                value = stored.get(mid) if isinstance(stored, dict) else stored
                if value is not None:
                    data["mine_found"] = True
                    data["mine_value"] = value
        elif tab == "beacons":
            # return_yaml=False keeps this a real mapping (see
            # schedules.index). A second pillar-excluded call
            # attributes the `pillar` source badge; when it fails
            # no badge is shown rather than a wrong one.
            value = client.local(mid, "beacons.list", kwarg={"return_yaml": False})[
                0
            ].get(mid, {})
            entries = parse_beacon_list(value)
            data["beacon_entries"] = entries
            data["beacon_pillar"] = set()
            if not entries and isinstance(value, str) and value.strip():
                data["beacon_raw"] = value
            elif entries:
                try:
                    local_value = client.local(
                        mid,
                        "beacons.list",
                        kwarg={"return_yaml": False, "include_pillar": False},
                    )[0].get(mid, {})
                except SaltApiError:
                    pass
                else:
                    data["beacon_pillar"] = set(entries) - set(
                        parse_beacon_list(local_value)
                    )
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
            get_session()
            .query(JobReturn)
            .filter_by(minion_id=mid)
            .order_by(JobReturn.jid.desc())
            .limit(20)
            .all()
        )

        def ret_key(r):
            if sort == "status":
                return (0 if r.success else 1, r.jid)
            return r.jid

        data["returns"] = sorted(returns, key=ret_key, reverse=(direction == "desc"))
    recent = []
    if tab == "overview":
        recent_returns = (
            get_session()
            .query(JobReturn)
            .filter_by(minion_id=mid)
            .order_by(JobReturn.jid.desc())
            .limit(5)
            .all()
        )
        funs = {
            j.jid: j.fun
            for j in get_session()
            .query(Job)
            .filter(Job.jid.in_([r.jid for r in recent_returns]))
            .all()
        }
        recent = [{"ret": r, "fun": funs.get(r.jid, "—")} for r in recent_returns]
    ctx = {
        "mid": mid,
        "tab": tab,
        "tabs": DETAIL_TABS,
        "data": data,
        "error": error,
        "snapshot": bool(row),
        "sort": sort,
        "direction": direction,
        "key_status": row.key_status if row else None,
        "last_seen": row.last_seen if row else None,
        "conformity": (row.conformity or {}) if row else {},
        "recent": recent,
        "os_icon": os_icon_slug(data["grains"]),
    }
    if tab == "jobs" and request.headers.get("HX-Request") == "true":
        return render_template("_minion_returns.html", **ctx)
    return render_template("minion_detail.html", **ctx)


@bp.post("/<mid>/beacons/<action>")
@roles_required("operator")
def beacon_act(mid: str, action: str):
    """Enable/disable one beacon (runtime state only; definitions live
    in pillar and are never edited here)."""
    if action not in BEACON_ACTIONS:
        flash("Unknown beacon action.", "error")
        return redirect(url_for("minions.detail", mid=mid, tab="beacons"))
    name = request.form.get("beacon", "")
    if not name:
        flash("Beacon name is required.", "error")
        return redirect(url_for("minions.detail", mid=mid, tab="beacons"))
    try:
        outcome = get_salt().local(mid, BEACON_ACTIONS[action], arg=[name])
    except SaltApiError as exc:
        flash(f"salt-api error: {exc}", "error")
        return redirect(url_for("minions.detail", mid=mid, tab="beacons"))
    refusal = _beacon_refusal(outcome, mid)
    if refusal is not None:
        flash(f"{mid}/{name}: {refusal}", "error")
        return redirect(url_for("minions.detail", mid=mid, tab="beacons"))
    log_event(current_user.username, f"beacon-{action}:{name}")
    flash(f"{mid}/{name}: {action}d.", "success")
    return redirect(url_for("minions.detail", mid=mid, tab="beacons"))
