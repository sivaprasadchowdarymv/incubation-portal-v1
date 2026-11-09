"""Knowledge-base ingestion, metadata-aware chunking, FAISS + BM25 hybrid retrieval with persistence."""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from rank_bm25 import BM25Okapi

from validation.checks import STOPWORDS
from validation.extract import extract

AUDIENCE_FOR_ROLE = {"student": {"student", "all"}, "reviewer": {"student", "reviewer", "all"}, "admin": {"student", "reviewer", "admin", "all"}}
WORD = re.compile(r"[a-z0-9]+")
QUERY_FILLER = {"many", "much", "get", "got", "happens", "happen", "tell", "know", "please", "ok"}


def tok(text: str) -> list[str]:
    return [w for w in WORD.findall(text.lower()) if w not in STOPWORDS and w not in QUERY_FILLER]


def manifest_sha(kb_dir: Path) -> str:
    h = hashlib.sha256()
    man = Path(kb_dir) / "manifest.json"
    h.update(man.read_bytes())
    for e in json.loads(man.read_text())["documents"]:
        p = Path(kb_dir) / e["file"]
        h.update(p.read_bytes() if p.exists() else b"missing")
    return h.hexdigest()


def load_chunks(kb_dir: Path, chunk_words: int = 200, overlap: int = 40) -> tuple[list[dict], list[str]]:
    """Only documents with status 'approved' are indexed. Returns (chunks, skipped notes)."""
    kb_dir = Path(kb_dir)
    man = json.loads((kb_dir / "manifest.json").read_text())
    chunks, skipped = [], []
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for e in man["documents"]:
        if e.get("status") != "approved":
            skipped.append(f"{e['file']}: status={e.get('status')} (not indexed)")
            continue
        p = kb_dir / e["file"]
        if not p.exists() or kb_dir.resolve() not in p.resolve().parents:
            skipped.append(f"{e['file']}: missing or outside KB")
            continue
        ex = extract(p.name, p.read_bytes())
        if ex.error:
            skipped.append(f"{e['file']}: {ex.error}")
            continue
        for u in ex.units:
            words = u["text"].split()
            step = max(chunk_words - overlap, 1)
            for start in range(0, max(len(words), 1), step):
                piece = " ".join(words[start : start + chunk_words])
                if not piece.strip():
                    continue
                chunks.append({"chunk_id": f"{e['file']}#{len(chunks)}", "text": piece, "source": e["file"], "version": e.get("version", "?"),
                               "doc_type": e.get("doc_type", "other"), "audience": e.get("audience", "student"),
                               "section": u.get("section"), "page": u.get("page"), "ingested_at": ts})
                if start + chunk_words >= len(words):
                    break
    return chunks, skipped


