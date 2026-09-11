"""Audit trail. Every mutating action records who, what, and which JID."""

from .db import get_session
from .models import AuditEvent


def log_event(user: str, action: str, jid: str | None = None) -> AuditEvent:
    session = get_session()
    event = AuditEvent(user=user, action=action, jid=jid)
    session.add(event)
    session.commit()
    return event
