"""Peer review (evaluation) of accepted abstracts, run the way technical conferences do it.

Flow:  ACCEPTED_FOR_EVALUATION abstract -> admin assigns reviewers (manual or automatic, load-balanced,
       expertise-matched, conflict-of-interest aware) -> reviewer accepts/declines -> downloads the file ->
       scores a weighted rubric + recommendation + confidence + comments -> admin sees all reviews side by side
       (mean, spread, disagreement flag) -> records a decision (shortlisted / waitlisted / not shortlisted) ->
       releases results to teams (one at a time or all at once) with anonymised reviewer comments.

Rules: reviewers only see the abstracts assigned to them; with blind review they never see team names;
       reviews can be revised until a decision exists; decisions and submitted-review snapshots are permanent."""
from __future__ import annotations

import json
import statistics
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from database.db import tx
from notifications import service as notify
from security.auth import AuthError, User
from workflow.services import WorkflowError, audit, authorize, iso, now_utc, submission_file

ACTIVE = ("assigned", "accepted", "submitted")
OPEN = ("assigned", "accepted")
REC_LABEL = {"strong_accept": "Strong accept", "accept": "Accept", "weak_accept": "Weak accept",
             "weak_reject": "Weak reject", "reject": "Reject"}
REC_POSITIVE = {"strong_accept", "accept", "weak_accept"}
OUTCOME_LABEL = {"shortlisted": "Shortlisted for the next phase", "waitlisted": "Waitlisted",
                 "not_shortlisted": "Not shortlisted"}


def paper_code(submission_id: int) -> str:
    return f"P-{int(submission_id):04d}"


def cfg(policy: dict) -> dict:
    return policy.get("evaluation", {})


# ---------------- settings ----------------

def clean_evaluation_settings(v: dict) -> dict:
    crit = []
    seen = set()
    for c in v.get("criteria", []):
        cid = str(c.get("id") or c.get("name", "")).strip().lower().replace(" ", "_")[:30]
        name = str(c.get("name", "")).strip()
        w = float(c.get("weight", 0) or 0)
        if not cid or not name:
            continue
        if cid in seen:
            raise WorkflowError(f"Criterion id '{cid}' is used twice")
        if w <= 0:
            raise WorkflowError(f"Criterion '{name}' needs a weight above 0")
        seen.add(cid)
        crit.append({"id": cid, "name": name[:80], "weight": w, "guide": str(c.get("guide", "") or "").strip()[:300]})
    if not crit:
        raise WorkflowError("The rubric needs at least one criterion")
    lo, hi = int(v.get("score_min", 1)), int(v.get("score_max", 5))
    if not 0 <= lo < hi <= 10:
        raise WorkflowError("Score scale must be like 1 to 5 (0-10 allowed)")
    k = int(v.get("reviewers_per_submission", 2))
    days = int(v.get("review_days", 7))
    if not 1 <= k <= 5:
        raise WorkflowError("Reviewers per abstract must be between 1 and 5")
    if not 1 <= days <= 60:
        raise WorkflowError("Review period must be between 1 and 60 days")
    hrs = sorted({int(h) for h in v.get("reminder_hours_before_due", [48, 24]) if int(h) > 0})
    return {"reviewers_per_submission": k, "review_days": days, "blind_review": bool(v.get("blind_review", True)),
            "share_scores_with_team": bool(v.get("share_scores_with_team", False)),
            "min_comment_chars": max(0, min(int(v.get("min_comment_chars", 40)), 2000)),
            "disagreement_spread": max(1.0, min(float(v.get("disagreement_spread", 25)), 100.0)),
            "reminder_hours_before_due": hrs, "score_min": lo, "score_max": hi, "criteria": crit,
            "recommendations": list(REC_LABEL), "outcomes": list(OUTCOME_LABEL)}


# ---------------- helpers ----------------

def _parse(ts: str) -> datetime:
    d = datetime.fromisoformat(ts)
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _fmt(ts: str | None) -> str:
    return _parse(ts).strftime("%d %b %Y, %H:%M UTC") if ts else "-"


def _is_staff_reviewer(conn, uid: int):
    r = conn.execute("SELECT id, username, email, role, active FROM users WHERE id=?", (uid,)).fetchone()
    if not r or r["role"] != "reviewer" or not r["active"]:
        raise WorkflowError("Choose an active user with the reviewer role.")
    return r


def _sub(conn, sid: int):
    s = conn.execute("SELECT s.*, t.team_code, t.name AS team_name, t.domain_id, d.name AS track, p.title AS ps_title "
                     "FROM submissions s JOIN teams t ON t.id=s.team_id LEFT JOIN domains d ON d.id=t.domain_id "
                     "LEFT JOIN problem_statements p ON p.id=s.ps_id WHERE s.id=?", (sid,)).fetchone()
    if not s:
        raise WorkflowError("Submission not found")
    return s


def _require_evaluable(s) -> None:
    if s["status"] != "ACCEPTED_FOR_EVALUATION":
        raise WorkflowError(f"{paper_code(s['id'])} is not accepted for evaluation (status {s['status']}); only accepted abstracts are peer-reviewed.")


