"""Non-interactive first-start preparation (used by the Docker entrypoint; safe to run on every start).

--role app    : migrate DB, load catalogue, create the first admin from INCUB_ADMIN_PASSWORD, wait for Ollama, build index if missing/stale
--role worker : migrate DB only (the app container does the rest)
"""
import argparse
import os
import sys
import time

import _path  # noqa: F401
from rag.index import HybridIndex
from rag.ollama import make_client
from workflow.context import open_app
from workflow.services import bootstrap_admin


def log(msg: str) -> None:
    print(f"[bootstrap] {msg}", flush=True)


ap = argparse.ArgumentParser()
ap.add_argument("--role", choices=["app", "worker"], default="app")
role = ap.parse_args().role

conn, settings, policy = open_app()
log(f"database ready at {settings.db_path} (schema v{conn.execute('SELECT MAX(version) FROM schema_migrations').fetchone()[0]})")
if role == "worker":
    sys.exit(0)

if not conn.execute("SELECT 1 FROM users WHERE role='admin'").fetchone():
    pw = os.environ.get("INCUB_ADMIN_PASSWORD", "")
    if len(pw) < 8:
        log("ERROR: no admin exists yet. Set INCUB_ADMIN_PASSWORD (at least 8 characters) in .env and restart.")
        sys.exit(1)
    bootstrap_admin(conn, os.environ.get("INCUB_ADMIN_USER", "admin"), pw)
    log(f"admin account '{os.environ.get('INCUB_ADMIN_USER', 'admin')}' created. You can now remove INCUB_ADMIN_PASSWORD from .env.")

client = make_client(settings)
wait = int(os.environ.get("OLLAMA_WAIT_SECONDS", "120"))
deadline = time.time() + wait
status = client.status()
while status.get("provider") != "api" and not status["embed_ok"] and time.time() < deadline:
    log(f"waiting for Ollama models at {settings.ollama_base_url} ...")
    time.sleep(5)
    status = client.status()
log(f"Ollama reachable={status['reachable']} chat={status['chat_ok']} embed={status['embed_ok']}")

if HybridIndex.needs_rebuild(settings.kb_dir, settings.index_dir):
    idx = HybridIndex.build(settings.kb_dir, settings.index_dir, client if status["embed_ok"] else None,
                            policy["rag"]["chunk_words"], policy["rag"]["chunk_overlap"])
    log(f"knowledge-base index built: {idx.meta['chunks']} chunks, semantic={idx.meta['semantic']}"
        + ("" if idx.meta["semantic"] else " (keyword search only; rebuild later from the Knowledge Base page)"))
else:
    log("knowledge-base index up to date")
