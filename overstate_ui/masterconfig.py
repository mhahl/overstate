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
import threading
import time

import yaml
from flask import (
    Blueprint,
    abort,
    current_app,
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
# Async restart status, polled by the page while a rollout is running.
# The wait moved off-request because it can outlast gunicorn's 90s
# worker timeout (a synchronous wait got the request killed and the
# browser a 502/504).
RESTART_STATE_KEY = "master_restart_state"
RESTART_ESTIMATE_S = 90
# A daemon thread can die with its gunicorn worker. Past this age a
# "running" row is treated as unknown so the UI never spins forever.
RESTART_STALE_S = 600
# Terminal outcomes are shown for this long, then the banner resets.
RESTART_NOTICE_S = 300
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
            "No cluster connection from this page. "
            f"Edit with kubectl instead: {_manual(client.config.namespace, live_name)}.",
            "error",
        )
        return None
    except K8sError as exc:
        flash(f"The app could not read the live config: {exc}. Nothing changed.", "error")
        return None
    return current["data"] or {}, current["resourceVersion"]


def _checklist() -> dict | None:
    from .tasks_queue import read_mastercheck_cache

    return read_mastercheck_cache()


def _index_context(client: K8sClient) -> dict:
    live_name, history_name = _names()
    restart = _restart_status_payload()
    loaded = _load_live(client, live_name)
    if loaded is None:
        return {
            "rows": None,
            "manual": _manual(client.config.namespace, live_name),
            "snapshots": None,
            "restart_state": restart,
            "namespace": client.config.namespace,
        }
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
    return {
        "rows": rows,
        "revision": revision,
        "manual": None,
        "snapshots": _history_count(client, history_name),
        "restart_state": restart,
        "namespace": client.config.namespace,
    }


@bp.route("/")
@roles_required("admin")
def index():
    from .authz import rbac_mode, require

    if rbac_mode() == "scoped":
        require("master.read")
    client = K8sClient()
    return render_template(
        "masterconfig.html", checklist=_checklist(), **_index_context(client)
    )


@bp.post("/checklist/refresh")
@roles_required("admin")
def checklist_refresh():
    from .authz import rbac_mode, require

    if rbac_mode() == "scoped":
        require("master.write")
    """Re-run the observability checklist: worker when one answers,
    inline fallback otherwise (D1+D5). The inline result renders
    immediately; the queued one lands in cache for the next load."""
    from .tasks import mastercheck_task, queue_or_none

    job = queue_or_none(mastercheck_task)
    if job is None:
        from .tasks_k8s import mastercheck_now
        from .tasks_queue import write_mastercheck_cache

        out = mastercheck_now()
        write_mastercheck_cache(out)
        flash("No worker answered. The app checked inline instead.", "info")
        log_event(current_user.username, "mastercheck:inline")
        return render_template(
            "masterconfig.html", checklist=out, **_index_context(K8sClient())
        )
    flash("The app queued the checklist. Refresh for results.", "info")
    log_event(current_user.username, "mastercheck:queued")
    return redirect(url_for("masterconfig.index"))


@bp.route("/view")
@roles_required("admin")
def view():
    from .authz import rbac_mode, require

    if rbac_mode() == "scoped":
        require("master.read")
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
    from .authz import rbac_mode, require

    if rbac_mode() == "scoped":
        require("master.write")
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
    from .authz import rbac_mode, require

    if rbac_mode() == "scoped":
        require("master.write")
    key = request.form.get("key", "")
    text = request.form.get("content", "")
    base_rv = request.form.get("base_resource_version", "")
    client = K8sClient()
    live_name, history_name = _names()
    try:
        current = client.get_configmap(live_name)
    except K8sUnavailableError:
        flash(
            "No cluster connection from this page. "
            f"Edit with kubectl instead: {_manual(client.config.namespace, live_name)}.",
            "error",
        )
        log_event(current_user.username, f"masterconfig-save-refused:{key}:offline")
        return redirect(url_for("masterconfig.index"))
    except K8sError as exc:
        flash(f"The app could not read the live config: {exc}. Nothing changed.", "error")
        log_event(current_user.username, f"masterconfig-save-refused:{key}:read")
        return redirect(url_for("masterconfig.index"))
    data, revision = current["data"] or {}, current["resourceVersion"]
    if key not in data:
        abort(404)
    raw = text.encode("utf-8")
    if len(raw) > MAX_BYTES:
        flash(
            f"This file is too large to save ({len(raw)} bytes; limit is {MAX_BYTES}). "
            "Nothing changed.",
            "error",
        )
        log_event(current_user.username, f"masterconfig-save-refused:{key}:oversize")
        return redirect(url_for("masterconfig.view", key=key))
    if text == data[key]:
        flash("No changes. Nothing saved.", "info")
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
        flash(f"The app could not snapshot history: {exc}. Nothing changed.", "error")
        log_event(current_user.username, f"masterconfig-save-refused:{key}:history")
        return redirect(url_for("masterconfig.view", key=key))
    updated = dict(data)
    updated[key] = text
    try:
        new_rv = client.replace_configmap(live_name, updated, base_rv)
    except K8sConflictError:
        flash(
            "That config changed since you opened it. Reload the edit page and "
            "re-apply your change. Nothing was written.",
            "error",
        )
        log_event(current_user.username, f"masterconfig-save-refused:{key}:stale")
        return redirect(url_for("masterconfig.edit", key=key))
    except K8sError as exc:
        flash(f"The app could not write the live config: {exc}. Nothing changed.", "error")
        log_event(current_user.username, f"masterconfig-save-refused:{key}:write")
        return redirect(url_for("masterconfig.view", key=key))
    flash(
        f"Saved {key} (revision {new_rv}). Restart the master to apply it.",
        "success",
    )
    from .groups import set_restart_needed

    set_restart_needed()
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