def _latest_decision(conn, sid: int):
    return conn.execute("SELECT d.*, r.released_at FROM evaluation_decisions d LEFT JOIN evaluation_releases r ON r.decision_id=d.id "
                        "WHERE d.submission_id=? ORDER BY d.id DESC LIMIT 1", (sid,)).fetchone()


def profile(conn, uid: int) -> dict:
    r = conn.execute("SELECT * FROM reviewer_profiles WHERE user_id=?", (uid,)).fetchone()
    return {"expertise": json.loads(r["expertise"]), "max_load": r["max_load"], "available": bool(r["available"])} if r \
        else {"expertise": [], "max_load": 8, "available": True}


def load(conn, uid: int) -> int:
    return conn.execute(f"SELECT COUNT(*) FROM review_assignments WHERE reviewer_id=? AND status IN ({','.join('?' * len(ACTIVE))})",
                        (uid, *ACTIVE)).fetchone()[0]


def conflict_reason(conn, reviewer_id: int, team_id: int) -> str | None:
    """Declared conflict, or an automatic one when the reviewer's email belongs to the team."""
    r = conn.execute("SELECT reason FROM reviewer_conflicts WHERE reviewer_id=? AND team_id=?", (reviewer_id, team_id)).fetchone()
    if r:
        return r["reason"]
    em = conn.execute("SELECT lower(email) FROM users WHERE id=?", (reviewer_id,)).fetchone()[0]
    if em:
        team_emails = {e for (e,) in conn.execute("SELECT lower(contact_email) FROM teams WHERE id=? UNION SELECT lower(email) FROM team_members "
                                                   "WHERE team_id=? AND email IS NOT NULL", (team_id, team_id))}
        if em in team_emails:
            return "reviewer's email belongs to this team"
    return None


def weighted_total(policy: dict, scores: dict) -> float:
    c = cfg(policy)
    lo, hi = c["score_min"], c["score_max"]
    tw = sum(x["weight"] for x in c["criteria"])
    return round(100 * sum(x["weight"] * (float(scores[x["id"]]) - lo) / (hi - lo) for x in c["criteria"]) / tw, 1)


# ---------------- reviewer pool ----------------

def reviewer_pool(conn, admin: User) -> list[dict]:
    authorize(conn, admin, "admin", action="reviewer_pool")
    out = []
    for r in conn.execute("SELECT id, username, email, active FROM users WHERE role='reviewer' ORDER BY username"):
        p = profile(conn, r["id"])
        done = conn.execute("SELECT COUNT(*) FROM review_assignments WHERE reviewer_id=? AND status='submitted'", (r["id"],)).fetchone()[0]
        out.append({"id": r["id"], "username": r["username"], "email": r["email"] or "", "active": bool(r["active"]),
                    "available": p["available"], "expertise": p["expertise"], "max_load": p["max_load"],
                    "load": load(conn, r["id"]), "reviews_done": done})
    return out


def set_profile(conn, admin: User, reviewer_id: int, expertise: list[str], max_load: int, available: bool) -> None:
    authorize(conn, admin, "admin", action="reviewer_profile")
    _is_staff_reviewer(conn, reviewer_id)
    valid = {r[0] for r in conn.execute("SELECT id FROM domains")}
    exp = sorted({e for e in expertise if e in valid})
    if int(max_load) < 0:
        raise WorkflowError("Maximum load cannot be negative")
    conn.execute("INSERT INTO reviewer_profiles(user_id, expertise, max_load, available, updated_at) VALUES (?,?,?,?,?) "
                 "ON CONFLICT(user_id) DO UPDATE SET expertise=excluded.expertise, max_load=excluded.max_load, "
                 "available=excluded.available, updated_at=excluded.updated_at",
                 (reviewer_id, json.dumps(exp), int(max_load), int(bool(available)), iso(now_utc())))
    audit(conn, admin, "reviewer_profile_updated", "user", reviewer_id, {"expertise": exp, "max_load": int(max_load), "available": bool(available)})


def declare_conflict(conn, actor: User, reviewer_id: int, team_id: int, reason: str) -> None:
    """Admin for anyone; a reviewer for themselves. Open assignments with that team are withdrawn."""
    authorize(conn, actor, "admin", "reviewer", action="declare_conflict")
    if actor.role == "reviewer" and actor.id != reviewer_id:
        raise AuthError("Reviewers can only declare their own conflicts.")
    reason = (reason or "").strip()
    if len(reason) < 5:
        raise WorkflowError("Give a short reason for the conflict (e.g. 'same department', 'mentor of a member').")
    _is_staff_reviewer(conn, reviewer_id)
    if not conn.execute("SELECT 1 FROM teams WHERE id=?", (team_id,)).fetchone():
        raise WorkflowError("Team not found")
    with tx(conn):
        conn.execute("INSERT OR IGNORE INTO reviewer_conflicts(reviewer_id, team_id, reason, declared_by, declared_at) VALUES (?,?,?,?,?)",
                     (reviewer_id, team_id, reason[:300], actor.id, iso(now_utc())))
        conn.execute("UPDATE review_assignments SET status='withdrawn', note=?, responded_at=? WHERE reviewer_id=? AND status IN ('assigned','accepted') "
                     "AND submission_id IN (SELECT id FROM submissions WHERE team_id=?)", ("conflict of interest", iso(now_utc()), reviewer_id, team_id))
        audit(conn, actor, "conflict_declared", "team", team_id, {"reviewer_id": reviewer_id})


