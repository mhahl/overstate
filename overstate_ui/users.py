"""Admin user management. Lists users, changes roles, deletes users."""

from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_login import current_user

from .auth import LEVELS, roles_required
from .authz import permission_required
from .db import get_session
from .models import User

bp = Blueprint("users", __name__, url_prefix="/users")

#: Role filter values accepted in scoped mode: the ladder plus the cache.
SCOPED_ROLE_FILTERS = ("viewer", "operator", "admin", "scoped", "none")

#: Grouped role options for the grant dialog.
ROLE_GROUPS = [
    ("Global (today's roles)", ["viewer", "operator", "admin"]),
    (
        "Team",
        [
            "team-viewer",
            "team-operator",
            "team-state",
            "secrets-reader",
            "scheduler",
            "team-lead",
        ],
    ),
    (
        "Duty",
        ["security-reader", "auditor", "key-custodian", "file-reader", "file-editor"],
    ),
]


def _fleet_grant_admin() -> bool:
    """Full grant power: legacy admin, or a fleet ``grant.admin`` grant."""
    from .authz import has_fleet, rbac_mode

    return rbac_mode() != "scoped" or has_fleet(current_user, "grant.admin")


def _fleet_user_admin() -> bool:
    """Full directory power: legacy admin, or a fleet ``user.admin``."""
    from .authz import has_fleet, rbac_mode

    return rbac_mode() != "scoped" or has_fleet(current_user, "user.admin")


def _deny(perm: str, detail: str = "no-grant") -> None:
    """Audit a forged write and stop with 403. Responses carry no ids."""
    from flask import abort

    from .authz import audit_deny

    audit_deny(perm, detail=detail)
    abort(403)


def _delegate_grant_write_error(
    role: str, scope_kind: str, scope_value: str, grant=None, subject_user_id=None
) -> str | None:
    """None when a caller without fleet ``grant.admin`` may write this
    grant: the delegate subset rule, plus no self-edit, plus no edits
    to ``source=backfill`` fleet ladder grants (those are fleet admin's).
    """
    from .authz import LADDER_ROLES, delegate_grant_error

    if (
        grant is not None
        and grant.source == "backfill"
        and grant.role in LADDER_ROLES
        and grant.scope_kind == "fleet"
    ):
        return "Backfill ladder grants need a fleet admin."
    if subject_user_id is not None and subject_user_id == current_user.id:
        return "You cannot edit your own grants."
    return delegate_grant_error(current_user, role, scope_kind, scope_value)


def auth_label(user) -> str:
    """Auth source for the Users table. Branch on ``kind`` first: a
    service account has neither password nor OIDC subject."""
    if getattr(user, "kind", "human") == "service":
        return "service"
    if getattr(user, "password_hash", None):
        return "local"
    return "sso"


