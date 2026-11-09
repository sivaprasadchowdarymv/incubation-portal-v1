# Getting started — complete beginner guide (Windows, no Docker)

> Want it online for free with nothing installed? Use **CLOUD_SETUP.md** instead.
> Shortcut on Windows: after installing Python and Ollama (Part 1), double-click `install_windows.bat` (replaces Part 2 and the
> database step), fill `.env` (Part 3), then double-click `start_portal.bat` (one window runs the website, emails and reminders).

Follow the parts in order. Every step says what you should see (✅) and what to do if not (❌).
Commands go into **PowerShell**: type (or paste) the line and press **Enter**.

> 🔒 **Confidential data:** your problem statements are in `private/ps_catalogue.csv`. The `private` folder is excluded
> from Git/GitHub and Docker. Don't email or upload it anywhere; import it only into your own portal.

---

## Part 1 — Install the tools (once, about 30 minutes)

**1.1 Python**
1. Go to https://www.python.org/downloads/windows/ and download **Python 3.12 (64-bit)**.
2. Run it. ⚠️ Tick **"Add python.exe to PATH"** on the first screen → **Install Now**.
3. Open PowerShell (Windows key → type `powershell` → Enter) and run `python --version`. ✅ `Python 3.12.x`

**1.2 Ollama (the local AI)**
1. Download from https://ollama.com/download → run `OllamaSetup.exe` → Install. A llama icon appears near the clock.
2. In PowerShell (about 4.5 GB download, wait for `success` after each):
   ```
   ollama pull mistral
   ollama pull nomic-embed-text
   ollama list
   ```
   ✅ both models listed. (8 GB RAM computer? Use `ollama pull llama3.2` instead of mistral.)

