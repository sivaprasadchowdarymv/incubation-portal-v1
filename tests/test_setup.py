import io
import json
import shutil
import sqlite3

import pandas as pd
import pytest

from conftest import BEFORE, KW, do_submit, make_student, make_team
from config.settings import ROOT
from database.db import MIGRATIONS, SCHEMA_V1, connect, migrate
from evaluation import synthetic as sy
from rag.answer import answer
from rag.index import HybridIndex
from security.auth import AuthError
from workflow import services as sv
from workflow import setup
from workflow.context import open_app

CSV = setup.catalogue_template_csv()


def csv_bytes(rows):
    return pd.DataFrame(rows).to_csv(index=False).encode()


@pytest.fixture
def kb(tmp_path):
    d = tmp_path / "kb"
    shutil.copytree(ROOT / "kb", d)
    return d


# ---------- catalogue parsing ----------

def test_parse_template_csv_and_aliases():
    rows, errs = setup.parse_catalogue("c.csv", CSV)
    assert not errs and len(rows) == 3 and rows[0]["keywords"][:2] == ["irrigation", "soil moisture"]
    alias = csv_bytes([{"Domain": "Space", "Domain ID": "SPC", "PS No": "SP-1", "Problem Statement": "Satellite data for farmers"}])
    rows, errs = setup.parse_catalogue("c.csv", alias)
    assert not errs and rows[0]["track_id"] == "SPC" and rows[0]["keywords"]  # keywords auto-derived from title


def test_parse_xlsx_and_json():
    buf = io.BytesIO()
    pd.read_csv(io.BytesIO(CSV)).to_excel(buf, index=False)
    rows, errs = setup.parse_catalogue("c.xlsx", buf.getvalue())
    assert not errs and len(rows) == 3
    j = json.dumps({"domains": [{"id": "X1", "name": "X", "problem_statements": [{"id": "X-01", "title": "Thing", "keywords": ["a"]}]}]}).encode()
    rows, errs = setup.parse_catalogue("c.json", j)
    assert not errs and rows[0]["ps_id"] == "X-01"


@pytest.mark.parametrize("name,data,msg", [
    ("c.csv", b"foo,bar\n1,2\n", "Missing required column"),
    ("c.csv", b"", "empty"),
    ("c.pdf", b"x", "Unsupported"),
    ("c.xlsx", b"not an excel file", "Could not read"),
    ("c.csv", b"track_id,track_name,ps_id,ps_title\nA,Alpha,P1,One\nA,Alpha,P1,Two\n", "duplicate ps_id"),
    ("c.csv", b"track_id,track_name,ps_id,ps_title\nA,Alpha,P1,One\nA,Beta,P2,Two\n", "two different names"),
    ("c.csv", b"track_id,track_name,ps_id,ps_title\nA B,Alpha,P1,One\n", "track_id"),
    ("c.csv", b"track_id,track_name,ps_id,ps_title\nA,Alpha,P1,\n", "required"),
])
def test_parse_errors(name, data, msg):
    rows, errs = setup.parse_catalogue(name, data)
    assert errs and msg in " ".join(errs)


# ---------- catalogue import ----------

def test_import_replace_keeps_locked_team(env, kb):
    conn = env["conn"]
    u = make_team(env, "old", ps=("AGRI", "AG-01"))
    new = csv_bytes([{"track_id": "SPC", "track_name": "Space Tech", "ps_id": "SP-01", "ps_title": "Satellite crop maps",
                      "keywords": "satellite; crop; map; farmer"}])
    s = setup.import_catalogue(conn, env["admin"], "new.csv", new, replace=True, kb_dir=kb)
    assert s["tracks"] == 1 and s["deactivated_problem_statements"] == 4
    assert [d["id"] for d in sv.catalog(conn)] == ["SPC"]
    assert do_submit(env, u, sy.make_docx(sy.abstract(1, 50, KW)))["status"] == "ACCEPTED_FOR_EVALUATION"  # locked AG-01 still works
    v = make_team(env, "new", ps=("SPC", "SP-01"))
    assert sv.get_team(conn, v, v.team_id)["ps_id"] == "SP-01"
    with pytest.raises(sv.WorkflowError):
        sv.select_problem_statement(conn, make_student(conn, "late"), "AGRI", "AG-02")  # no team; and AG-02 hidden anyway
    assert conn.execute("SELECT COUNT(*) FROM audit_events WHERE action='catalogue_imported'").fetchone()[0] == 1


def test_hidden_ps_cannot_be_selected(env):
    conn = env["conn"]
    setup.import_catalogue(conn, env["admin"], "n.csv", csv_bytes([{"track_id": "SPC", "track_name": "Space", "ps_id": "SP-01", "ps_title": "Sat"}]))
    with pytest.raises(sv.WorkflowError, match="Unknown problem statement"):
        make_team(env, "x", ps=("AGRI", "AG-01"))


