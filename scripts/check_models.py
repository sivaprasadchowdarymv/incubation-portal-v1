"""Check local Ollama availability and required models."""
import json

import _path  # noqa: F401
from config.settings import get_settings
from rag.ollama import make_client

s = get_settings()
st = make_client(s).status()
print(json.dumps({"base_url": s.ollama_base_url, "chat_model": s.chat_model, "embed_model": s.embed_model, **st}, indent=2))
raise SystemExit(0 if st["reachable"] and st["chat_ok"] and st["embed_ok"] else 1)
