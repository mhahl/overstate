"""Scoped RBAC schema: migration roundtrip and grant uniqueness.

Most tests build tables with ``create_all`` and never run alembic, so a
model/revision drift would only fail in production. These tests run the
real migration on SQLite instead.
"""

import os
import subprocess

import pytest
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.exc import IntegrityError

from overstate_ui import create_app
from overstate_ui.auth import seed_admin
from overstate_ui.config import TestConfig
from overstate_ui.db import create_all, get_session, init_db
from overstate_ui.models import Grant, LocalGroup, User

HEAD = "f2b3c4d5e6f7"


def _alembic(db_path, *args):
    env = dict(os.environ, DATABASE_URL=f"sqlite:///{db_path}")
    return subprocess.run(
        [".venv/bin/alembic", *args],
        capture_output=True,
        text=True,
        env=env,
        timeout=180,
        check=False,
    )


def _tables(db_path):
    from sqlalchemy import create_engine

    engine = create_engine(f"sqlite:///{db_path}")
    try:
        return set(sa_inspect(engine).get_table_names())
    finally:
        engine.dispose()


def _version(db_path):
    from sqlalchemy import create_engine, text

    engine = create_engine(f"sqlite:///{db_path}")
    try:
        with engine.connect() as conn:
            return conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
    finally:
        engine.dispose()


_RBAC_TABLES = (
    "grants",
    "local_groups",
    "local_group_members",
    "user_idp_groups",
    "idp_role_mappings",
    "api_tokens",
)


def test_migration_upgrade_downgrade_roundtrip(tmp_path):
    db = tmp_path / "rbac.sqlite"
    rv = _alembic(db, "upgrade", "head")
    assert rv.returncode == 0, rv.stderr
    assert _version(db) == HEAD
    tables = _tables(db)
    for table in _RBAC_TABLES:
        assert table in tables

    # -1 rolls back the idempotent backfill; tables stay.
    rv = _alembic(db, "downgrade", "-1")
    assert rv.returncode == 0, rv.stderr
    assert _version(db) == "f1a2b3c4d5e6"
    assert "grants" in _tables(db)

    # A second -1 drops the tables.
    rv = _alembic(db, "downgrade", "-1")
    assert rv.returncode == 0, rv.stderr
    assert "grants" not in _tables(db)

    rv = _alembic(db, "upgrade", "head")
    assert rv.returncode == 0, rv.stderr
    assert _version(db) == HEAD
    assert "grants" in _tables(db)


def test_upgrade_over_create_all_tables_is_quiet(tmp_path):
    """A database create_all already built must upgrade without the
    "already exists" error the entrypoint stamps on."""
    db = tmp_path / "prebuilt.sqlite"
    init_db(f"sqlite:///{db}")
    app = create_app(TestConfig)
    with app.app_context():
        create_all()
    rv = _alembic(db, "upgrade", "head")
    assert rv.returncode == 0, rv.stderr
    assert "already exists" not in rv.stderr


def _app_over(db):
    init_db(f"sqlite:///{db}")
    app = create_app(TestConfig)
    app.config["WTF_CSRF_ENABLED"] = False
    return app


def test_duplicate_user_grant_rejected(tmp_path):
    db = tmp_path / "grants.sqlite"
    assert _alembic(db, "upgrade", "head").returncode == 0
    app = _app_over(db)
    with app.app_context():
        create_all()
        seed_admin(password="pw")
        user = get_session().query(User).filter_by(username="admin").one()

        def _grant():
            return Grant(
                subject_kind="user",
                subject_user_id=user.id,
                subject_group_id=None,
                role="team-operator",
                scope_kind="glob",
                scope_value="web-*",
                source="manual",
            )

        get_session().add(_grant())
        get_session().commit()
        get_session().add(_grant())
        with pytest.raises(IntegrityError):
            get_session().commit()
        get_session().rollback()


def test_duplicate_local_group_grant_rejected(tmp_path):
    db = tmp_path / "group-grants.sqlite"
    assert _alembic(db, "upgrade", "head").returncode == 0
    app = _app_over(db)
    with app.app_context():
        create_all()
        group = LocalGroup(name="payments")
        get_session().add(group)
        get_session().commit()

        def _grant():
            return Grant(
                subject_kind="local_group",
                subject_user_id=None,
                subject_group_id=group.id,
                role="team-viewer",
                scope_kind="group",
                scope_value="3",
                source="manual",
            )

        get_session().add(_grant())
        get_session().commit()
        get_session().add(_grant())
        with pytest.raises(IntegrityError):
            get_session().commit()
        get_session().rollback()


def test_grant_requires_exactly_one_subject(tmp_path):
    db = tmp_path / "grant-subject.sqlite"
    assert _alembic(db, "upgrade", "head").returncode == 0
    app = _app_over(db)
    with app.app_context():
        create_all()
        seed_admin(password="pw")
        user = get_session().query(User).filter_by(username="admin").one()
        get_session().add(
            Grant(
                subject_kind="user",
                subject_user_id=None,
                subject_group_id=None,
                role="team-viewer",
                scope_kind="fleet",
                scope_value="*",
                source="manual",
            )
        )
        with pytest.raises(IntegrityError):
            get_session().commit()
        get_session().rollback()
        assert user.kind == "human"
