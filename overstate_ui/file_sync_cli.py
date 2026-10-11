"""Minute-tick entrypoint for configurable scheduled file-roots sync."""

from . import create_app
from .config import Config
from .db import create_all
from .file_sync_schedule import run_scheduled_sync


def main() -> int:
    app = create_app(Config)
    with app.app_context():
        create_all()
        result = run_scheduled_sync()
    if not result["ran"]:
        return 0
    print(result["message"])
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
