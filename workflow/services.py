"""Application services: users, registration, PS lock, submissions, review, audit, deadlines.

All business rules are enforced here (never only in the UI)."""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
import sqlite3
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import agents
from database.db import tx
from evaluation import metrics
from notifications import service as notify
from security.auth import AuthError, User, hash_password, require_role, safe_filename, verify_password
from validation import checks, extract
from workflow import states

log = logging.getLogger("incubation")
EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class WorkflowError(ValueError):
    pass


class SubmissionBlocked(WorkflowError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


# ---------------- audit & authz ----------------

def audit(conn, user: User | None, action: str, entity_type: str | None = None, entity_id=None, details: dict | None = None) -> None:
    conn.execute(
        "INSERT INTO audit_events(actor_id,actor_role,action,entity_type,entity_id,details,at) VALUES (?,?,?,?,?,?,?)",
        (user.id if user else None, user.role if user else "system", action, entity_type,
         str(entity_id) if entity_id is not None else None, json.dumps(details or {}), iso(now_utc())),
    )


def authorize(conn, user: User | None, *roles: str, action: str = "") -> User:
    """Role check against the database, so a deactivated account or a changed role takes effect immediately."""
    try:
        if user is not None:
            r = conn.execute("SELECT role, active FROM users WHERE id=?", (user.id,)).fetchone()
            if not r or not r["active"]:
                raise AuthError("This account is not active. Contact the incubation office.")
            if r["role"] != user.role:
                user = User(user.id, user.username, r["role"], user.team_id)
        return require_role(user, *roles)
    except AuthError:
        if not conn.in_transaction:
            audit(conn, user, "authorization_denied", details={"action": action, "required": roles})
        raise


# ---------------- catalogue / policy ----------------

def sync_catalog(conn, policy: dict) -> None:
    """Record the policy version; seed domains/PS from policy.json only into an empty catalogue."""
    content = json.dumps({k: v for k, v in policy.items() if not k.startswith("_")}, sort_keys=True)
    with tx(conn):
        conn.execute("INSERT OR IGNORE INTO policy_versions(version,sha256,content,loaded_at) VALUES (?,?,?,?)",
                     (policy["version"], policy["_sha256"], content, iso(now_utc())))
        if conn.execute("SELECT COUNT(*) FROM domains").fetchone()[0]:
            return  # catalogue already present (seeded or uploaded by an admin): the database is authoritative
        for d in policy["domains"]:
            conn.execute("INSERT INTO domains(id,name,description,keywords,active) VALUES (?,?,?,?,1) "
                         "ON CONFLICT(id) DO UPDATE SET name=excluded.name, description=excluded.description, keywords=excluded.keywords, active=1",
                         (d["id"], d["name"], d.get("description", ""), json.dumps(d.get("keywords", []))))
            for ps in d["problem_statements"]:
                conn.execute("INSERT INTO problem_statements(id,domain_id,title,description,keywords,active) VALUES (?,?,?,?,?,1) "
                             "ON CONFLICT(id) DO UPDATE SET title=excluded.title, description=excluded.description, keywords=excluded.keywords, active=1",
                             (ps["id"], d["id"], ps["title"], ps.get("description", ""), json.dumps(ps.get("keywords", []))))


def catalog(conn) -> list[dict]:
    out = []
    for d in conn.execute("SELECT * FROM domains WHERE active=1 ORDER BY id"):
        pss = [dict(p) | {"keywords": json.loads(p["keywords"])} for p in conn.execute("SELECT * FROM problem_statements WHERE domain_id=? AND active=1 ORDER BY id", (d["id"],))]
        out.append(dict(d) | {"keywords": json.loads(d["keywords"]), "problem_statements": pss})
    return out


# ---------------- users ----------------

def _create_user(conn, username: str, password: str, role: str, email: str | None = None) -> int:
    username = (username or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_.-]{3,40}", username):
        raise WorkflowError("Username must be 3-40 characters: letters, digits, _ . -")
    email = (email or "").strip() or None
    if email and not EMAIL.match(email):
        raise WorkflowError("Invalid email address.")
    try:
        cur = conn.execute("INSERT INTO users(username,pw_hash,role,email,created_at) VALUES (?,?,?,?,?)",
                           (username, hash_password(password), role, email, iso(now_utc())))
    except sqlite3.IntegrityError:
        raise WorkflowError("Username already exists") from None
    return cur.lastrowid


def signup_student(conn, username: str, password: str, email: str | None = None, policy: dict | None = None,
                   access_code: str | None = None, required_code: str | None = None) -> int:
    """required_code (SIGNUP_CODE setting) stops strangers on the internet from creating accounts."""
    if required_code and not hmac.compare_digest((access_code or "").strip().encode(), required_code.strip().encode()):
        audit(conn, None, "signup_rejected_code", "user", None, {"username": (username or "")[:40]})
        raise WorkflowError("Invalid sign-up code. Ask the incubation office for the code.")
    with tx(conn):
        uid = _create_user(conn, username, password, "student", email)
        audit(conn, None, "student_signup", "user", uid, {"has_email": bool(email)})
    notify.notify_user(conn, policy, uid, "signup_ack", "Your student account was created",
                       [f"Your account '{username.strip()}' has been created.", "Next steps: sign in, register your team (membership is locked after registration), "
                        "then select and lock your track and problem statement.", "If you did not create this account, contact the incubation office."],
                       f"signup:{uid}")
    return uid


def admin_create_user(conn, admin: User, username: str, password: str, role: str, email: str | None = None, policy: dict | None = None) -> int:
    authorize(conn, admin, "admin", action="create_user")
    if role not in ("student", "reviewer", "admin"):
        raise WorkflowError("Invalid role")
    with tx(conn):
        uid = _create_user(conn, username, password, role, email)
        audit(conn, admin, "user_created", "user", uid, {"role": role, "has_email": bool(email)})
    notify.notify_user(conn, policy, uid, "account_created", f"Your {role} account was created",
                       [f"An administrator created a {role} account for you with username '{username.strip()}'.",
                        "Your temporary password will be given to you separately; please sign in and keep it private."]
                       + (["You will receive alerts and reminders about review cases at this address."] if role in ("reviewer", "admin") else []),
                       f"created:{uid}")
    return uid


def set_user_email(conn, admin: User, user_id: int, email: str | None, policy: dict | None = None) -> None:
    authorize(conn, admin, "admin", action="set_user_email")
    email = (email or "").strip() or None
    if email and not EMAIL.match(email):
        raise WorkflowError("Invalid email address.")
    with tx(conn):
        if not conn.execute("UPDATE users SET email=? WHERE id=?", (email, user_id)).rowcount:
            raise WorkflowError("User not found")
        audit(conn, admin, "user_email_set", "user", user_id, {"has_email": bool(email)})
    if email:
        notify.notify_user(conn, policy, user_id, "email_confirmed", "Email address registered for notifications",
                           ["This address will now receive notifications from the incubation portal.",
                            "If you did not expect this, contact the incubation office."], f"emailset:{user_id}:{now_utc().strftime('%Y%m%d%H%M%S')}")


def must_change_password(conn, user: User) -> bool:
    r = conn.execute("SELECT must_change_password FROM users WHERE id=?", (user.id,)).fetchone()
    return bool(r and r["must_change_password"])


def change_password(conn, user: User, current: str, new: str, confirm: str | None = None) -> None:
    """Any signed-in user changes their own password (required at first sign-in for imported reviewers)."""
    r = conn.execute("SELECT pw_hash FROM users WHERE id=? AND active=1", (user.id,)).fetchone()
    if not r or not verify_password(current or "", r["pw_hash"]):
        raise WorkflowError("Your current password is not correct.")
    if confirm is not None and new != confirm:
        raise WorkflowError("The two new passwords do not match.")
    if new == current:
        raise WorkflowError("Choose a password different from the current one.")
    try:
        h = hash_password(new)
    except ValueError as exc:
        raise WorkflowError(str(exc)) from None
    conn.execute("UPDATE users SET pw_hash=?, must_change_password=0 WHERE id=?", (h, user.id))
    audit(conn, user, "password_changed", "user", user.id)
    notify.notify_user(conn, None, user.id, "password_changed", "Your password was changed",
                       ["Your portal password was changed just now.", "If this was not you, contact the incubation office immediately."],
                       f"pwchg:{user.id}:{now_utc().strftime('%Y%m%d%H%M%S%f')}")


def list_users(conn, admin: User, search: str = "", role: str = "") -> list[dict]:
    authorize(conn, admin, "admin", action="list_users")
    sql = ("SELECT u.id, u.username, u.role, u.email, u.active, u.created_at, t.team_code, "
           "(SELECT MAX(at) FROM audit_events a WHERE a.actor_id=u.id AND a.action='login_success') AS last_login "
           "FROM users u LEFT JOIN teams t ON t.id=u.team_id WHERE 1=1")
    args: list = []
    if role:
        sql += " AND u.role=?"
        args.append(role)
    if search:
        sql += " AND (u.username LIKE ? OR u.email LIKE ? OR t.team_code LIKE ?)"
        args += [f"%{search}%"] * 3
    return [dict(r) for r in conn.execute(sql + " ORDER BY u.role, u.username", args)]


def _active_admins(conn) -> int:
    return conn.execute("SELECT COUNT(*) FROM users WHERE role='admin' AND active=1").fetchone()[0]


def update_user(conn, policy: dict | None, admin: User, user_id: int, *, email: str | None = None, role: str | None = None,
                active: bool | None = None, new_password: str | None = None) -> dict:
    """Edit an account. Safety rules: you cannot demote or deactivate yourself, the last active admin stays,
    students who belong to a team keep the student role, reviewers with open assignments keep the reviewer role."""
    authorize(conn, admin, "admin", action="update_user")
    u = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    if not u:
        raise WorkflowError("User not found")
    changes: dict = {}
    if email is not None:
        email = email.strip() or None
        if email and not EMAIL.match(email):
            raise WorkflowError("Invalid email address.")
        if email != u["email"]:
            changes["email"] = email
    if role is not None and role != u["role"]:
        if role not in ("student", "reviewer", "admin"):
            raise WorkflowError("Invalid role")
        if user_id == admin.id:
            raise WorkflowError("You cannot change your own role.")
        if u["role"] == "admin" and u["active"] and _active_admins(conn) <= 1:
            raise WorkflowError("This is the last active admin; create another admin first.")
        if u["team_id"]:
            raise WorkflowError("This student belongs to a team, so the role cannot be changed.")
        if u["role"] == "reviewer" and conn.execute("SELECT 1 FROM review_assignments WHERE reviewer_id=? AND status IN ('assigned','accepted')", (user_id,)).fetchone():
            raise WorkflowError("This reviewer has open assignments; withdraw them first (🎯 Peer Review → Progress).")
        changes["role"] = role
    if active is not None and bool(active) != bool(u["active"]):
        if not active:
            if user_id == admin.id:
                raise WorkflowError("You cannot deactivate your own account.")
            if u["role"] == "admin" and _active_admins(conn) <= 1:
                raise WorkflowError("This is the last active admin; create another admin first.")
        changes["active"] = int(bool(active))
    if new_password:
        changes["must_change_password"] = 1          # the person chooses their own password at next sign-in
        try:
            changes["pw_hash"] = hash_password(new_password)
        except ValueError as exc:
            raise WorkflowError(str(exc)) from None
    if not changes:
        return {"changed": []}
    with tx(conn):
        conn.execute(f"UPDATE users SET {', '.join(k + '=?' for k in changes)} WHERE id=?", (*changes.values(), user_id))
        if changes.get("active") == 0 and u["role"] == "reviewer":
            conn.execute("UPDATE review_assignments SET status='withdrawn', note='reviewer deactivated', responded_at=? "
                         "WHERE reviewer_id=? AND status IN ('assigned','accepted')", (iso(now_utc()), user_id))
        audit(conn, admin, "user_updated", "user", user_id, {k: ("***" if k == "pw_hash" else v) for k, v in changes.items()})
    changes.pop("must_change_password", None)
    said = {"email": "your email address was updated", "role": f"your role is now {changes.get('role')}",
            "active": "your account was " + ("re-activated" if changes.get("active") else "deactivated"),
            "pw_hash": "your password was reset by an administrator (you will receive the new password separately)"}
    notify.notify_user(conn, policy, user_id, "account_updated", "Your portal account was updated",
                       ["An administrator changed your account:"] + [f"  • {said[k]}" for k in changes] +
                       ["", "If you did not expect this, contact the incubation office."], f"userupd:{user_id}:{now_utc().strftime('%Y%m%d%H%M%S%f')}")
    return {"changed": [k.replace("pw_hash", "password") for k in changes]}


def delete_user(conn, admin: User, user_id: int) -> None:
    """Remove an account that has no history. Accounts with a team, submissions, reviews or queries are deactivated instead,
    so that every record keeps its author."""
    authorize(conn, admin, "admin", action="delete_user")
    u = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    if not u:
        raise WorkflowError("User not found")
    if user_id == admin.id:
        raise WorkflowError("You cannot delete your own account.")
    if u["role"] == "admin" and u["active"] and _active_admins(conn) <= 1:
        raise WorkflowError("This is the last active admin; create another admin first.")
    used = u["team_id"] or any(conn.execute(q, (user_id,)).fetchone() for q in (
        "SELECT 1 FROM teams WHERE created_by=?", "SELECT 1 FROM submissions WHERE submitted_by=?",
        "SELECT 1 FROM review_assignments WHERE reviewer_id=?", "SELECT 1 FROM review_assignments WHERE assigned_by=?",
        "SELECT 1 FROM peer_reviews WHERE reviewer_id=?", "SELECT 1 FROM review_decisions WHERE reviewer_id=?",
        "SELECT 1 FROM evaluation_decisions WHERE decided_by=?", "SELECT 1 FROM queries WHERE user_id=?"))
    if used:
        raise WorkflowError(f"'{u['username']}' has a team, submissions, reviews or queries, so the account cannot be deleted. "
                            "Deactivate it instead: the person can no longer sign in and all records are kept.")
    try:
        with tx(conn):
            conn.execute("DELETE FROM reviewer_profiles WHERE user_id=?", (user_id,))
            conn.execute("DELETE FROM reviewer_conflicts WHERE reviewer_id=?", (user_id,))
            conn.execute("DELETE FROM users WHERE id=?", (user_id,))
            audit(conn, admin, "user_deleted", "user", user_id, {"username": u["username"], "role": u["role"]})
    except sqlite3.IntegrityError:
        raise WorkflowError(f"'{u['username']}' is linked to other records; deactivate the account instead.") from None


def bootstrap_admin(conn, username: str, password: str) -> int | None:
    if conn.execute("SELECT 1 FROM users WHERE role='admin'").fetchone():
        return None
    with tx(conn):
        uid = _create_user(conn, username, password, "admin")
        audit(conn, None, "admin_bootstrapped", "user", uid)
    return uid


LOCKOUT_FAILURES, LOCKOUT_MINUTES = 5, 15


class LoginLocked(WorkflowError):
    pass


def _recent_failures(conn, username: str) -> int:
    """Failed logins for this username in the lockout window, counted since its last successful login."""
    since = iso(now_utc() - timedelta(minutes=LOCKOUT_MINUTES))
    last_ok = conn.execute("SELECT MAX(a.at) FROM audit_events a JOIN users u ON u.id=a.actor_id WHERE a.action='login_success' AND lower(u.username)=?",
                           (username,)).fetchone()[0]
    if last_ok and last_ok > since:
        since = last_ok
    return conn.execute("SELECT COUNT(*) FROM audit_events WHERE action='login_failed' AND lower(json_extract(details,'$.username'))=? AND at>=?",
                        (username, since)).fetchone()[0]


def authenticate(conn, username: str, password: str) -> User | None:
    """Returns the user, or None for wrong credentials. Raises LoginLocked after repeated failures (brute-force protection)."""
    key = (username or "").strip()[:40].lower()
    if key and _recent_failures(conn, key) >= LOCKOUT_FAILURES:
        audit(conn, None, "login_locked", "user", None, {"username": key})
        raise LoginLocked(f"Too many failed sign-in attempts for this account. Try again in {LOCKOUT_MINUTES} minutes.")
    r = conn.execute("SELECT * FROM users WHERE username=? AND active=1", ((username or "").strip(),)).fetchone()
    if r and verify_password(password, r["pw_hash"]):
        u = User(r["id"], r["username"], r["role"], r["team_id"])
        audit(conn, u, "login_success", "user", u.id)
        return u
    audit(conn, None, "login_failed", "user", None, {"username": (username or "")[:40]})
    return None


def refresh_user(conn, user: User) -> User:
    r = conn.execute("SELECT * FROM users WHERE id=?", (user.id,)).fetchone()
    return User(r["id"], r["username"], r["role"], r["team_id"])


# ---------------- teams ----------------

def register_team(conn, policy: dict, user: User, name: str, contact_email: str, members: list[dict]) -> int:
    authorize(conn, user, "student", action="register_team")
    user = refresh_user(conn, user)
    if user.team_id:
        raise WorkflowError("You are already registered in a team; membership is locked.")
    lo, hi = policy["team"]["min_members"], policy["team"]["max_members"]
    if not lo <= len(members) <= hi:
        raise WorkflowError(f"A team must have {lo}-{hi} members (got {len(members)}).")
    name = (name or "").strip()
    if not 3 <= len(name) <= 60:
        raise WorkflowError("Team name must be 3-60 characters.")
    if not EMAIL.match(contact_email or ""):
        raise WorkflowError("A valid contact email is required.")
    refs = []
    for m in members:
        if not (m.get("full_name") or "").strip() or not (m.get("member_ref") or "").strip():
            raise WorkflowError("Every member needs a name and a member ID (e.g. roll number).")
        if m.get("email") and not EMAIL.match(m["email"]):
            raise WorkflowError(f"Invalid email for {m['full_name']}.")
        refs.append(m["member_ref"].strip().lower())
    if len(set(refs)) != len(refs):
        raise WorkflowError("Duplicate member IDs in the team.")
    code = "T-" + uuid.uuid4().hex[:8].upper()
    try:
        with tx(conn):
            cur = conn.execute("INSERT INTO teams(team_code,name,contact_email,created_by,membership_locked,created_at) VALUES (?,?,?,?,0,?)",
                               (code, name, contact_email.strip(), user.id, iso(now_utc())))
            tid = cur.lastrowid
            for m in members:
                conn.execute("INSERT INTO team_members(team_id,full_name,member_ref,email) VALUES (?,?,?,?)",
                             (tid, m["full_name"].strip(), m["member_ref"].strip(), (m.get("email") or "").strip() or None))
            conn.execute("UPDATE teams SET membership_locked=1 WHERE id=?", (tid,))
            conn.execute("UPDATE users SET team_id=? WHERE id=?", (tid, user.id))
            audit(conn, user, "team_registered", "team", tid, {"team_code": code, "members": len(members)})
            notify.notify_team(conn, policy, tid, "registration_ack", "Team registration confirmed",
                               [f"Your team '{name}' is registered with code {code}. Membership is now locked:"]
                               + [f"  • {m['full_name'].strip()} ({m['member_ref'].strip()})" for m in members]
                               + ["Next step: select and lock your track and problem statement in the portal."], f"reg:{tid}")
    except sqlite3.IntegrityError as exc:
        msg = str(exc)
        if "member_ref" in msg:
            raise WorkflowError("A member ID is already registered in another team.") from None
        if "teams.name" in msg:
            raise WorkflowError("Team name already taken.") from None
        raise
    return tid


def change_membership(conn, user: User, team_id: int, **_) -> None:
    """Membership is locked after registration; every change request is refused and audited."""
    audit(conn, user, "membership_change_denied", "team", team_id)
    raise WorkflowError("Team membership is locked after registration.")


def get_team(conn, user: User, team_id: int) -> dict:
    if user.role == "student" and user.team_id != team_id:
        audit(conn, user, "authorization_denied", "team", team_id, {"action": "view_team"})
        raise AuthError("You may only view your own team.")
    t = conn.execute("SELECT * FROM teams WHERE id=?", (team_id,)).fetchone()
    if not t:
        raise WorkflowError("Team not found")
    d = dict(t)
    d["members"] = [dict(m) for m in conn.execute("SELECT full_name, member_ref, email FROM team_members WHERE team_id=?", (team_id,))]
    return d


def select_problem_statement(conn, user: User, domain_id: str, ps_id: str, policy: dict | None = None) -> None:
    authorize(conn, user, "student", action="select_ps")
    user = refresh_user(conn, user)
    if not user.team_id:
        raise WorkflowError("Register a team first.")
    ps = conn.execute("SELECT * FROM problem_statements WHERE id=? AND active=1", (ps_id,)).fetchone()
    if not ps:
        raise WorkflowError("Unknown problem statement.")
    if ps["domain_id"] != domain_id:
        raise WorkflowError("The selected problem statement does not belong to the selected domain.")
    with tx(conn):
        t = conn.execute("SELECT ps_locked FROM teams WHERE id=?", (user.team_id,)).fetchone()
        if t["ps_locked"]:
            raise WorkflowError("Domain and problem statement are already locked.")
        conn.execute("UPDATE teams SET domain_id=?, ps_id=?, ps_locked=1, ps_locked_at=? WHERE id=?", (domain_id, ps_id, iso(now_utc()), user.team_id))
        conn.execute("INSERT OR IGNORE INTO team_entries(team_id, domain_id, ps_id, entry_no, locked_at, locked_by) VALUES (?,?,?,1,?,?)",
                     (user.team_id, domain_id, ps_id, iso(now_utc()), user.id))
        audit(conn, user, "ps_locked", "team", user.team_id, {"domain": domain_id, "ps": ps_id})
        if policy is not None:
            dname = conn.execute("SELECT name FROM domains WHERE id=?", (domain_id,)).fetchone()["name"]
            notify.notify_team(conn, policy, user.team_id, "ps_lock_ack", "Track and problem statement locked",
                               [f"Track: {dname} ({domain_id})", f"Problem statement: {ps_id} — {ps['title']}",
                                f"This selection is final. Submit your abstract before {deadline(policy).strftime('%d %b %Y, %I:%M %p %Z')}."],
                               f"pslock:{user.team_id}")


def max_entries(policy: dict) -> int:
    return max(1, int(policy.get("submission", {}).get("max_problem_statements_per_team", 1) or 1))


def team_entries(conn, team_id: int) -> list[dict]:
    """Every problem statement a team is working on, with its own attempts and latest status."""
    out = []
    for r in conn.execute("SELECT e.*, p.title AS ps_title, d.name AS track FROM team_entries e JOIN problem_statements p ON p.id=e.ps_id "
                          "JOIN domains d ON d.id=e.domain_id WHERE e.team_id=? ORDER BY e.entry_no", (team_id,)):
        last = latest_submission(conn, team_id, r["ps_id"])
        out.append(dict(r) | {"attempts_used": attempts_used(conn, team_id, r["ps_id"]), "last_status": last["status"] if last else None,
                              "last_submission_id": last["id"] if last else None})
    return out


def add_problem_statement(conn, policy: dict, user: User, domain_id: str, ps_id: str, now: datetime | None = None) -> int:
    """Same team, same account: work on one more problem statement (same or another track). Each has its own attempts."""
    authorize(conn, user, "student", action="add_ps")
    user = refresh_user(conn, user)
    now = now or now_utc()
    if not user.team_id:
        raise WorkflowError("Register a team first.")
    team = conn.execute("SELECT * FROM teams WHERE id=?", (user.team_id,)).fetchone()
    if not team["ps_locked"]:
        raise WorkflowError("Lock your first problem statement before adding another.")
    if not before_deadline(policy, now):
        raise WorkflowError("The submission deadline has passed; no new problem statements can be added.")
    ps = conn.execute("SELECT * FROM problem_statements WHERE id=? AND active=1", (ps_id,)).fetchone()
    if not ps:
        raise WorkflowError("Unknown problem statement.")
    if ps["domain_id"] != domain_id:
        raise WorkflowError("The selected problem statement does not belong to the selected track.")
    with tx(conn):
        rows = conn.execute("SELECT ps_id, entry_no FROM team_entries WHERE team_id=?", (user.team_id,)).fetchall()
        if any(r["ps_id"] == ps_id for r in rows):
            raise WorkflowError("Your team already works on this problem statement.")
        if len(rows) >= max_entries(policy):
            raise WorkflowError(f"A team can work on at most {max_entries(policy)} problem statement(s).")
        no = max((r["entry_no"] for r in rows), default=0) + 1
        eid = conn.execute("INSERT INTO team_entries(team_id, domain_id, ps_id, entry_no, locked_at, locked_by) VALUES (?,?,?,?,?,?)",
                           (user.team_id, domain_id, ps_id, no, iso(now), user.id)).lastrowid
        audit(conn, user, "ps_added", "team", user.team_id, {"domain": domain_id, "ps": ps_id, "entry": no})
    dname = conn.execute("SELECT name FROM domains WHERE id=?", (domain_id,)).fetchone()["name"]
    notify.notify_team(conn, policy, user.team_id, "ps_added_ack", f"Problem statement {no} of {max_entries(policy)} added",
                       [f"Your team now also works on: {ps_id} — {ps['title']} (track: {dname}).",
                        f"It has its own {policy['submission']['max_attempts']} submission attempts.",
                        f"Choose it on the Submit page and submit before {deadline(policy).strftime('%d %b %Y, %I:%M %p %Z')}."],
                       f"psadd:{user.team_id}:{ps_id}")
    return eid


def _auto_propose(conn, policy: dict, sid: int) -> None:
    """Assignment agent: propose reviewers for a newly accepted abstract (the admin approves before anything is shared)."""
    if not policy.get("agents", {}).get("assignment", {}).get("auto_propose", True):
        return
    try:
        from agents import assigner
        assigner.propose(conn, policy, sid)
    except Exception as exc:  # never let the agent break an acceptance
        log.warning("assignment agent failed for submission %s: %s", sid, exc)


def _maybe_team_complete(conn, policy: dict, team_id: int) -> None:
    """Completion acknowledgement: every problem statement of the team now has an accepted abstract."""
    entries = team_entries(conn, team_id)
    if entries and all(e["last_status"] == "ACCEPTED_FOR_EVALUATION" for e in entries):
        notify.notify_team(conn, policy, team_id, "submission_complete", "Your Phase 1 submission is complete",
                           ["All your abstracts are accepted for evaluation:"] +
                           [f"  • {e['ps_id']} — {e['ps_title']} (attempt {e['attempts_used']})" for e in entries] +
                           ["", "Nothing more is needed from your team now. Reviewers will evaluate the abstracts and you will be emailed the results."],
                           f"teamdone:{team_id}:{len(entries)}")


# ---------------- deadlines & attempts ----------------

def deadline(policy: dict) -> datetime:
    dt = datetime.fromisoformat(policy["submission"]["deadline"])
    return dt if dt.tzinfo else dt.replace(tzinfo=ZoneInfo(policy["timezone"]))


def before_deadline(policy: dict, now: datetime) -> bool:
    d = deadline(policy)
    return now <= d if policy["submission"].get("deadline_inclusive", True) else now < d


def attempts_used(conn, team_id: int, ps_id: str | None) -> int:
    return conn.execute("SELECT COUNT(*) FROM submissions WHERE team_id=? AND ps_id IS ?", (team_id, ps_id)).fetchone()[0]


def latest_submission(conn, team_id: int, ps_id: str | None = None):
    if ps_id:
        return conn.execute("SELECT * FROM submissions WHERE team_id=? AND ps_id=? ORDER BY attempt_no DESC, id DESC LIMIT 1", (team_id, ps_id)).fetchone()
    return conn.execute("SELECT * FROM submissions WHERE team_id=? ORDER BY id DESC LIMIT 1", (team_id,)).fetchone()


def submission_gate(conn, policy: dict, team_id: int, now: datetime, ps_id: str | None = None) -> tuple[bool, str, str, int]:
    """Decide whether a new formal attempt may be created for one of the team's problem statements."""
    t = conn.execute("SELECT * FROM teams WHERE id=?", (team_id,)).fetchone()
    if not t["ps_locked"]:
        return False, "PS_NOT_LOCKED", "Select and lock your domain and problem statement first.", 0
    ps_id = ps_id or t["ps_id"]
    if not conn.execute("SELECT 1 FROM team_entries WHERE team_id=? AND ps_id=?", (team_id, ps_id)).fetchone():
        return False, "PS_NOT_LOCKED", "This problem statement is not one of your team's locked problem statements.", 0
    used = attempts_used(conn, team_id, ps_id)
    last = latest_submission(conn, team_id, ps_id)
    status = last["status"] if last else None
    maxa = policy["submission"]["max_attempts"]
    rules = {
        "deadline": (not before_deadline(policy, now), "DEADLINE_PASSED", f"The submission deadline ({deadline(policy).isoformat()}) has passed."),
        "pending_review": (status in states.IN_PROGRESS, "PENDING", f"Your latest submission is still being processed ({status})."),
        "attempts_exhausted": (used >= maxa, "ATTEMPTS_EXHAUSTED", f"All {maxa} submission attempts have been used."),
    }
    for key in policy["submission"]["resubmission_precedence"]:
        blocked, code, msg = rules[key]
        if blocked:
            return False, code, msg, used
    if status == "ACCEPTED_FOR_EVALUATION":
        return False, "ALREADY_ACCEPTED", "Your submission has already been accepted for evaluation.", used
    if status in states.TERMINAL:
        return False, "CLOSED", f"Submission process is closed ({status}).", used
    if status not in (None, "RESUBMISSION_ALLOWED"):
        return False, "NOT_ALLOWED", f"Resubmission not allowed from state {status}.", used
    return True, "OK", f"Attempt {used + 1} of {maxa} available.", used


# ---------------- state transitions ----------------

def _transition(conn, sub_id: int, to: str, actor: User | None, reason: str = "") -> None:
    kind = "system" if actor is None else actor.role
    with tx(conn):
        cur = conn.execute("SELECT status FROM submissions WHERE id=?", (sub_id,)).fetchone()["status"]
        try:
            states.check(cur, to, kind)
        except states.InvalidTransition:
            log.warning("invalid transition %s -> %s by %s on %s", cur, to, kind, sub_id)
            raise
        conn.execute("UPDATE submissions SET status=? WHERE id=?", (to, sub_id))
        conn.execute("INSERT INTO submission_status_history(submission_id,from_status,to_status,actor_id,reason,at) VALUES (?,?,?,?,?,?)",
                     (sub_id, cur, to, actor.id if actor else None, reason, iso(now_utc())))


def _after_failure_state(conn, policy: dict, sub, now: datetime) -> tuple[str, str]:
    if sub["attempt_no"] >= policy["submission"]["max_attempts"]:
        return "DROPPED", "All attempts used"
    if not before_deadline(policy, now):
        return "DEADLINE_MISSED", "Deadline passed"
    return "RESUBMISSION_ALLOWED", f"{policy['submission']['max_attempts'] - sub['attempt_no']} attempt(s) remaining"


# ---------------- submissions ----------------

def submit(conn, policy: dict, storage_dir: Path, user: User, file_name: str, data: bytes, now: datetime | None = None, embedder=None,
           ps_id: str | None = None, ai_client=None) -> dict:
    authorize(conn, user, "student", action="submit")
    user = refresh_user(conn, user)
    if not user.team_id:
        raise WorkflowError("Register a team first.")
    now = now or now_utc()
    t0 = time.perf_counter()
    safe = safe_filename(file_name)

    def blocked(code: str, msg: str):
        audit(conn, user, "submission_blocked", "team", user.team_id, {"code": code, "file": safe})
        metrics.record(conn, "submission_blocked", 1, {"code": code})
        notify.notify_team(conn, policy, user.team_id, "upload_refused", "Upload not accepted (no attempt used)",
                           [f"The file '{safe}' uploaded at {now.astimezone(deadline(policy).tzinfo).strftime('%d %b %Y, %I:%M %p %Z')} was not accepted:",
                            f"  • {msg}", "", "This did not count as a submission attempt."],
                           f"refused:{user.team_id}:{code}:{hashlib.sha256(data or b'').hexdigest()[:12]}:{now.strftime('%Y%m%d%H%M%S%f')}")
        raise SubmissionBlocked(code, msg)

    if not ps_id:
        ps_id = conn.execute("SELECT ps_id FROM teams WHERE id=?", (user.team_id,)).fetchone()["ps_id"]
    ok, code, msg, _ = submission_gate(conn, policy, user.team_id, now, ps_id)
    if not ok:
        blocked(code, msg)
    pre = extract.precheck(safe, data, policy)
    if not pre.ok:
        blocked(pre.code, pre.message)
    if policy.get("duplicates", {}).get("block_identical_resubmission", True):
        fsha = hashlib.sha256(data).hexdigest()
        prev = conn.execute("SELECT attempt_no, ps_id, file_sha256, text_hash FROM submissions WHERE team_id=?", (user.team_id,)).fetchall()
        if prev:
            ex0 = extract.extract(safe, data)
            th = checks.normalize_hash(ex0.text) if ex0.text else None
            same = next((r for r in prev if r["file_sha256"] == fsha or (th and r["text_hash"] == th)), None)
            if same:
                blocked("DUPLICATE_OF_PREVIOUS_ATTEMPT", f"This document is identical to your attempt {same['attempt_no']} for {same['ps_id']}. "
                        "Make the corrections from that report before resubmitting. No attempt was used.")

    team = conn.execute("SELECT * FROM teams WHERE id=?", (user.team_id,)).fetchone()
    path: Path | None = None
    try:
        with tx(conn):  # BEGIN IMMEDIATE: serialises concurrent attempt creation
            ok, code, msg, used = submission_gate(conn, policy, user.team_id, now, ps_id)
            if not ok:
                raise SubmissionBlocked(code, msg)
            cur = conn.execute(
                "INSERT INTO submissions(team_id,ps_id,attempt_no,status,file_name,file_sha256,policy_version,submitted_by,submitted_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (team["id"], ps_id, used + 1, "SUBMITTED", safe, hashlib.sha256(data).hexdigest(), policy["version"], user.id, iso(now)))
            sid = cur.lastrowid
            folder = Path(storage_dir) / "uploads" / f"team_{team['id']}"
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / f"{sid}_{safe}"
            path.write_bytes(data)
            path.chmod(0o600)
            conn.execute("UPDATE submissions SET stored_path=? WHERE id=?", (str(path), sid))
            conn.execute("INSERT INTO submission_status_history(submission_id,from_status,to_status,actor_id,reason,at) VALUES (?,?,?,?,?,?)",
                         (sid, None, "SUBMITTED", user.id, "formal submission", iso(now)))
            audit(conn, user, "submission_created", "submission", sid, {"attempt": used + 1, "file": safe})
    except SubmissionBlocked as e:
        blocked(e.code, str(e))
    except Exception:
        if path and path.exists():
            path.unlink()
        raise

    notify.notify_team(conn, policy, team["id"], "submission_ack", f"Submission received (attempt {used + 1} of {policy['submission']['max_attempts']})",
                       [f"We received '{safe}' at {now.astimezone(deadline(policy).tzinfo).strftime('%d %b %Y, %I:%M %p %Z')} as attempt {used + 1}.",
                        f"Problem statement: {ps_id}",
                        "Automatic checks are running; you will receive the result shortly."], f"recv:{sid}")
    _transition(conn, sid, "VALIDATING", None, "validation started")
    return _validate(conn, policy, user, team, sid, safe, data, now, t0, embedder, ps_id, ai_client)


DRAFT_CHECKS_PER_DAY = 20


def check_draft(conn, policy: dict, user: User, file_name: str, data: bytes, now: datetime | None = None, embedder=None,
                ps_id: str | None = None, ai_client=None) -> dict:
    """Run the same automatic checks on a draft WITHOUT creating a submission or using an attempt. Nothing is stored."""
    authorize(conn, user, "student", action="check_draft")
    user = refresh_user(conn, user)
    now = now or now_utc()
    team = conn.execute("SELECT * FROM teams WHERE id=?", (user.team_id,)).fetchone() if user.team_id else None
    if not team or not team["ps_locked"]:
        raise WorkflowError("Register your team and lock your problem statement first.")
    since = iso(now - timedelta(days=1))
    used = conn.execute("SELECT COUNT(*) FROM audit_events WHERE action='draft_checked' AND entity_id=? AND at>=?", (str(team["id"]), since)).fetchone()[0]
    if used >= DRAFT_CHECKS_PER_DAY:
        raise WorkflowError(f"Your team has used its {DRAFT_CHECKS_PER_DAY} draft checks for today. Try again tomorrow.")
    safe = safe_filename(file_name)
    pre = extract.precheck(safe, data, policy)
    audit(conn, user, "draft_checked", "team", team["id"], {"file": safe})
    if not pre.ok:
        return {"verdict": "fix", "results": [{"check_id": pre.code, "result": "fail", "severity": "error", "message": pre.message,
                                               "evidence": "", "guidance": "", "review_flag": False}], "word_count": 0, "checks_left": DRAFT_CHECKS_PER_DAY - used - 1}
    ps_id = ps_id or team["ps_id"]
    if not conn.execute("SELECT 1 FROM team_entries WHERE team_id=? AND ps_id=?", (team["id"], ps_id)).fetchone():
        raise WorkflowError("This problem statement is not one of your team's locked problem statements.")
    ps = dict(conn.execute("SELECT * FROM problem_statements WHERE id=?", (ps_id,)).fetchone())
    ps["keywords"] = json.loads(ps["keywords"])
    trow = conn.execute("SELECT * FROM domains WHERE id=?", (ps["domain_id"],)).fetchone()
    track = dict(trow) | {"keywords": json.loads(trow["keywords"])}
    ps_list = [dict(r) | {"keywords": json.loads(r["keywords"])} for r in conn.execute(
        "SELECT p.*, d.name AS track_name FROM problem_statements p JOIN domains d ON d.id=p.domain_id WHERE p.active=1 OR p.id=?", (ps["id"],))]
    ex = extract.extract(safe, data)
    results = checks.hard_checks(ex.text, ex.error, policy, ps, track, ps_list, embedder)
    if not ex.error and ex.text:
        results += agents.screen(ex.text, data, safe, ps, track, policy, ai_client, agents.keywords_ok(results))
        corpus = [dict(r) for r in conn.execute("SELECT id, team_id, extracted_text, text_hash FROM submissions WHERE extracted_text IS NOT NULL AND extracted_text != ''")]
        results += checks.similarity_assessment(ex.text, team["id"], corpus, policy, embedder)[1]
    errors = [r for r in results if r.result == "fail" and r.severity == "error"]
    verdict = "review" if agents.guardrail_hit(results) else "fix" if errors else "review" if any(r.review_flag for r in results) else "pass"
    return {"verdict": verdict, "results": [r.to_dict() for r in results], "word_count": len(checks.tokens(ex.text or "")),
            "checks_left": DRAFT_CHECKS_PER_DAY - used - 1}


def _validate(conn, policy, user, team, sid, safe, data, now, t0, embedder, ps_id=None, ai_client=None) -> dict:
    ps = dict(conn.execute("SELECT * FROM problem_statements WHERE id=?", (ps_id or team["ps_id"],)).fetchone())
    ps["keywords"] = json.loads(ps["keywords"])
    trow = conn.execute("SELECT * FROM domains WHERE id=?", (ps["domain_id"],)).fetchone()
    track = dict(trow) | {"keywords": json.loads(trow["keywords"])}
    ps_list = [dict(r) | {"keywords": json.loads(r["keywords"])} for r in conn.execute(
        "SELECT p.*, d.name AS track_name FROM problem_statements p JOIN domains d ON d.id=p.domain_id WHERE p.active=1 OR p.id=?", (ps["id"],))]
    ex = extract.extract(safe, data)
    results = checks.hard_checks(ex.text, ex.error, policy, ps, track, ps_list, embedder)
    findings: list[dict] = []
    if not ex.error and ex.text:
        corpus = [dict(r) for r in conn.execute("SELECT id, team_id, extracted_text, text_hash FROM submissions WHERE id != ? AND extracted_text IS NOT NULL AND extracted_text != ''", (sid,))]
        findings, sim_results = checks.similarity_assessment(ex.text, team["id"], corpus, policy, embedder)
        results += sim_results
        results += agents.screen(ex.text, data, safe, ps, track, policy, ai_client, agents.keywords_ok(results))   # guardrail + content analyser
    ts = iso(now_utc())
    with tx(conn):
        conn.execute("UPDATE submissions SET extracted_text=?, text_hash=?, word_count=? WHERE id=?",
                     (ex.text, checks.normalize_hash(ex.text) if ex.text else None, len(checks.tokens(ex.text)), sid))
        for r in results:
            conn.execute("INSERT INTO validation_results(submission_id,check_id,result,severity,evidence,message,guidance,review_flag,rule_version,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                         (sid, r.check_id, r.result, r.severity, r.evidence, r.message, r.guidance, int(r.review_flag), policy["version"], ts))
        for f in findings:
            conn.execute("INSERT INTO similarity_findings(submission_id,compared_submission_id,method,score,comparison_scope,evidence,flagged,created_at) VALUES (?,?,?,?,?,?,?,?)",
                         (sid, f["compared_submission_id"], f["method"], f["score"], f["comparison_scope"], f["evidence"], int(f["flagged"]), ts))
    errors = [r for r in results if r.result == "fail" and r.severity == "error"]
    flags = [r.check_id for r in results if r.review_flag]
    sub = conn.execute("SELECT * FROM submissions WHERE id=?", (sid,)).fetchone()
    att, maxa = sub["attempt_no"], policy["submission"]["max_attempts"]
    if agents.guardrail_hit(results):
        # a manipulation attempt always goes to a person, even when format checks also failed
        errors = []
        g = next(r for r in results if r.check_id in agents.GUARDRAIL_IDS and r.review_flag)
        if policy.get("agents", {}).get("guardrail", {}).get("alert_admins", True):
            notify.notify_staff(conn, policy, ("admin",), "guardrail_alert", f"Possible manipulation attempt: {team['team_code']} attempt {att}",
                                [f"Submission #{sid} from team {team['team_code']} ({ps['id']}) contains text aimed at the evaluation system.",
                                 g.message, "", "It was NOT accepted automatically. Decide in the Review Queue."], f"guardalert:{sid}", force=True)
    if errors:
        _transition(conn, sid, "VALIDATION_FAILED", None, "; ".join(r.check_id for r in errors))
        nxt, why = _after_failure_state(conn, policy, sub, now)
        _transition(conn, sid, nxt, None, why)
        nxt_line = {"RESUBMISSION_ALLOWED": f"You may correct and resubmit: {maxa - att} attempt(s) left, deadline "
                                            f"{deadline(policy).strftime('%d %b %Y, %I:%M %p %Z')}.",
                    "DROPPED": "All attempts have been used; the team cannot submit again.",
                    "DEADLINE_MISSED": "The deadline has passed; no further attempts are possible."}[nxt]
        notify.notify_team(conn, policy, team["id"], "validation_failed", f"Attempt {att} did not pass the checks",
                           [f"Your attempt {att} ('{safe}') for {ps['id']} did not pass these checks:"] + notify.checks_summary(conn, sid) + ["", nxt_line], f"fail:{sid}")
        if any(r.check_id == "DUPLICATE_SUBMISSION" for r in errors):
            orig = [f["compared_submission_id"] for f in findings if f["method"] == "exact_hash"]
            notify.notify_staff(conn, policy, ("admin",), "duplicate_alert", f"Duplicate submission rejected: {team['team_code']} attempt {att}",
                                [f"Submission #{sid} from team {team['team_code']} was rejected because its text is identical to submission(s) "
                                 f"#{', #'.join(str(o) for o in orig)} by another team.", "Both submissions are listed under Submissions for follow-up."],
                                f"dupalert:{sid}", force=True)
    elif flags:
        _transition(conn, sid, "PENDING_REVIEW", None, "review flags: " + ", ".join(flags))
        with tx(conn):
            conn.execute("INSERT INTO review_cases(submission_id,reasons,status,created_at) VALUES (?,?, 'open', ?)", (sid, json.dumps(flags), ts))
        notify.notify_team(conn, policy, team["id"], "pending_review", f"Attempt {att} is under human review",
                           ["Your submission passed the automatic format checks and was sent to a reviewer for:", "  • " + ", ".join(flags),
                            "This is not a rejection. You will be emailed the reviewer's decision."], f"review:{sid}")
        notify.notify_staff(conn, policy, ("reviewer", "admin"), "review_alert", f"New review case: {team['team_code']} attempt {att}",
                            [f"Submission #{sid} from team {team['team_code']} ({ps['id']}) needs a decision.", f"Flags: {', '.join(flags)}",
                             "Open the Review Queue in the portal."], f"reviewalert:{sid}")
    else:
        _transition(conn, sid, "ACCEPTED_FOR_EVALUATION", None, "all checks passed")
        notify.notify_team(conn, policy, team["id"], "accepted", "Abstract accepted for evaluation",
                           [f"Your attempt {att} ('{safe}') for {ps['id']} — {ps['title']} passed all Phase 1 checks and is accepted for evaluation.",
                            "No further action is needed for this problem statement."],
                           f"accept:{sid}")
        _maybe_team_complete(conn, policy, team["id"])
        _auto_propose(conn, policy, sid)
    ms = (time.perf_counter() - t0) * 1000
    conn.execute("UPDATE submissions SET processing_ms=? WHERE id=?", (ms, sid))
    metrics.record(conn, "submission_processing_ms", ms, {"submission": sid})
    return report(conn, user, sid)


def list_submissions(conn, user: User, team_id: int | None = None, search: str = "") -> list[dict]:
    if user.role == "student":
        user = refresh_user(conn, user)
        if team_id is not None and team_id != user.team_id:
            audit(conn, user, "authorization_denied", "team", team_id, {"action": "list_submissions"})
            raise AuthError("You may only view your own team's submissions.")
        team_id = user.team_id
        if team_id is None:
            return []
    elif user.role == "reviewer":
        authorize(conn, user, "reviewer", action="list_submissions")
        ids = [r[0] for r in conn.execute("SELECT submission_id FROM review_cases UNION SELECT submission_id FROM review_assignments "
                                          "WHERE reviewer_id=? AND status IN ('assigned','accepted','submitted')", (user.id,))]
        if not ids:
            return []
    sql = "SELECT s.id, s.team_id, t.team_code, t.name AS team_name, s.ps_id, s.attempt_no, s.status, s.file_name, s.word_count, s.submitted_at FROM submissions s JOIN teams t ON t.id=s.team_id WHERE 1=1"
    args: list = []
    if team_id is not None:
        sql += " AND s.team_id=?"
        args.append(team_id)
    if user.role == "reviewer":
        sql += f" AND s.id IN ({','.join('?' * len(ids))})"
        args += ids
    if search:
        sql += " AND (s.file_name LIKE ? OR s.status LIKE ? OR t.team_code LIKE ? OR t.name LIKE ?)"
        args += [f"%{search}%"] * 4
    return [dict(r) for r in conn.execute(sql + " ORDER BY s.id DESC", args)]


def reviewer_may_see(conn, reviewer_id: int, sub_id: int) -> bool:
    """Reviewers see only abstracts assigned to them for peer review, or ones in the compliance review queue."""
    if conn.execute("SELECT 1 FROM review_cases WHERE submission_id=?", (sub_id,)).fetchone():
        return True
    return bool(conn.execute("SELECT 1 FROM review_assignments WHERE submission_id=? AND reviewer_id=? AND status IN ('assigned','accepted','submitted')",
                             (sub_id, reviewer_id)).fetchone())


def report(conn, user: User, sub_id: int) -> dict:
    s = conn.execute("SELECT * FROM submissions WHERE id=?", (sub_id,)).fetchone()
    if not s:
        raise WorkflowError("Submission not found")
    if user.role == "student" and refresh_user(conn, user).team_id != s["team_id"]:
        audit(conn, user, "authorization_denied", "submission", sub_id, {"action": "view_report"})
        raise AuthError("You may only view your own team's submissions.")
    if user.role == "reviewer" and not reviewer_may_see(conn, user.id, sub_id):
        audit(conn, user, "authorization_denied", "submission", sub_id, {"action": "view_report"})
        raise AuthError("This abstract is not assigned to you.")
    t = conn.execute("SELECT team_code FROM teams WHERE id=?", (s["team_id"],)).fetchone()
    vr = [dict(r) for r in conn.execute("SELECT check_id,result,severity,message,evidence,guidance,review_flag,rule_version FROM validation_results WHERE submission_id=? ORDER BY id", (sub_id,))]
    sim = [dict(r) for r in conn.execute("SELECT compared_submission_id,method,score,comparison_scope,evidence,flagged FROM similarity_findings WHERE submission_id=?", (sub_id,))]
    if user.role == "student":  # do not reveal other teams' identities/content
        for f in sim:
            f["compared_submission_id"] = None
            if f["method"] == "lexical_shingle_jaccard" and f["score"] is not None:
                f["evidence"] = "redacted for students"
    case = conn.execute("SELECT * FROM review_cases WHERE submission_id=?", (sub_id,)).fetchone()
    decisions = [dict(r) for r in conn.execute("SELECT outcome, reason, policy_ref, decided_at FROM review_decisions WHERE submission_id=?", (sub_id,))]
    hist = [dict(r) for r in conn.execute("SELECT from_status,to_status,reason,at FROM submission_status_history WHERE submission_id=? ORDER BY id", (sub_id,))]
    missing = []
    for r in vr:
        if r["check_id"] == "REQUIRED_SECTIONS" and r["result"] == "fail":
            missing = json.loads(r["evidence"]).get("missing", [])
    return {
        "submission_id": s["id"], "team_id": s["team_id"], "team_code": t["team_code"], "ps_id": s["ps_id"],
        "attempt_no": s["attempt_no"], "file_name": s["file_name"], "status": s["status"], "word_count": s["word_count"],
        "missing_sections": missing, "checks": vr, "similarity": sim,
        "review_status": (case["status"] if case else "not_required"), "review_reasons": json.loads(case["reasons"]) if case else [],
        "decisions": decisions, "history": hist,
        "corrective_actions": [r["guidance"] for r in vr if r["result"] in ("fail", "warn") and r["guidance"]],
        "submitted_at": s["submitted_at"], "policy_version": s["policy_version"], "processing_ms": s["processing_ms"],
    }


def submission_file(conn, user: User, sub_id: int, storage_dir: Path | None = None) -> tuple[str, bytes]:
    """The original uploaded abstract: reviewers/admins for any submission, students only for their own team.
    Call log_file_download() when the file is actually downloaded."""
    s = conn.execute("SELECT id, team_id, file_name, stored_path FROM submissions WHERE id=?", (sub_id,)).fetchone()
    if not s:
        raise WorkflowError("Submission not found")
    if user.role == "student":
        if refresh_user(conn, user).team_id != s["team_id"]:
            audit(conn, user, "authorization_denied", "submission", sub_id, {"action": "download_file"})
            raise AuthError("You may only download your own team's files.")
    else:
        authorize(conn, user, "reviewer", "admin", action="download_file")
        if user.role == "reviewer" and not reviewer_may_see(conn, user.id, sub_id):
            audit(conn, user, "authorization_denied", "submission", sub_id, {"action": "download_file"})
            raise AuthError("This abstract is not assigned to you.")
    candidates = [Path(s["stored_path"])] if s["stored_path"] else []
    if storage_dir and s["stored_path"]:  # the storage folder may have been moved/renamed since upload
        candidates.append(Path(storage_dir) / "uploads" / f"team_{s['team_id']}" / Path(s["stored_path"]).name)
    for p in candidates:
        if p.is_file():
            return s["file_name"], p.read_bytes()
    raise WorkflowError("The uploaded file is not on this server any more (was the storage folder moved or restored without uploads?).")


def log_file_download(conn, user: User, sub_id: int, file_name: str) -> None:
    audit(conn, user, "submission_file_downloaded", "submission", sub_id, {"file": file_name})


# ---------------- review ----------------

def review_queue(conn, user: User, only_open: bool = True, flag: str = "") -> list[dict]:
    authorize(conn, user, "reviewer", "admin", action="review_queue")
    sql = ("SELECT c.id AS case_id, c.submission_id, c.reasons, c.status AS case_status, c.created_at, s.attempt_no, s.status, s.file_name, "
           "t.team_code, t.name AS team_name, s.ps_id, (SELECT MAX(score) FROM similarity_findings f WHERE f.submission_id=s.id AND f.method='lexical_shingle_jaccard') AS max_lexical "
           "FROM review_cases c JOIN submissions s ON s.id=c.submission_id JOIN teams t ON t.id=s.team_id")
    sql += " WHERE c.status='open'" if only_open else " WHERE 1=1"
    rows = [dict(r) | {"reasons": json.loads(r["reasons"])} for r in conn.execute(sql + " ORDER BY c.created_at")]
    return [r for r in rows if not flag or flag in r["reasons"]]


def decide(conn, policy: dict, user: User, submission_id: int, outcome: str, reason: str, policy_ref: str | None = None, now: datetime | None = None) -> dict:
    authorize(conn, user, "reviewer", "admin", action="review_decision")
    now = now or now_utc()
    reason = (reason or "").strip()
    if outcome not in ("cleared", "corrections_requested", "rejected"):
        raise WorkflowError("Invalid outcome")
    if len(reason) < policy["review"]["reason_min_chars"]:
        raise WorkflowError(f"A reason of at least {policy['review']['reason_min_chars']} characters is required.")
    approved = {p["id"] for p in policy["review"]["approved_rejection_policies"]}
    if outcome == "rejected" and policy_ref not in approved:
        audit(conn, user, "rejection_refused_no_policy", "submission", submission_id, {"policy_ref": policy_ref})
        raise WorkflowError("Rejection requires an approved rejection policy reference. Similarity alone is not grounds for rejection.")
    sub = conn.execute("SELECT * FROM submissions WHERE id=?", (submission_id,)).fetchone()
    case = conn.execute("SELECT * FROM review_cases WHERE submission_id=? AND status='open'", (submission_id,)).fetchone()
    if not sub or not case or sub["status"] != "PENDING_REVIEW":
        raise WorkflowError("No open review case for this submission.")
    evidence = json.dumps([dict(r) for r in conn.execute("SELECT method,score,compared_submission_id,flagged FROM similarity_findings WHERE submission_id=?", (submission_id,))])
    target = {"cleared": "REVIEW_CLEARED", "corrections_requested": "CORRECTIONS_REQUESTED", "rejected": "REJECTED_BY_AUTHORIZED_REVIEW"}[outcome]
    with tx(conn):
        conn.execute("INSERT INTO review_decisions(review_case_id,submission_id,reviewer_id,outcome,reason,policy_ref,evidence,decided_at) VALUES (?,?,?,?,?,?,?,?)",
                     (case["id"], submission_id, user.id, outcome, reason, policy_ref, evidence, iso(now)))
        conn.execute("UPDATE review_cases SET status='closed', closed_at=? WHERE id=?", (iso(now), case["id"]))
        _transition(conn, submission_id, target, user, reason)
        if outcome == "cleared":
            _transition(conn, submission_id, "ACCEPTED_FOR_EVALUATION", None, "review cleared")
        elif outcome == "corrections_requested":
            nxt, why = _after_failure_state(conn, policy, sub, now)
            _transition(conn, submission_id, nxt, None, why)
        audit(conn, user, "review_decision", "submission", submission_id, {"outcome": outcome, "policy_ref": policy_ref})
    team = conn.execute("SELECT * FROM teams WHERE id=?", (sub["team_id"],)).fetchone()
    final = conn.execute("SELECT status FROM submissions WHERE id=?", (submission_id,)).fetchone()["status"]
    nxt = {"ACCEPTED_FOR_EVALUATION": "Your abstract is accepted for evaluation. No further action is needed.",
           "RESUBMISSION_ALLOWED": f"Please correct and resubmit before {deadline(policy).strftime('%d %b %Y, %I:%M %p %Z')} "
                                   f"({policy['submission']['max_attempts'] - sub['attempt_no']} attempt(s) left).",
           "REJECTED_BY_AUTHORIZED_REVIEW": f"The submission was rejected under policy {policy_ref}.",
           "DROPPED": "No attempts remain.", "DEADLINE_MISSED": "The deadline has passed."}.get(final, final)
    notify.notify_team(conn, policy, team["id"], "review_decision", f"Review outcome: {outcome.replace('_', ' ')}",
                       [f"A reviewer has decided on attempt {sub['attempt_no']}: {outcome.replace('_', ' ').upper()}.", f"Reason: {reason}", "", nxt],
                       f"decision:{submission_id}")
    if final == "ACCEPTED_FOR_EVALUATION":
        _maybe_team_complete(conn, policy, team["id"])
        _auto_propose(conn, policy, submission_id)
    notify.notify_user(conn, policy, user.id, "decision_ack", f"Decision recorded: {team['team_code']} submission #{submission_id}",
                       [f"Your decision '{outcome.replace('_', ' ')}' for submission #{submission_id} (team {team['team_code']}) was recorded.",
                        f"Reason: {reason}"] + ([f"Policy reference: {policy_ref}"] if policy_ref else []) + ["The team has been notified."],
                       f"decisionack:{submission_id}")
    return report(conn, user, submission_id)


# ---------------- admin ----------------

def sweep_deadlines(conn, policy: dict, admin: User, now: datetime | None = None) -> int:
    """After the deadline, mark open resubmission windows as DEADLINE_MISSED."""
    authorize(conn, admin, "admin", action="sweep_deadlines")
    now = now or now_utc()
    if before_deadline(policy, now):
        return 0
    n = 0
    for r in conn.execute("SELECT id, team_id, attempt_no FROM submissions s WHERE status='RESUBMISSION_ALLOWED' "
                          "AND attempt_no=(SELECT MAX(attempt_no) FROM submissions x WHERE x.team_id=s.team_id AND x.ps_id IS s.ps_id)").fetchall():
        _transition(conn, r["id"], "DEADLINE_MISSED", None, "deadline sweep")
        notify.notify_team(conn, policy, r["team_id"], "deadline_missed", "Submission window closed",
                           [f"The deadline ({deadline(policy).strftime('%d %b %Y, %I:%M %p %Z')}) has passed and your team did not complete a successful submission.",
                            f"Your last attempt ({r['attempt_no']}) required corrections; no further attempts are possible.",
                            "Contact the incubation office if you believe this is an error."], f"deadlinemissed:{r['id']}")
        n += 1
    audit(conn, admin, "deadline_sweep", details={"marked": n})
    notify.notify_user(conn, policy, admin.id, "sweep_ack", "Deadline sweep completed",
                       [f"{n} team(s) were marked DEADLINE_MISSED and notified."], f"sweep:{now.strftime('%Y%m%d%H%M%S')}")
    return n


def audit_log(conn, user: User, limit: int = 500, action: str = "") -> list[dict]:
    authorize(conn, user, "admin", action="view_audit")
    sql, args = "SELECT * FROM audit_events", []
    if action:
        sql += " WHERE action LIKE ?"
        args.append(f"%{action}%")
    return [dict(r) for r in conn.execute(sql + " ORDER BY id DESC LIMIT ?", (*args, limit))]


# ---------------- organiser updates (feedback on drafts, announcements) ----------------

UPDATE_CATEGORIES = ("Update", "Feedback on your draft", "Action required", "Shortlisted / good news", "Announcement")
AUDIENCES = {
    "all": "All registered teams",
    "submitted": "Teams that have submitted at least once",
    "not_submitted": "Teams that have not submitted yet",
    "accepted": "Teams accepted for evaluation",
    "needs_resubmission": "Teams that must correct and resubmit",
    "pending_review": "Teams whose submission is under review",
}


def teams_for_audience(conn, user: User, audience: str) -> list[dict]:
    """Teams an announcement goes to. audience = one of AUDIENCES or 'track:<TRACK_ID>'."""
    authorize(conn, user, "reviewer", "admin", action="announcement_audience")
    rows = [dict(r) for r in conn.execute(
        "SELECT t.id, t.team_code, t.name, t.domain_id, (SELECT s.status FROM submissions s WHERE s.team_id=t.id ORDER BY s.attempt_no DESC LIMIT 1) AS last_status "
        "FROM teams t ORDER BY t.team_code")]
    if audience.startswith("track:"):
        return [r for r in rows if r["domain_id"] == audience.split(":", 1)[1]]
    pick = {
        "all": lambda r: True,
        "submitted": lambda r: r["last_status"] is not None,
        "not_submitted": lambda r: r["last_status"] is None,
        "accepted": lambda r: r["last_status"] == "ACCEPTED_FOR_EVALUATION",
        "needs_resubmission": lambda r: r["last_status"] == "RESUBMISSION_ALLOWED",
        "pending_review": lambda r: r["last_status"] in ("PENDING_REVIEW", "REVIEW_CLEARED"),
    }.get(audience)
    if pick is None:
        raise WorkflowError("Unknown audience")
    return [r for r in rows if pick(r)]


def _check_message(category: str, subject: str, body: str) -> tuple[str, str, str]:
    subject, body = (subject or "").strip(), (body or "").strip()
    if category not in UPDATE_CATEGORIES:
        raise WorkflowError("Choose a message type")
    if not 3 <= len(subject) <= 150:
        raise WorkflowError("The subject must be 3-150 characters.")
    if not 10 <= len(body) <= 5000:
        raise WorkflowError("The message must be 10-5000 characters.")
    return category, subject, body


def send_submission_update(conn, policy: dict, user: User, submission_id: int, category: str, subject: str, body: str) -> int:
    """Organiser (reviewer/admin) sends feedback or an update about one submitted draft to that team.
    Stored permanently, shown to the team in the portal and emailed to the contact + members. Returns the message id."""
    authorize(conn, user, "reviewer", "admin", action="send_submission_update")
    category, subject, body = _check_message(category, subject, body)
    sub = conn.execute("SELECT * FROM submissions WHERE id=?", (submission_id,)).fetchone()
    if not sub:
        raise WorkflowError("Submission not found")
    team = conn.execute("SELECT * FROM teams WHERE id=?", (sub["team_id"],)).fetchone()
    with tx(conn):
        mid = conn.execute("INSERT INTO team_messages(team_id,submission_id,sender_id,category,subject,body,created_at) VALUES (?,?,?,?,?,?,?)",
                           (team["id"], submission_id, user.id, category, subject, body, iso(now_utc()))).lastrowid
        audit(conn, user, "submission_update_sent", "submission", submission_id, {"message_id": mid, "category": category})
    notify.notify_team(conn, policy, team["id"], "organizer_update", f"{category}: {subject}",
                       [f"The incubation team has sent an update about your attempt {sub['attempt_no']} ('{sub['file_name']}', "
                        f"current status: {sub['status'].replace('_', ' ').lower()}):", "", body, "",
                        "You can also read this message in the portal under History."], f"update:{mid}")
    notify.notify_user(conn, policy, user.id, "update_sent_ack", f"Update sent to team {team['team_code']}",
                       [f"Your message '{subject}' about submission #{submission_id} was sent to team {team['team_code']}."], f"updateack:{mid}")
    return mid


def send_announcement(conn, policy: dict, user: User, audience: str, category: str, subject: str, body: str) -> int:
    """Organiser message to many teams at once (e.g. 'deadline extended', 'shortlist published'). Returns the number of teams."""
    authorize(conn, user, "admin", action="send_announcement")
    category, subject, body = _check_message(category, subject, body)
    teams = teams_for_audience(conn, user, audience)
    if not teams:
        raise WorkflowError("No teams match this audience.")
    ann = f"A{now_utc().strftime('%Y%m%d%H%M%S%f')}"
    with tx(conn):
        ids = [conn.execute("INSERT INTO team_messages(team_id,submission_id,sender_id,category,subject,body,announcement_id,created_at) VALUES (?,?,?,?,?,?,?,?)",
                            (t["id"], None, user.id, category, subject, body, ann, iso(now_utc()))).lastrowid for t in teams]
        audit(conn, user, "announcement_sent", "announcement", ann, {"audience": audience, "teams": len(teams), "category": category})
    for t, mid in zip(teams, ids):
        notify.notify_team(conn, policy, t["id"], "organizer_update", f"{category}: {subject}",
                           [body, "", "You can also read this message in the portal under History."], f"update:{mid}")
    notify.notify_user(conn, policy, user.id, "update_sent_ack", f"Announcement sent to {len(teams)} team(s)",
                       [f"'{subject}' was sent to {len(teams)} team(s) ({AUDIENCES.get(audience, audience)})."], f"annack:{ann}")
    return len(teams)


def team_messages(conn, user: User, team_id: int | None = None, submission_id: int | None = None) -> list[dict]:
    """Messages from the organisers. Students only ever see their own team's."""
    if user.role == "student":
        user = refresh_user(conn, user)
        if team_id is not None and team_id != user.team_id:
            audit(conn, user, "authorization_denied", "team", team_id, {"action": "read_messages"})
            raise AuthError("You may only read your own team's messages.")
        team_id = user.team_id
        if team_id is None:
            return []
    elif user.role == "reviewer":
        authorize(conn, user, "reviewer", action="list_submissions")
        ids = [r[0] for r in conn.execute("SELECT submission_id FROM review_cases UNION SELECT submission_id FROM review_assignments "
                                          "WHERE reviewer_id=? AND status IN ('assigned','accepted','submitted')", (user.id,))]
        if not ids:
            return []
    sql = ("SELECT m.id, m.team_id, t.team_code, m.submission_id, m.category, m.subject, m.body, m.created_at, u.username AS sent_by "
           "FROM team_messages m JOIN teams t ON t.id=m.team_id JOIN users u ON u.id=m.sender_id WHERE 1=1")
    args: list = []
    if team_id is not None:
        sql += " AND m.team_id=?"
        args.append(team_id)
    if submission_id is not None:
        sql += " AND m.submission_id=?"
        args.append(submission_id)
    return [dict(r) for r in conn.execute(sql + " ORDER BY m.id DESC", args)]


def discard_waiting_emails(conn, admin: User) -> int:
    """Admin: cancel all emails still waiting to be sent (they stay listed, marked as discarded)."""
    authorize(conn, admin, "admin", action="discard_emails")
    from notifications.outbox import discard_waiting

    n = discard_waiting(conn)
    audit(conn, admin, "emails_discarded", "notifications", None, {"count": n})
    return n
