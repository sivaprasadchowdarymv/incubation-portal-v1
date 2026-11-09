# Troubleshooting
| Symptom | Fix |
|---|---|
| "Ollama unreachable" in sidebar | `ollama serve`; check `OLLAMA_BASE_URL`; `python scripts/check_models.py` |
| Model missing | `ollama pull mistral` / `ollama pull nomic-embed-text` (or set `OLLAMA_CHAT_MODEL`) |
| Assistant says index missing/stale | `python scripts/ingest.py` (auto-detects KB changes; `--force` to rebuild) |
| Index built "BM25-only" | embed model was unavailable at ingest; start Ollama and `python scripts/ingest.py --force` |
| `database is locked` | another process holds a write; WAL + 30 s busy timeout normally handles it; avoid network drives |
| `submission content is immutable` / `membership is locked` | intended DB guarantees; do not edit rows manually |
| Valid file "MIME_MISMATCH" | file renamed from another format; re-export from Word/PowerPoint/PDF tool |
| Scanned PDF fails CONTENT_NONEMPTY | OCR is not included; submit a text-based PDF/DOCX |
| Deadline appears wrong | `deadline` is interpreted in `timezone` (Asia/Kolkata) unless it has an explicit offset |
| Windows `ZoneInfoNotFoundError` | `pip install tzdata` |
| Notifications stay `queued_local` | expected when SMTP not configured; set SMTP_* and use "Attempt SMTP delivery" |
| Catalogue upload rejected | Read the row-numbered errors; required columns `track_id, track_name, ps_id, ps_title`; IDs = letters/digits/-/_ (max 20) |
| New tracks not known to the assistant | Knowledge Base → Rebuild index |
| PS cannot move to another track | Teams have locked it; give the new one a new `ps_id` |
| Test email fails `SMTPAuthenticationError` | Gmail needs 2-step verification + an App password; Microsoft 365 tenants may block SMTP AUTH (ask IT) |
| Test email fails `ConnectionRefusedError` / timeout | Wrong host/port, firewall, or no internet; check SMTP_PORT (587 with SMTP_TLS=true, or 465 with SMTP_SSL=true) |
| Emails stay "queued_local" | SMTP not configured, or the worker isn't running; use Notifications → Send pending emails now |
| No deadline reminders | Worker not running, reminder hours not yet reached, or every team is already accepted/under review |
| Abstract fails "Missing required sections" although content exists | Put each heading on its own line (or `Methodology: ...`); alternative names can be added in Programme Setup → Abstract format |
| Many abstracts go to review for PS_SIMILARITY | Add better keywords/descriptions to the problem statements, or raise the tolerance in Validation checks |
