"""Tests for the 7 requested checks/features:
1 domain relevance, 2 format/compliance, 3 minimum length, 4 English, 5 similarity with existing PS,
6 duplicate rejection, 7 email acknowledgements/alerts/reminders."""
import dataclasses
import json
import socket
import sys
from datetime import timedelta
from pathlib import Path

import pytest
from aiosmtpd.controller import Controller

from conftest import BEFORE, KW, do_submit, good_docx, make_team, near_copy_docx
from evaluation import synthetic as sy
from notifications import outbox
from notifications.service import send_deadline_reminders, team_recipients
from validation import checks
from workflow import services as sv
from workflow import setup

def chk(rep, cid):
    return next(c for c in rep["checks"] if c["check_id"] == cid)


def doc(sections):
    return sy.make_docx(sections)


TELEMED = ["patient", "rural", "telemedicine", "triage", "clinic", "doctor", "health"]


# ---------------- 1. related to the track (domain) ----------------

def test_domain_relevance_uses_track_and_ps_keywords(env):
    p = env["p"]
    ps = {"id": "AG-01", "title": "Precision irrigation", "keywords": ["irrigation"], "domain_id": "AGRI"}
    track = {"name": "Agritech", "keywords": ["farmer", "crop"]}
    r = checks.relevance_check("Our farmers grow a better crop.", ps, track, p["validation"]["relevance"])
    assert r.result == "pass"  # 'farmers' matches 'farmer' (plural), 'crop' matches
    ev = json.loads(r.evidence)
    assert ev["track_keywords_matched"] == ["farmer", "crop"] and ev["ps_keywords_matched"] == []
    r = checks.relevance_check("A music streaming app for teenagers.", ps, track, p["validation"]["relevance"])
    assert r.result == "warn" and r.review_flag
    r = checks.relevance_check("A music streaming app.", ps, track, {"min_keyword_matches": 2, "action": "fail"})
    assert r.result == "fail" and r.severity == "error"
    assert checks.relevance_check("irrigation only", ps, track, {"min_keyword_matches": 1, "action": "fail"}).result == "pass"


def test_unrelated_abstract_end_to_end(env):
    u = make_team(env, "a")  # AGRI / AG-01
    r = do_submit(env, u, sy.make_docx(sy.abstract(3, 50, ["music", "playlist", "concert"])))
    assert chk(r, "DOMAIN_RELEVANCE")["result"] == "warn" and r["status"] == "PENDING_REVIEW"
    env["p"]["validation"]["relevance"]["action"] = "fail"
    v = make_team(env, "b")
    r = do_submit(env, v, sy.make_docx(sy.abstract(4, 50, ["music", "playlist", "concert"])))
    assert chk(r, "DOMAIN_RELEVANCE")["result"] == "fail" and r["status"] == "RESUBMISSION_ALLOWED"


def test_uploaded_track_keywords_drive_relevance(env):
    csv = (b"track_id,track_name,track_keywords,ps_id,ps_title,keywords\n"
           b"MUS,Music Tech,music; concert; playlist,MU-01,Smart playlists,recommendation\n"
           b"AGRI,Agritech,farm; crop,AG-01,Irrigation,irrigation\n")
    setup.import_catalogue(env["conn"], env["admin"], "c.csv", csv)
    cat = {d["id"]: d for d in sv.catalog(env["conn"])}
    assert cat["MUS"]["keywords"] == ["music", "concert", "playlist"]
    u = make_team(env, "m", ps=("MUS", "MU-01"))
    r = do_submit(env, u, sy.make_docx(sy.abstract(5, 50, ["music", "playlist", "concert"])))
    assert chk(r, "DOMAIN_RELEVANCE")["result"] == "pass"


# ---------------- 2. format / compliance ----------------

def test_new_required_sections_and_inline_headings(env):
    req = env["p"]["validation"]["required_sections"]
    assert list(req) == ["Problem Statement / Motivation", "Impact", "Methodology", "Deliverables / Outcomes"]
    text = ("1. Problem Statement / Motivation\nFarmers waste water.\n2. Impact:\nLess water.\n"
            "Methodology: We will build sensors and test them.\nExpected Outcomes\nA prototype.")
    found, missing, bodies = checks.find_sections(text, req)
    assert missing == [] and bodies["Methodology"].startswith("We will build")
    found, missing, _ = checks.find_sections("Motivation\nx\nApproach\ny", req)
    assert found == ["Problem Statement / Motivation", "Methodology"] and missing == ["Impact", "Deliverables / Outcomes"]
    # a long body sentence starting with a heading word is not a heading
    _, missing, _ = checks.find_sections("Impact of rainfall on yield is studied by many groups in the past decade.", {"Impact": ["impact"]})
    assert missing == ["Impact"]


