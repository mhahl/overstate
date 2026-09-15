"""Per-minion conformity verdict trail.

Revision ID: d5e6f7a8b9c0
Revises: a7c4e9f2b6d1
Create Date: 2026-09-15 00:00:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "d5e6f7a8b9c0"
down_revision: Union[str, None] = "a7c4e9f2b6d1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "state_conformity_history",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("minion_id", sa.String(length=255), nullable=False),
        sa.Column("jid", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column(
            "checked_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_state_conformity_history_minion",
        "state_conformity_history",
        ["minion_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_state_conformity_history_minion",
        table_name="state_conformity_history",
    )
    op.drop_table("state_conformity_history")
