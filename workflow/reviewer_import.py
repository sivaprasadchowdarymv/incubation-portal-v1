"""Bulk import of reviewers from an Excel (.xlsx) or CSV file — e.g. the responses sheet of a Google Form.

Columns are recognised by their header words, so a Google Form export works as it is:
  email (required) · full name · username · tracks/expertise · topics/keywords · maximum abstracts · organisation · designation
Tracks are matched to your catalogue by id or name ("AGRI", "Agriculture", "Agri-tech …"); anything unrecognised is kept
as a topic word. New reviewers get a temporary password by email and must choose their own at first sign-in.
Existing reviewers (same email) are updated, never duplicated."""
from __future__ import annotations

import io
import re
import secrets
import string

from database.db import tx
from notifications import service as notify
from workflow.services import EMAIL, WorkflowError, _create_user, audit, authorize

FIELDS = {  # field -> header words (checked in this order; first match wins)
    "email": ["email address", "e-mail", "email", "mail id", "mail"],
    "username": ["username", "user name", "login id", "user id"],
    "name": ["full name", "reviewer name", "your name", "name"],
    "expertise": ["tracks", "track", "domains", "domain", "expertise", "areas you can review", "preferred"],
    "topics": ["topics", "keywords", "specialisation", "specialization", "research interest", "areas of interest", "skills", "subject"],
    "max_load": ["maximum", "max ", "how many", "number of abstracts", "capacity", "load"],
    "organisation": ["organisation", "organization", "institution", "institute", "college", "university", "company", "affiliation"],
    "designation": ["designation", "position", "job title"],
}
COLUMNS = ["Timestamp", "Email address", "Full name", "Organisation", "Designation", "Tracks you can review",
           "Topics / keywords of expertise", "Maximum abstracts you can review"]


def read_table(file_name: str, data: bytes):
    import pandas as pd
    name = file_name.lower()
    try:
        if name.endswith((".xlsx", ".xlsm")):
            df = pd.read_excel(io.BytesIO(data), dtype=str, engine="openpyxl")
        elif name.endswith(".csv"):
            df = pd.read_csv(io.BytesIO(data), dtype=str, encoding="utf-8-sig")
        else:
            raise WorkflowError("Upload an Excel (.xlsx) or CSV file.")
    except WorkflowError:
        raise
    except Exception as exc:
        raise WorkflowError(f"Could not read the file ({type(exc).__name__}). Save it again as .xlsx or .csv.") from None
    return df.fillna("")


def detect_columns(headers: list[str]) -> dict[str, str]:
    mapping: dict[str, str] = {}
    used: set[str] = set()
    low = {h: re.sub(r"\s+", " ", str(h).strip().lower()) for h in headers}
    for field, words in FIELDS.items():
        for w in words:
            hit = next((h for h in headers if h not in used and w in low[h]), None)
            if hit:
                mapping[field] = hit
                used.add(hit)
                break
    if "email" not in mapping:
        raise WorkflowError("No email column found. The sheet needs a column whose header contains 'Email'.")
    return mapping


def _tracks(conn) -> list[dict]:
    return [{"id": r["id"], "name": r["name"], "words": {w for w in re.findall(r"[a-z]{4,}", (r["name"] or "").lower())}}
            for r in conn.execute("SELECT id, name FROM domains")]


def match_tracks(conn, raw: str) -> tuple[list[str], list[str]]:
    """'Agriculture, Health care; T3' -> (['AGRI', 'HLTH', 'T3'], [unrecognised parts])."""
    tracks = _tracks(conn)
    found, unknown = [], []
    for part in [p.strip() for p in re.split(r"[,;/|\n]+", raw or "") if p.strip()]:
        pl = part.lower()
        t = next((t for t in tracks if pl == t["id"].lower() or pl == (t["name"] or "").lower()), None) \
            or next((t for t in tracks if (t["name"] or "").lower() in pl or (len(pl) >= 4 and pl in (t["name"] or "").lower())), None) \
            or next((t for t in tracks if t["words"] & set(re.findall(r"[a-z]{4,}", pl))), None) \
            or next((t for t in tracks if any(w[:4] == tw[:4] for w in re.findall(r"[a-z]{4,}", pl) for tw in t["words"])), None)
        if t and t["id"] not in found:
            found.append(t["id"])
        elif not t:
            unknown.append(part)
    return found, unknown


def _username(conn, email: str, wanted: str, taken: set[str]) -> str:
    base = re.sub(r"[^a-z0-9_.]", "", (wanted or email.split("@")[0]).lower())[:24] or "reviewer"
    if len(base) < 3:
        base = f"rev_{base}"
    u, n = base, 1
    while u in taken or conn.execute("SELECT 1 FROM users WHERE username=?", (u,)).fetchone():
        n += 1
        u = f"{base}{n}"
    taken.add(u)
    return u


