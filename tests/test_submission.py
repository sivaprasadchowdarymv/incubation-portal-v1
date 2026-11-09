import sqlite3
import threading
from datetime import timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from conftest import BEFORE, KW, bad_docx, do_submit, good_docx, make_team
from database.db import connect
from evaluation import synthetic as sy
from workflow import services as sv


def used(env, u):
    return env["conn"].execute("SELECT COUNT(*) FROM submissions WHERE team_id=?", (u.team_id,)).fetchone()[0]


@pytest.mark.parametrize("fmt", ["docx", "pptx", "pdf"])
def test_valid_formats_accepted(env, fmt):
    u = make_team(env, "a")
    data = {"docx": sy.make_docx, "pptx": sy.make_pptx, "pdf": sy.make_pdf}[fmt](sy.abstract(1, 50, KW))
    r = do_submit(env, u, data, f"abs.{fmt}")
    assert r["status"] == "ACCEPTED_FOR_EVALUATION", r["checks"]
    assert r["attempt_no"] == 1


@pytest.mark.parametrize("name,data,code", [
    ("a.txt", b"hello", "INVALID_EXTENSION"),
    ("a.docx", b"", "EMPTY_FILE"),
    ("a.docx", b"%PDF-1.4 not a docx", "MIME_MISMATCH"),
    ("a.pptx", None, "MIME_MISMATCH"),  # a docx renamed to .pptx
])
def test_prechecks_do_not_consume_attempt(env, name, data, code):
    u = make_team(env, "a")
    with pytest.raises(sv.SubmissionBlocked) as e:
        do_submit(env, u, data if data is not None else good_docx(), name)
    assert e.value.code == code
    assert used(env, u) == 0


def test_oversize_blocked(env):
    u = make_team(env, "a")
    env["p"]["submission"]["max_file_mb"] = 0.001
    with pytest.raises(sv.SubmissionBlocked) as e:
        do_submit(env, u, good_docx())
    assert e.value.code == "FILE_TOO_LARGE" and used(env, u) == 0


def test_corrupted_and_empty_content_fail_formally(env):
    u = make_team(env, "a")
    r = do_submit(env, u, b"PK\x03\x04" + b"\x00" * 100)
    assert r["status"] == "RESUBMISSION_ALLOWED" and any(c["check_id"] == "FILE_READABLE" and c["result"] == "fail" for c in r["checks"])
    r2 = do_submit(env, u, sy.make_docx([]))
    assert any(c["check_id"] == "CONTENT_NONEMPTY" and c["result"] == "fail" for c in r2["checks"])
    assert used(env, u) == 2


def test_missing_sections_reported(env):
    u = make_team(env, "a")
    r = do_submit(env, u, bad_docx())
    assert r["missing_sections"] == ["Methodology"]
    assert r["corrective_actions"]


