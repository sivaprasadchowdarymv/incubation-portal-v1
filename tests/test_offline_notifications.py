import dataclasses
import socket

import pytest

from conftest import do_submit, good_docx, make_team, near_copy_docx
from config.settings import ROOT
from notifications import outbox
from rag.answer import answer
from rag.index import HybridIndex
from rag.ollama import OllamaClient
from workflow import services as sv


@pytest.fixture
def no_network(monkeypatch):
    attempts = []
    real = socket.socket.connect

    def guarded(self, addr):
        host = addr[0] if isinstance(addr, tuple) else str(addr)
        if host not in ("127.0.0.1", "::1", "localhost") and not str(host).startswith("/"):
            attempts.append(host)
            raise OSError("network disabled for offline test")
        return real(self, addr)

    monkeypatch.setattr(socket.socket, "connect", guarded)
    return attempts


def test_core_workflow_offline(env, no_network):
    a, b = make_team(env, "a"), make_team(env, "b")
    assert do_submit(env, a, good_docx(1))["status"] == "ACCEPTED_FOR_EVALUATION"
    rb = do_submit(env, b, near_copy_docx(1))
    assert rb["status"] == "PENDING_REVIEW"
    assert sv.decide(env["conn"], env["p"], env["admin"], rb["submission_id"], "cleared", "Reviewed offline, acceptable.")["status"] == "ACCEPTED_FOR_EVALUATION"
    idx = HybridIndex.build(ROOT / "kb", env["tmp"] / "ix")
    r = answer("How many submission attempts?", idx, "student", OllamaClient("http://127.0.0.1:9", "m", "e", timeout=1, retries=0))
    assert r["status"] == "ai_unavailable" and r["sources"]
    assert no_network == []  # nothing tried to reach a non-local host


def test_outbox_queue_dedupe_and_no_false_delivery(env):
    u = make_team(env, "a")
    do_submit(env, u, good_docx())
    c = env["conn"]
    rows = c.execute("SELECT event_type, status FROM notifications").fetchall()
    assert {r[0] for r in rows} == {"registration_ack", "submission_ack", "accepted"} and {r[1] for r in rows} == {"queued_local"}
    assert outbox.enqueue(c, "x@example.org", "t", "s", "b", "k1") and not outbox.enqueue(c, "x@example.org", "t", "s", "b", "k1")
    res = outbox.deliver_pending(c, env["s"])  # SMTP not configured
    assert res["sent"] == 0 and "not configured" in res["note"]
    assert c.execute("SELECT COUNT(*) FROM notifications WHERE status='sent'").fetchone()[0] == 0


def test_smtp_failure_recorded(env, monkeypatch):
    c = env["conn"]
    outbox.enqueue(c, "x@example.org", "t", "s", "b", "k2")

    class Boom:
        def __init__(self, *a, **k):
            raise ConnectionRefusedError("no smtp")

    monkeypatch.setattr(outbox.smtplib, "SMTP", Boom)
    s = dataclasses.replace(env["s"], smtp_host="127.0.0.1", smtp_from="noreply@example.org")
    res = outbox.deliver_pending(c, s)
    assert res["failed"] == 1 and res["sent"] == 0
    assert "ConnectionRefusedError" in c.execute("SELECT last_error FROM notifications WHERE dedupe_key='k2'").fetchone()[0]


def test_smtp_success_marks_sent(env, monkeypatch):
    c = env["conn"]
    outbox.enqueue(c, "x@example.org", "t", "s", "b", "k3")
    sent = []

    class Ok:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def starttls(self, **k): pass
        def login(self, *a): pass
        def send_message(self, m): sent.append(m["To"])

    monkeypatch.setattr(outbox.smtplib, "SMTP", Ok)
    s = dataclasses.replace(env["s"], smtp_host="127.0.0.1", smtp_from="noreply@example.org")
    outbox.deliver_pending(c, s)
    assert "x@example.org" in sent and c.execute("SELECT status FROM notifications WHERE dedupe_key='k3'").fetchone()[0] == "sent"