def test_merge_mode_and_locked_ps_cannot_move(env):
    conn = env["conn"]
    make_team(env, "a", ps=("AGRI", "AG-01"))
    s = setup.import_catalogue(conn, env["admin"], "m.csv", csv_bytes([{"track_id": "SPC", "track_name": "Space", "ps_id": "SP-01", "ps_title": "Sat"}]), replace=False)
    assert s["deactivated_problem_statements"] == 0 and len(sv.catalog(conn)) == 4
    with pytest.raises(sv.WorkflowError, match="already locked"):
        setup.import_catalogue(conn, env["admin"], "m.csv", csv_bytes([{"track_id": "SPC", "track_name": "Space", "ps_id": "AG-01", "ps_title": "Moved"}]))


def test_import_requires_admin_and_valid_file(env):
    u = make_team(env, "a", ps=None)
    with pytest.raises(AuthError):
        setup.import_catalogue(env["conn"], u, "c.csv", CSV)
    with pytest.raises(sv.WorkflowError, match="Catalogue rejected"):
        setup.import_catalogue(env["conn"], env["admin"], "c.csv", b"foo\n1\n")


def test_uploaded_catalogue_survives_restart(env):
    conn = env["conn"]
    setup.import_catalogue(conn, env["admin"], "n.csv", csv_bytes([{"track_id": "SPC", "track_name": "Space", "ps_id": "SP-01", "ps_title": "Sat"}]))
    conn.close()
    c2, _, _ = open_app(env["s"], env["p"])  # restart re-reads policy.json
    assert [d["id"] for d in sv.catalog(c2)] == ["SPC"]
    env["conn"] = c2


def test_catalogue_published_to_assistant(env, kb, tmp_path):
    setup.import_catalogue(env["conn"], env["admin"], "n.csv",
                           csv_bytes([{"track_id": "SPC", "track_name": "Space Technology", "ps_id": "SP-01", "ps_title": "Satellite crop maps for smallholders"}]), kb_dir=kb)
    assert HybridIndex.needs_rebuild(kb, tmp_path / "ix")
    idx = HybridIndex.build(kb, tmp_path / "ix")
    r = answer("Which problem statements are in the Space Technology track?", idx, "student", None)
    assert any(s["label"].startswith(setup.GENERATED_CATALOGUE) for s in r["sources"])


# ---------- template / format ----------

def test_detect_sections_docx_pptx_pdf():
    secs = [("Problem Statement", "x"), ("Market Analysis", "y"), ("Team Details", "z")]
    for maker, name in ((sy.make_docx, "t.docx"), (sy.make_pptx, "t.pptx"), (sy.make_pdf, "t.pdf")):
        got = setup.detect_template_sections(name, maker(secs))
        assert got == ["Problem Statement", "Market Analysis", "Team Details"], (name, got)
    with pytest.raises(sv.WorkflowError):
        setup.detect_template_sections("t.docx", b"PK\x03\x04" + b"\x00" * 50)


def test_sections_text_roundtrip():
    t = "Problem Statement | problem, problem definition\nBudget"
    d = setup.sections_from_text(t)
    assert d == {"Problem Statement": ["problem", "problem definition"], "Budget": ["budget"]}
    assert setup.sections_from_text(setup.sections_to_text(d)) == d


def test_save_template_changes_validation(env, kb):
    conn, admin = env["conn"], env["admin"]
    tpl = sy.make_docx([(h, "") for h in sy.SECTIONS] + [("Market Analysis", "")])
    secs = {h: [h.lower()] for h in setup.detect_template_sections("tpl.docx", tpl)}
    pol = setup.save_template(conn, admin, env["p"], env["s"].storage_dir, "tpl.docx", tpl, secs, 100, 3000, [".docx", ".pdf"], kb)
    assert pol["version"].endswith("+admin.r1") and "Market Analysis" in pol["validation"]["required_sections"]
    assert setup.template_file(pol)[0] == "tpl.docx"
    assert any(d["file"] == setup.GENERATED_TEMPLATE for d in setup.kb_documents(kb))
    env["p"] = pol
    u = make_team(env, "a")
    r = do_submit(env, u, sy.make_docx(sy.abstract(1, 50, KW)))
    assert r["status"] == "RESUBMISSION_ALLOWED" and r["missing_sections"] == ["Market Analysis"] and r["policy_version"] == pol["version"]
    with pytest.raises(sv.SubmissionBlocked) as e:
        do_submit(env, u, sy.make_pptx(sy.abstract(1, 50, KW)), "a.pptx")
    assert e.value.code == "INVALID_EXTENSION"
    with pytest.raises(AuthError):
        setup.save_template(conn, u, env["p"], env["s"].storage_dir, "tpl.docx", tpl, secs, 100, 3000, [".docx"])


