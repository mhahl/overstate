"""Scoped RBAC evaluation: pure function over rows already in Postgres.

Grants of built-in roles on scopes, unioned, evaluated in-process. No
Salt calls on this path: job targets are intersected against the
``minions`` snapshot locally before publish. While ``rbac_mode()`` is
legacy (in particular while ``ENFORCEMENT_COMPLETE`` is False) none of
this is consulted; ``users.role`` plus ``roles_required`` stay the
authority.
"""

from __future__ import annotations

import fnmatch
import json
import logging

from flask import g, has_app_context

from .db import get_session
from .inventory import SNAPSHOT_GRAINS
from .jobs_helpers import ALLOWED_FUNS
from .models import Grant, IdpRoleMapping, LocalGroupMember, Minion, UserIdpGroup

logger = logging.getLogger(__name__)

# While False, rbac_mode() stays legacy even if the flag row says
# scoped. Rollback is the flag row, not this constant.
ENFORCEMENT_COMPLETE = True

LADDER_ROLES = ("viewer", "operator", "admin")

FLEET_ONLY_ROLES = frozenset(
    {"viewer", "operator", "admin", "security-reader", "auditor", "key-custodian"}
)

_VIEWER_PERMS = frozenset(
    {
        "minion.read",
        "minion.onboard",
        "pillar.read",
        "mine.read",
        "job.read",
        "state.read",
        "schedule.read",
        "beacon.read",
        "group.read",
        "file.read",
        "reactor.read",
        "event.read",
        "audit.read",
        "key.read",
        "settings.read",
    }
)

_OPERATOR_DELTA = frozenset(
    {
        "minion.refresh",
        "minion.remove",
        "pillar.capture",
        "job.run.read",
        "job.run.change",
        "job.run.state",
        "job.run.orchestrate",
        "job.kill",
        "job.save",
        "job.batch",
        "key.accept",
        "key.delete",
        "schedule.write",
        "beacon.write",
        "group.write",
        "file.write",
        "file.sync",
        "reactor.write",
        "state.watch",
        "console.runner",
        "dashboard.probe",
    }
)

_ADMIN_DELTA = frozenset(
    {
        "file.git",
        "reactor.persist",
        "master.read",
        "master.write",
        "master.rollout",
        "settings.write",
        "settings.rotate_eauth",
        "user.read",
        "user.admin",
        "grant.admin",
    }
)

_TEAM_VIEWER_PERMS = frozenset(
    {
        "minion.read",
        "job.read",
        "state.read",
        "schedule.read",
        "beacon.read",
        "group.read",
        "key.read",
    }
)

_TEAM_OPERATOR_DELTA = frozenset(
    {
        "minion.refresh",
        "job.run.read",
        "job.run.change",
        "job.kill",
        "job.save",
        "job.batch",
    }
)

ROLE_PERMS: dict[str, frozenset] = {
    "viewer": _VIEWER_PERMS,
    "operator": _VIEWER_PERMS | _OPERATOR_DELTA,
    "admin": _VIEWER_PERMS | _OPERATOR_DELTA | _ADMIN_DELTA,
    "team-viewer": _TEAM_VIEWER_PERMS,
    "team-operator": _TEAM_VIEWER_PERMS | _TEAM_OPERATOR_DELTA,
    "team-state": _TEAM_VIEWER_PERMS | _TEAM_OPERATOR_DELTA | {"job.run.state"},
    "secrets-reader": frozenset({"minion.read", "pillar.read", "mine.read"}),
    "scheduler": frozenset(
        {
            "minion.read",
            "schedule.read",
            "schedule.write",
            "schedule.allow.read",
            "schedule.allow.change",
            "schedule.allow.state",
            "beacon.read",
            "beacon.write",
        }
    ),
    "security-reader": frozenset(
        {
            "minion.read",
            "job.read",
            "state.read",
            "schedule.read",
            "beacon.read",
            "key.read",
            "audit.read",
            "group.read",
            "file.read",
            "reactor.read",
        }
    ),
    "auditor": frozenset(
        {
            "audit.read",
            "minion.read",
            "job.read",
            "state.read",
            "schedule.read",
            "key.read",
            "group.read",
            "settings.read",
        }
    ),
    "key-custodian": frozenset(
        {
            "key.read",
            "key.accept",
            "key.delete",
            "minion.read",
            "minion.remove",
            "minion.onboard",
        }
    ),
    "file-reader": frozenset({"file.read"}),
    "file-editor": frozenset({"file.read", "file.write"}),
    "team-lead": _TEAM_VIEWER_PERMS
    | _TEAM_OPERATOR_DELTA
    | {"job.run.state", "grant.delegate", "group.write", "user.read", "audit.read"},
}

#: Union of every built-in role: the viewer-floor backstop.
_ALL_PERMS = frozenset().union(*ROLE_PERMS.values())