@bp.route("/")
@permission_required("user.read", "user.admin")
def index():
    from .authz import has_fleet, rbac_mode
    from .models import Grant, IdpRoleMapping, LocalGroup, UserIdpGroup

    if rbac_mode() == "scoped" and not (
        has_fleet(current_user, "user.admin")
        or has_fleet(current_user, "user.read")
    ):
        # Minion-scoped user.read: the filtered delegate view, 200 and
        # filtered, never 403. Fleet grants, tokens, and service
        # accounts are not in the query result.
        return _delegate_index()
    role = request.args.get("role", "")
    if rbac_mode() == "scoped":
        if role not in SCOPED_ROLE_FILTERS:
            role = ""
    elif role not in LEVELS:
        role = ""
    sort = request.args.get("sort", "username")
    if sort not in ("username", "role"):
        sort = "username"
    direction = request.args.get("dir", "asc")
    if direction not in ("asc", "desc"):
        direction = "asc"
    session = get_session()
    query = session.query(User)
    if role:
        query = query.filter_by(role=role)
    col = User.username if sort == "username" else User.role
    col = col.desc() if direction == "desc" else col.asc()
    users = query.order_by(col, User.username).all()
    grants_by_user: dict[int, list] = {}
    if users:
        for grant in (
            session.query(Grant)
            .filter(
                Grant.subject_kind == "user",
                Grant.subject_user_id.in_([u.id for u in users]),
            )
            .order_by(Grant.role, Grant.scope_kind, Grant.scope_value)
            .all()
        ):
            grants_by_user.setdefault(grant.subject_user_id, []).append(grant)
    idp_by_user: dict[int, list] = {}
    if users:
        claims = (
            session.query(UserIdpGroup)
            .filter(UserIdpGroup.user_id.in_([u.id for u in users]))
            .all()
        )
        if claims:
            mappings = (
                session.query(IdpRoleMapping)
                .filter(
                    IdpRoleMapping.idp_group.in_({c.group_name for c in claims})
                )
                .all()
            )
            by_group: dict[str, list] = {}
            for mapping in mappings:
                by_group.setdefault(mapping.idp_group, []).append(mapping)
            for claim in claims:
                idp_by_user.setdefault(claim.user_id, []).extend(
                    (claim.group_name, m) for m in by_group.get(claim.group_name, [])
                )
    from .models import Grant as _Grant
    from .models import LocalGroupMember

    local_groups = session.query(LocalGroup).order_by(LocalGroup.name).all()
    group_names = {str(g.id): g.name for g in local_groups}
    group_grants: dict[int, list] = {}
    if local_groups:
        for grant in (
            session.query(_Grant)
            .filter(
                _Grant.subject_kind == "local_group",
                _Grant.subject_group_id.in_([g.id for g in local_groups]),
            )
            .order_by(_Grant.role, _Grant.scope_kind, _Grant.scope_value)
            .all()
        ):
            group_grants.setdefault(grant.subject_group_id, []).append(grant)
    member_ids: dict[int, list] = {}
    for row in session.query(LocalGroupMember).all():
        member_ids.setdefault(row.group_id, []).append(row.user_id)
    all_users = session.query(User).order_by(User.username).all()
    from .inventory import SNAPSHOT_GRAINS

    def _scope_label(kind, value):
        if kind == "fleet":
            return "fleet"
        if kind == "group":
            return "group " + group_names.get(value, value)
        return f"{kind}:{value}"

    user_grant_rows: dict[int, list] = {}
    for uid, rows in grants_by_user.items():
        user_grant_rows[uid] = [
            {
                "grant": g,
                "delete_url": url_for("users.delete_grant", uid=uid, gid=g.id),
                "scope": _scope_label(g.scope_kind, g.scope_value),
                "readonly": False,
            }
            for g in rows
        ]
    for uid, pairs in idp_by_user.items():
        user_grant_rows.setdefault(uid, []).extend(
            {
                "grant": {"role": m.role, "source": m.origin},
                "delete_url": "",
                "scope": _scope_label(m.scope_kind, m.scope_value),
                "readonly": True,
            }
            for _, m in pairs
        )
    group_grant_rows: dict[int, list] = {}
    for gid, rows in group_grants.items():
        group_grant_rows[gid] = [
            {
                "grant": g,
                "delete_url": url_for(
                    "users.delete_local_group_grant", gid=gid, grant_id=g.id
                ),
                "scope": _scope_label(g.scope_kind, g.scope_value),
                "readonly": False,
            }
            for g in rows
        ]
    from .models import ApiToken as _ApiToken
    from .models import SavedJob as _SavedJob

    service_rows: list = []
    saved_jobs: list = []
    if rbac_mode() == "scoped":
        for candidate in all_users:
            if getattr(candidate, "kind", "human") != "service":
                continue
            service_rows.append(
                {
                    "user": candidate,
                    "tokens": session.query(_ApiToken)
                    .filter_by(user_id=candidate.id)
                    .order_by(_ApiToken.id)
                    .all(),
                }
            )
        saved_jobs = session.query(_SavedJob).order_by(_SavedJob.name).all()
    return render_template(
        "users.html",
        users=users,
        roles=list(LEVELS),
        role=role,
        sort=sort,
        direction=direction,
        auth_label=auth_label,
        grants_by_user=grants_by_user,
        idp_by_user=idp_by_user,
        local_groups=local_groups,
        group_names=group_names,
        group_grants=group_grants,
        user_grant_rows=user_grant_rows,
        group_grant_rows=group_grant_rows,
        member_ids=member_ids,
        all_users=all_users,
        grain_keys=list(SNAPSHOT_GRAINS),
        role_groups=ROLE_GROUPS,
        service_rows=service_rows,
        saved_jobs=saved_jobs,
    )


