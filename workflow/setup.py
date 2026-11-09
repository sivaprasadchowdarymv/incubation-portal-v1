"""Administrator programme setup: tracks & problem statements, abstract template (format),
rule settings and knowledge-base documents - all uploadable from the UI, validated, audited."""
from __future__ import annotations

import copy
import io
import json
import os
import re
from datetime import datetime
from pathlib import Path

import pandas as pd

from database.db import tx
from security.auth import User, safe_filename
from validation import extract
from notifications import service as notify
from workflow.services import WorkflowError, audit, authorize, iso, now_utc


def _admin_ack(conn, policy, title: str, lines: list[str]) -> None:
    """Confirmation of a configuration change to every admin with an email (so changes are never silent)."""
    notify.notify_staff(conn, policy or {}, ("admin",), "setup_ack", title, lines, f"setup:{title}:{now_utc().strftime('%Y%m%d%H%M%S%f')}", force=True)

ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,20}$")
COLUMN_ALIASES = {
    "track_id": ["track_id", "track_code", "domain_id", "domain_code"],
    "track_name": ["track_name", "track", "domain_name", "domain"],
    "track_description": ["track_description", "domain_description"],
    "ps_id": ["ps_id", "ps_code", "problem_statement_id", "ps_no"],
    "ps_title": ["ps_title", "problem_statement", "title", "problem_statement_title"],
    "ps_description": ["ps_description", "description", "problem_statement_description"],
    "keywords": ["keywords", "ps_keywords", "tags"],
    "track_keywords": ["track_keywords", "domain_keywords"],
}
REQUIRED_COLUMNS = ["track_id", "track_name", "ps_id", "ps_title"]
SUBMISSION_FORMATS = {".pdf", ".docx", ".pptx"}
KB_FORMATS = {".pdf", ".docx", ".pptx", ".md", ".txt"}
KB_STATUSES = {"approved", "draft", "retired"}
KB_AUDIENCES = {"student", "reviewer", "admin", "all"}
GENERATED_CATALOGUE = "generated_tracks_and_problem_statements.md"
GENERATED_TEMPLATE = "generated_submission_format.md"

# Settings an administrator may change from the UI (dotted path inside policy) -> validator
EDITABLE = {
    "submission.deadline": "deadline",
    "submission.deadline_inclusive": "bool",
    "submission.max_file_mb": "positive_number",
    "submission.allowed_extensions": "extensions",
    "validation.min_words": "positive_int",
    "validation.max_words": "positive_int",
    "validation.required_sections": "sections",
    "review.approved_rejection_policies": "rejection_policies",
    "template.file": "template",
    "validation.min_words_per_section": "nonneg_int",
    "validation.relevance": "relevance",
    "validation.ps_similarity": "ps_similarity",
    "duplicates": "duplicates",
    "notifications": "notifications",
    "evaluation": "evaluation",
}


# ---------------- effective policy ----------------

def _set_path(d: dict, dotted: str, value) -> None:
    *parents, leaf = dotted.split(".")
    for p in parents:
        d = d.setdefault(p, {})
    d[leaf] = value


def effective_policy(conn, base: dict) -> dict:
    """policy.json overlaid with administrator settings stored in the database."""
    pol = copy.deepcopy(base)
    rows = conn.execute("SELECT key, value FROM programme_settings").fetchall()
    rev = 0
    for r in rows:
        if r["key"] == "_revision":
            rev = int(json.loads(r["value"]))
        elif r["key"] in EDITABLE:
            _set_path(pol, r["key"], json.loads(r["value"]))
    if rev:
        pol["version"] = f"{base['version']}+admin.r{rev}"
    return pol