@pytest.mark.parametrize("maker,name", [(sy.make_docx, "a.docx"), (sy.make_pptx, "a.pptx"), (sy.make_pdf, "a.pdf")])
def test_format_check_all_file_types(env, maker, name):
    u = make_team(env, "a")
    r = do_submit(env, u, maker(sy.abstract(1, 50, KW, omit=("Impact",))), name)
    assert chk(r, "REQUIRED_SECTIONS")["result"] == "fail" and r["missing_sections"] == ["Impact"]


def test_empty_section_fails_content_check(env):
    u = make_team(env, "a")
    secs = sy.abstract(1, 70, KW)
    secs[2] = ("Methodology", "To be decided.")
    r = do_submit(env, u, doc(secs))
    c = chk(r, "SECTION_CONTENT")
    assert c["result"] == "fail" and "Methodology (3 words)" in c["message"] and r["status"] == "RESUBMISSION_ALLOWED"
    env["p"]["validation"]["min_words_per_section"] = 0
    v = make_team(env, "b")
    r2 = do_submit(env, v, doc([(h, b if h != "Methodology" else "To be decided.") for h, b in sy.abstract(2, 70, KW)]))
    assert not any(c["check_id"] == "SECTION_CONTENT" for c in r2["checks"])


# ---------------- 3. minimum length of the complete document ----------------

def test_min_length_counts_whole_document(env):
    p = env["p"]
    ps = {"id": "AG-01", "title": "t", "keywords": KW, "domain_id": "AGRI"}
    short = "\n".join(f"{h}\n" + sy.body(i, 20, KW) for i, h in enumerate(sy.SECTIONS))
    res = {r.check_id: r for r in checks.hard_checks(short, None, p, ps)}
    assert res["MIN_WORDS"].result == "fail" and "complete document" in res["MIN_WORDS"].message


# ---------------- 4. English ----------------

@pytest.mark.parametrize("body,ok", [
    ("The farmers in the village will use the sensor to measure the water in the soil and plan irrigation for each field.", True),
    ("किसान खेत में पानी की कमी से परेशान हैं और फसल की पैदावार घट रही है इसलिए हम सेंसर बनाएंगे", False),
    ("రైతులు నీటి కొరతతో బాధపడుతున్నారు కాబట్టి మేము సెన్సార్ తయారు చేస్తాము", False),
    ("Lorem ipsum dolor sit amet consectetur adipiscing elit sed eiusmod tempor incididunt labore dolore magna", False),
])
def test_english_only(env, body, ok):
    r = checks.language_check(" ".join([body] * 5), env["p"]["validation"]["language"])
    assert (r.result == "pass") is ok


# ---------------- 5. similarity with existing problem statements ----------------

def test_rank_problem_statements(env):
    ps_list = [dict(p) | {"keywords": json.loads(p["keywords"])} for p in env["conn"].execute("SELECT * FROM problem_statements")]
    text = " ".join(sy.body(i, 60, TELEMED) for i in range(3))
    ranked, method = checks.rank_problem_statements(text, ps_list)
    assert method == "tfidf_cosine" and ranked[0]["ps_id"] == "HC-01" and ranked[0]["score"] > ranked[1]["score"]


def test_abstract_closer_to_other_ps_is_flagged(env):
    u = make_team(env, "a")  # selected AG-01 irrigation
    secs = [(h, sy.body(20 + i, 50, TELEMED + ["farm", "water"])) for i, h in enumerate(sy.SECTIONS)]
    r = do_submit(env, u, doc(secs))
    c = chk(r, "PS_SIMILARITY")
    ev = json.loads(c["evidence"])
    assert c["result"] == "warn" and ev["top_matches"][0]["ps_id"] == "HC-01" and ev["selected"]["ps_id"] == "AG-01"
    assert "PS_SIMILARITY" in r["review_reasons"]


