"""RAG retrieval evaluation and labeled workflow (hard-check) evaluation. Results are measured, never assumed."""
from __future__ import annotations

import json
import statistics
import time
from pathlib import Path

from evaluation import metrics, synthetic
from rag.answer import answer
from validation import checks, extract


EVAL_CSV_COLUMNS = ["question", "role", "expected_sources", "expected_facts", "answerable"]


def load_items(path_or_items) -> list[dict]:
    """A JSON file ({"items": [...]}), a CSV file/bytes (columns EVAL_CSV_COLUMNS; lists separated by ';'), or a ready list."""
    if isinstance(path_or_items, list):
        return path_or_items
    if isinstance(path_or_items, (bytes, bytearray)):
        raw, name = path_or_items.decode("utf-8-sig"), "upload.csv"
    else:
        raw, name = Path(path_or_items).read_text(encoding="utf-8-sig"), str(path_or_items)
    if name.endswith(".json") or raw.lstrip().startswith("{"):
        return json.loads(raw)["items"]
    import csv
    import io
    items = []
    for i, r in enumerate(csv.DictReader(io.StringIO(raw)), 2):
        r = {k.strip().lower(): (v or "").strip() for k, v in r.items() if k}
        if not r.get("question"):
            continue
        split = lambda v: [x.strip() for x in v.split(";") if x.strip()]
        ans = r.get("answerable", "yes").lower() not in ("no", "false", "0", "n")
        srcs = split(r.get("expected_sources", ""))
        if ans and not srcs:
            raise ValueError(f"Row {i}: an answerable question needs expected_sources (document file names separated by ';')")
        items.append({"q": r["question"], "role": r.get("role") or "student", "expected_sources": srcs,
                      "expected_facts": split(r.get("expected_facts", "")), "answerable": ans})
    if not items:
        raise ValueError("No questions found. Columns: " + ", ".join(EVAL_CSV_COLUMNS))
    return items


def _fact_found(fact: str, context: str) -> bool:
    """A fact is present if its exact words, or all its meaningful (stemmed) words, appear in the retrieved context."""
    from rag.index import tok
    if " ".join(fact.lower().split()) in " ".join(context.lower().split()):
        return True
    need, have = set(tok(fact)), set(tok(context))
    return bool(need) and need <= have


def rag_eval(index, dataset, client=None, k: int = 5, cfg: dict | None = None, conn=None, *, embedder=None, reranker=None,
             judge_answers: bool = False) -> dict:
    """Retrieval quality on a labeled question set.

    Hit@K   — share of answerable questions with a correct document in the top K (K = 1, 3, 5)
    MRR     — mean of 1 / rank of the first correct document (0 when none in the top K)
    Recall@K — share of all expected documents found in the top K (useful when a question has several sources)
    Context recall — share of expected facts present in the passages actually given to the AI (top context_k)
    Faithfulness — share of answer sentences supported by their cited passage (only with judge_answers=True and an AI client)
    Abstention — share of unanswerable questions correctly answered "not in the knowledge base"."""
    items = load_items(dataset)
    cfg = dict(cfg or {}, top_k=max(k, 5))
    ctx_k = int(cfg.get("context_k", 3))
    hits_at = {1: [], 3: [], 5: []}
    rr, rec, p_at_k, ctx_rec, faith, lat = [], [], [], [], [], []
    correct_abstain = false_abstain = answerable_n = unans_n = 0
    rows = []
    for it in items:
        gen = client if (judge_answers and it["answerable"]) else None
        res = answer(it["q"], index, it["role"], gen, cfg, embedder=embedder, reranker=reranker, use_cache=False)
        lat.append(res["retrieval_ms"])
        srcs = [h["source"] for h in res["hits"]]
        exp = set(it.get("expected_sources") or [])
        row = {"question": it["q"], "role": it["role"], "answerable": it["answerable"], "top_sources": srcs[:5], "mode": res["mode"]}
        if it["answerable"]:
            answerable_n += 1
            rank = next((i + 1 for i, s in enumerate(srcs[:k]) if s in exp), None)
            for kk in hits_at:
                hits_at[kk].append(1.0 if rank and rank <= kk else 0.0)
            rr.append(1 / rank if rank else 0.0)
            rec.append(len(exp & set(srcs[:k])) / len(exp) if exp else 0.0)
            p_at_k.append(sum(s in exp for s in srcs[:k]) / max(len(srcs[:k]), 1) if srcs else 0.0)
            facts = it.get("expected_facts") or []
            ctx = " ".join(h["text"] for h in res["hits"][:ctx_k])
            found = [f for f in facts if _fact_found(f, ctx)]
            if facts:
                ctx_rec.append(len(found) / len(facts))
            false_abstain += not res["hits"]
            if gen is not None and res.get("faithfulness") is not None:
                faith.append(res["faithfulness"])
            row |= {"first_correct_rank": rank, "facts_found": f"{len(found)}/{len(facts)}" if facts else "-",
                    "missing_facts": "; ".join(f for f in facts if f not in found), "faithfulness": res.get("faithfulness")}
        else:
            unans_n += 1
            ok = not res["hits"] or res["status"] == "abstained"
            correct_abstain += ok
            row |= {"first_correct_rank": None, "facts_found": "-", "missing_facts": "", "abstained_correctly": ok}
        rows.append(row)
    m = lambda xs: round(statistics.mean(xs), 3) if xs else None
    out = {
        "k": k, "n": len(items), "answerable": answerable_n, "retrieval_mode": rows[0]["mode"] if rows else None,
        "hit_at_1": m(hits_at[1]), "hit_at_3": m(hits_at[3]), "hit_at_5": m(hits_at[5]), "mrr": m(rr),
        "recall_at_k": m(rec), "precision_at_k": m(p_at_k), "context_recall": m(ctx_rec),
        "faithfulness": m(faith) if faith else None,
        "unanswerable_abstention_rate": round(correct_abstain / unans_n, 3) if unans_n else None,
        "answerable_false_abstention_rate": round(false_abstain / answerable_n, 3) if answerable_n else None,
        "retrieval_latency_ms_mean": m(lat), "rows": rows,
    }
    if conn is not None:
        for key in ("hit_at_1", "hit_at_3", "hit_at_5", "mrr", "recall_at_k", "precision_at_k", "context_recall", "faithfulness",
                    "unanswerable_abstention_rate", "retrieval_latency_ms_mean"):
            if out[key] is not None:
                metrics.record(conn, f"eval_{key}", out[key], {"k": k, "mode": out["retrieval_mode"], "n": out["n"]})
    return out


