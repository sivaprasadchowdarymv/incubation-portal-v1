import itertools
import sqlite3

import pytest

from conftest import do_submit, good_docx, make_team
from database.db import MIGRATIONS, connect, current_version, migrate
from security.auth import AuthError, hash_password, safe_filename, verify_password
from workflow import services as sv
from workflow import states

ACTORS = ["system", "reviewer", "admin", "student"]


@pytest.mark.parametrize("src,dst,actor", list(itertools.product(sorted(states.STATES), sorted(states.STATES), ACTORS)))
def test_every_transition(src, dst, actor):
    allowed = dst in states.TRANSITIONS[src] and actor in states.TRANSITIONS[src][dst]
    if allowed:
        states.check(src, dst, actor)
    else:
        with pytest.raises(states.InvalidTransition):
            states.check(src, dst, actor)


def test_students_never_drive_transitions():
    assert all("student" not in actors for t in states.TRANSITIONS.values() for actors in t.values())


def test_service_rejects_invalid_transition(env):
    u = make_team(env, "a")
    r = do_submit(env, u, good_docx())
    with pytest.raises(states.InvalidTransition):
        sv._transition(env["conn"], r["submission_id"], "PENDING_REVIEW", None)


@pytest.mark.parametrize("raw,expected", [("../../etc/passwd", "passwd"), ("..\\..\\win.ini", "win.ini"), ("a b;rm -rf.docx", "a_b_rm_-rf.docx"), ("", "upload"), (".hidden.pdf", "hidden.pdf")])
def test_safe_filename(raw, expected):
    assert safe_filename(raw) == expected


def test_upload_stored_inside_storage(env):
    u = make_team(env, "a")
    r = do_submit(env, u, good_docx(), name="../../../evil.docx")
    path = env["conn"].execute("SELECT stored_path FROM submissions WHERE id=?", (r["submission_id"],)).fetchone()[0]
    assert str(env["tmp"]) in path and ".." not in path


def test_passwords():
    h = hash_password("correct horse")
    assert verify_password("correct horse", h) and not verify_password("wrong", h)
    with pytest.raises(ValueError):
        hash_password("short")


def test_rbac_data_access(env):
    a, b = make_team(env, "a"), make_team(env, "b")
    ra = do_submit(env, a, good_docx(1))
    with pytest.raises(AuthError):
        sv.report(env["conn"], b, ra["submission_id"])
    with pytest.raises(AuthError):
        sv.list_submissions(env["conn"], b, team_id=a.team_id)
    with pytest.raises(AuthError):
        sv.get_team(env["conn"], b, a.team_id)
    assert all(r["team_id"] == b.team_id for r in sv.list_submissions(env["conn"], b))
    with pytest.raises(AuthError):
        sv.audit_log(env["conn"], a)
    with pytest.raises(AuthError):
        sv.admin_create_user(env["conn"], a, "x123", "password123", "admin")
    assert len(sv.list_submissions(env["conn"], env["admin"])) == 1


def test_login_audit_no_password(env):
    assert sv.authenticate(env["conn"], "admin", "bad-password") is None
    row = env["conn"].execute("SELECT details FROM audit_events WHERE action='login_failed'").fetchone()
    assert "bad-password" not in row[0]


def test_audit_append_only(env):
    with pytest.raises(sqlite3.IntegrityError):
        env["conn"].execute("DELETE FROM audit_events")
    with pytest.raises(sqlite3.IntegrityError):
        env["conn"].execute("UPDATE audit_events SET action='x'")


def test_migrations_idempotent_and_persistence(env):
    u = make_team(env, "a")
    r = do_submit(env, u, good_docx())
    path = env["s"].db_path
    env["conn"].close()
    c2 = connect(path)  # simulated application restart
    assert migrate(c2) == [] and current_version(c2) == MIGRATIONS[-1][0]
    assert c2.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    t = c2.execute("SELECT ps_locked, membership_locked FROM teams WHERE id=?", (u.team_id,)).fetchone()
    assert tuple(t) == (1, 1)
    assert c2.execute("SELECT status FROM submissions WHERE id=?", (r["submission_id"],)).fetchone()[0] == "ACCEPTED_FOR_EVALUATION"
    env["conn"] = c2


def test_deadline_sweep(env):
    from datetime import timedelta

    from conftest import bad_docx

    u = make_team(env, "a")
    r = do_submit(env, u, bad_docx())
    assert r["status"] == "RESUBMISSION_ALLOWED"
    after = sv.deadline(env["p"]) + timedelta(minutes=1)
    assert sv.sweep_deadlines(env["conn"], env["p"], env["admin"], after) == 1
    assert sv.report(env["conn"], u, r["submission_id"])["status"] == "DEADLINE_MISSED"


def test_precedence_configurable(env):
    from datetime import timedelta

    from conftest import do_submit as ds

    a, b = make_team(env, "a"), make_team(env, "b")
    ds(env, a, good_docx(1))
    from conftest import near_copy_docx

    ds(env, b, near_copy_docx(1))  # b now PENDING_REVIEW
    after = sv.deadline(env["p"]) + timedelta(seconds=5)
    assert sv.submission_gate(env["conn"], env["p"], b.team_id, after)[1] == "DEADLINE_PASSED"
    env["p"]["submission"]["resubmission_precedence"] = ["pending_review", "deadline", "attempts_exhausted"]
    assert sv.submission_gate(env["conn"], env["p"], b.team_id, after)[1] == "PENDING"
