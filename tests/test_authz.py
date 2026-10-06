"""Scoped RBAC pure evaluator: roles, grain matching, target constraint.

No Flask routes, no Salt calls, no change to ``roles_required``. These
tests exercise ``overstate_ui/authz.py`` against the snapshot only.
"""

import time

import pytest

from overstate_ui import auth as authmod
from overstate_ui import create_app
from overstate_ui.authz import (
    AuthzDenied,
    authorize,
    constrain_kill,
    constrain_target,
    has_fleet,
    job_group_members,
    match_grain,
    may_define_schedule,
    minions_with,
    scope_group_members,
)
from overstate_ui.config import TestConfig
from overstate_ui.db import create_all, get_session, init_db
from overstate_ui.models import Grant, Minion, MinionGroup, User

PW = "test-password"


@pytest.fixture()
def app():
    init_db("sqlite://")
    app = create_app(TestConfig)
    app.config["WTF_CSRF_ENABLED"] = False
    with app.app_context():
        create_all()
        yield app


def _user(username: str, role: str = "viewer") -> User:
    session = get_session()
    user = User(
        username=username, password_hash=authmod._ph.hash(PW), role=role
    )
    session.add(user)
    session.commit()
    return user


def _minion(mid: str, grains: dict | None = None) -> None:
    get_session().add(Minion(id=mid, grains=grains or {}, conformity={}))
    get_session().commit()


def _grant(user, role, kind, value, subject_kind="user", source="manual"):
    session = get_session()
    grant = Grant(
        subject_kind=subject_kind,
        subject_user_id=user.id if subject_kind == "user" else None,
        subject_group_id=user.id if subject_kind == "local_group" else None,
        role=role,
        scope_kind=kind,
        scope_value=value,
        source=source,
    )
    session.add(grant)
    session.commit()
    return grant


# match_grain shapes (no SQL ->> anywhere on the authz path).


def test_match_grain_string(app):
    with app.app_context():
        assert match_grain({"osfinger": "Ubuntu 22.04"}, "osfinger:Ubuntu 22.04")
        assert not match_grain({"osfinger": "Ubuntu 22.04"}, "osfinger:Debian*")


def test_match_grain_int(app):
    with app.app_context():
        assert match_grain({"num_cpus": 4}, "num_cpus:4")
        assert not match_grain({"num_cpus": 4}, "num_cpus:8")


def test_match_grain_ipv4_list(app):
    with app.app_context():
        grains = {"ipv4": ["127.0.0.1", "10.0.0.5"]}
        assert match_grain(grains, "ipv4:10.0.0.5")
        assert not match_grain(grains, "ipv4:10.0.0.9")


def test_match_grain_rejects_non_snapshot(app):
    with app.app_context():
        assert not match_grain({"cpu_flags": ["avx"]}, "cpu_flags:avx")
        assert not match_grain({"os": "Ubuntu"}, "notakey:x")
        assert not match_grain({"os": "Ubuntu"}, "no-colon-here")
        assert not match_grain({"os": None}, "os:x")
        assert not match_grain({"os": {"nested": 1}}, "os:x")


def test_match_grain_glob(app):
    with app.app_context():
        assert match_grain({"os": "Ubuntu"}, "os:Ub*")
        assert match_grain({"osfinger": "Ubuntu-22.04"}, "osfinger:Ubuntu-*" )


# Role expansion and fleet-only storage.


def test_team_state_has_state_but_no_pillar(app):
    with app.app_context():
        user = _user("op1")
        _minion("web-01")
        _grant(user, "team-state", "glob", "web-*")
        assert authorize(user, "job.run.state", minion="web-01")
        assert not authorize(user, "pillar.read", minion="web-01")
        assert not authorize(user, "job.run.state", minion="db-01")


def test_team_operator_lacks_state(app):
    with app.app_context():
        user = _user("op2")
        _minion("web-01")
        _grant(user, "team-operator", "glob", "web-*")
        assert authorize(user, "job.run.change", minion="web-01")
        assert not authorize(user, "job.run.state", minion="web-01")


def test_fleet_only_role_ignored_off_fleet(app):
    with app.app_context():
        user = _user("sneaky")
        _minion("web-01")
        _grant(user, "admin", "glob", "web-*")
        assert not authorize(user, "master.write")
        assert not authorize(user, "job.run.read", minion="web-01")
        assert not has_fleet(user, "job.run.read")


def test_team_role_allowed_on_fleet(app):
    with app.app_context():
        user = _user("platform")
        _minion("web-01")
        _minion("db-01")
        _grant(user, "team-operator", "fleet", "*")
        assert has_fleet(user, "job.run.read")
        assert authorize(user, "job.run.read", minion="db-01")
        # Fleet-domain permissions still need a fleet ladder role.
        assert not authorize(user, "settings.write")


def test_unknown_permission_denied(app):
    with app.app_context():
        user = _user("u")
        _grant(user, "admin", "fleet", "*")
        assert not authorize(user, "job.run.arbitrary", minion="web-01")


# Scope resolution helpers.


def test_unknown_group_resolves_empty(app):
    with app.app_context():
        user = _user("u2")
        _minion("web-01")
        _grant(user, "team-operator", "group", "9999")
        assert scope_group_members(9999) == set()
        assert job_group_members("no-such-group") == set()
        assert minions_with(user, "job.run.read") == set()


