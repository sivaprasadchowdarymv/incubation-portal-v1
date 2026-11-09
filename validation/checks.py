"""Deterministic hard checks, relevance and similarity assessment."""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass

import numpy as np

STOPWORDS = set(
    "a an the and or but if of to in on for with by at from as is are was were be been being this that these those it its "
    "we our you your they their he she his her will would can could should may might must not no do does did have has had "
    "which who whom what when where why how all any each more most other some such only own same so than too very into "
    "about over under again further then once here there both few also using use used".split()
)
TOKEN = re.compile(r"[A-Za-zÀ-ɏऀ-෿]+")


@dataclass
class CheckResult:
    check_id: str
    result: str  # pass | fail | warn | skip
    severity: str  # info | warning | error
    message: str
    evidence: str = ""
    guidance: str = ""
    review_flag: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


def tokens(text: str) -> list[str]:
    return [t.lower() for t in TOKEN.findall(text or "")]


def normalize_hash(text: str) -> str:
    return hashlib.sha256(" ".join(tokens(text)).encode()).hexdigest()


HEAD_CLEAN = re.compile(r"^[\s#*\d.):(-]+")


def _section_of(line: str, required: dict[str, list[str]]) -> tuple[str | None, str]:
    """If the line is a heading for a required section return (section, inline_rest)."""
    norm = HEAD_CLEAN.sub("", line).strip()
    low = norm.lower()
    for name, aliases in required.items():
        for a in sorted(aliases, key=len, reverse=True):
            a = a.lower()
            if low.startswith(a):
                rest = norm[len(a):]
                rest_s = rest.lstrip(" /&-")
                if rest_s.startswith(":"):  # inline heading  "Methodology: we will ..."
                    return name, rest_s[1:].strip()
                if len(low.split()) <= 10 and not low.endswith("."):
                    return name, ""
    return None, ""


def find_sections(text: str, required: dict[str, list[str]]) -> tuple[list[str], list[str], dict[str, str]]:
    """Returns (found, missing, bodies). Body = text between this heading and the next required heading."""
    bodies: dict[str, list[str]] = {}
    current = None
    for ln in (text or "").splitlines():
        sec, rest = _section_of(ln, required)
        if sec and sec not in bodies:
            current = sec
            bodies[sec] = [rest] if rest else []
        elif sec:  # repeated heading of an already-seen section
            current = sec
        elif current:
            bodies[current].append(ln)
    found = [n for n in required if n in bodies]
    missing = [n for n in required if n not in bodies]
    return found, missing, {k: "\n".join(v).strip() for k, v in bodies.items()}


def language_check(text: str, cfg: dict) -> CheckResult:
    toks = tokens(text)
    letters = [c for c in text if c.isalpha()]
    non_ascii = sum(1 for c in letters if ord(c) > 127) / max(len(letters), 1)
    sw = sum(1 for t in toks if t in STOPWORDS) / max(len(toks), 1)
    ok = sw >= cfg["min_stopword_ratio"] and non_ascii <= cfg["max_non_ascii_ratio"]
    ev = f"stopword_ratio={sw:.3f} (min {cfg['min_stopword_ratio']}), non_ascii_ratio={non_ascii:.3f} (max {cfg['max_non_ascii_ratio']})"
    if ok:
        return CheckResult("LANGUAGE", "pass", "info", "Text is in English.", ev)
    return CheckResult("LANGUAGE", "fail", cfg.get("severity", "error"), "The document does not appear to be written in English.",
                       ev, "Write the complete abstract in English, as required by the template.")


def _kw_hits(low_text: str, kws: list[str]) -> list[str]:
    return [k for k in dict.fromkeys(k.lower().strip() for k in kws if k and k.strip()) if re.search(r"\b" + re.escape(k) + r"s?\b", low_text)]


def relevance_check(text: str, ps: dict, track: dict | None, cfg: dict) -> CheckResult:
    """Is the abstract related to the selected track/domain? Counts distinct keyword matches (track + PS keywords)."""
    low = " ".join(tokens(text))
    tkws, pkws = list((track or {}).get("keywords", [])), list(ps.get("keywords", []))
    t_hits, p_hits = _kw_hits(low, tkws), _kw_hits(low, pkws)
    hits = list(dict.fromkeys(t_hits + p_hits))
    need = int(cfg.get("min_keyword_matches", 2))
    tname = (track or {}).get("name", ps.get("domain_id", "the selected track"))
    ev = json.dumps({"track_keywords_matched": t_hits, "ps_keywords_matched": p_hits, "required_matches": need,
                     "track_keywords": tkws, "ps_keywords": pkws})
    if len(hits) >= need:
        return CheckResult("DOMAIN_RELEVANCE", "pass", "info", f"Related to the {tname} track ({len(hits)} keyword(s) matched: {', '.join(hits)}).", ev)
    action = cfg.get("action", "review")
    msg = f"Not clearly related to the {tname} track: only {len(hits)} of the required {need} domain keywords found ({', '.join(hits) or 'none'})."
    guide = f"Describe how your idea addresses '{ps['title']}' using the track's terminology (e.g. {', '.join((tkws + pkws)[:6])})."
    if action == "fail":
        return CheckResult("DOMAIN_RELEVANCE", "fail", "error", msg, ev, guide)
    return CheckResult("DOMAIN_RELEVANCE", "warn", "warning", msg + " Routed to human review.", ev, guide, review_flag=True)


