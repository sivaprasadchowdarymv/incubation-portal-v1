"""Content analyser agent — stage 2 of the relevance screening.

Stage 1 (validation/checks.py, DOMAIN_RELEVANCE): the abstract must contain the track / problem-statement keywords.
            No match -> rejected. Only when stage 1 passes does this agent run.
Stage 2 (this file, CONTENT_TECHNICAL): read the WHOLE submission and decide, sentence by sentence and in context,
            what is technical content and what is not. At least `min_technical_percent` (default 75 %) of the text,
            weighted by words, must be technical; otherwise the submission is rejected (or sent to review, if the admin
            chooses that action).

What counts as technical: the concrete problem with facts and figures, who is affected and how, the method, design,
architecture, data, algorithms, hardware/software, experiments, implementation plan, costs, metrics, measurable outcomes
and deliverables.
What does not: stories, film plots and fiction, personal memories, emotions, slogans and marketing hype, general
statements that say nothing specific, unrelated topics, filler, and instructions aimed at the evaluator.

Two ways of reading the text:
  ai     (when AI keys are configured) — an AI model reads the whole abstract, writes one sentence on what it really
         proposes ("essence") and labels every numbered sentence T / N. The percentage is computed here from those
         labels, not taken from the AI. The abstract is passed as untrusted data (see agents/judge.py).
  local  (no AI, or the AI fails) — every sentence is scored on technical signals (problem-statement vocabulary, technical
         vocabulary, quantities with units, technical acronyms) against non-technical signals (story/film words, emotional
         and hype words, characters acting in the past tense, personal anecdotes). Unclear sentences take their label from
         the sentences around them, and a document that reads as a story gives unclear sentences no benefit of the doubt.
The reviewer and the student see the percentage, the essence (AI) and examples of the non-technical sentences."""
from __future__ import annotations

import json
import re

from validation.checks import STOPWORDS, CheckResult

WORD = re.compile(r"[a-z][a-z\-]{2,}")
SENT = re.compile(r"(?<=[.!?])\s+|\n+")

GENERIC = set("""using based system systems solution solutions platform develop development design create build enable ensure improve
provide need needs current existing new better use way ways approach real time across within like also may can will one two three many
much more less high low key core main often still make making help helps challenge challenges problem problems issue issues impact impacts
lack limited manual process processes give scalable smart effective efficient today every early detection detect""".split())

# word beginnings that signal technical content in any track (matched with startswith, so "sensor" covers "sensors")
TECH_PREFIXES = tuple("""sensor data dataset model prototyp pilot survey algorithm app applica mobile device hardware software dashboard
analys analyz measur accura evaluat test deploy install implement user cost percent trial compar baseline train classif network iot
cloud gateway node battery solar power energy module api database server report metric target reduc increas monitor predict
forecast schedul interview feedback stakeholder calibrat parameter signal frequen voltage current efficien throughput latency simulat
optimi framework integrat pipeline interface protocol wireless gps image camera vision regress inference architect microcontroll esp32
arduino raspberry lora bluetooth wifi offline online sms voice queue score threshold rule config storage sync encrypt secur privacy
validat specific sensitiv benchmark experiment method sample label annotat collect record register form tablet phone android
consult referr triage diagnos treatment dose clinic patient record yield harvest irrigat pump fertili pesticid spray nitrat runoff
moisture soil weather crop leaf disease detect pressure temperature humidity flow rate volume capacity load grid inverter panel
logist inventor supply route procure pric market budget fund revenue subsid licens open-source repositor manual control actuat valve
motor drone robot navigat map gis satellite remote spectral ndvi""".split())

QUANTITY = re.compile(r"(?:₹|rs\.?\s?|inr\s?|\$)\s?\d|\b\d[\d,.]*\s?(?:%|percent|per cent|kg|g|km|m|cm|mm|hz|khz|mhz|ghz|v|mv|ma|mah|w|kw|"
                      r"kwh|mw|s|ms|sec|seconds?|minutes?|hours?|days?|weeks?|months?|years?|rupees|acres?|hectares?|ha|litres?|liters?|l|"
                      r"ppm|°c|c|mb|gb|tb|people|patients|farmers|families|users|plots|clinics|villages|schools|students|units|nodes)\b", re.I)
ACRONYM = re.compile(r"\b(?:[A-Z]{2,6}\d*|[A-Z][a-z]?[A-Z]{1,4}|\d[Gg])\b")
ACRONYM_IGNORE = {"I", "OK", "THE", "AND", "IMPORTANT", "NOTE", "AI"}

