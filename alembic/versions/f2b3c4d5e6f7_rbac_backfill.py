"""Scoped RBAC backfill: fleet grants from users.role, fleet ladder
mappings from the OIDC group settings.

Revision ID: f2b3c4d5e6f7
Revises: f1a2b3c4d5e6
Create Date: 2026-10-04 00:00:00.000000

Idempotent and re-runnable: existing grants and mappings are left in
place, so a retried boot inserts nothing twice. Inserts no settings
rows.
"""

import os
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.orm import Session

revision: str = "f2b3c4d5e6f7"
down_revision: Union[str, None] = "f1a2b3c4d5e6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _setting(session: Session, key: str) -> str:
    bind = op.get_bind()
    if not inspect(bind).has_table("settings"):
        return ""
    row = session.execute(
        sa.text("SELECT value FROM settings WHERE key = :key"), {"key": key}
    ).first()
    return row[0] if row else ""


def upgrade() -> None:
    from overstate_ui.authz import backfill_rbac

    bind = op.get_bind()
    session = Session(bind=bind)
    try:
        admin_groups = _setting(session, "oidc_admin_groups") or os.environ.get(
            "OIDC_ADMIN_GROUPS", ""
        )
        operator_groups = _setting(
            session, "oidc_operator_groups"
        ) or os.environ.get("OIDC_OPERATOR_GROUPS", "")
        backfill_rbac(
            session,
            admin_groups=admin_groups,
            operator_groups=operator_groups,
        )
        session.commit()
    finally:
        session.close()


def downgrade() -> None:
    bind = op.get_bind()
    session = Session(bind=bind)
    try:
        session.execute(sa.text("DELETE FROM grants WHERE source = 'backfill'"))
        session.execute(
            sa.text("DELETE FROM idp_role_mappings WHERE origin = 'backfill'")
        )
        session.commit()
    finally:
        session.close()
