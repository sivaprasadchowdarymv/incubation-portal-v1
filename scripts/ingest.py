"""Build (or rebuild) the knowledge-base index. Usage: python scripts/ingest.py [--force]"""
import argparse
import json

import _path  # noqa: F401

from config.settings import get_settings, load_policy
from rag.index import HybridIndex
from rag.ollama import make_client

ap = argparse.ArgumentParser()
ap.add_argument("--force", action="store_true")
a = ap.parse_args()
s, p = get_settings(), load_policy()
if not a.force and not HybridIndex.needs_rebuild(s.kb_dir, s.index_dir):
    print("Index up to date (use --force to rebuild).")
    raise SystemExit(0)
client = make_client(s)
st = client.status()
from rag.local_models import pick_embedder  # noqa: E402  (local model first, then Ollama)

emb = pick_embedder(client)
if emb is None:
    print("WARNING: no embedding model available -> building keyword-only index. Fix:", "; ".join(st["instructions"]))
idx = HybridIndex.build(s.kb_dir, s.index_dir, emb, p["rag"]["chunk_words"], p["rag"]["chunk_overlap"])
print(json.dumps(idx.meta, indent=2))