def conflicts(conn, admin: User) -> list[dict]:
    authorize(conn, admin, "admin", action="list_conflicts")
    return [dict(r) for r in conn.execute("SELECT c.id, u.username AS reviewer, t.team_code, t.name AS team_name, c.reason, c.declared_at "
                                           "FROM reviewer_conflicts c JOIN users u ON u.id=c.reviewer_id JOIN teams t ON t.id=c.team_id ORDER BY c.id DESC")]


# ---------------- assignment ----------------

def evaluable(conn, admin: User, policy: dict) -> list[dict]:
    """Every accepted abstract with its review progress, scores and decision (admin overview)."""
    authorize(conn, admin, "admin", action="evaluation_overview")
    k = cfg(policy)["reviewers_per_submission"]
    out = []
    for s in conn.execute("SELECT s.id, s.team_id, s.ps_id, s.attempt_no, s.file_name, s.submitted_at, t.team_code, t.name AS team_name, "
                          "t.domain_id, d.name AS track, p.title AS ps_title FROM submissions s JOIN teams t ON t.id=s.team_id "
                          "LEFT JOIN domains d ON d.id=t.domain_id LEFT JOIN problem_statements p ON p.id=s.ps_id "
                          "WHERE s.status='ACCEPTED_FOR_EVALUATION' ORDER BY s.id"):
        rows = conn.execute("SELECT a.status, u.username, a.due_at FROM review_assignments a JOIN users u ON u.id=a.reviewer_id "
                            "WHERE a.submission_id=? ORDER BY a.id", (s["id"],)).fetchall()
        agg = aggregate(conn, policy, s["id"])
        dec = _latest_decision(conn, s["id"])
        active = [r for r in rows if r["status"] in ACTIVE]
        out.append({"submission_id": s["id"], "paper": paper_code(s["id"]), "team_code": s["team_code"], "team_name": s["team_name"],
                    "domain_id": s["domain_id"], "track": s["track"], "ps_id": s["ps_id"], "ps_title": s["ps_title"],
                    "reviewers": ", ".join(f"{r['username']} ({r['status']})" for r in rows) or "—",
                    "assigned": len(active), "needed": max(0, k - len(active)), "reviews_done": agg["count"],
                    "mean_score": agg["mean"], "spread": agg["spread"], "disagreement": agg["disagreement"],
                    "recommendations": agg["recommendations"],
                    "decision": dec["outcome"] if dec else "", "released": bool(dec and dec["released_at"])})
    return out


def assign(conn, policy: dict, admin: User, submission_id: int, reviewer_id: int, due: date | None = None,
           now: datetime | None = None, _notify: bool = True) -> int:
    authorize(conn, admin, "admin", action="assign_reviewer")
    now = now or now_utc()
    s = _sub(conn, submission_id)
    _require_evaluable(s)
    rv = _is_staff_reviewer(conn, reviewer_id)
    why = conflict_reason(conn, reviewer_id, s["team_id"])
    if why:
        raise WorkflowError(f"{rv['username']} has a conflict of interest with this team ({why}).")
    if _latest_decision(conn, submission_id):
        raise WorkflowError("A decision already exists for this abstract.")
    due_at = (datetime.combine(due, datetime.max.time().replace(microsecond=0), tzinfo=timezone.utc) if due
              else now + timedelta(days=cfg(policy)["review_days"]))
    if due_at <= now:
        raise WorkflowError("The review due date must be in the future.")
    old = conn.execute("SELECT id, status FROM review_assignments WHERE submission_id=? AND reviewer_id=?", (submission_id, reviewer_id)).fetchone()
    with tx(conn):
        if old and old["status"] in ACTIVE:
            raise WorkflowError(f"{rv['username']} is already assigned to {paper_code(submission_id)}.")
        if old and old["status"] == "declined":
            raise WorkflowError(f"{rv['username']} declined {paper_code(submission_id)}; choose another reviewer.")
        if old:  # withdrawn earlier -> re-activate
            conn.execute("UPDATE review_assignments SET status='assigned', due_at=?, assigned_at=?, assigned_by=?, responded_at=NULL, note=NULL WHERE id=?",
                         (iso(due_at), iso(now), admin.id, old["id"]))
            aid = old["id"]
        else:
            aid = conn.execute("INSERT INTO review_assignments(submission_id, reviewer_id, assigned_by, status, due_at, assigned_at) VALUES (?,?,?,?,?,?)",
                               (submission_id, reviewer_id, admin.id, "assigned", iso(due_at), iso(now))).lastrowid
        audit(conn, admin, "reviewer_assigned", "submission", submission_id, {"reviewer_id": reviewer_id, "due_at": iso(due_at)})
    if _notify:
        blind = cfg(policy).get("blind_review", True)
        notify.notify_user(conn, policy, reviewer_id, "review_assigned", f"New abstract to review: {paper_code(submission_id)} (due {due_at.strftime('%d %b %Y')})",
                           [f"You have been assigned abstract {paper_code(submission_id)} for evaluation.",
                            f"Track: {s['track'] or '-'} · Problem statement: {s['ps_id']} {s['ps_title'] or ''}",
                            *([] if blind else [f"Team: {s['team_code']}"]),
                            f"Review due: {due_at.strftime('%d %b %Y, %H:%M UTC')}", "",
                            "Sign in → 📝 My Reviews: accept the assignment (or decline / declare a conflict of interest),",
                            "download the abstract, score it against the rubric and submit your comments."],
                           f"assigned:{aid}:{iso(now)}")
    return aid


