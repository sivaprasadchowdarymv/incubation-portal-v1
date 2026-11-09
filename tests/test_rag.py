import hashlib

import numpy as np
import pytest

from config.settings import ROOT
from evaluation.evaluate import rag_eval
from rag.answer import ABSTAIN, answer, verify_citations
from rag.index import HybridIndex, tok
from rag.ollama import OllamaClient, OllamaUnavailable


class FakeEmbedder:
    """Deterministic local test double (hashed bag-of-words). Not used at runtime."""
    embed_model = "fake-hash-embed"

    def embed(self, texts):
        v = np.zeros((len(texts), 256), dtype="float32")
        for r, t in enumerate(texts):
            for w in tok(t):
                v[r, int(hashlib.md5(w.encode()).hexdigest(), 16) % 256] += 1
        return v / (np.linalg.norm(v, axis=1, keepdims=True) + 1e-12)


class FakeLLM(FakeEmbedder):
    def __init__(self, reply):
        self.reply = reply

    def chat(self, system, user):
        assert "untrusted" in system and "SOURCES" in user
        return self.reply


@pytest.fixture
def idx(tmp_path):
    return HybridIndex.build(ROOT / "kb", tmp_path / "ix")


def test_only_approved_indexed(idx):
    sources = {c["source"] for c in idx.chunks}
    assert "draft_clarification.md" not in sources and "incubation_guidelines.md" in sources
    assert any("draft_clarification.md" in s for s in idx.meta["skipped"])
    assert all(c["version"] and c["doc_type"] and c["ingested_at"] for c in idx.chunks)


def test_citations_from_retrieved_sources_without_llm(idx):
    r = answer("How many members can a team have?", idx, "student", None)
    assert r["status"] == "ai_unavailable" and r["hits"]
    assert r["hits"][0]["source"] == "incubation_guidelines.md"
    assert all(s["label"].split(" ")[0] in {c["source"] for c in idx.chunks} for s in r["sources"])
    assert "four members" not in " ".join(h["text"] for h in r["hits"])  # draft never surfaces


def test_unsupported_question_abstains(idx):
    r = answer("What is the prize money for the winning team?", idx, "student", None)
    assert r["status"] == "abstained" and r["answer"] == ABSTAIN and r["sources"] == []


def test_reviewer_docs_hidden_from_students(idx):
    q = "How should reviewers treat common phrases from the problem statement?"
    assert all(h["audience"] != "reviewer" for h in answer(q, idx, "student", None)["hits"])
    assert any(h["source"] == "reviewer_guidelines.md" for h in answer(q, idx, "reviewer", None)["hits"])


def test_citation_verification(idx):
    clean, cited, faith = verify_citations("Teams have two to three members [S1]. Deadline is unknown [S9].", 2)
    assert cited == [1] and "[S9]" not in clean and faith == 0.5
    r = answer("How many members can a team have?", idx, "student", FakeLLM("Teams have two or three members [S1]. Bogus [S7]."))
    assert r["status"] == "answered" and r["citations"] == [1] and "[S7]" not in r["answer"]
    r2 = answer("How many members can a team have?", idx, "student", FakeLLM("No citations at all here."))
    assert r2["status"] == "uncited"
    r3 = answer("How many members can a team have?", idx, "student", FakeLLM("INSUFFICIENT_EVIDENCE"))
    assert r3["status"] == "abstained"


def test_semantic_faiss_persistence_and_rebuild(tmp_path):
    kb = tmp_path / "kb"
    kb.mkdir()
    for f in (ROOT / "kb").iterdir():
        (kb / f.name).write_bytes(f.read_bytes())
    built = HybridIndex.build(kb, tmp_path / "ix", FakeEmbedder())
    assert built.meta["semantic"] and (tmp_path / "ix" / "faiss.index").exists()
    loaded = HybridIndex.load(tmp_path / "ix")
    res = loaded.search("submission attempts allowed", "student", 3, FakeEmbedder())
    assert res["mode"] == "hybrid" and "submission_policy.md" in [h["source"] for h in res["hits"]]
    assert any("semantic" in h["retrievers"] for h in res["hits"])
    assert not HybridIndex.needs_rebuild(kb, tmp_path / "ix")
    (kb / "faq.md").write_text((kb / "faq.md").read_text() + "\n# New\nNew approved answer.")
    assert HybridIndex.needs_rebuild(kb, tmp_path / "ix")


def test_rag_eval_metrics(idx):
    out = rag_eval(idx, ROOT / "evaluation" / "rag_eval.json", None, 5)
    assert out["n"] == 10 and 0 <= out["precision_at_k"] <= 1 and 0 <= out["recall_at_k"] <= 1
    assert out["unanswerable_abstention_rate"] is not None


def test_ollama_unreachable_status():
    c = OllamaClient("http://127.0.0.1:9", "mistral", "nomic-embed-text", timeout=1, retries=0)
    st = c.status()
    assert st["reachable"] is False and any("ollama pull" in i for i in st["instructions"])
    with pytest.raises(OllamaUnavailable):
        c.chat("s", "u")


def test_ollama_model_missing(monkeypatch):
    c = OllamaClient("http://127.0.0.1:9", "mistral", "nomic-embed-text", retries=0)
    monkeypatch.setattr(c, "list_models", lambda: ["llama3.2:latest", "nomic-embed-text:latest"])
    st = c.status()
    assert st["reachable"] and not st["chat_ok"] and st["embed_ok"] and st["instructions"] == ["ollama pull mistral"]


def test_answer_when_ollama_down_mid_request(idx):
    c = OllamaClient("http://127.0.0.1:9", "mistral", "nomic-embed-text", timeout=1, retries=0)
    r = answer("How many members can a team have?", idx, "student", c)
    assert r["status"] == "ai_unavailable" and r["sources"]
