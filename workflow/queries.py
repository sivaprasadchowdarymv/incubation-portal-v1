"""Query mail to the incubation team (replaces the student/reviewer Policy Assistant).

A student or reviewer writes a query -> it is stored, emailed to the incubation inbox and to every admin, and the sender
gets an acknowledgement with a reference number (Q-0007). Admins answer from the Queries inbox (optionally starting from an
AI-drafted reply built from the approved knowledge base); the answer is emailed to the sender. Senders can follow up in the
same thread, which reopens it. Messages are permanent (database triggers)."""
from __future__ import annotations

from datetime import timedelta

from database.db import tx
from notifications import service as notify
from security.auth import User
from workflow.services import WorkflowError, audit, authorize, iso, now_utc, refresh_user

CATEGORIES = ["Registration & team", "Problem statements", "Submission & checks", "Deadlines", "Peer review", "Results",
              "Technical problem", "Other"]
QUERIES_PER_DAY = 10


def ref(qid: int) -> str:
    return f"Q-{int(qid):04d}"


def _inbox(policy: dict) -> str:
    return str(policy.get("notifications", {}).get("query_inbox", "") or "").strip()


def _messages(conn, qid: int) -> list[dict]:
    return [dict(r) for r in conn.execute("SELECT m.id, m.body, m.at, m.from_staff, u.username AS author FROM query_messages m "
                                           "LEFT JOIN users u ON u.id=m.author_id WHERE m.query_id=? ORDER BY m.id", (qid,))]


def _check(subject: str | None, body: str) -> tuple[str, str]:
    subject, body = (subject or "").strip(), (body or "").strip()
    if subject is not None and subject == "" and body == "":
        raise WorkflowError("Write your message.")
    if len(body) < 15:
        raise WorkflowError("Please describe your query in at least 15 characters.")
    if len(body) > 5000:
        raise WorkflowError("Please keep the message under 5000 characters.")
    return subject[:150], body


def send_query(conn, policy: dict, user: User, category: str, subject: str, body: str) -> int:
    authorize(conn, user, "student", "reviewer", action="send_query")
    user = refresh_user(conn, user)
    if category not in CATEGORIES:
        raise WorkflowError("Choose a category.")
    subject, body = _check(subject, body)
    if len(subject) < 5:
        raise WorkflowError("Give the query a short subject (at least 5 characters).")
    since = iso(now_utc() - timedelta(days=1))
    if conn.execute("SELECT COUNT(*) FROM queries WHERE user_id=? AND created_at>=?", (user.id, since)).fetchone()[0] >= QUERIES_PER_DAY:
        raise WorkflowError(f"You can send at most {QUERIES_PER_DAY} new queries a day. Add to an existing query instead.")
    now = iso(now_utc())
    with tx(conn):
        qid = conn.execute("INSERT INTO queries(user_id, team_id, category, subject, status, created_at, updated_at) VALUES (?,?,?,?, 'open', ?, ?)",
                           (user.id, user.team_id, category, subject, now, now)).lastrowid
        conn.execute("INSERT INTO query_messages(query_id, author_id, from_staff, body, at) VALUES (?,?,0,?,?)", (qid, user.id, body, now))
        audit(conn, user, "query_sent", "query", qid, {"category": category})
    team = conn.execute("SELECT team_code, name FROM teams WHERE id=?", (user.team_id,)).fetchone() if user.team_id else None
    who = f"{user.username} ({user.role}" + (f", team {team['team_code']} · {team['name']}" if team else "") + ")"
    lines = [f"New query {ref(qid)} from {who}", f"Category: {category}", f"Subject: {subject}", "", body, "",
             "Answer it in the portal: 💬 Queries."]
    notify.notify_staff(conn, policy, ("admin",), "query_received", f"[{ref(qid)}] {subject}", lines, f"query:{qid}", force=True)
    if _inbox(policy):
        notify.notify_address(conn, policy, _inbox(policy), "query_received", f"[{ref(qid)}] {subject}", lines, f"queryinbox:{qid}")
    notify.notify_user(conn, policy, user.id, "query_ack", f"We received your query {ref(qid)}",
                       [f"Thank you. Your query has been sent to the incubation team (reference {ref(qid)}).", f"Subject: {subject}", "",
                        "Your message:", body, "", "You will get an email when we reply. You can also follow it under 💬 Ask the Incubation Team."],
                       f"queryack:{qid}")
    return qid


def follow_up(conn, policy: dict, user: User, query_id: int, body: str) -> None:
    authorize(conn, user, "student", "reviewer", action="query_follow_up")
    q = conn.execute("SELECT * FROM queries WHERE id=?", (query_id,)).fetchone()
    if not q or q["user_id"] != user.id:
        raise WorkflowError("Query not found.")
    _, body = _check(None, body)
    now = iso(now_utc())
    with tx(conn):
        conn.execute("INSERT INTO query_messages(query_id, author_id, from_staff, body, at) VALUES (?,?,0,?,?)", (query_id, user.id, body, now))
        conn.execute("UPDATE queries SET status='open', updated_at=? WHERE id=?", (now, query_id))
        audit(conn, user, "query_follow_up", "query", query_id)
    n = conn.execute("SELECT COUNT(*) FROM query_messages WHERE query_id=?", (query_id,)).fetchone()[0]
    lines = [f"{user.username} added to query {ref(query_id)} ({q['subject']}):", "", body, "", "Answer it in the portal: 💬 Queries."]
    notify.notify_staff(conn, policy, ("admin",), "query_follow_up", f"[{ref(query_id)}] Follow-up: {q['subject']}", lines, f"queryfu:{query_id}:{n}", force=True)
    if _inbox(policy):
        notify.notify_address(conn, policy, _inbox(policy), "query_follow_up", f"[{ref(query_id)}] Follow-up: {q['subject']}", lines, f"queryfuinbox:{query_id}:{n}")
    notify.notify_user(conn, policy, user.id, "query_ack", f"Follow-up added to {ref(query_id)}",
                       [f"Your additional message on {ref(query_id)} was sent to the incubation team.", "", body], f"queryfuack:{query_id}:{n}")