def auto_assign(conn, policy: dict, admin: User, now: datetime | None = None) -> dict:
    """Fill every accepted abstract up to N reviewers: no conflicts, available, under max load,
    track experts first, then the least-loaded reviewer (fair spread)."""
    authorize(conn, admin, "admin", action="auto_assign")
    now = now or now_utc()
    pool = [r for r in reviewer_pool(conn, admin) if r["active"] and r["available"]]
    loads = {r["id"]: r["load"] for r in pool}
    made, short = 0, []
    for s in evaluable(conn, admin, policy):
        if s["decision"] or s["needed"] <= 0:
            continue
        taken = {r[0] for r in conn.execute("SELECT reviewer_id FROM review_assignments WHERE submission_id=?", (s["submission_id"],))}
        cands = [r for r in pool if r["id"] not in taken and loads[r["id"]] < r["max_load"]
                 and not conflict_reason(conn, r["id"], conn.execute("SELECT team_id FROM submissions WHERE id=?", (s["submission_id"],)).fetchone()[0])]
        cands.sort(key=lambda r: (0 if s["domain_id"] in r["expertise"] else 1, loads[r["id"]], r["id"]))
        for r in cands[:s["needed"]]:
            assign(conn, policy, admin, s["submission_id"], r["id"], now=now)
            loads[r["id"]] += 1
            made += 1
        if len(cands) < s["needed"]:
            short.append(s["paper"])
    audit(conn, admin, "auto_assign", details={"assigned": made, "short": short})
    return {"assigned": made, "short": short}


def withdraw(conn, admin: User, assignment_id: int, reason: str = "") -> None:
    authorize(conn, admin, "admin", action="withdraw_assignment")
    a = conn.execute("SELECT * FROM review_assignments WHERE id=?", (assignment_id,)).fetchone()
    if not a or a["status"] not in OPEN:
        raise WorkflowError("Only assignments that are not yet reviewed can be withdrawn.")
    conn.execute("UPDATE review_assignments SET status='withdrawn', note=?, responded_at=? WHERE id=?", (reason[:200] or "withdrawn by admin", iso(now_utc()), assignment_id))
    audit(conn, admin, "assignment_withdrawn", "submission", a["submission_id"], {"reviewer_id": a["reviewer_id"], "reason": reason})
    notify.notify_user(conn, None, a["reviewer_id"], "assignment_withdrawn", f"Assignment withdrawn: {paper_code(a['submission_id'])}",
                       [f"Your review assignment for {paper_code(a['submission_id'])} has been withdrawn by the organisers. No action is needed."],
                       f"withdrawn:{assignment_id}:{iso(now_utc())}")


def all_assignments(conn, admin: User, now: datetime | None = None) -> list[dict]:
    authorize(conn, admin, "admin", action="list_assignments")
    now = now or now_utc()
    out = []
    for r in conn.execute("SELECT a.*, u.username AS reviewer, t.team_code FROM review_assignments a JOIN users u ON u.id=a.reviewer_id "
                          "JOIN submissions s ON s.id=a.submission_id JOIN teams t ON t.id=s.team_id ORDER BY a.due_at"):
        overdue = r["status"] in OPEN and _parse(r["due_at"]) < now
        out.append({"assignment_id": r["id"], "paper": paper_code(r["submission_id"]), "submission_id": r["submission_id"], "team_code": r["team_code"],
                    "reviewer": r["reviewer"], "status": r["status"] + (" ⚠ overdue" if overdue else ""), "due": _fmt(r["due_at"]),
                    "assigned": _fmt(r["assigned_at"]), "note": r["note"] or ""})
    return out


# ---------------- reviewer side ----------------

def _own_assignment(conn, reviewer: User, assignment_id: int):
    authorize(conn, reviewer, "reviewer", action="review_assignment")
    a = conn.execute("SELECT * FROM review_assignments WHERE id=?", (assignment_id,)).fetchone()
    if not a or a["reviewer_id"] != reviewer.id:
        audit(conn, reviewer, "authorization_denied", "assignment", assignment_id, {"action": "open_assignment"})
        raise AuthError("This abstract is not assigned to you.")
    return a


