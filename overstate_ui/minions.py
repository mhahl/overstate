"""Minion inventory: live list merged from Salt, snapshot cache, detail tabs.

Routes only: helpers live in :mod:`overstate_ui.minions_helpers` and
are re-exported here so existing ``overstate_ui.minions.*`` import
paths keep working.
"""

import csv
import datetime as dt
import io

import httpx
from flask import (
    Blueprint,
    Response,
    abort,
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
from .files import highlight_json
from .inventory import GRAIN_COLUMNS
from .jobs_helpers import FUN_RE, _split_state_id, _state_label
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
    summarize_schedule,
)
from .models import Job, JobReturn, Minion
from .salt_client import SaltApiError

bp = Blueprint("minions", __name__, url_prefix="/minions")

PAGE_SIZES = (10, 25, 50)
DETAIL_TABS = [
    "overview",
    "states",
    "jobs",
    "schedule",
    "pillar",
    "beacons",
    "mine",
    "raw",
]
BEACON_ACTIONS = {
    "enable": "beacons.enable_beacon",
    "disable": "beacons.disable_beacon",
}
SORT_COLUMNS = ("id", "key", "presence", "os")


def _exact_mid(mid: str) -> str:
    """A path minion id is one exact minion: glob characters would fan a
    single-minion read out to the fleet, so they 404 instead."""
    if not mid or any(c in mid for c in "*?[]"):
        abort(404)
    return mid


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
    "_exact_mid",
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


def _summarize_state_run(payload: dict) -> list[dict]:
    """Per-state chips from a highstate-style return payload.

    The State column shows the human label (``id: name``) instead of
    the raw ``module_|-id_|-name_|-fun`` tag — same helper as the job
    detail page, so both surfaces name states identically.
    """
    rows = []
    if not isinstance(payload, dict):
        return rows
    for sid in sorted(payload):
        st = payload[sid]
        if not isinstance(st, dict):
            rows.append({"id": sid, "sls": "", "verdict": "unknown", "comment": ""})
            continue
        module, name = _split_state_id(sid)
        result = st.get("result")
        changes = st.get("changes")
        if result is False:
            verdict = "failed"
        elif result is True:
            verdict = "changed" if changes else "ok"
        else:
            verdict = "changed" if changes else "unknown"
        rows.append(
            {
                "id": _state_label(sid, st, module, name),
                "sls": st.get("__sls__", "") or "",
                "verdict": verdict,
                "comment": str(st.get("comment", ""))[:200],
            }
        )
    return rows