def _ps_text(p: dict) -> str:
    return " ".join([p.get("title", ""), p.get("description", "") or "", " ".join(p.get("keywords", [])), p.get("track_name", "") or ""])


def rank_problem_statements(text: str, ps_list: list[dict], embedder=None) -> tuple[list[dict], str]:
    """Similarity of the abstract to every problem statement in the database. TF-IDF cosine (or embeddings if available)."""
    if not ps_list:
        return [], "none"
    if embedder is not None:
        try:
            vecs = embedder.embed([" ".join(text.split()[:2000])] + [_ps_text(p) for p in ps_list])
            sims = vecs[1:] @ vecs[0]
            ranked = sorted(({"ps_id": p["id"], "title": p["title"], "score": round(float(s), 4)} for p, s in zip(ps_list, sims)), key=lambda r: -r["score"])
            return ranked, "semantic_embedding_cosine"
        except Exception:  # Ollama unavailable -> deterministic fallback, reported as such
            pass
    docs = [[t for t in tokens(_ps_text(p)) if t not in STOPWORDS] for p in ps_list]
    q = [t for t in tokens(text) if t not in STOPWORDS]
    vocab = {t: i for i, t in enumerate(sorted({t for d in docs for t in d}))}
    if not vocab:
        return [{"ps_id": p["id"], "title": p["title"], "score": 0.0} for p in ps_list], "tfidf_cosine"
    n = len(docs)
    df = np.zeros(len(vocab))
    for d in docs:
        for t in set(d):
            df[vocab[t]] += 1
    idf = np.log((1 + n) / (1 + df)) + 1

    def vec(toks):
        v = np.zeros(len(vocab))
        for t in toks:
            if t in vocab:
                v[vocab[t]] += 1
        v = (1 + np.log(np.where(v > 0, v, 1))) * (v > 0) * idf
        nrm = np.linalg.norm(v)
        return v / nrm if nrm else v

    qv = vec(q)
    ranked = sorted(({"ps_id": p["id"], "title": p["title"], "score": round(float(vec(d) @ qv), 4)} for p, d in zip(ps_list, docs)), key=lambda r: -r["score"])
    return ranked, "tfidf_cosine"


def ps_similarity_check(text: str, ps: dict, ps_list: list[dict], cfg: dict, embedder=None) -> CheckResult:
    if not cfg.get("enabled", True):
        return CheckResult("PS_SIMILARITY", "skip", "info", "Problem-statement similarity check disabled by administrator.")
    if len(ps_list) < 2:
        return CheckResult("PS_SIMILARITY", "skip", "info", "Only one problem statement in the database; nothing to compare.")
    ranked, method = rank_problem_statements(text, ps_list, embedder)
    sel = next((r for r in ranked if r["ps_id"] == ps["id"]), {"ps_id": ps["id"], "title": ps["title"], "score": 0.0})
    best = ranked[0]
    rank = [r["ps_id"] for r in ranked].index(ps["id"]) + 1 if any(r["ps_id"] == ps["id"] for r in ranked) else None
    ev = json.dumps({"method": method, "selected": sel, "selected_rank": rank, "top_matches": ranked[:3]})
    margin = float(cfg.get("margin", 0.05))
    if best["ps_id"] == ps["id"] or best["score"] - sel["score"] <= margin:
        return CheckResult("PS_SIMILARITY", "pass", "info",
                           f"Most similar problem statement: {best['ps_id']} ({best['score']:.2f}); selected {ps['id']} scores {sel['score']:.2f} (rank {rank}).", ev)
    msg = (f"The abstract is closer to {best['ps_id']} '{best['title']}' ({best['score']:.2f}) than to your selected "
           f"{ps['id']} '{ps['title']}' ({sel['score']:.2f}).")
    guide = f"Make sure the abstract addresses your locked problem statement {ps['id']}: {ps['title']}."
    if cfg.get("action", "review") == "fail":
        return CheckResult("PS_SIMILARITY", "fail", "error", msg, ev, guide)
    return CheckResult("PS_SIMILARITY", "warn", "warning", msg + " Routed to human review.", ev, guide, review_flag=True)


