"""gunicorn entrypoint: gunicorn -b 0.0.0.0:8000 overstate_ui.wsgi:app"""

import os
import time

from sqlalchemy.exc import OperationalError

from . import create_app
from .db import create_all

RETRY_ATTEMPTS = 30
RETRY_DELAY_SECONDS = 2


def init_with_retry(
    app, attempts: int = RETRY_ATTEMPTS, delay: float = RETRY_DELAY_SECONDS
) -> int:
    """Create tables and seed the admin, retrying while Postgres starts.

    Returns the zero-based attempt that succeeded. Raises the last
    OperationalError when attempts run out.
    """
    for attempt in range(attempts):
        try:
            with app.app_context():
                create_all()
                from .auth import seed_admin

                # ADMIN_PASSWORD sets the initial admin password (installer).
                # Unset: seed_admin prints a random one to the container log.
                seed_admin(password=os.environ.get("ADMIN_PASSWORD") or None)
            return attempt
        except OperationalError:
            if attempt == attempts - 1:
                raise
            time.sleep(delay)
    raise AssertionError("unreachable")  # pragma: no cover


app = create_app()

# Postgres may still be starting (fresh compose up has no health gate).
init_with_retry(app)
