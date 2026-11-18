"""Small CPU models that run inside the portal (no API key, no Ollama):

* embeddings  — BAAI/bge-small-en-v1.5 (≈130 MB): meaning-based search, so "how big can our group be?" finds "two to three members"
* re-ranking  — Xenova/ms-marco-MiniLM-L-6-v2 (≈80 MB): re-orders the best candidates so the right passage comes first

Both are optional. They are downloaded once (the Hugging Face Dockerfile pre-downloads them at build time). If they cannot be
loaded — no internet, too little memory, INCUB_LOCAL_MODELS=0 — the portal keeps working with keyword search and says so."""
from __future__ import annotations

import os
import threading
import time

import numpy as np

EMBED_MODEL = os.environ.get("INCUB_EMBED_MODEL", "BAAI/bge-small-en-v1.5")
RERANK_MODEL = os.environ.get("INCUB_RERANK_MODEL", "Xenova/ms-marco-MiniLM-L-6-v2")
CACHE_DIR = os.environ.get("INCUB_MODEL_CACHE") or None

_lock = threading.Lock()
_state: dict = {"embedder": None, "reranker": None, "embed_error": None, "rerank_error": None, "embed_ms": None, "rerank_ms": None}


def enabled() -> bool:
    return os.environ.get("INCUB_LOCAL_MODELS", "1") not in ("0", "false", "no")


class LocalEmbedder:
    """Same interface the index already uses for Ollama: .embed(list[str]) -> normalized float32 matrix."""

    def __init__(self, model):
        self._m = model
        self.embed_model = EMBED_MODEL

    def embed(self, texts: list[str]) -> np.ndarray:
        v = np.asarray(list(self._m.embed(list(texts))), dtype="float32")
        v /= np.linalg.norm(v, axis=1, keepdims=True) + 1e-12
        return v


class LocalReranker:
    def __init__(self, model):
        self._m = model
        self.model = RERANK_MODEL

    def scores(self, query: str, texts: list[str]) -> list[float]:
        return [float(s) for s in self._m.rerank(query, list(texts))]


def get_embedder() -> LocalEmbedder | None:
    if not enabled():
        return None
    with _lock:
        if _state["embedder"] is None and _state["embed_error"] is None:
            t0 = time.perf_counter()
            try:
                from fastembed import TextEmbedding
                _state["embedder"] = LocalEmbedder(TextEmbedding(EMBED_MODEL, cache_dir=CACHE_DIR))
                _state["embed_ms"] = round((time.perf_counter() - t0) * 1000)
            except Exception as exc:  # no internet / memory / package: fall back to keyword search
                _state["embed_error"] = f"{type(exc).__name__}: {str(exc)[:200]}"
        return _state["embedder"]


def get_reranker() -> LocalReranker | None:
    if not enabled():
        return None
    with _lock:
        if _state["reranker"] is None and _state["rerank_error"] is None:
            t0 = time.perf_counter()
            try:
                from fastembed.rerank.cross_encoder import TextCrossEncoder
                _state["reranker"] = LocalReranker(TextCrossEncoder(RERANK_MODEL, cache_dir=CACHE_DIR))
                _state["rerank_ms"] = round((time.perf_counter() - t0) * 1000)
            except Exception as exc:
                _state["rerank_error"] = f"{type(exc).__name__}: {str(exc)[:200]}"
        return _state["reranker"]


def status() -> dict:
    return {"enabled": enabled(), "embed_model": EMBED_MODEL, "embed_loaded": _state["embedder"] is not None, "embed_error": _state["embed_error"],
            "rerank_model": RERANK_MODEL, "rerank_loaded": _state["reranker"] is not None, "rerank_error": _state["rerank_error"]}


def reset() -> None:
    """Forget a failed load so the admin can retry (e.g. after the network came back)."""
    with _lock:
        _state.update(embedder=None, reranker=None, embed_error=None, rerank_error=None)


def pick_embedder(client=None):
    """Embedder for the knowledge-base index: local model first, else the AI client if it can embed (Ollama), else none."""
    e = get_embedder()
    if e is not None:
        return e
    try:
        if client is not None and client.status().get("embed_ok"):
            return client
    except Exception:
        pass
    return None


def warm_and_upgrade(settings, policy: dict) -> dict:
    """Background start-up step: load both models, then switch a keyword-only index to meaning-based search.
    The portal is usable the whole time; the shared index reloads automatically when its files change."""
    import json

    from rag.index import HybridIndex
    emb, rr = get_embedder(), get_reranker()
    out = {"embedder": emb is not None, "reranker": rr is not None, "upgraded": False}
    meta_p = settings.index_dir / "meta.json"
    if emb is not None and meta_p.exists() and not json.loads(meta_p.read_text()).get("semantic"):
        HybridIndex.build(settings.kb_dir, settings.index_dir, emb, policy["rag"]["chunk_words"], policy["rag"]["chunk_overlap"])
        from rag.answer import clear_cache
        clear_cache()
        out["upgraded"] = True
    return out