#: Coarse ladder floors for the scoped branch of ``roles_required``. This
#: is only a backstop so a forgotten ``require`` still denies a zero-grant
#: user; per-view ``require("<specific perm>")`` is the real map.
LEGACY_FLOOR_PERMS: dict[int, frozenset] = {
    0: _ALL_PERMS,
    1: _OPERATOR_DELTA,
    2: _ADMIN_DELTA,
}

_FLEET_DOMAIN_PERMS = frozenset(
    {
        "minion.onboard",
        "job.run.orchestrate",
        "key.accept",
        "state.watch",
        "file.sync",
        "file.git",
        "reactor.read",
        "reactor.write",
        "reactor.persist",
        "event.read",
        "master.read",
        "master.write",
        "master.rollout",
        "settings.read",
        "settings.write",
        "settings.rotate_eauth",
        "user.admin",
        "grant.admin",
        "console.runner",
        "dashboard.probe",
    }
)

_PREFIX_DOMAIN_PERMS = frozenset({"file.read", "file.write"})

#: Honored on a minion scope as the filtered delegate view, and on fleet
#: as the unfiltered view. Never dropped on a team-lead grant.
_DELEGATE_PERMS = frozenset({"user.read", "audit.read"})

#: Free-form CI body allowlist: the read class only. ``state.show_sls``
#: is state-class, ``grains.items`` needs pillar.read, and ``schedule.add``
#: is forbidden here even though it is in ALLOWED_FUNS.
API_READ_FUNS = frozenset(
    {
        "test.ping",
        "service.status",
        "schedule.list",
        "beacons.list",
        "sys.doc",
        "sys.list_functions",
    }
)

_READ_CLASS = frozenset(
    {
        "test.ping",
        "service.status",
        "schedule.list",
        "beacons.list",
        "sys.doc",
        "sys.list_functions",
    }
)

_CHANGE_CLASS = frozenset(
    {
        "pkg.install",
        "pkg.remove",
        "service.restart",
        "ps.kill_pid",
        "mine.update",
        "saltutil.sync_all",
        "saltutil.refresh_pillar",
    }
)

_STATE_CLASS = frozenset({"state.apply", "state.highstate", "state.show_sls"})


def perm_domain(perm: str) -> str:
    """Domain of a permission: fleet, prefix, delegate, or minion."""
    if perm in _FLEET_DOMAIN_PERMS:
        return "fleet"
    if perm in _PREFIX_DOMAIN_PERMS:
        return "prefix"
    if perm in _DELEGATE_PERMS:
        return "delegate"
    return "minion"


#: Deny detail codes for audit rows. Responses and flashes must not add
#: ids the caller did not supply; the full dropped-id list is stored for
#: fleet audit.read and redacted at read for everyone else.
_REASON_DETAILS = {
    "target type needs a fleet grant": "needs-fleet",
    "unknown target type": "needs-fleet",
    "unknown function class": "no-grant",
    "empty intersection": "empty-intersection",
    "out of scope": "out-of-scope",
}


class AuthzDenied(Exception):
    """A scoped target or scope check refused publish. Carries the
    permission, a human reason, and a short machine-readable detail."""

    def __init__(self, perm: str, reason: str, detail: str | None = None):
        super().__init__(f"{perm}: {reason}")
        self.perm = perm
        self.reason = reason
        self.detail = detail or _REASON_DETAILS.get(reason, "no-grant")


def rbac_flag(key: str, env_name: str, default: str) -> str:
    """Resolve an RBAC flag: non-empty DB row wins, else non-empty env,
    else the default. Never inserts a row. Empty string counts as absent.

    Deliberately not ``get_setting``: that helper returns the env value or
    ``""`` for keys in ``OIDC_ENV_FALLBACK`` without falling through to
    defaults, which would ship the fallback off and lock out a
    half-migrated admin.
    """
    import os

    from flask import current_app

    from .models import Setting

    row = get_session().get(Setting, key)
    if row is not None and row.value.strip():
        return row.value.strip()
    env_value = ""
    if has_app_context():
        env_value = current_app.config.get(env_name, "") or ""
    if not env_value:
        env_value = os.environ.get(env_name, "") or ""
    if env_value.strip():
        return env_value.strip()
    return default


def rbac_mode() -> str:
    """'legacy' or 'scoped'. Legacy until ENFORCEMENT_COMPLETE is true,
    even if the setting row says scoped; afterwards only the exact string
    'scoped' enables scoped mode."""
    if not ENFORCEMENT_COMPLETE:
        return "legacy"
    return (
        "scoped"
        if rbac_flag("rbac_mode", "RBAC_MODE", "legacy") == "scoped"
        else "legacy"
    )


def rbac_fallback_on() -> bool:
    """Zero-grant users holding a ladder role column keep that fleet role
    until an admin turns fallback off. Only the exact string 'off'
    disables it."""
    return rbac_flag("rbac_role_fallback", "RBAC_ROLE_FALLBACK", "on") != "off"