def _delegate_index():
    """Filtered Users page for minion-scoped ``user.read``.

    Lists users and local groups holding a grant inside the caller's
    user.read scope, with only those grant rows. Service accounts,
    fleet grants, tokens, and the rotation card are never queried, so
    the template cannot receive them. Member pickers include current
    members of manageable groups (otherwise saving would silently drop
    them); editing UI renders only where the delegate rules can pass,
    and every writer re-checks.
    """
    from .authz import (
        FLEET_ONLY_ROLES,
        delegate_group_error,
        perm_scope_minions,
        scope_inside,
    )
    from .inventory import SNAPSHOT_GRAINS
    from .models import (
        Grant,
        IdpRoleMapping,
        LocalGroup,
        LocalGroupMember,
        UserIdpGroup,
    )

    lead_scope = perm_scope_minions(current_user, "user.read")
    session = get_session()

    def inside(scope_kind, scope_value) -> bool:
        return scope_inside(scope_kind, scope_value, lead_scope)

    role = request.args.get("role", "")
    if role not in SCOPED_ROLE_FILTERS:
        role = ""
    sort = request.args.get("sort", "username")
    if sort not in ("username", "role"):
        sort = "username"
    direction = request.args.get("dir", "asc")
    if direction not in ("asc", "desc"):
        direction = "asc"

    kept = [
        grant
        for grant in session.query(Grant)
        .order_by(Grant.role, Grant.scope_kind, Grant.scope_value)
        .all()
        if grant.scope_kind != "fleet"
        and inside(grant.scope_kind, grant.scope_value)
    ]
    user_grants = [grant for grant in kept if grant.subject_kind == "user"]
    shown_ids = [grant.subject_user_id for grant in user_grants]
    users = (
        session.query(User).filter(User.id.in_(shown_ids)).all() if shown_ids else []
    )
    users = [u for u in users if getattr(u, "kind", "human") != "service"]
    if role:
        users = [u for u in users if u.role == role]
    users.sort(
        key=lambda u: (
            u.username if sort == "username" else (u.role, u.username)
        ),
        reverse=(direction == "desc"),
    )
    shown = {u.id for u in users}
    grants_by_user: dict[int, list] = {}
    for grant in user_grants:
        if grant.subject_user_id in shown:
            grants_by_user.setdefault(grant.subject_user_id, []).append(grant)
    idp_by_user: dict[int, list] = {}
    if users:
        claims = (
            session.query(UserIdpGroup)
            .filter(UserIdpGroup.user_id.in_([u.id for u in users]))
            .all()
        )
        if claims:
            mappings = (
                session.query(IdpRoleMapping)
                .filter(
                    IdpRoleMapping.idp_group.in_({c.group_name for c in claims})
                )
                .all()
            )
            by_group: dict[str, list] = {}
            for mapping in mappings:
                if mapping.scope_kind != "fleet" and inside(
                    mapping.scope_kind, mapping.scope_value
                ):
                    by_group.setdefault(mapping.idp_group, []).append(mapping)
            for claim in claims:
                rows = by_group.get(claim.group_name, [])
                if rows:
                    idp_by_user.setdefault(claim.user_id, []).extend(
                        (claim.group_name, m) for m in rows
                    )
    group_rows = [grant for grant in kept if grant.subject_kind == "local_group"]
    group_ids = sorted({grant.subject_group_id for grant in group_rows})
    local_groups = (
        session.query(LocalGroup)
        .filter(LocalGroup.id.in_(group_ids))
        .order_by(LocalGroup.name)
        .all()
        if group_ids
        else []
    )
    present = {group.id for group in local_groups}
    group_grants: dict[int, list] = {}
    for grant in group_rows:
        if grant.subject_group_id in present:
            group_grants.setdefault(grant.subject_group_id, []).append(grant)
    member_ids: dict[int, list] = {}
    if local_groups:
        for row in (
            session.query(LocalGroupMember)
            .filter(LocalGroupMember.group_id.in_([g.id for g in local_groups]))
            .all()
        ):
            member_ids.setdefault(row.group_id, []).append(row.user_id)
    manageable = {
        group.id
        for group in local_groups
        if delegate_group_error(current_user, group.id) is None
    }
    option_ids = set(shown)
    for gid in manageable:
        option_ids.update(member_ids.get(gid, []))
    all_users = (
        session.query(User)
        .filter(User.id.in_(sorted(option_ids)))
        .order_by(User.username)
        .all()
        if option_ids
        else []
    )
    group_names = {str(g.id): g.name for g in local_groups}
    delegable_roles = [
        (label, [r for r in roles if r not in FLEET_ONLY_ROLES and r not in ("file-reader", "file-editor")])
        for label, roles in ROLE_GROUPS
    ]
    delegable_roles = [(label, roles) for label, roles in delegable_roles if roles]

    def _scope_label(kind, value):
        if kind == "fleet":
            return "fleet"
        if kind == "group":
            return "group " + group_names.get(value, value)
        return f"{kind}:{value}"

    user_grant_rows: dict[int, list] = {}
    for uid, rows in grants_by_user.items():
        user_grant_rows[uid] = [
            {
                "grant": g,
                "delete_url": url_for("users.delete_grant", uid=uid, gid=g.id),
                "scope": _scope_label(g.scope_kind, g.scope_value),
                "readonly": _delegate_grant_write_error(
                    g.role,
                    g.scope_kind,
                    g.scope_value,
                    grant=g,
                    subject_user_id=uid,
                )
                is not None,
            }
            for g in rows
        ]
    group_grant_rows: dict[int, list] = {}
    for gid, rows in group_grants.items():
        group_grant_rows[gid] = [
            {
                "grant": g,
                "delete_url": url_for(
                    "users.delete_local_group_grant", gid=gid, grant_id=g.id
                ),
                "scope": _scope_label(g.scope_kind, g.scope_value),
                "readonly": _delegate_grant_write_error(
                    g.role, g.scope_kind, g.scope_value, grant=g
                )
                is not None,
            }
            for g in rows
        ]
    return render_template(
        "users.html",
        users=users,
        roles=list(LEVELS),
        role=role,
        sort=sort,
        direction=direction,
        auth_label=auth_label,
        grants_by_user=grants_by_user,
        idp_by_user=idp_by_user,
        local_groups=local_groups,
        group_names=group_names,
        group_grants=group_grants,
        user_grant_rows=user_grant_rows,
        group_grant_rows=group_grant_rows,
        member_ids=member_ids,
        all_users=all_users,
        grain_keys=list(SNAPSHOT_GRAINS),
        role_groups=delegable_roles,
        service_rows=[],
        saved_jobs=[],
        delegate_view=True,
        local_group_manageable=manageable,
    )


@bp.post("/<int:uid>/role")
@roles_required("admin")
def set_role(uid: int):
    from .authz import rbac_mode, refresh_role_cache, require
    from .models import Grant

    if rbac_mode() == "scoped":
        require("user.admin")
    session = get_session()
    user = session.get(User, uid)
    role = request.form.get("role", "")
    if user is None:
        flash("Unknown user.", "error")
    elif role not in LEVELS:
        flash("Unknown role.", "error")
    elif user.id == current_user.id and role != "admin":
        flash("You cannot demote yourself.", "error")
    elif rbac_mode() != "scoped":
        user.role = role
        session.commit()
        flash(f"'{user.username}' is now {role}.", "success")
    else:
        # Scoped mode writes a manual fleet grant of that ladder role,
        # replacing any fleet ladder grant, and recomputes the cache.
        # Non-fleet grants are left alone. The dropdown stays the global
        # rung, labeled fleet-wide including secrets.
        session.query(Grant).filter_by(
            subject_kind="user", subject_user_id=user.id, scope_kind="fleet"
        ).filter(Grant.role.in_(("viewer", "operator", "admin"))).delete(
            synchronize_session=False
        )
        session.add(
            Grant(
                subject_kind="user",
                subject_user_id=user.id,
                subject_group_id=None,
                role=role,
                scope_kind="fleet",
                scope_value="*",
                source="manual",
                created_by=current_user.id,
            )
        )
        from .audit import log_event

        log_event(
            current_user.username,
            "grant-create",
            permission="grant.admin",
            detail=f"{role} fleet * user:{user.username}",
        )
        refresh_role_cache(user)
        session.commit()
        flash(f"'{user.username}' is now {role}.", "success")
    return redirect(url_for("users.index"))