def preview(conn, admin, file_name: str, data: bytes) -> dict:
    """Read the sheet and say, row by row, what the import would do. Nothing is saved."""
    authorize(conn, admin, "admin", action="reviewer_import_preview")
    df = read_table(file_name, data)
    if df.empty:
        raise WorkflowError("The sheet has no rows.")
    cols = detect_columns(list(df.columns))
    rows, seen, taken = [], set(), set()
    for i, rec in enumerate(df.to_dict("records"), start=2):
        vals = {f: str(rec.get(col, "") or "").strip() for f, col in cols.items()}

        def get(f, _v=vals):
            return _v.get(f, "")
        email = get("email").lower()
        issues = []
        if not EMAIL.match(email or "x"):
            rows.append({"row": i, "action": "skip", "email": email, "issues": ["missing or invalid email"]})
            continue
        if email in seen:
            rows.append({"row": i, "action": "skip", "email": email, "issues": ["same email appears earlier in the sheet (later answer ignored)"]})
            continue
        seen.add(email)
        exp, unknown = match_tracks(conn, get("expertise"))
        if unknown:
            issues.append(f"not a track, kept as topic: {', '.join(unknown)}")
        try:
            mx = int(float(get("max_load"))) if get("max_load") else 8
        except ValueError:
            mx = 8
            issues.append(f"maximum '{get('max_load')}' is not a number; using 8")
        mx = max(0, min(mx, 100))
        topics = ", ".join([x for x in [get("topics")] + unknown if x])
        ex = conn.execute("SELECT id, username, role FROM users WHERE lower(email)=?", (email,)).fetchone()
        if ex and ex["role"] != "reviewer":
            rows.append({"row": i, "action": "skip", "email": email, "issues": [f"this email belongs to a {ex['role']} account"]})
            continue
        action = "update" if ex else "create"
        uname = ex["username"] if ex else _username(conn, email, get("username"), taken)
        rows.append({"row": i, "action": action, "email": email, "username": uname, "name": get("name")[:120], "expertise": exp,
                     "topics": topics[:500], "max_load": mx, "organisation": get("organisation")[:200], "designation": get("designation")[:120],
                     "user_id": ex["id"] if ex else None, "issues": issues})
    return {"columns": cols, "rows": rows, "create": sum(r["action"] == "create" for r in rows),
            "update": sum(r["action"] == "update" for r in rows), "skip": sum(r["action"] == "skip" for r in rows)}


def temp_password() -> str:
    alphabet = string.ascii_letters + string.digits
    while True:
        p = "".join(secrets.choice(alphabet) for _ in range(12))
        if any(c.isdigit() for c in p) and any(c.isalpha() for c in p):
            return p


def import_reviewers(conn, policy: dict, admin, prev: dict, send_invites: bool = True, portal_url: str = "") -> dict:
    """Apply a preview: create new reviewers (temporary password, must change at first sign-in) and update existing ones."""
    from workflow import evaluation as ev
    authorize(conn, admin, "admin", action="reviewer_import")
    created, updated, creds = 0, 0, []
    for r in prev["rows"]:
        if r["action"] == "skip":
            continue
        with tx(conn):
            if r["action"] == "create":
                pw = temp_password()
                uid = _create_user(conn, r["username"], pw, "reviewer", r["email"])
                conn.execute("UPDATE users SET full_name=?, must_change_password=1 WHERE id=?", (r["name"] or None, uid))
                created += 1
                creds.append({"username": r["username"], "email": r["email"], "temporary_password": pw})
            else:
                uid = r["user_id"]
                if r["name"]:
                    conn.execute("UPDATE users SET full_name=? WHERE id=?", (r["name"], uid))
                updated += 1
        ev.set_profile(conn, admin, uid, r["expertise"], r["max_load"], True, topics=r["topics"], organisation=r["organisation"],
                       designation=r["designation"])
        if r["action"] == "create" and send_invites:
            tnames = {t["id"]: t["name"] for t in _tracks(conn)}
            notify.notify_user(conn, policy, uid, "reviewer_invite", "You are invited as a reviewer",
                               [f"Dear {r['name'] or r['username']},", "",
                                "Thank you for agreeing to review abstracts for our incubation programme. Your reviewer account is ready:",
                                f"  Portal: {portal_url or (policy.get('notifications', {}).get('app_url') or '(ask the organisers)')}",
                                f"  Username: {r['username']}", f"  Temporary password: {creds[-1]['temporary_password']}", "",
                                "You will be asked to choose your own password when you first sign in.",
                                "Tracks recorded for you: " + (", ".join(tnames.get(t, t) for t in r["expertise"]) or "none yet (the organisers will add them)"),
                                "You will receive an email whenever an abstract is assigned to you."], f"invite:{uid}")
    audit(conn, admin, "reviewers_imported", details={"created": created, "updated": updated, "invites": bool(send_invites)})
    return {"created": created, "updated": updated, "skipped": prev["skip"], "credentials": creds}


def template_xlsx() -> bytes:
    """An example sheet with the recommended Google Form questions as columns."""
    import pandas as pd
    df = pd.DataFrame([["2026-11-18 10:00", "anita.rao@example.org", "Dr Anita Rao", "Example Institute of Technology", "Associate Professor",
                        "Agriculture, Health", "irrigation, IoT sensors, crop disease", "5"]], columns=COLUMNS)
    buf = io.BytesIO()
    df.to_excel(buf, index=False)
    return buf.getvalue()


def credentials_csv(creds: list[dict]) -> str:
    import pandas as pd
    return pd.DataFrame(creds).to_csv(index=False) if creds else ""

