import sqlite3

import pytest

from conftest import make_student, make_team
from security.auth import AuthError
from workflow import services as sv


@pytest.mark.parametrize("n,ok", [(1, False), (2, True), (3, True), (4, False)])
def test_team_sizes(env, n, ok):
    u = make_student(env["conn"], f"s{n}")
    members = [{"full_name": f"M{i}", "member_ref": f"R{n}-{i}"} for i in range(n)]
    if ok:
        assert sv.register_team(env["conn"], env["p"], u, f"Team{n}", "x@example.org", members) > 0
    else:
        with pytest.raises(sv.WorkflowError):
            sv.register_team(env["conn"], env["p"], u, f"Team{n}", "x@example.org", members)


def test_membership_locked(env):
    u = make_team(env, "alpha", ps=None)
    conn = env["conn"]
    with pytest.raises(sv.WorkflowError):
        sv.register_team(conn, env["p"], u, "Again", "a@example.org", [{"full_name": "x", "member_ref": "q1"}, {"full_name": "y", "member_ref": "q2"}])
    with pytest.raises(sv.WorkflowError):
        sv.change_membership(conn, u, u.team_id)
    for sql in ["INSERT INTO team_members(team_id,full_name,member_ref) VALUES (%d,'z','zz')" % u.team_id,
                "DELETE FROM team_members WHERE team_id=%d" % u.team_id,
                "UPDATE teams SET membership_locked=0 WHERE id=%d" % u.team_id]:
        with pytest.raises(sqlite3.IntegrityError, match="locked"):
            conn.execute(sql)


def test_member_in_two_teams_rejected(env):
    make_team(env, "alpha", ps=None)
    u = make_student(env["conn"], "beta")
    with pytest.raises(sv.WorkflowError, match="another team"):
        sv.register_team(env["conn"], env["p"], u, "Beta", "b@example.org", [{"full_name": "x", "member_ref": "alpha-R0"}, {"full_name": "y", "member_ref": "b2"}])


def test_ps_domain_consistency_and_lock(env):
    u = make_team(env, "alpha", ps=None)
    with pytest.raises(sv.WorkflowError, match="does not belong"):
        sv.select_problem_statement(env["conn"], u, "HLTH", "AG-01")
    sv.select_problem_statement(env["conn"], u, "AGRI", "AG-01")
    with pytest.raises(sv.WorkflowError, match="locked"):
        sv.select_problem_statement(env["conn"], u, "AGRI", "AG-02")
    with pytest.raises(sqlite3.IntegrityError, match="locked"):
        env["conn"].execute("UPDATE teams SET ps_id='AG-02' WHERE id=?", (u.team_id,))


def test_only_students_register(env):
    with pytest.raises(AuthError):
        sv.register_team(env["conn"], env["p"], env["admin"], "Adm", "a@example.org", [])