@bp.route("/groups")
@permission_required("user.read", "user.admin")
def groups():
    """User groups directory: manual local groups plus IdP membership.

    Local groups are created and edited by hand; IdP groups appear
    automatically from the OIDC groups claim at each login and are
    read-only here. Scoped leads without fleet ``user.read`` see only
    the local groups they may manage, and no IdP section: membership
    across teams is not theirs to browse.
    """
    from .authz import (
        FLEET_ONLY_ROLES,
        delegate_group_error,
        perm_scope_minions,
        rbac_mode,
        scope_inside,
    )
    from .inventory import SNAPSHOT_GRAINS
    from .models import (
        Grant,
        IdpRoleMapping,
        LocalGroup,
        LocalGroupMember,
        UserIdpGroup,
    )

    session = get_session()
    scoped = rbac_mode() == "scoped"
    fleet = _fleet_user_admin()
    delegate_view = scoped and not fleet
    group_names: dict[str, str] = {}

    def _scope_label(kind, value):
        if kind == "fleet":
            return "fleet"
        if kind == "group":
            return "group " + group_names.get(value, value)
        return f"{kind}:{value}"

    if delegate_view:
        lead_scope = perm_scope_minions(current_user, "user.read")

        def _inside(kind, value) -> bool:
            return scope_inside(kind, value, lead_scope)

        kept = [
            grant
            for grant in session.query(Grant)
            .order_by(Grant.role, Grant.scope_kind, Grant.scope_value)
            .all()
            if grant.subject_kind == "local_group"
            and grant.scope_kind != "fleet"
            and _inside(grant.scope_kind, grant.scope_value)
        ]
        group_ids = sorted({grant.subject_group_id for grant in kept})
        local_groups = (
            session.query(LocalGroup)
            .filter(LocalGroup.id.in_(group_ids))
            .order_by(LocalGroup.name)
            .all()
            if group_ids
            else []
        )
        present = {group.id for group in local_groups}
        group_grants: dict[int, list] = {}
        for grant in kept:
            if grant.subject_group_id in present:
                group_grants.setdefault(grant.subject_group_id, []).append(grant)
        member_ids: dict[int, list] = {}
        if local_groups:
            for row in (
                session.query(LocalGroupMember)
                .filter(LocalGroupMember.group_id.in_([g.id for g in local_groups]))
                .all()
            ):
                member_ids.setdefault(row.group_id, []).append(row.user_id)
        manageable = {
            group.id
            for group in local_groups
            if delegate_group_error(current_user, group.id) is None
        }
        option_ids = set()
        for ids in member_ids.values():
            option_ids.update(ids)
        for grant in (
            session.query(Grant)
            .filter(
                Grant.subject_kind == "user",
                Grant.scope_kind != "fleet",
            )
            .all()
        ):
            if _inside(grant.scope_kind, grant.scope_value):
                option_ids.add(grant.subject_user_id)
        all_users = (
            session.query(User)
            .filter(User.id.in_(sorted(option_ids)))
            .order_by(User.username)
            .all()
            if option_ids
            else []
        )
        delegable_roles = [
            (
                label,
                [
                    r
                    for r in roles
                    if r not in FLEET_ONLY_ROLES
                    and r not in ("file-reader", "file-editor")
                ],
            )
            for label, roles in ROLE_GROUPS
        ]
        role_groups = [(label, roles) for label, roles in delegable_roles if roles]
        idp_groups: list = []
    else:
        local_groups = session.query(LocalGroup).order_by(LocalGroup.name).all()
        group_grants = {}
        if local_groups:
            for grant in (
                session.query(Grant)
                .filter(
                    Grant.subject_kind == "local_group",
                    Grant.subject_group_id.in_([g.id for g in local_groups]),
                )
                .order_by(Grant.role, Grant.scope_kind, Grant.scope_value)
                .all()
            ):
                group_grants.setdefault(grant.subject_group_id, []).append(grant)
        member_ids = {}
        for row in session.query(LocalGroupMember).all():
            member_ids.setdefault(row.group_id, []).append(row.user_id)
        all_users = session.query(User).order_by(User.username).all()
        manageable = {group.id for group in local_groups}
        role_groups = ROLE_GROUPS
        claims = session.query(UserIdpGroup).all()
        users_by_id = {
            u.id: u.username for u in session.query(User.id, User.username).all()
        }
        members: dict[str, set] = {}
        for claim_row in claims:
            members.setdefault(claim_row.group_name, set()).add(
                users_by_id.get(claim_row.user_id, f"#{claim_row.user_id}")
            )
        mappings: dict[str, list] = {}
        if members:
            for mapping in (
                session.query(IdpRoleMapping)
                .filter(IdpRoleMapping.idp_group.in_(set(members)))
                .order_by(IdpRoleMapping.role)
                .all()
            ):
                mappings.setdefault(mapping.idp_group, []).append(mapping)
        idp_groups = [
            {
                "name": name,
                "members": sorted(users, key=str.lower),
                "mappings": mappings.get(name, []),
            }
            for name, users in sorted(members.items(), key=lambda kv: kv[0].lower())
        ]
    group_names = {str(g.id): g.name for g in local_groups}
    group_grant_rows: dict[int, list] = {}
    for gid, rows in group_grants.items():
        group_grant_rows[gid] = [
            {
                "grant": g,
                "delete_url": url_for(
                    "users.delete_local_group_grant", gid=gid, grant_id=g.id
                ),
                "scope": _scope_label(g.scope_kind, g.scope_value),
                "readonly": delegate_view
                and _delegate_grant_write_error(
                    g.role, g.scope_kind, g.scope_value, grant=g
                )
                is not None,
            }
            for g in rows
        ]
    return render_template(
        "users_groups.html",
        local_groups=local_groups,
        group_names=group_names,
        group_grants=group_grants,
        group_grant_rows=group_grant_rows,
        member_ids=member_ids,
        all_users=all_users,
        local_group_manageable=manageable,
        grain_keys=list(SNAPSHOT_GRAINS),
        role_groups=role_groups,
        delegate_view=delegate_view,
        idp_groups=idp_groups,
    )


