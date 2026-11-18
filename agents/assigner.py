"""Reviewer-assignment agent.

When an abstract is accepted for evaluation, the agent PROPOSES reviewers; nothing is shared yet. The admin sees the
proposals (🎯 Peer Review → 🤖 Agent proposals), can change any reviewer, and approves. Only on approval is the
assignment created, the reviewer emailed and the abstract made available to that reviewer.

Ranking for each abstract (explained to the admin in plain words):
  + 3    the reviewer's expertise includes the abstract's track
  + 0.5  per shared topic word between the reviewer's topics and the abstract/problem statement (max + 3)
  - 0.4  per abstract the reviewer is already reviewing or proposed for (fair workload)
Never proposed: inactive or unavailable reviewers, anyone at their maximum load, anyone with a conflict of interest
(declared, or same email as a team member), anyone who already declined or reviews this abstract."""
from __future__ import annotations

import re

from notifications import service as notify
from workflow import evaluation as ev
from workflow.services import WorkflowError, audit, authorize, iso, now_utc

WORD = re.compile(r"[a-z][a-z\-]{3,}")
COMMON = set("""with from that this will have their they about into more than also which these those using based system data model
project team work field area areas research development management engineering technology technologies science""".split())


def _words(text: str) -> set[str]:
    return {w.rstrip("s") for w in WORD.findall((text or "").lower()) if w not in COMMON}


def _pending_load(conn, uid: int) -> int:
    return conn.execute("SELECT COUNT(*) FROM assignment_proposals WHERE reviewer_id=? AND status='pending'", (uid,)).fetchone()[0]


def _needed(conn, policy: dict, sid: int) -> int:
    k = ev.cfg(policy)["reviewers_per_submission"]
    active = conn.execute("SELECT COUNT(*) FROM review_assignments WHERE submission_id=? AND status IN ('assigned','accepted','submitted')",
                          (sid,)).fetchone()[0]
    pend = conn.execute("SELECT COUNT(*) FROM assignment_proposals WHERE submission_id=? AND status='pending'", (sid,)).fetchone()[0]
    return max(0, k - active - pend)


def rank(conn, sid: int, exclude: set[int] = frozenset()) -> list[dict]:
    """All eligible reviewers for one abstract, best first, with the reason in words."""
    s = conn.execute("SELECT s.team_id, s.extracted_text, p.id AS ps_id, p.title, p.description, p.keywords, p.domain_id, d.name AS track "
                     "FROM submissions s JOIN problem_statements p ON p.id=s.ps_id JOIN domains d ON d.id=p.domain_id WHERE s.id=?", (sid,)).fetchone()
    if not s:
        raise WorkflowError("Submission not found")
    paper = _words(" ".join([s["title"] or "", s["description"] or "", s["keywords"] or "", (s["extracted_text"] or "")[:6000]]))
    taken = {r[0] for r in conn.execute("SELECT reviewer_id FROM review_assignments WHERE submission_id=? "
                                        "UNION SELECT reviewer_id FROM assignment_proposals WHERE submission_id=? AND status IN ('pending','approved')",
                                        (sid, sid))}
    out = []
    for r in conn.execute("SELECT id, username, full_name FROM users WHERE role='reviewer' AND active=1 ORDER BY id"):
        uid = r["id"]
        if uid in taken or uid in exclude:
            continue
        prof = ev.profile(conn, uid)
        if not prof["available"] or ev.conflict_reason(conn, uid, s["team_id"]):
            continue
        load = ev.load(conn, uid) + _pending_load(conn, uid)
        if load >= prof["max_load"]:
            continue
        expert = s["domain_id"] in prof["expertise"]
        shared = sorted(_words(prof["topics"]) & paper)
        score = (3 if expert else 0) + min(3.0, 0.5 * len(shared)) - 0.4 * load
        why = (["track expert (" + s["track"] + ")"] if expert else ["not a declared expert for this track"]) \
            + ([f"topics: {', '.join(shared[:4])}"] if shared else []) + [f"{load} open review(s)"]
        out.append({"reviewer_id": uid, "username": r["username"], "name": r["full_name"] or r["username"], "score": round(score, 2),
                    "reason": " · ".join(why)})
    return sorted(out, key=lambda x: (-x["score"], x["reviewer_id"]))


def propose(conn, policy: dict, sid: int, notify_admins: bool = True) -> list[int]:
    """Create pending proposals for one accepted abstract (called automatically on acceptance)."""
    s = conn.execute("SELECT status FROM submissions WHERE id=?", (sid,)).fetchone()
    if not s or s["status"] != "ACCEPTED_FOR_EVALUATION" or ev._latest_decision(conn, sid):
        return []
    need = _needed(conn, policy, sid)
    ids = []
    for c in rank(conn, sid)[:need]:
        ids.append(conn.execute("INSERT INTO assignment_proposals(submission_id, reviewer_id, score, reason, status, created_at) "
                                "VALUES (?,?,?,?, 'pending', ?)", (sid, c["reviewer_id"], c["score"], c["reason"], iso(now_utc()))).lastrowid)
    if ids:
        audit(conn, None, "reviewers_proposed", "submission", sid, {"proposals": ids})
        if notify_admins:
            n = conn.execute("SELECT COUNT(*) FROM assignment_proposals WHERE status='pending'").fetchone()[0]
            notify.notify_staff(conn, policy, ("admin",), "proposals_waiting", f"{n} reviewer proposal(s) waiting for your approval",
                                [f"The assignment agent proposed reviewers for abstract {ev.paper_code(sid)} (and {n} proposal(s) are pending in total).",
                                 "Nothing is shared with reviewers until you approve.", "",
                                 "Open 🎯 Peer Review → 🤖 Agent proposals to check, change or approve them."],
                                f"proposals:{now_utc().strftime('%Y-%m-%d')}", force=True)
    return ids