def test_matching_ps_passes_and_modes(env):
    u = make_team(env, "a")
    r = do_submit(env, u, good_docx(1))
    c = chk(r, "PS_SIMILARITY")
    assert c["result"] == "pass" and json.loads(c["evidence"])["selected_rank"] == 1
    ps = {"id": "AG-01", "title": "Irrigation", "keywords": KW}
    pl = [ps, {"id": "HC-01", "title": "Rural telemedicine triage", "keywords": TELEMED}]
    text = sy.body(1, 80, TELEMED)
    assert checks.ps_similarity_check(text, ps, pl, {"enabled": True, "margin": 0.05, "action": "fail"}).result == "fail"
    assert checks.ps_similarity_check(text, ps, pl, {"enabled": False}).result == "skip"
    assert checks.ps_similarity_check(text, ps, [ps], {"enabled": True}).result == "skip"
    assert checks.ps_similarity_check(text, ps, pl, {"enabled": True, "margin": 1.0, "action": "fail"}).result == "pass"


# ---------------- 6. duplicate submissions ----------------

def test_exact_copy_of_other_team_is_rejected(env):
    a, b = make_team(env, "a"), make_team(env, "b")
    assert do_submit(env, a, good_docx(1))["status"] == "ACCEPTED_FOR_EVALUATION"
    r = do_submit(env, b, sy.make_pdf(sy.abstract(1, 50, KW)), "copy.pdf")  # same text, different file format
    c = chk(r, "DUPLICATE_SUBMISSION")
    assert c["result"] == "fail" and c["severity"] == "error" and r["status"] == "RESUBMISSION_ALLOWED"
    assert sv.report(env["conn"], env["admin"], 1)["status"] == "ACCEPTED_FOR_EVALUATION"  # original team unaffected


def test_exact_copy_review_mode(env):
    env["p"]["duplicates"]["exact_action"] = "review"
    a, b = make_team(env, "a"), make_team(env, "b")
    do_submit(env, a, good_docx(1))
    r = do_submit(env, b, good_docx(1))
    assert r["status"] == "PENDING_REVIEW" and "DUPLICATE_SUBMISSION" in r["review_reasons"]


def test_near_duplicate_flagged_not_rejected(env):
    a, b = make_team(env, "a"), make_team(env, "b")
    do_submit(env, a, good_docx(1))
    secs = sy.abstract(1, 50, KW)
    secs[0] = (secs[0][0], secs[0][1] + " Additionally we thank our mentor.")  # tiny edit
    r = do_submit(env, b, doc(secs))
    assert r["status"] == "PENDING_REVIEW" and "NEAR_DUPLICATE" in r["review_reasons"]
    assert not any(c["check_id"] == "DUPLICATE_SUBMISSION" for c in r["checks"])


def test_reupload_of_own_previous_attempt_blocked_without_using_attempt(env):
    u = make_team(env, "a")
    bad = sy.make_docx(sy.abstract(1, 60, KW, omit=("Methodology",)))
    assert do_submit(env, u, bad)["status"] == "RESUBMISSION_ALLOWED"
    for data, name in ((bad, "again.docx"), (sy.make_pdf(sy.abstract(1, 60, KW, omit=("Methodology",))), "same_text.pdf")):
        with pytest.raises(sv.SubmissionBlocked) as e:
            do_submit(env, u, data, name)
        assert e.value.code == "DUPLICATE_OF_PREVIOUS_ATTEMPT"
    assert sv.submission_gate(env["conn"], env["p"], u.team_id, BEFORE)[3] == 1
    env["p"]["duplicates"]["block_identical_resubmission"] = False
    assert do_submit(env, u, bad)["attempt_no"] == 2


def test_first_submitter_never_penalised(env):
    a, b = make_team(env, "a"), make_team(env, "b")
    ra = do_submit(env, a, good_docx(1))
    do_submit(env, b, good_docx(1))
    assert sv.report(env["conn"], env["admin"], ra["submission_id"])["status"] == "ACCEPTED_FOR_EVALUATION"


# ---------------- 7. emails ----------------

class _Sink:
    def __init__(self):
        self.msgs = []

    async def handle_DATA(self, server, session, envelope):
        self.msgs.append((envelope.rcpt_tos, envelope.content.decode("utf-8", "replace")))
        return "250 OK"


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


@pytest.fixture
def smtp(env):
    sink, port = _Sink(), _free_port()
    ctl = Controller(sink, hostname="127.0.0.1", port=port)
    ctl.start()
    s = dataclasses.replace(env["s"], smtp_host="127.0.0.1", smtp_port=port, smtp_from="noreply@example.org", smtp_tls=False)
    yield sink, s
    ctl.stop()


