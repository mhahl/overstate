"""Seed-mock tests: counts, double-run refusal, --force replace."""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from overstate_ui.models import Base, Job, Minion
from overstate_ui.seed_mock import main, seed


@pytest.fixture()
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def test_seed_inserts_expected_rows(session):
    counts = seed(session)
    assert counts == {"minions": 5, "jobs": 3, "returns": 4}
    assert session.query(Minion).count() == 5
    assert session.query(Job).count() == 3
    pending = session.query(Minion).filter_by(key_status="pending").all()
    assert [m.id for m in pending] == ["new-node-01"]
    failed = (
        session.query(Job).filter_by(fun="state.highstate").one()
    )
    assert failed.complete is True


def test_seed_refuses_when_data_present(session):
    seed(session)
    with pytest.raises(RuntimeError):
        seed(session)


def test_seed_force_replaces(session):
    seed(session)
    counts = seed(session, force=True)
    assert session.query(Minion).count() == 5
    assert counts["minions"] == 5


def test_main_second_run_refuses(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/seed.db")
    assert main([]) == 0
    assert main([]) == 1
    assert main(["--force"]) == 0