def match_grain(grains: dict, expr: str) -> bool:
    """Match one ``key:value`` expression against a snapshot grain dict.

    Shared by grain grant scopes and grain job targets so SQLite tests and
    Postgres production cannot disagree. ``key`` must be a snapshot grain;
    full live grains are not in ``minions.grains`` and authz never calls
    ``grains.items``.
    """
    if not isinstance(grains, dict) or ":" not in expr:
        return False
    key, _, pattern = expr.partition(":")
    if key not in SNAPSHOT_GRAINS:
        return False
    value = grains.get(key)

    def _candidates(val) -> list[str]:
        if val is None or isinstance(val, dict):
            return []
        if isinstance(val, bool):
            return ["true" if val else "false"]
        if isinstance(val, (int, float)):
            return [str(val)]
        if isinstance(val, str):
            return [val]
        if isinstance(val, (list, tuple)):
            out: list[str] = []
            for item in val:
                out.extend(_candidates(item))
            return out
        return []

    has_glob = any(c in pattern for c in ("*", "?", "["))
    for candidate in _candidates(value):
        if has_glob:
            if fnmatch.fnmatchcase(candidate, pattern):
                return True
        elif candidate == pattern:
            return True
    return False


def snapshot_ids() -> set[str]:
    """Every minion id in the snapshot. One indexed query, no Salt."""
    return {
        row[0] for row in get_session().query(Minion.id).all()
    }


def snapshot_grains() -> dict[str, dict]:
    """Snapshot grain documents keyed by minion id. Python matching only:
    no Postgres ``->>``, which is not valid SQLite."""
    return {row[0]: row[1] or {} for row in get_session().query(Minion.id, Minion.grains).all()}


def scope_group_members(group_id: int | str) -> set[str]:
    """Members of a grant scope group intersected with the snapshot.

    Unknown id, missing row, or no member in the snapshot returns ``[]``
    semantics (empty set), never raises: an unknown group scope is an
    empty intersection and therefore 403, not 500.
    """
    from .models import MinionGroup

    try:
        gid = int(group_id)
    except (TypeError, ValueError):
        return set()
    group = get_session().get(MinionGroup, gid)
    if group is None:
        return set()
    members = group.members if isinstance(group.members, list) else []
    return {str(m) for m in members} & snapshot_ids()


def job_group_members(name: str) -> set[str]:
    """Same empty-on-miss rule for a job form posting a group name."""
    from .models import MinionGroup

    group = get_session().query(MinionGroup).filter_by(name=name).first()
    if group is None:
        return set()
    members = group.members if isinstance(group.members, list) else []
    return {str(m) for m in members} & snapshot_ids()


def _resolve_scope(scope_kind: str, scope_value: str) -> set[str]:
    """Minion ids a non-fleet scope resolves to. Fleet is not resolved
    here; callers short-circuit fleet grants before touching this."""
    if scope_kind == "group":
        return scope_group_members(scope_value)
    if scope_kind == "list":
        try:
            ids = json.loads(scope_value)
        except (ValueError, TypeError):
            return set()
        if not isinstance(ids, list):
            return set()
        return {str(i).strip() for i in ids if str(i).strip()} & snapshot_ids()
    if scope_kind == "glob":
        return {m for m in snapshot_ids() if fnmatch.fnmatchcase(m, scope_value)}
    if scope_kind == "grain":
        return {
            mid
            for mid, grains in snapshot_grains().items()
            if match_grain(grains, scope_value)
        }
    return set()


def _grant_entries(user) -> list[tuple[str, str, str]]:
    """(role, scope_kind, scope_value) from manual grants, local-group
    grants, and IdP mappings. No fallback: the role cache and default
    deny must see the empty case. Memoized on ``flask.g`` so a page
    rendering many rows queries once; grant-write views redirect after
    commit, so no request reads stale rows."""
    from flask import g, has_request_context

    uid = getattr(user, "id", None)
    if has_request_context():
        cache = g.get("grant_entries")
        if cache is None:
            cache = g.grant_entries = {}
        if uid in cache:
            return list(cache[uid])
    session = get_session()
    entries: list[tuple[str, str, str]] = [
        (row.role, row.scope_kind, row.scope_value)
        for row in session.query(Grant)
        .filter_by(subject_kind="user", subject_user_id=user.id)
        .all()
    ]
    member_group_ids = [
        row.group_id
        for row in session.query(LocalGroupMember).filter_by(user_id=user.id).all()
    ]
    if member_group_ids:
        entries.extend(
            (row.role, row.scope_kind, row.scope_value)
            for row in session.query(Grant)
            .filter(
                Grant.subject_kind == "local_group",
                Grant.subject_group_id.in_(member_group_ids),
            )
            .all()
        )
    idp_names = [
        row.group_name
        for row in session.query(UserIdpGroup).filter_by(user_id=user.id).all()
    ]
    if idp_names:
        entries.extend(
            (row.role, row.scope_kind, row.scope_value)
            for row in session.query(IdpRoleMapping)
            .filter(IdpRoleMapping.idp_group.in_(idp_names))
            .all()
        )
    if has_request_context():
        g.grant_entries[uid] = list(entries)
    return entries