def reply(conn, policy: dict, admin: User, query_id: int, body: str, close: bool = False) -> None:
    authorize(conn, admin, "admin", action="query_reply")
    q = conn.execute("SELECT * FROM queries WHERE id=?", (query_id,)).fetchone()
    if not q:
        raise WorkflowError("Query not found.")
    _, body = _check(None, body)
    now = iso(now_utc())
    with tx(conn):
        conn.execute("INSERT INTO query_messages(query_id, author_id, from_staff, body, at) VALUES (?,?,1,?,?)", (query_id, admin.id, body, now))
        conn.execute("UPDATE queries SET status=?, updated_at=? WHERE id=?", ("closed" if close else "answered", now, query_id))
        audit(conn, admin, "query_answered", "query", query_id, {"closed": close})
    n = conn.execute("SELECT COUNT(*) FROM query_messages WHERE query_id=?", (query_id,)).fetchone()[0]
    notify.notify_user(conn, policy, q["user_id"], "query_answer", f"Reply to your query {ref(query_id)}: {q['subject']}",
                       ["The incubation team has replied to your query:", "", body, "",
                        "This query is now closed. Write a new query if you need more help." if close else
                        "If you need more help, reply in the portal under 💬 Ask the Incubation Team (replying to this email does not reach us)."],
                       f"queryans:{query_id}:{n}")


def set_status(conn, admin: User, query_id: int, status: str) -> None:
    authorize(conn, admin, "admin", action="query_status")
    if status not in ("open", "answered", "closed"):
        raise WorkflowError("Unknown status")
    if not conn.execute("UPDATE queries SET status=?, updated_at=? WHERE id=?", (status, iso(now_utc()), query_id)).rowcount:
        raise WorkflowError("Query not found.")
    audit(conn, admin, "query_status", "query", query_id, {"status": status})


def my_queries(conn, user: User) -> list[dict]:
    authorize(conn, user, "student", "reviewer", action="my_queries")
    return [dict(r) | {"ref": ref(r["id"]), "messages": _messages(conn, r["id"])}
            for r in conn.execute("SELECT * FROM queries WHERE user_id=? ORDER BY updated_at DESC", (user.id,))]


def inbox(conn, admin: User, status: str = "", category: str = "", search: str = "") -> list[dict]:
    authorize(conn, admin, "admin", action="query_inbox")
    sql = ("SELECT q.*, u.username, u.role, u.email, t.team_code, (SELECT COUNT(*) FROM query_messages m WHERE m.query_id=q.id) AS n_messages "
           "FROM queries q JOIN users u ON u.id=q.user_id LEFT JOIN teams t ON t.id=q.team_id WHERE 1=1")
    args: list = []
    if status:
        sql += " AND q.status=?"
        args.append(status)
    if category:
        sql += " AND q.category=?"
        args.append(category)
    if search:
        sql += " AND (q.subject LIKE ? OR u.username LIKE ? OR t.team_code LIKE ? OR EXISTS (SELECT 1 FROM query_messages m WHERE m.query_id=q.id AND m.body LIKE ?))"
        args += [f"%{search}%"] * 4
    rows = [dict(r) | {"ref": ref(r["id"])} for r in conn.execute(sql + " ORDER BY CASE q.status WHEN 'open' THEN 0 WHEN 'answered' THEN 1 ELSE 2 END, q.updated_at DESC", args)]
    return rows


def thread(conn, admin: User, query_id: int) -> list[dict]:
    authorize(conn, admin, "admin", action="query_thread")
    return _messages(conn, query_id)


def counts(conn) -> dict:
    return {r["status"]: r["n"] for r in conn.execute("SELECT status, COUNT(*) AS n FROM queries GROUP BY status")}


def ai_draft(conn, admin: User, query_id: int, idx, client, rag_cfg: dict) -> str:
    """A suggested reply grounded in the approved knowledge base. The admin always edits/approves before sending."""
    authorize(conn, admin, "admin", action="query_ai_draft")
    from rag.answer import answer
    msgs = [m for m in _messages(conn, query_id) if not m["from_staff"]]
    if not msgs:
        raise WorkflowError("Nothing to answer.")
    q = conn.execute("SELECT subject FROM queries WHERE id=?", (query_id,)).fetchone()
    res = answer(f"{q['subject']}. {msgs[-1]['body']}", idx, "student", client, rag_cfg, conn)
    text = res["answer"].strip()
    if res["sources"]:
        text += "\n\nReference: " + "; ".join(s["label"] for s in res["sources"][:3])
    return "Dear participant,\n\n" + text + "\n\nRegards,\nIncubation Team"