def _validate_value(kind: str, v):
    if kind == "deadline":
        try:
            datetime.fromisoformat(str(v))
        except ValueError:
            raise WorkflowError("Deadline must look like 2026-11-30T23:59:59") from None
        return str(v)
    if kind == "bool":
        return bool(v)
    if kind == "positive_number":
        if float(v) <= 0:
            raise WorkflowError("Value must be positive")
        return float(v)
    if kind == "positive_int":
        if int(v) < 1:
            raise WorkflowError("Value must be at least 1")
        return int(v)
    if kind == "extensions":
        exts = sorted({("." + e.strip().lower().lstrip(".")) for e in v if e.strip()})
        if not exts or not set(exts) <= SUBMISSION_FORMATS:
            raise WorkflowError(f"Allowed formats must be a non-empty subset of {sorted(SUBMISSION_FORMATS)}")
        return exts
    if kind == "sections":
        if not isinstance(v, dict) or not v:
            raise WorkflowError("At least one required section is needed")
        out = {}
        for name, aliases in v.items():
            name = str(name).strip()
            al = [a.strip().lower() for a in (aliases or []) if a and a.strip()] or [name.lower()]
            if not name or len(name) > 80:
                raise WorkflowError("Section names must be 1-80 characters")
            out[name] = al
        return out
    if kind == "rejection_policies":
        out = []
        for p in v:
            if not ID_RE.match(str(p.get("id", ""))) or not str(p.get("title", "")).strip():
                raise WorkflowError("Each rejection policy needs an id (letters/digits/-/_) and a title")
            out.append({"id": p["id"], "title": p["title"].strip()})
        return out
    if kind == "template":
        return v
    if kind == "nonneg_int":
        if int(v) < 0:
            raise WorkflowError("Value cannot be negative")
        return int(v)
    if kind == "relevance":
        if v.get("action") not in ("review", "fail") or int(v.get("min_keyword_matches", -1)) < 0:
            raise WorkflowError("Relevance needs min_keyword_matches >= 0 and action 'review' or 'fail'")
        return {"min_keyword_matches": int(v["min_keyword_matches"]), "action": v["action"]}
    if kind == "ps_similarity":
        m = float(v.get("margin", -1))
        if v.get("action") not in ("review", "fail") or not 0 <= m <= 1:
            raise WorkflowError("PS similarity needs a margin between 0 and 1 and action 'review' or 'fail'")
        return {"enabled": bool(v.get("enabled", True)), "margin": m, "action": v["action"]}
    if kind == "duplicates":
        nd = v.get("near_duplicate_threshold")
        if v.get("exact_action") not in ("fail", "review") or (nd is not None and not 0.5 < float(nd) <= 1):
            raise WorkflowError("Duplicates need exact_action 'fail' or 'review' and a near-duplicate threshold between 0.5 and 1 (or empty)")
        return {"exact_action": v["exact_action"], "near_duplicate_threshold": None if nd is None else float(nd),
                "block_identical_resubmission": bool(v.get("block_identical_resubmission", True))}
    if kind == "notifications":
        hrs = sorted({int(h) for h in v.get("reminder_hours_before_deadline", [])})
        if any(h <= 0 or h > 24 * 60 for h in hrs):
            raise WorkflowError("Reminder hours must be between 1 and 1440")
        url = str(v.get("app_url", "")).strip()
        if url and not re.match(r"^https?://", url):
            raise WorkflowError("Portal address must start with http:// or https://")
        return {"auto_send": bool(v.get("auto_send", True)), "notify_members": bool(v.get("notify_members", True)),
                "alert_reviewers": bool(v.get("alert_reviewers", True)), "reminder_hours_before_deadline": hrs,
                "review_reminder_hours": max(0, min(int(v.get("review_reminder_hours", 24)), 24 * 14)),
                "app_url": url, "programme_name": str(v.get("programme_name", "Incubation Programme")).strip()[:80] or "Incubation Programme"}
    if kind == "evaluation":
        from workflow.evaluation import clean_evaluation_settings
        return clean_evaluation_settings(v)
    raise WorkflowError(f"Unknown setting kind {kind}")


