"""Unique (jid, minion_id) on job_returns.

Revision ID: e8f0a1b2c3d4
Revises: d5e6f7a8b9c0
Create Date: 2026-09-15 00:00:00.000000

"""

from typing import Sequence, Union

from alembic import op

revision: str = "e8f0a1b2c3d4"
down_revision: Union[str, None] = "d5e6f7a8b9c0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("job_returns") as batch_op:
        batch_op.create_unique_constraint(
            "uq_job_returns_jid_minion", ["jid", "minion_id"]
        )


def downgrade() -> None:
    with op.batch_alter_table("job_returns") as batch_op:
        batch_op.drop_constraint("uq_job_returns_jid_minion", type_="unique")