# distinctive vocabulary of stories and films; ambiguous words (story, plot, scene, fight, character) are deliberately left out
NARRATIVE = set("""hero heroes heroine villain villains protagonist antagonist romance romantic lover lovers marries marry married wedding
kidnap kidnaps kidnapped revenge climax interval superhit blockbuster movie movies film films cinema
song songs sings sang dances dancing goons henchmen landlord princess prince kingdom fairy dragon magic wizard ghost haunted heartbroken
tears weeps cried cries prays temple heroic sequel trailer box-office screenplay actor actress grandfather grandmother grandpa grandma
childhood memories sunset""".split()) | {"box office", "once upon a time", "happily ever after", "the end"}

# emotion, hype and slogans: say nothing technical
EMOTIVE = set("""dream dreams dreamt passion passionate love loves loved heart soul inspire inspires inspiring inspiration amazing incredible
revolutionary revolutionise revolutionize magical beautiful wonderful sad happy happier happily smile smiles smiling joy joyful proud
pride glory glorious destiny journey believe believes belief hope hopes wish wishes grateful blessed god divine miracle awesome greatest
world-class game-changer extraordinary fantastic marvellous marvelous brighter greener happier empowered precious heroes noble
dedication impossible honour honor celebrate celebrates celebration fans trophy trophies stadium victory famous thrilled excited
legendary unbelievable fabulous""".split())

PERSON = re.compile(r"\b(?:he|she|him|his|her|hers|my|me|i)\b", re.I)
STORY_VERB = re.compile(r"\b(?:was|were|went|walked|ran|cried|sang|danced|smiled|laughed|looked|saw|told|said|felt|woke|fell|loved|"
                        r"married|visited|sat|watched|remember|remembered|spent|grew up)\b", re.I)
STEM_SUFFIXES = ("ations", "ation", "ings", "ing", "ies", "ers", "er", "es", "ed", "s")


def stem(w: str) -> str:
    for suf in STEM_SUFFIXES:
        if w.endswith(suf) and len(w) - len(suf) >= 4:
            return w[: -len(suf)]
    return w


def topic_stems(ps: dict, track: dict | None) -> set[str]:
    text = " ".join([ps.get("title", ""), ps.get("description", "") or "", " ".join(ps.get("keywords", []) or []),
                     (track or {}).get("name", ""), " ".join((track or {}).get("keywords", []) or [])]).lower()
    return {stem(w) for w in WORD.findall(text) if w not in STOPWORDS and w not in GENERIC and len(w) >= 4}


def sentences(text: str) -> list[str]:
    """Sentences worth judging (headings and fragments under 4 words are skipped)."""
    return [s.strip() for s in SENT.split(text or "") if len(WORD.findall(s.lower())) >= 4]


def _signals(s: str, vocab: set[str]) -> dict:
    low = s.lower()
    ws = WORD.findall(low)
    topic = {w for w in ws if stem(w) in vocab}
    tech = {w for w in ws if w.startswith(TECH_PREFIXES) and w not in topic}
    qty = len(QUANTITY.findall(s))
    acr = {a for a in ACRONYM.findall(s) if a not in ACRONYM_IGNORE}
    narr = {w for w in ws if w in NARRATIVE} | {p for p in NARRATIVE if " " in p and p in low}
    emo = {w for w in ws if w in EMOTIVE}
    person = len(PERSON.findall(s))
    story = bool(STORY_VERB.search(s)) and (person > 0 or bool(re.search(r"\b[A-Z][a-z]{2,}\b", s[1:])))
    t = len(topic) + len(tech) + min(2, qty) + 0.5 * min(2, len(acr))
    n = 2 * len(narr) + len(emo) + (1.5 if story else 0) + (0.5 if person >= 2 else 0)
    return {"t": t, "n": n, "topic": sorted(topic), "tech": sorted(tech), "narrative": sorted(narr), "emotive": sorted(emo), "story": story}


def local_labels(sents: list[str], vocab: set[str]) -> tuple[list[str], dict]:
    """T / N / ? per sentence, then context: '?' sentences follow their neighbours (no benefit of the doubt in a story)."""
    sig = [_signals(s, vocab) for s in sents]
    narrative_doc = len({w for g in sig for w in g["narrative"]}) >= 4   # the text as a whole reads as a story
    lab = []
    for g in sig:
        if narrative_doc and (g["narrative"] or g["story"]):
            lab.append("N")                 # inside a story, a sentence with story elements is story, whatever keywords it has
        elif g["n"] >= 2 and g["n"] >= g["t"]:
            lab.append("N")
        elif g["t"] >= 2 and g["t"] > g["n"]:
            lab.append("T")
        elif g["t"] >= 1 and g["n"] == 0:
            lab.append("?")
        elif g["t"] == 0 and g["n"] == 0:
            lab.append("0")                 # neutral wording: only counts inside clearly technical surroundings
        else:
            lab.append("N")
    out = []
    for i, x in enumerate(lab):
        if x in "TN":
            out.append(x)
            continue
        near = [lab[j] for j in (i - 1, i + 1) if 0 <= j < len(lab)]
        need = len(near) if x == "0" else max(1, len(near) - 1)
        out.append("T" if not narrative_doc and near and near.count("T") >= need else "N")
    words = sorted({w for g in sig for w in g["narrative"]})
    return out, {"narrative_words": words, "emotive_words": sorted({w for g in sig for w in g["emotive"]})}