def update_settings(conn, admin: User, changes: dict, base: dict) -> dict:
    authorize(conn, admin, "admin", action="update_settings")
    clean = {}
    for k, v in changes.items():
        if k not in EDITABLE:
            raise WorkflowError(f"Setting '{k}' cannot be changed from the UI")
        clean[k] = _validate_value(EDITABLE[k], v)
    merged = effective_policy(conn, base)
    for k, v in clean.items():
        _set_path(merged, k, v)
    if merged["validation"]["min_words"] >= merged["validation"]["max_words"]:
        raise WorkflowError("Minimum words must be lower than maximum words")
    with tx(conn):
        for k, v in clean.items():
            conn.execute("INSERT INTO programme_settings(key,value,updated_by,updated_at) VALUES (?,?,?,?) "
                         "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_by=excluded.updated_by, updated_at=excluded.updated_at",
                         (k, json.dumps(v), admin.id, iso(now_utc())))
        row = conn.execute("SELECT value FROM programme_settings WHERE key='_revision'").fetchone()
        rev = (int(json.loads(row["value"])) if row else 0) + 1
        conn.execute("INSERT INTO programme_settings(key,value,updated_by,updated_at) VALUES ('_revision',?,?,?) "
                     "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_by=excluded.updated_by, updated_at=excluded.updated_at",
                     (json.dumps(rev), admin.id, iso(now_utc())))
        audit(conn, admin, "settings_updated", "programme_settings", rev, {"keys": sorted(clean)})
    pol = effective_policy(conn, base)
    _admin_ack(conn, pol, f"Programme settings updated (rules version {pol['version']})",
               [f"{admin.username} changed: {', '.join(sorted(clean))}.", "The new rules apply to submissions made from now on."])
    return pol


# ---------------- tracks & problem statements ----------------

def _norm_col(c: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(c).strip().lower()).strip("_")


def parse_catalogue(filename: str, data: bytes) -> tuple[list[dict], list[str]]:
    """Parse CSV / XLSX / JSON into rows {track_id, track_name, track_description, ps_id, ps_title, ps_description, keywords[]}."""
    ext = Path(filename).suffix.lower()
    if not data:
        return [], ["The file is empty."]
    if len(data) > 5 * 1024 * 1024:
        return [], ["Catalogue file exceeds 5 MB."]
    try:
        if ext == ".json":
            obj = json.loads(data.decode("utf-8"))
            doms = obj["domains"] if isinstance(obj, dict) else obj
            recs = [{"track_id": d.get("id"), "track_name": d.get("name"), "track_description": d.get("description", ""),
                     "track_keywords": d.get("keywords", []),
                     "ps_id": p.get("id"), "ps_title": p.get("title"), "ps_description": p.get("description", ""),
                     "keywords": p.get("keywords", [])} for d in doms for p in d.get("problem_statements", [])]
            df = pd.DataFrame(recs)
        elif ext == ".csv":
            df = pd.read_csv(io.BytesIO(data), dtype=str, keep_default_na=False, encoding_errors="replace")
        elif ext in (".xlsx", ".xls"):
            df = pd.read_excel(io.BytesIO(data), dtype=str, engine="openpyxl").fillna("")
        else:
            return [], [f"Unsupported catalogue format '{ext}'. Use .csv, .xlsx or .json."]
    except Exception as exc:  # malformed upload
        return [], [f"Could not read the catalogue file ({type(exc).__name__})."]
    rename = {}
    normed = {_norm_col(c): c for c in df.columns}
    for target, aliases in COLUMN_ALIASES.items():
        for a in aliases:
            if a in normed:
                rename[normed[a]] = target
                break
    df = df.rename(columns=rename)
    missing = [c for c in REQUIRED_COLUMNS if c not in df.columns]
    if missing:
        return [], [f"Missing required column(s): {', '.join(missing)}. Required: {', '.join(REQUIRED_COLUMNS)}."]
    if len(df) > 2000:
        return [], ["Too many rows (max 2000)."]
    rows, errors, ps_seen, track_names = [], [], {}, {}
    for i, r in enumerate(df.to_dict("records"), start=2):
        g = lambda k: str(r.get(k, "") if r.get(k, "") is not None else "").strip()  # noqa: B023
        tid, tname, pid, ptitle = g("track_id"), g("track_name"), g("ps_id"), g("ps_title")
        if not any((tid, tname, pid, ptitle)):
            continue  # blank line
        kw = r.get("keywords", "")
        kws = [k.strip().lower() for k in (kw if isinstance(kw, list) else re.split(r"[;,|]", str(kw or ""))) if k and k.strip()]
        if not ID_RE.match(tid):
            errors.append(f"Row {i}: track_id '{tid}' must be 1-20 letters/digits/-/_")
        if not ID_RE.match(pid):
            errors.append(f"Row {i}: ps_id '{pid}' must be 1-20 letters/digits/-/_")
        if not tname or not ptitle:
            errors.append(f"Row {i}: track_name and ps_title are required")
        if pid in ps_seen:
            errors.append(f"Row {i}: duplicate ps_id '{pid}' (first seen in row {ps_seen[pid]})")
        ps_seen.setdefault(pid, i)
        if tid in track_names and track_names[tid] != tname:
            errors.append(f"Row {i}: track_id '{tid}' has two different names ('{track_names[tid]}' / '{tname}')")
        track_names.setdefault(tid, tname)
        if not kws:
            kws = [w for w in re.findall(r"[a-z]{4,}", (ptitle + " " + g("ps_description")).lower())][:8]
        tk = r.get("track_keywords", "")
        tkws = [k.strip().lower() for k in (tk if isinstance(tk, list) else re.split(r"[;,|]", str(tk or ""))) if k and k.strip()]
        rows.append({"track_id": tid, "track_name": tname, "track_description": g("track_description"), "ps_id": pid,
                     "ps_title": ptitle, "ps_description": g("ps_description"), "keywords": kws, "track_keywords": tkws})
    if not rows and not errors:
        errors.append("No problem statements found in the file.")
    return rows, errors