def test_list_scope_parses_and_intersects_snapshot(app):
    with app.app_context():
        user = _user("u3")
        _minion("web-01")
        _grant(user, "team-operator", "list", '["web-01", "ghost-99"]')
        assert minions_with(user, "job.run.read") == {"web-01"}
        _grant(user, "team-viewer", "list", "not-json")
        assert minions_with(user, "job.read") == {"web-01"}


# constrain_target.


def test_scoped_star_becomes_snapshot_list(app):
    with app.app_context():
        user = _user("scoped-op")
        _minion("web-01")
        _minion("db-01")
        _grant(user, "team-operator", "glob", "web-*")
        tgt, tgt_type = constrain_target(user, "job.run.read", "*", "glob")
        assert tgt_type == "list"
        assert tgt == "web-01"


def test_scoped_empty_intersection_denied(app):
    with app.app_context():
        user = _user("scoped-op2")
        _minion("web-01")
        _minion("db-01")
        _grant(user, "team-operator", "glob", "web-*")
        with pytest.raises(AuthzDenied):
            constrain_target(user, "job.run.read", "db-*", "glob")


def test_scoped_compound_needs_fleet(app):
    with app.app_context():
        user = _user("scoped-op3")
        _minion("web-01")
        _grant(user, "team-operator", "glob", "web-*")
        with pytest.raises(AuthzDenied):
            constrain_target(user, "job.run.read", "G@os:Ubuntu", "compound")
        with pytest.raises(AuthzDenied):
            constrain_target(user, "job.run.read", "webservers", "nodegroup")


def test_fleet_target_unchanged(app):
    with app.app_context():
        user = _user("fleet-op", role="operator")
        _minion("web-01")
        _grant(user, "operator", "fleet", "*")
        assert constrain_target(user, "job.run.read", "*", "glob") == ("*", "glob")
        tgt, _ = constrain_target(user, "mine.read", "*", "glob")
        assert tgt == "*"


def test_constrain_grain_target_uses_snapshot(app):
    with app.app_context():
        user = _user("grain-op")
        _minion("web-01", {"os": "Ubuntu", "num_cpus": 4})
        _minion("db-01", {"os": "Debian", "num_cpus": 8})
        _grant(user, "team-operator", "grain", "os:Ubuntu")
        tgt, tgt_type = constrain_target(user, "job.run.read", "os:Ubuntu", "grain")
        assert (tgt, tgt_type) == ("web-01", "list")


# constrain_kill.


def test_kill_subset_of_compound_denied(app):
    with app.app_context():
        user = _user("killer")
        _minion("web-01")
        _grant(user, "team-operator", "glob", "web-*")
        with pytest.raises(AuthzDenied):
            constrain_kill(user, "G@os:Ubuntu", "compound")
        with pytest.raises(AuthzDenied):
            constrain_kill(user, "os:Ubuntu", "grain")


def test_kill_outside_scope_denied_not_subset(app):
    with app.app_context():
        user = _user("killer2")
        _minion("web-01")
        _minion("db-01")
        _grant(user, "team-operator", "glob", "web-*")
        with pytest.raises(AuthzDenied):
            constrain_kill(user, "web-01,db-01", "list")
        tgt, tgt_type = constrain_kill(user, "web-01", "list")
        assert (tgt, tgt_type) == ("web-01", "list")


def test_kill_fleet_republishes(app):
    with app.app_context():
        user = _user("killer3", role="operator")
        _minion("web-01")
        _grant(user, "operator", "fleet", "*")
        assert constrain_kill(user, "G@os:Ubuntu", "compound") == (
            "G@os:Ubuntu",
            "compound",
        )


# may_define_schedule.


def test_scheduler_define_only(app):
    with app.app_context():
        sched = _user("sched")
        _minion("web-01")
        _grant(sched, "scheduler", "glob", "web-*")
        assert may_define_schedule(sched, "web-01", "state.apply")
        assert not may_define_schedule(sched, "db-01", "state.apply")

        op = _user("teamop")
        _grant(op, "team-operator", "glob", "web-*")
        # team-operator holds no schedule.write.
        assert not may_define_schedule(op, "web-01", "state.apply")


# Latency tripwire: 3,000-row snapshot constrains inside 100 ms.


def test_constrain_tripwire_3000_minions(app):
    with app.app_context():
        user = _user("trip")
        session = get_session()
        session.add_all(
            Minion(id=f"web-{i:04d}", grains={"os": "Ubuntu"}, conformity={})
            for i in range(3000)
        )
        session.commit()
        _grant(user, "team-operator", "glob", "web-*")
        group = MinionGroup(name="web", members=[f"web-{i:04d}" for i in range(3000)])
        session.add(group)
        session.commit()
        start = time.perf_counter()
        tgt, tgt_type = constrain_target(user, "job.run.read", "*", "glob")
        elapsed_ms = (time.perf_counter() - start) * 1000
        assert tgt_type == "list"
        assert len(tgt.split(",")) == 3000
        assert elapsed_ms < 100, f"constrain took {elapsed_ms:.1f} ms"