def my_assignments(conn, policy: dict, reviewer: User, now: datetime | None = None) -> list[dict]:
    authorize(conn, reviewer, "reviewer", action="my_assignments")
    now = now or now_utc()
    blind = cfg(policy).get("blind_review", True)
    out = []
    for a in conn.execute("SELECT * FROM review_assignments WHERE reviewer_id=? AND status IN ('assigned','accepted','submitted') ORDER BY due_at", (reviewer.id,)):
        s = _sub(conn, a["submission_id"])
        rv = conn.execute("SELECT status, total, recommendation FROM peer_reviews WHERE assignment_id=?", (a["id"],)).fetchone()
        dec = _latest_decision(conn, a["submission_id"])
        left = _parse(a["due_at"]) - now
        out.append({"assignment_id": a["id"], "submission_id": s["id"], "paper": paper_code(s["id"]), "status": a["status"],
                    "team": "hidden (blind review)" if blind else f"{s['team_code']} · {s['team_name']}",
                    "track": s["track"] or "-", "ps": f"{s['ps_id']} · {s['ps_title'] or ''}", "word_count": s["word_count"],
                    "due": _fmt(a["due_at"]), "overdue": a["status"] in OPEN and left.total_seconds() < 0,
                    "days_left": left.days, "review_status": rv["status"] if rv else "not started",
                    "total": rv["total"] if rv else None, "recommendation": rv["recommendation"] if rv else None,
                    "locked": bool(dec)})
    return out


def respond(conn, policy: dict, reviewer: User, assignment_id: int, accept: bool, reason: str = "", conflict: bool = False) -> None:
    a = _own_assignment(conn, reviewer, assignment_id)
    if a["status"] != "assigned":
        raise WorkflowError("You have already responded to this assignment.")
    now = iso(now_utc())
    if accept:
        conn.execute("UPDATE review_assignments SET status='accepted', responded_at=? WHERE id=?", (now, assignment_id))
        audit(conn, reviewer, "assignment_accepted", "submission", a["submission_id"])
        return
    reason = (reason or "").strip()
    if len(reason) < 5:
        raise WorkflowError("Please give a short reason for declining.")
    team_id = conn.execute("SELECT team_id FROM submissions WHERE id=?", (a["submission_id"],)).fetchone()[0]
    with tx(conn):
        conn.execute("UPDATE review_assignments SET status='declined', responded_at=?, note=? WHERE id=?",
                     (now, ("COI: " if conflict else "") + reason[:200], assignment_id))
        if conflict:
            conn.execute("INSERT OR IGNORE INTO reviewer_conflicts(reviewer_id, team_id, reason, declared_by, declared_at) VALUES (?,?,?,?,?)",
                         (reviewer.id, team_id, reason[:300], reviewer.id, now))
        audit(conn, reviewer, "assignment_declined", "submission", a["submission_id"], {"conflict": conflict})
    notify.notify_staff(conn, policy, ("admin",), "assignment_declined", f"Reviewer declined {paper_code(a['submission_id'])}",
                        [f"{reviewer.username} declined abstract {paper_code(a['submission_id'])}" + (" (conflict of interest)." if conflict else "."),
                         f"Reason: {reason}", "", "Please assign another reviewer on 🎯 Peer Review → Assign (or click Auto-assign)."],
                        f"declined:{assignment_id}", force=True)


PLEDGE = ("I will keep every abstract and review confidential, use the ideas for no other purpose, delete downloaded files "
          "after the evaluation, and declare any conflict of interest.")


def has_pledged(conn, reviewer_id: int) -> bool:
    return bool(conn.execute("SELECT 1 FROM audit_events WHERE actor_id=? AND action='reviewer_confidentiality_pledge'", (reviewer_id,)).fetchone())


def pledge(conn, reviewer: User) -> None:
    authorize(conn, reviewer, "reviewer", action="confidentiality_pledge")
    if not has_pledged(conn, reviewer.id):
        audit(conn, reviewer, "reviewer_confidentiality_pledge", "user", reviewer.id, {"text": PLEDGE})


def assignment_file(conn, policy: dict, reviewer: User, assignment_id: int, storage_dir: Path | None = None) -> tuple[str, bytes]:
    """The abstract file for an assigned reviewer. With blind review the file name is anonymised."""
    a = _own_assignment(conn, reviewer, assignment_id)
    if not has_pledged(conn, reviewer.id):
        raise WorkflowError("Please accept the confidentiality pledge on 📝 My Reviews before downloading abstracts.")
    if a["status"] not in ACTIVE:
        raise AuthError("This assignment is no longer active.")
    name, data = submission_file(conn, reviewer, a["submission_id"], storage_dir)
    if cfg(policy).get("blind_review", True):
        name = f"{paper_code(a['submission_id'])}{Path(name).suffix}"
    audit(conn, reviewer, "submission_file_downloaded", "submission", a["submission_id"], {"file": name, "via": "assignment"})
    return name, data


def get_review(conn, reviewer: User, assignment_id: int) -> dict | None:
    _own_assignment(conn, reviewer, assignment_id)
    r = conn.execute("SELECT * FROM peer_reviews WHERE assignment_id=?", (assignment_id,)).fetchone()
    return dict(r) | {"scores": json.loads(r["scores"])} if r else None


