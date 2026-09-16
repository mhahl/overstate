"""Master Config: own the salt-master ConfigMap from the browser.

Admin-only. Lists the data keys of the owned ConfigMap, edits them in the
vendored CodeMirror bundle (textarea fallback), and saves through the
in-cluster Kubernetes client: the whole ConfigMap snapshots to the history
ConfigMap first, then the live object PUT-replaces with the base
``resourceVersion`` the form was read at. A raced save refuses with the
current revision and writes nothing. Invalid YAML is blocked, never
advisory — a broken master config rolls out to both masters.

Restarting onto saved config is always an explicit second click (the
restart route); saving never rolls the masters by itself.
"""

import datetime
import json
import time

import yaml
from flask import (
    Blueprint,
    abort,
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
from .files import MAX_BYTES
from .k8s import K8sClient, K8sConflictError, K8sError, K8sUnavailableError

bp = Blueprint("masterconfig", __name__, url_prefix="/settings/master")

# Forwarding address for the pre-move /master-config/* URLs (bookmarks).
legacy_bp = Blueprint("masterconfig_legacy", __name__)


@legacy_bp.route("/master-config/")
@legacy_bp.route("/master-config/<path:subpath>")
@login_required
def legacy_redirect(subpath: str = ""):
    target = f"/settings/master/{subpath}"
    if request.query_string:
        target += "?" + request.query_string.decode("ascii", "replace")
    return redirect(target)


HISTORY_KEY = "history.json"
HISTORY_LIMIT = 20
# Bounded restart wait: polls * interval ~= 180s in production; tests
# shrink both to run the timeout path without waiting.
RESTART_MAX_POLLS = 36
POLL_INTERVAL_S = 5
YAML_SUFFIXES = (".conf", ".yaml", ".yml")
# Editing auth config can lock the UI out of salt-api; recovery is
# revert + restart over kubectl, so the banner restates that at the
# point of action.
AUTH_ADJACENT = {"api.conf"}


def _names() -> tuple[str, str]:
    return (
        current_app.config["MASTER_CONFIGMAP"],
        current_app.config["MASTER_CONFIG_HISTORY"],
    )


def _manual(namespace: str, name: str) -> str:
    return f"kubectl -n {namespace} edit configmap {name}"


def _yaml_error(key: str, text: str) -> str | None:
    """Blocking validation for config keys. None means acceptable."""
    if not key.endswith(YAML_SUFFIXES):
        return None
    try:
        parsed = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        return f"Invalid YAML: {str(exc)[:300]}"
    if parsed is not None and not isinstance(parsed, dict):
        return "Config files must be a top-level mapping."
    return None


def _read_history(client: K8sClient, history_name: str) -> tuple[list, str]:
    """(revisions, resourceVersion); corrupt history refuses loudly."""
    try:
        current = client.get_configmap(history_name)
    except K8sError as exc:
        # A missing history ConfigMap self-heals on first save.
        if "NotFound" in str(exc) or "404" in str(exc):
            return [], ""
        raise
    raw = (current["data"] or {}).get(HISTORY_KEY, "[]")
    try:
        revisions = json.loads(raw)
    except ValueError as exc:
        raise K8sError(f"history {HISTORY_KEY} is unreadable: {exc}")
    if not isinstance(revisions, list):
        raise K8sError(f"history {HISTORY_KEY} is unreadable: not a list")
    return revisions, current["resourceVersion"]


def _snapshot(
    client: K8sClient,
    live_name: str,
    history_name: str,
    live_data: dict,
    live_rv: str,
) -> None:
    """Snapshot the whole live ConfigMap into history (cap enforced).

    Retries once on a history race; a second conflict fails loudly with
    the live object untouched.
    """
    revisions, history_rv = _read_history(client, history_name)
    entry = {
        "ts": datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds"),
        "user": current_user.username,
        "resourceVersion": live_rv,
        "data": live_data,
    }
    revisions.append(entry)
    payload = {HISTORY_KEY: json.dumps(revisions[-HISTORY_LIMIT:])}
    try:
        client.replace_configmap(history_name, payload, history_rv)
    except K8sConflictError:
        revisions, history_rv = _read_history(client, history_name)
        revisions.append(entry)
        payload = {HISTORY_KEY: json.dumps(revisions[-HISTORY_LIMIT:])}
        client.replace_configmap(history_name, payload, history_rv)


def _load_live(client: K8sClient, live_name: str) -> tuple[dict, str] | None:
    try:
        current = client.get_configmap(live_name)
    except K8sUnavailableError:
        flash(
            "No cluster connection from here. "
            f"Edit by hand instead: {_manual(client.config.namespace, live_name)}.",
            "error",
        )
        return None
    except K8sError as exc:
        flash(f"Could not read the live config: {exc}. Nothing changed.", "error")
        return None
    return current["data"] or {}, current["resourceVersion"]


@bp.route("/")
@roles_required("admin")
def index():
    client = K8sClient()
    live_name, history_name = _names()
    loaded = _load_live(client, live_name)
    if loaded is None:
        return render_template(
            "masterconfig.html",
            rows=None,
            manual=_manual(client.config.namespace, live_name),
            snapshots=None,
        )
    data, revision = loaded
    rows = [
        {
            "key": key,
            "size": len(value.encode("utf-8")),
            "yaml": key.endswith(YAML_SUFFIXES),
            "auth": key in AUTH_ADJACENT,
        }
        for key, value in sorted(data.items())
    ]
    return render_template(
        "masterconfig.html",
        rows=rows,
        revision=revision,
        manual=None,
        snapshots=_history_count(client, history_name),
    )


@bp.route("/view")
@roles_required("admin")
def view():
    key = request.args.get("key", "")
    client = K8sClient()
    live_name, _ = _names()
    loaded = _load_live(client, live_name)
    if loaded is None:
        return redirect(url_for("masterconfig.index"))
    data, revision = loaded
    if key not in data:
        abort(404)
    return render_template(
        "masterconfig_view.html",
        key=key,
        content=data[key],
        size=len(data[key].encode("utf-8")),
        revision=revision,
        auth=key in AUTH_ADJACENT,
    )


@bp.route("/edit")
@roles_required("admin")
def edit():
    key = request.args.get("key", "")
    client = K8sClient()
    live_name, _ = _names()
    loaded = _load_live(client, live_name)
    if loaded is None:
        return redirect(url_for("masterconfig.index"))
    data, revision = loaded
    if key not in data:
        abort(404)
    content = data[key]
    if len(content.encode("utf-8")) > MAX_BYTES:
        flash("That key is too large to edit here. Nothing changed.", "error")
        return redirect(url_for("masterconfig.view", key=key))
    return render_template(
        "masterconfig_edit.html",
        key=key,
        content=content,
        lang="yaml" if key.endswith(YAML_SUFFIXES) else "text",
        base_resource_version=revision,
        auth=key in AUTH_ADJACENT,
    )


@bp.post("/save")
@roles_required("admin")
def save():
    key = request.form.get("key", "")
    text = request.form.get("content", "")
    base_rv = request.form.get("base_resource_version", "")
    client = K8sClient()
    live_name, history_name = _names()
    try:
        current = client.get_configmap(live_name)
    except K8sUnavailableError:
        flash(
            "No cluster connection from here. "
            f"Edit by hand instead: {_manual(client.config.namespace, live_name)}.",
            "error",
        )
        log_event(current_user.username, f"masterconfig-save-refused:{key}:offline")
        return redirect(url_for("masterconfig.index"))
    except K8sError as exc:
        flash(f"Could not read the live config: {exc}. Nothing changed.", "error")
        log_event(current_user.username, f"masterconfig-save-refused:{key}:read")
        return redirect(url_for("masterconfig.index"))
    data, revision = current["data"] or {}, current["resourceVersion"]
    if key not in data:
        abort(404)
    raw = text.encode("utf-8")
    if len(raw) > MAX_BYTES:
        flash(
            f"Too large to save ({len(raw)} bytes; limit is {MAX_BYTES}). "
            "Nothing changed.",
            "error",
        )
        log_event(current_user.username, f"masterconfig-save-refused:{key}:oversize")
        return redirect(url_for("masterconfig.view", key=key))
    if text == data[key]:
        flash("No changes — nothing saved.", "info")
        return redirect(url_for("masterconfig.view", key=key))
    problem = _yaml_error(key, text)
    if problem is not None:
        log_event(current_user.username, f"masterconfig-save-refused:{key}:invalid")
        return render_template(
            "masterconfig_edit.html",
            key=key,
            content=text,
            lang="yaml" if key.endswith(YAML_SUFFIXES) else "text",
            base_resource_version=base_rv,
            auth=key in AUTH_ADJACENT,
            error=problem,
        )
    try:
        _snapshot(client, live_name, history_name, data, revision)
    except K8sError as exc:
        flash(f"Could not snapshot history: {exc}. Nothing changed.", "error")
        log_event(current_user.username, f"masterconfig-save-refused:{key}:history")
        return redirect(url_for("masterconfig.view", key=key))
    updated = dict(data)
    updated[key] = text
    try:
        new_rv = client.replace_configmap(live_name, updated, base_rv)
    except K8sConflictError:
        flash(
            "That config changed underneath you. Reload the edit page and "
            "re-apply your change. Nothing was written.",
            "error",
        )
        log_event(current_user.username, f"masterconfig-save-refused:{key}:stale")
        return redirect(url_for("masterconfig.edit", key=key))
    except K8sError as exc:
        flash(f"Could not write the live config: {exc}. Nothing changed.", "error")
        log_event(current_user.username, f"masterconfig-save-refused:{key}:write")
        return redirect(url_for("masterconfig.view", key=key))
    flash(
        f"Saved {key} (revision {new_rv}). Restart the master to apply it.",
        "success",
    )
    log_event(current_user.username, f"masterconfig-save:{key}:{new_rv}")
    return redirect(url_for("masterconfig.view", key=key))


def _salt_api_healthy() -> bool:
    """True when salt-api answers a fresh login. Never raises."""
    try:
        current_app.extensions["salt_client"].login(http_timeout=15.0)
    except Exception:  # noqa: BLE001 — any failure means unhealthy
        return False
    return True


def _rollout_converged(client: K8sClient, sts_name: str) -> bool:
    try:
        snap = client.statefulset_rollout(sts_name)
    except K8sError:
        return False
    if snap["observedGeneration"] != snap["generation"]:
        return False
    replicas = snap["replicas"] or 1
    return snap["readyReplicas"] == replicas and snap["updatedReplicas"] == replicas


def _wait_rollout(client: K8sClient, sts_name: str) -> bool:
    for _ in range(RESTART_MAX_POLLS):
        if _rollout_converged(client, sts_name):
            return True
        time.sleep(POLL_INTERVAL_S)
    return _rollout_converged(client, sts_name)


def _roll_masters(client: K8sClient, sts_name: str) -> tuple[bool, str]:
    """Stamp the restart and wait for a healthy rollout.

    Returns (healthy, reason). A False never means "half done silently":
    the caller flashes and audits the reason.
    """
    try:
        client.restart_statefulset(sts_name)
    except K8sError as exc:
        return False, f"could not trigger the restart: {exc}"
    if not _wait_rollout(client, sts_name):
        return False, "the rollout did not finish in time"
    if not _salt_api_healthy():
        return False, "salt-api did not come back healthy"
    return True, ""


def _history_count(client: K8sClient, history_name: str) -> int | None:
    try:
        revisions, _ = _read_history(client, history_name)
    except K8sError:
        return None
    return len(revisions)


@bp.post("/restart")
@roles_required("admin")
def restart():
    """Roll the masters one at a time and wait for health. Explicit only."""
    client = K8sClient()
    sts_name = current_app.config["MASTER_STATEFULSET"]
    namespace = client.config.namespace
    if not client.config.available:
        flash(
            "No cluster connection from here. Restart by hand: "
            f"kubectl -n {namespace} rollout restart statefulset/{sts_name}.",
            "error",
        )
        log_event(current_user.username, "master-restart:refused-offline")
        return redirect(url_for("masterconfig.index"))
    healthy, reason = _roll_masters(client, sts_name)
    if not healthy:
        flash(
            f"Master restart unhealthy: {reason}. The fleet may be unmanaged: "
            "revert to the last snapshot and restart from this page.",
            "error",
        )
        log_event(current_user.username, "master-restart:timeout")
        return redirect(url_for("masterconfig.index"))
    flash("Masters restarted and healthy.", "success")
    log_event(current_user.username, "master-restart:ok")
    return redirect(url_for("masterconfig.index"))


@bp.post("/revert")
@roles_required("admin")
def revert():
    """Re-patch the last snapshot (snapshotting current first), then roll.

    Revert is undoable: current state becomes the newest snapshot, so a
    bad revert reverts again.
    """
    client = K8sClient()
    live_name, history_name = _names()
    sts_name = current_app.config["MASTER_STATEFULSET"]
    if not client.config.available:
        flash(
            "No cluster connection from here. Revert by hand: "
            f"{_manual(client.config.namespace, live_name)}.",
            "error",
        )
        log_event(current_user.username, "masterconfig-revert:refused-offline")
        return redirect(url_for("masterconfig.index"))
    try:
        revisions, _ = _read_history(client, history_name)
    except K8sError as exc:
        flash(f"Could not read history: {exc}. Nothing changed.", "error")
        log_event(current_user.username, "masterconfig-revert:refused-history")
        return redirect(url_for("masterconfig.index"))
    if not revisions:
        flash("No snapshots yet — nothing to revert to.", "error")
        log_event(current_user.username, "masterconfig-revert:refused-empty")
        return redirect(url_for("masterconfig.index"))
    try:
        current = client.get_configmap(live_name)
    except K8sError as exc:
        flash(f"Could not read the live config: {exc}. Nothing changed.", "error")
        log_event(current_user.username, "masterconfig-revert:refused-read")
        return redirect(url_for("masterconfig.index"))
    data, revision = current["data"] or {}, current["resourceVersion"]
    try:
        _snapshot(client, live_name, history_name, data, revision)
    except K8sError as exc:
        flash(f"Could not snapshot history: {exc}. Nothing changed.", "error")
        log_event(current_user.username, "masterconfig-revert:refused-history")
        return redirect(url_for("masterconfig.index"))
    try:
        fresh = client.get_configmap(live_name)
        new_rv = client.replace_configmap(
            live_name, revisions[-1]["data"], fresh["resourceVersion"]
        )
    except K8sConflictError:
        flash(
            "That config changed underneath you. Reload and revert again. "
            "Nothing was written.",
            "error",
        )
        log_event(current_user.username, "masterconfig-revert:refused-stale")
        return redirect(url_for("masterconfig.index"))
    except K8sError as exc:
        flash(f"Could not write the live config: {exc}. Nothing changed.", "error")
        log_event(current_user.username, "masterconfig-revert:refused-write")
        return redirect(url_for("masterconfig.index"))
    healthy, reason = _roll_masters(client, sts_name)
    if not healthy:
        flash(
            f"Reverted to the previous snapshot (revision {new_rv}) but the "
            f"restart is unhealthy: {reason}. Check the masters over kubectl.",
            "error",
        )
        log_event(current_user.username, f"masterconfig-revert:{new_rv}:timeout")
        return redirect(url_for("masterconfig.index"))
    flash(f"Reverted and restarted healthy (revision {new_rv}).", "success")
    log_event(current_user.username, f"masterconfig-revert:{new_rv}")
    return redirect(url_for("masterconfig.index"))
