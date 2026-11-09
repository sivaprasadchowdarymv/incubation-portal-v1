# Offline Deployment (single machine pilot)

## While online (once)
```bash
pip download -r requirements.txt -d wheels         # optional: for air-gapped install
pip install -r requirements.txt
ollama pull mistral && ollama pull nomic-embed-text # stored in ~/.ollama/models
```
## Offline
```bash
pip install --no-index --find-links wheels -r requirements.txt   # if installing on the offline box
ollama serve
python scripts/check_models.py && python scripts/init_db.py && python scripts/ingest.py
streamlit run app.py --server.address 127.0.0.1 --browser.gatherUsageStats false
```
`.streamlit/config.toml` disables Streamlit usage telemetry and binds to 127.0.0.1 (without it Streamlit contacts data.streamlit.io and checkip.amazonaws.com). No runtime call leaves the machine: Ollama is local, SMTP is off unless `SMTP_HOST` is set. Verified by `tests/test_offline_notifications.py` (non-loopback sockets blocked).

## Security
- Bind Streamlit and Ollama to `127.0.0.1`. For LAN use, put a TLS reverse proxy in front and firewall port 11434.
- `storage/` holds DB + uploads (files created with 0600); restrict the folder to the service account.
- Change the admin password after first login; create reviewers from the Users page.
- Uploaded files are parsed, never executed; archive size and MIME are checked first.

## Backup / restore
```bash
sqlite3 storage/incubation.db ".backup 'backup/incubation-$(date +%F).db'"   # safe while running (WAL)
tar czf backup/uploads-$(date +%F).tgz storage/uploads config kb
```
Restore: stop app, copy DB back, restore uploads, start app. The index is rebuildable (`python scripts/ingest.py --force`).

## Email while offline
Emails need a reachable mail server. Without one, every acknowledgement/alert/reminder is kept in the outbox (Notifications page) and sent automatically by `scripts/notify_worker.py` when the connection returns. A mail server inside the campus network works without internet.
