"""Dashboard: live counts from salt-api, history from Postgres."""

import httpx
from flask import Blueprint, current_app, render_template
from flask_login import login_required

from .db import get_session
from .models import Job, JobReturn
from .salt_client import SaltApiError, SaltClient

bp = Blueprint("dashboard", __name__)


def get_salt() -> SaltClient:
    return current_app.extensions["salt_client"]


def collect_stats(client: SaltClient) -> dict:
    """Live key/presence counts; falls back to unreachable marker."""
    stats: dict = {"reachable": False}
    try:
        keys = client.wheel("key.list_all")[0]["data"]["return"]
        accepted = keys.get("minions", [])
        pending = keys.get("minions_pre", [])
        status = client.runner("manage.status")[0]
        stats.update(
            {
                "reachable": True,
                "accepted": len(accepted),
                "pending": len(pending),
                "up": len(status.get("up", [])),
                "down": len(status.get("down", [])),
            }
        )
    except (SaltApiError, httpx.HTTPError, KeyError, IndexError, TypeError):
        pass
    session = get_session()
    stats["in_flight"] = session.query(Job).filter_by(complete=False).count()
    stats["last_failures"] = (
        session.query(JobReturn)
        .filter_by(success=False)
        .order_by(JobReturn.id.desc())
        .limit(5)
        .all()
    )
    return stats


@bp.route("/")
@login_required
def index():
    client = get_salt()
    return render_template(
        "dashboard.html", stats=collect_stats(client), health=client.health()
    )
