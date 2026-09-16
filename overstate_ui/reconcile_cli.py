"""Hourly key-reconcile entrypoint: python -m overstate_ui.reconcile_cli.

Built for the key-reconcile CronJob. Uses the same completion rule as
the Keys-page button (trust completed, never created), audited the same
way. Exit 1 only when no pod was reachable — skips are normal drift
handling, not failure.
"""

from . import create_app
from .audit import log_event
from .config import Config
from .dashboard import get_salt
from .fleet import pod_clients
from .keys import reconcile_keys


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
        for pod, mid in report["accepted"]:
            print(f"accepted {mid} on {pod}")
        for mid, reason in report["skipped"]:
            print(f"skipped {mid}: {reason}")
        for pod, mid in report["errors"]:
            print(f"error accepting {mid} on {pod}")
        log_event("cron", f"reconcile-keys:{len(report['accepted'])}")
        print(
            f"reconcile done: {len(report['accepted'])} accepted, "
            f"{len(report['skipped'])} skipped"
        )
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
