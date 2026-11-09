"""SQLite connection, transactions and versioned migrations."""
from __future__ import annotations

import contextlib
import os
import sqlite3
from pathlib import Path

SCHEMA_V1 = """
CREATE TABLE users (
  id INTEGER PRIMARY KEY,
  username TEXT NOT NULL UNIQUE COLLATE NOCASE,
  pw_hash TEXT NOT NULL,
  role TEXT NOT NULL CHECK (role IN ('student','reviewer','admin')),
  team_id INTEGER REFERENCES teams(id),
  active INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL
);
CREATE TABLE domains (
  id TEXT PRIMARY KEY, name TEXT NOT NULL, description TEXT, active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE problem_statements (
  id TEXT PRIMARY KEY, domain_id TEXT NOT NULL REFERENCES domains(id),
  title TEXT NOT NULL, description TEXT, keywords TEXT NOT NULL DEFAULT '[]', active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE teams (
  id INTEGER PRIMARY KEY,
  team_code TEXT NOT NULL UNIQUE,
  name TEXT NOT NULL UNIQUE COLLATE NOCASE,
  contact_email TEXT NOT NULL,
  created_by INTEGER NOT NULL REFERENCES users(id),
  membership_locked INTEGER NOT NULL DEFAULT 0,
  domain_id TEXT REFERENCES domains(id),
  ps_id TEXT REFERENCES problem_statements(id),
  ps_locked INTEGER NOT NULL DEFAULT 0,
  ps_locked_at TEXT,
  created_at TEXT NOT NULL
);
CREATE TABLE team_members (
  id INTEGER PRIMARY KEY,
  team_id INTEGER NOT NULL REFERENCES teams(id),
  full_name TEXT NOT NULL,
  member_ref TEXT NOT NULL UNIQUE COLLATE NOCASE,
  email TEXT
);
CREATE TABLE submissions (
  id INTEGER PRIMARY KEY,
  team_id INTEGER NOT NULL REFERENCES teams(id),
  ps_id TEXT NOT NULL REFERENCES problem_statements(id),
  attempt_no INTEGER NOT NULL CHECK (attempt_no >= 1),
  status TEXT NOT NULL,
  file_name TEXT NOT NULL,
  stored_path TEXT,
  file_sha256 TEXT NOT NULL,
  text_hash TEXT,
  word_count INTEGER,
  extracted_text TEXT,
  policy_version TEXT NOT NULL,
  submitted_by INTEGER NOT NULL REFERENCES users(id),
  submitted_at TEXT NOT NULL,
  processing_ms REAL,
  UNIQUE (team_id, ps_id, attempt_no)
);
CREATE INDEX ix_sub_team ON submissions(team_id);
CREATE INDEX ix_sub_status ON submissions(status);
CREATE TABLE submission_status_history (
  id INTEGER PRIMARY KEY,
  submission_id INTEGER NOT NULL REFERENCES submissions(id),
  from_status TEXT, to_status TEXT NOT NULL,
  actor_id INTEGER REFERENCES users(id), reason TEXT, at TEXT NOT NULL
);
CREATE TABLE validation_results (
  id INTEGER PRIMARY KEY,
  submission_id INTEGER NOT NULL REFERENCES submissions(id),
  check_id TEXT NOT NULL,
  result TEXT NOT NULL CHECK (result IN ('pass','fail','warn','skip')),
  severity TEXT NOT NULL CHECK (severity IN ('info','warning','error')),
  evidence TEXT, message TEXT NOT NULL, guidance TEXT,
  review_flag INTEGER NOT NULL DEFAULT 0,
  rule_version TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE similarity_findings (
  id INTEGER PRIMARY KEY,
  submission_id INTEGER NOT NULL REFERENCES submissions(id),
  compared_submission_id INTEGER REFERENCES submissions(id),
  method TEXT NOT NULL CHECK (method IN ('exact_hash','lexical_shingle_jaccard','semantic_cosine')),
  score REAL, comparison_scope TEXT NOT NULL, evidence TEXT,
  flagged INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL
);
CREATE TABLE review_cases (
  id INTEGER PRIMARY KEY,
  submission_id INTEGER NOT NULL UNIQUE REFERENCES submissions(id),
  reasons TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('open','closed')),
  created_at TEXT NOT NULL, closed_at TEXT
);
CREATE TABLE review_decisions (
  id INTEGER PRIMARY KEY,
  review_case_id INTEGER NOT NULL REFERENCES review_cases(id),
  submission_id INTEGER NOT NULL REFERENCES submissions(id),
  reviewer_id INTEGER NOT NULL REFERENCES users(id),
  outcome TEXT NOT NULL CHECK (outcome IN ('cleared','corrections_requested','rejected')),
  reason TEXT NOT NULL CHECK (length(trim(reason)) > 0),
  policy_ref TEXT, evidence TEXT, decided_at TEXT NOT NULL
);
CREATE TABLE notifications (
  id INTEGER PRIMARY KEY,
  recipient TEXT NOT NULL, event_type TEXT NOT NULL, subject TEXT NOT NULL, body TEXT NOT NULL,
  dedupe_key TEXT NOT NULL UNIQUE,
  status TEXT NOT NULL CHECK (status IN ('queued_local','sent','failed')),
  attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT,
  created_at TEXT NOT NULL, sent_at TEXT
);
CREATE TABLE policy_versions (
  id INTEGER PRIMARY KEY, version TEXT NOT NULL, sha256 TEXT NOT NULL UNIQUE, content TEXT NOT NULL, loaded_at TEXT NOT NULL
);
CREATE TABLE audit_events (
  id INTEGER PRIMARY KEY, actor_id INTEGER, actor_role TEXT, action TEXT NOT NULL,
  entity_type TEXT, entity_id TEXT, details TEXT, at TEXT NOT NULL
);
CREATE INDEX ix_audit_at ON audit_events(at);
CREATE TABLE metrics (
  id INTEGER PRIMARY KEY, name TEXT NOT NULL, value REAL NOT NULL, labels TEXT, at TEXT NOT NULL
);
CREATE INDEX ix_metrics_name ON metrics(name);

-- Append-only / immutability guarantees
CREATE TRIGGER audit_no_update BEFORE UPDATE ON audit_events BEGIN SELECT RAISE(ABORT, 'audit_events is append-only'); END;
CREATE TRIGGER audit_no_delete BEFORE DELETE ON audit_events BEGIN SELECT RAISE(ABORT, 'audit_events is append-only'); END;
CREATE TRIGGER decision_no_update BEFORE UPDATE ON review_decisions BEGIN SELECT RAISE(ABORT, 'review decisions are final'); END;
CREATE TRIGGER decision_no_delete BEFORE DELETE ON review_decisions BEGIN SELECT RAISE(ABORT, 'review decisions are final'); END;
CREATE TRIGGER vr_no_update BEFORE UPDATE ON validation_results BEGIN SELECT RAISE(ABORT, 'validation results are immutable'); END;
CREATE TRIGGER vr_no_delete BEFORE DELETE ON validation_results BEGIN SELECT RAISE(ABORT, 'validation results are immutable'); END;
CREATE TRIGGER sub_no_delete BEFORE DELETE ON submissions BEGIN SELECT RAISE(ABORT, 'submissions are immutable'); END;
CREATE TRIGGER sub_immutable BEFORE UPDATE OF team_id, ps_id, attempt_no, file_name, file_sha256, submitted_by, submitted_at, policy_version ON submissions
BEGIN SELECT RAISE(ABORT, 'submission version is immutable'); END;
CREATE TRIGGER sub_set_once BEFORE UPDATE OF stored_path, extracted_text, text_hash, word_count ON submissions
WHEN (OLD.stored_path IS NOT NULL AND NEW.stored_path IS NOT OLD.stored_path)
  OR (OLD.extracted_text IS NOT NULL AND NEW.extracted_text IS NOT OLD.extracted_text)
  OR (OLD.text_hash IS NOT NULL AND NEW.text_hash IS NOT OLD.text_hash)
  OR (OLD.word_count IS NOT NULL AND NEW.word_count IS NOT OLD.word_count)
BEGIN SELECT RAISE(ABORT, 'submission content is immutable'); END;
CREATE TRIGGER members_locked_ins BEFORE INSERT ON team_members
WHEN (SELECT membership_locked FROM teams WHERE id = NEW.team_id) = 1
BEGIN SELECT RAISE(ABORT, 'team membership is locked'); END;
CREATE TRIGGER members_locked_upd BEFORE UPDATE ON team_members
WHEN (SELECT membership_locked FROM teams WHERE id = OLD.team_id) = 1
BEGIN SELECT RAISE(ABORT, 'team membership is locked'); END;
CREATE TRIGGER members_locked_del BEFORE DELETE ON team_members
WHEN (SELECT membership_locked FROM teams WHERE id = OLD.team_id) = 1
BEGIN SELECT RAISE(ABORT, 'team membership is locked'); END;
CREATE TRIGGER team_unlock_forbidden BEFORE UPDATE OF membership_locked ON teams
WHEN OLD.membership_locked = 1 AND NEW.membership_locked = 0
BEGIN SELECT RAISE(ABORT, 'team membership is locked'); END;
CREATE TRIGGER ps_locked BEFORE UPDATE OF domain_id, ps_id, ps_locked ON teams
WHEN OLD.ps_locked = 1 AND (NEW.ps_id IS NOT OLD.ps_id OR NEW.domain_id IS NOT OLD.domain_id OR NEW.ps_locked = 0)
BEGIN SELECT RAISE(ABORT, 'domain/problem statement is locked'); END;
"""