def save_review(conn, policy: dict, reviewer: User, assignment_id: int, scores: dict, recommendation: str | None, confidence: int | None,
                comments_team: str, comments_confidential: str = "", submit: bool = False, now: datetime | None = None) -> dict:
    a = _own_assignment(conn, reviewer, assignment_id)
    now = now or now_utc()
    c = cfg(policy)
    if a["status"] not in ACTIVE:
        raise WorkflowError("This assignment is no longer active.")
    if _latest_decision(conn, a["submission_id"]):
        raise WorkflowError("A decision has been recorded for this abstract; reviews are locked.")
    clean = {}
    for cr in c["criteria"]:
        v = scores.get(cr["id"])
        if v is None:
            if submit:
                raise WorkflowError(f"Give a score for '{cr['name']}'.")
            continue
        if not c["score_min"] <= int(v) <= c["score_max"]:
            raise WorkflowError(f"Scores must be between {c['score_min']} and {c['score_max']}.")
        clean[cr["id"]] = int(v)
    comments_team, comments_confidential = (comments_team or "").strip(), (comments_confidential or "").strip()
    if submit:
        if recommendation not in REC_LABEL:
            raise WorkflowError("Choose an overall recommendation.")
        if confidence is None or not 1 <= int(confidence) <= 5:
            raise WorkflowError("Choose your confidence (1-5).")
        if len(comments_team) < c["min_comment_chars"]:
            raise WorkflowError(f"Comments for the team must be at least {c['min_comment_chars']} characters — explain strengths and what to improve.")
    total = weighted_total(policy, clean) if len(clean) == len(c["criteria"]) else None
    old = conn.execute("SELECT * FROM peer_reviews WHERE assignment_id=?", (assignment_id,)).fetchone()
    status = "submitted" if submit or (old and old["status"] == "submitted") else "draft"
    if old and old["status"] == "submitted" and not submit:
        raise WorkflowError("This review is already submitted; use 'Submit review' to save a revision.")
    with tx(conn):
        if old:
            rev = old["revision"] + (1 if submit else 0)
            conn.execute("UPDATE peer_reviews SET scores=?, total=?, recommendation=?, confidence=?, comments_team=?, comments_confidential=?, "
                         "status=?, revision=?, updated_at=?, submitted_at=COALESCE(?, submitted_at) WHERE id=?",
                         (json.dumps(clean), total, recommendation, confidence, comments_team, comments_confidential, status, rev, iso(now),
                          iso(now) if submit else None, old["id"]))
            rid = old["id"]
        else:
            rev = 1 if submit else 0
            rid = conn.execute("INSERT INTO peer_reviews(assignment_id, submission_id, reviewer_id, scores, total, recommendation, confidence, comments_team, "
                               "comments_confidential, status, revision, updated_at, submitted_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                               (assignment_id, a["submission_id"], reviewer.id, json.dumps(clean), total, recommendation, confidence,
                                comments_team, comments_confidential, status, rev, iso(now), iso(now) if submit else None)).lastrowid
        if submit:
            conn.execute("UPDATE review_assignments SET status='submitted', responded_at=COALESCE(responded_at, ?) WHERE id=?", (iso(now), assignment_id))
            conn.execute("INSERT INTO peer_review_history(review_id, revision, data, at) VALUES (?,?,?,?)",
                         (rid, rev, json.dumps({"scores": clean, "total": total, "recommendation": recommendation, "confidence": confidence,
                                                "comments_team": comments_team, "comments_confidential": comments_confidential}), iso(now)))
        audit(conn, reviewer, "peer_review_submitted" if submit else "peer_review_draft_saved", "submission", a["submission_id"], {"revision": rev, "total": total})
    if submit:
        p = paper_code(a["submission_id"])
        notify.notify_user(conn, policy, reviewer.id, "review_submitted_ack", f"Review received: {p}" + (f" (revision {rev})" if rev > 1 else ""),
                           [f"Thank you — your review of {p} has been recorded.", f"Weighted score: {total}/100 · Recommendation: {REC_LABEL[recommendation]}",
                            "You can revise it until the organisers record a decision."], f"reviewack:{rid}:{rev}")
        k = cfg(policy)["reviewers_per_submission"]
        n_done = conn.execute("SELECT COUNT(*) FROM peer_reviews WHERE submission_id=? AND status='submitted'", (a["submission_id"],)).fetchone()[0]
        n_open = conn.execute("SELECT COUNT(*) FROM review_assignments WHERE submission_id=? AND status IN ('assigned','accepted')", (a["submission_id"],)).fetchone()[0]
        if rev == 1 and n_done >= k and n_open == 0:
            agg = aggregate(conn, policy, a["submission_id"])
            notify.notify_staff(conn, policy, ("admin",), "reviews_complete", f"All reviews in: {p} — ready for decision",
                                [f"{p} now has {agg['count']} review(s). Mean score {agg['mean']}/100, spread {agg['spread']}.",
                                 "⚠ Reviewers disagree — please look closely or add a reviewer." if agg["disagreement"] else "Reviewers broadly agree.",
                                 "", "Record the decision on 🎯 Peer Review → Decisions."], f"reviewscomplete:{a['submission_id']}", force=True)
    return get_review(conn, reviewer, assignment_id)


