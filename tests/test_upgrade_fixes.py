"""Fixes found while upgrading a real installation: moved project folder, old queued emails, generic-word matches."""
import dataclasses
import shutil

from config.settings import ROOT
from conftest import do_submit, good_docx, make_team
from evaluation import synthetic as sy
from notifications import outbox
from notifications.service import alert_delivery_failures
from rag.answer import answer
from rag.index import HybridIndex
from workflow import services as sv
from workflow import setup


def test_template_found_after_project_folder_moved(env, tmp_path, monkeypatch):
    tpl = sy.make_docx([(h, "") for h in sy.SECTIONS])
    pol = setup.save_template(env["conn"], env["admin"], env["p"], env["s"].storage_dir, "tpl.docx", tpl,
                              {h: [h.lower()] for h in sy.SECTIONS}, 100, 3000, [".docx"])
    stored = pol["template"]["file"]["path"]
    # simulate a database created on Windows in another folder: the saved full path no longer exists
    pol["template"]["file"]["path"] = r"C:\Users\someone\Documents\old_folder\storage\templates\\" + stored.split("/")[-1]
    import config.settings as cs

    monkeypatch.setattr(cs, "get_settings", lambda: env["s"])
    name, data = setup.template_file(pol)
    assert name == "tpl.docx" and data == tpl
    pol["template"]["file"]["path"] = r"C:\gone\nothing_here.docx"
    assert setup.template_file(pol) is None


def test_discard_old_waiting_emails(env):
    sv.set_user_email(env["conn"], env["admin"], env["admin"].id, "admin@example.org", env["p"])
    u = make_team(env, "a")
    do_submit(env, u, good_docx(1))
    c = env["conn"]
    waiting = outbox.count_waiting(c)
    assert waiting >= 4
    assert sv.discard_waiting_emails(c, env["admin"]) == waiting and outbox.count_waiting(c) == 0
    rows = c.execute("SELECT status, last_error FROM notifications").fetchall()
    assert all(r[0] == "failed" and r[1] == outbox.DISCARDED for r in rows)
    assert alert_delivery_failures(c, env["p"]) == 0                 # discarded mail is not reported as a delivery failure
    s = dataclasses.replace(env["s"], smtp_host="127.0.0.1", smtp_port=9, smtp_from="x@example.org", smtp_tls=False)
    assert outbox.deliver_pending(c, s)["note"] == "Nothing to send."  # never sent later
    do_submit(env, make_team(env, "b"), good_docx(2))                 # new emails are queued normally afterwards
    assert outbox.count_waiting(c) > 0
    assert c.execute("SELECT COUNT(*) FROM audit_events WHERE action='emails_discarded'").fetchone()[0] == 1


def test_discard_is_admin_only(env):
    import pytest

    from security.auth import AuthError

    with pytest.raises(AuthError):
        sv.discard_waiting_emails(env["conn"], make_team(env, "a"))


def test_single_generic_word_is_not_an_answer(tmp_path):
    kb = tmp_path / "kb"
    shutil.copytree(ROOT / "kb", kb)
    for n in range(6):  # more documents make a common word like "team" rarer and score higher
        (kb / f"extra{n}.md").write_text(f"# Track {n}\nProblem statement {n} about solar, water and health projects for villages.")
    import json

    man = json.loads((kb / "manifest.json").read_text())
    man["documents"] += [{"file": f"extra{n}.md", "version": "1", "doc_type": "x", "status": "approved", "audience": "student"} for n in range(6)]
    (kb / "manifest.json").write_text(json.dumps(man))
    idx = HybridIndex.build(kb, tmp_path / "ix")
    assert answer("What is the prize money for the winning team?", idx, "student", None)["status"] == "abstained"
    assert answer("How many members can a team have?", idx, "student", None)["hits"][0]["source"] == "incubation_guidelines.md"
    assert HybridIndex.coverage(["members", "team"], "A team must have two or three members.") == 1.0
    assert HybridIndex.coverage(["prize", "money", "winning", "team"], "A team must have two members.") == 0.25


def test_dotenv_ignored_in_tests_but_read_by_portal(tmp_path, monkeypatch):
    import os

    from config.settings import load_env

    f = tmp_path / ".env"
    f.write_text("SMTP_HOST=smtp.example.org\n")
    monkeypatch.delenv("SMTP_HOST", raising=False)
    load_env(f)                                    # INCUB_NO_DOTENV=1 during tests -> ignored
    assert "SMTP_HOST" not in os.environ
    monkeypatch.delenv("INCUB_NO_DOTENV")
    load_env(f)                                    # normal portal start -> read
    assert os.environ["SMTP_HOST"] == "smtp.example.org"
    monkeypatch.delenv("SMTP_HOST")
