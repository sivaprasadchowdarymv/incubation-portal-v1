"""Persisted metrics and workflow summaries (computed from real records only)."""
from __future__ import annotations

import json
from datetime import datetime, timezone


def record(conn, name: str, value: float, labels: dict | None = None) -> None:
    conn.execute("INSERT INTO metrics(name,value,labels,at) VALUES (?,?,?,?)",
                 (name, float(value), json.dumps(labels or {}), datetime.now(timezone.utc).isoformat(timespec="seconds")))


def _avg(conn, name: str):
    r = conn.execute("SELECT AVG(value), COUNT(*) FROM metrics WHERE name=?", (name,)).fetchone()
    return (round(r[0], 2) if r[0] is not None else None), r[1]


def workflow_summary(conn) -> dict:
    q = lambda sql, *a: conn.execute(sql, a).fetchall()
    status = {r[0]: r[1] for r in q("SELECT status, COUNT(*) FROM submissions GROUP BY status")}
    attempts = {r[0]: r[1] for r in q("SELECT attempt_no, COUNT(*) FROM submissions GROUP BY attempt_no")}
    failed = {r[0]: r[1] for r in q("SELECT check_id, COUNT(*) FROM validation_results WHERE result='fail' GROUP BY check_id")}
    blocked = {}
    for r in q("SELECT labels FROM metrics WHERE name='submission_blocked'"):
        code = json.loads(r[0]).get("code", "?")
        blocked[code] = blocked.get(code, 0) + 1
    notif = {r[0]: r[1] for r in q("SELECT status, COUNT(*) FROM notifications GROUP BY status")}
    avg_ms, n_proc = _avg(conn, "submission_processing_ms")
    rag_ms, n_rag = _avg(conn, "rag_latency_ms")
    rag_status = {}
    for r in q("SELECT labels FROM metrics WHERE name='rag_query'"):
        s = json.loads(r[0]).get("status", "?")
        rag_status[s] = rag_status.get(s, 0) + 1
    return {
        "teams": q("SELECT COUNT(*) FROM teams")[0][0],
        "teams_ps_locked": q("SELECT COUNT(*) FROM teams WHERE ps_locked=1")[0][0],
        "submissions_by_status": status,
        "attempt_distribution": attempts,
        "failed_checks": failed,
        "blocked_submissions": blocked,
        "review_backlog": q("SELECT COUNT(*) FROM review_cases WHERE status='open'")[0][0],
        "deadline_missed": status.get("DEADLINE_MISSED", 0) + blocked.get("DEADLINE_PASSED", 0),
        "notifications": notif,
        "avg_processing_ms": avg_ms, "processed_count": n_proc,
        "state_transition_errors": q("SELECT COUNT(*) FROM metrics WHERE name='state_transition_error'")[0][0],
        "rag_avg_latency_ms": rag_ms, "rag_queries": n_rag, "rag_status": rag_status,
    }
