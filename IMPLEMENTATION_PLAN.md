# Implementation Plan

## Audit (2026-10-13)
- Repository: empty (no git, no prior code/DB) → new project created per master structure; nothing overwritten.
- Python 3.13.16; streamlit 1.65.0, faiss-cpu 1.15.1, numpy 2.5.3, pandas 3.0.5, pypdf 5.9.0, python-docx 1.2.0, python-pptx 1.0.2, rank-bm25 0.2.2, pytest 9.1.1; ruff installed for lint.
- Ollama: **not installed / not reachable** in the build environment → LLM generation and Ollama embeddings could not be exercised live.

## Modules
| Module | Responsibility |
|---|---|
| `config/` | settings from env/.env, versioned `policy.json` |
| `database/db.py` | connection (FK, WAL, busy timeout), `tx()` (BEGIN IMMEDIATE), versioned migrations, integrity triggers |
| `security/auth.py` | PBKDF2 passwords, roles, safe file names |
| `workflow/states.py` | explicit state machine with actor permissions |
| `workflow/services.py` | users, registration, PS lock, submission gate, submit+validate, review, deadline sweep, audit, reports |
| `validation/` | pre-checks (ext/size/MIME/zip), extraction PDF/DOCX/PPTX, hard checks, relevance, exact/lexical/semantic similarity |
| `rag/` | ingestion+chunking with metadata, FAISS+BM25 RRF, Ollama client, grounded answers + citation verification |
| `notifications/` | persistent outbox, dedupe, optional SMTP |
| `evaluation/` | metrics, workflow summary, RAG + hard-check evaluation, synthetic documents |
| `app.py` | Streamlit UI (all actions call services) |
| `scripts/` | init_db, check_models, ingest, evaluate, make_samples, acceptance |

## Stages (all completed)
1 audit · 2 DB+policy · 3 registration/state machine · 4 extraction/validation · 5 RAG/Ollama · 6 similarity/review ·
7 notifications/audit/metrics · 8 tests+fixes · 9 offline verification/packaging · 10 docs.

## Database
Tables: users, domains, problem_statements, teams, team_members, submissions, submission_status_history, validation_results,
similarity_findings, review_cases, review_decisions, notifications, policy_versions, audit_events, metrics, schema_migrations.
Migration v1. Future changes: append `(2, sql)` to `MIGRATIONS`; never edit applied migrations.

## Acceptance criteria
See README "Key guarantees" and TEST_REPORT.md; all automated acceptance steps pass.

## v3 (2026-10-27): requested checks and emails
- Checks 1–6 (domain relevance with track keywords, 4-section format + per-section content, document length, English, similarity with all problem statements, duplicate rejection) — `validation/checks.py`, `workflow/services.py`.
- Email acknowledgements/alerts/reminders — `notifications/service.py`, `notifications/outbox.py` (STARTTLS/SSL, test email), `scripts/notify_worker.py`.
- Migration v3: `users.email`, `domains.keywords`. Admin UI: Programme Setup → Validation checks / Emails & reminders; Notifications page.
- Decision recorded: exact duplicates of another team are rejected automatically (configurable); similarity alone still never rejects.
