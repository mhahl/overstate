"""Batch execution columns: jobs.batch_group/state, saved_jobs.batch.

Revision ID: f3a9c2d1e4b5
Revises: b81c2d3e4f50
Create Date: 2026-09-12 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'f3a9c2d1e4b5'
down_revision: Union[str, None] = 'b81c2d3e4f50'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("jobs") as batch_op:
        batch_op.add_column(sa.Column("batch_group", sa.String(length=32),
                                      nullable=True))
        batch_op.add_column(sa.Column("batch_state", sa.JSON(), nullable=True))
        batch_op.create_index("ix_jobs_batch_group", ["batch_group"])
    with op.batch_alter_table("saved_jobs") as batch_op:
        batch_op.add_column(sa.Column("batch", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("saved_jobs") as batch_op:
        batch_op.drop_column("batch")
    with op.batch_alter_table("jobs") as batch_op:
        batch_op.drop_index("ix_jobs_batch_group")
        batch_op.drop_column("batch_state")
        batch_op.drop_column("batch_group")
