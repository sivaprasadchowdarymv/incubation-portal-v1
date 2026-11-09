"""Headless Streamlit UI test (streamlit.testing.AppTest) - widgets drive real backend services."""
import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest

from config.settings import ROOT


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("INCUB_DB_PATH", str(tmp_path / "ui.db"))
    monkeypatch.setenv("INCUB_STORAGE_DIR", str(tmp_path))
    monkeypatch.setenv("INCUB_INDEX_DIR", str(tmp_path / "ix"))
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("OLLAMA_RETRIES", "0")
    st.cache_resource.clear()
    st.cache_data.clear()
    at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=60)
    at.run()
    return at


def test_signup_login_register_lock(app, tmp_path):
    at = app
    assert not at.exception
    at.text_input[2].input("uistudent")
    at.text_input[3].input("password123")
    at.button[1].click().run()
    assert any("Account created" in s.value for s in at.success)
    at.text_input[0].input("uistudent")
    at.text_input[1].input("password123")
    at.button[0].click().run()
    assert not at.exception and at.sidebar.radio[0].value == "My Team"
    at.text_input[0].input("UI Team")
    at.text_input[1].input("ui@example.org")
    for i, v in enumerate(["Ann", "U-1", "", "Bob", "U-2", ""]):
        at.text_input[2 + i].input(v)
    at.button[0].click().run()
    assert not at.exception
    from database.db import connect

    c = connect(tmp_path / "ui.db")
    assert c.execute("SELECT membership_locked FROM teams WHERE name='UI Team'").fetchone()[0] == 1
    at.checkbox[0].check().run()
    at.button[0].click().run()
    assert c.execute("SELECT ps_locked FROM teams WHERE name='UI Team'").fetchone()[0] == 1
    at.sidebar.radio[0].set_value("Submit").run()
    assert not at.exception and any("Attempt 1 of 3" in i.value for i in at.info)


def test_every_page_renders_for_each_role(tmp_path, monkeypatch):
    """Seed real data via services, then open every page of every role and assert no exception."""

    from config.settings import get_settings, load_policy
    from workflow import services as sv
    from workflow.context import open_app

    for k, v in {"INCUB_DB_PATH": tmp_path / "ui.db", "INCUB_STORAGE_DIR": tmp_path, "INCUB_INDEX_DIR": tmp_path / "ix",
                 "OLLAMA_BASE_URL": "http://127.0.0.1:9", "OLLAMA_RETRIES": "0"}.items():
        monkeypatch.setenv(k, str(v))
    conn, s, p = open_app(get_settings(), load_policy())
    sv.bootstrap_admin(conn, "admin", "adminpass1")
    admin = sv.authenticate(conn, "admin", "adminpass1")
    sv.admin_create_user(conn, admin, "rev1", "reviewpass1", "reviewer")
    from conftest import good_docx, near_copy_docx

    for t in ("aa", "bb"):
        sv.signup_student(conn, f"stu_{t}", "password123")
        u = sv.authenticate(conn, f"stu_{t}", "password123")
        sv.register_team(conn, p, u, f"Team {t}", f"{t}@example.org", [{"full_name": "x", "member_ref": f"{t}1"}, {"full_name": "y", "member_ref": f"{t}2"}])
        sv.select_problem_statement(conn, u, "AGRI", "AG-01")
        data = good_docx(1) if t == "aa" else near_copy_docx(1)
        sv.submit(conn, p, s.storage_dir, sv.refresh_user(conn, u), "a.docx", data)
    from rag.index import HybridIndex
    from workflow import evaluation as ev

    acc = conn.execute("SELECT id FROM submissions WHERE status='ACCEPTED_FOR_EVALUATION'").fetchone()[0]
    rev = sv.authenticate(conn, "rev1", "reviewpass1")
    aid = ev.assign(conn, p, admin, acc, rev.id)
    ev.respond(conn, p, rev, aid, True)
    ev.pledge(conn, rev)
    ev.save_review(conn, p, rev, aid, {c["id"]: 4 for c in p["evaluation"]["criteria"]}, "accept", 4, "Strong framing; add a pilot plan with measurable targets.", "", True)
    ev.decide(conn, p, admin, acc, "shortlisted", "Well done", True, True)
    HybridIndex.build(s.kb_dir, s.index_dir)
    from security.auth import User

    pages = {"admin": ("adminpass1", ["Dashboard", "Programme Setup", "Review Queue", "Submissions", "Peer Review", "Users", "Knowledge Base", "Notifications", "Audit Log", "Policy"]),
             "rev1": ("reviewpass1", ["My Reviews", "Review Queue", "Policy Assistant"]),
             "stu_bb": ("password123", ["My Team", "Submit", "History", "Results", "Policy Assistant"]),
             "stu_aa": ("password123", ["My Team", "Results"])}
    for user, (_pw, plist) in pages.items():
        st.cache_resource.clear()
        at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=60)
        r = conn.execute("SELECT * FROM users WHERE username=?", (user,)).fetchone()
        at.session_state["user"] = User(r["id"], r["username"], r["role"], r["team_id"])
        at.run()
        for pg in plist:
            at.sidebar.radio[0].set_value(pg).run()
            assert not at.exception, (user, pg, at.exception)
        if user == "stu_bb":
            at.chat_input[0].set_value("How many members can a team have?").run()
            assert not at.exception
            assert any("incubation_guidelines.md" in m.value for m in at.markdown)
        if user == "stu_aa":
            assert any("Shortlisted" in x.value for x in at.success)
        if user == "rev1":
            at.sidebar.radio[0].set_value("My Reviews").run()
            assert any("Download abstract" in b.label for b in at.get("download_button"))
            at.sidebar.radio[0].set_value("Review Queue").run()
            assert any("SIMILARITY" in e.label for e in at.expander)
