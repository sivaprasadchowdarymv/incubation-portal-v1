import sqlite3

import pytest

from conftest import do_submit, good_docx, make_team, near_copy_docx
from security.auth import AuthError
from validation import checks
from workflow import services as sv


@pytest.mark.parametrize("score,flag", [(0.49, False), (0.50, False), (0.5000001, True), (0.51, True), (None, False)])
def test_threshold_strictly_greater(score, flag):
    assert checks.exceeds_threshold(score, 0.5) is flag


def _corpus_case(policy, a_text, b_text):
    corpus = [{"id": 1, "team_id": 1, "extracted_text": a_text, "text_hash": checks.normalize_hash(a_text)}]
    return checks.similarity_assessment(b_text, 2, corpus, policy)


def test_exactly_fifty_percent_not_flagged(env):
    p = env["p"]
    p["similarity"]["shingle_size"] = 1
    # unigram sets: A={a,b,c}, B={a,b,c,d,e,f}? -> 3/6 = 0.5 exactly
    f, r = _corpus_case(p, "alpha beta gamma", "alpha beta gamma delta epsilon zeta")
    lex = [x for x in f if x["method"] == "lexical_shingle_jaccard"][0]
    assert lex["score"] == 0.5 and not lex["flagged"]
    f, r = _corpus_case(p, "alpha beta gamma delta", "alpha beta gamma delta epsilon")  # 4/5 = 0.8
    assert [x for x in f if x["method"] == "lexical_shingle_jaccard"][0]["flagged"]


def test_no_corpus_reported_honestly(env):
    f, r = checks.similarity_assessment("some text here", 1, [], env["p"])
    assert f[0]["score"] is None and r[0].result == "skip"


def _copied_case(env):
    a, b = make_team(env, "a"), make_team(env, "b")
    do_submit(env, a, good_docx(1))
    rb = do_submit(env, b, near_copy_docx(1))  # heavily overlapping (not identical) content from another team
    return a, b, rb


def test_high_similarity_goes_to_review_not_rejection(env):
    a, b, rb = _copied_case(env)
    assert rb["status"] == "PENDING_REVIEW"
    assert "SIMILARITY" in rb["review_reasons"] and "DUPLICATE_SUBMISSION" not in rb["review_reasons"]
    assert all(s["compared_submission_id"] is None for s in rb["similarity"])  # redacted for students
    with pytest.raises(sv.SubmissionBlocked) as e:
        do_submit(env, b, good_docx(5))
    assert e.value.code == "PENDING"


def test_unauthorized_review_denied_and_audited(env):
    a, b, rb = _copied_case(env)
    with pytest.raises(AuthError):
        sv.decide(env["conn"], env["p"], b, rb["submission_id"], "cleared", "looks fine to me")
    assert env["conn"].execute("SELECT COUNT(*) FROM audit_events WHERE action='authorization_denied'").fetchone()[0] >= 1
    with pytest.raises(AuthError):
        sv.review_queue(env["conn"], a)


def test_reviewer_clears(env):
    a, b, rb = _copied_case(env)
    sv.admin_create_user(env["conn"], env["admin"], "rev1", "reviewpass1", "reviewer")
    rev = sv.authenticate(env["conn"], "rev1", "reviewpass1")
    q = sv.review_queue(env["conn"], rev)
    assert [x["submission_id"] for x in q] == [rb["submission_id"]] and 0.5 < q[0]["max_lexical"] < 1.0
    with pytest.raises(sv.WorkflowError, match="reason"):
        sv.decide(env["conn"], env["p"], rev, rb["submission_id"], "cleared", "  ")
    r = sv.decide(env["conn"], env["p"], rev, rb["submission_id"], "cleared", "Shared template text only; idea differs.")
    assert r["status"] == "ACCEPTED_FOR_EVALUATION" and r["decisions"][0]["outcome"] == "cleared"
    d = env["conn"].execute("SELECT * FROM review_decisions").fetchone()
    assert d["reviewer_id"] == rev.id and d["evidence"] and d["decided_at"]
    with pytest.raises(sqlite3.IntegrityError):
        env["conn"].execute("UPDATE review_decisions SET outcome='rejected'")
    with pytest.raises(sv.WorkflowError):
        sv.decide(env["conn"], env["p"], rev, rb["submission_id"], "cleared", "second decision attempt")


def test_corrections_requested_allows_resubmission(env):
    a, b, rb = _copied_case(env)
    r = sv.decide(env["conn"], env["p"], env["admin"], rb["submission_id"], "corrections_requested", "Please rewrite in your own words.")
    assert r["status"] == "RESUBMISSION_ALLOWED"
    assert do_submit(env, b, good_docx(42))["attempt_no"] == 2


def test_rejection_requires_approved_policy(env):
    a, b, rb = _copied_case(env)
    with pytest.raises(sv.WorkflowError, match="approved rejection policy"):
        sv.decide(env["conn"], env["p"], env["admin"], rb["submission_id"], "rejected", "Copied from team a verbatim.")
    env["p"]["review"]["approved_rejection_policies"] = [{"id": "ORIG-1", "title": "Verbatim copying confirmed by reviewer"}]
    with pytest.raises(sv.WorkflowError):
        sv.decide(env["conn"], env["p"], env["admin"], rb["submission_id"], "rejected", "Copied from team a verbatim.", "NOT-APPROVED")
    r = sv.decide(env["conn"], env["p"], env["admin"], rb["submission_id"], "rejected", "Copied from team a verbatim.", "ORIG-1")
    assert r["status"] == "REJECTED_BY_AUTHORIZED_REVIEW"


def test_low_relevance_routes_to_review(env):
    from evaluation import synthetic as sy

    u = make_team(env, "a", ps=("HLTH", "HC-01"))
    r = do_submit(env, u, sy.make_docx(sy.abstract(1, 50, ["irrigation", "water"])))
    assert r["status"] == "PENDING_REVIEW" and set(r["review_reasons"]) == {"DOMAIN_RELEVANCE", "PS_SIMILARITY"}
