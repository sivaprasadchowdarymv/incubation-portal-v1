"""Who gets which email: acknowledgements, alerts and deadline reminders (all queued in the outbox first)."""
from __future__ import annotations

from datetime import datetime, timedelta

from notifications.outbox import DISCARDED, MAX_ATTEMPTS, enqueue

DONE_STATES = {"ACCEPTED_FOR_EVALUATION", "REJECTED_BY_AUTHORIZED_REVIEW", "DROPPED", "DEADLINE_MISSED"}
WAITING_STATES = {"SUBMITTED", "VALIDATING", "PENDING_REVIEW", "REVIEW_CLEARED"}


def _cfg(policy: dict) -> dict:
    return policy.get("notifications", {})


def render(policy: dict, greeting: str, lines: list[str]) -> str:
    c = _cfg(policy)
    body = [greeting, ""] + lines + ["", f"Open the portal: {c.get('app_url', '')}".rstrip(": "), "",
                                     f"— {c.get('programme_name', 'Incubation Programme')} (automated message, please do not reply)"]
    return "\n".join(body)


def team_recipients(conn, policy: dict, team_id: int) -> list[str]:
    t = conn.execute("SELECT contact_email FROM teams WHERE id=?", (team_id,)).fetchone()
    emails = [t["contact_email"]] if t else []
    if _cfg(policy).get("notify_members", True):
        emails += [r["email"] for r in conn.execute("SELECT email FROM team_members WHERE team_id=? AND email IS NOT NULL AND email != ''", (team_id,))]
    return list(dict.fromkeys(e.strip().lower() for e in emails if e))


def notify_team(conn, policy: dict, team_id: int, event: str, subject: str, lines: list[str], key: str) -> int:
    t = conn.execute("SELECT name, team_code FROM teams WHERE id=?", (team_id,)).fetchone()
    body = render(policy, f"Dear team {t['name']} ({t['team_code']}),", lines)
    subj = f"[{t['team_code']}] {subject}"
    return sum(enqueue(conn, e, event, subj, body, f"{key}:{e}") for e in team_recipients(conn, policy, team_id))


def notify_staff(conn, policy: dict, roles: tuple[str, ...], event: str, subject: str, lines: list[str], key: str, force: bool = False) -> int:
    """force=True: operational alerts that ignore the alert_reviewers switch (reminders, duplicates, delivery failures)."""
    if not force and not _cfg(policy).get("alert_reviewers", True):
        return 0
    rows = conn.execute(f"SELECT DISTINCT lower(email) AS email FROM users WHERE active=1 AND email IS NOT NULL AND email != '' AND role IN ({','.join('?' * len(roles))})", roles)
    body = render(policy, "Hello,", lines)
    return sum(enqueue(conn, r["email"], event, f"[Incubation] {subject}", body, f"{key}:{r['email']}") for r in rows)


def notify_address(conn, policy: dict | None, email: str | None, event: str, subject: str, lines: list[str], key: str, greeting: str = "Hello,") -> int:
    """Acknowledgement to one specific address (account holder, reviewer, admin who acted)."""
    if not email:
        return 0
    e = email.strip().lower()
    return int(enqueue(conn, e, event, f"[Incubation] {subject}", render(policy or {}, greeting, lines), f"{key}:{e}"))


def notify_user(conn, policy: dict | None, user_id: int, event: str, subject: str, lines: list[str], key: str) -> int:
    r = conn.execute("SELECT username, email FROM users WHERE id=?", (user_id,)).fetchone()
    return notify_address(conn, policy, r["email"] if r else None, event, subject, lines, key, f"Dear {r['username']}," if r else "Hello,")


def checks_summary(conn, sub_id: int) -> list[str]:
    out = []
    for r in conn.execute("SELECT check_id, result, message, guidance FROM validation_results WHERE submission_id=? AND result IN ('fail','warn') ORDER BY id", (sub_id,)):
        out.append(f"  • {r['message']}" + (f"\n    → {r['guidance']}" if r["guidance"] else ""))
    return out