def _validate_grant_scope(role: str, scope_kind: str, form) -> tuple[str | None, str | None]:
    """Validate a grant dialog post. Returns (error, scope_value):
    error names the problem for a flash, scope_value is the normalized
    stored value. Fleet-only roles are rejected off fleet; file roles
    are prefix-or-fleet only; every other role takes a minion matcher."""
    from .authz import FLEET_ONLY_ROLES, ROLE_PERMS
    from .groups import parse_member_ids
    from .inventory import SNAPSHOT_GRAINS
    from .models import MinionGroup

    if role not in ROLE_PERMS:
        return "Unknown role.", None
    if scope_kind == "fleet":
        return None, "*"
    if role in FLEET_ONLY_ROLES:
        return f"Role '{role}' can only be granted on the whole fleet.", None
    if scope_kind == "prefix":
        if role not in ("file-reader", "file-editor"):
            return f"Role '{role}' has no file permissions to scope to a path.", None
        from posixpath import normpath

        prefix = (form.get("scope_value", "") or "").strip().strip("/")
        if not prefix or prefix.startswith("/") or ".." in prefix.split("/"):
            return "Prefix must be a relative path without '..'.", None
        return None, normpath(prefix)
    if role in ("file-reader", "file-editor"):
        return f"Role '{role}' can only be granted on a file prefix or the fleet.", None
    if scope_kind == "group":
        raw = (form.get("scope_value_group", "") or "").strip() or (
            form.get("scope_value", "") or ""
        ).strip()
        if not raw.isdigit():
            return "Pick a minion group.", None
        session = get_session()
        if session.get(MinionGroup, int(raw)) is None:
            return "Unknown minion group.", None
        return None, raw
    if scope_kind == "list":
        members = parse_member_ids(form)
        if not members:
            return "A list scope needs at least one minion id.", None
        import json

        return None, json.dumps(members)
    if scope_kind == "glob":
        pattern = (form.get("scope_value", "") or "").strip()
        if not pattern:
            return "A glob scope needs a pattern.", None
        return None, pattern
    if scope_kind == "grain":
        key = (form.get("grain_key", "") or "").strip()
        value = (form.get("grain_value", "") or "").strip()
        if key not in SNAPSHOT_GRAINS:
            return "Pick a snapshot grain key.", None
        if not value:
            return "A grain scope needs a value.", None
        return None, f"{key}:{value}"
    return "Unknown scope kind.", None


def _subject_label(grant) -> str:
    if grant.subject_kind == "local_group":
        from .models import LocalGroup

        group = get_session().get(LocalGroup, grant.subject_group_id)
        name = group.name if group else str(grant.subject_group_id)
        return f"local_group:{name}"
    user = get_session().get(User, grant.subject_user_id)
    return f"user:{user.username if user else grant.subject_user_id}"


@bp.post("/<int:uid>/grants")
@permission_required("grant.admin", "grant.delegate")
def create_grant(uid: int):
    """Grant dialog writer. Fleet ``grant.admin`` in scoped mode, or
    ``grant.delegate`` under the subset rule (no self-edit)."""
    from sqlalchemy.exc import IntegrityError

    from .audit import log_event
    from .models import Grant

    session = get_session()
    user = session.get(User, uid)
    role = request.form.get("role", "").strip()
    scope_kind = request.form.get("scope_kind", "").strip()
    if user is None:
        flash("Unknown user.", "error")
        return redirect(url_for("users.index"))
    error, scope_value = _validate_grant_scope(role, scope_kind, request.form)
    if error is not None:
        flash(error, "error")
        return redirect(url_for("users.index"))
    delegated = not _fleet_grant_admin()
    if delegated:
        err = _delegate_grant_write_error(
            role, scope_kind, scope_value, subject_user_id=user.id
        )
        if err is not None:
            _deny("grant.delegate", detail="out-of-scope")
    try:
        session.add(
            Grant(
                subject_kind="user",
                subject_user_id=user.id,
                subject_group_id=None,
                role=role,
                scope_kind=scope_kind,
                scope_value=scope_value,
                source="manual",
                created_by=current_user.id,
            )
        )
        session.commit()
    except IntegrityError:
        session.rollback()
        flash("That grant already exists.", "error")
        return redirect(url_for("users.index"))
    from .authz import refresh_role_cache

    log_event(
        current_user.username,
        "grant-create",
        permission="grant.delegate" if delegated else "grant.admin",
        detail=f"{role} {scope_kind} {scope_value} user:{user.username}",
    )
    refresh_role_cache(user)
    flash(f"Granted {role} to '{user.username}'.", "success")
    return redirect(url_for("users.index"))