def peer_reviews_visible_to(conn, policy: dict, reviewer: User, assignment_id: int) -> list[dict]:
    """Discussion phase: after submitting their own review, a reviewer may read the other (anonymised) reviews."""
    a = _own_assignment(conn, reviewer, assignment_id)
    if a["status"] != "submitted":
        return []
    rows = conn.execute("SELECT reviewer_id, total, recommendation, confidence, comments_team FROM peer_reviews WHERE submission_id=? AND status='submitted' ORDER BY id",
                        (a["submission_id"],)).fetchall()
    return [{"reviewer": "You" if r["reviewer_id"] == reviewer.id else f"Reviewer {i + 1}", "total": r["total"],
             "recommendation": REC_LABEL.get(r["recommendation"], r["recommendation"]), "confidence": r["confidence"], "comments": r["comments_team"]}
            for i, r in enumerate(rows)]


# ---------------- admin: reviews, decisions, release ----------------

def aggregate(conn, policy: dict, submission_id: int) -> dict:
    rows = conn.execute("SELECT total, recommendation, confidence FROM peer_reviews WHERE submission_id=? AND status='submitted' AND total IS NOT NULL",
                        (submission_id,)).fetchall()
    tots = [r["total"] for r in rows]
    recs = [r["recommendation"] for r in rows]
    spread = round(max(tots) - min(tots), 1) if len(tots) > 1 else 0.0
    w = [max(1, r["confidence"] or 1) for r in rows]
    mixed = len({x in REC_POSITIVE for x in recs}) > 1
    return {"count": len(rows), "mean": round(statistics.mean(tots), 1) if tots else None,
            "weighted_mean": round(sum(t * c for t, c in zip(tots, w)) / sum(w), 1) if tots else None,
            "spread": spread, "disagreement": bool(rows) and (spread >= cfg(policy).get("disagreement_spread", 25) or mixed),
            "recommendations": ", ".join(REC_LABEL.get(x, x) for x in recs)}


def submission_reviews(conn, policy: dict, admin: User, submission_id: int) -> dict:
    authorize(conn, admin, "admin", action="submission_reviews")
    s = _sub(conn, submission_id)
    rows = conn.execute("SELECT r.*, u.username FROM peer_reviews r JOIN users u ON u.id=r.reviewer_id WHERE r.submission_id=? ORDER BY r.id",
                        (submission_id,)).fetchall()
    dec = [dict(d) for d in conn.execute("SELECT d.*, u.username AS decided_by_name, r.released_at FROM evaluation_decisions d JOIN users u ON u.id=d.decided_by "
                                          "LEFT JOIN evaluation_releases r ON r.decision_id=d.id WHERE d.submission_id=? ORDER BY d.id DESC", (submission_id,))]
    return {"paper": paper_code(submission_id), "team_code": s["team_code"], "team_name": s["team_name"], "track": s["track"],
            "ps": f"{s['ps_id']} · {s['ps_title'] or ''}",
            "reviews": [dict(r) | {"scores": json.loads(r["scores"])} for r in rows], "aggregate": aggregate(conn, policy, submission_id),
            "decisions": dec}


def decide(conn, policy: dict, admin: User, submission_id: int, outcome: str, note_to_team: str = "", share_comments: bool = True,
           release_now: bool = False, now: datetime | None = None) -> int:
    authorize(conn, admin, "admin", action="evaluation_decision")
    now = now or now_utc()
    if outcome not in OUTCOME_LABEL:
        raise WorkflowError("Choose shortlisted, waitlisted or not shortlisted.")
    s = _sub(conn, submission_id)
    _require_evaluable(s)
    agg = aggregate(conn, policy, submission_id)
    if agg["count"] == 0:
        raise WorkflowError("At least one submitted review is needed before a decision.")
    with tx(conn):
        did = conn.execute("INSERT INTO evaluation_decisions(submission_id, team_id, outcome, mean_score, reviews_count, note_to_team, share_comments, decided_by, decided_at) "
                           "VALUES (?,?,?,?,?,?,?,?,?)", (submission_id, s["team_id"], outcome, agg["mean"], agg["count"], (note_to_team or "").strip(),
                                                          int(bool(share_comments)), admin.id, iso(now))).lastrowid
        conn.execute("UPDATE review_assignments SET status='withdrawn', note='decision recorded before review', responded_at=? "
                     "WHERE submission_id=? AND status IN ('assigned','accepted')", (iso(now), submission_id))
        audit(conn, admin, "evaluation_decision", "submission", submission_id, {"outcome": outcome, "mean": agg["mean"], "decision_id": did})
    if release_now:
        release(conn, policy, admin, [did], now)
    return did