SCHEMA_V2 = """
CREATE TABLE programme_settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL,
  updated_by INTEGER REFERENCES users(id),
  updated_at TEXT NOT NULL
);
"""

SCHEMA_V3 = """
ALTER TABLE users ADD COLUMN email TEXT;
ALTER TABLE domains ADD COLUMN keywords TEXT NOT NULL DEFAULT '[]';
CREATE INDEX ix_notif_status ON notifications(status);
"""

SCHEMA_V4 = """
CREATE TABLE team_messages (
  id INTEGER PRIMARY KEY,
  team_id INTEGER NOT NULL REFERENCES teams(id),
  submission_id INTEGER REFERENCES submissions(id),
  sender_id INTEGER NOT NULL REFERENCES users(id),
  category TEXT NOT NULL,
  subject TEXT NOT NULL CHECK (length(trim(subject)) > 0),
  body TEXT NOT NULL CHECK (length(trim(body)) > 0),
  announcement_id TEXT,
  created_at TEXT NOT NULL
);
CREATE INDEX ix_team_messages_team ON team_messages(team_id);
CREATE TRIGGER team_messages_no_update BEFORE UPDATE ON team_messages BEGIN SELECT RAISE(ABORT, 'organiser updates are a permanent record'); END;
CREATE TRIGGER team_messages_no_delete BEFORE DELETE ON team_messages BEGIN SELECT RAISE(ABORT, 'organiser updates are a permanent record'); END;
"""