@bp.post("/<int:uid>/grants/<int:gid>/delete")
@permission_required("grant.admin", "grant.delegate")
def delete_grant(uid: int, gid: int):
    from .audit import log_event
    from .authz import refresh_role_cache
    from .models import Grant

    session = get_session()
    grant = session.get(Grant, gid)
    if grant is None or grant.subject_kind != "user" or grant.subject_user_id != uid:
        flash("Unknown grant.", "error")
        return redirect(url_for("users.index"))
    delegated = not _fleet_grant_admin()
    if delegated:
        err = _delegate_grant_write_error(
            grant.role,
            grant.scope_kind,
            grant.scope_value,
            grant=grant,
            subject_user_id=grant.subject_user_id,
        )
        if err is not None:
            _deny("grant.delegate", detail="out-of-scope")
    if (
        grant.subject_user_id == current_user.id
        and grant.role == "admin"
        and grant.scope_kind == "fleet"
        and not _has_other_fleet_admin(session, current_user.id, exclude=grant.id)
    ):
        flash("You cannot delete your own last fleet admin grant.", "error")
        return redirect(url_for("users.index"))
    label = _subject_label(grant)
    detail = f"{grant.role} {grant.scope_kind} {grant.scope_value} {label}"
    user = session.get(User, uid)
    session.delete(grant)
    session.commit()
    log_event(
        current_user.username,
        "grant-delete",
        permission="grant.delegate" if delegated else "grant.admin",
        detail=detail,
    )
    if user is not None:
        refresh_role_cache(user)
    flash("Grant deleted.", "success")
    return redirect(url_for("users.index"))


def _has_other_fleet_admin(session, user_id: int, exclude: int) -> bool:
    """Another fleet admin grant, or an admin IdP mapping still applying."""
    from .models import Grant, IdpRoleMapping, UserIdpGroup

    other = (
        session.query(Grant.id)
        .filter(
            Grant.subject_kind == "user",
            Grant.subject_user_id == user_id,
            Grant.role == "admin",
            Grant.scope_kind == "fleet",
            Grant.id != exclude,
        )
        .first()
    )
    if other is not None:
        return True
    idp_names = [
        row.group_name
        for row in session.query(UserIdpGroup).filter_by(user_id=user_id).all()
    ]
    if not idp_names:
        return False
    return (
        session.query(IdpRoleMapping.id)
        .filter(
            IdpRoleMapping.idp_group.in_(idp_names),
            IdpRoleMapping.role == "admin",
            IdpRoleMapping.scope_kind == "fleet",
        )
        .first()
        is not None
    )


@bp.post("/local-groups")
@permission_required("user.admin", "grant.delegate")
def create_local_group():
    """Local-group section on the Groups page (not a top-level product).

    Delegates may create an empty group; membership and grants on it
    still pass the subset rule per write."""
    from .models import LocalGroup

    session = get_session()
    name = request.form.get("name", "").strip()
    if not name:
        flash("Group needs a name.", "error")
    elif session.query(LocalGroup).filter_by(name=name).first():
        flash(f"Group '{name}' already exists.", "error")
    else:
        session.add(LocalGroup(name=name))
        session.commit()
        flash(f"Local group '{name}' created.", "success")
    return redirect(url_for("users.groups"))


@bp.post("/local-groups/<int:gid>/rename")
@permission_required("user.admin", "grant.delegate")
def rename_local_group(gid: int):
    """Rename changes no access, so delegates may use it."""
    from .models import LocalGroup

    session = get_session()
    group = session.get(LocalGroup, gid)
    name = request.form.get("name", "").strip()
    if group is None:
        flash("Unknown group.", "error")
    elif not name:
        flash("Group needs a name.", "error")
    elif session.query(LocalGroup).filter(LocalGroup.name == name, LocalGroup.id != gid).first():
        flash(f"Group '{name}' already exists.", "error")
    else:
        group.name = name
        session.commit()
        flash(f"Local group renamed to '{name}'.", "success")
    return redirect(url_for("users.groups"))