def _states_stored(mid: str) -> dict | None:
    """Newest persisted state-style return for a minion, if any."""
    session = get_session()
    ret = (
        session.query(JobReturn)
        .join(Job, Job.jid == JobReturn.jid)
        .filter(JobReturn.minion_id == mid, Job.fun.like("state.%"))
        .order_by(JobReturn.jid.desc())
        .first()
    )
    if ret is None:
        return None
    job = session.get(Job, ret.jid)
    payload = ret.payload if isinstance(ret.payload, dict) else {}
    return {
        "jid": ret.jid,
        "fun": job.fun if job else "",
        "success": ret.success,
        "states": _summarize_state_run(payload),
        "raw": payload,
    }


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
    the table, so bulk checkbox selections survive polling. Served from
    a short Redis cache shared by every gunicorn worker, so an open tab
    costs one Salt round-trip per TTL, not one per worker per poll."""
    from .tasks_queue import read_presence_cache, write_presence_cache

    cached = read_presence_cache()
    if isinstance(cached, dict):
        return jsonify(cached)
    statuses, up, _ = live_roster(get_salt())
    rows = minion_rows(statuses, up, "", "")
    payload = {r["id"]: presence_of(r) for r in rows}
    write_presence_cache(payload)
    return jsonify(payload)


@bp.route("/export.csv")
@login_required
def export_csv():
    q = request.args.get("q", "").strip()
    status_filter = request.args.get("status", "")
    if status_filter not in ("", "accepted", "pending", "rejected", "denied"):
        status_filter = ""
    statuses, up, _ = live_roster(get_salt())
    rows = minion_rows(statuses, up, q, status_filter)
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
                "up" if r["up"] else ("accepted, dead" if r["dead"] else "down"),
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
            flash("Refresh queued. Reload to see it.", "info")
        else:
            flash(f"refresh failed in the background: {value}", "error")
    return redirect(url_for("minions.index"))


@bp.post("/<mid>/refresh")
@roles_required("operator")
def refresh_one(mid: str):
    """Re-pull grains for a single minion into the snapshot cache."""
    mid = _exact_mid(mid)
    row = get_session().get(Minion, mid)
    if row is None:
        flash(f"Unknown minion '{mid}'.", "error")
        return redirect(url_for("minions.index"))
    try:
        grains = get_salt().local(mid, "grains.items", tgt_type="list")[0].get(mid)
    except SaltApiError as exc:
        flash(f"salt-api error: {exc}", "error")
        return redirect(url_for("minions.index"))
    if not isinstance(grains, dict):
        flash(f"{mid} returned no grain data.", "error")
        return redirect(url_for("minions.index"))
    row.grains = normalize_grains(grains)
    row.last_seen = dt.datetime.now(dt.UTC)
    get_session().commit()
    log_event(current_user.username, f"minion-refresh:{mid}")
    flash(f"{mid} refreshed.", "success")
    return redirect(url_for("minions.index"))


@bp.post("/<mid>/remove")
@roles_required("operator")
def remove(mid: str):
    """Drop a minion's snapshot row from the database. Job history is
    kept. The Salt key on the master is only deleted when the remove
    dialog's checkbox asks for it — and then only when the wheel call
    succeeds, so a failed key delete leaves the snapshot in place
    instead of half-finishing."""
    mid = _exact_mid(mid)
    session = get_session()
    row = session.get(Minion, mid)
    if row is None:
        flash(f"Unknown minion '{mid}'.", "error")
        return redirect(url_for("minions.index"))
    delete_key = request.form.get("delete_key") == "yes"
    if delete_key:
        try:
            get_salt().wheel("key.delete", match=mid)
        except SaltApiError as exc:
            flash(f"salt-api error: {exc}", "error")
            return redirect(url_for("minions.index"))
        log_event(current_user.username, f"delete-key:{mid}")
    session.delete(row)
    session.commit()
    log_event(current_user.username, f"minion-remove:{mid}")
    if delete_key:
        flash(f"{mid} removed; Salt key deleted.", "success")
    else:
        flash(f"{mid} removed from the inventory cache.", "success")
    return redirect(url_for("minions.index"))


@bp.post("/<mid>/states/refresh")
@roles_required("operator")
def states_refresh(mid: str):
    """One-shot live highstate description for a minion.

    Display-only: the live result is shown next to the stored return
    and never written into job history. Any failure degrades to the
    stored view plus an advisory note."""
    mid = _exact_mid(mid)
    from .tasks import queue_or_none, show_highstate_now, show_highstate_task, wait_for

    live: dict | None = None
    note: str | None = None
    try:
        queued = queue_or_none(show_highstate_task, mid)
        if queued is None:
            live = show_highstate_now(get_salt(), mid)
        else:
            status, value = wait_for(queued, wait=10.0)
            if status == "ready" and isinstance(value, dict) and value:
                live = value
            elif status == "pending":
                note = "Refresh still running — showing stored data."
            else:
                note = f"Refresh failed in the background: {value}"
    except (SaltApiError, httpx.HTTPError) as exc:
        note = f"Live refresh unavailable: salt-api error: {exc}"
    if live:
        log_event(current_user.username, f"states-refresh:{mid}")
        flash(f"{mid}: {len(live)} live states loaded.", "success")
    else:
        note = note or "Live refresh returned nothing — showing stored data."
        flash(note, "warning")
    row = get_session().get(Minion, mid)
    grains = normalize_grains(row.grains if row else {})
    return render_template(
        "minion_detail.html",
        mid=mid,
        tab="states",
        tabs=DETAIL_TABS,
        data={
            "grains": grains,
            "stored": _states_stored(mid),
            "live": _summarize_state_run(live) if live else None,
            "live_note": note,
        },
        error=None,
        snapshot=bool(row),
        sort="jid",
        direction="desc",
        key_status=row.key_status if row else None,
        last_seen=row.last_seen if row else None,
        conformity=(row.conformity or {}) if row else {},
        recent=[],
        os_icon=os_icon_slug(grains),
    )


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
    mid = _exact_mid(mid)
    tab = request.args.get("tab", "overview")
    if tab not in DETAIL_TABS:
        tab = "overview"
    client = get_salt()
    row = get_session().get(Minion, mid)
    data: dict = {"grains": normalize_grains(row.grains if row else {})}
    error = None
    try:
        if tab == "overview":
            live = client.local(mid, "grains.items", tgt_type="list")[0].get(mid)
            if isinstance(live, dict):
                data["grains"] = normalize_grains(live)
        elif tab == "states":
            # Stored first: the last persisted state-style return. Live
            # data arrives only via the explicit Refresh action below,
            # so a down minion never hangs this page.
            data["stored"] = _states_stored(mid)
            data["live"] = None
            data["live_note"] = None
        elif tab == "schedule":
            # return_yaml=False keeps this a real mapping: an empty
            # schedule arrives as {} so the empty state triggers,
            # not as the string "schedule: {}\n" (see schedules.index).
            data["schedule"] = client.local(
                mid,
                "schedule.list",
                tgt_type="list",
                kwarg={"return_yaml": False},
            )[0].get(mid, {})
            data["sched_enabled"], data["schedule_rows"] = summarize_schedule(
                data["schedule"]
            )
        elif tab == "pillar":
            data["pillar"] = client.local(mid, "pillar.items", tgt_type="list")[0].get(
                mid
            )
            data["pillar_html"] = (
                highlight_json(data["pillar"]) if data["pillar"] else None
            )
        elif tab == "mine":
            # This minion's stored mine values for one function:
            # mine.get run on the minion answers {mid: value}.
            # Missing/None means nothing stored (empty state).
            mine_fun = request.args.get("mine_fun", "").strip()
            data["mine_fun"] = mine_fun
            data["mine_found"] = False
            data["mine_value"] = None
            if mine_fun and FUN_RE.match(mine_fun):
                stored = client.local(
                    mid, "mine.get", arg=[mid, mine_fun], tgt_type="list"
                )[0].get(mid, {})
                value = stored.get(mid) if isinstance(stored, dict) else stored
                if value is not None:
                    data["mine_found"] = True
                    data["mine_value"] = value
        elif tab == "beacons":
            # return_yaml=False keeps this a real mapping (see
            # schedules.index). A second pillar-excluded call
            # attributes the `pillar` source badge; when it fails
            # no badge is shown rather than a wrong one.
            value = client.local(
                mid,
                "beacons.list",
                tgt_type="list",
                kwarg={"return_yaml": False},
            )[0].get(mid, {})
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
                        tgt_type="list",
                        kwarg={"return_yaml": False, "include_pillar": False},
                    )[0].get(mid, {})
                except SaltApiError:
                    pass
                else:
                    data["beacon_pillar"] = set(entries) - set(
                        parse_beacon_list(local_value)
                    )
        elif tab == "raw":
            # Advanced diagnostic dump: the payloads the per-tab raw
            # accordions used to show, in one panel. Stored states come
            # from the DB; schedule and pillar cost one live call each —
            # the price of opening this tab, never prefetched elsewhere.
            data["stored"] = _states_stored(mid)
            data["schedule"] = client.local(
                mid,
                "schedule.list",
                tgt_type="list",
                kwarg={"return_yaml": False},
            )[0].get(mid, {})
            data["pillar"] = client.local(mid, "pillar.items", tgt_type="list")[0].get(
                mid
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
    mid = _exact_mid(mid)
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
        outcome = get_salt().local(
            mid, BEACON_ACTIONS[action], arg=[name], tgt_type="list"
        )
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