def send_deadline_reminders(conn, policy: dict, deadline: datetime, now: datetime) -> int:
    """Remind every team that still has to act. Only the most recent due reminder is sent (no burst after downtime)."""
    hours = sorted(_cfg(policy).get("reminder_hours_before_deadline", []))
    if now > deadline or not hours:
        return 0
    due = [h for h in hours if now >= deadline - timedelta(hours=h)]
    if not due:
        return 0
    h = min(due)
    left = deadline - now
    left_txt = f"{left.days} day(s) {left.seconds // 3600} hour(s)" if left.days else f"{left.seconds // 3600} hour(s) {left.seconds % 3600 // 60} minute(s)"
    sent = 0
    maxa = policy["submission"]["max_attempts"]
    for t in conn.execute("SELECT * FROM teams").fetchall():
        last = conn.execute("SELECT status, attempt_no FROM submissions WHERE team_id=? ORDER BY attempt_no DESC LIMIT 1", (t["id"],)).fetchone()
        if last and (last["status"] in DONE_STATES or last["status"] in WAITING_STATES):
            continue
        if not t["ps_locked"]:
            todo = "Your team has not yet selected and locked a track and problem statement, and has not submitted an abstract."
        elif not last:
            todo = "Your team has not submitted its abstract yet."
        else:
            todo = f"Your last attempt needs corrections. You have {maxa - last['attempt_no']} attempt(s) left."
        lines = [todo, f"The submission deadline is {deadline.strftime('%d %b %Y, %I:%M %p %Z')} — {left_txt} from now.",
                 "Late submissions cannot be accepted."]
        sent += notify_team(conn, policy, t["id"], "deadline_reminder", f"Reminder: deadline in {left_txt}", lines, f"remind:{t['id']}:{h}h")
    return sent


def send_review_reminders(conn, policy: dict, now: datetime) -> int:
    """Remind reviewers/admins about review cases left open longer than notifications.review_reminder_hours (once per case per day)."""
    hrs = int(_cfg(policy).get("review_reminder_hours", 24) or 0)
    if hrs <= 0:
        return 0
    cutoff = (now - timedelta(hours=hrs)).isoformat(timespec="seconds")
    rows = conn.execute("SELECT c.submission_id, c.created_at, c.reasons, t.team_code FROM review_cases c JOIN submissions s ON s.id=c.submission_id "
                        "JOIN teams t ON t.id=s.team_id WHERE c.status='open' AND c.created_at <= ? ORDER BY c.created_at", (cutoff,)).fetchall()
    if not rows:
        return 0
    lines = [f"{len(rows)} review case(s) have been waiting more than {hrs} hour(s):"]
    lines += [f"  • Submission #{r['submission_id']} — team {r['team_code']} — waiting since {r['created_at'][:16].replace('T', ' ')} UTC" for r in rows[:30]]
    lines += ["", "Teams cannot resubmit while their case is pending. Please record a decision in the Review Queue."]
    return notify_staff(conn, policy, ("reviewer", "admin"), "review_reminder", f"{len(rows)} review case(s) awaiting decision", lines,
                        f"reviewremind:{now.strftime('%Y-%m-%d')}:{','.join(str(r['submission_id']) for r in rows)}", force=True)


def alert_delivery_failures(conn, policy: dict) -> int:
    """Tell admins (once per message) when an email has permanently failed after the maximum number of attempts."""
    rows = conn.execute("SELECT id, recipient, subject, last_error FROM notifications WHERE status='failed' AND attempts >= ? "
                        "AND event_type != 'delivery_failure_alert' AND COALESCE(last_error, '') != ? ORDER BY id", (MAX_ATTEMPTS, DISCARDED)).fetchall()
    n = 0
    for r in rows:
        n += notify_staff(conn, policy, ("admin",), "delivery_failure_alert", f"Email could not be delivered to {r['recipient']}",
                          [f"Subject: {r['subject']}", f"Last error: {r['last_error']}", "", "Check the address or the SMTP settings; the message is listed on the Notifications page."],
                          f"deliveryfail:{r['id']}", force=True)
    return n


def outbox_counts(conn) -> dict:
    return {r[0]: r[1] for r in conn.execute("SELECT status, COUNT(*) FROM notifications GROUP BY status")}


def recent_failures(conn, limit: int = 20) -> list[dict]:
    return [dict(r) for r in conn.execute("SELECT id, recipient, subject, attempts, last_error FROM notifications WHERE status='failed' ORDER BY id DESC LIMIT ?", (limit,))]