def eval_history(conn, limit: int = 30) -> list[dict]:
    """One row per evaluation run (newest first), from the persisted metrics."""
    names = ["hit_at_3", "mrr", "context_recall", "faithfulness", "hit_at_1", "recall_at_k"]
    rows: dict[str, dict] = {}
    for r in conn.execute(f"SELECT name, value, at, labels FROM metrics WHERE name IN ({','.join('?' * len(names))}) ORDER BY id DESC LIMIT ?",
                          [f"eval_{n}" for n in names] + [limit * len(names)]):
        d = rows.setdefault(r["at"], {"run": r["at"].replace("T", " ")[:16], "mode": json.loads(r["labels"]).get("mode"),
                                      "questions": json.loads(r["labels"]).get("n")})
        d[r["name"][5:]] = r["value"]
    return list(rows.values())[:limit]


def labeled_documents() -> list[dict]:
    kw = ["irrigation", "soil moisture", "water", "sensor", "farm"]
    good = synthetic.abstract(1, 50, kw)
    return [
        {"name": "valid.docx", "data": synthetic.make_docx(good), "should_pass": True},
        {"name": "valid.pptx", "data": synthetic.make_pptx(synthetic.abstract(2, 50, kw)), "should_pass": True},
        {"name": "valid.pdf", "data": synthetic.make_pdf(synthetic.abstract(3, 50, kw)), "should_pass": True},
        {"name": "missing_methodology.docx", "data": synthetic.make_docx(synthetic.abstract(4, 60, kw, omit=("Methodology",))), "should_pass": False},
        {"name": "too_short.docx", "data": synthetic.make_docx(synthetic.abstract(5, 20, kw)), "should_pass": False},
        {"name": "empty_text.docx", "data": synthetic.make_docx([(s, "") for s in []]), "should_pass": False},
        {"name": "empty_methodology.docx", "data": synthetic.make_docx([(h, b if h != "Methodology" else "TBD") for h, b in synthetic.abstract(6, 60, kw)]), "should_pass": False},
        {"name": "corrupt.docx", "data": b"PK\x03\x04" + b"\x00" * 200, "should_pass": False},
        {"name": "non_english.docx", "data": synthetic.make_docx([(s, "Lorem ipsum dolor sit amet consectetur adipiscing elit sed eiusmod tempor " * 6) for s in synthetic.SECTIONS]), "should_pass": False},
    ]


def workflow_eval(policy: dict, conn=None) -> dict:
    ps = {"id": "AG-01", "title": "Precision irrigation scheduling", "keywords": ["irrigation", "soil moisture", "water", "sensor", "farm", "weather", "crop"]}
    tp = tn = fp = fn = 0
    rows, times = [], []
    for d in labeled_documents():
        t0 = time.perf_counter()
        pre = extract.precheck(d["name"], d["data"], policy)
        if not pre.ok:
            passed, why = False, pre.code
        else:
            ex = extract.extract(d["name"], d["data"])
            res = checks.hard_checks(ex.text, ex.error, policy, ps)
            errs = [r.check_id for r in res if r.result == "fail" and r.severity == "error"]
            passed, why = not errs, ",".join(errs)
        times.append((time.perf_counter() - t0) * 1000)
        if d["should_pass"] and passed:
            tp += 1
        elif d["should_pass"] and not passed:
            fn += 1  # false rejection
        elif not d["should_pass"] and not passed:
            tn += 1
        else:
            fp += 1  # missed violation
        rows.append({"doc": d["name"], "expected_pass": d["should_pass"], "actual_pass": passed, "reasons": why})
    n = tp + tn + fp + fn
    out = {"n": n, "accuracy": round((tp + tn) / n, 3), "false_rejection_rate": round(fn / max(tp + fn, 1), 3),
           "missed_violation_rate": round(fp / max(tn + fp, 1), 3), "mean_check_ms": round(statistics.mean(times), 2), "rows": rows}
    if conn is not None:
        for key in ("accuracy", "false_rejection_rate", "missed_violation_rate"):
            metrics.record(conn, f"eval_hardcheck_{key}", out[key])
    return out