SCHEMA_V5 = """
CREATE TABLE reviewer_profiles (
  user_id INTEGER PRIMARY KEY REFERENCES users(id),
  expertise TEXT NOT NULL DEFAULT '[]',
  max_load INTEGER NOT NULL DEFAULT 8 CHECK (max_load >= 0),
  available INTEGER NOT NULL DEFAULT 1,
  updated_at TEXT NOT NULL
);
CREATE TABLE reviewer_conflicts (
  id INTEGER PRIMARY KEY,
  reviewer_id INTEGER NOT NULL REFERENCES users(id),
  team_id INTEGER NOT NULL REFERENCES teams(id),
  reason TEXT NOT NULL,
  declared_by INTEGER REFERENCES users(id),
  declared_at TEXT NOT NULL,
  UNIQUE (reviewer_id, team_id)
);
CREATE TABLE review_assignments (
  id INTEGER PRIMARY KEY,
  submission_id INTEGER NOT NULL REFERENCES submissions(id),
  reviewer_id INTEGER NOT NULL REFERENCES users(id),
  assigned_by INTEGER REFERENCES users(id),
  status TEXT NOT NULL CHECK (status IN ('assigned','accepted','declined','submitted','withdrawn')),
  due_at TEXT NOT NULL,
  assigned_at TEXT NOT NULL,
  responded_at TEXT,
  note TEXT,
  UNIQUE (submission_id, reviewer_id)
);
CREATE INDEX ix_assign_reviewer ON review_assignments(reviewer_id, status);
CREATE TABLE peer_reviews (
  id INTEGER PRIMARY KEY,
  assignment_id INTEGER NOT NULL UNIQUE REFERENCES review_assignments(id),
  submission_id INTEGER NOT NULL REFERENCES submissions(id),
  reviewer_id INTEGER NOT NULL REFERENCES users(id),
  scores TEXT NOT NULL,
  total REAL,
  recommendation TEXT,
  confidence INTEGER,
  comments_team TEXT NOT NULL DEFAULT '',
  comments_confidential TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL CHECK (status IN ('draft','submitted')),
  revision INTEGER NOT NULL DEFAULT 0,
  updated_at TEXT NOT NULL,
  submitted_at TEXT
);
CREATE TABLE peer_review_history (
  id INTEGER PRIMARY KEY,
  review_id INTEGER NOT NULL REFERENCES peer_reviews(id),
  revision INTEGER NOT NULL,
  data TEXT NOT NULL,
  at TEXT NOT NULL
);
CREATE TRIGGER peer_review_history_no_update BEFORE UPDATE ON peer_review_history BEGIN SELECT RAISE(ABORT, 'review history is a permanent record'); END;
CREATE TRIGGER peer_review_history_no_delete BEFORE DELETE ON peer_review_history BEGIN SELECT RAISE(ABORT, 'review history is a permanent record'); END;
CREATE TABLE evaluation_decisions (
  id INTEGER PRIMARY KEY,
  submission_id INTEGER NOT NULL REFERENCES submissions(id),
  team_id INTEGER NOT NULL REFERENCES teams(id),
  outcome TEXT NOT NULL,
  mean_score REAL,
  reviews_count INTEGER NOT NULL,
  note_to_team TEXT NOT NULL DEFAULT '',
  share_comments INTEGER NOT NULL DEFAULT 1,
  decided_by INTEGER NOT NULL REFERENCES users(id),
  decided_at TEXT NOT NULL
);
CREATE TABLE evaluation_releases (
  decision_id INTEGER PRIMARY KEY REFERENCES evaluation_decisions(id),
  released_by INTEGER REFERENCES users(id),
  released_at TEXT NOT NULL
);
CREATE TRIGGER eval_decisions_no_update BEFORE UPDATE ON evaluation_decisions BEGIN SELECT RAISE(ABORT, 'decisions are a permanent record; record a new decision instead'); END;
CREATE TRIGGER eval_decisions_no_delete BEFORE DELETE ON evaluation_decisions BEGIN SELECT RAISE(ABORT, 'decisions are a permanent record'); END;
"""

MIGRATIONS: list[tuple[int, str]] = [(1, SCHEMA_V1), (2, SCHEMA_V2), (3, SCHEMA_V3), (4, SCHEMA_V4), (5, SCHEMA_V5)]


def connect(path: str | Path) -> sqlite3.Connection:
    path = Path(path)
    if str(path) != ":memory:":
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    if str(path) != ":memory:":
        conn.execute("PRAGMA journal_mode=WAL")
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    return conn


@contextlib.contextmanager
def tx(conn: sqlite3.Connection, immediate: bool = True):
    """Explicit transaction. BEGIN IMMEDIATE takes the write lock up-front (race-safe counters)."""
    if conn.in_transaction:
        yield conn
        return
    conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
    try:
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def current_version(conn: sqlite3.Connection) -> int:
    conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)")
    row = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
    return row[0] or 0


def migrate(conn: sqlite3.Connection) -> list[int]:
    applied = []
    have = current_version(conn)
    for version, sql in MIGRATIONS:
        if version <= have:
            continue
        try:
            conn.executescript(
                "BEGIN;\n" + sql + f"\nINSERT INTO schema_migrations VALUES ({version}, datetime('now'));\nCOMMIT;"
            )
        except Exception:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        applied.append(version)
    return applied
