"""Internet deployment: data persistence (Hugging Face bucket snapshots), Brevo email API, public-URL protections."""
import dataclasses
import http.server
import json
import sqlite3
import threading

import pytest

from conftest import do_submit, good_docx, make_team
from notifications import outbox
from persist import snapshot
from workflow import services as sv
from workflow import setup

# ---------------- persistence (snapshot to a Storage Bucket) ----------------


@pytest.fixture
def backed(env, tmp_path, monkeypatch):
    s = dataclasses.replace(env["s"], backup_dir=tmp_path / "bucket" / "incubation_backup", kb_dir=tmp_path / "kb")
    (tmp_path / "kb").mkdir()
    (tmp_path / "kb" / "manifest.json").write_text('{"documents": []}')
    monkeypatch.setattr(snapshot, "get_settings", lambda: s)
    return env | {"s2": s}


def test_disabled_without_backup_dir(env, monkeypatch):
    monkeypatch.setattr(snapshot, "get_settings", lambda: env["s"])
    assert snapshot.status()["enabled"] is False
    assert snapshot.backup()["skipped"] and snapshot.restore()["skipped"]
    snapshot.backup_soon()  # must be a harmless no-op


def test_backup_and_restore_roundtrip(backed):
    env, s = backed, backed["s2"]
    u = make_team(env, "a")
    r = do_submit(env, u, good_docx(1))
    upload = s.storage_dir / "uploads" / f"team_{u.team_id}"
    (s.kb_dir / "admin_doc.md").write_text("# Uploaded by admin\ntext")
    b1 = snapshot.backup()
    assert b1["db_copied"] and b1["files_copied"] >= 3
    assert snapshot.backup()["db_copied"] is False and snapshot.backup()["files_copied"] == 0  # nothing changed -> nothing copied
    st = snapshot.status()
    assert st["enabled"] and st["last_backup_at"]

    # simulate a Hugging Face restart: the container disk is wiped
    env["conn"].close()
    for f in s.db_path.parent.glob(s.db_path.name + "*"):  # the database and its -wal/-shm files
        f.unlink()
    for f in upload.iterdir():
        f.unlink()
    (s.kb_dir / "admin_doc.md").unlink()
    out = snapshot.restore()
    assert out["db_restored_from"] == "incubation.db" and out["files_restored"] >= 3
    c = sqlite3.connect(s.db_path)
    assert c.execute("SELECT status FROM submissions WHERE id=?", (r["submission_id"],)).fetchone()[0] == "ACCEPTED_FOR_EVALUATION"
    assert any(upload.iterdir()) and (s.kb_dir / "admin_doc.md").exists()
    c.close()
    env["conn"] = sqlite3.connect(s.db_path)


def test_restore_never_overwrites_live_db_and_uses_older_copy_if_damaged(backed):
    env, s = backed, backed["s2"]
    make_team(env, "a")
    snapshot.backup()
    make_team(env, "b")
    snapshot.backup()                              # second generation -> previous kept as incubation.prev.db
    root = s.backup_dir / "db"
    assert (root / "incubation.prev.db").exists()
    assert snapshot.restore()["db_restored_from"] is None   # live DB exists: untouched
    (root / "incubation.db").write_bytes(b"this is not a database")  # damaged newest copy
    env["conn"].close()
    for f in s.db_path.parent.glob(s.db_path.name + "*"):  # the database and its -wal/-shm files
        f.unlink()
    out = snapshot.restore()
    assert out["db_restored_from"] == "incubation.prev.db" and out["rejected"] == ["incubation.db"]
    c = sqlite3.connect(s.db_path)
    assert c.execute("SELECT COUNT(*) FROM teams").fetchone()[0] == 1  # state of the older snapshot
    c.close()
    env["conn"] = sqlite3.connect(s.db_path)


def test_backup_soon_runs_in_background(backed):
    import time

    make_team(backed, "a")
    snapshot.backup_soon()
    snapshot.backup_soon()  # bursts are coalesced
    for _ in range(50):
        if snapshot.status()["last_backup_at"]:
            break
        time.sleep(0.2)
    assert snapshot.status()["last_backup_at"]


# ---------------- Brevo email API (works where SMTP ports are blocked) ----------------


class _Brevo(http.server.BaseHTTPRequestHandler):
    calls: list = []
    reply = (201, {"messageId": "<1@brevo>"})

    def do_POST(self):  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        type(self).calls.append({"key": self.headers.get("api-key"), **body})
        code, payload = type(self).reply
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(payload).encode())

    def log_message(self, *a):
        pass


@pytest.fixture
def brevo(env):
    _Brevo.calls, _Brevo.reply = [], (201, {"messageId": "<1@brevo>"})
    srv = http.server.HTTPServer(("127.0.0.1", 0), _Brevo)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    s = dataclasses.replace(env["s"], email_provider="brevo", brevo_api_key="xkeysib-test", smtp_host=None,
                            smtp_from="portal@example.org", brevo_api_url=f"http://127.0.0.1:{srv.server_port}/v3/smtp/email")
    yield s
    srv.shutdown()