class HybridIndex:
    def __init__(self, chunks: list[dict], meta: dict, faiss_index=None):
        self.chunks, self.meta, self.faiss = chunks, meta, faiss_index
        self.bm25 = BM25Okapi([tok(c["text"]) or ["_"] for c in chunks]) if chunks else None

    # ---- build / persist ----
    @classmethod
    def build(cls, kb_dir: Path, index_dir: Path, embedder=None, chunk_words: int = 200, overlap: int = 40) -> "HybridIndex":
        chunks, skipped = load_chunks(kb_dir, chunk_words, overlap)
        meta = {"manifest_sha": manifest_sha(kb_dir), "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "chunks": len(chunks), "skipped": skipped, "semantic": False, "embed_model": None, "semantic_error": None}
        fidx = None
        if embedder is not None and chunks:
            try:
                import faiss

                vecs = np.vstack([embedder.embed([c["text"] for c in chunks[i : i + 32]]) for i in range(0, len(chunks), 32)])
                fidx = faiss.IndexFlatIP(vecs.shape[1])
                fidx.add(vecs)
                meta.update(semantic=True, embed_model=getattr(embedder, "embed_model", "custom"))
            except Exception as exc:
                meta["semantic_error"] = f"{type(exc).__name__}: {exc}"
        index_dir = Path(index_dir)
        index_dir.mkdir(parents=True, exist_ok=True)
        (index_dir / "chunks.json").write_text(json.dumps(chunks))
        if fidx is not None:
            import faiss

            faiss.write_index(fidx, str(index_dir / "faiss.index"))
        elif (index_dir / "faiss.index").exists():
            (index_dir / "faiss.index").unlink()
        (index_dir / "meta.json").write_text(json.dumps(meta, indent=2))
        return cls(chunks, meta, fidx)

    @classmethod
    def load(cls, index_dir: Path) -> "HybridIndex":
        index_dir = Path(index_dir)
        chunks = json.loads((index_dir / "chunks.json").read_text())
        meta = json.loads((index_dir / "meta.json").read_text())
        fidx = None
        if meta.get("semantic") and (index_dir / "faiss.index").exists():
            import faiss

            fidx = faiss.read_index(str(index_dir / "faiss.index"))
        return cls(chunks, meta, fidx)

    @staticmethod
    def needs_rebuild(kb_dir: Path, index_dir: Path) -> bool:
        m = Path(index_dir) / "meta.json"
        return not m.exists() or json.loads(m.read_text()).get("manifest_sha") != manifest_sha(kb_dir)

    # ---- search ----
    @staticmethod
    def coverage(query_terms: list[str], text: str) -> float:
        """Share of the question's meaningful words found in a passage (plural/singular tolerant)."""
        terms = set(query_terms)
        if not terms:
            return 0.0
        words = set(tok(text))
        hit = sum(1 for t in terms if t in words or t.rstrip("s") in words or t + "s" in words)
        return hit / len(terms)

    def search(self, query: str, role: str, k: int = 5, embedder=None, bm25_min: float = 0.5, cos_min: float = 0.35, rrf_k: int = 60,
               min_coverage: float = 0.5) -> dict:
        allowed = AUDIENCE_FOR_ROLE.get(role, {"student", "all"})
        ok_ids = [i for i, c in enumerate(self.chunks) if c["audience"] in allowed]
        lex, sem, notes = {}, {}, []
        q = tok(query)
        if self.bm25 is not None and q:
            scores = self.bm25.get_scores(q)
            # a keyword hit must contain enough of the question's words: one generic word (e.g. "team") is not an answer
            ranked = sorted((i for i in ok_ids if scores[i] >= bm25_min and self.coverage(q, self.chunks[i]["text"]) >= min_coverage),
                            key=lambda i: -scores[i])[: k * 4]
            lex = {i: float(scores[i]) for i in ranked}
        if self.faiss is not None and embedder is not None:
            try:
                qv = embedder.embed([query])
                D, ids = self.faiss.search(qv, min(len(self.chunks), k * 8))
                for d, i in zip(D[0], ids[0]):
                    if i >= 0 and i in ok_ids and d >= cos_min:
                        sem[int(i)] = float(d)
            except Exception as exc:
                notes.append(f"semantic retrieval unavailable ({type(exc).__name__}); used BM25 only")
        elif self.faiss is None:
            notes.append("semantic index not built; BM25 only")
        else:
            notes.append("embedding model unavailable; BM25 only")
        fused: dict[int, float] = {}
        for ranking in (sorted(lex, key=lambda i: -lex[i]), sorted(sem, key=lambda i: -sem[i])):
            for r, i in enumerate(ranking):
                fused[i] = fused.get(i, 0) + 1 / (rrf_k + r + 1)
        hits, seen = [], set()
        for i in sorted(fused, key=lambda i: -fused[i]):
            h = hashlib.sha1(self.chunks[i]["text"].encode()).hexdigest()
            if h in seen:
                continue
            seen.add(h)
            hits.append(self.chunks[i] | {"bm25": lex.get(i), "cosine": sem.get(i), "rrf": round(fused[i], 5),
                                          "retrievers": [n for n, d in (("bm25", lex), ("semantic", sem)) if i in d]})
            if len(hits) >= k:
                break
        mode = "hybrid" if sem else "bm25"
        return {"hits": hits, "mode": mode, "notes": notes}