def import_catalogue(conn, admin: User, filename: str, data: bytes, replace: bool = True, kb_dir: Path | None = None) -> dict:
    """Upsert tracks/PS. With replace=True, entries missing from the file are deactivated (hidden from new selections);
    teams that already locked a PS keep it."""
    authorize(conn, admin, "admin", action="import_catalogue")
    rows, errors = parse_catalogue(safe_filename(filename), data)
    if errors:
        raise WorkflowError("Catalogue rejected:\n" + "\n".join(errors[:15]) + (f"\n… {len(errors) - 15} more" if len(errors) > 15 else ""))
    for r in rows:
        ex = conn.execute("SELECT domain_id FROM problem_statements WHERE id=?", (r["ps_id"],)).fetchone()
        if ex and ex["domain_id"] != r["track_id"]:
            locked = conn.execute("SELECT COUNT(*) FROM teams WHERE ps_id=?", (r["ps_id"],)).fetchone()[0]
            if locked:
                raise WorkflowError(f"Catalogue rejected: {r['ps_id']} is already locked by {locked} team(s) under track "
                                    f"{ex['domain_id']}; it cannot move to {r['track_id']}. Use a new ps_id instead.")
    tracks: dict[str, dict] = {}
    for r in rows:  # merge track keywords from every row of the track
        t = tracks.setdefault(r["track_id"], dict(r, track_keywords=[]))
        t["track_keywords"] = list(dict.fromkeys(t["track_keywords"] + r["track_keywords"]))
        if r["track_description"] and not t["track_description"]:
            t["track_description"] = r["track_description"]
    with tx(conn):
        for t in tracks.values():
            conn.execute("INSERT INTO domains(id,name,description,keywords,active) VALUES (?,?,?,?,1) ON CONFLICT(id) DO UPDATE SET "
                         "name=excluded.name, description=CASE WHEN excluded.description='' THEN domains.description ELSE excluded.description END, "
                         "keywords=CASE WHEN excluded.keywords='[]' THEN domains.keywords ELSE excluded.keywords END, active=1",
                         (t["track_id"], t["track_name"], t["track_description"], json.dumps(t["track_keywords"])))
        for r in rows:
            conn.execute("INSERT INTO problem_statements(id,domain_id,title,description,keywords,active) VALUES (?,?,?,?,?,1) "
                         "ON CONFLICT(id) DO UPDATE SET domain_id=excluded.domain_id, title=excluded.title, description=excluded.description, keywords=excluded.keywords, active=1",
                         (r["ps_id"], r["track_id"], r["ps_title"], r["ps_description"], json.dumps(r["keywords"])))
        deact_ps = deact_tr = 0
        if replace:
            keep_ps, keep_tr = [r["ps_id"] for r in rows], list(tracks)
            deact_ps = conn.execute(f"UPDATE problem_statements SET active=0 WHERE active=1 AND id NOT IN ({','.join('?' * len(keep_ps))})", keep_ps).rowcount
            deact_tr = conn.execute(f"UPDATE domains SET active=0 WHERE active=1 AND id NOT IN ({','.join('?' * len(keep_tr))})", keep_tr).rowcount
        summary = {"tracks": len(tracks), "problem_statements": len(rows), "deactivated_problem_statements": deact_ps,
                   "deactivated_tracks": deact_tr, "mode": "replace" if replace else "merge"}
        audit(conn, admin, "catalogue_imported", "catalogue", None, summary | {"file": safe_filename(filename)})
    _admin_ack(conn, None, "Tracks and problem statements imported",
               [f"{admin.username} imported '{safe_filename(filename)}' ({summary['mode']}): {summary['tracks']} track(s), {summary['problem_statements']} problem statement(s).",
                f"Hidden from new selections: {summary['deactivated_problem_statements']} problem statement(s), {summary['deactivated_tracks']} track(s).",
                "Remember to rebuild the knowledge-base index so the assistant knows the new catalogue."])
    if kb_dir:
        publish_catalogue_doc(conn, kb_dir)
    return summary


