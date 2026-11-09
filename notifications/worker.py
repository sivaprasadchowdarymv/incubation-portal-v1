"""One pass of the email worker: reminders, alerts, delivery with retries (used by scripts/notify_worker.py and the cloud app)."""
from notifications.outbox import deliver_pending, smtp_configured
from notifications.service import alert_delivery_failures, send_deadline_reminders, send_review_reminders
from workflow import evaluation as ev
from workflow import services as sv
from workflow import setup


def one_pass(conn, settings, base) -> dict:
    policy = setup.effective_policy(conn, base)
    now = sv.now_utc()
    queued = send_deadline_reminders(conn, policy, sv.deadline(policy), now)
    review = send_review_reminders(conn, policy, now)
    review += ev.send_due_reminders(conn, policy, now)
    res = deliver_pending(conn, settings)
    alerts = alert_delivery_failures(conn, policy)
    if alerts:
        res2 = deliver_pending(conn, settings)
        res = {**res, "sent": res["sent"] + res2["sent"], "failed": res["failed"] + res2["failed"]}
    out = {"at": sv.iso(now), "reminders_queued": queued, "review_reminders_queued": review, "delivery_alerts_queued": alerts,
           **res, "smtp_configured": smtp_configured(settings)}
    sv.audit(conn, None, "notify_worker_pass", details={k: out[k] for k in ("reminders_queued", "review_reminders_queued", "sent", "failed")})
    return out

