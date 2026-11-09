"""Run RAG retrieval evaluation and labeled hard-check evaluation; persists metrics."""
import json

import _path  # noqa: F401
from config.settings import ROOT
from evaluation.evaluate import rag_eval, workflow_eval
from rag.index import HybridIndex
from rag.ollama import OllamaClient
from workflow.context import open_app

conn, s, p = open_app()
if HybridIndex.needs_rebuild(s.kb_dir, s.index_dir):
    raise SystemExit("Index missing or stale: run python scripts/ingest.py first")
idx = HybridIndex.load(s.index_dir)
c = OllamaClient.from_settings(s)
st = c.status()
client = c if st["chat_ok"] else None
r = rag_eval(idx, ROOT / "evaluation" / "rag_eval.json", client, p["rag"]["top_k"], p["rag"], conn)
w = workflow_eval(p, conn)
print(json.dumps({"ollama_chat_available": bool(client), "rag": {k: v for k, v in r.items() if k != "rows"}, "hard_checks": {k: v for k, v in w.items() if k != "rows"}}, indent=2))
