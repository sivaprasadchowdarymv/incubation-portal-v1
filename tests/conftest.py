import dataclasses
import os
from datetime import datetime, timezone

import pytest

# Tests must never read the real .env: it holds the real email account (tests would send real emails) and sign-up code.
os.environ["INCUB_NO_DOTENV"] = "1"

from config.settings import ROOT, get_settings, load_policy
from evaluation import synthetic as sy
from security.auth import User
from workflow import services as sv
from workflow.context import open_app

KW = ["irrigation", "soil moisture", "water", "sensor", "farm"]
_REAL_ENV_KEYS = ["SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASSWORD", "SMTP_FROM", "SMTP_TLS", "SMTP_SSL", "EMAIL_PROVIDER", "EMAIL_FROM",
                  "EMAIL_FROM_NAME", "BREVO_API_KEY", "BREVO_API_URL", "SIGNUP_CODE", "INCUB_PUBLIC_URL", "INCUB_BACKUP_DIR", "INCUB_DB_PATH",
                  "INCUB_STORAGE_DIR", "INCUB_INDEX_DIR", "INCUB_KB_DIR", "INCUB_POLICY_FILE", "INCUB_ADMIN_PASSWORD", "OLLAMA_CHAT_MODEL",
                  "OLLAMA_EMBED_MODEL", "PERSIST_DIR", "SPACE_HOST", "SPACE_ID",
                  "LLM_API_KEY", "LLM_API_BASE", "LLM_MODEL", "HF_TOKEN", "HF_DATA_REPO", "ADMIN_PASSWORD", "PUBLIC_URL", "RUN_WORKER_IN_APP"]


@pytest.fixture(autouse=True)
def _isolated_environment(monkeypatch):
    """Remove settings that may be set on the computer (or loaded from .env earlier) so every test starts clean."""
    monkeypatch.setenv("INCUB_NO_DOTENV", "1")
    for k in _REAL_ENV_KEYS:
        monkeypatch.delenv(k, raising=False)
BEFORE = datetime(2026, 11, 1, tzinfo=timezone.utc)


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://127.0.0.1:9")  # closed port: Ollama unavailable
    s = dataclasses.replace(get_settings(), db_path=tmp_path / "t.db", storage_dir=tmp_path, index_dir=tmp_path / "index",
                            kb_dir=ROOT / "kb", ollama_base_url="http://127.0.0.1:9", ollama_retries=0,
                            # email explicitly OFF: individual tests switch on a local test mail server when they need one
                            email_provider="smtp", smtp_host=None, smtp_user=None, smtp_password=None, smtp_from=None,
                            brevo_api_key=None, signup_code=None, backup_dir=None)
    p = load_policy(ROOT / "config" / "policy.json")
    conn, s, p = open_app(s, p)
    sv.bootstrap_admin(conn, "admin", "adminpass1")
    admin = sv.authenticate(conn, "admin", "adminpass1")
    yield {"conn": conn, "s": s, "p": p, "admin": admin, "tmp": tmp_path}
    conn.close()


def make_student(conn, name):
    uid = sv.signup_student(conn, f"user_{name}", "password123")
    return User(uid, f"user_{name}", "student")


def make_team(env, name, ps=("AGRI", "AG-01"), n=2):
    conn, p = env["conn"], env["p"]
    u = make_student(conn, name)
    members = [{"full_name": f"{name} m{i}", "member_ref": f"{name}-R{i}"} for i in range(n)]
    sv.register_team(conn, p, u, f"Team {name}", f"{name}@example.org", members)
    if ps:
        sv.select_problem_statement(conn, u, *ps)
    return sv.refresh_user(conn, u)


def good_docx(seed=1):
    return sy.make_docx(sy.abstract(seed, 50, KW))


def bad_docx(seed=1):
    return sy.make_docx(sy.abstract(seed, 60, KW, omit=("Methodology",)))


def do_submit(env, user, data, name="a.docx", now=BEFORE, **kw):
    return sv.submit(env["conn"], env["p"], env["s"].storage_dir, user, name, data, now=now, **kw)


def near_copy_docx(seed=1):
    """Same abstract as good_docx(seed) with one section rewritten: >50% overlap but not an exact duplicate."""
    secs = sy.abstract(seed, 50, KW)
    secs[-1] = (secs[-1][0], sy.body(seed + 999, 50, KW))
    return sy.make_docx(secs)