@bp.post("/local-groups/<int:gid>/members")
@permission_required("user.admin", "grant.delegate")
def edit_local_group_members(gid: int):
    """Replace the member list; recompute the role cache for everyone
    whose membership changed. Delegates may edit only a group whose
    grants all sit inside their delegated scope."""
    from .audit import log_event
    from .authz import delegate_group_error, refresh_role_cache
    from .models import LocalGroup, LocalGroupMember

    session = get_session()
    group = session.get(LocalGroup, gid)
    if group is None:
        flash("Unknown group.", "error")
        return redirect(url_for("users.groups"))
    delegated = not _fleet_user_admin()
    if delegated:
        err = delegate_group_error(current_user, gid)
        if err is not None:
            _deny("grant.delegate", detail="out-of-scope")
    wanted = set()
    for raw in request.form.getlist("user_ids"):
        raw = raw.strip()
        if raw.isdigit():
            wanted.add(int(raw))
    users = session.query(User).filter(User.id.in_(wanted)).all() if wanted else []
    wanted = {u.id for u in users}
    current = {
        row.user_id for row in session.query(LocalGroupMember).filter_by(group_id=gid).all()
    }
    for user_id in wanted - current:
        session.add(LocalGroupMember(group_id=gid, user_id=user_id))
    if current - wanted:
        session.query(LocalGroupMember).filter_by(group_id=gid).filter(
            LocalGroupMember.user_id.in_(current - wanted)
        ).delete(synchronize_session=False)
    session.commit()
    for user_id in (wanted | current):
        member = session.get(User, user_id)
        if member is not None:
            refresh_role_cache(member)
    log_event(
        current_user.username,
        f"local-group-members:{group.name}",
        permission="grant.delegate" if delegated else "user.admin",
    )
    flash(f"Local group '{group.name}' now has {len(wanted)} members.", "success")
    return redirect(url_for("users.groups"))


@bp.post("/local-groups/<int:gid>/delete")
@permission_required("user.admin", "grant.delegate")
def delete_local_group(gid: int):
    """Deleting only removes access, but a delegate may still only
    delete a group whose grants sit inside their delegated scope."""
    from .authz import delegate_group_error, refresh_role_cache
    from .models import Grant, LocalGroup, LocalGroupMember

    session = get_session()
    group = session.get(LocalGroup, gid)
    if group is None:
        flash("Unknown group.", "error")
        return redirect(url_for("users.groups"))
    if not _fleet_user_admin():
        err = delegate_group_error(current_user, gid)
        if err is not None:
            _deny("grant.delegate", detail="out-of-scope")
    member_ids = [
        row.user_id for row in session.query(LocalGroupMember).filter_by(group_id=gid).all()
    ]
    session.query(LocalGroupMember).filter_by(group_id=gid).delete(synchronize_session=False)
    session.query(Grant).filter_by(subject_kind="local_group", subject_group_id=gid).delete(
        synchronize_session=False
    )
    session.delete(group)
    session.commit()
    for user_id in member_ids:
        member = session.get(User, user_id)
        if member is not None:
            refresh_role_cache(member)
    flash(f"Local group '{group.name}' deleted.", "success")
    return redirect(url_for("users.groups"))


@bp.post("/local-groups/<int:gid>/grants")
@permission_required("grant.admin", "grant.delegate")
def create_local_group_grant(gid: int):
    """Same dialog as user grants, with a group subject. Delegates use
    the subset rule; a group grant is never a self-edit."""
    from sqlalchemy.exc import IntegrityError

    from .audit import log_event
    from .authz import refresh_role_cache
    from .models import Grant, LocalGroup, LocalGroupMember

    session = get_session()
    group = session.get(LocalGroup, gid)
    role = request.form.get("role", "").strip()
    scope_kind = request.form.get("scope_kind", "").strip()
    if group is None:
        flash("Unknown group.", "error")
        return redirect(url_for("users.groups"))
    error, scope_value = _validate_grant_scope(role, scope_kind, request.form)
    if error is not None:
        flash(error, "error")
        return redirect(url_for("users.groups"))
    delegated = not _fleet_grant_admin()
    if delegated:
        err = _delegate_grant_write_error(role, scope_kind, scope_value)
        if err is not None:
            _deny("grant.delegate", detail="out-of-scope")
    try:
        session.add(
            Grant(
                subject_kind="local_group",
                subject_user_id=None,
                subject_group_id=group.id,
                role=role,
                scope_kind=scope_kind,
                scope_value=scope_value,
                source="manual",
                created_by=current_user.id,
            )
        )
        session.commit()
    except IntegrityError:
        session.rollback()
        flash("That grant already exists.", "error")
        return redirect(url_for("users.groups"))
    log_event(
        current_user.username,
        "grant-create",
        permission="grant.delegate" if delegated else "grant.admin",
        detail=f"{role} {scope_kind} {scope_value} local_group:{group.name}",
    )
    for row in session.query(LocalGroupMember).filter_by(group_id=group.id).all():
        member = session.get(User, row.user_id)
        if member is not None:
            refresh_role_cache(member)
    flash(f"Granted {role} to group '{group.name}'.", "success")
    return redirect(url_for("users.groups"))


