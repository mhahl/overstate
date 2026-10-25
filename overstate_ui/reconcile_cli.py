"""Hourly key-reconcile entrypoint: python -m overstate_ui.reconcile_cli.

Built for the key-reconcile CronJob. Same rules as the Keys-page button:
complete same-fingerprint trust, and copy byte-identical accepted keys
onto pods missing them (first check-ins, outage splits) — verified per
pod by re-read. Never overrides reject/deny, never invents trust.
Audited the same way. Exit 1 only when no pod was reachable — skips are
normal drift handling, not failure.
"""

from . import create_app
from .audit import log_event
from .config import Config
from .dashboard import get_salt
from .fleet import pod_clients
from .keys import backfill_absent_keys, reconcile_keys


def main() -> int:
    app = create_app(Config)
    with app.app_context():
        clients = pod_clients(get_salt())
        try:
            report = reconcile_keys(clients)
        except Exception as exc:  # noqa: BLE001 — cron reports, not traces
            print(f"reconcile failed: {exc}")
            return 1
        if len(report["unreachable"]) == len(clients):
            print("reconcile: no master reachable")
            return 1
        try:
            bf = backfill_absent_keys(clients)
        except Exception as exc:  # noqa: BLE001 — backfill degrades to report
            print(f"backfill failed: {exc}")
            bf = {
                "backfilled": [],
                "skipped": [],
                "errors": [],
                "unreachable": [],
                "unavailable": True,
            }
        for pod, mid in report["accepted"]:
            print(f"accepted {mid} on {pod}")
        for pod, mid in bf["backfilled"]:
            print(f"backfilled {mid} to {pod}")
        for mid, reason in report["skipped"]:
            print(f"skipped {mid}: {reason}")
        for mid, reason in bf["skipped"]:
            print(f"backfill skipped {mid}: {reason}")
        for pod, mid in report["errors"]:
            print(f"error accepting {mid} on {pod}")
        for pod, reason in bf["errors"]:
            print(f"error backfilling on {pod}: {reason}")
        if bf["unavailable"]:
            print("backfill unavailable (no pod mapping or K8s access)")
        log_event("cron", f"reconcile-keys:{len(report['accepted'])}")
        if bf["backfilled"]:
            log_event("cron", f"backfill-keys:{len(bf['backfilled'])}")
        print(
            f"reconcile done: {len(report['accepted'])} accepted, "
            f"{len(bf['backfilled'])} backfilled, "
            f"{len(report['skipped']) + len(bf['skipped'])} skipped"
        )
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