def _restart_state() -> dict:
    """Persisted restart outcome, shared by both app replicas."""
    from .settings import get_setting

    raw = get_setting(RESTART_STATE_KEY)
    try:
        state = json.loads(raw) if raw else {}
    except ValueError:
        state = {}
    return state if isinstance(state, dict) else {}


def _set_restart_state(**fields) -> None:
    from .settings import set_setting

    state = _restart_state()
    state.update(fields)
    set_setting(RESTART_STATE_KEY, json.dumps(state))


def _seconds_since(iso: str | None) -> int | None:
    if not iso:
        return None
    try:
        started = datetime.datetime.fromisoformat(iso)
    except ValueError:
        return None
    return int((datetime.datetime.now(datetime.UTC) - started).total_seconds())


def _restart_status_payload() -> dict:
    """The page's countdown/poll view of a master restart.

    ``in_progress`` drives the banner; ``remaining`` is an estimate the
    client counts down locally and refreshes on each poll. Terminal
    outcomes expire after ``RESTART_NOTICE_S`` so a stale "healthy"
    banner does not outlive the moment it described.
    """
    state = _restart_state()
    status = str(state.get("status") or "idle")
    elapsed = _seconds_since(state.get("started_at"))
    if status == "running" and (elapsed is None or elapsed > RESTART_STALE_S):
        # The thread died with its worker (or never started): never
        # leave the UI reconnecting forever.
        status = "unknown"
    if status in ("ok", "failed"):
        finished = _seconds_since(state.get("finished_at"))
        if finished is not None and finished > RESTART_NOTICE_S:
            status = "idle"
    remaining = (
        max(0, RESTART_ESTIMATE_S - (elapsed or 0)) if status == "running" else 0
    )
    return {
        "status": status,
        "in_progress": status == "running",
        "elapsed": elapsed or 0,
        "remaining": remaining,
        "reason": state.get("reason") or "",
    }


def _finish_restart(app, sts_name: str) -> None:
    """Wait for the rollout off-request, then publish the outcome.

    Runs on a daemon thread with its own app context (the request's
    context is gone by the time the rollout finishes). Writes the
    result to the settings row the page polls, so no flash is needed.
    """
    with app.app_context():
        client = K8sClient()
        converged = _wait_rollout(client, sts_name)
        reason = ""
        if not converged:
            reason = "the rollout did not finish in time"
        elif not _salt_api_healthy():
            reason = "salt-api did not come back healthy"
        _set_restart_state(
            status="ok" if converged and not reason else "failed",
            finished_at=datetime.datetime.now(datetime.UTC).isoformat(),
            reason=reason,
        )
        if converged and not reason:
            from .groups import clear_restart_needed

            clear_restart_needed()
            log_event("system", "master-restart:ok")
        else:
            log_event("system", f"master-restart:failed:{reason}")


def _spawn_restart(app, sts_name: str) -> None:
    """Run the wait on a daemon thread.

    The request must return well inside gunicorn's 90s timeout; the
    rollout itself can take longer (three pods, one at a time).
    """
    threading.Thread(target=_finish_restart, args=(app, sts_name), daemon=True).start()


