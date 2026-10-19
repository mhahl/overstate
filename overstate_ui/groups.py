"""Node groups: Salt nodegroups saved in master.conf.

Replaces the Postgres MinionGroup model. Nodegroups live in the
salt-master ConfigMap's ``master.conf`` as a ``nodegroups:`` stanza,
so they are usable in pillar top files, compound targets, and reactor
definitions. Editing writes the whole ConfigMap back through the same
conflict-handling path as Master Config saves. A master restart is
required for changes to take effect.
"""

import yaml
from flask import Blueprint, current_app, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from .audit import log_event
from .auth import roles_required
from .k8s import K8sClient, K8sConflictError, K8sError
from .settings import get_setting, set_setting

bp = Blueprint("groups", __name__, url_prefix="/groups")

RESTART_KEY = "restart_pending"

NODEGROUP_HELP = {
    "L@": "List: L@minion1,minion2",
    "G@": "Grain: G@os:Ubuntu",
    "P@": "Grain regex: P@os:(Centos|Redhat)",
    "S@": "Subnet: S@10.0.0.0/8",
    "I@": "Pillar: I@role:web",
    "C@": "Compound: G@os:Ubuntu and L@minion1",
}


def restart_needed() -> bool:
    """Whether a restart is pending for ConfigMap-based changes."""
    return get_setting(RESTART_KEY) == "1"


def set_restart_needed() -> None:
    """Mark that a master restart is needed."""
    set_setting(RESTART_KEY, "1")
    try:
        name = current_user.username
    except Exception:  # noqa: BLE001
        name = "system"
    log_event(name, "restart-pending:config-change")


def clear_restart_needed() -> None:
    """Clear the restart-needed flag (after successful restart)."""
    from .db import get_session
    from .models import Setting

    session = get_session()
    row = session.get(Setting, RESTART_KEY)
    if row is not None:
        session.delete(row)
        session.commit()
        try:
            name = current_user.username
        except Exception:  # noqa: BLE001
            name = "system"
        log_event(name, "restart-pending:cleared")


def _load_nodegroups() -> dict[str, str] | None:
    """Read ``nodegroups:`` from the live ConfigMap. None on failure."""
    name = current_app.config["MASTER_CONFIGMAP"]
    client = K8sClient()
    try:
        current = client.get_configmap(name)
    except K8sError:
        return None
    raw = (current["data"] or {}).get("master.conf", "")
    try:
        parsed = yaml.safe_load(raw)
    except yaml.YAMLError:
        return None
    if not isinstance(parsed, dict):
        return None
    ng = parsed.get("nodegroups", {})
    return dict(ng) if isinstance(ng, dict) else {}


def _write_nodegroups(groups: dict[str, str]) -> str | None:
    """Write ``nodegroups:`` into master.conf in the ConfigMap.

    Loads the live ConfigMap, replaces the nodegroups stanza, writes
    back (conflict-guarded). Returns None on success, or an error
    message.
    """
    name = current_app.config["MASTER_CONFIGMAP"]
    client = K8sClient()
    try:
        current = client.get_configmap(name)
    except K8sError as exc:
        return f"could not read master config: {exc}"
    data, revision = (current["data"] or {}), current["resourceVersion"]
    raw = data.get("master.conf", "")
    try:
        parsed = yaml.safe_load(raw) or {}
    except yaml.YAMLError:
        return "master.conf is not valid YAML"
    if not isinstance(parsed, dict):
        return "master.conf is not a mapping"
    # Build new master.conf with updated nodegroups.
    parsed["nodegroups"] = groups
    updated = data.copy()
    updated["master.conf"] = yaml.safe_dump(parsed, default_flow_style=False, allow_unicode=True)
    try:
        new_rv = client.replace_configmap(name, updated, revision)
    except K8sConflictError:
        return "the config changed since you opened it — reload and re-apply"
    except K8sError as exc:
        return f"the app could not write the live config: {exc}"
    set_restart_needed()
    return None