def notes(env, event=None):
    q = "SELECT recipient, event_type, subject, body, status FROM notifications" + (" WHERE event_type=?" if event else "")
    return [dict(r) for r in env["conn"].execute(q, (event,) if event else ())]


def test_acknowledgements_go_to_contact_and_members(env):
    from conftest import make_student

    conn, p = env["conn"], env["p"]
    u = make_student(conn, "ack")
    tid = sv.register_team(conn, p, u, "Ack Team", "Lead@Example.org", [{"full_name": "A", "member_ref": "a1", "email": "a@example.org"},
                                                                        {"full_name": "B", "member_ref": "b1", "email": "LEAD@example.org"}])
    assert sorted(team_recipients(conn, p, tid)) == ["a@example.org", "lead@example.org"]  # deduplicated, lower-case
    reg = notes(env, "registration_ack")
    assert len(reg) == 2 and "Membership is now locked" in reg[0]["body"] and "A (a1)" in reg[0]["body"]
    sv.select_problem_statement(conn, sv.refresh_user(conn, u), "AGRI", "AG-01", policy=p)
    lock = notes(env, "ps_lock_ack")
    assert len(lock) == 2 and "AG-01" in lock[0]["body"] and "Agritech" in lock[0]["body"]
    p["notifications"]["notify_members"] = False
    assert team_recipients(conn, p, tid) == ["lead@example.org"]


def test_submission_result_emails_include_guidance(env):
    u = make_team(env, "a")
    do_submit(env, u, sy.make_docx(sy.abstract(1, 60, KW, omit=("Methodology",))))
    assert notes(env, "submission_ack")[0]["subject"].endswith("Submission received (attempt 1 of 3)")
    fail = notes(env, "validation_failed")[0]
    assert "Missing required sections: Methodology" in fail["body"] and "→" in fail["body"] and "2 attempt(s) left" in fail["body"]
    do_submit(env, u, good_docx(3))
    assert "accepted for evaluation" in notes(env, "accepted")[0]["body"]


def test_reviewer_alert_and_decision_email(env):
    sv.admin_create_user(env["conn"], env["admin"], "rev_mail", "reviewpass1", "reviewer", "reviewer@example.org")
    sv.admin_create_user(env["conn"], env["admin"], "rev_nomail", "reviewpass1", "reviewer")
    with pytest.raises(sv.WorkflowError):
        sv.admin_create_user(env["conn"], env["admin"], "rev_bad", "reviewpass1", "reviewer", "not-an-email")
    a, b = make_team(env, "a"), make_team(env, "b")
    do_submit(env, a, good_docx(1))
    rb = do_submit(env, b, near_copy_docx(1))
    alerts = notes(env, "review_alert")
    assert [n["recipient"] for n in alerts] == ["reviewer@example.org"] and "Review Queue" in alerts[0]["body"]
    assert any("not a rejection" in n["body"] for n in notes(env, "pending_review"))
    sv.decide(env["conn"], env["p"], env["admin"], rb["submission_id"], "corrections_requested", "Rewrite the methodology in your own words.")
    d = notes(env, "review_decision")[0]
    assert "CORRECTIONS REQUESTED" in d["body"] and "Rewrite the methodology" in d["body"] and "attempt(s) left" in d["body"]


def test_deadline_reminders(env):
    conn, p = env["conn"], env["p"]
    dl = sv.deadline(p)
    pending = make_team(env, "pend", ps=None)          # not locked
    notsub = make_team(env, "nosub")                    # locked, nothing submitted
    done = make_team(env, "done")
    do_submit(env, done, good_docx(1))                  # accepted -> no reminder
    fixme = make_team(env, "fixme")
    do_submit(env, fixme, sy.make_docx(sy.abstract(7, 60, KW, omit=("Methodology",))))  # must resubmit
    assert send_deadline_reminders(conn, p, dl, dl - timedelta(days=10)) == 0           # before first window
    n = send_deadline_reminders(conn, p, dl, dl - timedelta(hours=70))                  # inside the 72 h window
    teams = {r["recipient"] for r in notes(env, "deadline_reminder")}
    assert n == 3 and teams == {"pend@example.org", "nosub@example.org", "fixme@example.org"}
    bodies = {r["recipient"]: r["body"] for r in notes(env, "deadline_reminder")}
    assert "not yet selected" in bodies["pend@example.org"] and "2 attempt(s) left" in bodies["fixme@example.org"]
    assert send_deadline_reminders(conn, p, dl, dl - timedelta(hours=69)) == 0          # same window: no repeat
    assert send_deadline_reminders(conn, p, dl, dl - timedelta(hours=2)) == 3           # 3 h window: one more each, not 24 h + 3 h
    assert send_deadline_reminders(conn, p, dl, dl + timedelta(minutes=1)) == 0         # after the deadline
    assert len(notes(env, "deadline_reminder")) == 6
    _ = pending, notsub


