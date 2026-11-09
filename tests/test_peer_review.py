"""Conference-style peer review: assignment, conflicts, reviewer file access, rubric reviews, decisions, release, reminders."""
import sqlite3
from datetime import date, timedelta

import pytest

from conftest import BEFORE, do_submit, good_docx, make_team
from security.auth import AuthError
from workflow import evaluation as ev
from workflow import services as sv
from workflow import setup

NOW = BEFORE


def reviewer(env, name, email=None):
    sv.admin_create_user(env["conn"], env["admin"], name, "reviewpass1", "reviewer", email or f"{name}@example.org")
    return sv.authenticate(env["conn"], name, "reviewpass1")


def accepted(env, name, seed):
    t = make_team(env, name)
    r = do_submit(env, t, good_docx(seed))
    assert r["status"] == "ACCEPTED_FOR_EVALUATION", r["status"]
    return t, r["submission_id"]


def full_scores(p, v=4):
    return {c["id"]: v for c in p["evaluation"]["criteria"]}


def outbox(env, event):
    return [dict(r) for r in env["conn"].execute("SELECT recipient, subject, body FROM notifications WHERE event_type=?", (event,))]


def submit_review(env, rv, aid, v=4, rec="accept"):
    return ev.save_review(env["conn"], env["p"], rv, aid, full_scores(env["p"], v), rec, 4,
                          "Clear problem framing; strengthen the methodology with a pilot plan and metrics.", "ok", submit=True, now=NOW)


def test_weighted_total_bounds(env):
    p = env["p"]
    assert ev.weighted_total(p, full_scores(p, 5)) == 100 and ev.weighted_total(p, full_scores(p, 1)) == 0
    assert ev.weighted_total(p, full_scores(p, 3)) == 50


def test_assign_notify_and_file_access(env):
    r1, r2 = reviewer(env, "rev_a"), reviewer(env, "rev_b")
    _, sid = accepted(env, "a", 1)
    with pytest.raises(AuthError):
        sv.submission_file(env["conn"], r1, sid)
    aid = ev.assign(env["conn"], env["p"], env["admin"], sid, r1.id, now=NOW)
    mail = outbox(env, "review_assigned")
    assert mail[0]["recipient"] == "rev_a@example.org" and "P-" in mail[0]["subject"]
    assert "Team a" not in mail[0]["body"]                       # blind review: no team identity
    with pytest.raises(sv.WorkflowError, match="pledge"):
        ev.assignment_file(env["conn"], env["p"], r1, aid, env["s"].storage_dir)
    ev.pledge(env["conn"], r1)
    ev.pledge(env["conn"], r1)  # idempotent
    assert env["conn"].execute("SELECT COUNT(*) FROM audit_events WHERE action='reviewer_confidentiality_pledge'").fetchone()[0] == 1
    name, data = ev.assignment_file(env["conn"], env["p"], r1, aid, env["s"].storage_dir)
    assert name == f"P-{sid:04d}.docx" and data == good_docx(1)
    with pytest.raises(AuthError):                                # another reviewer cannot use this assignment
        ev.assignment_file(env["conn"], env["p"], r2, aid)
    with pytest.raises(sv.WorkflowError):
        ev.assign(env["conn"], env["p"], env["admin"], sid, r1.id, now=NOW)  # already assigned
    rows = ev.my_assignments(env["conn"], env["p"], r1, NOW)
    assert rows[0]["team"].startswith("hidden") and rows[0]["paper"] == f"P-{sid:04d}"
    assert ev.my_assignments(env["conn"], env["p"], r2, NOW) == []
    assert [s["id"] for s in sv.list_submissions(env["conn"], r1)] == [sid] and sv.list_submissions(env["conn"], r2) == []


