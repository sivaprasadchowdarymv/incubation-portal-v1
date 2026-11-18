"""Knowledge-base ingestion, section-aware chunking, BM25 (stemmed, synonym-expanded) + semantic hybrid retrieval,
optional cross-encoder re-ranking, with persistence."""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from rank_bm25 import BM25Okapi

try:  # light English stemming: "attempts" = "attempt", "members" = "member"
    import snowballstemmer
    _STEM = snowballstemmer.stemmer("english").stemWord
except ImportError:  # pragma: no cover - fallback keeps the portal working
    def _STEM(w: str) -> str:
        return w[:-1] if len(w) > 3 and w.endswith("s") and not w.endswith("ss") else w

from validation.checks import STOPWORDS
from validation.extract import extract

AUDIENCE_FOR_ROLE = {"student": {"student", "all"}, "reviewer": {"student", "reviewer", "all"}, "admin": {"student", "reviewer", "admin", "all"}}
WORD = re.compile(r"[a-z0-9]+")
QUERY_FILLER = {"many", "much", "get", "got", "happens", "happen", "tell", "know", "please", "ok"}


INDEX_VERSION = 3   # bump when chunk text or index format changes: old indexes are rebuilt automatically


def tok(text: str) -> list[str]:
    """Meaningful words, stemmed."""
    return [_STEM(w) for w in WORD.findall(text.lower()) if w not in STOPWORDS and w not in QUERY_FILLER]


def parse_synonyms(lines) -> dict[str, set[str]]:
    """'group = team, squad' -> every word maps to the others (stemmed, both directions)."""
    groups: dict[str, set[str]] = {}
    for line in lines or []:
        line = line if isinstance(line, str) else " = ".join(line)
        words = [w for part in re.split(r"[=,;]", line) for w in [" ".join(tok(part))] if w]
        for w in words:
            groups.setdefault(w, set()).update(x for x in words if x != w)
    return groups


def expand(q: list[str], synonyms: dict[str, set[str]] | None) -> list[list[str]]:
    """Each question word (or two/three-word phrase such as 'last date') with its alternatives: [[last date, deadlin, due date], [submit, upload]]."""
    syn = synonyms or {}
    out, i = [], 0
    while i < len(q):
        for n in (3, 2, 1):
            phrase = " ".join(q[i : i + n])
            if n == 1 or phrase in syn:
                out.append(list(dict.fromkeys([phrase] + sorted(syn.get(phrase, ())))))
                i += n
                break
    return out


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
                body = piece
                if u.get("section"):  # keep the heading with every passage, so the AI and meaning-based search see its topic
                    piece = f"{u['section']}: {piece}"
                chunks.append({"chunk_id": f"{e['file']}#{len(chunks)}", "text": piece, "body": body, "source": e["file"], "version": e.get("version", "?"),
                               "doc_type": e.get("doc_type", "other"), "audience": e.get("audience", "student"),
                               "section": u.get("section"), "page": u.get("page"), "ingested_at": ts})
                if start + chunk_words >= len(words):
                    break
    return chunks, skipped


class HybridIndex:
    def __init__(self, chunks: list[dict], meta: dict, faiss_index=None):
        self.chunks, self.meta, self.faiss = chunks, meta, faiss_index
        # keyword scores use the passage body only: FAQ-style question headings must not outrank the real answer
        self.bm25 = BM25Okapi([tok(c.get("body", c["text"])) or ["_"] for c in chunks]) if chunks else None

    # ---- build / persist ----
    @classmethod
    def build(cls, kb_dir: Path, index_dir: Path, embedder=None, chunk_words: int = 200, overlap: int = 40) -> "HybridIndex":
        chunks, skipped = load_chunks(kb_dir, chunk_words, overlap)
        meta = {"manifest_sha": manifest_sha(kb_dir), "index_version": INDEX_VERSION, "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
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
        if not m.exists():
            return True
        meta = json.loads(m.read_text())
        return meta.get("manifest_sha") != manifest_sha(kb_dir) or meta.get("index_version") != INDEX_VERSION

    # ---- search ----
    @staticmethod
    def coverage(query_terms, text: str) -> float:
        """Share of the question's meaningful words (or one of their synonyms) found in a passage."""
        groups = [[" ".join(tok(a)) or a for a in (g if isinstance(g, list) else [g])] for g in query_terms]
        if not groups:
            return 0.0
        words = set(tok(text))
        hit = sum(1 for g in groups if any(all(x in words for x in alt.split()) for alt in g))
        return hit / len(groups)

    def search(self, query: str, role: str, k: int = 5, embedder=None, bm25_min: float = 0.5, cos_min: float = 0.35, rrf_k: int = 60,
               min_coverage: float = 0.5, synonyms: dict | None = None, reranker=None, rerank_pool: int = 20) -> dict:
        allowed = AUDIENCE_FOR_ROLE.get(role, {"student", "all"})
        ok_set = {i for i, c in enumerate(self.chunks) if c["audience"] in allowed}
        ok_ids = sorted(ok_set)
        lex, sem, notes = {}, {}, []
        q = tok(query)
        groups = expand(q, synonyms)
        if self.bm25 is not None and q:
            scores = self.bm25.get_scores([w for g in groups for alt in g for w in alt.split()])
            # a keyword hit must contain enough of the question's words: one generic word (e.g. "team") is not an answer
            ranked = sorted((i for i in ok_ids if scores[i] >= bm25_min and self.coverage(groups, self.chunks[i].get("body", self.chunks[i]["text"])) >= min_coverage),
                            key=lambda i: -scores[i])[: max(k * 4, rerank_pool)]
            lex = {i: float(scores[i]) for i in ranked}
        if self.faiss is not None and embedder is not None and getattr(embedder, "embed_model", None) not in (None, self.meta.get("embed_model")):
            notes.append(f"index built with {self.meta.get('embed_model')}, query model is {getattr(embedder, 'embed_model', '?')}: rebuild the index; BM25 only")
            embedder = None
        if self.faiss is not None and embedder is not None:
            try:
                qv = embedder.embed([query])
                D, ids = self.faiss.search(qv, min(len(self.chunks), max(k * 8, rerank_pool)))
                for d, i in zip(D[0], ids[0]):
                    if i >= 0 and int(i) in ok_set and d >= cos_min:
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
        order, seen = [], set()
        for i in sorted(fused, key=lambda i: -fused[i]):
            h = hashlib.sha1(self.chunks[i]["text"].encode()).hexdigest()
            if h not in seen:
                seen.add(h)
                order.append(i)
        rr: dict[int, float] = {}
        if reranker is not None and len(order) > 1:
            pool = order[:rerank_pool]
            try:
                sc = reranker.scores(query, [self.chunks[i]["text"] for i in pool])
                rr = dict(zip(pool, sc))
                order = sorted(pool, key=lambda i: -rr[i]) + order[rerank_pool:]
            except Exception as exc:
                notes.append(f"re-ranking unavailable ({type(exc).__name__})")
        hits = [self.chunks[i] | {"bm25": lex.get(i), "cosine": sem.get(i), "rrf": round(fused[i], 5), "rerank": rr.get(i),
                                  "retrievers": [n for n, d in (("bm25", lex), ("semantic", sem)) if i in d]} for i in order[:k]]
        mode = ("hybrid" if sem else "bm25") + ("+rerank" if rr else "")
        return {"hits": hits, "mode": mode, "notes": notes}