def release(conn, policy: dict, admin: User, decision_ids: list[int] | None = None, now: datetime | None = None) -> int:
    """Email results to teams. None = every latest decision not yet released (the classic 'notification day')."""
    authorize(conn, admin, "admin", action="release_results")
    now = now or now_utc()
    latest = {r["id"] for r in conn.execute("SELECT MAX(id) AS id FROM evaluation_decisions GROUP BY submission_id")}
    ids = [i for i in (decision_ids if decision_ids is not None else latest) if i in latest]
    n = 0
    for did in ids:
        if conn.execute("SELECT 1 FROM evaluation_releases WHERE decision_id=?", (did,)).fetchone():
            continue
        d = conn.execute("SELECT * FROM evaluation_decisions WHERE id=?", (did,)).fetchone()
        conn.execute("INSERT INTO evaluation_releases(decision_id, released_by, released_at) VALUES (?,?,?)", (did, admin.id, iso(now)))
        lines = [f"The evaluation of your abstract ({paper_code(d['submission_id'])}) is complete.", "",
                 f"Result: {OUTCOME_LABEL[d['outcome']].upper()}"]
        if cfg(policy).get("share_scores_with_team") and d["mean_score"] is not None:
            lines.append(f"Average reviewer score: {d['mean_score']}/100 ({d['reviews_count']} review(s))")
        if d["note_to_team"]:
            lines += ["", "Message from the organisers:", d["note_to_team"]]
        if d["share_comments"]:
            for i, r in enumerate(conn.execute("SELECT comments_team FROM peer_reviews WHERE submission_id=? AND status='submitted' ORDER BY id", (d["submission_id"],))):
                if r["comments_team"]:
                    lines += ["", f"Reviewer {i + 1}:", r["comments_team"]]
        lines += ["", "You can also read this in the portal under 🏅 Results."]
        notify.notify_team(conn, policy, d["team_id"], "evaluation_result", f"Evaluation result: {OUTCOME_LABEL[d['outcome']]}", lines, f"evalresult:{did}")
        audit(conn, admin, "evaluation_released", "submission", d["submission_id"], {"decision_id": did})
        n += 1
    if n:
        notify.notify_user(conn, policy, admin.id, "release_ack", f"Results released to {n} team(s)", [f"{n} evaluation result(s) were emailed to the teams."],
                           f"releaseack:{iso(now)}")
    return n


def leaderboard(conn, policy: dict, admin: User) -> list[dict]:
    rows = [r for r in evaluable(conn, admin, policy) if r["mean_score"] is not None]
    rows.sort(key=lambda r: (r["track"] or "", -r["mean_score"]))
    rank, prev = 0, None
    for r in rows:
        rank = 1 if r["track"] != prev else rank + 1
        prev = r["track"]
        r["rank_in_track"] = rank
    return rows


def team_result(conn, policy: dict, student: User) -> dict | None:
    """What a team sees after release: outcome, organiser note, anonymised reviewer comments (never scores of individuals or confidential notes)."""
    from workflow.services import refresh_user
    u = refresh_user(conn, student)
    if not u.team_id:
        return None
    d = conn.execute("SELECT d.* FROM evaluation_decisions d JOIN evaluation_releases r ON r.decision_id=d.id WHERE d.team_id=? ORDER BY d.id DESC LIMIT 1",
                     (u.team_id,)).fetchone()
    if not d or d["id"] != conn.execute("SELECT MAX(id) FROM evaluation_decisions WHERE submission_id=?", (d["submission_id"],)).fetchone()[0]:
        return None
    comments = [r["comments_team"] for r in conn.execute("SELECT comments_team FROM peer_reviews WHERE submission_id=? AND status='submitted' ORDER BY id",
                                                           (d["submission_id"],))] if d["share_comments"] else []
    return {"outcome": d["outcome"], "label": OUTCOME_LABEL[d["outcome"]], "note": d["note_to_team"],
            "mean_score": d["mean_score"] if cfg(policy).get("share_scores_with_team") else None,
            "comments": [c for c in comments if c], "paper": paper_code(d["submission_id"])}


# ---------------- reminders (called by the email worker) ----------------

def send_due_reminders(conn, policy: dict, now: datetime) -> int:
    hours = sorted(cfg(policy).get("reminder_hours_before_due", [48, 24]))
    n = 0
    overdue = []
    for a in conn.execute("SELECT * FROM review_assignments WHERE status IN ('assigned','accepted')").fetchall():
        due = _parse(a["due_at"])
        p = paper_code(a["submission_id"])
        if now > due:
            overdue.append(a)
            n += notify.notify_user(conn, policy, a["reviewer_id"], "review_overdue", f"Overdue review: {p}",
                                    [f"Your review of {p} was due on {due.strftime('%d %b %Y, %H:%M UTC')}.",
                                     "Please submit it as soon as possible, or decline so the organisers can reassign it."],
                                    f"evaloverdue:{a['id']}:{now.strftime('%Y-%m-%d')}")
            continue
        reached = [h for h in hours if now >= due - timedelta(hours=h)]
        if reached:
            h = min(reached)
            n += notify.notify_user(conn, policy, a["reviewer_id"], "review_due_reminder", f"Reminder: review of {p} due in {h} hours",
                                    [f"Your review of {p} is due on {due.strftime('%d %b %Y, %H:%M UTC')}.",
                                     "Sign in → 📝 My Reviews to finish it." + (" You have not accepted the assignment yet." if a["status"] == "assigned" else "")],
                                    f"evaldue:{a['id']}:{h}")
    if overdue:
        names = {r["id"]: r["username"] for r in conn.execute("SELECT id, username FROM users")}
        n += notify.notify_staff(conn, policy, ("admin",), "reviews_overdue", f"{len(overdue)} review(s) overdue",
                                 [f"  • {paper_code(a['submission_id'])} — {names.get(a['reviewer_id'])} — due {_fmt(a['due_at'])}" for a in overdue[:40]]
                                 + ["", "Withdraw and reassign on 🎯 Peer Review → Progress if needed."],
                                 f"evaloverdueadmin:{now.strftime('%Y-%m-%d')}", force=True)
    return n
