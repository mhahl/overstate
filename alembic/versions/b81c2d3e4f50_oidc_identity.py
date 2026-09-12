"""SSO identity key: issuer + subject pair for OIDC users

Revision ID: b81c2d3e4f50
Revises: 9f2c7a1b4d03
Create Date: 2026-09-11 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'b81c2d3e4f50'
down_revision: Union[str, None] = '9f2c7a1b4d03'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Batch mode so the ALTER also applies on SQLite (dev scratch DBs);
    # Postgres uses the same path.
    with op.batch_alter_table("users") as batch_op:
        batch_op.add_column(sa.Column("oidc_issuer", sa.String(length=255),
                                      nullable=True))
        batch_op.add_column(sa.Column("oidc_sub", sa.String(length=255),
                                      nullable=True))
        batch_op.create_unique_constraint("uq_users_oidc",
                                          ["oidc_issuer", "oidc_sub"])


def downgrade() -> None:
    with op.batch_alter_table("users") as batch_op:
        batch_op.drop_constraint("uq_users_oidc", type_="unique")
        batch_op.drop_column("oidc_sub")
        batch_op.drop_column("oidc_issuer")
