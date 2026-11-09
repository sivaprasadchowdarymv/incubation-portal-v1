"""End-to-end acceptance run on a SEPARATE synthetic database (never production).
Usage: python scripts/acceptance.py [--db storage/acceptance.db]"""
import argparse
import dataclasses
import json
from datetime import datetime, timedelta, timezone

import _path  # noqa: F401
from config.settings import ROOT, get_settings, load_policy
from database.db import connect
from evaluation import synthetic as sy
from rag.answer import answer
from rag.index import HybridIndex
from rag.ollama import OllamaClient
from workflow import services as sv
from workflow.context import open_app

ap = argparse.ArgumentParser()
ap.add_argument("--db", default="storage/acceptance.db")
a = ap.parse_args()
s0 = get_settings()
db = ROOT / a.db
if db.exists():
    raise SystemExit(f"{db} exists; choose a new --db path (this script never overwrites data).")
s = dataclasses.replace(s0, db_path=db, storage_dir=db.parent / "acceptance_files")
p = load_policy()
conn, s, p = open_app(s, p)
now = min(datetime.now(timezone.utc), sv.deadline(p) - timedelta(days=1))
results = []


def step(name, cond, detail=""):
    results.append({"step": name, "result": "PASS" if cond else "FAIL", "detail": detail})


kw = ["irrigation", "soil moisture", "water", "sensor", "farm"]
sv.bootstrap_admin(conn, "acc_admin", "acc-admin-pass")
admin = sv.authenticate(conn, "acc_admin", "acc-admin-pass")
sv.admin_create_user(conn, admin, "acc_reviewer", "acc-review-pass", "reviewer")
rev = sv.authenticate(conn, "acc_reviewer", "acc-review-pass")
users = {}
for t in ("alpha", "beta", "gamma", "delta"):
    sv.signup_student(conn, f"acc_{t}", "student-pass")
    u = sv.authenticate(conn, f"acc_{t}", "student-pass")
    sv.register_team(conn, p, u, f"Acc {t}", f"{t}@example.org", [{"full_name": f"{t} one", "member_ref": f"{t}-1"}, {"full_name": f"{t} two", "member_ref": f"{t}-2"}])
    sv.select_problem_statement(conn, u, "AGRI", "AG-01", policy=p)
    users[t] = sv.refresh_user(conn, u)
step("6 registration + PS lock", all(r[0] for r in conn.execute("SELECT membership_locked AND ps_locked FROM teams").fetchall()))

r = sv.submit(conn, p, s.storage_dir, users["alpha"], "alpha.pdf", sy.make_pdf(sy.abstract(21, 50, kw)), now=now)
step("7 passing submission (PDF)", r["status"] == "ACCEPTED_FOR_EVALUATION", r["status"])

r1 = sv.submit(conn, p, s.storage_dir, users["gamma"], "gamma.docx", sy.make_docx(sy.abstract(22, 60, kw, omit=("Methodology",))), now=now)
r2 = sv.submit(conn, p, s.storage_dir, users["gamma"], "gamma_v2.pptx", sy.make_pptx(sy.abstract(23, 50, kw)), now=now)
step("8 failed submission then successful resubmission", r1["status"] == "RESUBMISSION_ALLOWED" and r1["missing_sections"] == ["Methodology"]
     and r2["status"] == "ACCEPTED_FOR_EVALUATION" and r2["attempt_no"] == 2, f"{r1['status']} -> {r2['status']}")

