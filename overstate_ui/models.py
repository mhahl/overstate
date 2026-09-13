"""Overstate data model — snapshots and returns, never source of truth (Salt is)."""

from __future__ import annotations

import datetime as dt

from flask_login import UserMixin
from sqlalchemy import (
    JSON,
    BigInteger,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class User(Base, UserMixin):
    __tablename__ = "users"
    __table_args__ = (
        UniqueConstraint("oidc_issuer", "oidc_sub", name="uq_users_oidc"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    password_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)
    role: Mapped[str] = mapped_column(String(16), nullable=False, default="viewer")
    oidc_issuer: Mapped[str | None] = mapped_column(String(255), nullable=True)
    oidc_sub: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class Setting(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)


class Minion(Base):
    """Grains snapshot cache. Refreshed on demand + RQ job; Salt is truth."""

    __tablename__ = "minions"

    id: Mapped[str] = mapped_column(String(255), primary_key=True)
    key_status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="accepted"
    )
    last_seen: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    grains: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    conformity: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)


class Job(Base):
    __tablename__ = "jobs"

    jid: Mapped[str] = mapped_column(String(32), primary_key=True)
    fun: Mapped[str] = mapped_column(String(128), nullable=False)
    tgt: Mapped[str] = mapped_column(Text, nullable=False)
    tgt_type: Mapped[str] = mapped_column(String(16), nullable=False)
    user: Mapped[str] = mapped_column(String(64), nullable=False)
    started_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    complete: Mapped[bool] = mapped_column(nullable=False, default=False)
    batch_group: Mapped[str | None] = mapped_column(String(32), nullable=True)
    batch_state: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    __table_args__ = (Index("ix_jobs_batch_group", "batch_group"),)


class JobReturn(Base):
    __tablename__ = "job_returns"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    jid: Mapped[str] = mapped_column(ForeignKey("jobs.jid"), nullable=False)
    minion_id: Mapped[str] = mapped_column(String(255), nullable=False)
    success: Mapped[bool] = mapped_column(nullable=False)
    retcode: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    payload: Mapped[dict] = mapped_column("return", JSON, nullable=False, default=dict)

    __table_args__ = (Index("ix_job_returns_jid", "jid"),)


class AuditEvent(Base):
    __tablename__ = "audit_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user: Mapped[str] = mapped_column(String(64), nullable=False)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    jid: Mapped[str | None] = mapped_column(String(32))
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (Index("ix_audit_events_created", "created_at"),)


class MinionGroup(Base):
    __tablename__ = "minion_groups"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    members: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class SavedJob(Base):
    __tablename__ = "saved_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    fun: Mapped[str] = mapped_column(String(128), nullable=False)
    tgt: Mapped[str] = mapped_column(Text, nullable=False)
    tgt_type: Mapped[str] = mapped_column(String(16), nullable=False)
    args: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    batch: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class PillarSnapshot(Base):
    """Rendered pillar per minion. Captured on explicit action only;
    Salt is truth. Retention enforced at capture time."""

    __tablename__ = "pillar_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    minion_id: Mapped[str] = mapped_column(String(255), nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    captured_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (Index("ix_pillar_snapshots_minion", "minion_id"),)


class WatchedState(Base):
    __tablename__ = "watched_states"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    sls: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)


class SaltJid(Base):
    """Stock pgjsonb returner transport table. Written by the master."""

    __tablename__ = "jids"

    jid: Mapped[str] = mapped_column(String(255), primary_key=True)
    load: Mapped[dict] = mapped_column(JSON, nullable=False)


class SaltReturn(Base):
    """Stock pgjsonb returner transport table. Written by the master.

    Mirrors the upstream schema exactly: ``id`` is the minion id, so the
    natural composite key (jid, id) is the primary key.
    """

    __tablename__ = "salt_returns"

    fun: Mapped[str] = mapped_column(String(50), nullable=False)
    jid: Mapped[str] = mapped_column(String(255), primary_key=True)
    minion_id: Mapped[str] = mapped_column("id", String(255), primary_key=True)
    success: Mapped[str] = mapped_column(String(10), nullable=False)
    payload: Mapped[dict] = mapped_column("return", JSON, nullable=False)
    full_ret: Mapped[dict] = mapped_column(JSON, nullable=False)
    alter_time: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        Index("idx_salt_returns_jid", "jid"),
        Index("idx_salt_returns_id", "id"),
        Index("idx_salt_returns_fun", "fun"),
    )


class SaltEvent(Base):
    """Stock pgjsonb returner transport table. Upstream uses a sequence-backed
    UNIQUE id; the model declares it PK, which is behaviorally equivalent and
    keeps the ORM happy. The app never writes here."""

    __tablename__ = "salt_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    tag: Mapped[str] = mapped_column(String(255), nullable=False)
    data: Mapped[dict] = mapped_column(JSON, nullable=False)
    alter_time: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    master_id: Mapped[str] = mapped_column(String(255), nullable=False)

    __table_args__ = (Index("idx_salt_events_tag", "tag"),)
