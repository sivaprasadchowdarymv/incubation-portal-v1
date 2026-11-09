"""Every user-facing action produces an acknowledgement email where an address is known."""
from datetime import timedelta

import pytest

from conftest import KW, do_submit, good_docx, make_team
from evaluation import synthetic as sy
from workflow import services as sv
from workflow import setup


def notes(env, event):
    return [dict(r) for r in env["conn"].execute("SELECT recipient, subject, body FROM notifications WHERE event_type=?", (event,))]


@pytest.fixture
def admin_mail(env):
    sv.set_user_email(env["conn"], env["admin"], env["admin"].id, "admin@example.org", env["p"])
    return env


def test_student_signup_ack_only_with_email(env):
    sv.signup_student(env["conn"], "with_mail", "password123", "s@example.org", env["p"])
    sv.signup_student(env["conn"], "no_mail", "password123")
    n = notes(env, "signup_ack")
    assert [x["recipient"] for x in n] == ["s@example.org"] and "register your team" in n[0]["body"] and "password123" not in n[0]["body"]
    with pytest.raises(sv.WorkflowError):
        sv.signup_student(env["conn"], "bad_mail", "password123", "nope")


def test_staff_account_and_email_change_acks(admin_mail):
    env = admin_mail
    assert notes(env, "email_confirmed")[0]["recipient"] == "admin@example.org"
    sv.admin_create_user(env["conn"], env["admin"], "rev_ack", "secretpass9", "reviewer", "rev@example.org", env["p"])
    n = notes(env, "account_created")
    assert n[0]["recipient"] == "rev@example.org" and "rev_ack" in n[0]["body"] and "secretpass9" not in n[0]["body"]


def test_refused_upload_ack_no_attempt(env):
    u = make_team(env, "a")
    with pytest.raises(sv.SubmissionBlocked):
        do_submit(env, u, b"hello", "notes.txt")
    n = notes(env, "upload_refused")
    assert n and n[0]["recipient"] == "a@example.org" and "did not count as a submission attempt" in n[0]["body"] and "notes.txt" in n[0]["body"]
    bad = sy.make_docx(sy.abstract(1, 60, KW, omit=("Methodology",)))
    do_submit(env, u, bad)
    with pytest.raises(sv.SubmissionBlocked):
        do_submit(env, u, bad)  # identical re-upload
    assert any("identical to your attempt 1" in x["body"] for x in notes(env, "upload_refused"))


def test_reviewer_gets_copy_of_decision(env):
    from conftest import near_copy_docx

    sv.admin_create_user(env["conn"], env["admin"], "rev_dec", "reviewpass1", "reviewer", "rd@example.org")
    rev = sv.authenticate(env["conn"], "rev_dec", "reviewpass1")
    a, b = make_team(env, "a"), make_team(env, "b")
    do_submit(env, a, good_docx(1))
    rb = do_submit(env, b, near_copy_docx(1))
    sv.decide(env["conn"], env["p"], rev, rb["submission_id"], "cleared", "Independent work, shared template phrases.")
    n = notes(env, "decision_ack")
    assert n[0]["recipient"] == "rd@example.org" and "cleared" in n[0]["body"] and "team has been notified" in n[0]["body"]


def test_deadline_sweep_notifies_teams_and_admin(admin_mail):
    env = admin_mail
    u = make_team(env, "a")
    do_submit(env, u, sy.make_docx(sy.abstract(1, 60, KW, omit=("Methodology",))))
    after = sv.deadline(env["p"]) + timedelta(minutes=1)
    assert sv.sweep_deadlines(env["conn"], env["p"], env["admin"], after) == 1
    assert notes(env, "deadline_missed")[0]["recipient"] == "a@example.org"
    assert "1 team(s)" in notes(env, "sweep_ack")[0]["body"]


def test_admin_setup_changes_acknowledged(admin_mail, tmp_path):
    import shutil

    from config.settings import ROOT

    env = admin_mail
    kb = tmp_path / "kb"
    shutil.copytree(ROOT / "kb", kb)
    setup.update_settings(env["conn"], env["admin"], {"submission.max_file_mb": 12}, env["p"])
    setup.import_catalogue(env["conn"], env["admin"], "c.csv", setup.catalogue_template_csv())
    setup.add_kb_document(env["conn"], env["admin"], kb, "faq2.md", b"# FAQ\nAnswers to common questions.", "1", "faq")
    setup.set_kb_status(env["conn"], env["admin"], kb, "faq2.md", "retired")
    subjects = [n["subject"] for n in notes(env, "setup_ack")]
    assert len(subjects) == 4 and all(n["recipient"] == "admin@example.org" for n in notes(env, "setup_ack"))
    assert any("settings updated" in s for s in subjects) and any("imported" in s for s in subjects)
