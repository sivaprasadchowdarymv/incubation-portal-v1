"""RAG retrieval evaluation and labeled workflow (hard-check) evaluation. Results are measured, never assumed."""
from __future__ import annotations

import json
import statistics
import time
from pathlib import Path

from evaluation import metrics, synthetic
from rag.answer import answer
from validation import checks, extract


def rag_eval(index, dataset_path: Path, client=None, k: int = 5, cfg: dict | None = None, conn=None) -> dict:
    items = json.loads(Path(dataset_path).read_text())["items"]
    cfg = dict(cfg or {}, top_k=k)
    p_at_k, r_at_k, lat, correct_abstain, false_abstain, answerable_n, unans_n = [], [], [], 0, 0, 0, 0
    rows = []
    for it in items:
        res = answer(it["q"], index, it["role"], client, cfg)
        lat.append(res["retrieval_ms"])
        srcs = [h["source"] for h in res["hits"]]
        exp = set(it["expected_sources"])
        abstained = res["status"] == "abstained"
        if it["answerable"]:
            answerable_n += 1
            p_at_k.append(sum(s in exp for s in srcs) / max(len(srcs), 1) if srcs else 0.0)
            r_at_k.append(len(exp & set(srcs)) / len(exp))
            false_abstain += abstained
        else:
            unans_n += 1
            correct_abstain += abstained
        rows.append({"q": it["q"], "status": res["status"], "top_sources": srcs, "mode": res["mode"]})
    out = {
        "k": k, "n": len(items), "retrieval_mode": rows[0]["mode"] if rows else None,
        "precision_at_k": round(statistics.mean(p_at_k), 3) if p_at_k else None,
        "recall_at_k": round(statistics.mean(r_at_k), 3) if r_at_k else None,
        "unanswerable_abstention_rate": round(correct_abstain / unans_n, 3) if unans_n else None,
        "answerable_false_abstention_rate": round(false_abstain / answerable_n, 3) if answerable_n else None,
        "retrieval_latency_ms_mean": round(statistics.mean(lat), 2) if lat else None,
        "rows": rows,
    }
    if conn is not None:
        for key in ("precision_at_k", "recall_at_k", "unanswerable_abstention_rate", "retrieval_latency_ms_mean"):
            if out[key] is not None:
                metrics.record(conn, f"eval_{key}", out[key], {"k": k, "mode": out["retrieval_mode"]})
    return out


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