**1.3 Gmail App Password (for the portal's emails)**
1. Sign in to the Gmail account the portal will send from → https://myaccount.google.com → **Security**.
2. Turn on **2-Step Verification**. Then search **"App passwords"** → name it `Incubation Portal` → **Create**.
3. Copy the 16-letter code (shown only once). Keep it private.

---

## Part 2 — Install the portal (about 10 minutes)

1. Right-click the zip → **Extract All…** → type `C:\incubation` → **Extract**.
   You now have `C:\incubation\incubation_rag` (contains `app.py`).
2. Open that folder in File Explorer → click the address bar → type `powershell` → Enter.
3. Run, one line at a time:
   ```
   python -m venv .venv
   .venv\Scripts\Activate.ps1
   pip install -r requirements.txt
   ```
   ❌ "running scripts is disabled": run `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`, answer `Y`, repeat line 2.
   ✅ the prompt starts with `(.venv)` and pip ends with `Successfully installed …`.

> Every time you open a new PowerShell for the portal, run `.venv\Scripts\Activate.ps1` first.

---

## Part 3 — Settings (about 5 minutes)

1. Create the settings file and open it:
   ```
   copy .env.example .env
   notepad .env
   ```
2. Fill in (no `#` in front, no spaces around `=`, App Password without spaces):
   ```
   EMAIL_PROVIDER=smtp
   SMTP_HOST=smtp.gmail.com
   SMTP_PORT=587
   SMTP_USER=your.portal.address@gmail.com
   SMTP_PASSWORD=abcdefghijklmnop
   SMTP_FROM=your.portal.address@gmail.com
   SMTP_TLS=true
   SMTP_SSL=false
   SIGNUP_CODE=choose-a-code-for-students
   ```
   Optional: `OLLAMA_CHAT_MODEL=llama3.2` if you pulled the small model. Save (**Ctrl+S**), close.
3. Check the portal can read it:
   ```
   python -c "from config.settings import get_settings as g; s=g(); print(s.smtp_host, s.smtp_from)"
   ```
   ✅ prints `smtp.gmail.com your.portal.address@gmail.com`. ❌ `None` → fix the line in `.env`
   (if the file shows as `.env.txt`, run `Rename-Item .env.txt .env`).

---

## Part 4 — First-time setup (about 5 minutes)

```
python scripts/check_models.py
```
✅ `"chat_ok": true` and `"embed_ok": true` (if not: start Ollama from the Start menu / run the `ollama pull` it shows).

```
python scripts/init_db.py --admin-user admin
```
Type a new admin password (8+ characters; nothing appears while typing). ✅ `Admin 'admin' created.` **Write the password down.**

Import your tracks & problem statements (already converted from your dashboard into `private\ps_catalogue.csv`):
```
python scripts/import_catalogue_csv.py private\ps_catalogue.csv
```
✅ `Imported: 6 tracks, 15 problem statements …`.

*Got a newer version of the dashboard HTML later?* Convert it with
`python scripts/import_dashboard.py "C:\path\PS_dashboard.html" --import` (writes a fresh `private\ps_catalogue.csv` and imports it).

```
python scripts/ingest.py --force
```
✅ ends with `"semantic": true` (the assistant now knows your guidelines and problem statements).

```
python -m pytest
```
✅ last line: `… passed` (about 2 minutes; the tests never use your real email or data).

---

## Part 5 — Start the portal (every day)

**Window 1 (portal):**
```
.venv\Scripts\Activate.ps1
streamlit run app.py
```
Open **http://127.0.0.1:8501** in Chrome/Edge. Keep the window open.

**Window 2 (email worker — reminders and retries):** open a second PowerShell in the same folder:
```
.venv\Scripts\Activate.ps1
python scripts/notify_worker.py --loop 300
```
Stop either with **Ctrl + C**.

---

## Part 6 — Admin setup in the browser (once, about 20 minutes)

Sign in as `admin`. The left menu has: 📊 Dashboard, ⚙️ Programme Setup, 🧑‍⚖️ Review Queue, 📄 Submissions, 🎯 Peer Review, 🔑 Users,
📚 Knowledge Base, ✉️ Notifications, 🧾 Audit Log, 📜 Policy.

1. **🔑 Users → Save email:** choose `admin`, type your email, **Save email**. 📧 confirmation arrives.
2. **✉️ Notifications → Send test email** to yourself. ✅ "Test email accepted by smtp.gmail.com". Check Spam too.
3. **🔑 Users:** create each reviewer (role *reviewer*, with email). Give them their password yourself.
4. **⚙️ Programme Setup**
   - *Tracks & problem statements:* check that your 6 tracks / 15 problem statements are listed
     (to change them: edit `private\ps_catalogue.csv` in Excel → upload it here → **Replace** → **Import catalogue**).
   - *Abstract format / template:* upload your Word template → check the detected headings → set min/max words → **Save**.
   - *Deadline & rules:* deadline like `2026-11-30T23:59:59` (India time) → **Save rules**.
   - *Validation checks:* review (2 keywords, words per heading, exact copies rejected) → **Save checks**.
   - *Emails & reminders:* **Programme name** (shown in the portal header and every email), portal address → **Save**.
   - *Peer review rubric:* reviewers per abstract (2), days to review (7), blind review ON, the 5 weighted criteria
     (edit the table) → **Save peer-review settings**.
5. **📚 Knowledge Base:** upload official guidelines/FAQ (approved, visible to student) → **Rebuild index**.

---

## Part 7 — Trial run (about 15 minutes)

Use a **private/incognito window** for the student (keep admin in the normal window).

1. **Student sign-up** (with the sign-up code) → sign in → **👥 My Team**:
   the progress bar shows *Account ✓ → Team → Problem statement → Submitted → Result*.
2. Register the team (2–3 members) → 📧 confirmation.
3. **Choose your track and problem statement:** read the card (summary, challenges, impact); **Browse all** to compare
   → tick the box → **Confirm and lock** → 📧 confirmation.
4. **📤 Submit:** download the official template, fill it, upload → **Submit**.
   📧 "Submission received (attempt 1 of 3)" + 📧 result (accepted / fix & resubmit / under review).
5. Admin → **📄 Submissions** → open the report → **✉️ Send an update to this team** → student sees it under
   **🗂️ History → Messages from the organisers** and gets an email.
6. Admin → **✉️ Notifications → 📣 Send an announcement** to a group of teams.
7. Reviewer → **🧑‍⚖️ Review Queue** for anything flagged → record a decision (team is emailed).

**Peer review of accepted abstracts (conference style)**

8. Admin → **🎯 Peer Review → 1 · Reviewers:** set each reviewer's expertise tracks and max load; record any conflicts of interest.
9. **2 · Assign:** **⚡ Auto-assign** (experts first, fair load, conflicts skipped) or pick reviewers manually + due date.
   📧 each reviewer gets "New abstract to review: P-0001".
10. Reviewer (another incognito window) → **📝 My Reviews** → **Accept** (or Decline / conflict of interest) →
    **⬇ Download abstract** → score the 5 sliders, recommendation, confidence, comments for the team (+ confidential note) →
    **Save draft** or **Submit review**. 📧 receipt. After submitting they can read the other reviews and revise.
11. Admin → **3 · Progress:** who is late; send reminders; withdraw and reassign. (Window 2 sends reminders 48 h / 24 h before the due date and daily when overdue.)
12. **4 · Decisions:** see all reviews side by side (mean, spread, ⚠ disagreement) → **Shortlisted / Waitlisted / Not shortlisted** + message.
13. **5 · Results & release:** ranking per track, **Export CSV**, then **📣 Release all pending results** →
    📧 every team gets its result with anonymised comments; students see it under **🏅 Results**.

**Go live with a clean database:** stop both windows → rename `storage` to `storage_trial` →
run Part 4 again (init_db, import, ingest) → Part 5 → Part 6 → share the address + sign-up code with students.

---

## Part 8 — Daily care

| Task | How |
|---|---|
| Start / stop | Part 5 / Ctrl + C |
| Backup | stop the portal → copy the `storage` folder (name it with the date) to another drive |
| Emails not going | ✉️ Notifications shows *Failed* + reason; fix `.env`, restart Window 1, **Send pending emails now** |
| After the deadline | 📜 Policy → **Run deadline sweep now** |

## Problems & fixes

| You see | Do this |
|---|---|
| `ModuleNotFoundError` | `.venv\Scripts\Activate.ps1` first |
| "Email: not set" | Part 3 step 3, then Ctrl+C in Window 1 and start it again |
| `535 … Username and Password not accepted` | use the 16-letter App Password, no spaces |
| 🔴 Ollama in the sidebar | start Ollama from the Start menu |
| "Index missing or stale" | 📚 Knowledge Base → **Rebuild index** |
| A good abstract goes to review for "domain" | add better keywords in `private\ps_catalogue.csv`, re-import (Part 6.4) |
| "Too many failed sign-in attempts" | wait 15 minutes |

Online access (internet URL, Docker, Hugging Face) is optional and comes later: see `DEPLOYMENT.md`.
