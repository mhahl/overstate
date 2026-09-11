"""user roles for v2 RBAC; nullable password for OIDC-only users

Revision ID: c7d2e41a90b4
Revises: 95ffc8849775
Create Date: 2026-09-10 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'c7d2e41a90b4'
down_revision: Union[str, None] = '95ffc8849775'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("users", sa.Column("role", sa.String(length=16),
                                     server_default="viewer", nullable=False))
    # Existing installs predate roles; the seeded local admin keeps working.
    op.execute("UPDATE users SET role = 'admin' WHERE username = 'admin'")
    op.alter_column("users", "password_hash", existing_type=sa.String(length=255),
                    nullable=True)


def downgrade() -> None:
    op.alter_column("users", "password_hash", existing_type=sa.String(length=255),
                    nullable=False)
    op.drop_column("users", "role")