near = sy.abstract(21, 50, kw)
near[-1] = (near[-1][0], sy.body(777, 50, kw))  # one section rewritten -> heavy overlap, not identical
rb = sv.submit(conn, p, s.storage_dir, users["beta"], "beta.pdf", sy.make_pdf(near), now=now)
lex = [f["score"] for f in sv.report(conn, rev, rb["submission_id"])["similarity"] if f["method"] == "lexical_shingle_jaccard"][0]
step("9 similarity > 50% -> PENDING_REVIEW (not rejected)", rb["status"] == "PENDING_REVIEW" and lex > 0.5, f"lexical={lex}")
rd = sv.submit(conn, p, s.storage_dir, users["delta"], "delta.docx", sy.make_docx(sy.abstract(21, 50, kw)), now=now)
dup = [c for c in rd["checks"] if c["check_id"] == "DUPLICATE_SUBMISSION"]
step("9b exact copy of another team rejected", rd["status"] == "RESUBMISSION_ALLOWED" and dup and dup[0]["result"] == "fail")
try:
    sv.submit(conn, p, s.storage_dir, users["delta"], "delta_again.docx", sy.make_docx(sy.abstract(21, 50, kw)), now=now)
    again = "ALLOWED"
except sv.SubmissionBlocked as e:
    again = e.code
step("9c re-upload of own previous file blocked (no attempt used)", again == "DUPLICATE_OF_PREVIOUS_ATTEMPT", again)

try:
    sv.decide(conn, p, users["alpha"], rb["submission_id"], "cleared", "student trying to review")
    denied = False
except Exception:
    denied = True
d = sv.decide(conn, p, rev, rb["submission_id"], "corrections_requested", "Text duplicates another team; rewrite in your own words.")
step("10 authorized review decision recorded (student denied)", denied and d["status"] == "RESUBMISSION_ALLOWED" and d["decisions"][0]["outcome"] == "corrections_requested")

sv.submit(conn, p, s.storage_dir, users["beta"], "b2.docx", sy.make_docx(sy.abstract(31, 20, kw)), now=now)
r3 = sv.submit(conn, p, s.storage_dir, users["beta"], "b3.docx", sy.make_docx(sy.abstract(32, 20, kw)), now=now)
try:
    sv.submit(conn, p, s.storage_dir, users["beta"], "b4.docx", sy.make_docx(sy.abstract(33, 50, kw)), now=now)
    fourth = "ALLOWED"
except sv.SubmissionBlocked as e:
    fourth = e.code
step("11 fourth formal submission blocked", r3["attempt_no"] == 3 and r3["status"] == "DROPPED" and fourth in ("ATTEMPTS_EXHAUSTED", "CLOSED"), f"3rd={r3['status']}, 4th={fourth}")

conn.close()
c2 = connect(db)
cnt = c2.execute("SELECT COUNT(*) FROM submissions").fetchone()[0]
dec = c2.execute("SELECT COUNT(*) FROM review_decisions").fetchone()[0]
step("12 persistence after restart", cnt == 7 and dec == 1, f"submissions={cnt}, decisions={dec}")

if HybridIndex.needs_rebuild(s.kb_dir, s.index_dir):
    HybridIndex.build(s.kb_dir, s.index_dir, None)
idx = HybridIndex.load(s.index_dir)
c = OllamaClient.from_settings(s)
st = c.status()
ra = answer("How many submission attempts does a team get?", idx, "student", c if st["chat_ok"] else None, p["rag"])
rn = answer("What is the prize money for the winner?", idx, "student", c if st["chat_ok"] else None, p["rag"])
approved = {c["source"] for c in idx.chunks}
cited = [x["label"].split(" ")[0] for x in ra["sources"]]
step("RAG grounded answer with citations", "submission_policy.md" in cited and set(cited) <= approved, f"status={ra['status']}, mode={ra['mode']}, cited={cited}")
step("RAG abstains on unsupported question", rn["status"] == "abstained")
ev = {r[0] for r in c2.execute("SELECT DISTINCT event_type FROM notifications")}
need = {"registration_ack", "ps_lock_ack", "submission_ack", "validation_failed", "accepted", "pending_review", "review_decision"}
step("emails queued for every workflow event", need <= ev, ", ".join(sorted(ev)))
print(json.dumps({"ollama": {k: st[k] for k in ("reachable", "chat_ok", "embed_ok")}, "db": str(db), "steps": results}, indent=2))
raise SystemExit(0 if all(r["result"] == "PASS" for r in results) else 1)