def _load_nodegroup_names() -> list[str]:
    """Nodegroup names for the job-form dropdown and palette, or empty."""
    ng = _load_nodegroups()
    return sorted(ng) if ng else []


def _parse_expression(text: str) -> tuple[str, str]:
    """Split an expression like ``L@minion1,minion2`` into (prefix, rest)."""
    text = (text or "").strip()
    for prefix in ("L@", "C@", "G@", "P@", "S@", "I@", "N@"):
        if text.startswith(prefix):
            return prefix, text[len(prefix):]
    return "", text


def _format_expression(prefix: str, value: str) -> str:
    return f"{prefix}{value}" if prefix else value


@bp.get("/", strict_slashes=False)
@login_required
def index():
    from .authz import require

    require("group.read")
    group_data: dict[str, str] | None = None
    try:
        group_data = _load_nodegroups()
        if group_data is None:
            error = "could not read nodegroups from master.conf"
    except Exception as exc:
        error = str(exc)
    return render_template(
        "groups.html",
        groups=dict(sorted(group_data.items())) if group_data else {},
        error=error,
        help_examples=NODEGROUP_HELP,
        restart_pending=restart_needed(),
    )


def _sync_to_minion_group(name: str, expression: str) -> None:
    """Create or delete a ``MinionGroup`` row from a nodegroup expression.

    ``L@`` expressions can be parsed into a member list for RBAC scope
    resolution (grants referencing group IDs). Other matchers are
    skipped — they cannot be resolved to specific minions by the UI.
    """
    from .db import get_session
    from .models import MinionGroup

    session = get_session()
    if expression.startswith("L@"):
        members = [m.strip() for m in expression[2:].split(",") if m.strip()]
        row = session.query(MinionGroup).filter_by(name=name).first()
        if row is not None:
            row.members = members
        else:
            session.add(MinionGroup(name=name, members=members))
        session.commit()
    else:
        # Non-L@ expressions: remove any stale MinionGroup row so
        # RBAC grants cannot reference an unresolvable group.
        row = session.query(MinionGroup).filter_by(name=name).first()
        if row is not None:
            session.delete(row)
            session.commit()


@bp.post("/", strict_slashes=False)
@roles_required("operator")
def save():
    from .authz import require

    require("group.write")
    name = (request.form.get("name") or "").strip()
    prefix = request.form.get("prefix", "L@").strip()
    value = (request.form.get("value") or "").strip()
    delete_name = request.form.get("delete", "")
    groups = _load_nodegroups()
    if groups is None:
        flash("Cannot read nodegroups from the master config.", "error")
        return redirect(url_for("groups.index"))
    if delete_name:
        if delete_name not in groups:
            flash(f"Node group '{delete_name}' does not exist.", "error")
            return redirect(url_for("groups.index"))
        del groups[delete_name]
        err = _write_nodegroups(groups)
        if err:
            flash(f"Delete failed: {err}", "error")
        else:
            _sync_to_minion_group(delete_name, "")
            log_event(current_user.username, f"nodegroup-delete:{delete_name}")
            flash(f"Node group '{delete_name}' deleted.", "success")
        return redirect(url_for("groups.index"))
    if not name:
        flash("Enter a group name.", "error")
        return redirect(url_for("groups.index"))
    if not value:
        flash("Enter an expression value.", "error")
        return redirect(url_for("groups.index"))
    expression = _format_expression(prefix, value)
    groups[name] = expression
    err = _write_nodegroups(groups)
    if err:
        flash(f"Save failed: {err}", "error")
    else:
        _sync_to_minion_group(name, expression)
        log_event(current_user.username, f"nodegroup-save:{name}")
        flash(
            f"Node group '{name}' saved as '{expression}'. "
            "Restart masters to activate it.",
            "success",
        )
    return redirect(url_for("groups.index"))