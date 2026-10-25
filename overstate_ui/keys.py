"""Key management. Tabs per status; every action fans out to all masters.

A minion must be accepted on the pod it lands on, so accept/reject/delete
run on every reachable master pod (idempotent wheel calls). The roster
merges all pods with per-pod state chips; an unreachable pod degrades to
a warning, never a silent split.
"""

import hashlib
import re
import shlex

from flask import (
    Blueprint,
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
from .fleet import pod_clients
from .k8s import K8sClient, K8sError
from .models import Minion
from .salt_client import SaltApiError, SaltClient

bp = Blueprint("keys", __name__, url_prefix="/keys")

TABS = [
    ("pending", "minions_pre"),
    ("accepted", "minions"),
    ("rejected", "minions_rejected"),
    ("denied", "minions_denied"),
]

ACTIONS = {"accept": "key.accept", "reject": "key.reject", "delete": "key.delete"}

PAST = {"accept": "accepted", "reject": "rejected", "delete": "deleted"}

# Salt key buckets, used to read wheel key.* glob_match returns.
_KEY_BUCKETS = ("minions", "minions_pre", "minions_rejected", "minions_denied")


def key_action_applied(action: str, result, mid: str) -> bool:
    """Whether a wheel key.* return shows mid applied.

    Salt returns the post-change glob_match ({"minions": [...]}) through
    salt-api as [{"data": {"return": {...}}}]. Fail-open on unknown
    shapes (True, {"success": True}, [{"result": True}]) so older
    masters never read as failures; {} means no match, which is
    unapplied for accept/reject but success for delete (already gone).
    """
    try:
        node = result[0] if isinstance(result, list) and result else result
        if isinstance(node, dict):
            data = node.get("data")
            if isinstance(data, dict) and "return" in data:
                if data.get("success") is False:
                    return False
                node = data["return"]
        if node is True:
            return True
        if isinstance(node, dict):
            if any(k in node for k in _KEY_BUCKETS):
                present = {
                    k for k in _KEY_BUCKETS if mid in (node.get(k) or [])
                }
                if not present:
                    # Absent on this pod: nothing to apply there (the
                    # key lives on another pod). Absent is converged.
                    return True
                if action == "accept":
                    return "minions" in present
                if action == "reject":
                    return "minions_rejected" in present
                if action == "delete":
                    return False
                return True
            if node == {}:
                # No match on this pod: nothing to apply there (the
                # key lives on another pod). Absent is converged, and
                # a delete of an already-absent key is the end state.
                return True
            return True
        return True
    except (KeyError, IndexError, TypeError, AttributeError):
        return True

# key.finger section names per roster tab (when the return is sectioned).
_FINGER_SECTIONS = {
    "accepted": "minions",
    "pending": "minions_pre",
    "rejected": "minions_rejected",
    "denied": "minions_denied",
}

_FP_RE = re.compile(r"^([0-9a-fA-F]{2}:)+[0-9a-fA-F]{2}$")


def _fingerprints(client, http_timeout: float | None = 8.0) -> dict:
    """Merge key.finger output into {minion_id: fingerprint}."""
    fp_re = _FP_RE
    try:
        # match is required: key.finger with no match crashes server-side
        # (UnboundLocalError on Salt 3008).
        result = client.wheel("key.finger", match="*", http_timeout=http_timeout)[0]
    except (SaltApiError, KeyError, IndexError, TypeError):
        return {}
    node = result.get("data", {}).get("return", result)
    merged: dict = {}

    def walk(obj) -> None:
        if isinstance(obj, dict):
            for key, value in obj.items():
                if isinstance(value, str) and fp_re.match(value):
                    merged[key] = value
                else:
                    walk(value)
        elif isinstance(obj, list):
            for item in obj:
                walk(item)

    walk(node)
    return merged


def get_key_data(client) -> dict:
    listed = client.wheel("key.list_all")[0]["data"]["return"]
    fingers = _fingerprints(client)
    out = {}
    for tab, field in TABS:
        out[tab] = [
            {"id": mid, "fingerprint": fingers.get(mid, "—")}
            for mid in sorted(listed.get(field, []))
        ]
    return out


def merged_key_data(
    clients: list[tuple[str, SaltClient]],
    http_timeout: float | None = 8.0,
) -> tuple[dict, list[str]]:
    """(union roster, unreachable pod names).

    Each row carries per-pod states; a minion appears under a tab when ANY
    pod reports it there. Unreachable pods contribute nothing (their
    absence is flashed by the caller). Reads carry a short HTTP backstop
    so a sick master degrades the page fast instead of pinning a worker.
    """
    states: dict[str, dict] = {}
    order: list[str] = []
    failed: list[str] = []
    for name, client in clients:
        try:
            listed = client.wheel("key.list_all", http_timeout=http_timeout)[0]["data"][
                "return"
            ]
            fingers = _fingerprints(client, http_timeout=http_timeout)
        except (SaltApiError, KeyError, IndexError, TypeError):
            failed.append(name)
            continue
        for tab, field in TABS:
            for mid in listed.get(field, []) or []:
                entry = states.setdefault(
                    mid, {"id": mid, "fingerprint": "—", "states": {}}
                )
                if mid not in order:
                    order.append(mid)
                entry["states"][name] = tab
                if entry["fingerprint"] == "—":
                    entry["fingerprint"] = fingers.get(mid, "—")
    out: dict = {}
    for tab, _ in TABS:
        out[tab] = [
            states[mid]
            for mid in sorted(order)
            if tab in states[mid]["states"].values()
        ]
    return out, failed


def _attribute_fingerprints(node, listed: dict[str, set]) -> dict[str, dict]:
    """{tab: {minion_id: fingerprint}}, failing closed on ambiguity.

    Section-keyed key.finger returns attribute exactly. A flat return
    attributes a fingerprint only to an id sitting in a single status on
    that pod, so a same-id collision across statuses can never read as
    agreement. Anything unresolvable stays absent.
    """
    out: dict[str, dict] = {tab: {} for tab, _ in TABS}
    if not isinstance(node, dict):
        return out
    sections = {tab: node.get(field) for tab, field in _FINGER_SECTIONS.items()}
    if any(isinstance(ids, dict) and ids for ids in sections.values()):
        for tab, ids in sections.items():
            if isinstance(ids, dict):
                for mid, fp in ids.items():
                    if isinstance(fp, str) and _FP_RE.match(fp):
                        out[tab][mid] = fp
        return out
    flat: dict = {}

    def walk(obj) -> None:
        if isinstance(obj, dict):
            for key, value in obj.items():
                if isinstance(value, str) and _FP_RE.match(value):
                    flat[key] = value
                else:
                    walk(value)
        elif isinstance(obj, list):
            for item in obj:
                walk(item)

    walk(node)
    counts: dict = {}
    for ids in listed.values():
        for mid in ids:
            counts[mid] = counts.get(mid, 0) + 1
    for tab, ids in listed.items():
        for mid in ids:
            if counts[mid] == 1 and mid in flat:
                out[tab][mid] = flat[mid]
    return out


def _fleet_key_states(
    clients: list[tuple[str, SaltClient]],
) -> tuple[dict, list[str]]:
    """Per-pod ``(listed, fingers)`` plus unreachable pod names.

    Shared classification read for reconcile and backfill so both act on
    the same fleet picture: ``listed`` is ``{tab: set(ids)}`` from
    ``key.list_all`` and ``fingers`` the attributed
    ``{tab: {id: fingerprint}}`` from ``key.finger``.
    """
    per_pod: dict[str, tuple] = {}
    unreachable: list[str] = []
    for name, cli in clients:
        try:
            listed_raw = cli.wheel("key.list_all", http_timeout=8.0)[0]["data"][
                "return"
            ]
            listed = {tab: set(listed_raw.get(field, []) or []) for tab, field in TABS}
        except (SaltApiError, KeyError, IndexError, TypeError):
            unreachable.append(name)
            continue
        try:
            node = cli.wheel("key.finger", match="*", http_timeout=8.0)[0]
            node = node.get("data", {}).get("return", node)
        except (SaltApiError, KeyError, IndexError, TypeError):
            node = None
        per_pod[name] = (listed, _attribute_fingerprints(node, listed))
    return per_pod, unreachable


def reconcile_keys(clients, execute: bool = True) -> dict:
    """Complete key trust across pods; never create it.

    For each minion pending on a reachable pod, accept it there iff
    another reachable pod reports it accepted with the same fingerprint.
    Globally-pending keys, fingerprint mismatches, and anything
    rejected/denied anywhere stay for a human. With execute=False the
    same plan is returned but no wheel accept fires (preview).

    Returns {"accepted": [(pod, id)], "skipped": [(id, reason)],
    "unreachable": [pod], "errors": [(pod, id)]}.
    """
    per_pod, unreachable = _fleet_key_states(clients)
    by_name = dict(clients)
    accepted: list[tuple[str, str]] = []
    skipped: list[tuple[str, str]] = []
    errors: list[tuple[str, str]] = []
    ids = sorted(
        {
            mid
            for listed, _ in per_pod.values()
            for ids in listed.values()
            for mid in ids
        }
    )
    for mid in ids:
        state = {
            name: next((tab for tab, ids in listed.items() if mid in ids), None)
            for name, (listed, _) in per_pod.items()
        }
        deciders = [
            (name, tab) for name, tab in state.items() if tab in ("rejected", "denied")
        ]
        if deciders:
            name, tab = deciders[0]
            skipped.append((mid, f"explicit {tab} on {name}; decide by hand"))
            continue
        pending_on = [name for name, tab in state.items() if tab == "pending"]
        if not pending_on:
            # No pending key anywhere: either healthy (accepted on every
            # pod that lists it) or key-file drift — accepted on one
            # reachable pod while the key file never arrived on another
            # (wheel key.accept only moves pending keys, so fan-out can
            # never create trust there; the minion only auths to the
            # master it is attached to, so the blind master never even
            # files it as pending). Skipping silently here produced
            # "0 accepted, 0 skipped" with a blind master in the fleet:
            # report the drift, still accepting nothing by hand.
            accepted_on = [name for name, tab in state.items() if tab == "accepted"]
            absent_on = [name for name, tab in state.items() if tab is None]
            if accepted_on and absent_on:
                skipped.append(
                    (
                        mid,
                        f"accepted on {', '.join(accepted_on)} but key file "
                        + f"absent on {', '.join(absent_on)}. Backfill the "
                        + "accepted key by hand (reconcile cannot accept a "
                        + "key that was never pending there)",
                    )
                )
            continue  # absent elsewhere or already accepted everywhere
        trusted = [
            (name, fingers["accepted"][mid])
            for name, (_, fingers) in per_pod.items()
            if state[name] == "accepted" and mid in fingers["accepted"]
        ]
        if not trusted:
            skipped.append((mid, "trusted nowhere; accept by hand"))
            continue
        for pod in pending_on:
            here = per_pod[pod][1]["pending"].get(mid)
            if here and any(here == fp for _, fp in trusted):
                if execute:
                    try:
                        by_name[pod].wheel("key.accept", match=mid)
                    except SaltApiError:
                        errors.append((pod, mid))
                        continue
                accepted.append((pod, mid))
            else:
                skipped.append((mid, f"{pod}: fingerprint mismatch or unavailable"))
    return {
        "accepted": accepted,
        "skipped": skipped,
        "unreachable": unreachable,
        "errors": errors,
    }


# --- Accepted-key backfill ----------------------------------------------

CLUSTER_PKI_DIR = "/home/salt/data/keys/_cluster"
"""Where minion pubkeys live on each master pod.

Mirrors ``cluster_pki_dir`` in master.conf. Because ``cluster_id`` is set,
Salt's localfs_key driver (``salt/cache/localfs_key.py::init_kwargs``)
stores the master PKI under the cluster dir instead of ``pki_dir``, so an
accepted minion pubkey is ``<dir>/minions/<id>``. Every read (list, fetch,
auth) hits the filesystem — no in-memory cache — so a file copied here
takes effect live with no restart.
"""

_POD_CLIENT_RE = re.compile(r"^pod-(\d+)$")


def _k8s_pod_names(
    clients: list[tuple[str, SaltClient]], k8s_pods: dict | None = None
) -> dict | None:
    """{salt client name: K8s pod name} for exec backfill, or None.

    An explicit ``k8s_pods`` mapping (tests, callers that already mapped)
    wins. Otherwise ``pod-{i}`` derives ``{STATEFULSET}-{i}`` from app
    config. None means backfill is unavailable — single-master fallback,
    no app context, or K8s unreachable — and callers keep today's
    fan-out-only behavior.
    """
    if k8s_pods is not None:
        return dict(k8s_pods)
    try:
        sts = current_app.config["MASTER_STATEFULSET"]
        if not K8sClient().config.available:
            return None
    except (RuntimeError, KeyError, AttributeError, K8sError):
        return None
    names = {}
    for name, _ in clients:
        match = _POD_CLIENT_RE.match(name)
        if not match:
            return None
        names[name] = f"{sts}-{match.group(1)}"
    return names


def _backfill_candidates(per_pod: dict) -> tuple[list, list]:
    """[(mid, fingerprint, accepted_on, absent_on)] needing a copy.

    Only unanimous, human-approved trust replicates: accepted on at least
    one reachable pod, key file missing on at least one other reachable
    pod, nothing rejected/denied anywhere, every accepted copy carrying
    the same fingerprint. Everything else stays for reconcile (pending)
    or a human (conflicts, quarantine).
    """
    planned: list = []
    skipped: list = []
    ids = sorted(
        {
            mid
            for listed, _ in per_pod.values()
            for ids in listed.values()
            for mid in ids
        }
    )
    for mid in ids:
        state = {
            name: next((tab for tab, ids in listed.items() if mid in ids), None)
            for name, (listed, _) in per_pod.items()
        }
        if any(tab in ("rejected", "denied") for tab in state.values() if tab):
            continue  # quarantine wins; reconcile reports it
        accepted_on = sorted(n for n, tab in state.items() if tab == "accepted")
        if not accepted_on:
            continue  # trusted nowhere: reconcile's lane
        absent_on = sorted(n for n, tab in state.items() if tab is None)
        if not absent_on:
            continue  # converged
        fps = {per_pod[n][1]["accepted"].get(mid) for n in accepted_on}
        if len(fps) != 1 or None in fps:
            skipped.append(
                (
                    mid,
                    "accepted copies disagree or lack fingerprints; decide by hand",
                )
            )
            continue
        (fingerprint,) = fps
        planned.append((mid, fingerprint, accepted_on, absent_on))
    return planned, skipped


def _read_accepted_pubkey(clients_by_name: dict, source: str, mid: str) -> str | None:
    """PEM bytes the source pod trusts for mid, or None (fail closed).

    Reads ``key.key_str`` from a pod whose ``key.list_all`` already shows
    mid accepted, and only trusts the ``minions`` (accepted) section.
    """
    try:
        result = clients_by_name[source].wheel("key.key_str", match=mid)
    except SaltApiError:
        return None
    try:
        node = result[0] if isinstance(result, list) and result else result
        if isinstance(node, dict):
            data = node.get("data")
            if isinstance(data, dict) and "return" in data:
                node = data["return"]
        sections = node if isinstance(node, dict) else {}
        accepted = sections.get("minions")
        pem = accepted.get(mid) if isinstance(accepted, dict) else None
    except (KeyError, IndexError, TypeError, AttributeError):
        return None
    if not isinstance(pem, str) or "PUBLIC KEY" not in pem or not pem.strip():
        return None
    return pem


def _backfill_script(pem: str, dest: str) -> list[str]:
    """Atomic accepted-key install: temp file, verify-by-hash downstream.

    The caller compares the script's sha256 stdout against the source
    digest, then re-reads ``key.list_all`` on the target: bytes and
    state both confirmed before the pod counts as backfilled.
    """
    script = (
        "set -eu; "
        f"dest={shlex.quote(dest)}; "
        'dir=$(dirname "$dest"); '
        'test -d "$dir" || install -d -m 0755 -o salt -g salt "$dir"; '
        'tmp=$(mktemp "$dir/.backfill.XXXXXX"); '
        f"printf '%s' {shlex.quote(pem)} > \"$tmp\"; "
        'chown salt:salt "$tmp"; chmod 0644 "$tmp"; '
        'mv -f "$tmp" "$dest"; '
        "sha256sum \"$dest\" | awk '{print $1}'"
    )
    return ["/bin/bash", "-c", script]


def _backfill_one(
    mid: str,
    fingerprint: str,
    accepted_on: list[str],
    absent_on: list[str],
    clients_by_name: dict,
    k8s_pods: dict,
    k8s_client,
) -> tuple[list, list, list]:
    """Copy mid's accepted pubkey onto absent pods. Returns
    (backfilled, skipped, errors) with pod-qualified entries."""
    backfilled: list = []
    skipped: list = []
    errors: list = []
    if "/" in mid or "\x00" in mid or mid in (".", "..") or mid.startswith("."):
        return (
            backfilled,
            [(mid, "unsafe key id; copy the accepted key by hand")],
            errors,
        )
    pem = _read_accepted_pubkey(clients_by_name, accepted_on[0], mid)
    if pem is None:
        return (
            backfilled,
            [(mid, f"{accepted_on[0]}: accepted key unreadable")],
            errors,
        )
    digest = hashlib.sha256(pem.encode()).hexdigest()
    dest = f"{CLUSTER_PKI_DIR}/minions/{mid}"
    for pod in absent_on:
        target = k8s_pods.get(pod)
        if target is None:
            skipped.append((mid, f"{pod}: no pod mapping"))
            continue
        try:
            res = k8s_client.exec_command(
                target, _backfill_script(pem, dest), timeout=60.0
            )
        except K8sError as exc:
            errors.append((pod, f"{mid}: exec failed ({str(exc)[:120]})"))
            continue
        if not res.get("ok") or res.get("stdout", "").strip() != digest:
            errors.append((pod, f"{mid}: copy unverified"))
            continue
        try:
            listed = clients_by_name[pod].wheel("key.list_all")[0]["data"]["return"]
            now = listed.get("minions", []) or []
        except (SaltApiError, KeyError, IndexError, TypeError):
            errors.append((pod, f"{mid}: re-read failed"))
            continue
        if mid in now:
            backfilled.append(pod)
        else:
            errors.append((pod, f"{mid}: not accepted after copy"))
    return backfilled, skipped, errors


def backfill_absent_keys(
    clients: list[tuple[str, SaltClient]],
    k8s_pods: dict | None = None,
    dry_run: bool = False,
) -> dict:
    """Replicate unanimously-trusted accepted keys onto pods missing them.

    The fan-out accept cannot reach pods that never saw the key: wheel
    ``key.accept`` only moves pending keys, and a first check-in lands
    pending on exactly one pod (single-homed minions). This copies the
    byte-identical accepted pubkey there instead — same trust, new pods —
    and verifies hash plus re-read state per pod. Never overrides
    reject/deny, never invents trust (fingerprint-unanimous sources
    only), never writes on ``dry_run``.

    Returns {"plan": [(pod, mid)], "backfilled": [(pod, mid)],
    "skipped": [(mid, reason)], "errors": [(pod, reason)],
    "unreachable": [pod], "unavailable": bool}.
    """
    per_pod, unreachable = _fleet_key_states(clients)
    planned, skipped = _backfill_candidates(per_pod)
    report = {
        "plan": [(pod, mid) for mid, _, _, absent in planned for pod in absent],
        "backfilled": [],
        "skipped": skipped,
        "errors": [],
        "unreachable": unreachable,
        "unavailable": False,
    }
    if dry_run or not planned:
        return report
    mapping = _k8s_pod_names(clients, k8s_pods)
    if mapping is None:
        report["unavailable"] = True
        return report
    try:
        k8s_client = K8sClient()
    except K8sError:
        report["unavailable"] = True
        return report
    clients_by_name = dict(clients)
    for mid, _fingerprint, accepted_on, absent_on in planned:
        done, skip, errs = _backfill_one(
            mid,
            _fingerprint,
            accepted_on,
            absent_on,
            clients_by_name,
            mapping,
            k8s_client,
        )
        report["backfilled"].extend((pod, mid) for pod in done)
        report["skipped"].extend(skip)
        report["errors"].extend(errs)
    return report


def backfill_key(
    mid: str,
    clients: list[tuple[str, SaltClient]],
    k8s_pods: dict | None = None,
) -> dict:
    """Ensure every reachable pod trusts mid's accepted key (single id).

    Thin wrapper over :func:`backfill_absent_keys` for the accept paths:
    after a fan-out accept, replicate mid where it is still absent and
    verify. Returns {"backfilled": [pod], "skipped": [(mid, reason)],
    "errors": [(pod, reason)], "unavailable": bool}.
    """
    report = backfill_absent_keys(clients, k8s_pods=k8s_pods)
    return {
        "backfilled": [pod for pod, _m in report["backfilled"] if _m == mid],
        "skipped": [s for s in report["skipped"] if s[0] == mid],
        "errors": [e for e in report["errors"] if e[1].startswith(f"{mid}:")],
        "unavailable": report["unavailable"],
        "plan": [pod for pod, _m in report["plan"] if _m == mid],
    }


@bp.route("/")
@login_required
def index():
    from flask_login import current_user

    from .authz import has_fleet, minions_with, rbac_mode, require

    require("key.read")
    tab = request.args.get("tab", "pending")
    if tab not in dict(TABS):
        tab = "pending"
    from .tasks_queue import read_keys_cache, write_keys_cache

    clients = pod_clients(get_salt())
    # Merged roster + fingerprints, cached briefly: rebuilding it costs
    # two wheel calls per pod on every view. Mutations clear it below.
    cached = read_keys_cache()
    if (
        isinstance(cached, dict)
        and isinstance(cached.get("data"), dict)
        and isinstance(cached.get("failed"), list)
    ):
        data, failed = cached["data"], cached["failed"]
    else:
        data, failed = merged_key_data(clients)
        if len(failed) < len(clients):
            write_keys_cache(data, failed)
    if len(failed) == len(clients):
        flash("Salt API error: no master is reachable.", "error")
        data = {t: [] for t, _ in TABS}
    if rbac_mode() == "scoped" and not has_fleet(current_user, "key.read"):
        # Accepted ids in scope only; the pending, rejected, and denied
        # piles stay fleet key.read.
        allowed = minions_with(current_user, "key.read")
        data = {
            t: [r for r in rows if r["id"] in allowed] if t == "accepted" else []
            for t, rows in data.items()
        }
    for name in failed:
        flash(f"{name} is unreachable. Key states are partial.", "warning")
    counts = {t: len(data[t]) for t, _ in TABS}
    masters = [
        m.strip()
        for m in current_app.config.get("SYNDIC_MASTERS", "").split(",")
        if m.strip()
    ]
    q = request.args.get("q", "").strip()
    ql = q.lower()
    rows = data[tab]
    if ql:
        rows = [
            r for r in rows if ql in r["id"].lower() or ql in r["fingerprint"].lower()
        ]
    sort = request.args.get("sort", "id")
    if sort not in ("id",):
        sort = "id"
    direction = request.args.get("dir", "asc")
    if direction not in ("asc", "desc"):
        direction = "asc"
    rows = sorted(rows, key=lambda r: r["id"], reverse=(direction == "desc"))
    from .minions import resolve_page_size

    per_page = resolve_page_size(request.args.get("per_page"))
    try:
        requested_page = max(1, int(request.args.get("page", 1)))
    except ValueError:
        requested_page = 1
    total = len(rows)
    pages = max(1, -(-total // per_page))
    page = min(requested_page, pages)
    page_rows = rows[(page - 1) * per_page:page * per_page]
    # Scale-up drift signal: ids pending on one pod but accepted on
    # another are one Reconcile click from converging.
    by_id: dict[str, dict] = {}
    for tab_rows in data.values():
        for row in tab_rows:
            by_id.setdefault(row["id"], {}).update(row.get("states") or {})
    completable = sum(
        1
        for states in by_id.values()
        if {p for p, s in states.items() if s == "pending"}
        - {p for p, s in states.items() if s == "accepted"}
        and "accepted" in states.values()
    )
    # First-check-in / outage splits: accepted on some reachable pod but
    # the key file never arrived on another (accept fans out, but wheel
    # key.accept cannot create trust where no pending key exists). One
    # Reconcile click copies the byte-identical accepted key there.
    reachable = len(clients) - len(failed)
    missing = sum(
        1
        for states in by_id.values()
        if "accepted" in states.values()
        and not any(s in ("rejected", "denied") for s in states.values())
        and len(states) < reachable
    )
    return render_template(
        "keys.html",
        tab=tab,
        rows=page_rows,
        total=total,
        page=page,
        pages=pages,
        per_page=per_page,
        counts=counts,
        syndic_masters=masters,
        completable=completable,
        missing=missing,
        q=q,
        sort=sort,
        direction=direction,
    )


@bp.post("/reconcile")
@roles_required("operator")
def reconcile():
    from .authz import rbac_mode, require

    if rbac_mode() == "scoped":
        # Accept admits pending keys on every master: fleet-only, since
        # a pending key has no trustworthy grain.
        require("key.accept")
    clients = pod_clients(get_salt())
    if request.form.get("confirm") == "1":
        report = reconcile_keys(clients)
        if len(report["unreachable"]) == len(clients):
            flash("Salt API error: no master is reachable. Nothing changed.", "error")
        else:
            # Copy byte-identical accepted keys onto pods missing them
            # (first check-ins, outage splits): same unanimous-trust
            # rule as the accept path, verified per pod by re-read.
            bf = backfill_absent_keys(clients)
            from .tasks_queue import clear_key_caches

            clear_key_caches()
            n = len(report["accepted"])
            m = len(bf["backfilled"])
            log_event(current_user.username, f"reconcile-keys:{n}")
            if bf["backfilled"]:
                log_event(
                    current_user.username,
                    f"backfill-keys:{m}:{','.join(pod for pod, _ in bf['backfilled'])}",
                    permission="key.accept",
                )
            detail = "; ".join(
                [f"{mid}: {reason}" for mid, reason in report["skipped"]]
                + [f"{pod}/{mid}: accept failed" for pod, mid in report["errors"]]
                + [reason for _, reason in bf["errors"]]
                + [f"{mid}: {reason}" for mid, reason in bf["skipped"]]
                + (
                    ["backfill unavailable (no pod mapping or K8s access)"]
                    if bf["unavailable"] and bf["plan"]
                    else []
                )
                + (
                    [f"unreachable: {', '.join(report['unreachable'])}"]
                    if report["unreachable"]
                    else []
                )
            )
            summary = f"Reconciled {n} key(s)."
            if m:
                summary += (
                    f" Backfilled {m} key(s) "
                    f"({', '.join(f'{mid} on {pod}' for pod, mid in bf['backfilled'])})."
                )
            flash(
                summary + (f" {detail}" if detail else ""),
                "success"
                if (n or m) and not detail
                else "warning"
                if (n or m)
                else "info",
            )
        return redirect(url_for("keys.index"))
    report = reconcile_keys(clients, execute=False)
    if len(report["unreachable"]) == len(clients):
        flash("Salt API error: no master is reachable. Nothing changed.", "error")
        return redirect(url_for("keys.index"))
    plan = backfill_absent_keys(clients, dry_run=True)
    return render_template(
        "keys_reconcile.html", report=report, backfill_plan=plan["plan"]
    )


@bp.post("/<action>")
@roles_required("operator")
def act(action: str):
    if action not in ACTIONS:
        flash("Unknown key action.", "error")
        return redirect(url_for("keys.index"))
    mid = request.form.get("id", "").strip()
    tab = request.form.get("tab", "pending")
    keep = {"tab": tab}
    for f in ("q", "sort", "dir"):
        if request.form.get(f):
            keep[f] = request.form[f]
    if not mid:
        flash("Select a minion first. An empty key selection does nothing.", "error")
        return redirect(url_for("keys.index", **keep))
    # match= is a Salt glob: a forged "*" would accept or delete every
    # key, so only exact ids from the roster may pass.
    if any(c in mid for c in "*?[]"):
        flash("You cannot use wildcards in key ids.", "error")
        return redirect(url_for("keys.index", **keep))
    from .authz import rbac_mode, require

    if rbac_mode() == "scoped":
        # Gate before the roster read: a denied caller publishes nothing.
        # Unknown ids still redirect below, but only for gate-passers.
        if action == "delete":
            require("key.delete", minion=mid)
        else:
            require("key.accept")
    clients = pod_clients(get_salt())
    roster, _ = merged_key_data(clients)
    current_ids = {row["id"] for rows in roster.values() for row in rows}
    if mid not in current_ids:
        # An empty union means no master answered: say so plainly.
        flash("That key is missing from the current list.", "error")
        return redirect(url_for("keys.index", **keep))
    failed = []
    unapplied = []
    for name, cli in clients:
        try:
            # key.accept only moves pending by default: a rotated
            # minion re-files its new pub under denied (anti-hijack),
            # so an accept that skips denied/rejected no-ops there
            # while flashing success. Include both: the roster gate
            # above means only a known id can reach here.
            kwargs = {"match": mid}
            if action == "accept":
                kwargs["include_rejected"] = True
                kwargs["include_denied"] = True
            result = cli.wheel(ACTIONS[action], **kwargs)
            if not key_action_applied(action, result, mid):
                unapplied.append(name)
        except SaltApiError:
            failed.append(name)
    if len(failed) == len(clients):
        flash("Salt API error: no master is reachable. Nothing changed.", "error")
        return redirect(url_for("keys.index", **keep))
    # A first check-in lands pending on exactly one pod (single-homed
    # minions), so the fan-out above no-ops on the others: wheel
    # key.accept only moves keys already filed there. Replicate the
    # now-accepted pubkey onto reachable pods still missing it
    # (byte-identical, fingerprint-unanimous) and verify by re-read.
    bf = (
        backfill_key(mid, clients)
        if action == "accept"
        else {
            "backfilled": [],
            "skipped": [],
            "errors": [],
            "unavailable": False,
            "plan": [],
        }
    )
    from .tasks_queue import clear_key_caches

    clear_key_caches()
    past = PAST[action]
    # A delete must also clear the inventory snapshot: the minion list
    # unions the snapshot cache with the live roster, so a key delete
    # that leaves the snapshot behind never removes the minion. Job
    # history is kept. Accept/reject only change trust, so the snapshot
    # (grains, history) stays put.
    removed = False
    if action == "delete":
        session = get_session()
        row = session.get(Minion, mid)
        if row is not None:
            session.delete(row)
            session.commit()
            removed = True
            log_event(
                current_user.username,
                f"minion-remove:{mid}",
                permission="minion.remove",
                minion_id=mid,
            )
    suffix = " Inventory row removed." if removed else ""
    if bf["backfilled"]:
        suffix += f" Backfilled to {', '.join(bf['backfilled'])}."
        log_event(
            current_user.username,
            f"backfill-key:{mid}:{','.join(bf['backfilled'])}",
            permission="key.accept",
            minion_id=mid,
        )
    unanswered = sorted(set(failed) | set(unapplied))
    bf_errors = "; ".join(reason for _, reason in bf["errors"])
    if bf["unavailable"] and bf["plan"] and not (failed or unapplied):
        # Accepted here, but the copy step could not reach the missing
        # pods (dev box, no K8s). Say so instead of flashing full
        # convergence: the fleet is still split.
        bits = [
            f"accepted here but not copied to {', '.join(bf['plan'])} "
            "(backfill unavailable)"
        ]
        flash(
            f"{mid}: {past} on the converged masters. "
            f"{'; '.join(bits)}. Retry to converge.{suffix}",
            "warning",
        )
        log_event(
            current_user.username,
            f"{action}-key-partial:{mid}:backfill-unavailable",
            permission="key.delete" if action == "delete" else "key.accept",
            minion_id=mid,
        )
    elif failed or unapplied or bf["errors"]:
        bits = []
        if failed:
            bits.append(f"{', '.join(failed)} did not answer")
        if unapplied:
            # The wheel call succeeded but left mid out of the target
            # state there: e.g. rejecting a denied key, or acting on a
            # pod that never saw it. Name it instead of flashing success.
            bits.append(
                f"{', '.join(unapplied)} left it in another state "
                "(delete the key there if that state is wrong)"
            )
        if bf["errors"]:
            bits.append(f"backfill failed ({bf_errors})")
        flash(
            f"{mid}: {past} on the converged masters. "
            f"{'; '.join(bits)}. Retry to converge.{suffix}",
            "warning",
        )
        log_event(
            current_user.username,
            f"{action}-key-partial:{mid}:{','.join(unanswered)}",
            permission="key.delete" if action == "delete" else "key.accept",
            minion_id=mid,
        )
    else:
        log_event(
            current_user.username,
            f"{action}-key",
            permission="key.delete" if action == "delete" else "key.accept",
            minion_id=mid,
        )
        flash(f"{mid}: {past}.{suffix}", "success")
    return redirect(url_for("keys.index", **keep))
