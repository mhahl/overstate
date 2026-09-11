"""gunicorn entrypoint: gunicorn -b 0.0.0.0:8000 overstate_ui.wsgi:app"""

import time

from sqlalchemy.exc import OperationalError

from . import create_app
from .db import create_all

app = create_app()

# Postgres may still be starting (fresh compose up has no health gate).
for attempt in range(30):
    try:
        with app.app_context():
            create_all()
            from .auth import seed_admin

            seed_admin()
        break
    except OperationalError:
        if attempt == 29:
            raise
        time.sleep(2)
