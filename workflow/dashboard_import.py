"""Read the 'PS dashboard' HTML handbook (const DOMAINS / const ORIGINAL_DATA) and turn it into the portal's
tracks & problem-statements catalogue (CSV for Programme Setup, or a direct import).

Pure Python: no browser/Node needed. The HTML is only parsed as data, never executed."""
from __future__ import annotations

import csv
import io
import re
from collections import Counter

from validation.checks import STOPWORDS

GENERIC = set("""using based system systems solution solutions platform develop development design create build enable enabling ensure
improve improving improved provide providing need needs current existing new better use way ways approach real time across within
like also may can will one two three many much more less high low key core main across often still make making help helps
challenge challenges problem problems issue issues impact impacts lack limited manual process processes data give gives giving
scalable dynamic intelligent smart experienced advanced robust efficient effective optimal faster today every""".split())
WORD = re.compile(r"[a-zA-Z][a-zA-Z\-]{2,}")


class _JS:
    """Minimal parser for JavaScript object/array literals (unquoted keys, ' \" ` strings, trailing commas, comments)."""

    def __init__(self, s: str):
        self.s, self.i = s, 0

    def ws(self):
        s = self.s
        while self.i < len(s):
            if s[self.i].isspace():
                self.i += 1
            elif s.startswith("//", self.i):
                self.i = s.find("\n", self.i) if s.find("\n", self.i) != -1 else len(s)
            elif s.startswith("/*", self.i):
                self.i = s.index("*/", self.i) + 2
            else:
                break

    def value(self):
        self.ws()
        c = self.s[self.i]
        if c == "{":
            return self.obj()
        if c == "[":
            return self.arr()
        if c in "'\"`":
            return self.string()
        m = re.compile(r"-?\d+(\.\d+)?").match(self.s, self.i)
        if m:
            self.i = m.end()
            return float(m.group()) if m.group(1) else int(m.group())
        m = re.compile(r"[A-Za-z_$][\w$]*").match(self.s, self.i)
        if m:
            self.i = m.end()
            return {"true": True, "false": False, "null": None, "undefined": None}.get(m.group(), m.group())
        raise ValueError(f"unexpected character at {self.i}")

    def string(self):
        q, out, s = self.s[self.i], [], self.s
        self.i += 1
        while s[self.i] != q:
            if s[self.i] == "\\":
                self.i += 1
                out.append({"n": "\n", "t": "\t"}.get(s[self.i], s[self.i]))
            else:
                out.append(s[self.i])
            self.i += 1
        self.i += 1
        return "".join(out)

    def arr(self):
        self.i += 1
        out = []
        while True:
            self.ws()
            if self.s[self.i] == "]":
                self.i += 1
                return out
            out.append(self.value())
            self.ws()
            if self.s[self.i] == ",":
                self.i += 1

    def obj(self):
        self.i += 1
        out = {}
        while True:
            self.ws()
            if self.s[self.i] == "}":
                self.i += 1
                return out
            if self.s[self.i] in "'\"`":
                key = self.string()
            else:
                m = re.compile(r"[\w$]+").match(self.s, self.i)
                key, self.i = m.group(), m.end()
            self.ws()
            self.i += 1  # ':'
            out[key] = self.value()
            self.ws()
            if self.s[self.i] == ",":
                self.i += 1


def _literal_after(html: str, name: str):
    m = re.search(r"(?:const|let|var)\s+" + name + r"\s*=\s*([\[{])", html)
    if not m:
        raise ValueError(f"'{name}' not found in the file")
    p = _JS(html)
    p.i = m.start(1)
    return p.value()


def _text(v) -> str:
    return " ".join(_text(x) for x in v) if isinstance(v, list) else str(v or "")


def keywords(text: str, prefer: str = "", n: int = 8) -> list[str]:
    """Distinctive words: title words first, then the most frequent content words (no stop/generic words)."""
    ok = lambda w: w not in STOPWORDS and w not in GENERIC and len(w) > 3  # noqa: E731
    first = [w for w in (x.lower().strip("-") for x in WORD.findall(prefer)) if ok(w)]
    freq = Counter(w for w in (x.lower().strip("-") for x in WORD.findall(text)) if ok(w))
    out = list(dict.fromkeys(first))
    out += [w for w, _ in freq.most_common() if w not in out]
    return out[:n]


def parse_dashboard(html: str) -> tuple[dict, list[dict]]:
    return _literal_after(html, "DOMAINS"), _literal_after(html, "ORIGINAL_DATA")


def dashboard_rows(html: str) -> list[dict]:
    """Catalogue rows in the Programme Setup CSV format."""
    domains, items = parse_dashboard(html)
    rows = []
    for it in items:
        tkey = str(it.get("track", ""))
        dom = domains.get(tkey, {}) if isinstance(domains, dict) else {}
        desc_parts = [str(it.get("hook", "")).strip()]
        if it.get("challenges"):
            desc_parts.append("Challenges: " + "; ".join(_text(x) for x in it["challenges"]))
        if it.get("impact"):
            desc_parts.append("Expected impact: " + "; ".join(_text(x) for x in it["impact"]))
        if it.get("category"):
            desc_parts.append(f"Category: {it['category']}")
        if it.get("mapping"):
            desc_parts.append(f"Mapping: {it['mapping']}")
        body = " ".join([str(it.get("title", "")), _text(it.get("hook")), _text(it.get("challenges")), _text(it.get("category"))])
        rows.append({
            "track_id": f"T{tkey}",
            "track_name": str(dom.get("name") or f"Track {tkey}").strip(),
            "track_description": "",
            "track_keywords": "",
            "ps_id": re.sub(r"[^A-Za-z0-9_-]", "", str(it.get("num") or f"PS{it.get('id')}"))[:20],
            "ps_title": str(it.get("title", "")).strip(),
            "ps_description": "\n".join(p for p in desc_parts if p),
            "keywords": "; ".join(keywords(body, prefer=str(it.get("title", "")))),
        })
    by_track: dict[str, list[dict]] = {}
    for r in rows:
        by_track.setdefault(r["track_id"], []).append(r)
    for rs in by_track.values():  # track keywords = what the track's problem statements have in common
        text = " ".join(r["ps_title"] + " " + r["keywords"].replace(";", " ") for r in rs)
        kw = "; ".join(keywords(text, prefer=rs[0]["track_name"], n=10))
        for r in rs:
            r["track_keywords"] = kw
    return rows


COLUMNS = ["track_id", "track_name", "track_description", "track_keywords", "ps_id", "ps_title", "ps_description", "keywords"]


def rows_to_csv(rows: list[dict]) -> bytes:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=COLUMNS)
    w.writeheader()
    w.writerows(rows)
    return buf.getvalue().encode("utf-8-sig")  # -sig: opens correctly in Excel
