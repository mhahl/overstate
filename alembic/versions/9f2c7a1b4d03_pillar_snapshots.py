"""pillar snapshots for the v3 explorer (explicit capture, capped history)

Revision ID: 9f2c7a1b4d03
Revises: c7d2e41a90b4
Create Date: 2026-09-10 16:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '9f2c7a1b4d03'
down_revision: Union[str, None] = 'c7d2e41a90b4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "pillar_snapshots",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("minion_id", sa.String(length=255), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("captured_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Index("ix_pillar_snapshots_minion", "minion_id"),
    )


def downgrade() -> None:
    op.drop_table("pillar_snapshots")
