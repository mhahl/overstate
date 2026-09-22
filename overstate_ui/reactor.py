"""Master reactor mapping: list, inspect SLS, add/delete, export to file.

The mapping (event tag -> SLS) has two truths. The `reactor:` stanza in
``master.conf`` (owned ConfigMap) is the persisted, boot-time mapping;
the salt-api ``reactor`` runner changes the live mapping, fanned out to
every master pod because reactor systems are per-master. Runner writes
persist nothing, so a restart restores the file's stanza — record durable
mappings there (the export renders the live mapping in stanza shape).
SLS bodies are admin-editable: reactor code runs with master privileges
and fires on events fleet-wide, so operators keep the read-only view.
"""

import hashlib
import os
import re
from pathlib import Path

import yaml
from flask import (
    Blueprint,
    Response,
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
from .dashboard import get_salt
from .db import get_session
from .events import TAG_CHOICES
from .files import MAX_BYTES, highlight_yaml
from .fleet import pod_clients
from .k8s import K8sClient
from .models import AuditEvent
from .salt_client import SaltApiError

bp = Blueprint("reactor", __name__, url_prefix="/reactor")

# Event tags are slash-separated globs; quotes and shell metacharacters
# never belong in one.
NOT_RUNNING = "Reactor system is not running"

EVENT_RE = re.compile(r"^[A-Za-z0-9_./*?\[\]{}|+=:,@-]+$")
SLS_RE = re.compile(r"^[A-Za-z0-9_./:\-]+$")
MAX_EVENT_LEN = 256
MAX_SLS_LEN = 512


def roots() -> Path:
    return Path(current_app.config["REACTOR_ROOTS"]).resolve()


def _safe_join(rel: str) -> Path | None:
    """Resolve ``rel`` inside the reactor roots; None on traversal."""
    base = roots()
    try:
        target = (base / rel).resolve()
    except (OSError, ValueError):
        return None
    if target != base and base not in target.parents:
        return None
    return target


def sls_to_rel(sls: str) -> str | None:
    """Map a reactor SLS reference to a path under the reactor roots.

    Accepts ``salt://…``, absolute paths inside the roots, and bare
    relative paths. Anything else is listed but not browsable.
    """
    ref = sls.strip()
    if ref.startswith("salt://"):
        ref = ref[len("salt://") :]
    elif ref.startswith("/"):
        try:
            return str(Path(ref).resolve().relative_to(roots()))
        except (OSError, ValueError):
            return None
    if not ref or ref.startswith((".", "/")) or "\\" in ref:
        return None
    if _safe_join(ref) is None:
        return None
    return ref


def parse_reactor_list(value) -> tuple[dict[str, list[str]], str]:
    """Normalize ``reactor.list`` output to {event: [sls, …]}.

    The runner returns the master's reactor config: usually a list of
    single-key mappings, sometimes a plain mapping. Anything else is
    returned as raw text for the fallback box.
    """
    items: list = []
    if isinstance(value, dict):
        items = [value]
    elif isinstance(value, list):
        items = [v for v in value if isinstance(v, dict)]
    else:
        return {}, "" if value is None else str(value)
    entries: dict[str, list[str]] = {}
    for item in items:
        for event, sls in item.items():
            if not isinstance(event, str):
                continue
            refs = sls if isinstance(sls, list) else [sls]
            entries.setdefault(event, []).extend(str(r) for r in refs)
    return entries, ""


def _unwrap(value):
    """Unwrap salt-api's single-element runner envelope."""
    if isinstance(value, list) and len(value) == 1:
        return value[0]
    return value


RUNNER_FAILURE_MARKERS = (
    "Exception occurred in runner",
    "Traceback (most recent call last)",
)


def _extract_failure(value) -> str | None:
    """Runner tracebacks embedded in a 200 payload: last line only."""
    if isinstance(value, str) and any(m in value for m in RUNNER_FAILURE_MARKERS):
        lines = [ln.strip() for ln in value.splitlines() if ln.strip()]
        return lines[-1][:200] if lines else "unknown runner error"
    return None


def nearest_tag(event: str) -> str:
    """Event-family deep link for a reactor pattern (events viewer)."""
    for choice in TAG_CHOICES:
        if event.startswith(choice):
            return choice
    return "salt/job"


def _yaml_quote(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


def render_export(entries: dict[str, list[str]]) -> str:
    """Render the live mapping as a master-config YAML block for git."""
    lines = ["reactor:"]
    for event in sorted(entries):
        lines.append(f"  - {_yaml_quote(event)}:")
        for sls in entries[event]:
            lines.append(f"    - {_yaml_quote(sls)}")
    return "\n".join(lines) + "\n"


WIZARD_RECENTS_LIMIT = 5
WIZARD_FILE_CAP = 200


def _valid_event(event: str) -> bool:
    return (
        bool(event)
        and len(event) <= MAX_EVENT_LEN
        and EVENT_RE.match(event) is not None
    )


def _valid_sls(sls: str) -> bool:
    return bool(sls) and len(sls) <= MAX_SLS_LEN and SLS_RE.match(sls) is not None


def recent_events(limit: int = WIZARD_RECENTS_LIMIT) -> list[str]:
    """Last distinct reactor-add event patterns, presets excluded."""
    seen: list[str] = []
    rows = (
        get_session()
        .query(AuditEvent.action)
        .order_by(AuditEvent.id.desc())
        .limit(60)
        .all()
    )
    for (action,) in rows:
        if not action.startswith("reactor-add:"):
            continue
        event = action[len("reactor-add:") :].removesuffix(":partial")
        if not event or event in TAG_CHOICES or event in seen:
            continue
        seen.append(event)
        if len(seen) >= limit:
            break
    return seen


def list_reactor_files(cap: int = WIZARD_FILE_CAP) -> list[str] | None:
    """Flat relative paths under REACTOR_ROOTS; None when unreadable."""
    try:
        base = roots()
        out: list[str] = []
        for dirpath, _dirnames, filenames in os.walk(base):
            for name in filenames:
                out.append(str(Path(dirpath, name).relative_to(base)))
                if len(out) >= cap:
                    return sorted(out)
        return sorted(out)
    except OSError:
        return None


def build_persisted_text(current_text: str, event: str, sls: str) -> tuple[str, str]:
    """Merge one mapping into master.conf's reactor stanza, text-preserving.

    Comments, key order, and everything outside the stanza survive:
    an empty ``reactor: []`` line becomes a full block, a populated
    block gains one inserted item, and a missing stanza is appended.
    Returns (new_text, verdict) with verdict ``added`` / ``present`` /
    ``invalid``.
    """
    try:
        parsed = yaml.safe_load(current_text)
    except yaml.YAMLError:
        return current_text, "invalid"
    entries: dict[str, list[str]] = {}
    if isinstance(parsed, dict):
        entries, _ = parse_reactor_list(parsed.get("reactor"))
    if sls in entries.get(event, []):
        return current_text, "present"
    ending = "\n" if current_text.endswith("\n") else ""
    lines = current_text.splitlines()
    for i, line in enumerate(lines):
        if re.match(r"^reactor:\s*\[\s*\]\s*$", line):
            entries.setdefault(event, []).append(sls)
            block = render_export(entries).rstrip("\n")
            lines[i : i + 1] = block.splitlines()
            return "\n".join(lines) + ending, "added"
        if re.match(r"^reactor:\s*(#.*)?$", line):
            item = f"  - {_yaml_quote(event)}:\n    - {_yaml_quote(sls)}"
            lines.insert(i + 1, item)
            return "\n".join(lines) + ending, "added"
    merged = dict(entries)
    merged.setdefault(event, []).append(sls)
    block = render_export(merged)
    sep = "" if current_text.endswith("\n\n") else "\n"
    return current_text + sep + block, "added"


def _persist_to_master_conf(event: str, sls: str) -> tuple[bool, str, str]:
    """Append one mapping to the master.conf stanza, snapshot-first.

    Returns (ok, flash message, audit tag). Outside a cluster the
    write refuses with the manual kubectl equivalent.
    """
    from . import masterconfig as mc

    client = K8sClient()
    live_name = current_app.config["MASTER_CONFIGMAP"]
    history_name = current_app.config["MASTER_CONFIG_HISTORY"]
    namespace = client.config.namespace
    try:
        current = client.get_configmap(live_name)
    except Exception as exc:  # noqa: BLE001 — mapped below by type
        from .k8s import K8sUnavailableError as Offline

        if isinstance(exc, Offline):
            msg = (
                "No cluster connection from here. "
                f"Edit by hand instead: kubectl -n {namespace} "
                f"edit configmap {live_name}. Nothing changed."
            )
            tag = "masterconfig-save-refused:master.conf:persist-offline"
            return False, msg, tag
        return (
            False,
            f"Could not read the live config: {exc}. Nothing changed.",
            "masterconfig-save-refused:master.conf:persist-read",
        )
    data, revision = current["data"] or {}, current["resourceVersion"]
    if "master.conf" not in data:
        return (
            False,
            "master.conf is not in the live config. Nothing changed.",
            ("masterconfig-save-refused:master.conf:persist-missing"),
        )
    new_text, verdict = build_persisted_text(data["master.conf"], event, sls)
    if verdict == "invalid":
        return (
            False,
            "master.conf does not parse as YAML. Nothing changed.",
            "masterconfig-save-refused:master.conf:persist-invalid",
        )
    if verdict == "present":
        return (
            True,
            "Already recorded in the master.conf stanza.",
            ("masterconfig-save:master.conf:persist-present"),
        )
    try:
        mc._snapshot(client, live_name, history_name, data, revision)
    except Exception as exc:  # noqa: BLE001 — snapshot must not half-write
        return (
            False,
            f"Could not snapshot history: {exc}. Nothing changed.",
            "masterconfig-save-refused:master.conf:persist-history",
        )
    updated = dict(data)
    updated["master.conf"] = new_text
    try:
        new_rv = client.replace_configmap(live_name, updated, revision)
    except Exception as exc:  # noqa: BLE001 — conflict vs write mapped below
        from .k8s import K8sConflictError as Conflict

        if isinstance(exc, Conflict):
            msg = (
                "That config changed underneath you. Reload and try again. "
                "Nothing was written."
            )
            tag = "masterconfig-save-refused:master.conf:persist-stale"
            return False, msg, tag
        return (
            False,
            f"Could not write the live config: {exc}. Nothing changed.",
            "masterconfig-save-refused:master.conf:persist-write",
        )
    msg = (
        f"Recorded in the master.conf stanza (revision {new_rv}). "
        "Restart the masters to apply it."
    )
    return True, msg, f"masterconfig-save:master.conf:{new_rv}"


def _live_mappings(clients):
    """Union of the live reactor mapping across master pods.

    Returns (merged, raw, failed, divergent): merged {event: [sls]} with
    refs unioned in first-seen order; first non-empty raw fallback; failed
    pod names (transport/runner errors); divergent events (missing on at
    least one reachable pod). A pod whose reactor system is not running
    contributes an empty mapping — an empty state, not a failure. Runner
    add/delete mutate only the live mapping (Salt persists nothing), so a
    restart restores the `reactor:` stanza in master.conf.
    """
    merged: dict[str, list[str]] = {}
    present: dict[str, set[str]] = {}
    reached: list[str] = []
    failed: list[str] = []
    raw = ""
    for name, client in clients:
        try:
            value = client.runner("reactor.list", http_timeout=30.0)
            value = _unwrap(value)
            failure = _extract_failure(value)
            if failure is not None:
                if NOT_RUNNING in failure:
                    reached.append(name)
                    continue
                failed.append(name)
                continue
            entries, pod_raw = parse_reactor_list(value)
            if pod_raw and not raw:
                raw = pod_raw
        except SaltApiError as exc:
            # A 500 carrying the traceback body means the reactor system
            # is not running there — empty state, not a failure.
            if NOT_RUNNING in str(exc):
                reached.append(name)
                continue
            failed.append(name)
            continue
        reached.append(name)
        for event, refs in entries.items():
            present.setdefault(event, set()).add(name)
            for ref in refs:
                if ref not in merged.setdefault(event, []):
                    merged[event].append(ref)
    divergent = (
        sorted(e for e, pods in present.items() if len(pods) < len(reached))
        if reached
        else []
    )
    return merged, raw, failed, divergent


@bp.route("/")
@login_required
def index():
    q = request.args.get("q", "").strip().lower()
    sort = request.args.get("sort", "event")
    if sort not in ("event", "sls"):
        sort = "event"
    direction = request.args.get("dir", "asc")
    if direction not in ("asc", "desc"):
        direction = "asc"
    clients = pod_clients(get_salt())
    entries, raw, failed, divergent = _live_mappings(clients)
    error = None
    if not entries and len(failed) == len(clients):
        error = "salt-api error: no master reachable."
    for name in failed:
        flash(f"{name} unreachable: mapping may be partial.", "warning")
    # A master without reactor configured answers with "Reactor system
    # is not running" — an empty state with setup guidance, not an error.
    disabled = not entries and error is None
    rows = [
        {
            "event": event,
            "refs": refs,
            "rels": [sls_to_rel(r) for r in refs],
            "tag": nearest_tag(event),
        }
        for event, refs in entries.items()
        if not q or q in event.lower() or any(q in r.lower() for r in refs)
    ]
    key = (
        (lambda r: r["event"].lower())
        if sort == "event"
        else (lambda r: ",".join(r["refs"]).lower())
    )
    rows.sort(key=key, reverse=(direction == "desc"))
    return render_template(
        "reactor.html",
        rows=rows,
        raw=raw,
        error=error,
        disabled=disabled,
        pod_count=len(clients),
        divergent=divergent,
        q=request.args.get("q", ""),
        sort=sort,
        direction=direction,
        total=len(entries),
    )


@bp.route("/view")
@login_required
def view():
    rel = request.args.get("sls", "")
    target = _safe_join(rel) if rel and sls_to_rel(rel) == rel else None
    if not target or not target.is_file():
        abort(404)
    try:
        data = target.read_bytes()
    except OSError:
        abort(404)
    if len(data) > MAX_BYTES:
        return render_template(
            "reactor_view.html",
            rel=rel,
            content=None,
            reason="too large to display",
            size=len(data),
            max_bytes=MAX_BYTES,
        )
    try:
        content = data.decode("utf-8")
    except UnicodeDecodeError:
        return render_template(
            "reactor_view.html",
            rel=rel,
            content=None,
            reason="not readable text",
            size=len(data),
            max_bytes=MAX_BYTES,
        )
    return render_template(
        "reactor_view.html",
        rel=rel,
        content=content,
        highlighted=highlight_yaml(content),
        reason=None,
        size=None,
    )


@bp.post("/add")
@roles_required("operator")
def add():
    event = request.form.get("event", "").strip()
    sls = request.form.get("sls", "").strip()
    if (
        not event
        or not sls
        or len(event) > MAX_EVENT_LEN
        or len(sls) > MAX_SLS_LEN
        or not EVENT_RE.match(event)
        or not SLS_RE.match(sls)
    ):
        flash("An event pattern and one SLS reference are required.", "error")
        return redirect(url_for("reactor.index"))
    # Fan out to every pod: publish buses and reactor systems are
    # per-master, so a single-pod add would fire only for minions on
    # that pod. Runner writes are runtime-only (Salt persists nothing),
    # so a restart restores the `reactor:` stanza in master.conf.
    clients = pod_clients(get_salt())
    ok, refused, failed = [], [], []
    for name, client in clients:
        try:
            result = client.runner(
                "reactor.add", event=event, reactors=sls, http_timeout=30.0
            )
        except SaltApiError:
            failed.append(name)
            continue
        if isinstance(result, dict) and result.get("result") is False:
            refused.append(name)
        else:
            ok.append(name)
    if not ok:
        flash("salt-api error: no master reachable. Nothing changed.", "error")
        return redirect(url_for("reactor.index"))
    for name in failed:
        flash(
            f"{name} unreachable: mapping may be partial — re-run to converge.",
            "warning",
        )
    if refused:
        flash(
            f"{event}: salt did not confirm the add on {', '.join(refused)}.",
            "warning",
        )
    if failed or refused:
        log_event(current_user.username, f"reactor-add:{event}:partial")
        flash(
            f"{event}: reactor added on {len(ok)} of {len(clients)} pod(s).",
            "warning",
        )
    else:
        log_event(current_user.username, f"reactor-add:{event}")
        flash(f"{event}: reactor added.", "success")
    if request.form.get("persist", "") == "1" and current_user.role == "admin":
        pok, pmsg, ptag = _persist_to_master_conf(event, sls)
        log_event(current_user.username, ptag)
        flash(pmsg, "success" if pok else "error")
    return redirect(url_for("reactor.index"))


@bp.post("/delete")
@roles_required("operator")
def delete():
    event = request.form.get("event", "").strip()
    if not event:
        flash("Pick a reactor first: an empty selection never fires.", "error")
        return redirect(url_for("reactor.index"))
    # Fan out like add: a mapping deleted on one pod only keeps firing
    # for minions attached to the other pods.
    clients = pod_clients(get_salt())
    ok, failed = [], []
    for name, client in clients:
        try:
            client.runner("reactor.delete", event=event, http_timeout=30.0)
        except SaltApiError:
            failed.append(name)
            continue
        ok.append(name)
    if not ok:
        flash("salt-api error: no master reachable. Nothing changed.", "error")
        return redirect(url_for("reactor.index"))
    for name in failed:
        flash(
            f"{name} unreachable: mapping may be partial — re-run to converge.",
            "warning",
        )
    if failed:
        log_event(current_user.username, f"reactor-delete:{event}:partial")
        flash(
            f"{event}: reactor deleted on {len(ok)} of {len(clients)} pod(s).",
            "warning",
        )
    else:
        log_event(current_user.username, f"reactor-delete:{event}")
        flash(f"{event}: reactor deleted.", "success")
    return redirect(url_for("reactor.index"))


@bp.get("/add")
@roles_required("operator")
def add_wizard():
    """Add-wizard step 1: event pattern with presets and recents."""
    return render_template(
        "reactor_add.html",
        step=1,
        event="",
        presets=TAG_CHOICES,
        recents=recent_events(),
        error=None,
    )


@bp.post("/add/step2")
@roles_required("operator")
def add_step2():
    """Add-wizard step 2: SLS picker. Re-renders step 1 on bad event."""
    event = request.form.get("event", "").strip()
    if not _valid_event(event):
        return render_template(
            "reactor_add.html",
            step=1,
            event=event,
            presets=TAG_CHOICES,
            recents=recent_events(),
            error="Enter an event pattern using letters, digits, and "
            "/ . - * ? [ ] { } | + = : , @ _ (no spaces, max 256 chars).",
        )
    return render_template(
        "reactor_add.html",
        step=2,
        event=event,
        files=list_reactor_files(),
        pick="",
        custom="",
        error=None,
    )


@bp.post("/add/review")
@roles_required("operator")
def add_review():
    """Add-wizard step 3: blast-radius review before confirm."""
    event = request.form.get("event", "").strip()
    if not _valid_event(event):
        return redirect(url_for("reactor.add_wizard"))
    sls = (
        request.form.get("sls_custom", "").strip()
        or request.form.get("sls", "").strip()
    )
    if not _valid_sls(sls):
        files = list_reactor_files()
        return render_template(
            "reactor_add.html",
            step=2,
            event=event,
            files=files,
            pick=sls if files and sls in ["salt://" + f for f in files] else "",
            custom="" if files and sls in ["salt://" + f for f in files] else sls,
            error="Pick an SLS file or enter one SLS reference (max 512 chars).",
        )
    clients = pod_clients(get_salt())
    entries, _raw, failed, _divergent = _live_mappings(clients)
    down = not entries and not (clients and len(failed) == len(clients))
    unreachable = bool(clients) and not entries and len(failed) == len(clients)
    return render_template(
        "reactor_add.html",
        step=3,
        event=event,
        sls=sls,
        browsable=sls_to_rel(sls) is not None,
        pod_count=len(clients),
        down=down,
        unreachable=unreachable,
        is_admin=(current_user.role == "admin"),
        error=None,
    )


@bp.route("/edit")
@roles_required("admin")
def edit():
    """Edit form for one reactor SLS body (admin-only).

    Reactor code runs with master privileges and fires on events, so
    bodies stay out of operators' hands. ``base_hash`` records what the
    form was read at; the save route checks it (stale bases refuse).
    """
    rel = request.args.get("sls", "")
    target = _safe_join(rel) if rel and sls_to_rel(rel) == rel else None
    if not target or not target.is_file():
        abort(404)
    try:
        raw = target.read_bytes()
    except OSError:
        abort(404)
    if len(raw) > MAX_BYTES:
        flash("That SLS is too large to edit here. Nothing changed.", "error")
        return redirect(url_for("reactor.view", sls=rel))
    try:
        content = raw.decode("utf-8")
    except UnicodeDecodeError:
        flash("That SLS is not editable text. Nothing changed.", "error")
        return redirect(url_for("reactor.view", sls=rel))
    return render_template(
        "reactor_edit.html",
        rel=rel,
        content=content,
        base_hash=hashlib.sha256(raw).hexdigest(),
    )


@bp.post("/save")
@roles_required("admin")
def save():
    """Write one reactor SLS body (admin-only, edit-existing-only).

    Blocking YAML gate (bodies auto-fire on events), stale-hash refusal,
    identical-content no-op. New files arrive via git, not this form.
    """
    rel = request.form.get("sls", "")
    target = _safe_join(rel) if rel and sls_to_rel(rel) == rel else None
    if not target or not target.is_file():
        abort(404)
    try:
        current = target.read_bytes()
    except OSError:
        abort(404)
    try:
        current.decode("utf-8")
        editable = len(current) <= MAX_BYTES
    except UnicodeDecodeError:
        editable = False
    if not editable:
        flash("That SLS is no longer editable text. Nothing changed.", "error")
        log_event(current_user.username, f"reactor-save-refused:{rel}:uneditable")
        return redirect(url_for("reactor.view", sls=rel))
    data = request.form.get("content", "").encode("utf-8")
    if len(data) > MAX_BYTES:
        flash(
            f"Too large to save ({len(data)} bytes; limit is {MAX_BYTES}). "
            "Nothing changed.",
            "error",
        )
        log_event(current_user.username, f"reactor-save-refused:{rel}:oversize")
        return redirect(url_for("reactor.view", sls=rel))
    if data == current:
        flash("No changes — nothing saved.", "info")
        return redirect(url_for("reactor.view", sls=rel))
    if request.form.get("base_hash") != hashlib.sha256(current).hexdigest():
        flash(
            "That SLS changed underneath you. Reload the edit page and "
            "re-apply your change. Nothing was written.",
            "error",
        )
        log_event(current_user.username, f"reactor-save-refused:{rel}:stale")
        return redirect(url_for("reactor.edit", sls=rel))
    try:
        parsed = yaml.safe_load(data)
    except yaml.YAMLError as exc:
        flash(f"Invalid YAML: {str(exc)[:300]}. Nothing was written.", "error")
        log_event(current_user.username, f"reactor-save-refused:{rel}:invalid")
        return redirect(url_for("reactor.edit", sls=rel))
    if parsed is not None and not isinstance(parsed, dict):
        flash("Reactor SLS must be a top-level mapping. Nothing written.", "error")
        log_event(current_user.username, f"reactor-save-refused:{rel}:invalid")
        return redirect(url_for("reactor.edit", sls=rel))
    try:
        tmp = target.with_name(target.name + ".overstate-tmp")
        tmp.write_bytes(data)
        os.replace(tmp, target)
    except OSError:
        flash("Could not write the SLS. Nothing changed.", "error")
        log_event(current_user.username, f"reactor-save-refused:{rel}:write-failed")
        return redirect(url_for("reactor.view", sls=rel))
    flash(f"Saved {rel}. It fires on matching events from now on.", "success")
    log_event(current_user.username, f"reactor-save:{rel}")
    return redirect(url_for("reactor.view", sls=rel))


@bp.route("/export")
@login_required
def export():
    """Render the live mapping as a YAML block for master.conf.

    Union across pods (same fan-out reason as add/delete); paste the
    block into the `reactor:` stanza in master.conf through the Master
    Config page, then restart to make it the persisted mapping.
    """
    clients = pod_clients(get_salt())
    entries, _, failed, _ = _live_mappings(clients)
    error = None
    if not entries and len(failed) == len(clients):
        error = "salt-api error: no master reachable."
    disabled = not entries and error is None
    body = render_export(entries) if entries else ""
    if request.args.get("download") == "1" and not error:
        return Response(
            body,
            mimetype="text/plain",
            headers={"Content-Disposition": "attachment; filename=reactor.conf"},
        )
    return render_template(
        "reactor_export.html",
        body=body,
        error=None if disabled else error,
        disabled=disabled,
        total=len(entries),
    )