def test_real_smtp_delivery_and_retry(env, smtp):
    sink, s = smtp
    u = make_team(env, "a")
    do_submit(env, u, good_docx(1))
    res = outbox.deliver_pending(env["conn"], s)
    queued = len(notes(env))
    assert res["sent"] == queued and res["failed"] == 0 and len(sink.msgs) == queued
    rcpts = {r for m in sink.msgs for r in m[0]}
    assert rcpts == {"a@example.org"} and any("Abstract accepted for evaluation" in m[1] for m in sink.msgs)
    assert all(n["status"] == "sent" for n in notes(env))
    assert outbox.deliver_pending(env["conn"], s)["note"] == "Nothing to send."
    ok, msg = outbox.send_test_email(s, "admin@example.org")
    assert ok and sink.msgs[-1][0] == ["admin@example.org"]


def test_smtp_down_then_recovers(env, smtp):
    sink, s = smtp
    outbox.enqueue(env["conn"], "x@example.org", "t", "subj", "body", "k-down")
    down = dataclasses.replace(s, smtp_port=_free_port())  # nothing listening
    r = outbox.deliver_pending(env["conn"], down)
    row = env["conn"].execute("SELECT status, attempts, last_error FROM notifications WHERE dedupe_key='k-down'").fetchone()
    assert r["failed"] == 1 and row["status"] == "failed" and row["attempts"] == 1 and "Connection" in row["last_error"]
    assert outbox.deliver_pending(env["conn"], s)["sent"] == 1 and len(sink.msgs) == 1
    assert not outbox.send_test_email(down, "a@example.org")[0]


def test_failed_messages_stop_after_max_attempts(env):
    outbox.enqueue(env["conn"], "x@example.org", "t", "s", "b", "k-max")
    down = dataclasses.replace(env["s"], smtp_host="127.0.0.1", smtp_port=_free_port(), smtp_from="n@example.org", smtp_tls=False)
    for _ in range(outbox.MAX_ATTEMPTS + 2):
        outbox.deliver_pending(env["conn"], down)
    assert env["conn"].execute("SELECT attempts FROM notifications WHERE dedupe_key='k-max'").fetchone()[0] == outbox.MAX_ATTEMPTS


def test_worker_one_pass(env, smtp):
    sink, s = smtp
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
    import notify_worker

    env["p"]["submission"]["deadline"] = sv.iso(sv.now_utc() + timedelta(hours=2))
    base = dict(env["p"])
    make_team(env, "w")
    out = notify_worker.one_pass(env["conn"], s, base)
    assert out["reminders_queued"] == 1 and out["sent"] >= 1 and any("Reminder: deadline" in m[1] for m in sink.msgs)


# ---------------- admin settings for the new checks ----------------

@pytest.mark.parametrize("changes,msg", [
    ({"validation.relevance": {"min_keyword_matches": 2, "action": "delete"}}, "Relevance"),
    ({"validation.ps_similarity": {"enabled": True, "margin": 3, "action": "review"}}, "margin"),
    ({"duplicates": {"exact_action": "ignore"}}, "exact_action"),
    ({"duplicates": {"exact_action": "fail", "near_duplicate_threshold": 0.3}}, "near-duplicate"),
    ({"notifications": {"reminder_hours_before_deadline": [-1]}}, "Reminder hours"),
    ({"notifications": {"app_url": "portal.local"}}, "http"),
    ({"validation.min_words_per_section": -5}, "negative"),
])
def test_new_settings_validation(env, changes, msg):
    with pytest.raises(sv.WorkflowError, match=msg):
        setup.update_settings(env["conn"], env["admin"], changes, env["p"])


def test_new_settings_apply(env):
    pol = setup.update_settings(env["conn"], env["admin"], {
        "validation.relevance": {"min_keyword_matches": 4, "action": "fail"},
        "duplicates": {"exact_action": "review", "near_duplicate_threshold": None},
        "notifications": {"reminder_hours_before_deadline": [24, 48], "app_url": "https://portal.example.org"}}, env["p"])
    assert pol["validation"]["relevance"] == {"min_keyword_matches": 4, "action": "fail"}
    assert pol["duplicates"]["exact_action"] == "review" and pol["notifications"]["reminder_hours_before_deadline"] == [24, 48]


