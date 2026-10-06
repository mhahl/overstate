"""Scoped RBAC grant, group, mapping, and token tables.

Purely additive. Every step guards on inspector state so a database
that already has the tables upgrades without emitting "already
exists".
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

revision: str = "f1a2b3c4d5e6"
down_revision: Union[str, None] = "e8f0a1b2c3d4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_table(bind, name: str) -> bool:
    return inspect(bind).has_table(name)


def _has_column(bind, table: str, column: str) -> bool:
    insp = inspect(bind)
    if not insp.has_table(table):
        return False
    return any(c["name"] == column for c in insp.get_columns(table))


def upgrade() -> None:
    bind = op.get_bind()

    if not _has_table(bind, "local_groups"):
        op.create_table(
            "local_groups",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("name", sa.String(length=128), nullable=False),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
            sa.UniqueConstraint("name"),
        )

    if not _has_table(bind, "local_group_members"):
        op.create_table(
            "local_group_members",
            sa.Column(
                "group_id",
                sa.Integer(),
                sa.ForeignKey("local_groups.id", ondelete="CASCADE"),
                primary_key=True,
            ),
            sa.Column(
                "user_id",
                sa.Integer(),
                sa.ForeignKey("users.id", ondelete="CASCADE"),
                primary_key=True,
            ),
        )

    if not _has_table(bind, "user_idp_groups"):
        op.create_table(
            "user_idp_groups",
            sa.Column(
                "user_id",
                sa.Integer(),
                sa.ForeignKey("users.id", ondelete="CASCADE"),
                primary_key=True,
            ),
            sa.Column("group_name", sa.String(length=255), primary_key=True),
        )

    if not _has_table(bind, "idp_role_mappings"):
        op.create_table(
            "idp_role_mappings",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("idp_group", sa.String(length=255), nullable=False),
            sa.Column("role", sa.String(length=32), nullable=False),
            sa.Column("scope_kind", sa.String(length=16), nullable=False),
            sa.Column("scope_value", sa.Text(), nullable=False),
            sa.Column("origin", sa.String(length=16), nullable=False),
            sa.UniqueConstraint(
                "idp_group",
                "role",
                "scope_kind",
                "scope_value",
                name="uq_idp_role_mappings_group_role_scope",
            ),
        )

    if not _has_table(bind, "grants"):
        op.create_table(
            "grants",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("subject_kind", sa.String(length=16), nullable=False),
            sa.Column(
                "subject_user_id",
                sa.Integer(),
                sa.ForeignKey("users.id", ondelete="CASCADE"),
                nullable=True,
            ),
            sa.Column(
                "subject_group_id",
                sa.Integer(),
                sa.ForeignKey("local_groups.id", ondelete="CASCADE"),
                nullable=True,
            ),
            sa.Column("role", sa.String(length=32), nullable=False),
            sa.Column("scope_kind", sa.String(length=16), nullable=False),
            sa.Column("scope_value", sa.Text(), nullable=False),
            sa.Column("source", sa.String(length=16), nullable=False),
            sa.Column(
                "created_by",
                sa.Integer(),
                sa.ForeignKey("users.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
            sa.CheckConstraint(
                "(subject_user_id IS NOT NULL AND subject_group_id IS NULL)"
                " OR (subject_user_id IS NULL AND subject_group_id IS NOT NULL)",
                name="ck_grants_one_subject",
            ),
        )

    if not _has_table(bind, "api_tokens"):
        op.create_table(
            "api_tokens",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column(
                "user_id",
                sa.Integer(),
                sa.ForeignKey("users.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("name", sa.String(length=128), nullable=False),
            sa.Column("token_hash", sa.String(length=255), nullable=False),
            sa.Column("token_prefix", sa.String(length=12), nullable=False),
            sa.Column(
                "saved_job_id",
                sa.Integer(),
                sa.ForeignKey("saved_jobs.id", ondelete="RESTRICT"),
                nullable=True,
            ),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column(
                "created_by",
                sa.Integer(),
                sa.ForeignKey("users.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
        )

    if not _has_column(bind, "users", "kind"):
        with op.batch_alter_table("users") as batch_op:
            batch_op.add_column(
                sa.Column(
                    "kind",
                    sa.String(length=16),
                    nullable=False,
                    server_default="human",
                )
            )

    if not _has_column(bind, "jobs", "tgt_requested"):
        with op.batch_alter_table("jobs") as batch_op:
            batch_op.add_column(sa.Column("tgt_requested", sa.Text(), nullable=True))

    for column, ctype in (
        ("outcome", sa.String(length=16)),
        ("permission", sa.String(length=64)),
        ("minion_id", sa.String(length=255)),
        ("detail", sa.Text()),
    ):
        if not _has_column(bind, "audit_events", column):
            with op.batch_alter_table("audit_events") as batch_op:
                batch_op.add_column(sa.Column(column, ctype, nullable=True))
    with op.batch_alter_table("audit_events") as batch_op:
        batch_op.alter_column(
            "action",
            existing_type=sa.String(length=64),
            type_=sa.String(length=128),
            existing_nullable=False,
        )

    # IF NOT EXISTS keeps this quiet when create_all built the tables
    # first; plain create_index would raise "already exists".
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_grants_user"
        " ON grants (subject_kind, subject_user_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_grants_local_group"
        " ON grants (subject_kind, subject_group_id)"
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS grants_user_scope_uq"
        " ON grants (subject_user_id, role, scope_kind, scope_value)"
        " WHERE subject_user_id IS NOT NULL"
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS grants_local_group_scope_uq"
        " ON grants (subject_group_id, role, scope_kind, scope_value)"
        " WHERE subject_group_id IS NOT NULL"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_idp_role_mappings_group"
        " ON idp_role_mappings (idp_group)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_user_idp_groups_group"
        " ON user_idp_groups (group_name)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_audit_events_outcome_created"
        " ON audit_events (outcome, created_at)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_api_tokens_prefix"
        " ON api_tokens (token_prefix)"
    )


def downgrade() -> None:
    bind = op.get_bind()
    for index in (
        "ix_api_tokens_prefix",
        "ix_audit_events_outcome_created",
        "ix_user_idp_groups_group",
        "ix_idp_role_mappings_group",
        "grants_local_group_scope_uq",
        "grants_user_scope_uq",
        "ix_grants_local_group",
        "ix_grants_user",
    ):
        op.execute(f"DROP INDEX IF EXISTS {index}")

    with op.batch_alter_table("audit_events") as batch_op:
        batch_op.alter_column(
            "action",
            existing_type=sa.String(length=128),
            type_=sa.String(length=64),
            existing_nullable=False,
        )
    for column in ("detail", "minion_id", "permission", "outcome"):
        if _has_column(bind, "audit_events", column):
            with op.batch_alter_table("audit_events") as batch_op:
                batch_op.drop_column(column)
    if _has_column(bind, "jobs", "tgt_requested"):
        with op.batch_alter_table("jobs") as batch_op:
            batch_op.drop_column("tgt_requested")
    if _has_column(bind, "users", "kind"):
        with op.batch_alter_table("users") as batch_op:
            batch_op.drop_column("kind")

    for table in (
        "api_tokens",
        "grants",
        "idp_role_mappings",
        "user_idp_groups",
        "local_group_members",
        "local_groups",
    ):
        if _has_table(bind, table):
            op.drop_table(table)