def _effective_entries(user) -> list[tuple[str, str, str]]:
    """Grant entries plus the migration fallback: a user with zero
    effective permissions whose role column is still a ladder rung keeps
    that fleet role until fallback is turned off."""
    entries = _grant_entries(user)
    if (
        not entries
        and rbac_fallback_on()
        and (getattr(user, "role", None) in LADDER_ROLES)
    ):
        entries = [(user.role, "fleet", "*")]
    return entries


def _entry_covers(
    role: str, scope_kind: str, scope_value: str, perm: str,
    minion: str | None, prefix: str | None,
) -> bool:
    perms = ROLE_PERMS.get(role)
    if perms is None or perm not in perms:
        return False
    if role in FLEET_ONLY_ROLES and scope_kind != "fleet":
        logger.warning("ignoring non-fleet grant of fleet-only role %s", role)
        return False
    domain = perm_domain(perm)
    if domain == "fleet":
        return scope_kind == "fleet"
    if domain == "delegate":
        return True
    if domain == "prefix":
        if scope_kind == "fleet":
            return True
        if scope_kind != "prefix" or not scope_value:
            return False
        if prefix is None:
            return True
        return prefix == scope_value or prefix.startswith(scope_value + "/")
    # Minion domain.
    if scope_kind == "fleet":
        return True
    if scope_kind == "prefix":
        return False
    if minion is None:
        # Some scope holds the permission; per-id checks still call
        # require(perm, minion=mid) before Salt.
        return True
    return minion in _resolve_scope(scope_kind, scope_value)


def authorize(
    user, perm: str, *, minion: str | None = None, prefix: str | None = None
) -> bool:
    """Fleet, minion, or prefix check over the caller's effective grants.
    Memoized on ``flask.g`` so a page rendering many rows queries once."""
    if perm not in _ALL_PERMS:
        return False
    cache = None
    if has_app_context():
        cache = g.get("authz_cache")
        if cache is None:
            cache = g.authz_cache = {}
        key = (getattr(user, "id", None), perm, minion, prefix)
        if key in cache:
            return cache[key]
    result = any(
        _entry_covers(role, kind, value, perm, minion, prefix)
        for role, kind, value in _effective_entries(user)
    )
    if cache is not None:
        cache[key] = result
    return result


def has_fleet(user, perm: str) -> bool:
    """A fleet-scope grant for this permission: Salt targets (grain,
    compound, nodegroup, ``*``) keep today's meaning, including minions
    not yet in the snapshot."""
    if perm not in _ALL_PERMS:
        return False
    # A fleet scope cannot violate the fleet-only invariant, so no role
    # check is needed here beyond membership in the catalog.
    return any(
        role in ROLE_PERMS
        and perm in ROLE_PERMS[role]
        and scope_kind == "fleet"
        for role, scope_kind, _ in _effective_entries(user)
    )


def minions_with(user, perm: str) -> set[str]:
    """Snapshot minion ids the caller holds ``perm`` on. Fleet grants
    expand to the current snapshot; use ``has_fleet`` first when Salt's
    own target semantics (including not-yet-seen minions) are needed."""
    ids: set[str] = set()
    for role, scope_kind, scope_value in _effective_entries(user):
        perms = ROLE_PERMS.get(role)
        if perms is None or perm not in perms:
            continue
        if role in FLEET_ONLY_ROLES and scope_kind != "fleet":
            continue
        domain = perm_domain(perm)
        if domain != "minion":
            continue
        if scope_kind == "fleet":
            ids |= snapshot_ids()
        elif scope_kind != "prefix":
            ids |= _resolve_scope(scope_kind, scope_value)
    return ids


def constrain_target_verbose(
    user, perm: str, tgt: str, tgt_type: str
) -> tuple[str, str, set[str] | None, set[str] | None]:
    """Constrain a target, also returning (requested, hit).

    Fleet grants return the target unchanged with ``(None, None)``: Salt
    semantics (including not-yet-seen minions) are kept and there is no
    narrowing to audit. Otherwise the request is resolved against the
    snapshot, intersected with the caller's minion set for ``perm``, and
    published as a list; the literal ``*`` is never published for a
    scoped caller. Empty intersections and unevaluable types raise
    ``AuthzDenied``.
    """
    if has_fleet(user, perm):
        return tgt, tgt_type, None, None
    allowed = minions_with(user, perm)
    roster = snapshot_ids()
    if tgt_type == "list":
        requested = {p.strip() for p in tgt.split(",") if p.strip()} & roster
    elif tgt_type == "glob":
        requested = {m for m in roster if fnmatch.fnmatchcase(m, tgt)}
    elif tgt_type == "group":
        requested = job_group_members(tgt) & roster
    elif tgt_type == "grain":
        requested = {
            mid
            for mid, grains in snapshot_grains().items()
            if match_grain(grains, tgt)
        }
    elif tgt_type in ("compound", "nodegroup"):
        raise AuthzDenied(perm, "target type needs a fleet grant")
    else:
        raise AuthzDenied(perm, "unknown target type")
    hit = requested & allowed
    if not hit:
        raise AuthzDenied(perm, "empty intersection")
    return ",".join(sorted(hit)), "list", requested, hit


