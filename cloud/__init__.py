"""Zero-install hosting: Streamlit Community Cloud runs the portal (from GitHub); a private Hugging Face
dataset repository keeps the data safe across restarts. Nothing here runs on a normal PC install.

Needed settings (Streamlit "Secrets"): HF_TOKEN (write token) and HF_DATA_REPO (e.g. yourname/incubation-data).

  boot(st)      once per server process: secrets -> environment, download the saved data, restore it
  ready(...)    after the database is open: first admin from ADMIN_PASSWORD, search index if missing
  background()  daemon thread: copy changes to Hugging Face every minute, emails/reminders every 5 minutes
"""
from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MIRROR = ROOT / "storage_mirror"
_state = {"last_push": None, "last_error": None, "pushes": 0, "enabled": False, "repo": None}


def _log(msg: str) -> None:
    print(f"[cloud] {msg}", file=sys.stderr, flush=True)


def secrets_to_env(st) -> None:
    """Streamlit Cloud 'Secrets' -> environment variables the portal already understands."""
    try:
        items = dict(st.secrets)
    except Exception:  # no secrets file on a PC install
        items = {}
    for k, v in items.items():
        if isinstance(v, (str, int, float, bool)):
            os.environ.setdefault(k, str(v))
    if os.environ.get("ADMIN_PASSWORD") and not os.environ.get("INCUB_ADMIN_PASSWORD"):
        os.environ["INCUB_ADMIN_PASSWORD"] = os.environ["ADMIN_PASSWORD"]
    if os.environ.get("PUBLIC_URL") and not os.environ.get("INCUB_PUBLIC_URL"):
        os.environ["INCUB_PUBLIC_URL"] = os.environ["PUBLIC_URL"]


def enabled() -> bool:
    return bool(os.environ.get("HF_TOKEN") and os.environ.get("HF_DATA_REPO"))


def _api():
    from huggingface_hub import HfApi
    return HfApi(token=os.environ["HF_TOKEN"])


def pull() -> dict:
    """Download the saved data into storage_mirror and restore it into the live folders."""
    from huggingface_hub import snapshot_download

    from persist import snapshot
    repo = os.environ["HF_DATA_REPO"]
    _api().create_repo(repo, repo_type="dataset", private=True, exist_ok=True)
    MIRROR.mkdir(parents=True, exist_ok=True)
    snapshot_download(repo, repo_type="dataset", local_dir=str(MIRROR), token=os.environ["HF_TOKEN"])
    return snapshot.restore()


def push(force: bool = False) -> dict:
    """Snapshot the live data into storage_mirror; upload to Hugging Face only when something changed."""
    from persist import snapshot
    r = snapshot.backup()
    if force or r.get("db_copied") or r.get("files_copied"):
        _api().upload_folder(repo_id=os.environ["HF_DATA_REPO"], repo_type="dataset", folder_path=str(MIRROR),
                             commit_message=f"portal data {r.get('at')}", ignore_patterns=["*.part", ".cache/**"])
        _state["last_push"], _state["pushes"] = r.get("at"), _state["pushes"] + 1
        r["pushed"] = True
    return r


def boot(st) -> None:
    secrets_to_env(st)
    if not enabled():
        return
    os.environ["INCUB_BACKUP_DIR"] = str(MIRROR)
    _state.update(enabled=True, repo=os.environ["HF_DATA_REPO"])
    try:
        _log(f"restored from Hugging Face: {pull()}")
    except Exception as exc:  # keep the portal up; the error is shown to the admin
        _state["last_error"] = f"restore failed: {type(exc).__name__}: {exc}"
        _log(_state["last_error"])


def ready(conn, settings, policy, client) -> None:
    from rag.index import HybridIndex
    from workflow.services import bootstrap_admin
    if not conn.execute("SELECT 1 FROM users WHERE role='admin'").fetchone():
        pw = os.environ.get("INCUB_ADMIN_PASSWORD", "")
        if len(pw) >= 8:
            bootstrap_admin(conn, os.environ.get("INCUB_ADMIN_USER", "admin"), pw)
            _log("first admin account created")
    if HybridIndex.needs_rebuild(settings.kb_dir, settings.index_dir):
        emb = client if client.status().get("embed_ok") else None
        HybridIndex.build(settings.kb_dir, settings.index_dir, emb, policy["rag"]["chunk_words"], policy["rag"]["chunk_overlap"])


def _loop(sync_every: int, notify_every: int) -> None:
    from notifications.worker import one_pass
    from workflow.context import open_app
    conn, settings, base = open_app()  # own SQLite connection for this thread
    last_notify = 0.0
    while True:
        time.sleep(sync_every)
        if time.time() - last_notify >= notify_every:
            last_notify = time.time()
            try:
                one_pass(conn, settings, base)
            except Exception as exc:
                _log(f"email pass failed: {type(exc).__name__}: {exc}")
        if _state["enabled"]:
            try:
                push()
                _state["last_error"] = None
            except Exception as exc:
                _state["last_error"] = f"upload failed: {type(exc).__name__}: {exc}"
                _log(_state["last_error"])


def background(sync_every: int = 60, notify_every: int = 300) -> threading.Thread:
    t = threading.Thread(target=_loop, args=(sync_every, notify_every), daemon=True, name="cloud-worker")
    t.start()
    return t


def status() -> dict:
    return dict(_state)