def propose_all(conn, policy: dict, admin) -> dict:
    authorize(conn, admin, "admin", action="propose_all")
    made, short = 0, []
    for r in conn.execute("SELECT id FROM submissions WHERE status='ACCEPTED_FOR_EVALUATION' ORDER BY id").fetchall():
        need = _needed(conn, policy, r["id"])
        if not need or ev._latest_decision(conn, r["id"]):
            continue
        got = propose(conn, policy, r["id"], notify_admins=False)
        made += len(got)
        if len(got) < need:
            short.append(ev.paper_code(r["id"]))
    audit(conn, admin, "propose_all", details={"proposals": made, "short": short})
    return {"proposed": made, "short": short}


def pending(conn, admin) -> list[dict]:
    authorize(conn, admin, "admin", action="list_proposals")
    rows = conn.execute("SELECT a.*, u.username, t.team_code, s.ps_id, d.name AS track, p.title AS ps_title FROM assignment_proposals a "
                        "JOIN users u ON u.id=a.reviewer_id JOIN submissions s ON s.id=a.submission_id JOIN teams t ON t.id=s.team_id "
                        "JOIN problem_statements p ON p.id=s.ps_id JOIN domains d ON d.id=p.domain_id WHERE a.status='pending' "
                        "ORDER BY d.name, s.ps_id, a.submission_id, a.score DESC").fetchall()
    return [dict(r) | {"paper": ev.paper_code(r["submission_id"])} for r in rows]


def approve(conn, policy: dict, admin, decisions: list[dict], due=None) -> dict:
    """decisions = [{"id": proposal_id, "reviewer_id": optional replacement}]. Approval creates the assignment,
    emails the reviewer and shares the abstract with them. Problems are reported per row; the rest still go through."""
    authorize(conn, admin, "admin", action="approve_proposals")
    done, errors = 0, []
    for d in decisions:
        p = conn.execute("SELECT * FROM assignment_proposals WHERE id=? AND status='pending'", (d["id"],)).fetchone()
        if not p:
            continue
        rid = int(d.get("reviewer_id") or p["reviewer_id"])
        try:
            ev.assign(conn, policy, admin, p["submission_id"], rid, due)
        except WorkflowError as exc:
            errors.append(f"{ev.paper_code(p['submission_id'])}: {exc}")
            continue
        changed = rid != p["reviewer_id"]
        conn.execute("UPDATE assignment_proposals SET status='approved', reviewer_id=?, reason=?, decided_at=?, decided_by=? WHERE id=?",
                     (rid, p["reason"] + (" · changed by admin" if changed else ""), iso(now_utc()), admin.id, p["id"]))
        done += 1
    audit(conn, admin, "proposals_approved", details={"approved": done, "errors": errors})
    if done:
        notify.notify_user(conn, policy, admin.id, "proposals_approved_ack", f"{done} reviewer assignment(s) approved",
                           [f"You approved {done} reviewer assignment(s). The reviewers were emailed and can now open their abstracts."],
                           f"propack:{iso(now_utc())}")
    return {"approved": done, "errors": errors}


def reject(conn, policy: dict, admin, ids: list[int], repropose: bool = True) -> dict:
    """Reject proposals; optionally ask the agent for the next-best reviewers straight away."""
    authorize(conn, admin, "admin", action="reject_proposals")
    subs = set()
    for i in ids:
        p = conn.execute("SELECT submission_id FROM assignment_proposals WHERE id=? AND status='pending'", (i,)).fetchone()
        if p:
            conn.execute("UPDATE assignment_proposals SET status='rejected', decided_at=?, decided_by=? WHERE id=?", (iso(now_utc()), admin.id, i))
            subs.add(p["submission_id"])
    new = 0
    if repropose:
        for sid in subs:
            rejected = {r[0] for r in conn.execute("SELECT reviewer_id FROM assignment_proposals WHERE submission_id=? AND status='rejected'", (sid,))}
            need = _needed(conn, policy, sid)
            for c in rank(conn, sid, exclude=rejected)[:need]:
                conn.execute("INSERT INTO assignment_proposals(submission_id, reviewer_id, score, reason, status, created_at) VALUES (?,?,?,?, 'pending', ?)",
                             (sid, c["reviewer_id"], c["score"], c["reason"], iso(now_utc())))
                new += 1
    audit(conn, admin, "proposals_rejected", details={"rejected": len(ids), "new": new})
    return {"rejected": len(ids), "new_proposals": new}