def constrain_target(user, perm: str, tgt: str, tgt_type: str) -> tuple[str, str]:
    """Return the target to publish. See ``constrain_target_verbose``."""
    tgt, tgt_type, _, _ = constrain_target_verbose(user, perm, tgt, tgt_type)
    return tgt, tgt_type


def constrain_kill(user, stored_tgt: str, stored_tgt_type: str) -> tuple[str, str]:
    """Kill rule: ``jobs.kill`` calls ``client.local`` directly, not via
    ``launch``. Fleet ``job.kill`` republishes the stored target; stored
    compound, nodegroup, or grain targets are 403 without fleet
    ``job.kill`` (never kill a subset of someone else's compound); list,
    glob, or group targets must resolve entirely inside scope."""
    if has_fleet(user, "job.kill"):
        return stored_tgt, stored_tgt_type
    if stored_tgt_type in ("compound", "nodegroup", "grain"):
        raise AuthzDenied("job.kill", "target type needs a fleet grant")
    if stored_tgt_type == "glob" and stored_tgt.strip() == "*":
        # Salt's * also names minions the snapshot does not have, so a
        # stored * is never inside a non-fleet scope — even when the
        # caller's grant covers every snapshot id today.
        raise AuthzDenied("job.kill", "target type needs a fleet grant")
    roster = snapshot_ids()
    if stored_tgt_type == "list":
        resolved = {p.strip() for p in stored_tgt.split(",") if p.strip()} & roster
    elif stored_tgt_type == "glob":
        resolved = {m for m in roster if fnmatch.fnmatchcase(m, stored_tgt)}
    elif stored_tgt_type == "group":
        resolved = job_group_members(stored_tgt) & roster
    else:
        raise AuthzDenied("job.kill", "unknown target type")
    allowed = minions_with(user, "job.kill")
    if not resolved or not resolved <= allowed:
        raise AuthzDenied("job.kill", "out of scope")
    return ",".join(sorted(resolved)), "list"


def function_class(fun: str) -> str | None:
    """Job-run class of a function: read, change, state, orchestrate."""
    if fun in _READ_CLASS:
        return "read"
    if fun in _CHANGE_CLASS:
        return "change"
    if fun in _STATE_CLASS:
        return "state"
    if fun == "state.orchestrate":
        return "orchestrate"
    return None


def publish_perm(fun: str) -> str | None:
    """Permission whose scope constrains a ``launch`` publish of ``fun``.

    Execution classes map to ``job.run.*``; pillar and mine documents
    map to their read permissions; beacon and schedule writes map to
    their write permissions. ``None`` is fail-closed: scoped mode must
    not publish a function with no permission class.
    """
    cls = function_class(fun)
    if cls in ("read", "change", "state"):
        return f"job.run.{cls}"
    if fun in ("pillar.items", "grains.items", "state.show_highstate"):
        return "pillar.read"
    if fun == "mine.get":
        return "mine.read"
    if fun in ("beacons.enable_beacon", "beacons.disable_beacon"):
        return "beacon.write"
    if fun == "saltutil.kill_job":
        return "job.kill"
    if fun == "schedule.add" or (
        fun.startswith("schedule.") and fun in ALLOWED_FUNS
    ):
        return "schedule.write"
    return None


def can_see_return_body(user, fun: str, minion_id: str) -> bool:
    """Whether the full stored return body for ``fun`` on ``minion_id``
    may render. State results need ``job.run.state``; pillar documents,
    mine, highstate, and orchestrate bodies need ``pillar.read`` /
    ``mine.read`` (orchestrate needs fleet ``pillar.read``); anything
    else needs ``job.read``. Everyone else sees metadata only. Never
    consult ``describe_return``: it classifies payload shape, including
    ``pillar.items`` and ``mine.get`` dicts as ``kind=unknown``."""
    if rbac_mode() != "scoped":
        return True
    if fun in ("state.apply", "state.highstate", "state.show_sls"):
        return authorize(user, "job.run.state", minion=minion_id)
    if fun in ("state.show_highstate", "pillar.items", "grains.items", "beacons.list"):
        return authorize(user, "pillar.read", minion=minion_id)
    if fun == "state.orchestrate":
        return has_fleet(user, "pillar.read")
    if fun == "mine.get":
        return authorize(user, "mine.read", minion=minion_id)
    return authorize(user, "job.read", minion=minion_id)