@bp.post("/local-groups/<int:gid>/grants/<int:grant_id>/delete")
@permission_required("grant.admin", "grant.delegate")
def delete_local_group_grant(gid: int, grant_id: int):
    from .audit import log_event
    from .authz import refresh_role_cache
    from .models import Grant, LocalGroup, LocalGroupMember

    session = get_session()
    grant = session.get(Grant, grant_id)
    if (
        grant is None
        or grant.subject_kind != "local_group"
        or grant.subject_group_id != gid
    ):
        flash("Unknown grant.", "error")
        return redirect(url_for("users.groups"))
    delegated = not _fleet_grant_admin()
    if delegated:
        err = _delegate_grant_write_error(
            grant.role, grant.scope_kind, grant.scope_value, grant=grant
        )
        if err is not None:
            _deny("grant.delegate", detail="out-of-scope")
    group = session.get(LocalGroup, gid)
    detail = (
        f"{grant.role} {grant.scope_kind} {grant.scope_value} "
        f"local_group:{group.name if group else gid}"
    )
    member_ids = [
        row.user_id for row in session.query(LocalGroupMember).filter_by(group_id=gid).all()
    ]
    session.delete(grant)
    session.commit()
    log_event(
        current_user.username,
        "grant-delete",
        permission="grant.delegate" if delegated else "grant.admin",
        detail=detail,
    )
    for user_id in member_ids:
        member = session.get(User, user_id)
        if member is not None:
            refresh_role_cache(member)
    flash("Grant deleted.", "success")
    return redirect(url_for("users.groups"))


@bp.post("/service-accounts")
@roles_required("admin")
def create_service_account():
    """Mint a service account: no password, no OIDC, no ladder role."""
    from .authz import rbac_mode, refresh_role_cache, require

    if rbac_mode() == "scoped":
        require("user.admin")
    name = (request.form.get("name") or "").strip()
    session = get_session()
    if not name or len(name) > 64:
        flash("Service account name is required (max 64 chars).", "error")
    elif session.query(User).filter_by(username=name).first() is not None:
        flash(f"'{name}' is already taken.", "error")
    else:
        user = User(username=name, password_hash=None, role="none", kind="service")
        session.add(user)
        session.commit()
        refresh_role_cache(user)
        flash(f"Service account '{name}' created with no grants.", "success")
    return redirect(url_for("users.index"))


@bp.post("/service-accounts/<int:uid>/tokens")
@roles_required("admin")
def create_token(uid: int):
    """Mint a Bearer [REDACTED] a service account. The raw token is flashed
    once and never stored — only the argon2 hash and prefix persist."""
    import datetime as dt
    import secrets

    from .api import log_token_event
    from .auth import _ph
    from .authz import rbac_mode, require
    from .models import ApiToken, SavedJob

    if rbac_mode() == "scoped":
        require("user.admin")
    session = get_session()
    user = session.get(User, uid)
    if user is None or getattr(user, "kind", "human") != "service":
        flash("Unknown service account.", "error")
        return redirect(url_for("users.index"))
    name = (request.form.get("name") or "").strip()
    saved_job_id = None
    raw_pin = (request.form.get("saved_job_id") or "").strip()
    if raw_pin:
        try:
            saved_job_id = int(raw_pin)
        except ValueError:
            saved_job_id = -1
        if session.get(SavedJob, saved_job_id) is None:
            flash("Unknown saved job pin.", "error")
            return redirect(url_for("users.index"))
    expires_at = None
    raw_days = (request.form.get("expires_days") or "").strip()
    if raw_days:
        try:
            days = int(raw_days)
        except ValueError:
            days = 0
        if days < 1 or days > 3650:
            flash("Expiry must be 1–3650 days.", "error")
            return redirect(url_for("users.index"))
        expires_at = dt.datetime.now(dt.UTC).replace(tzinfo=None) + dt.timedelta(
            days=days
        )
    if not name or len(name) > 64:
        flash("Token name is required (max 64 chars).", "error")
        return redirect(url_for("users.index"))
    prefix = secrets.token_hex(6)
    raw = f"{prefix}_{secrets.token_urlsafe(32)}"
    session.add(
        ApiToken(
            user_id=user.id,
            name=name,
            token_prefix=prefix,
            token_hash=_ph.hash(raw),
            saved_job_id=saved_job_id,
            expires_at=expires_at,
            created_by=current_user.id,
        )
    )
    session.commit()
    log_token_event(current_user.username, "token-create", prefix, name)
    flash(
        f"Token '{name}' created — copy it now, it will not be shown again: {raw}",
        "success",
    )
    return redirect(url_for("users.index"))


@bp.post("/service-accounts/<int:uid>/tokens/<int:tid>/revoke")
@roles_required("admin")
def revoke_token(uid: int, tid: int):
    """Revoke a token now. Fires in flight are unaffected; the next Bearer
    use 401s."""
    import datetime as dt

    from .api import log_token_event
    from .authz import rbac_mode, require
    from .models import ApiToken

    if rbac_mode() == "scoped":
        require("user.admin")
    session = get_session()
    row = session.get(ApiToken, tid)
    if row is None or row.user_id != uid:
        flash("Unknown token.", "error")
    elif row.revoked_at is not None:
        flash("Token is already revoked.", "error")
    else:
        row.revoked_at = dt.datetime.now(dt.UTC).replace(tzinfo=None)
        session.commit()
        log_token_event(current_user.username, "token-revoke", row.token_prefix, row.name)
        flash(f"Token '{row.name}' revoked.", "success")
    return redirect(url_for("users.index"))


@bp.post("/<int:uid>/delete")
@roles_required("admin")
def delete(uid: int):
    from .authz import rbac_mode, require

    if rbac_mode() == "scoped":
        require("user.admin")
    session = get_session()
    user = session.get(User, uid)
    if user is None:
        flash("Unknown user.", "error")
    elif user.id == current_user.id:
        flash("You cannot delete yourself.", "error")
    else:
        session.delete(user)
        session.commit()
        flash(f"Deleted '{user.username}'.", "success")
    return redirect(url_for("users.index"))
