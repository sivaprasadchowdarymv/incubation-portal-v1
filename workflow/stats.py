"""Programme statistics for the admin dashboard (in the spirit of Microsoft CMT's submission summary):
one row per team × problem statement, then summaries per track and per problem statement."""
from __future__ import annotations

import statistics

from workflow.evaluation import paper_code
from workflow.services import authorize

STAGE = {None: "Not submitted", "SUBMITTED": "Checking", "VALIDATING": "Checking", "ACCEPTED_FOR_EVALUATION": "Accepted",
         "PENDING_REVIEW": "Human review", "REVIEW_CLEARED": "Human review", "VALIDATION_FAILED": "Needs resubmission",
         "RESUBMISSION_ALLOWED": "Needs resubmission", "CORRECTIONS_REQUESTED": "Needs resubmission", "DROPPED": "Closed",
         "DEADLINE_MISSED": "Closed", "REJECTED_BY_AUTHORIZED_REVIEW": "Closed"}
STAGES = ["Not submitted", "Checking", "Needs resubmission", "Human review", "Accepted", "Closed"]
DECISIONS = {"shortlisted": "Shortlisted", "waitlisted": "Waitlisted", "not_shortlisted": "Not shortlisted"}


def entry_rows(conn, admin) -> list[dict]:
    """One row per team × problem statement with its latest submission and peer-review progress."""
    authorize(conn, admin, "admin", action="stats")
    asg = {r["submission_id"]: dict(r) for r in conn.execute(
        "SELECT submission_id, SUM(status IN ('assigned','accepted','submitted')) AS assigned, SUM(status='submitted') AS done "
        "FROM review_assignments GROUP BY submission_id")}
    scores = {}
    for r in conn.execute("SELECT submission_id, total FROM peer_reviews WHERE status='submitted' AND total IS NOT NULL"):
        scores.setdefault(r["submission_id"], []).append(r["total"])
    dec = {r["submission_id"]: dict(r) for r in conn.execute(
        "SELECT d.submission_id, d.outcome, (SELECT 1 FROM evaluation_releases x WHERE x.decision_id=d.id) AS released FROM evaluation_decisions d "
        "WHERE d.id=(SELECT MAX(id) FROM evaluation_decisions y WHERE y.submission_id=d.submission_id)")}
    out = []
    for e in conn.execute("SELECT e.team_id, e.entry_no, e.ps_id, e.locked_at, t.team_code, t.name AS team_name, p.title AS ps_title, "
                          "p.domain_id AS track_id, d.name AS track FROM team_entries e JOIN teams t ON t.id=e.team_id "
                          "JOIN problem_statements p ON p.id=e.ps_id JOIN domains d ON d.id=p.domain_id ORDER BY d.id, e.ps_id, t.team_code"):
        s = conn.execute("SELECT id, status, attempt_no, submitted_at FROM submissions WHERE team_id=? AND ps_id=? ORDER BY attempt_no DESC LIMIT 1",
                         (e["team_id"], e["ps_id"])).fetchone()
        sid = s["id"] if s else None
        sc = scores.get(sid, [])
        a = asg.get(sid, {})
        d = dec.get(sid, {})
        out.append({"track_id": e["track_id"], "track": e["track"], "ps_id": e["ps_id"], "ps_title": e["ps_title"], "team_code": e["team_code"],
                    "team_name": e["team_name"], "entry_no": e["entry_no"], "submission_id": sid, "paper": paper_code(sid) if sid else "",
                    "status": s["status"] if s else None, "stage": STAGE.get(s["status"] if s else None, "Checking"),
                    "attempts": s["attempt_no"] if s else 0, "submitted_at": (s["submitted_at"] or "")[:16].replace("T", " ") if s else "",
                    "reviewers": int(a.get("assigned") or 0), "reviews_done": int(a.get("done") or 0),
                    "mean_score": round(statistics.mean(sc), 1) if sc else None, "decision": DECISIONS.get(d.get("outcome"), ""),
                    "released": bool(d.get("released"))})
    return out


def _summary(rows: list[dict], key: str, label: str, k_reviewers: int) -> list[dict]:
    groups: dict = {}
    for r in rows:
        groups.setdefault(r[key], []).append(r)
    out = []
    for g, rs in groups.items():
        acc = [r for r in rs if r["stage"] == "Accepted"]
        sc = [r["mean_score"] for r in rs if r["mean_score"] is not None]
        need = len(acc) * k_reviewers
        row = {label: g}
        if key == "ps_id":
            row["Title"] = rs[0]["ps_title"]
            row["Track"] = rs[0]["track"]
        row |= {"Teams": len({r["team_code"] for r in rs}), "Entries": len(rs),
                "Submitted": sum(r["stage"] != "Not submitted" for r in rs)}
        row |= {st: sum(r["stage"] == st for r in rs) for st in STAGES}
        row |= {"Reviewers assigned": sum(r["reviewers"] for r in acc), "Reviews done": sum(r["reviews_done"] for r in acc),
                "Review completion %": round(100 * sum(min(r["reviews_done"], k_reviewers) for r in acc) / need) if need else None,
                "Avg score": round(statistics.mean(sc), 1) if sc else None}
        row |= {v: sum(r["decision"] == v for r in rs) for v in DECISIONS.values()}
        row["Released"] = sum(r["released"] for r in rs)
        out.append(row)
    return out


def track_summary(rows: list[dict], policy: dict) -> list[dict]:
    out = _summary(rows, "track", "Track", policy.get("evaluation", {}).get("reviewers_per_submission", 2))
    return sorted(out, key=lambda r: r["Track"])


def ps_summary(rows: list[dict], policy: dict) -> list[dict]:
    out = _summary(rows, "ps_id", "Problem statement", policy.get("evaluation", {}).get("reviewers_per_submission", 2))
    return sorted(out, key=lambda r: (r["Track"], r["Problem statement"]))


def totals(rows: list[dict], policy: dict, label: str = "Track") -> dict:
    t = _summary([dict(r, _all="All") for r in rows], "_all", label, policy.get("evaluation", {}).get("reviewers_per_submission", 2))
    return t[0] if t else {}


def headline(conn, rows: list[dict]) -> dict:
    return {"teams": conn.execute("SELECT COUNT(*) FROM teams").fetchone()[0],
            "teams_without_ps": conn.execute("SELECT COUNT(*) FROM teams WHERE ps_locked=0").fetchone()[0],
            "students": conn.execute("SELECT COUNT(*) FROM users WHERE role='student'").fetchone()[0],
            "reviewers": conn.execute("SELECT COUNT(*) FROM users WHERE role='reviewer' AND active=1").fetchone()[0],
            "entries": len(rows), "submitted": sum(r["stage"] != "Not submitted" for r in rows),
            "accepted": sum(r["stage"] == "Accepted" for r in rows),
            "human_review": sum(r["stage"] == "Human review" for r in rows),
            "reviews_done": sum(r["reviews_done"] for r in rows),
            "shortlisted": sum(r["decision"] == "Shortlisted" for r in rows),
            "open_queries": conn.execute("SELECT COUNT(*) FROM queries WHERE status='open'").fetchone()[0]}