@pytest.mark.parametrize("delta,ok", [(-1, False), (0, True)])
def test_min_words_boundary(env, delta, ok):
    from validation import checks, extract

    u = make_team(env, "a")
    words = env["p"]["validation"]["min_words"] + delta
    # heading words count too: 4 headings with 2+2+1+2 = 7 words
    per = (words - 7)
    secs = sy.abstract(3, 1, KW)
    secs = [(h, sy.body(9 + i, per // 4 + (1 if i < per % 4 else 0), KW)) for i, (h, _) in enumerate(secs)]
    data = sy.make_docx(secs)
    assert len(checks.tokens(extract.extract("a.docx", data).text)) == words
    r = do_submit(env, u, data)
    assert (r["status"] == "ACCEPTED_FOR_EVALUATION") is ok


def test_three_attempts_then_fourth_blocked_and_history_kept(env):
    u = make_team(env, "a")
    r1, r2 = do_submit(env, u, bad_docx(1)), do_submit(env, u, bad_docx(2))
    assert r1["status"] == r2["status"] == "RESUBMISSION_ALLOWED"
    r3 = do_submit(env, u, bad_docx(3))
    assert r3["attempt_no"] == 3 and r3["status"] == "DROPPED"
    with pytest.raises(sv.SubmissionBlocked):
        do_submit(env, u, good_docx(4))
    assert used(env, u) == 3
    hist = sv.list_submissions(env["conn"], u)
    assert sorted(h["attempt_no"] for h in hist) == [1, 2, 3]
    assert sv.report(env["conn"], u, r1["submission_id"])["checks"]  # earlier report preserved


def test_fail_then_pass_on_resubmission(env):
    u = make_team(env, "a")
    assert do_submit(env, u, bad_docx())["status"] == "RESUBMISSION_ALLOWED"
    r = do_submit(env, u, good_docx(7))
    assert r["attempt_no"] == 2 and r["status"] == "ACCEPTED_FOR_EVALUATION"
    with pytest.raises(sv.SubmissionBlocked) as e:
        do_submit(env, u, good_docx(8))
    assert e.value.code == "ALREADY_ACCEPTED"


def test_concurrent_submissions_single_attempt(env):
    u = make_team(env, "a")
    data, results = good_docx(), []

    def worker():
        c = connect(env["s"].db_path)
        try:
            results.append(sv.submit(c, env["p"], env["s"].storage_dir, u, "a.docx", data, now=BEFORE)["attempt_no"])
        except sv.SubmissionBlocked as e:
            results.append(e.code)
        finally:
            c.close()

    ts = [threading.Thread(target=worker) for _ in range(6)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert results.count(1) == 1 and used(env, u) == 1


def test_concurrent_resubmissions_never_exceed_limit(env):
    u = make_team(env, "a")
    do_submit(env, u, bad_docx(1))
    do_submit(env, u, bad_docx(2))
    results = []

    def worker(i):
        c = connect(env["s"].db_path)
        try:
            results.append(sv.submit(c, env["p"], env["s"].storage_dir, u, "a.docx", bad_docx(10 + i), now=BEFORE)["attempt_no"])
        except sv.SubmissionBlocked as e:
            results.append(e.code)
        finally:
            c.close()

    ts = [threading.Thread(target=worker, args=(i,)) for i in range(5)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert used(env, u) == 3 and results.count(3) == 1
    with pytest.raises(sqlite3.IntegrityError):
        env["conn"].execute("INSERT INTO submissions(team_id,ps_id,attempt_no,status,file_name,file_sha256,policy_version,submitted_by,submitted_at) "
                            "SELECT team_id,ps_id,3,'SUBMITTED','x','x','x',submitted_by,submitted_at FROM submissions WHERE team_id=? LIMIT 1", (u.team_id,))


def test_deadline_boundaries_and_timezone(env):
    p = env["p"]
    d = sv.deadline(p)
    assert d.tzinfo == ZoneInfo("Asia/Kolkata") and d.utcoffset() == timedelta(hours=5, minutes=30)
    assert sv.before_deadline(p, d)  # inclusive by default
    assert not sv.before_deadline(p, d + timedelta(seconds=1))
    # same instant expressed in UTC
    assert sv.before_deadline(p, d.astimezone(timezone.utc))
    p["submission"]["deadline_inclusive"] = False
    assert not sv.before_deadline(p, d)
    p["submission"]["deadline_inclusive"] = True
    u = make_team(env, "a")
    with pytest.raises(sv.SubmissionBlocked) as e:
        do_submit(env, u, good_docx(), now=d + timedelta(seconds=1))
    assert e.value.code == "DEADLINE_PASSED" and used(env, u) == 0
    assert do_submit(env, u, good_docx(), now=d)["attempt_no"] == 1


def test_ps_must_be_locked_before_submit(env):
    u = make_team(env, "a", ps=None)
    with pytest.raises(sv.SubmissionBlocked) as e:
        do_submit(env, u, good_docx())
    assert e.value.code == "PS_NOT_LOCKED"


def test_submission_versions_immutable(env):
    u = make_team(env, "a")
    r = do_submit(env, u, good_docx())
    c = env["conn"]
    for sql in ["UPDATE submissions SET file_name='x' WHERE id=?", "UPDATE submissions SET extracted_text='x' WHERE id=?",
                "DELETE FROM submissions WHERE id=?", "UPDATE validation_results SET result='pass' WHERE submission_id=?"]:
        with pytest.raises(sqlite3.IntegrityError):
            c.execute(sql, (r["submission_id"],))