def catalogue_dataframe(conn, include_inactive: bool = True) -> pd.DataFrame:
    sql = ("SELECT d.id AS track_id, d.name AS track_name, d.description AS track_description, d.keywords AS track_keywords, p.id AS ps_id, p.title AS ps_title, "
           "p.description AS ps_description, p.keywords, p.active AS ps_active, "
           "(SELECT COUNT(*) FROM teams t WHERE t.ps_id=p.id) AS teams_locked FROM problem_statements p JOIN domains d ON d.id=p.domain_id")
    if not include_inactive:
        sql += " WHERE p.active=1 AND d.active=1"
    df = pd.read_sql_query(sql + " ORDER BY d.id, p.id", conn)
    df["keywords"] = df["keywords"].map(lambda k: "; ".join(json.loads(k)))
    df["track_keywords"] = df["track_keywords"].map(lambda k: "; ".join(json.loads(k or "[]")))
    return df


def catalogue_template_csv() -> bytes:
    return ("track_id,track_name,track_description,track_keywords,ps_id,ps_title,ps_description,keywords\n"
            "AGRI,Agritech,Technology for farming,agriculture; farm; farmer; crop; soil; yield,AG-01,Precision irrigation scheduling,"
            "Reduce water use on small farms,irrigation; soil moisture; water; sensor\n"
            "AGRI,Agritech,Technology for farming,,AG-02,Early crop disease detection,Detect leaf diseases early,crop; disease; leaf; image\n"
            "HLTH,Healthcare,Accessible healthcare,health; patient; clinic; hospital; doctor,HC-01,Rural telemedicine triage,"
            "Prioritise rural patients,patient; rural; telemedicine; triage\n").encode()


# ---------------- abstract template (format) ----------------

def detect_template_sections(filename: str, data: bytes) -> list[str]:
    """Headings of the uploaded template: DOCX heading styles, PPTX slide titles, otherwise short heading-like lines."""
    ex = extract.extract(safe_filename(filename), data)
    if ex.error:
        raise WorkflowError(f"Template could not be read: {ex.error}")
    heads = [u["section"] for u in ex.units if u.get("section")]
    if not heads:
        for ln in ex.text.splitlines():
            t = re.sub(r"^[\s#*\d.):-]+", "", ln).strip().rstrip(":")
            if t and len(t.split()) <= 8 and not t.endswith(".") and (ln.strip().endswith(":") or t.istitle() or t.isupper()):
                heads.append(t)
    seen, out = set(), []
    for h in heads:
        h = re.sub(r"^[\s#*\d.):-]+", "", h).strip().rstrip(":")
        if h and h.lower() not in seen and len(h) <= 80:
            seen.add(h.lower())
            out.append(h)
    return out


def sections_from_text(text: str) -> dict[str, list[str]]:
    """Editor format, one per line:  Section Name | alias one, alias two"""
    out = {}
    for ln in text.splitlines():
        if not ln.strip():
            continue
        name, _, al = ln.partition("|")
        out[name.strip()] = [a.strip().lower() for a in al.split(",") if a.strip()] or [name.strip().lower()]
    return out


def sections_to_text(sections: dict[str, list[str]]) -> str:
    return "\n".join(f"{n} | {', '.join(a)}" for n, a in sections.items())