def test_brevo_configuration_rules(env):
    s = env["s"]
    assert not outbox.email_configured(dataclasses.replace(s, email_provider="brevo", brevo_api_key=None, smtp_from="a@b.org"))
    assert not outbox.email_configured(dataclasses.replace(s, email_provider="brevo", brevo_api_key="k", smtp_from=None))
    assert outbox.email_configured(dataclasses.replace(s, email_provider="brevo", brevo_api_key="k", smtp_from="a@b.org"))
    assert "Brevo" in outbox.describe(dataclasses.replace(s, email_provider="brevo", brevo_api_key="k", smtp_from="a@b.org"))


def test_brevo_delivers_workflow_emails(env, brevo):
    u = make_team(env, "a")
    do_submit(env, u, good_docx(1))
    res = outbox.deliver_pending(env["conn"], brevo)
    queued = env["conn"].execute("SELECT COUNT(*) FROM notifications").fetchone()[0]
    assert res["sent"] == queued and res["failed"] == 0 and len(_Brevo.calls) == queued
    c = _Brevo.calls[-1]
    assert c["key"] == "xkeysib-test" and c["sender"]["email"] == "portal@example.org" and c["to"] == [{"email": "a@example.org"}]
    assert c["subject"] and c["textContent"]
    ok, msg = outbox.send_test_email(brevo, "me@example.org")
    assert ok and "Brevo" in msg


def test_brevo_wrong_key_stops_pass(env, brevo):
    for i in range(3):
        outbox.enqueue(env["conn"], f"x{i}@example.org", "t", "s", "b", f"k{i}")
    _Brevo.reply = (401, {"code": "unauthorized", "message": "Key not found"})
    res = outbox.deliver_pending(env["conn"], brevo)
    assert res["sent"] == 0 and res["failed"] == 3 and len(_Brevo.calls) == 1 and "401" in res["note"]
    rows = env["conn"].execute("SELECT status, last_error FROM notifications").fetchall()
    assert all(r[0] == "failed" and "Key not found" in r[1] for r in rows)
    assert not outbox.send_test_email(brevo, "me@example.org")[0]


def test_brevo_bad_recipient_fails_only_that_message(env, brevo):
    outbox.enqueue(env["conn"], "bad", "t", "s", "b", "kb1")
    _Brevo.reply = (400, {"code": "invalid_parameter", "message": "email is not valid in to"})
    res = outbox.deliver_pending(env["conn"], brevo)
    assert res["failed"] == 1 and "400" in env["conn"].execute("SELECT last_error FROM notifications").fetchone()[0]


# ---------------- public-URL protections ----------------


def test_signup_code_required_when_configured(env):
    with pytest.raises(sv.WorkflowError, match="sign-up code"):
        sv.signup_student(env["conn"], "stranger", "password123", access_code="guess", required_code="INC-2026")
    with pytest.raises(sv.WorkflowError):
        sv.signup_student(env["conn"], "stranger2", "password123", required_code="INC-2026")
    assert sv.signup_student(env["conn"], "student_ok", "password123", access_code=" INC-2026 ", required_code="INC-2026")
    assert sv.signup_student(env["conn"], "no_code_needed", "password123")  # no code configured -> open sign-up
    assert env["conn"].execute("SELECT COUNT(*) FROM audit_events WHERE action='signup_rejected_code'").fetchone()[0] == 2


def test_login_lockout_after_repeated_failures(env, monkeypatch):
    for _ in range(sv.LOCKOUT_FAILURES):
        assert sv.authenticate(env["conn"], "admin", "wrong-password") is None
    with pytest.raises(sv.LoginLocked):
        sv.authenticate(env["conn"], "admin", "adminpass1")  # even the right password is refused while locked
    assert sv.authenticate(env["conn"], "someone_else", "x") is None  # other accounts unaffected
    from datetime import timedelta

    real_now = sv.now_utc
    monkeypatch.setattr(sv, "now_utc", lambda: real_now() + timedelta(minutes=sv.LOCKOUT_MINUTES + 1))  # lockout window over
    assert sv.authenticate(env["conn"], "admin", "adminpass1") is not None


def test_public_url_goes_into_emails(monkeypatch):
    from config.settings import ROOT, load_policy

    monkeypatch.setenv("INCUB_PUBLIC_URL", "https://priya-incubation-portal.hf.space/")
    assert load_policy(ROOT / "config" / "policy.json")["notifications"]["app_url"] == "https://priya-incubation-portal.hf.space"


def test_example_template_generated_with_required_headings():
    data = setup.example_template_docx()
    assert setup.detect_template_sections("t.docx", data) == ["Problem Statement / Motivation", "Impact", "Methodology", "Deliverables / Outcomes"]


def test_no_binary_files_in_repository():
    """Hugging Face rejects pushes containing binary files; the repository must stay text-only."""
    from config.settings import ROOT

    skip = {".git", ".venv", "storage", "samples", "backup", "__pycache__", ".pytest_cache", ".ruff_cache", "examples"}
    bad = [p for p in ROOT.rglob("*") if p.is_file() and not (set(p.relative_to(ROOT).parts) & skip)
           and p.suffix.lower() in {".docx", ".pptx", ".pdf", ".png", ".jpg", ".zip", ".db"}]
    assert bad == []
