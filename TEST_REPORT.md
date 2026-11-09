# Test Report — 2026-10-27 (updated; original run 2026-10-13) (build environment: Linux, Python 3.13.16, Ollama NOT available)

## Automated
| Command | Result |
|---|---|
| `ruff check .` | All checks passed |
| `python -m pytest` | **634 passed, 0 failed, 0 skipped** (~60 s) |

| File | Tests | Covers |
|---|---|---|
| test_registration.py | 8 | team sizes 1/2/3/4, membership lock (service + DB triggers), member in two teams, PS/domain consistency, PS lock, role |
| test_submission.py | 19 | DOCX/PPTX/PDF pass, pre-checks don't consume attempts (ext, empty, MIME, renamed, oversize), corrupted/empty content, missing sections, min-words boundary (149 fail / 150 pass), 3 attempts + 4th blocked + history kept, fail→pass resubmission, concurrency (6 parallel first submits → 1; 5 parallel 3rd attempts → exactly 1), deadline inclusive/exclusive + IST/UTC, immutability |
| test_similarity_review.py | 13 | threshold 0.49/0.50/>0.50/None, exact 50% not flagged, no-corpus honesty, >50% → PENDING_REVIEW, student redaction, unauthorized review denied+audited, clear/corrections/reject-needs-approved-policy, decisions immutable, low relevance → review |
| test_states_security_db.py | 499 | all 484 state×state×actor transition combinations (allowed and forbidden), path traversal, passwords, RBAC, no password in audit, append-only audit, migration idempotency + restart persistence, deadline sweep, configurable precedence |
| test_rag.py | 10 | approved-only indexing, citations from retrieved sources, abstention, reviewer-doc isolation, citation verification (invalid tags stripped, uncited, INSUFFICIENT_EVIDENCE), FAISS persistence + rebuild detection (deterministic test embedder), eval metrics, Ollama unreachable / model missing / down mid-request |
| test_offline_notifications.py | 4 | full workflow + RAG with non-loopback sockets blocked (0 external attempts), outbox queue/dedupe, no false "sent", SMTP failure recorded, SMTP success |
| test_setup.py | 34 | catalogue CSV/XLSX/JSON parsing + aliases, 8 malformed-file cases, replace/merge import, locked team keeps PS, hidden PS not selectable, locked PS can't move track, admin-only, survives restart, published to assistant; template heading detection DOCX/PPTX/PDF, new rules enforced on submission + version recorded, settings validation (6 cases), deadline/rejection-policy changes; KB upload/archive/retire/rejections/admin-only; v1→v2 migration without data loss |
| test_requirements_v2.py | 45 | the 7 requested features: track relevance via keywords (+ uploaded track keywords), 4 required headings incl. inline headings, per-section content, all 3 file types, whole-document length, English-only (Hindi/Telugu/French rejected), similarity to every PS in the database, exact-copy rejection (incl. DOCX→PDF), review mode, near-duplicate review, own re-upload blocked without using an attempt, first submitter never penalised; emails: acknowledgements to contact+members, failure guidance, reviewer alert, decisions, deadline reminders (windows, no repeats), review reminders (daily, switchable), duplicate alert, delivery-failure alert (once, non-recursive), real SMTP delivery via local aiosmtpd server, SMTP down→recovery, max-attempt cap, worker pass, settings validation |
| test_ui.py | 2 | AppTest: sign-up → login → register → PS lock → submit page via widgets; every page for admin/reviewer/student renders without exception; chat shows citation |

Total: 634 (per-file counts above were recorded at earlier runs; suites have grown since).

## Browser check of upload screens (headless Chromium + live Streamlit)
Admin uploaded a 3-row tracks CSV (preview → import) and a DOCX template (4 headings detected → saved); student then saw only the uploaded tracks, the new required sections and the template download button. No outbound network attempts after adding `.streamlit/config.toml` (Streamlit telemetry and external-IP lookup were attempted before it).

## Evaluation (`scripts/evaluate.py`, BM25-only because Ollama absent; synthetic sample KB)
RAG k=5, n=10: precision@5 **0.771**, recall@5 **1.0**, unanswerable abstention **1.0**, false abstention **0.0**, retrieval latency ≈0.04 ms.
Hard checks on 8 labeled synthetic docs: accuracy 1.0, false rejection 0.0, missed violation 0.0.
⚠ These are on small synthetic sets written alongside the code — not evidence of real-world accuracy.