def runnable_ids(user) -> set[str]:
    """Snapshot ids the caller may target with an interactive fire: the
    union over the job-run classes. Used for scoped rosters and the run
    form so ``suggest_glob`` never offers a fleet ``*``."""
    ids: set[str] = set()
    for perm in ("job.run.read", "job.run.change", "job.run.state"):
        ids |= minions_with(user, perm)
    return ids


def has_any_access(user) -> bool:
    """Any effective grant, mapping, or fallback role: False only for a
    true zero-grant principal (the empty-dashboard case)."""
    return bool(_effective_entries(user))


def perm_scope_minions(user, perm: str) -> set[str]:
    """Snapshot ids inside scopes of grants carrying ``perm``.

    Unlike ``minions_with`` this works for delegate-domain permissions
    (``user.read``, ``audit.read``), which carry a minion scope but no
    per-minion check. The filtered Users page and the scoped audit
    filter are drawn from this set. Fleet grants expand to the snapshot.
    """
    ids: set[str] = set()
    for role, scope_kind, scope_value in _effective_entries(user):
        perms = ROLE_PERMS.get(role)
        if perms is None or perm not in perms:
            continue
        if role in FLEET_ONLY_ROLES and scope_kind != "fleet":
            continue
        if scope_kind == "fleet":
            ids |= snapshot_ids()
        elif scope_kind != "prefix":
            ids |= _resolve_scope(scope_kind, scope_value)
    return ids & snapshot_ids()


#: Scope kinds a delegate may write grants on. Fleet would carry
#: fleet-domain permissions; prefix scopes resolve to paths, not minion
#: sets, so neither can satisfy "resolved minion set is a subset of S".
_DELEGATE_SCOPE_KINDS = frozenset({"group", "list", "glob", "grain"})


def delegate_grant_error(
    user, role: str, scope_kind: str, scope_value: str
) -> str | None:
    """Subset check for ``grant.delegate`` writers. None when the grant
    is delegable: a minion-matcher scope inside the delegator's
    ``grant.delegate`` scope, carrying only permissions the delegator
    holds on those minions. ``grant.admin`` is never delegable, and
    fleet-domain permissions are dropped off-fleet before the compare,
    so a lead cannot appoint what they do not hold (the secrets-reader
    brake: no ``pillar.read`` + ``mine.read`` on the scope, no
    secrets-reader grant). Self-edit and backfill-ladder rules live in
    the views, next to the subject they can see.
    """
    if scope_kind not in _DELEGATE_SCOPE_KINDS:
        return "Delegated grants need a minion scope, not fleet or a path."
    perms = ROLE_PERMS.get(role)
    if perms is None or role in FLEET_ONLY_ROLES:
        return f"Role '{role}' cannot be delegated."
    if "grant.admin" in perms:
        return "grant.admin cannot be delegated."
    wanted = set(perms) - _FLEET_DOMAIN_PERMS - _PREFIX_DOMAIN_PERMS
    resolved = _resolve_scope(scope_kind, scope_value) & snapshot_ids()
    if not resolved <= minions_with(user, "grant.delegate"):
        return "That scope reaches outside your delegated scope."
    for perm in sorted(wanted):
        if perm in _DELEGATE_PERMS:
            # Delegate-domain permissions carry a minion scope but no
            # per-minion check, so authorize() cannot prove coverage;
            # compare the scope union instead.
            held = resolved <= perm_scope_minions(user, perm)
        else:
            held = resolved <= minions_with(user, perm)
        if not held:
            return f"You do not hold '{perm}' on that scope."
    return None


def scope_inside(scope_kind: str, scope_value: str, ids) -> bool:
    """Whether a grant scope resolves inside a minion set. Fleet and
    prefix scopes are never inside a minion set: fleet would carry
    fleet-domain permissions, and a path is not a minion set."""
    if scope_kind in ("fleet", "prefix"):
        return False
    return _resolve_scope(scope_kind, scope_value) <= set(ids)


def delegate_group_error(user, group_id: int) -> str | None:
    """None when a delegate may edit membership of (or delete) a local
    group: every grant on it is a minion-matcher scope inside the
    delegator's ``grant.delegate`` scope. Any fleet or prefix grant
    vetoes, as does any grant reaching outside that scope. An empty
    group is manageable: there is no access to widen.
    """
    within = minions_with(user, "grant.delegate")
    rows = (
        get_session()
        .query(Grant)
        .filter_by(subject_kind="local_group", subject_group_id=group_id)
        .all()
    )
    for grant in rows:
        if grant.scope_kind not in _DELEGATE_SCOPE_KINDS:
            return "That group holds a grant outside delegated scopes."
        if not _resolve_scope(grant.scope_kind, grant.scope_value) <= within:
            return "That group holds a grant outside your delegated scope."
    return None