def test_only_accepted_abstracts_and_students_cannot_assign(env):
    r1 = reviewer(env, "rev_a")
    t, sid = accepted(env, "a", 1)
    with pytest.raises(AuthError):
        ev.assign(env["conn"], env["p"], t, sid, r1.id, now=NOW)
    env["conn"].execute("INSERT INTO submissions(team_id, ps_id, attempt_no, status, file_name, file_sha256, policy_version, submitted_by, submitted_at) "
                        "SELECT team_id, ps_id, 2, 'RESUBMISSION_ALLOWED', 'x.docx', 'h', 'v', submitted_by, submitted_at FROM submissions WHERE id=?", (sid,))
    bad = env["conn"].execute("SELECT MAX(id) FROM submissions").fetchone()[0]
    with pytest.raises(sv.WorkflowError, match="not accepted"):
        ev.assign(env["conn"], env["p"], env["admin"], bad, r1.id, now=NOW)


def test_conflicts_block_assignment(env):
    _, sid = accepted(env, "a", 1)
    team_id = env["conn"].execute("SELECT team_id FROM submissions WHERE id=?", (sid,)).fetchone()[0]
    insider = reviewer(env, "rev_in", "a@example.org")             # same email as the team contact -> automatic COI
    with pytest.raises(sv.WorkflowError, match="conflict"):
        ev.assign(env["conn"], env["p"], env["admin"], sid, insider.id, now=NOW)
    r2 = reviewer(env, "rev_b")
    aid = ev.assign(env["conn"], env["p"], env["admin"], sid, r2.id, now=NOW)
    ev.declare_conflict(env["conn"], r2, r2.id, team_id, "mentor of a member")
    assert env["conn"].execute("SELECT status FROM review_assignments WHERE id=?", (aid,)).fetchone()[0] == "withdrawn"
    with pytest.raises(sv.WorkflowError, match="conflict"):
        ev.assign(env["conn"], env["p"], env["admin"], sid, r2.id, now=NOW)
    r3 = reviewer(env, "rev_c")
    with pytest.raises(AuthError):
        ev.declare_conflict(env["conn"], r3, r2.id, team_id, "not mine to declare")
    assert len(ev.conflicts(env["conn"], env["admin"])) == 1


def test_decline_with_conflict_alerts_admin(env):
    sv.set_user_email(env["conn"], env["admin"], env["admin"].id, "admin@example.org", env["p"])
    r1 = reviewer(env, "rev_a")
    _, sid = accepted(env, "a", 1)
    aid = ev.assign(env["conn"], env["p"], env["admin"], sid, r1.id, now=NOW)
    with pytest.raises(sv.WorkflowError):
        ev.respond(env["conn"], env["p"], r1, aid, accept=False, reason="")
    ev.respond(env["conn"], env["p"], r1, aid, accept=False, reason="I know the team lead", conflict=True)
    assert outbox(env, "assignment_declined")[0]["recipient"] == "admin@example.org"
    assert ev.conflict_reason(env["conn"], r1.id, env["conn"].execute("SELECT team_id FROM submissions WHERE id=?", (sid,)).fetchone()[0])
    with pytest.raises(sv.WorkflowError, match="conflict"):
        ev.assign(env["conn"], env["p"], env["admin"], sid, r1.id, now=NOW)


def test_auto_assign_balances_and_prefers_experts(env):
    revs = [reviewer(env, f"rev_{i}") for i in range(3)]
    ev.set_profile(env["conn"], env["admin"], revs[2].id, ["AGRI"], 8, True)
    subs = [accepted(env, n, s)[1] for n, s in (("a", 1), ("b", 2), ("c", 3))]
    res = ev.auto_assign(env["conn"], env["p"], env["admin"], NOW)
    assert res == {"assigned": 6, "short": []}
    loads = {r["username"]: r["load"] for r in ev.reviewer_pool(env["conn"], env["admin"])}
    assert loads["rev_2"] == 3 and sorted(loads.values()) == [1, 2, 3]          # expert gets every AGRI paper, rest spread evenly
    assert ev.auto_assign(env["conn"], env["p"], env["admin"], NOW)["assigned"] == 0  # idempotent
    ev.set_profile(env["conn"], env["admin"], revs[0].id, [], 0, True)
    assert all(r["assigned"] == 2 for r in ev.evaluable(env["conn"], env["admin"], env["p"]) if r["submission_id"] in subs)


