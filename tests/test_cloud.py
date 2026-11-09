"""Zero-install cloud mode (Streamlit Cloud + Hugging Face dataset), cloud AI client, and the free draft check."""
import dataclasses
import shutil
import types

import pytest

import cloud
from conftest import BEFORE, bad_docx, do_submit, good_docx, make_team
from persist import snapshot
from rag import ollama
from workflow import services as sv

# ---------------- draft check ----------------


def test_draft_check_uses_no_attempt_and_stores_nothing(env):
    t = make_team(env, "a")
    r = sv.check_draft(env["conn"], env["p"], t, "draft.docx", good_docx(1), now=BEFORE)
    assert r["verdict"] == "pass" and r["word_count"] > 100 and r["checks_left"] == sv.DRAFT_CHECKS_PER_DAY - 1
    assert env["conn"].execute("SELECT COUNT(*) FROM submissions").fetchone()[0] == 0
    assert env["conn"].execute("SELECT COUNT(*) FROM notifications").fetchone()[0] == env["conn"].execute(
        "SELECT COUNT(*) FROM notifications WHERE event_type != 'upload_refused'").fetchone()[0]
    bad = sv.check_draft(env["conn"], env["p"], t, "draft.docx", bad_docx(1), now=BEFORE)
    assert bad["verdict"] == "fix" and any(x["result"] == "fail" for x in bad["results"])
    assert do_submit(env, t, good_docx(1))["status"] == "ACCEPTED_FOR_EVALUATION"   # first real attempt still available


def test_draft_check_limits_and_rules(env):
    t = make_team(env, "a")
    with pytest.raises(sv.AuthError):
        sv.check_draft(env["conn"], env["p"], env["admin"], "d.docx", good_docx(1))
    sv.DRAFT_CHECKS_PER_DAY, old = 2, sv.DRAFT_CHECKS_PER_DAY
    try:
        for _ in range(2):
            sv.check_draft(env["conn"], env["p"], t, "d.docx", good_docx(1))
        with pytest.raises(sv.WorkflowError, match="draft checks"):
            sv.check_draft(env["conn"], env["p"], t, "d.docx", good_docx(1))
    finally:
        sv.DRAFT_CHECKS_PER_DAY = old
    assert sv.check_draft(env["conn"], env["p"], t, "notes.txt", b"hello")["verdict"] == "fix"


# ---------------- cloud AI ----------------


def test_make_client_and_api_chat(env, monkeypatch):
    assert isinstance(ollama.make_client(env["s"]), ollama.OllamaClient)
    s = dataclasses.replace(env["s"], llm_api_key="k-123", llm_model="small-model")
    c = ollama.make_client(s)
    assert isinstance(c, ollama.ApiChatClient) and c.status()["chat_ok"] and not c.status()["embed_ok"]
    seen = {}

    def fake_post(url, timeout, headers, json):
        seen.update(url=url, auth=headers["Authorization"], model=json["model"])
        return types.SimpleNamespace(raise_for_status=lambda: None, json=lambda: {"choices": [{"message": {"content": "Answer [1]"}}]})
    monkeypatch.setattr(ollama.requests, "post", fake_post)
    assert c.chat("sys", "q") == "Answer [1]"
    assert seen == {"url": "https://api.groq.com/openai/v1/chat/completions", "auth": "Bearer k-123", "model": "small-model"}
    with pytest.raises(ollama.OllamaUnavailable):
        c.embed(["x"])
    monkeypatch.setattr(ollama.requests, "post", lambda *a, **k: (_ for _ in ()).throw(ollama.requests.ConnectionError()))
    with pytest.raises(ollama.OllamaUnavailable, match="LLM_API_KEY"):
        c.chat("s", "q")


# ---------------- Streamlit secrets + Hugging Face data sync ----------------


def test_secrets_to_env(monkeypatch):
    fake = types.SimpleNamespace(secrets={"ADMIN_PASSWORD": "Str0ngPass!", "HF_DATA_REPO": "me/data", "nested": {"a": 1}, "SMTP_PORT": 587})
    cloud.secrets_to_env(fake)
    import os
    assert os.environ["INCUB_ADMIN_PASSWORD"] == "Str0ngPass!" and os.environ["SMTP_PORT"] == "587" and "nested" not in os.environ
    assert not cloud.enabled()
    monkeypatch.setenv("HF_TOKEN", "hf_x")
    assert cloud.enabled()

    class NoSecrets:
        @property
        def secrets(self):
            raise FileNotFoundError
    cloud.secrets_to_env(NoSecrets())  # PC install without secrets: no error