def published_snapshot_ids(tgt: str | None, tgt_type: str | None) -> set[str] | None:
    """Snapshot ids a stored target names, or None when the type cannot
    be evaluated locally (compound, nodegroup, runner, unknown)."""
    if tgt_type == "list":
        return {p.strip() for p in (tgt or "").split(",") if p.strip()} & snapshot_ids()
    if tgt_type == "glob":
        return {m for m in snapshot_ids() if fnmatch.fnmatchcase(m, tgt or "")}
    if tgt_type == "group":
        return job_group_members(tgt or "") & snapshot_ids()
    if tgt_type == "grain":
        return {
            mid
            for mid, grains in snapshot_grains().items()
            if match_grain(grains, tgt or "")
        }
    return None


def own_group_ids(user) -> set[str]:
    """Group ids named by the caller's own grant or mapping scopes."""
    return {
        scope_value
        for _, scope_kind, scope_value in _grant_entries(user)
        if scope_kind == "group"
    }


def single_group_scope(user, perms: tuple[str, ...]) -> str | None:
    """Name of the caller's only minion-group scope for ``perms``, for
    prefilling the run form. ``None`` unless exactly one group scope
    applies, so the target otherwise starts empty and never ``*``."""
    from .models import MinionGroup

    group_ids: set[str] = set()
    for role, scope_kind, scope_value in _effective_entries(user):
        if scope_kind != "group":
            continue
        role_perms = ROLE_PERMS.get(role, frozenset())
        if role_perms & set(perms):
            group_ids.add(scope_value)
    if len(group_ids) != 1:
        return None
    try:
        group = get_session().get(MinionGroup, int(next(iter(group_ids))))
    except (TypeError, ValueError):
        return None
    return group.name if group is not None else None


def prefix_visible(prefixes: list[str], rel: str) -> bool:
    """Whether relative path ``rel`` sits under any granted prefix."""
    return any(rel == p or rel.startswith(p + "/") for p in prefixes)


def visible_prefixes(user) -> list[str] | None:
    """Granted file prefixes, or None for the whole tree (a fleet
    ``file.read`` / ``file.write`` grant). Empty means no file access."""
    for perm in ("file.read", "file.write"):
        if has_fleet(user, perm):
            return None
    prefixes: set[str] = set()
    for role, scope_kind, scope_value in _effective_entries(user):
        if scope_kind != "prefix" or not scope_value:
            continue
        if ROLE_PERMS.get(role, frozenset()) & {"file.read", "file.write"}:
            prefixes.add(scope_value)
    return sorted(prefixes)


def may_define_schedule_fleet(user, fun: str | None) -> bool:
    """Fleet half of ``may_define_schedule``: ``schedule.write`` on fleet
    plus the inner function class satisfied fleet-wide. A fleet target
    publishes unchanged, so per-snapshot-id checks have nothing to
    iterate; this is their fleet-wide equivalent."""
    if not fun or fun not in ALLOWED_FUNS:
        return False
    if not has_fleet(user, "schedule.write"):
        return False
    cls = function_class(fun)
    if cls in ("read", "change", "state"):
        return has_fleet(user, f"job.run.{cls}") or has_fleet(
            user, f"schedule.allow.{cls}"
        )
    if cls == "orchestrate":
        return has_fleet(user, "job.run.orchestrate")
    return False


def may_define_schedule(user, mid: str, fun: str) -> bool:
    """Define-time check for minion schedules. ``schedule.write`` on the
    minion, ``fun`` on the allowlist, and either the matching
    ``job.run.<class>`` or the matching ``schedule.allow.*``.
    ``schedule.allow.*`` never authorizes ``jobs.run`` or the console."""
    if fun not in ALLOWED_FUNS:
        return False
    if not authorize(user, "schedule.write", minion=mid):
        return False
    cls = function_class(fun)
    if cls in ("read", "change", "state"):
        return authorize(user, f"job.run.{cls}", minion=mid) or authorize(
            user, f"schedule.allow.{cls}", minion=mid
        )
    if cls == "orchestrate":
        return authorize(user, "job.run.orchestrate")
    return False


def _split_group_setting(raw: str | None) -> list[str]:
    return [g.strip() for g in (raw or "").split(",") if g.strip()]


