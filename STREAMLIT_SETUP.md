# Streamlit version — setup and upgrade (GitHub + Streamlit Community Cloud + Hugging Face dataset)

Result: a web address like `https://incubation-portal.streamlit.app`. Everyone only needs a browser. Cost: **₹0**.

| Service | What it does | Cost |
|---|---|---|
| GitHub | stores the code | free |
| Streamlit Community Cloud | runs the website | free |
| Hugging Face (private **dataset**, not a Space) | keeps all data safe across restarts | free |
| Groq / Gemini / OpenRouter … (optional) | AI-drafted replies to queries, staff AI assistant | free keys |
| Gmail or Brevo | emails | free |

> 🔒 Never upload the `private_do_not_upload` folder (it sits next to the app folder in the zip), `.env` or `secrets.toml` to GitHub. Your problem statements are imported inside the
> admin screen and stored only in your private Hugging Face dataset.

---

## ⬆ Already running the previous version? Upgrade in 10 minutes (data is kept)
1. Unzip `incubation_portal_streamlit.zip`.
2. Open your repository on GitHub → **Add file → Upload files**.
3. Open the unzipped `incubation_portal_streamlit` folder, select **everything inside it**, drag it into the page.
   GitHub replaces files with the same name. Wait until every file is listed → **Commit changes**.
4. Streamlit redeploys by itself (1–3 minutes). On the first start the database upgrades itself (schema v6); every team keeps its locked problem statement.
5. Sign in as admin → 📊 Dashboard → you should see the new filters and tabs.
6. Optional now: ⚙️ Programme Setup → **AI keys** (paste one or more keys) and → **Emails & reminders** → *Incubation inbox*.
7. The old `LLM_API_KEY` secret still works (used after the keys you paste). You can delete it.

Nothing else changes: same address, same Secrets, same users and data.

---

## 🆕 First-time setup

### Part A — Accounts (15 min)
1. **GitHub:** https://github.com/signup
2. **Streamlit:** https://share.streamlit.io → **Continue with GitHub** (same account).
3. **Hugging Face:** https://huggingface.co/join → confirm your email.
4. **Gmail App Password:** myaccount.google.com → Security → 2-Step Verification ON → search **App passwords** → create `Incubation Portal` → copy the 16 letters.
5. **AI key (optional, free):** e.g. Groq https://console.groq.com/keys or Google Gemini https://aistudio.google.com/apikey. You paste it later inside the portal.

### Part B — Hugging Face token (2 min)
1. https://huggingface.co/settings/tokens → **Create new token** → type **Write** → name `portal-data` → **Create** → copy (`hf_…`).
2. Your data will live in `your-hf-username/incubation-data` (created automatically, private).

### Part C — Code to GitHub (10 min)
1. Unzip `incubation_portal_streamlit.zip`.
2. GitHub → **+ → New repository** → `incubation-portal` → **Private** → **Create repository** → **uploading an existing file**.
3. Open the unzipped `incubation_portal_streamlit` folder → select **everything inside it** → drag into the page →
   wait → **Commit changes**. (GitHub takes at most 100 files per upload.)
4. Check that `app.py`, `requirements.txt` and the folders `cloud`, `workflow`, `rag` are at the **top level** of the repository.
5. To let students in later, the app must be public; if Streamlit cannot make it public from a private repository, make the
   repository public after checking its **Commits** history contains no secret file.

### Part D — Launch on Streamlit (10 min)
1. https://share.streamlit.io → **Create app** → **Deploy a public app from GitHub**.
   (Repository not listed? Click your name top-left → **Settings → Linked accounts → Connect** private repositories → **Authorize**.)
2. Repository `your-name/incubation-portal` · Branch `main` · Main file `app.py` · App URL e.g. `incubation-portal`.
3. **Advanced settings** → Python **3.12** → **Secrets**: paste the contents of `.streamlit/secrets.toml.example` and fill in
   `ADMIN_PASSWORD`, `SIGNUP_CODE`, `PUBLIC_URL`, `HF_TOKEN`, `HF_DATA_REPO`, and the Gmail lines (or the Brevo lines). → **Save** → **Deploy**.
4. First build: 3–6 minutes. Then: app **Share → Who can view → Public**. Test the link in an incognito window.

### Part E — First admin setup (20 min)
1. Sign in as `admin` (password = `ADMIN_PASSWORD`).
2. 📊 Dashboard must say **☁️ Data saved to Hugging Face dataset** (red warning → fix `HF_TOKEN`/`HF_DATA_REPO`, then **⋮ → Reboot app**).
3. 🔑 Users → choose `admin` → **Edit**: add your email → **Save changes**. ✉️ Notifications → **Send test email**.
4. ⚙️ Programme Setup:
   - **Tracks & problem statements:** upload `private_do_not_upload\ps_catalogue.csv` (from the zip, on your PC) → **Replace** → **Import catalogue**.
   - **Abstract format / template**, **Deadline & rules** (incl. *problem statements per team*), **Validation checks**.
   - **Emails & reminders:** programme name, portal address, **Incubation inbox** for queries (optional).
   - **Peer review rubric**, then **AI keys:** choose a provider → paste the key → **Add key** → **🧪 Test**. Add a second key from another provider for backup.
5. 📚 Knowledge Base → **Rebuild index** (used for AI-drafted query replies).
6. 🔑 Users → create reviewers (role reviewer + email).
7. 📜 Policy → **Back up now**.

### Part F — Trial run (20 min, incognito windows)
1. **Student:** sign up (code) → 👥 My Team: register, lock a problem statement, then **➕ Work on another problem statement** →
   📤 Submit: pick the problem statement → **Check my draft** → **Submit** → emails arrive. → 📨 **Ask the Incubation Team**: send a query.
2. **Admin:** 📬 **Queries** → open it → (✨ Draft with AI) → **Send reply**. Student receives the email.
3. **Admin:** 🎯 Peer Review → **2 · Assign** → Track → Problem statement → abstracts → reviewers → **Assign**.
4. **Reviewer:** 📝 My Reviews → pledge → accept → download → score → submit (acknowledgement emails at each step).
5. **Admin:** 4 · Decisions → 5 · Release. **Student:** 🏅 Results. **Admin:** 📊 Dashboard → filter and **Export CSV**.
6. Clean start for the real programme: change `HF_DATA_REPO` to e.g. `…/incubation-data-live` in Secrets → **Reboot app** → repeat Part E.

## Daily use
- Nothing to start: website, emails, reminders and data saving run by themselves.
- The app **sleeps after 12 hours without visitors**; the next visitor clicks *"Yes, get this app back up!"* (≈30 s). Open it daily during the programme so reminders keep going.
- Weekly safety copy: huggingface.co → your `incubation-data` dataset → **Files** → `db/incubation.db` → download.

## Troubleshooting
| You see | Fix |
|---|---|
| Build error / `ModuleNotFoundError` | A folder did not upload: upload it again (Part C3); Python must be 3.12 |
| Red "Data is NOT being saved" | `HF_TOKEN` must be **Write**; `HF_DATA_REPO` = `username/name`; Reboot app |
| "AI off" in the admin sidebar | No AI key yet: Programme Setup → AI keys (everything else works without AI) |
| "All AI keys are busy or failing" | Click 🧪 Test on each key; add a key from a second provider |
| Test email fails | Streamlit may block Gmail: switch Secrets to the Brevo lines |
| Others cannot open the link | App **Share → Public** (and repository public if needed) |
| "This account is not active" | An admin deactivated it: 🔑 Users → Edit → Active |
