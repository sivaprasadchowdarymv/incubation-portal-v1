"""Acknowledgement on successful submission + organiser updates on submitted drafts and announcements."""
import sqlite3

import pytest

from conftest import KW, do_submit, good_docx, make_team
from evaluation import synthetic as sy
from security.auth import AuthError
from workflow import services as sv


def notes(env, event):
    return [dict(r) for r in env["conn"].execute("SELECT recipient, subject, body FROM notifications WHERE event_type=? ORDER BY id", (event,))]


def test_success_acknowledgement_and_result(env):
    u = make_team(env, "a")
    do_submit(env, u, good_docx(1))
    ack, res = notes(env, "submission_ack"), notes(env, "accepted")
    assert ack and "Submission received (attempt 1 of 3)" in ack[0]["subject"]
    assert res and "accepted for evaluation" in res[0]["body"]


def test_update_on_a_submitted_draft(env):
    sv.set_user_email(env["conn"], env["admin"], env["admin"].id, "admin@example.org", env["p"])
    a, b = make_team(env, "a"), make_team(env, "b")
    r = do_submit(env, a, good_docx(1))
    mid = sv.send_submission_update(env["conn"], env["p"], env["admin"], r["submission_id"], "Feedback on your draft",
                                    "Strengthen the Methodology", "Please add details of the sensor calibration and the pilot village.")
    mails = notes(env, "organizer_update")
    assert {m["recipient"] for m in mails} == {"a@example.org"}
    assert "Feedback on your draft: Strengthen the Methodology" in mails[0]["subject"]
    assert "sensor calibration" in mails[0]["body"] and "attempt 1" in mails[0]["body"] and "History" in mails[0]["body"]
    assert notes(env, "update_sent_ack")[0]["recipient"] == "admin@example.org"     # sender gets a confirmation
    msgs = sv.team_messages(env["conn"], a)
    assert [m["id"] for m in msgs] == [mid] and msgs[0]["submission_id"] == r["submission_id"]
    assert sv.team_messages(env["conn"], b) == []                                    # other teams never see it
    with pytest.raises(AuthError):
        sv.team_messages(env["conn"], b, team_id=a.team_id)
    with pytest.raises(sqlite3.IntegrityError):
        env["conn"].execute("DELETE FROM team_messages")                              # permanent record


def test_reviewer_can_send_student_cannot(env):
    sv.admin_create_user(env["conn"], env["admin"], "rev_u", "reviewpass1", "reviewer", "rev@example.org")
    rev = sv.authenticate(env["conn"], "rev_u", "reviewpass1")
    a = make_team(env, "a")
    r = do_submit(env, a, good_docx(1))
    assert sv.send_submission_update(env["conn"], env["p"], rev, r["submission_id"], "Update", "Received", "Your draft is with the jury now.")
    with pytest.raises(AuthError):
        sv.send_submission_update(env["conn"], env["p"], a, r["submission_id"], "Update", "Hello", "I am a student trying this.")
    with pytest.raises(AuthError):
        sv.send_announcement(env["conn"], env["p"], rev, "all", "Announcement", "Hi all", "Only admins may announce.")


@pytest.mark.parametrize("cat,subj,body,msg", [
    ("Update", "", "A long enough message body.", "subject"),
    ("Update", "Subject ok", "short", "message"),
    ("Spam", "Subject ok", "A long enough message body.", "type"),
])
def test_update_validation(env, cat, subj, body, msg):
    a = make_team(env, "a")
    r = do_submit(env, a, good_docx(1))
    with pytest.raises(sv.WorkflowError, match=msg):
        sv.send_submission_update(env["conn"], env["p"], env["admin"], r["submission_id"], cat, subj, body)


def test_announcements_by_audience(env):
    acc, fix = make_team(env, "acc"), make_team(env, "fix")
    make_team(env, "none", ps=("HLTH", "HC-01"))
    do_submit(env, acc, good_docx(1))
    do_submit(env, fix, sy.make_docx(sy.abstract(2, 60, KW, omit=("Methodology",))))
    codes = lambda aud: {t["name"] for t in sv.teams_for_audience(env["conn"], env["admin"], aud)}  # noqa: E731
    assert codes("all") == {"Team acc", "Team fix", "Team none"}
    assert codes("accepted") == {"Team acc"} and codes("needs_resubmission") == {"Team fix"}
    assert codes("not_submitted") == {"Team none"} and codes("track:HLTH") == {"Team none"}
    n = sv.send_announcement(env["conn"], env["p"], env["admin"], "needs_resubmission", "Action required", "Resubmit by Friday",
                             "Teams that must correct their abstract: please resubmit by Friday 5 PM.")
    assert n == 1 and {m["recipient"] for m in notes(env, "organizer_update")} == {"fix@example.org"}
    assert sv.team_messages(env["conn"], fix)[0]["subject"] == "Resubmit by Friday" and sv.team_messages(env["conn"], acc) == []
    with pytest.raises(sv.WorkflowError, match="No teams"):
        sv.send_announcement(env["conn"], env["p"], env["admin"], "pending_review", "Announcement", "Nobody", "There is nobody in this group.")