def save_template(conn, admin: User, base: dict, storage_dir: Path, filename: str, data: bytes, required_sections: dict,
                  min_words: int, max_words: int, allowed_extensions: list[str], kb_dir: Path | None = None) -> dict:
    authorize(conn, admin, "admin", action="save_template")
    safe = safe_filename(filename)
    if Path(safe).suffix.lower() not in SUBMISSION_FORMATS:
        raise WorkflowError("Template must be a .docx, .pptx or .pdf file")
    pre = extract.precheck(safe, data, {"submission": {"allowed_extensions": sorted(SUBMISSION_FORMATS), "max_file_mb": 20, "max_uncompressed_mb": 100}})
    if not pre.ok:
        raise WorkflowError(f"Template rejected: {pre.message}")
    folder = Path(storage_dir) / "templates"
    folder.mkdir(parents=True, exist_ok=True)
    stamp = now_utc().strftime("%Y%m%dT%H%M%S")
    path = folder / f"{stamp}_{safe}"
    path.write_bytes(data)
    import hashlib

    meta = {"name": safe, "path": str(path), "sha256": hashlib.sha256(data).hexdigest(), "uploaded_at": iso(now_utc())}
    pol = update_settings(conn, admin, {"template.file": meta, "validation.required_sections": required_sections,
                                        "validation.min_words": min_words, "validation.max_words": max_words,
                                        "submission.allowed_extensions": allowed_extensions}, base)
    if kb_dir:
        publish_template_doc(pol, kb_dir)
    return pol


def template_file(policy: dict) -> tuple[str, bytes] | None:
    """The stored template. Works even if the project folder was moved (or Windows paths are read inside Docker):
    when the saved full path no longer exists, the file is looked up in the current storage/templates folder."""
    t = policy.get("template", {}).get("file")
    if not t:
        return None
    saved = str(t.get("path", ""))
    candidates = [Path(saved)]
    from config.settings import get_settings

    candidates.append(Path(get_settings().storage_dir) / "templates" / Path(saved.replace("\\", "/")).name)
    for p in candidates:
        if saved and p.is_file():
            return t["name"], p.read_bytes()
    return None


# ---------------- knowledge base ----------------

def _manifest(kb_dir: Path) -> dict:
    p = Path(kb_dir) / "manifest.json"
    return json.loads(p.read_text()) if p.exists() else {"documents": []}


def _write_manifest(kb_dir: Path, man: dict) -> None:
    p = Path(kb_dir) / "manifest.json"
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(man, indent=1))
    os.replace(tmp, p)


def _upsert_manifest(kb_dir: Path, entry: dict) -> None:
    man = _manifest(kb_dir)
    man["documents"] = [e for e in man["documents"] if e["file"] != entry["file"]] + [entry]
    _write_manifest(kb_dir, man)


def add_kb_document(conn, admin: User, kb_dir: Path, filename: str, data: bytes, version: str, doc_type: str,
                    status: str = "approved", audience: str = "student") -> dict:
    authorize(conn, admin, "admin", action="add_kb_document")
    safe = safe_filename(filename)
    if Path(safe).suffix.lower() not in KB_FORMATS:
        raise WorkflowError(f"Knowledge-base files must be one of {sorted(KB_FORMATS)}")
    if safe.startswith("generated_") or safe == "manifest.json":
        raise WorkflowError("That file name is reserved")
    if status not in KB_STATUSES or audience not in KB_AUDIENCES:
        raise WorkflowError("Invalid status or audience")
    if not data or len(data) > 20 * 1024 * 1024:
        raise WorkflowError("File is empty or larger than 20 MB")
    ex = extract.extract(safe, data)
    if ex.error or not ex.text.strip():
        raise WorkflowError(f"No readable text in the document ({ex.error or 'empty'}); scanned PDFs need OCR first.")
    kb_dir = Path(kb_dir)
    dest = kb_dir / safe
    if dest.exists():  # keep the previous version
        arch = kb_dir / "_archive"
        arch.mkdir(exist_ok=True)
        os.replace(dest, arch / f"{now_utc().strftime('%Y%m%dT%H%M%S')}_{safe}")
    dest.write_bytes(data)
    entry = {"file": safe, "version": (version or "1.0").strip()[:20], "doc_type": (doc_type or "other").strip()[:40],
             "status": status, "audience": audience}
    _upsert_manifest(kb_dir, entry)
    audit(conn, admin, "kb_document_added", "kb", safe, entry)
    _admin_ack(conn, None, f"Knowledge-base document added: {safe}",
               [f"{admin.username} added '{safe}' (version {entry['version']}, {entry['status']}, visible to {entry['audience']}).", "Rebuild the index to make it searchable."])
    return entry


