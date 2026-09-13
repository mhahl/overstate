"""Pillar explorer: rendered pillar per minion, snapshots, minion-vs-minion
diff. Snapshots are captured on explicit action only; Salt is truth."""

from __future__ import annotations

import json

from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_login import login_required

from .auth import roles_required
from .dashboard import get_salt
from .db import get_session
from .models import Minion, PillarSnapshot
from .salt_client import SaltApiError

bp = Blueprint("pillar", __name__, url_prefix="/pillar")

SNAPSHOT_LIMIT = 10


def flatten(prefix: str, obj, out: dict) -> None:
    """Flatten nested pillar into dotted-path leaves. Lists compare as a
    whole so reordering shows as one change, not per-index noise."""
    if isinstance(obj, dict):
        for key, value in obj.items():
            flatten(f"{prefix}{key}.", value, out)
    elif isinstance(obj, list):
        out[prefix.rstrip(".")] = json.dumps(obj, sort_keys=True, default=str)
    else:
        out[prefix.rstrip(".")] = obj


def diff_pillars(left: dict, right: dict) -> list[dict]:
    """Row-wise diff of two rendered pillars: added / removed / changed."""
    flat_left: dict = {}
    flat_right: dict = {}
    flatten("", left or {}, flat_left)
    flatten("", right or {}, flat_right)
    rows = []
    for path in sorted(set(flat_left) | set(flat_right)):
        old = flat_left.get(path, None)
        new = flat_right.get(path, None)
        if path not in flat_left:
            rows.append({"path": path, "kind": "added", "old": None, "new": new})
        elif path not in flat_right:
            rows.append({"path": path, "kind": "removed", "old": old, "new": None})
        elif json.dumps(old, sort_keys=True, default=str) != json.dumps(
            new, sort_keys=True, default=str
        ):
            rows.append({"path": path, "kind": "changed", "old": old, "new": new})
    return rows


def capture_pillar(mid: str) -> PillarSnapshot:
    """Fetch rendered pillar via salt-api and store a snapshot, pruning to
    the newest SNAPSHOT_LIMIT rows for the minion."""
    client = get_salt()
    payload = client.local(mid, "pillar.items")[0].get(mid)
    if not isinstance(payload, dict):
        raise SaltApiError(f"no pillar data returned for {mid}")
    session = get_session()
    snap = PillarSnapshot(minion_id=mid, payload=payload)
    session.add(snap)
    session.flush()
    stale = (
        session.query(PillarSnapshot)
        .filter_by(minion_id=mid)
        .order_by(PillarSnapshot.id.desc())
        .offset(SNAPSHOT_LIMIT)
        .all()
    )
    for row in stale:
        session.delete(row)
    session.commit()
    return snap


def minion_ids() -> list[str]:
    session = get_session()
    return [row.id for row in session.query(Minion).order_by(Minion.id).all()]


def snapshots_for(mid: str) -> list[PillarSnapshot]:
    return (
        get_session()
        .query(PillarSnapshot)
        .filter_by(minion_id=mid)
        .order_by(PillarSnapshot.id.desc())
        .limit(SNAPSHOT_LIMIT)
        .all()
    )


def render_value(value) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, indent=1, sort_keys=True, default=str)


@bp.route("/")
@login_required
def index():
    mids = minion_ids()
    counts = {
        mid: get_session().query(PillarSnapshot).filter_by(minion_id=mid).count()
        for mid in mids
    }
    q = request.args.get("q", "").strip().lower()
    if q:
        mids = [m for m in mids if q in m.lower()]
    sort = request.args.get("sort", "minion")
    if sort not in ("minion", "snapshots"):
        sort = "minion"
    direction = request.args.get("dir", "asc")
    if direction not in ("asc", "desc"):
        direction = "asc"
    if sort == "snapshots":
        mids = sorted(
            mids, key=lambda m: (counts.get(m, 0), m), reverse=(direction == "desc")
        )
    elif direction == "desc":
        mids = sorted(mids, reverse=True)
    return render_template(
        "pillar.html",
        mids=mids,
        counts=counts,
        q=request.args.get("q", ""),
        sort=sort,
        direction=direction,
    )


@bp.route("/<mid>")
@login_required
def detail(mid: str):
    snaps = snapshots_for(mid)
    live = None
    error = None
    try:
        live = get_salt().local(mid, "pillar.items")[0].get(mid)
    except SaltApiError as exc:
        error = str(exc)
    return render_template(
        "pillar_detail.html",
        mid=mid,
        snaps=snaps,
        live=live,
        error=error,
        render_value=render_value,
    )


@bp.post("/<mid>/capture")
@roles_required("operator")
def capture(mid: str):
    try:
        capture_pillar(mid)
    except SaltApiError as exc:
        flash(f"salt-api error: {exc}", "error")
    else:
        flash(f"Pillar snapshot captured for {mid}.", "success")
    return redirect(url_for("pillar.detail", mid=mid))


@bp.route("/diff")
@login_required
def diff():
    mids = minion_ids()
    mid_a = request.args.get("a", "")
    mid_b = request.args.get("b", "")
    rev_a = request.args.get("rev_a", "")
    rev_b = request.args.get("rev_b", "")
    left = _resolve(mid_a, rev_a) if mid_a else None
    right = _resolve(mid_b, rev_b) if mid_b else None
    rows = (
        diff_pillars(left or {}, right or {})
        if left is not None and right is not None
        else []
    )
    snaps_a = snapshots_for(mid_a) if mid_a else []
    snaps_b = snapshots_for(mid_b) if mid_b else []
    return render_template(
        "pillar_diff.html",
        mids=mids,
        mid_a=mid_a,
        mid_b=mid_b,
        rev_a=rev_a,
        rev_b=rev_b,
        snaps_a=snaps_a,
        snaps_b=snaps_b,
        rows=rows,
        compared=left is not None and right is not None,
        render_value=render_value,
    )


def _resolve(mid: str, rev: str) -> dict | None:
    """Snapshot payload by id, 'live' for a fresh salt-api read, or the
    newest snapshot when rev is empty."""
    session = get_session()
    if rev == "live":
        try:
            data = get_salt().local(mid, "pillar.items")[0].get(mid)
        except SaltApiError:
            return None
        return data if isinstance(data, dict) else None
    if rev:
        try:
            snap = session.get(PillarSnapshot, int(rev))
        except ValueError:
            return None
        if snap is None or snap.minion_id != mid:
            return None
        return snap.payload
    snaps = snapshots_for(mid)
    return snaps[0].payload if snaps else None