@pytest.fixture
def hub(env, tmp_path_factory, monkeypatch):
    """Fake Hugging Face Hub: a folder that upload_folder copies into and snapshot_download copies out of."""
    tmp_path = tmp_path_factory.mktemp("hub")  # outside the app's storage folder, which the test wipes
    remote = tmp_path / "remote"
    remote.mkdir()
    mirror = tmp_path / "mirror"
    s = dataclasses.replace(env["s"], backup_dir=mirror, kb_dir=tmp_path / "kb")
    (tmp_path / "kb").mkdir()
    (tmp_path / "kb" / "manifest.json").write_text('{"documents": []}')
    monkeypatch.setattr(snapshot, "get_settings", lambda: s)
    monkeypatch.setattr(cloud, "MIRROR", mirror)
    monkeypatch.setenv("HF_TOKEN", "hf_x")
    monkeypatch.setenv("HF_DATA_REPO", "me/incubation-data")
    calls = {"upload": 0, "create": 0}

    class Api:
        def __init__(self, token):
            assert token == "hf_x"

        def create_repo(self, repo, repo_type, private, exist_ok):
            assert repo_type == "dataset" and private and exist_ok
            calls["create"] += 1

        def upload_folder(self, repo_id, repo_type, folder_path, commit_message, ignore_patterns):
            calls["upload"] += 1
            shutil.copytree(folder_path, remote, dirs_exist_ok=True)

    def download(repo, repo_type, local_dir, token):
        shutil.copytree(remote, local_dir, dirs_exist_ok=True)
    import huggingface_hub
    monkeypatch.setattr(huggingface_hub, "HfApi", Api)
    monkeypatch.setattr(huggingface_hub, "snapshot_download", download)
    return env | {"s2": s, "remote": remote, "calls": calls}


def test_push_only_when_changed_and_restore_after_restart(hub):
    env, s = hub, hub["s2"]
    t = make_team(env, "a")
    do_submit(env, t, good_docx(1))
    r = cloud.push()
    assert r["pushed"] and hub["calls"]["upload"] == 1 and (hub["remote"] / "db" / "incubation.db").exists()
    assert not cloud.push().get("pushed") and hub["calls"]["upload"] == 1          # nothing changed -> no upload
    # restart: local disk and mirror wiped, data comes back from the Hub
    env["conn"].close()
    shutil.rmtree(s.storage_dir)
    shutil.rmtree(cloud.MIRROR)
    out = cloud.pull()
    assert out["db_restored_from"] == "incubation.db" and hub["calls"]["create"] == 1
    from workflow.context import open_app
    conn, _, _ = open_app(s, env["p"])
    assert conn.execute("SELECT COUNT(*) FROM submissions").fetchone()[0] == 1
    assert list((s.storage_dir / "uploads").rglob("*.docx"))


def test_ready_creates_first_admin_and_index(tmp_path, monkeypatch):
    from config.settings import get_settings
    from workflow.context import open_app
    for k, v in {"INCUB_DB_PATH": tmp_path / "db.sqlite", "INCUB_STORAGE_DIR": tmp_path, "INCUB_INDEX_DIR": tmp_path / "idx"}.items():
        monkeypatch.setenv(k, str(v))
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("OLLAMA_RETRIES", "0")
    conn, s, p = open_app(get_settings())
    c = ollama.make_client(s)
    monkeypatch.setenv("INCUB_ADMIN_PASSWORD", "short")
    cloud.ready(conn, s, p, c)
    assert conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0           # weak password: no admin
    monkeypatch.setenv("INCUB_ADMIN_PASSWORD", "CloudAdmin1")
    cloud.ready(conn, s, p, c)
    cloud.ready(conn, s, p, c)
    assert conn.execute("SELECT COUNT(*) FROM users WHERE role='admin'").fetchone()[0] == 1
    assert sv.authenticate(conn, "admin", "CloudAdmin1").role == "admin"
    assert (tmp_path / "idx" / "meta.json").exists()