def set_kb_status(conn, admin: User, kb_dir: Path, file: str, status: str) -> None:
    authorize(conn, admin, "admin", action="set_kb_status")
    if status not in KB_STATUSES:
        raise WorkflowError("Invalid status")
    man = _manifest(kb_dir)
    for e in man["documents"]:
        if e["file"] == file:
            e["status"] = status
            _write_manifest(kb_dir, man)
            audit(conn, admin, "kb_status_changed", "kb", file, {"status": status})
            _admin_ack(conn, None, f"Knowledge-base document {status}: {file}", [f"{admin.username} set '{file}' to {status}.", "Rebuild the index to apply."])
            return
    raise WorkflowError("Document not found in manifest")


def kb_documents(kb_dir: Path) -> list[dict]:
    return _manifest(kb_dir)["documents"]


def publish_catalogue_doc(conn, kb_dir: Path) -> None:
    """Write the active catalogue as an approved KB document so the assistant can answer about tracks/PS."""
    lines = ["# Tracks and Problem Statements", "Generated from the administrator-approved catalogue.", ""]
    for d in conn.execute("SELECT * FROM domains WHERE active=1 ORDER BY id"):
        lines.append(f"# Track {d['id']}: {d['name']}")
        if d["description"]:
            lines.append(d["description"])
        if json.loads(d["keywords"] or "[]"):
            lines.append("Track keywords: " + ", ".join(json.loads(d["keywords"])) + ".")
        for p in conn.execute("SELECT * FROM problem_statements WHERE domain_id=? AND active=1 ORDER BY id", (d["id"],)):
            lines.append(f"Problem statement {p['id']} ({d['name']} track): {p['title']}. {p['description'] or ''}")
        lines.append("")
    (Path(kb_dir) / GENERATED_CATALOGUE).write_text("\n".join(lines), encoding="utf-8")
    _upsert_manifest(kb_dir, {"file": GENERATED_CATALOGUE, "version": iso(now_utc())[:10], "doc_type": "domains_problem_statements",
                              "status": "approved", "audience": "student"})


def publish_template_doc(policy: dict, kb_dir: Path) -> None:
    v, s = policy["validation"], policy["submission"]
    secs = ", ".join(v["required_sections"])
    text = (f"# Abstract Submission Format\nThe abstract must contain these section headings: {secs}.\n"
            f"The abstract must contain at least {v['min_words']} words and no more than {v['max_words']} words.\n"
            f"Accepted file formats are {', '.join(e.strip('.').upper() for e in s['allowed_extensions'])}, with a maximum size of {s['max_file_mb']} MB.\n"
            + (f"The official template file is {policy['template']['file']['name']}, available on the Submit page.\n" if policy.get("template", {}).get("file") else ""))
    (Path(kb_dir) / GENERATED_TEMPLATE).write_text(text, encoding="utf-8")
    _upsert_manifest(kb_dir, {"file": GENERATED_TEMPLATE, "version": policy["version"], "doc_type": "submission_template",
                              "status": "approved", "audience": "student"})


EXAMPLE_TEMPLATE_GUIDANCE = [
    ("Problem Statement / Motivation", "Describe the problem, who is affected, and why it matters now. Replace this guidance with your text."),
    ("Impact", "Explain who benefits, how much, and how you will measure the impact."),
    ("Methodology", "Describe how your solution works: approach, technology, data and the main steps."),
    ("Deliverables / Outcomes", "List what you will deliver at the end of the programme (prototype, report, pilot results)."),
]


def example_template_docx() -> bytes:
    """Example abstract template generated on demand (keeps binary files out of the repository)."""
    from evaluation.synthetic import make_docx

    return make_docx(EXAMPLE_TEMPLATE_GUIDANCE)
