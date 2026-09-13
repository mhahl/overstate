"""Minion groups: saved member lists used by the group job target."""

from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from .auth import roles_required

from .dashboard import get_salt
from .db import get_session
from .minions import PAGE_SIZES, live_roster
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
    all_groups = (get_session().query(MinionGroup)
                  .order_by(MinionGroup.name).all())
    if q:
        all_groups = [g for g in all_groups
                      if q in g.name.lower()
                      or any(q in m.lower() for m in g.members)]
    total = len(all_groups)
    pages = max(1, (total + per_page - 1) // per_page)
    page = min(max(1, page), pages)
    statuses, _ = live_roster(get_salt())
    roster = sorted(row.id for row in get_session().query(Minion.id).all())
    return render_template(
        "groups.html", q=request.args.get("q", ""), page=page, pages=pages,
        per_page=per_page, total=total,
        groups=all_groups[(page - 1) * per_page: page * per_page],
        group_names=[g.name for g in all_groups],
        roster=roster,
        presence=sorted(set(roster) | set(statuses)))


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
    from .audit import log_event

    name = request.form.get("name", "").strip()
    members = parse_member_ids(request.form)
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
    from .audit import log_event

    session = get_session()
    group = session.get(MinionGroup, gid)
    name = request.form.get("name", "").strip()
    if group is None:
        flash("Unknown group.", "error")
    elif not name:
        flash("Group needs a name.", "error")
    elif (session.query(MinionGroup)
          .filter(MinionGroup.name == name, MinionGroup.id != gid).first()):
        flash(f"Group '{name}' already exists.", "error")
    else:
        log_event(current_user.username,
                  f"group-rename:{group.name}->{name}")
        group.name = name
        session.commit()
        flash(f"Group renamed to '{name}'.", "success")
    return redirect(url_for("groups.index"))


@bp.post("/<int:gid>/members")
@roles_required("operator")
def edit_group_members(gid: int):
    from .audit import log_event

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
    from .audit import log_event

    session = get_session()
    group = session.get(MinionGroup, gid)
    name = request.form.get("name", "").strip()
    if group is None:
        flash("Unknown group.", "error")
    elif not name:
        flash("Group needs a name.", "error")
    elif (session.query(MinionGroup)
          .filter(MinionGroup.name == name, MinionGroup.id != gid).first()):
        flash(f"Group '{name}' already exists.", "error")
    else:
        if name != group.name:
            log_event(current_user.username,
                      f"group-rename:{group.name}->{name}")
            group.name = name
        group.members = parse_member_ids(request.form)
        session.commit()
        log_event(current_user.username, f"group-members:{group.name}")
        flash(f"Group '{group.name}' saved "
              f"with {len(group.members)} members.", "success")
    return redirect(url_for("groups.index"))


@bp.post("/<int:gid>/delete")
@roles_required("operator")
def delete_group(gid: int):
    from .audit import log_event

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