## Acceptance (`scripts/acceptance.py`, fresh DB)
PASS: registration+PS lock · passing PDF · failed DOCX → passing PPTX resubmission · similarity 100% → PENDING_REVIEW · student review denied, reviewer decision recorded · 3rd attempt DROPPED, 4th blocked (ATTEMPTS_EXHAUSTED) · persistence after reconnect · cited RAG passages from approved docs · abstention on unsupported question.
Streamlit launched headless: `/_stcore/health` = ok, `/` = HTTP 200.

## Not verified (environment)
- Live Ollama chat generation, `nomic-embed-text` embeddings, semantic similarity with a real model, answer faithfulness of a real LLM.
- Real SMTP delivery (mocked only).
- Manual browser click-through (covered by AppTest instead); OS-level network disconnect (simulated by socket guard).

## 2026-10-27 — seven requested checks/features, verified in a real browser with a real SMTP server
Fresh database, live Streamlit, headless Chromium, local aiosmtpd mail server:
- Team 1 registered, locked AG-01, submitted a valid DOCX → ACCEPTED.
- Team 2 uploaded the same text as a **PDF** → `DUPLICATE_SUBMISSION` fail (attempt 1, resubmission allowed); admin received the duplicate alert.
- Team 2 uploaded a Hindi abstract → `LANGUAGE` fail; counters showed 2 / 3 immediately (stale-counter bug found and fixed during this run).
- 21 emails accepted by the SMTP server: registration and lock acknowledgements, submission receipts, results with guidance (contact + member), duplicate alert to admin. Outbox: all `sent`, none failed.
- `scripts/acceptance.py` on a fresh DB: all 12 steps PASS (incl. exact-copy rejection, own re-upload blocked, emails for every workflow event).
Still not verified here: Ollama models (not installed in this environment), delivery through a real external provider (Gmail/Outlook).

## 2026-10-27 — internet deployment (Docker, Hugging Face Space, GitHub)
- `python -m pytest`: **653 passed**, 0 failed, 0 skipped; `ruff check .` clean; `shellcheck` clean on both start scripts;
  `hadolint` clean on both Dockerfiles (ignoring unpinned apt/pip/base-image versions); `docker compose config` valid (incl. GPU override and tunnel profiles);
  GitHub workflow YAML parses.
- New tests (`tests/test_deployment.py`, 13): bucket snapshot backup/restore after a simulated disk wipe, damaged newest copy → older copy used,
  live DB never overwritten, background backup after changes; Brevo email API against a local HTTP server (delivery, wrong key stops the pass,
  bad recipient fails only that message); sign-up code; login lockout and expiry; public URL in emails; generated example template; no binary files in the repo.
- **Start-up script run for real (outside Docker, AI disabled):** started with persistent storage, admin created from the secret, wrong sign-up code refused
  in the browser, account + team created, backup written within seconds; TERM → final backup; **local disk wiped → restart restored the database and 7 files;
  the student signed in and the team was intact**; emails carried the public `https://…hf.space` address.
- **Not verified here:** building the Docker images (this environment cannot download base images from Docker Hub), running Ollama inside the container,
  the actual Hugging Face build/runtime, Storage Bucket mounting, real Brevo delivery, Cloudflare tunnels.

## 2026-10-28 — upgrade of a real installation (user's copy, schema v3 → v4)
- User's code had no local edits; only data (database, uploads, template, generated KB documents). KB documents carried into the package.
- Rehearsed in-place upgrade on a copy of the real data: `init_db.py` → schema v4, integrity ok; users/teams/submissions/settings/catalogue
  counts unchanged; custom rules (version `+admin.r4`) and the uploaded template kept; every page opened for admin, reviewer and student.
- Fixes found: template link broke when the folder moved (stored full Windows path) → fallback lookup in the current storage folder;
  26 never-sent test emails would all go out once email works → admin "Discard waiting emails"; with a larger KB a single generic word
  ("team") counted as an answer → keyword hits must contain at least half of the question's words (`rag.min_query_coverage`, 0.5).
- Retrieval on the labelled set (BM25): sample KB precision@5 0.958, recall@5 0.875, unanswerable abstention 1.0; user's KB precision@5 0.713,
  recall@5 0.875, abstention 1.0. The two missed sources are secondary documents; every answerable question still retrieves its main document.
- `python -m pytest`: 664 passed with the sample KB and with the user's KB.
