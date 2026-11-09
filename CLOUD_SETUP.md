# Put the portal online for free (GitHub + Streamlit Cloud + Hugging Face)

Result: a web address like `https://incubation-portal.streamlit.app`. Students, reviewers and the admin only need a browser.
You install **nothing** on your PC. Everything is done in the browser. Cost: **₹0**.

| Service | What it does | Free? |
|---|---|---|
| **GitHub** | stores the code (private) | yes |
| **Streamlit Community Cloud** | runs the website | yes |
| **Hugging Face** | keeps the data safe (private dataset) | yes |
| **Groq** (optional) | AI answers in the Policy Assistant | yes |
| **Gmail** (or Brevo) | sends the emails | yes |

> 🔒 Your confidential `private\ps_catalogue.csv` is **never** uploaded to GitHub (`.gitignore` blocks it). You upload it
> later inside the admin screen, and it is then stored only in your private Hugging Face dataset.

---

## Part A — Create the accounts (15 min)
1. **GitHub:** https://github.com/signup
2. **Streamlit:** https://share.streamlit.io → **Continue with GitHub** (use the same GitHub account).
3. **Hugging Face:** https://huggingface.co/join (confirm the email).
4. **Groq** (optional, for AI answers): https://console.groq.com → sign in → **API Keys → Create API Key** → copy it (`gsk_…`).
5. **Gmail App Password:** myaccount.google.com → Security → 2-Step Verification ON → search **App passwords** →
   create `Incubation Portal` → copy the 16 letters.

## Part B — Hugging Face token (2 min)
1. https://huggingface.co/settings/tokens → **Create new token** → type **Write** → name `portal-data` → **Create** → copy (`hf_…`).
2. Note your Hugging Face username (top-right avatar), e.g. `priya123`. Your data will go to `priya123/incubation-data`.
   It is created automatically and kept **private**.

## Part C — Upload the code to GitHub (10 min, no Git needed)
1. Unzip `incubation_portal.zip` on your PC.
2. GitHub → **+ → New repository** → name `incubation-portal` → **Private** → **Create repository**.
3. On the new page click **uploading an existing file**.
4. Open the unzipped `incubation_rag` folder. Select **everything except the `tests` folder** (GitHub accepts at most 100 files
   per upload) and drag it into the GitHub page → wait until all files are listed → **Commit changes**.
5. Optional: **Add file → Upload files** again and drag the `tests` folder → **Commit changes**.
6. Check that `app.py`, `requirements.txt` and the folders `cloud`, `workflow`, `rag` … are at the top level of the repository.
   - Hidden folders (`.streamlit`, `.github`) may not upload from the browser. That is fine: the portal works without them.
   - The `private` folder is never needed on GitHub (and `.gitignore` keeps it out when you use Git).

## Part D — Start the website on Streamlit Cloud (10 min)
1. https://share.streamlit.io → **Create app** → **Deploy a public app from GitHub**.
2. **Repository:** `your-github-name/incubation-portal` · **Branch:** `main` · **Main file path:** `app.py`.
3. **App URL:** type a name, e.g. `incubation-portal` → your address becomes `https://incubation-portal.streamlit.app`.
4. Click **Advanced settings**:
   - **Python version:** `3.12`
   - **Secrets:** open `.streamlit/secrets.toml.example` from the zip, copy it all, paste it here and fill in your values:
     - `ADMIN_PASSWORD` (8+ characters), `SIGNUP_CODE`, `PUBLIC_URL` = the address from step 3
     - `HF_TOKEN` (Part B) and `HF_DATA_REPO` = `your-hf-username/incubation-data`
     - Gmail lines: `SMTP_USER`, `SMTP_PASSWORD` (16 letters, no spaces), `SMTP_FROM`
     - `LLM_API_KEY` = your Groq key (or delete the two LLM lines to run without AI answers)
   - **Save**.
5. Click **Deploy**. The first build takes 3–6 minutes (it installs the packages). Then the sign-in page appears.
6. Make the address open for students: app page → **Share** (top right) → **Who can view this app: Public**
   (only if it shows as private).

## Part E — First sign-in and setup (20 min)
1. Open your address → **Sign in** as `admin` with `ADMIN_PASSWORD`.
2. **📊 Dashboard** must show **☁️ Data saved to Hugging Face dataset …**. If it shows a red warning, check `HF_TOKEN` / `HF_DATA_REPO`
   in Secrets (Part D4) → app menu **⋮ → Reboot app**.
3. **🔑 Users → Save email** for `admin`; **✉️ Notifications → Send test email** → check inbox and Spam.
4. **⚙️ Programme Setup → Tracks & problem statements:** upload your `private\ps_catalogue.csv` → **Replace** → **Import catalogue**
   (6 tracks, 15 problem statements).
5. Other tabs: template, deadline, validation checks, emails, peer-review rubric → **Save** on each.
6. **📚 Knowledge Base → Rebuild index**.
7. **🔑 Users:** create reviewers (role *reviewer*, with email). They accept the confidentiality pledge on first visit to 📝 My Reviews.
8. **📜 Policy → Back up now** → Dashboard shows the upload time. Your setup is safe.

## Part F — Test it like a user (15 min)
Use phone or incognito windows:
1. **Student:** sign up with the code → 👥 My Team (register, lock problem statement) → 📤 Submit → **🔍 Check my draft** (free, no attempt)
   → fix → **Submit** → emails arrive.
2. **Admin:** 🎯 Peer Review → **Auto-assign**. **Reviewer:** 📝 My Reviews → pledge → accept → download → score → submit.
3. **Admin:** Decisions → Release. **Student:** 🏅 Results.
4. Clean start before going live: create a new Hugging Face dataset name (e.g. `incubation-data-live`), change `HF_DATA_REPO` in Secrets,
   change `ADMIN_PASSWORD` if you like → **Reboot app** → repeat Part E.

## Part G — Daily use and updates
- **Nothing to start.** The website, emails, reminders and data uploads all run by themselves.
- **Sleep:** if nobody opens the portal for 12 hours it sleeps; the next visitor clicks **"Yes, get this app back up!"** (about 30 s).
  Reminders pause while it sleeps, so open the portal once a day during the programme.
- **Data:** saved to Hugging Face within about a minute of every change. Download a copy any time: huggingface.co → your
  `incubation-data` dataset → **Files** → `db/incubation.db`.
- **Change code:** edit the file on GitHub (pencil icon) → **Commit** → Streamlit redeploys in about a minute. Data stays.
- **Change a secret:** share.streamlit.io → your app **⋮ → Settings → Secrets** → Save (restarts the app).

## If something goes wrong
| You see | Fix |
|---|---|
| Build error mentioning a package | **Manage app** (bottom right) → read the log; make sure Python is 3.12 (app ⋮ → Settings) |
| "ModuleNotFoundError: cloud" | `cloud` folder missing in GitHub: upload it (Part C4) |
| Red "Data is NOT being saved" | HF_TOKEN must be a **Write** token; HF_DATA_REPO must be `username/name`; Reboot app |
| "AI service not reachable" | wrong `LLM_API_KEY`, or delete the LLM lines (portal still works, keyword search only) |
| Test email fails with SMTP | use the Brevo lines in Secrets instead (EMAIL_PROVIDER = "brevo") |
| Cannot sign in as admin | `ADMIN_PASSWORD` only creates the admin on the **first** start of a new dataset |
| Students see "You do not have access" | Share → Who can view → Public |

The PC version (Ollama on your own computer) still works: double-click `install_windows.bat`, then `start_portal.bat`.