@pytest.mark.parametrize("changes,msg", [
    ({"submission.deadline": "next friday"}, "Deadline"),
    ({"validation.min_words": 500, "validation.max_words": 400}, "lower"),
    ({"submission.allowed_extensions": [".exe"]}, "subset"),
    ({"validation.required_sections": {}}, "At least one"),
    ({"team.max_members": 9}, "cannot be changed"),
    ({"review.approved_rejection_policies": [{"id": "bad id!", "title": "x"}]}, "rejection policy"),
])
def test_settings_validation(env, changes, msg):
    with pytest.raises(sv.WorkflowError, match=msg):
        setup.update_settings(env["conn"], env["admin"], changes, env["p"])


def test_settings_deadline_and_rejection_policy(env):
    conn, base = env["conn"], env["p"]
    pol = setup.update_settings(conn, env["admin"], {"submission.deadline": "2026-10-31T17:00:00",
                                                     "review.approved_rejection_policies": [{"id": "ORIG-1", "title": "Verbatim copy"}]}, base)
    assert sv.deadline(pol).isoformat() == "2026-10-31T17:00:00+05:30"
    env["p"] = pol
    u = make_team(env, "a")
    with pytest.raises(sv.SubmissionBlocked) as e:
        do_submit(env, u, sy.make_docx(sy.abstract(1, 50, KW)), now=BEFORE)  # BEFORE is 1 Nov 2026 UTC > new deadline
    assert e.value.code == "DEADLINE_PASSED"
    assert setup.effective_policy(conn, base)["review"]["approved_rejection_policies"][0]["id"] == "ORIG-1"
    assert base["submission"]["deadline"] == "2026-11-30T23:59:59"  # base policy object never mutated


# ---------- knowledge base ----------

def test_kb_upload_status_and_archive(env, kb, tmp_path):
    conn, admin = env["conn"], env["admin"]
    doc = b"# Evaluation Criteria\nIdeas are scored on novelty, feasibility and impact by the jury panel."
    e = setup.add_kb_document(conn, admin, kb, "../../eval criteria.md", doc, "2.0", "evaluation_criteria", "approved", "student")
    assert e["file"] == "eval_criteria.md" and (kb / "eval_criteria.md").exists()
    setup.add_kb_document(conn, admin, kb, "eval_criteria.md", doc + b" Updated.", "2.1", "evaluation_criteria")
    assert len(list((kb / "_archive").iterdir())) == 1
    idx = HybridIndex.build(kb, tmp_path / "ix")
    assert any(c["source"] == "eval_criteria.md" for c in idx.chunks)
    setup.set_kb_status(conn, admin, kb, "eval_criteria.md", "retired")
    assert HybridIndex.needs_rebuild(kb, tmp_path / "ix")
    assert not any(c["source"] == "eval_criteria.md" for c in HybridIndex.build(kb, tmp_path / "ix").chunks)


@pytest.mark.parametrize("name,data,msg", [
    ("x.exe", b"MZ", "must be one of"),
    ("manifest.json", b"{}", "must be one of"),
    ("generated_tracks_and_problem_statements.md", b"# x\ny", "reserved"),
    ("scan.pdf", sy.make_pdf([]), "No readable text"),
    ("e.md", b"", "empty"),
])
def test_kb_upload_rejections(env, kb, name, data, msg):
    with pytest.raises(sv.WorkflowError, match=msg):
        setup.add_kb_document(env["conn"], env["admin"], kb, name, data, "1", "faq")


def test_kb_upload_requires_admin(env, kb):
    sv.admin_create_user(env["conn"], env["admin"], "rev9", "reviewpass1", "reviewer")
    rev = sv.authenticate(env["conn"], "rev9", "reviewpass1")
    with pytest.raises(AuthError):
        setup.add_kb_document(env["conn"], rev, kb, "a.md", b"# A\ntext", "1", "faq")


# ---------- migration ----------

def test_v1_database_upgrades_without_data_loss(tmp_path):
    c = connect(tmp_path / "old.db")
    c.execute("CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)")
    c.executescript("BEGIN;" + SCHEMA_V1 + "; INSERT INTO schema_migrations VALUES (1, 'x'); COMMIT;")
    c.execute("INSERT INTO users(username,pw_hash,role,created_at) VALUES ('legacy','x','admin','t')")
    assert migrate(c) == [v for v, _ in MIGRATIONS if v > 1]
    assert c.execute("SELECT username FROM users").fetchone()[0] == "legacy"
    c.execute("INSERT INTO programme_settings(key,value,updated_at) VALUES ('k','1','t')")
    with pytest.raises(sqlite3.IntegrityError):
        c.execute("INSERT INTO programme_settings(key,value,updated_at) VALUES ('k','2','t')")
