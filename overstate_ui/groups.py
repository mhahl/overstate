"""Minion groups: saved member lists used by the group job target."""

from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from .audit import log_event
from .auth import roles_required
from .dashboard import get_salt
from .db import get_session
from .minions import PAGE_SIZES, cached_roster
from .models import Minion, MinionGroup

bp = Blueprint("groups", __name__, url_prefix="/groups")


@bp.get("/", strict_slashes=False)
@login_required
def index():
    q = request.args.get("q", "").strip().lower()
    try:
        per_page = int(request.args.get("per_page", 25))
    except ValueError:
        per_page = 25
    if per_page not in PAGE_SIZES:
        per_page = 25
    try:
        page = int(request.args.get("page", 1))
    except ValueError:
        page = 1
    from flask_login import current_user as _u

    from .authz import has_fleet as _has_fleet
    from .authz import minions_with as _minions_with
    from .authz import own_group_ids as _own_ids
    from .authz import rbac_mode as _mode
    from .authz import require as _require

    _require("group.read")
    _scoped = _mode() == "scoped"
    _fleet = not _scoped or _has_fleet(_u, "group.read")
    _allowed: set[str] | None = None
    _own: set[str] = set()
    if _scoped and not _fleet:
        _allowed = _minions_with(_u, "group.read")
        _own = _own_ids(_u)
    all_groups = get_session().query(MinionGroup).order_by(MinionGroup.name).all()
    if _scoped and not _fleet:
        _roster = {row.id for row in get_session().query(Minion.id).all()}
        all_groups = [
            g
            for g in all_groups
            if str(g.id) in _own
            or (set(g.members or []) & _roster) <= _allowed
        ]
    if q:
        all_groups = [
            g
            for g in all_groups
            if q in g.name.lower() or any(q in m.lower() for m in g.members)
        ]
    total = len(all_groups)
    sort = request.args.get("sort", "name")
    if sort not in ("name", "members"):
        sort = "name"
    direction = request.args.get("dir", "asc")
    if direction not in ("asc", "desc"):
        direction = "asc"
    if sort == "members":
        all_groups.sort(
            key=lambda g: (len(g.members or []), g.name), reverse=(direction == "desc")
        )
    elif direction == "desc":
        all_groups.sort(key=lambda g: g.name, reverse=True)
    pages = max(1, (total + per_page - 1) // per_page)
    page = min(max(1, page), pages)
    statuses, _, _ = cached_roster(get_salt())
    roster = sorted(row.id for row in get_session().query(Minion.id).all())
    if _scoped and not _fleet:
        # The member picker renders only in-scope ids.
        roster = [m for m in roster if m in _allowed]
    return render_template(
        "groups.html",
        q=request.args.get("q", ""),
        page=page,
        pages=pages,
        per_page=per_page,
        total=total,
        sort=sort,
        direction=direction,
        groups=all_groups[(page - 1) * per_page : page * per_page],
        group_names=[g.name for g in all_groups],
        roster=roster,
        presence=sorted(set(roster) | set(statuses)) if _fleet or not _scoped else roster,
    )



def _frozen_group_ids() -> set[str]:
    """Group ids named as scope_value by a grant or IdP mapping."""
    from .db import get_session
    from .models import Grant, IdpRoleMapping

    session = get_session()
    refs = {
        row[0]
        for row in session.query(Grant.scope_value)
        .filter_by(scope_kind="group")
        .all()
    }
    refs |= {
        row[0]
        for row in session.query(IdpRoleMapping.scope_value)
        .filter_by(scope_kind="group")
        .all()
    }
    return refs


def _require_group_write(members: list[str] | None = None) -> None:
    """group.write, plus new members inside scope and the freeze rule."""
    from flask import abort
    from flask_login import current_user

    from .authz import audit_deny, has_fleet, minions_with, rbac_mode, require

    if rbac_mode() != "scoped":
        return
    require("group.write")
    if members is not None and not has_fleet(current_user, "group.write"):
        allowed = minions_with(current_user, "group.write")
        outside = [m for m in members if m not in allowed]
        if outside:
            audit_deny("group.write", detail="out-of-scope")
            abort(403)


def _require_unfrozen(gid: int) -> None:
    from flask import abort
    from flask_login import current_user

    from .authz import audit_deny, has_fleet, rbac_mode

    if rbac_mode() != "scoped":
        return
    if str(gid) in _frozen_group_ids() and not has_fleet(
        current_user, "grant.admin"
    ):
        audit_deny("group.write", detail="out-of-scope")
        abort(403)


def parse_member_ids(form) -> list[str]:
    """Member IDs from a multi-select, with comma/space fallback.

    The group modal posts one `members` value per selected minion;
    typed input still splits on commas and whitespace. Deduped,
    order kept.
    """
    raw = " ".join(form.getlist("members"))
    seen: list[str] = []
    for token in raw.replace(",", " ").split():
        token = token.strip()
        if token and token not in seen:
            seen.append(token)
    return seen


@bp.post("/", strict_slashes=False)
@roles_required("operator")
def create_group():
    name = request.form.get("name", "").strip()
    members = parse_member_ids(request.form)
    _require_group_write(members)
    session = get_session()
    if not name:
        flash("Group needs a name.", "error")
    elif session.query(MinionGroup).filter_by(name=name).first():
        flash(f"Group '{name}' already exists.", "error")
    else:
        session.add(MinionGroup(name=name, members=members))
        session.commit()
        log_event(current_user.username, f"group-create:{name}")
        flash(f"Group '{name}' saved with {len(members)} members.", "success")
    return redirect(url_for("groups.index"))


@bp.post("/<int:gid>/rename")
@roles_required("operator")
def rename_group(gid: int):
    _require_group_write()
    _require_unfrozen(gid)
    session = get_session()
    group = session.get(MinionGroup, gid)
    name = request.form.get("name", "").strip()
    if group is None:
        flash("Unknown group.", "error")
    elif not name:
        flash("Group needs a name.", "error")
    elif (
        session.query(MinionGroup)
        .filter(MinionGroup.name == name, MinionGroup.id != gid)
        .first()
    ):
        flash(f"Group '{name}' already exists.", "error")
    else:
        log_event(current_user.username, f"group-rename:{group.name}->{name}")
        group.name = name
        session.commit()
        flash(f"Group renamed to '{name}'.", "success")
    return redirect(url_for("groups.index"))


@bp.post("/<int:gid>/members")
@roles_required("operator")
def edit_group_members(gid: int):
    _require_group_write(parse_member_ids(request.form))
    _require_unfrozen(gid)
    session = get_session()
    group = session.get(MinionGroup, gid)
    if group is None:
        flash("Unknown group.", "error")
    else:
        group.members = parse_member_ids(request.form)
        session.commit()
        log_event(current_user.username, f"group-members:{group.name}")
        flash(f"Group '{group.name}' now has {len(group.members)} members.", "success")
    return redirect(url_for("groups.index"))


@bp.post("/<int:gid>/edit")
@roles_required("operator")
def edit_group(gid: int):
    """Combined rename plus member update from the group modal."""
    _require_group_write(parse_member_ids(request.form))
    _require_unfrozen(gid)
    session = get_session()
    group = session.get(MinionGroup, gid)
    name = request.form.get("name", "").strip()
    if group is None:
        flash("Unknown group.", "error")
    elif not name:
        flash("Group needs a name.", "error")
    elif (
        session.query(MinionGroup)
        .filter(MinionGroup.name == name, MinionGroup.id != gid)
        .first()
    ):
        flash(f"Group '{name}' already exists.", "error")
    else:
        if name != group.name:
            log_event(current_user.username, f"group-rename:{group.name}->{name}")
            group.name = name
        group.members = parse_member_ids(request.form)
        session.commit()
        log_event(current_user.username, f"group-members:{group.name}")
        flash(
            f"Group '{group.name}' saved with {len(group.members)} members.", "success"
        )
    return redirect(url_for("groups.index"))


@bp.post("/<int:gid>/delete")
@roles_required("operator")
def delete_group(gid: int):
    from flask import abort
    from flask_login import current_user

    from .authz import audit_deny, rbac_mode

    _require_group_write()
    if rbac_mode() == "scoped" and str(gid) in _frozen_group_ids():
        # A referenced group is never deleted: no cascade drops the
        # grant or the mapping. Rename keeps the id, so the grant
        # keeps resolving.
        audit_deny("group.write", detail="out-of-scope")
        abort(403)
    session = get_session()
    group = session.get(MinionGroup, gid)
    if group is None:
        flash("Unknown group.", "error")
    else:
        log_event(current_user.username, f"group-delete:{group.name}")
        session.delete(group)
        session.commit()
        flash(f"Group '{group.name}' deleted.", "success")
    return redirect(url_for("groups.index"))
