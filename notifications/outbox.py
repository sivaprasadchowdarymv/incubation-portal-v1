"""Persistent local notification outbox with optional SMTP delivery (STARTTLS on 587 or SSL on 465)."""
from __future__ import annotations

import smtplib
import ssl

import requests
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import formatdate, make_msgid

MAX_ATTEMPTS = 5
DISCARDED = "Discarded by admin (never sent)"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def enqueue(conn, recipient: str, event_type: str, subject: str, body: str, dedupe_key: str) -> bool:
    """Returns True if queued, False if an identical notification (dedupe_key) already exists."""
    cur = conn.execute(
        "INSERT OR IGNORE INTO notifications(recipient,event_type,subject,body,dedupe_key,status,created_at) VALUES (?,?,?,?,?, 'queued_local', ?)",
        (recipient, event_type, subject, body, dedupe_key, _now()),
    )
    return cur.rowcount == 1


def provider(settings) -> str:
    return (getattr(settings, "email_provider", "smtp") or "smtp").lower()


def email_configured(settings) -> bool:
    if provider(settings) == "brevo":
        return bool(getattr(settings, "brevo_api_key", None) and settings.smtp_from)
    return bool(settings.smtp_host and settings.smtp_from)


smtp_configured = email_configured  # older name, kept for compatibility


def describe(settings) -> str:
    if not email_configured(settings):
        return "not configured: emails are kept in the outbox"
    if provider(settings) == "brevo":
        return f"Brevo email API, from {settings.smtp_from}"
    return f"SMTP {settings.smtp_host}:{settings.smtp_port}, from {settings.smtp_from}"


class _AuthProblem(RuntimeError):
    """The provider rejected our credentials: stop this pass instead of failing every message one by one."""


def _brevo_send(settings, to: str, subject: str, body: str) -> None:
    r = requests.post(settings.brevo_api_url, timeout=20,
                      headers={"api-key": settings.brevo_api_key, "accept": "application/json", "content-type": "application/json"},
                      json={"sender": {"email": settings.smtp_from, "name": getattr(settings, "email_from_name", "Incubation Portal")},
                            "to": [{"email": to}], "subject": subject, "textContent": body})
    if r.status_code in (200, 201, 202):
        return
    msg = f"Brevo HTTP {r.status_code}: {r.text[:200]}"
    if r.status_code in (401, 403):
        raise _AuthProblem(msg)
    raise RuntimeError(msg)


def _connect(settings):
    if settings.smtp_ssl:
        return smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, timeout=20, context=ssl.create_default_context())
    s = smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=20)
    if settings.smtp_tls:
        s.starttls(context=ssl.create_default_context())
    return s


def _message(settings, to: str, subject: str, body: str) -> EmailMessage:
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = settings.smtp_from, to, subject
    msg["Date"], msg["Message-ID"] = formatdate(localtime=True), make_msgid()
    msg.set_content(body)
    return msg


def send_test_email(settings, to: str) -> tuple[bool, str]:
    if not email_configured(settings):
        return False, ("Email is not configured. SMTP: set SMTP_HOST and SMTP_FROM. Brevo: set EMAIL_PROVIDER=brevo, BREVO_API_KEY and "
                       "EMAIL_FROM. Then restart.")
    if provider(settings) == "brevo":
        try:
            _brevo_send(settings, to, "Incubation portal: test email", "Email delivery from the incubation portal works (Brevo API).")
            return True, f"Test email accepted by Brevo for {to}."
        except Exception as exc:
            return False, f"{exc}"[:300]
    try:
        with _connect(settings) as s:
            if settings.smtp_user:
                s.login(settings.smtp_user, settings.smtp_password or "")
            s.send_message(_message(settings, to, "Incubation portal: test email", "SMTP delivery from the incubation portal works."))
        return True, f"Test email accepted by {settings.smtp_host} for {to}."
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"[:300]


def deliver_pending(conn, settings, limit: int = 100) -> dict:
    """Send queued/failed messages. A message is marked 'sent' only after the SMTP server accepted it."""
    if not email_configured(settings):
        return {"attempted": 0, "sent": 0, "failed": 0, "note": "SMTP not configured; notifications remain in the local outbox."}
    rows = conn.execute("SELECT * FROM notifications WHERE status IN ('queued_local','failed') AND attempts < ? ORDER BY id LIMIT ?", (MAX_ATTEMPTS, limit)).fetchall()
    if not rows:
        return {"attempted": 0, "sent": 0, "failed": 0, "note": "Nothing to send."}
    sent = failed = 0
    if provider(settings) == "brevo":
        for i, r in enumerate(rows):
            try:
                _brevo_send(settings, r["recipient"], r["subject"], r["body"])
                conn.execute("UPDATE notifications SET status='sent', attempts=attempts+1, sent_at=?, last_error=NULL WHERE id=?", (_now(), r["id"]))
                sent += 1
            except _AuthProblem as exc:  # wrong key / unverified sender: record once for every remaining message and stop
                for rr in rows[i:]:
                    conn.execute("UPDATE notifications SET status='failed', attempts=attempts+1, last_error=? WHERE id=?", (str(exc)[:300], rr["id"]))
                return {"attempted": len(rows), "sent": sent, "failed": len(rows) - sent, "note": str(exc)[:300]}
            except Exception as exc:
                conn.execute("UPDATE notifications SET status='failed', attempts=attempts+1, last_error=? WHERE id=?", (f"{type(exc).__name__}: {exc}"[:300], r["id"]))
                failed += 1
        return {"attempted": len(rows), "sent": sent, "failed": failed, "note": ""}
    try:
        server = _connect(settings)
        if settings.smtp_user:
            server.login(settings.smtp_user, settings.smtp_password or "")
    except Exception as exc:  # server unreachable / auth failure: every message stays queued with the error recorded
        err = f"{type(exc).__name__}: {exc}"[:300]
        for r in rows:
            conn.execute("UPDATE notifications SET status='failed', attempts=attempts+1, last_error=? WHERE id=?", (err, r["id"]))
        return {"attempted": len(rows), "sent": 0, "failed": len(rows), "note": err}
    with server:
        for r in rows:
            try:
                server.send_message(_message(settings, r["recipient"], r["subject"], r["body"]))
                conn.execute("UPDATE notifications SET status='sent', attempts=attempts+1, sent_at=?, last_error=NULL WHERE id=?", (_now(), r["id"]))
                sent += 1
            except Exception as exc:
                conn.execute("UPDATE notifications SET status='failed', attempts=attempts+1, last_error=? WHERE id=?", (f"{type(exc).__name__}: {exc}"[:300], r["id"]))
                failed += 1
    return {"attempted": len(rows), "sent": sent, "failed": failed, "note": ""}


def count_waiting(conn) -> int:
    return conn.execute("SELECT COUNT(*) FROM notifications WHERE status IN ('queued_local','failed') AND attempts < ?", (MAX_ATTEMPTS,)).fetchone()[0]


def discard_waiting(conn) -> int:
    """Mark every email that is still waiting as discarded, so it will never be sent (e.g. old test emails queued
    before email delivery was set up). Kept in the table for the record."""
    cur = conn.execute("UPDATE notifications SET status='failed', attempts=?, last_error=? WHERE status IN ('queued_local','failed') AND attempts < ?",
                       (MAX_ATTEMPTS, DISCARDED, MAX_ATTEMPTS))
    return cur.rowcount