def test_review_draft_submit_revise_lock(env):
    sv.set_user_email(env["conn"], env["admin"], env["admin"].id, "admin@example.org", env["p"])
    env["p"]["evaluation"]["reviewers_per_submission"] = 2
    r1, r2 = reviewer(env, "rev_a"), reviewer(env, "rev_b")
    _, sid = accepted(env, "a", 1)
    a1 = ev.assign(env["conn"], env["p"], env["admin"], sid, r1.id, now=NOW)
    a2 = ev.assign(env["conn"], env["p"], env["admin"], sid, r2.id, now=NOW)
    ev.respond(env["conn"], env["p"], r1, a1, accept=True)
    d = ev.save_review(env["conn"], env["p"], r1, a1, {"relevance": 5}, None, None, "", submit=False, now=NOW)
    assert d["status"] == "draft" and d["total"] is None
    with pytest.raises(sv.WorkflowError, match="score"):
        ev.save_review(env["conn"], env["p"], r1, a1, {"relevance": 5}, "accept", 3, "x" * 50, submit=True, now=NOW)
    with pytest.raises(sv.WorkflowError, match="characters"):
        ev.save_review(env["conn"], env["p"], r1, a1, full_scores(env["p"]), "accept", 3, "too short", submit=True, now=NOW)
    with pytest.raises(sv.WorkflowError):
        ev.save_review(env["conn"], env["p"], r1, a1, full_scores(env["p"], 9), "accept", 3, "x" * 50, submit=True, now=NOW)
    r = submit_review(env, r1, a1, 5, "strong_accept")
    assert r["status"] == "submitted" and r["total"] == 100 and r["revision"] == 1
    assert outbox(env, "review_submitted_ack")[0]["recipient"] == "rev_a@example.org"
    assert ev.peer_reviews_visible_to(env["conn"], env["p"], r2, a2) == []        # must review first
    assert outbox(env, "reviews_complete") == []
    submit_review(env, r2, a2, 2, "weak_reject")
    done = outbox(env, "reviews_complete")
    assert done and "disagree" in done[0]["body"]
    agg = ev.aggregate(env["conn"], env["p"], sid)
    assert agg["count"] == 2 and agg["mean"] == 62.5 and agg["disagreement"]
    assert [x["reviewer"] for x in ev.peer_reviews_visible_to(env["conn"], env["p"], r2, a2)] == ["Reviewer 1", "You"]
    assert submit_review(env, r1, a1, 4)["revision"] == 2                       # revision allowed before decision
    assert env["conn"].execute("SELECT COUNT(*) FROM peer_review_history").fetchone()[0] == 3
    ev.decide(env["conn"], env["p"], env["admin"], sid, "shortlisted", "Well done", now=NOW)
    with pytest.raises(sv.WorkflowError, match="locked"):
        submit_review(env, r1, a1, 3)
    with pytest.raises(sqlite3.IntegrityError):
        env["conn"].execute("DELETE FROM peer_review_history")