def hard_checks(text: str, extraction_error: str | None, policy: dict, ps: dict, track: dict | None = None,
                ps_list: list[dict] | None = None, embedder=None) -> list[CheckResult]:
    v = policy["validation"]
    out: list[CheckResult] = []
    if extraction_error:
        out.append(CheckResult("FILE_READABLE", "fail", "error", extraction_error, "", "Re-export the document from the original editor and upload again."))
        return out
    out.append(CheckResult("FILE_READABLE", "pass", "info", "Document text extracted."))
    words = len(tokens(text))
    if words == 0:
        out.append(CheckResult("CONTENT_NONEMPTY", "fail", "error", "No readable text found.", "word_count=0",
                               "Ensure text is not embedded only as images; use the approved template."))
        return out
    out.append(CheckResult("CONTENT_NONEMPTY", "pass", "info", "Document contains text.", f"word_count={words}"))
    # 2. format / compliance
    found, missing, bodies = find_sections(text, v["required_sections"])
    if missing:
        out.append(CheckResult("REQUIRED_SECTIONS", "fail", "error", f"Missing required sections: {', '.join(missing)}.",
                               json.dumps({"found": found, "missing": missing}),
                               "Add each missing section as a heading exactly as in the template: " + ", ".join(v["required_sections"]) + "."))
    else:
        out.append(CheckResult("REQUIRED_SECTIONS", "pass", "info", "All required sections present.", json.dumps({"found": found})))
    per = int(v.get("min_words_per_section", 0))
    if per and found:
        counts = {k: len(tokens(bodies.get(k, ""))) for k in found}
        thin = {k: c for k, c in counts.items() if c < per}
        if thin:
            out.append(CheckResult("SECTION_CONTENT", "fail", "error",
                                   "Section(s) without enough content: " + ", ".join(f"{k} ({c} words)" for k, c in thin.items()) + f"; each needs at least {per} words.",
                                   json.dumps(counts), "Write substantive content under every required heading."))
        else:
            out.append(CheckResult("SECTION_CONTENT", "pass", "info", f"Every section has at least {per} words.", json.dumps(counts)))
    # 3. minimum content length of the complete document
    if words < v["min_words"]:
        out.append(CheckResult("MIN_WORDS", "fail", "error", f"Document has {words} words; the minimum for the complete document is {v['min_words']}.",
                               f"word_count={words}", f"Add at least {v['min_words'] - words} more words of substantive content."))
    elif words > v["max_words"]:
        out.append(CheckResult("MAX_WORDS", "fail", "error", f"Word count {words} exceeds the maximum of {v['max_words']}.",
                               f"word_count={words}", f"Remove at least {words - v['max_words']} words."))
    else:
        out.append(CheckResult("WORD_COUNT", "pass", "info", f"Word count {words} within limits ({v['min_words']}-{v['max_words']}).", f"word_count={words}"))
    # 4. language
    out.append(language_check(text, v["language"]))
    # 1. domain relevance
    out.append(relevance_check(text, ps, track, v["relevance"]))
    # 5. similarity with existing problem statements
    if ps_list is not None:
        out.append(ps_similarity_check(text, ps, ps_list, v.get("ps_similarity", {}), embedder))
    return out


# ---------------- similarity ----------------

def shingles(text: str, n: int) -> set[tuple]:
    t = tokens(text)
    if len(t) < n:
        return {tuple(t)} if t else set()
    return {tuple(t[i : i + n]) for i in range(len(t) - n + 1)}


def jaccard(a: set, b: set) -> float:
    u = a | b
    return len(a & b) / len(u) if u else 0.0


def exceeds_threshold(score: float | None, threshold: float) -> bool:
    """Review rule: STRICTLY greater than the threshold. Exactly-equal does not trigger."""
    return score is not None and score > threshold