def _start_restart(
    client: K8sClient, sts_name: str, actor: str, *, prefix: str = ""
) -> tuple[bool, str]:
    """Trigger the rollout and hand the wait to a background thread.

    Returns (started, error). ``started`` means the restart was
    stamped and the page should show the reconnecting banner; it does
    not mean the rollout has finished.
    """
    try:
        client.restart_statefulset(sts_name)
    except K8sError as exc:
        return False, f"could not trigger the restart: {exc}"
    _set_restart_state(
        status="running",
        started_at=datetime.datetime.now(datetime.UTC).isoformat(),
        finished_at=None,
        reason="",
    )
    log_event(actor, f"{prefix}master-restart:started")
    _spawn_restart(current_app._get_current_object(), sts_name)
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
    """Roll the masters one at a time in the background. Explicit only.

    The request returns as soon as the rollout is stamped; the wait and
    health check run off-request (see ``_start_restart``) so a slow
    three-pod roll cannot outlast gunicorn's worker timeout.
    """
    from .authz import rbac_mode, require

    if rbac_mode() == "scoped":
        require("master.rollout")
    client = K8sClient()
    sts_name = current_app.config["MASTER_STATEFULSET"]
    namespace = client.config.namespace
    if _restart_status_payload()["in_progress"]:
        flash(
            "A master restart is already running. This page updates when it finishes.",
            "info",
        )
        return redirect(url_for("masterconfig.index"))
    if not client.config.available:
        flash(
            "No cluster connection from this page. Restart with kubectl: "
            f"kubectl -n {namespace} rollout restart statefulset/{sts_name}.",
            "error",
        )
        log_event(current_user.username, "master-restart:refused-offline")
        return redirect(url_for("masterconfig.index"))
    started, reason = _start_restart(client, sts_name, current_user.username)
    if not started:
        flash(f"Master restart not started: {reason}.", "error")
        log_event(current_user.username, "master-restart:refused")
        return redirect(url_for("masterconfig.index"))
    flash(
        "Master restart started. The fleet reconnects in about a minute; "
        "this page updates when the masters are healthy.",
        "info",
    )
    return redirect(url_for("masterconfig.index"))


@bp.get("/restart/status")
@roles_required("admin")
def restart_status():
    """Polled by the master-config page while a rollout runs."""
    from .authz import rbac_mode, require

    if rbac_mode() == "scoped":
        require("master.read")
    return jsonify(_restart_status_payload())


@bp.post("/revert")
@roles_required("admin")
def revert():
    from .authz import rbac_mode, require

    if rbac_mode() == "scoped":
        require("master.write")
    """Re-patch the last snapshot (snapshotting current first), then roll.

    Revert is undoable: current state becomes the newest snapshot, so a
    bad revert reverts again.
    """
    client = K8sClient()
    live_name, history_name = _names()
    sts_name = current_app.config["MASTER_STATEFULSET"]
    if not client.config.available:
        flash(
            "No cluster connection from this page. Revert with kubectl: "
            f"{_manual(client.config.namespace, live_name)}.",
            "error",
        )
        log_event(current_user.username, "masterconfig-revert:refused-offline")
        return redirect(url_for("masterconfig.index"))
    try:
        revisions, _ = _read_history(client, history_name)
    except K8sError as exc:
        flash(f"The app could not read history: {exc}. Nothing changed.", "error")
        log_event(current_user.username, "masterconfig-revert:refused-history")
        return redirect(url_for("masterconfig.index"))
    if not revisions:
        flash("No snapshots yet. Nothing to revert to.", "error")
        log_event(current_user.username, "masterconfig-revert:refused-empty")
        return redirect(url_for("masterconfig.index"))
    try:
        current = client.get_configmap(live_name)
    except K8sError as exc:
        flash(f"The app could not read the live config: {exc}. Nothing changed.", "error")
        log_event(current_user.username, "masterconfig-revert:refused-read")
        return redirect(url_for("masterconfig.index"))
    data, revision = current["data"] or {}, current["resourceVersion"]
    try:
        _snapshot(client, live_name, history_name, data, revision)
    except K8sError as exc:
        flash(f"The app could not snapshot history: {exc}. Nothing changed.", "error")
        log_event(current_user.username, "masterconfig-revert:refused-history")
        return redirect(url_for("masterconfig.index"))
    try:
        fresh = client.get_configmap(live_name)
        new_rv = client.replace_configmap(
            live_name, revisions[-1]["data"], fresh["resourceVersion"]
        )
    except K8sConflictError:
        flash(
            "That config changed since you opened it. Reload and revert again. "
            "Nothing was written.",
            "error",
        )
        log_event(current_user.username, "masterconfig-revert:refused-stale")
        return redirect(url_for("masterconfig.index"))
    except K8sError as exc:
        flash(f"The app could not write the live config: {exc}. Nothing changed.", "error")
        log_event(current_user.username, "masterconfig-revert:refused-write")
        return redirect(url_for("masterconfig.index"))
    started, reason = _start_restart(
        client, sts_name, current_user.username, prefix=f"masterconfig-revert:{new_rv}:"
    )
    if not started:
        flash(
            f"Reverted to the previous snapshot (revision {new_rv}) but the "
            f"restart did not start ({reason}). Restart from this page.",
            "error",
        )
        log_event(current_user.username, f"masterconfig-revert:{new_rv}:refused")
        return redirect(url_for("masterconfig.index"))
    flash(
        f"Reverted to revision {new_rv}. Masters restarting; this page updates "
        "when they are healthy.",
        "success",
    )
    return redirect(url_for("masterconfig.index"))