def backfill_rbac(session, admin_groups: str = "", operator_groups: str = "") -> dict:
    """Idempotent backfill of fleet grants and IdP mappings.

    Every user whose ``role`` column is a ladder rung gets that fleet
    grant (``source=backfill``) unless one already exists; unknown role
    strings are skipped, never invented. Each comma-separated OIDC group
    becomes a fleet ladder mapping (``origin=backfill``). No settings
    rows are inserted. Flushes without committing; the caller owns the
    transaction.
    """
    counts = {"grants": 0, "mappings": 0}
    from .models import User

    for user in session.query(User).all():
        if user.role not in LADDER_ROLES:
            continue
        exists = (
            session.query(Grant)
            .filter_by(
                subject_kind="user",
                subject_user_id=user.id,
                role=user.role,
                scope_kind="fleet",
                scope_value="*",
            )
            .first()
        )
        if exists is None:
            session.add(
                Grant(
                    subject_kind="user",
                    subject_user_id=user.id,
                    subject_group_id=None,
                    role=user.role,
                    scope_kind="fleet",
                    scope_value="*",
                    source="backfill",
                    created_by=None,
                )
            )
            counts["grants"] += 1
    for group_name, role in [
        (grp, "admin") for grp in _split_group_setting(admin_groups)
    ] + [(grp, "operator") for grp in _split_group_setting(operator_groups)]:
        exists = (
            session.query(IdpRoleMapping)
            .filter_by(
                idp_group=group_name,
                role=role,
                scope_kind="fleet",
                scope_value="*",
            )
            .first()
        )
        if exists is None:
            session.add(
                IdpRoleMapping(
                    idp_group=group_name,
                    role=role,
                    scope_kind="fleet",
                    scope_value="*",
                    origin="backfill",
                )
            )
            counts["mappings"] += 1
    session.flush()
    return counts


def backfill_from_settings() -> dict:
    """App-context wrapper: group lists come from the OIDC settings, so
    editing those settings while legacy and then flipping the flag
    cannot authorize a stale list."""
    from .settings import get_setting

    counts = backfill_rbac(
        get_session(),
        admin_groups=get_setting("oidc_admin_groups"),
        operator_groups=get_setting("oidc_operator_groups"),
    )
    get_session().commit()
    return counts


def audit_deny(
    perm: str,
    minion_id: str | None = None,
    detail: str = "no-grant",
    actor: str | None = None,
) -> None:
    """Record a denied attempt. Denies go to audit_events, not the app log.

    The explicit actor name wins when given (service-account API fires
    evaluate as the token owner); otherwise the current login name, with
    ``anonymous`` outside a request context.
    """
    from flask_login import current_user

    from .audit import log_event

    username = actor
    if not username:
        try:
            username = current_user.username
        except Exception:
            username = "anonymous"
    log_event(
        username, "deny", outcome="deny", permission=perm, minion_id=minion_id, detail=detail
    )


def require(
    perm: str,
    *,
    minion: str | None = None,
    prefix: str | None = None,
    detail: str = "no-grant",
) -> None:
    """Abort 403 unless the current user holds ``perm`` on the scope.
    No-op while ``rbac_mode()`` is legacy."""
    from flask import abort
    from flask_login import current_user

    if rbac_mode() != "scoped":
        return
    if authorize(current_user, perm, minion=minion, prefix=prefix):
        return
    audit_deny(perm, minion_id=minion, detail=detail)
    abort(403)


def read_required(perm: str):
    """Decorator for read routes that are bare ``login_required`` in
    legacy mode: login-only there, ``perm``-gated when scoped."""
    import functools

    from flask_login import login_required

    def decorator(view):
        @functools.wraps(view)
        @login_required
        def guarded(*args, **kwargs):
            require(perm)
            return view(*args, **kwargs)

        return guarded

    return decorator


def permission_required(*perms: str):
    """Decorator: login, then any listed perm on any scope. Views that
    take a minion id still call ``require(perm, minion=mid)`` in the body.
    Legacy mode keeps the admin ladder for these (new) surfaces; the old
    routes keep ``roles_required``."""
    import functools

    from flask import abort
    from flask_login import current_user, login_required

    from .auth import LEVELS, role_level

    def decorator(view):
        @functools.wraps(view)
        @login_required
        def guarded(*args, **kwargs):
            if rbac_mode() != "scoped":
                if role_level(getattr(current_user, "role", None)) < LEVELS["admin"]:
                    abort(403)
                return view(*args, **kwargs)
            if not any(authorize(current_user, p) for p in perms):
                audit_deny(perms[0] if perms else "no-grant")
                abort(403)
            return view(*args, **kwargs)

        return guarded

    return decorator


def refresh_role_cache(user) -> str:
    """Recompute the ``users.role`` compatibility cache from grants and
    mappings: fleet ladder admin, else operator, else viewer, else
    'scoped' when any other grant or mapping applies, else 'none'."""
    from flask import g, has_app_context

    entries = _grant_entries(user)

    def _ladder(role: str) -> bool:
        return any(r == role and kind == "fleet" for r, kind, _ in entries)

    if _ladder("admin"):
        cached = "admin"
    elif _ladder("operator"):
        cached = "operator"
    elif _ladder("viewer"):
        cached = "viewer"
    elif entries:
        cached = "scoped"
    else:
        cached = "none"
    user.role = cached
    get_session().commit()
    if has_app_context():
        # Grants changed under this context: drop memoized entries and
        # decisions so later checks in the same request see fresh rows.
        # Request handlers redirect after writes, so this mostly helps
        # tests and the entering-scoped rebuild loop.
        cache = g.get("grant_entries")
        if isinstance(cache, dict):
            cache.pop(getattr(user, "id", None), None)
        if isinstance(g.get("authz_cache"), dict):
            g.authz_cache = {}
    return cached