def similarity_assessment(text: str, team_id: int, corpus: list[dict], policy: dict, embedder=None) -> tuple[list[dict], list[CheckResult]]:
    """corpus rows: {id, team_id, extracted_text, text_hash}. Exact duplicates follow duplicates.exact_action; similarity scores alone never reject."""
    cfg = policy["similarity"]
    thr, n, methods = cfg["review_threshold"], cfg["shingle_size"], set(cfg["flag_methods"])
    findings: list[dict] = []
    results: list[CheckResult] = []
    others = [r for r in corpus if r["team_id"] != team_id and r["extracted_text"]]
    own_prev = [r for r in corpus if r["team_id"] == team_id and r["extracted_text"]]
    h = normalize_hash(text)
    scope = f"{len(others)} submission(s) from other teams in this cohort"

    for r in own_prev:
        if r["text_hash"] == h:
            results.append(CheckResult("SAME_AS_PREVIOUS_ATTEMPT", "warn", "warning", "This file's text is identical to one of your earlier attempts.",
                                       f"previous_submission={r['id']}", "Address the earlier validation feedback before resubmitting."))
            break

    if not others:
        findings.append({"compared_submission_id": None, "method": "lexical_shingle_jaccard", "score": None, "comparison_scope": "none",
                         "evidence": "No comparison corpus available; similarity score not computed.", "flagged": False})
        results.append(CheckResult("SIMILARITY", "skip", "info", "No other submissions available for comparison; no similarity score computed."))
        return findings, results

    dup = [r for r in others if r["text_hash"] == h]
    for r in dup:
        flag = "exact_hash" in methods
        findings.append({"compared_submission_id": r["id"], "method": "exact_hash", "score": 1.0, "comparison_scope": scope,
                         "evidence": "Normalized text identical", "flagged": flag})
    dcfg = policy.get("duplicates", {})
    if dup:
        if dcfg.get("exact_action", "review") == "fail":
            results.append(CheckResult("DUPLICATE_SUBMISSION", "fail", "error",
                                       "Rejected as a duplicate: the text is identical to a submission already made by another team.",
                                       f"matches={len(dup)} (normalized text hash)", "Submit your team's own original abstract."))
        else:
            results.append(CheckResult("DUPLICATE_SUBMISSION", "warn", "warning", "Text is identical to another team's submission. Routed to human review.",
                                       f"matches={len(dup)}", "Await reviewer decision.", review_flag="exact_hash" in methods))

    mine = shingles(text, n)
    best_id, best, best_shared = None, -1.0, set()
    for r in others:
        s_other = shingles(r["extracted_text"], n)
        sc = jaccard(mine, s_other)
        if sc > best:
            best_id, best, best_shared = r["id"], sc, mine & s_other
    flag = exceeds_threshold(best, thr) and "lexical" in methods
    sample = [" ".join(s) for s in list(sorted(best_shared))[:5]]
    findings.append({"compared_submission_id": best_id, "method": "lexical_shingle_jaccard", "score": round(best, 4), "comparison_scope": scope,
                     "evidence": json.dumps({"shared_shingles": len(best_shared), "sample": sample}), "flagged": flag})
    near = dcfg.get("near_duplicate_threshold")
    if not dup and near is not None and best >= float(near):
        results.append(CheckResult("NEAR_DUPLICATE", "warn", "warning",
                                   f"Near-duplicate of another team's submission ({best:.1%} overlap). Routed to priority human review (not auto-rejected).",
                                   f"max_jaccard={best:.4f}", "A reviewer will compare both submissions.", review_flag=True))
    if flag:
        results.append(CheckResult("SIMILARITY", "warn", "warning",
                                   f"Lexical overlap {best:.1%} exceeds {thr:.0%}. Flagged for human review; this is an indicator, not proof of plagiarism.",
                                   f"max_jaccard={best:.4f} ({n}-word shingles)", "A reviewer will assess originality.", review_flag=True))
    else:
        results.append(CheckResult("SIMILARITY", "pass", "info", f"Max lexical overlap {best:.1%} (review threshold: > {thr:.0%}).", f"max_jaccard={best:.4f}"))

    if embedder is not None:
        try:
            texts = [" ".join(text.split()[:2000])] + [" ".join(r["extracted_text"].split()[:2000]) for r in others]
            vecs = embedder.embed(texts)
            sims = vecs[1:] @ vecs[0]
            i = int(np.argmax(sims))
            sc = float(sims[i])
            sflag = exceeds_threshold(sc, thr) and "semantic" in methods
            findings.append({"compared_submission_id": others[i]["id"], "method": "semantic_cosine", "score": round(sc, 4), "comparison_scope": scope,
                             "evidence": "embedding cosine similarity (informational unless 'semantic' in flag_methods)", "flagged": sflag})
            if sflag:
                results.append(CheckResult("SEMANTIC_SIMILARITY", "warn", "warning", f"Semantic similarity {sc:.1%} exceeds {thr:.0%}. Routed to review.",
                                           f"cosine={sc:.4f}", "", review_flag=True))
        except Exception as exc:
            findings.append({"compared_submission_id": None, "method": "semantic_cosine", "score": None, "comparison_scope": "unavailable",
                             "evidence": f"Semantic similarity unavailable: {type(exc).__name__}", "flagged": False})
    return findings, results