def technical_share(sents: list[str], labels: list[str]) -> float:
    total = sum(len(WORD.findall(s.lower())) for s in sents) or 1
    return sum(len(WORD.findall(s.lower())) for s, x in zip(sents, labels) if x == "T") / total


def _short(s: str, n: int = 110) -> str:
    s = re.sub(r"\s+", " ", s).strip()
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


def analyse(text: str, ps: dict, track: dict | None, policy: dict, ai_client=None, keywords_ok: bool = True) -> list[CheckResult]:
    cfg = policy.get("agents", {}).get("content", {})
    if not cfg.get("enabled", True) or not (text or "").strip():
        return []
    if not keywords_ok:
        return [CheckResult("CONTENT_TECHNICAL", "skip", "info", "Content analysis not run: the keyword check (stage 1) did not pass.")]
    sents = sentences(text)
    if len(sents) < 4:
        return []
    need = float(cfg.get("min_technical_percent", 75))
    vocab = topic_stems(ps, track)
    local, extra = local_labels(sents, vocab)
    method, labels, essence, on_ps = "local", local, "", True
    if ai_client is not None and cfg.get("use_ai", True):
        from . import judge
        v = judge.technical_reading(ai_client, sents, ps)
        if v:
            method, labels, essence, on_ps = "ai", v["labels"], v["essence"], v["addresses_problem_statement"]
    share = technical_share(sents, labels)
    pct = round(100 * share)
    non_tech = [_short(s) for s, x in zip(sents, labels) if x == "N"]
    ev = json.dumps({"method": method, "technical_percent": pct, "required_percent": need, "sentences": len(sents),
                     "technical_sentences": labels.count("T"), "essence": essence, "addresses_problem_statement": on_ps,
                     "local_technical_percent": round(100 * technical_share(sents, local)), "labels": "".join(labels),
                     "non_technical_examples": non_tech[:6]} | extra)
    how = "AI reading of the whole abstract" if method == "ai" else "automatic reading of the whole abstract"
    local_pct = round(100 * technical_share(sents, local))
    if method == "ai" and (pct >= need and on_ps) != (local_pct >= need) and (abs(pct - local_pct) >= 30 or not on_ps):
        # the AI and the portal's own reader strongly disagree: a person decides (protects against a misled AI and against AI mistakes)
        return [CheckResult("CONTENT_TECHNICAL", "warn", "warning",
                            f"The AI reading ({pct}% technical" + ("" if on_ps else ", not about the problem statement")
                            + f") and the portal's own reading ({local_pct}%) strongly disagree "
                            f"(minimum {need:g}%). Routed to human review." + (f" AI understood it as: {essence}" if essence else ""),
                            ev, "A reviewer will read the abstract.", review_flag=True)]
    if pct >= need and on_ps:
        return [CheckResult("CONTENT_TECHNICAL", "pass", "info",
                            f"Content is technical: {pct}% of the text (minimum {need:g}%), by {how}." + (f" Understood as: {essence}" if essence else ""), ev)]
    why = []
    if pct < need:
        why.append(f"only {pct}% of the text is technical content (minimum {need:g}%)")
    if not on_ps:
        why.append("it does not address the problem statement")
    msg = "Content check failed: " + " and ".join(why) + f" ({how})."
    if essence:
        msg += f" The submission was understood as: {essence}"
    if non_tech:
        msg += " Non-technical parts include: " + " | ".join(f"“{x}”" for x in non_tech[:3])
    guide = (f"Write every section about '{ps.get('title', '')}' in technical terms: the problem with facts and figures, who is affected, "
             "your method/design, data, implementation and measurable outcomes. Stories, slogans, emotions and general statements do not count, "
             "and adding keywords does not help.")
    if cfg.get("action", "reject") == "review":
        return [CheckResult("CONTENT_TECHNICAL", "warn", "warning", msg + " Routed to human review.", ev, guide, review_flag=True)]
    return [CheckResult("CONTENT_TECHNICAL", "fail", "error", msg, ev, guide)]