def test_decision_release_and_team_view(env):
    env["p"]["evaluation"]["reviewers_per_submission"] = 1
    r1 = reviewer(env, "rev_a")
    t, sid = accepted(env, "a", 1)
    with pytest.raises(sv.WorkflowError, match="review"):
        ev.decide(env["conn"], env["p"], env["admin"], sid, "shortlisted", now=NOW)
    aid = ev.assign(env["conn"], env["p"], env["admin"], sid, r1.id, now=NOW)
    submit_review(env, r1, aid)
    with pytest.raises(sv.WorkflowError):
        ev.decide(env["conn"], env["p"], env["admin"], sid, "maybe", now=NOW)
    d1 = ev.decide(env["conn"], env["p"], env["admin"], sid, "waitlisted", "First pass", now=NOW)
    assert ev.team_result(env["conn"], env["p"], t) is None                     # not released yet
    d2 = ev.decide(env["conn"], env["p"], env["admin"], sid, "shortlisted", "Congratulations!", now=NOW)
    assert ev.release(env["conn"], env["p"], env["admin"], [d1], NOW) == 0     # superseded decisions are never released
    assert ev.release(env["conn"], env["p"], env["admin"], None, NOW) == 1
    assert ev.release(env["conn"], env["p"], env["admin"], None, NOW) == 0     # once only
    mail = outbox(env, "evaluation_result")
    assert mail and "SHORTLISTED" in mail[0]["body"] and "Reviewer 1" in mail[0]["body"] and "rev_a" not in mail[0]["body"]
    assert "Congratulations" in mail[0]["body"] and "/100" not in mail[0]["body"]  # scores hidden by default
    res = ev.team_result(env["conn"], env["p"], t)
    assert res["outcome"] == "shortlisted" and res["comments"] and res["mean_score"] is None
    assert d2 > d1
    with pytest.raises(sqlite3.IntegrityError):
        env["conn"].execute("UPDATE evaluation_decisions SET outcome='x'")
    board = ev.leaderboard(env["conn"], env["p"], env["admin"])
    assert board[0]["rank_in_track"] == 1 and board[0]["decision"] == "shortlisted" and board[0]["released"]


def test_due_and_overdue_reminders(env):
    sv.set_user_email(env["conn"], env["admin"], env["admin"].id, "admin@example.org", env["p"])
    r1 = reviewer(env, "rev_a")
    _, sid = accepted(env, "a", 1)
    ev.assign(env["conn"], env["p"], env["admin"], sid, r1.id, due=(NOW + timedelta(days=3)).date(), now=NOW)
    assert ev.send_due_reminders(env["conn"], env["p"], NOW) == 0
    near = NOW + timedelta(days=2, hours=12)
    assert ev.send_due_reminders(env["conn"], env["p"], near) == 1
    assert ev.send_due_reminders(env["conn"], env["p"], near) == 0             # deduplicated
    late = NOW + timedelta(days=5)
    assert ev.send_due_reminders(env["conn"], env["p"], late) == 2             # reviewer + admin digest
    assert outbox(env, "reviews_overdue")[0]["recipient"] == "admin@example.org"
    rows = ev.all_assignments(env["conn"], env["admin"], late)
    assert "overdue" in rows[0]["status"]
    ev.withdraw(env["conn"], env["admin"], rows[0]["assignment_id"], "reassigning")
    assert ev.send_due_reminders(env["conn"], env["p"], late + timedelta(days=1)) == 0
    with pytest.raises(sv.WorkflowError):
        ev.assign(env["conn"], env["p"], env["admin"], sid, r1.id, due=date(2020, 1, 1), now=NOW)


def test_rubric_settings_editable(env):
    p = env["p"]
    new = dict(p["evaluation"], reviewers_per_submission=3,
               criteria=[{"id": "idea", "name": "Idea", "weight": 60}, {"id": "team", "name": "Team plan", "weight": 40}])
    pol = setup.update_settings(env["conn"], env["admin"], {"evaluation": new}, p)
    assert pol["evaluation"]["reviewers_per_submission"] == 3 and [c["id"] for c in pol["evaluation"]["criteria"]] == ["idea", "team"]
    for bad in ({"criteria": []}, {"criteria": [{"id": "a", "name": "A", "weight": 0}]}, {"reviewers_per_submission": 9},
                {"criteria": [{"id": "a", "name": "A", "weight": 1}, {"id": "a", "name": "B", "weight": 1}]}):
        with pytest.raises(sv.WorkflowError):
            setup.update_settings(env["conn"], env["admin"], {"evaluation": dict(new, **bad)}, p)