# ---------------- reviewer reminders and admin alerts ----------------

def _staff(env):
    sv.admin_create_user(env["conn"], env["admin"], "rev_r", "reviewpass1", "reviewer", "rev@example.org")
    sv.set_user_email(env["conn"], env["admin"], env["admin"].id, "admin@example.org")


def test_review_reminders_for_waiting_cases(env):
    from notifications.service import send_review_reminders

    _staff(env)
    a, b = make_team(env, "a"), make_team(env, "b")
    do_submit(env, a, good_docx(1))
    do_submit(env, b, near_copy_docx(1))                      # opens a review case now
    now = sv.now_utc()
    assert send_review_reminders(env["conn"], env["p"], now) == 0                       # not waiting long enough yet
    later = now + timedelta(hours=25)
    assert send_review_reminders(env["conn"], env["p"], later) == 2                     # reviewer + admin
    rem = notes(env, "review_reminder")
    assert {r["recipient"] for r in rem} == {"rev@example.org", "admin@example.org"} and "awaiting decision" in rem[0]["subject"]
    assert send_review_reminders(env["conn"], env["p"], later + timedelta(hours=1)) == 0  # once per day
    assert send_review_reminders(env["conn"], env["p"], later + timedelta(days=1)) == 2   # next day again
    env["p"]["notifications"]["alert_reviewers"] = False
    assert send_review_reminders(env["conn"], env["p"], later + timedelta(days=2)) == 2   # operational reminder is not switched off by alerts toggle
    env["p"]["notifications"]["review_reminder_hours"] = 0
    assert send_review_reminders(env["conn"], env["p"], later + timedelta(days=3)) == 0   # 0 = off


def test_admin_alerted_on_duplicate_rejection(env):
    _staff(env)
    a, b = make_team(env, "a"), make_team(env, "b")
    ra = do_submit(env, a, good_docx(1))
    rb = do_submit(env, b, good_docx(1))
    al = notes(env, "duplicate_alert")
    assert [n["recipient"] for n in al] == ["admin@example.org"]
    assert f"#{rb['submission_id']}" in al[0]["body"] and f"#{ra['submission_id']}" in al[0]["body"]


def test_delivery_failure_alert_once_and_not_recursive(env):
    from notifications.service import alert_delivery_failures

    _staff(env)
    c = env["conn"]
    outbox.enqueue(c, "bounce@example.org", "accepted", "s", "b", "k-fail")
    c.execute("UPDATE notifications SET status='failed', attempts=?, last_error='550 mailbox unavailable' WHERE dedupe_key='k-fail'", (outbox.MAX_ATTEMPTS,))
    assert alert_delivery_failures(c, env["p"]) == 1
    a = notes(env, "delivery_failure_alert")[0]
    assert a["recipient"] == "admin@example.org" and "550 mailbox unavailable" in a["body"]
    assert alert_delivery_failures(c, env["p"]) == 0                                      # only once per message
    c.execute("UPDATE notifications SET status='failed', attempts=? WHERE event_type='delivery_failure_alert'", (outbox.MAX_ATTEMPTS,))
    assert alert_delivery_failures(c, env["p"]) == 0                                      # no alert about a failed alert


def test_worker_sends_review_reminders(env, smtp):
    sink, s = smtp
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
    import notify_worker

    _staff(env)
    a, b = make_team(env, "a"), make_team(env, "b")
    do_submit(env, a, good_docx(1))
    rb = do_submit(env, b, near_copy_docx(1))
    old = sv.iso(sv.now_utc() - timedelta(hours=30))
    env["conn"].execute("UPDATE review_cases SET created_at=? WHERE submission_id=?", (old, rb["submission_id"]))
    out = notify_worker.one_pass(env["conn"], s, dict(env["p"]))
    assert out["review_reminders_queued"] == 2 and out["failed"] == 0
    assert any("awaiting decision" in m[1] for m in sink.msgs)


@pytest.mark.parametrize("hours,expected", [(0, 0), (48, 48), (10000, 336), (-5, 0)])
def test_review_reminder_setting_clamped(env, hours, expected):
    n = dict(env["p"]["notifications"], review_reminder_hours=hours)
    pol = setup.update_settings(env["conn"], env["admin"], {"notifications": n}, env["p"])
    assert pol["notifications"]["review_reminder_hours"] == expected
