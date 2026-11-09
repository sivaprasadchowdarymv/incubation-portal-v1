# Putting the portal on the internet (Docker · GitHub · Hugging Face)

This guide gives the portal a web address that students can open from anywhere. There are two routes:

| | **Route A: Hugging Face Space** | **Route B: your own PC/server + Docker + Cloudflare** |
|---|---|---|
| Address | `https://<your-hf-name>-incubation-portal.hf.space` | `https://random-words.trycloudflare.com` (free, changes on restart) or `https://portal.your-domain.in` |
| Where it runs | Hugging Face's servers (2 CPUs, 16 GB RAM) | Your computer (must stay switched on) |
| Cost | **Hugging Face PRO, about $9/month.** Since July 2026 Docker Spaces need a paid plan | Free (a permanent address needs your own domain, a few hundred rupees a year) |
| AI model | Small model `qwen2.5:1.5b` (the free CPU is slow for bigger ones) | Any model your PC can run (`mistral` by default) |
| Email | Normal email (SMTP) is **blocked** on Hugging Face, so use **Brevo** (free, 300 emails/day) | Gmail / Outlook / college mail server as before, or Brevo |
| Updates | Push to GitHub → tested and deployed automatically | `docker compose up -d --build` |

Words used below:
- **Docker:** packs the whole portal (Python, Ollama, AI models, code) into one box called a *container*, so it runs the same everywhere.
- **GitHub:** online home for your code. **Hugging Face Space:** a hosted app that builds and runs your Docker box and gives it a URL.
- **Secret:** a private setting (passwords, keys) stored on the website, never in the code.

---

## Route A: Hugging Face Space (step by step)

### A1. Create the accounts (about 15 minutes)
1. **GitHub:** https://github.com/signup (free).
2. **Hugging Face:** https://huggingface.co/join. Then subscribe to **PRO** at https://huggingface.co/pricing; Docker Spaces need it.
3. **Brevo** (for emails): https://www.brevo.com, free plan, no card.

### A2. Install Git on your computer (once)
- Windows: https://git-scm.com/download/win → run the installer with the default options.
- Check in PowerShell: `git --version` shows a version.
- Tell Git who you are (once):
  ```
  git config --global user.name "Your Name"
  git config --global user.email "you@example.com"
  ```

### A3. Put the code on GitHub
1. On GitHub click **+ → New repository**. Name: `incubation-portal`. Choose **Private**. Do **not** tick "Add a README". Click **Create repository**.
2. Open PowerShell in the unzipped project folder (`C:\incubation\incubation_rag`) and run, replacing `YOUR-GITHUB-NAME`:
   ```
   git init
   git add .
   git commit -m "Incubation portal"
   git branch -M main
   git remote add origin https://github.com/YOUR-GITHUB-NAME/incubation-portal.git
   git push -u origin main
   ```
3. When asked to sign in, a browser window opens: approve it. If it asks for a *password*, GitHub needs a **personal access token** instead: GitHub → Settings → Developer settings → Personal access tokens → Generate (tick `repo`). Paste the token as the password.
4. Refresh the GitHub page: your files are there. `.env` (your passwords) and `storage/` (your data) are **not** uploaded; `.gitignore` excludes them on purpose.

### A4. Create the Space
1. Go to https://huggingface.co/new-space.
2. **Space name:** `incubation-portal`. **SDK:** **Docker**, template **Blank**. **Hardware:** CPU basic. **Visibility: Public**, so students can open it without a Hugging Face account. The code becomes visible, but your passwords and data do not.
3. Click **Create Space**. Leave the page empty; GitHub will fill it in step A8.

### A5. Keep your data: attach a Storage Bucket (very important)
A Space's own disk is **wiped every time it restarts**. Without this step all registrations and submissions are lost on every restart or update.

1. Create a private bucket: https://huggingface.co/new-bucket → name `incubation-data` → **Private** → create.
2. Attach it to the Space at `/data`:
   - If the Space's **Settings** page shows a *Volumes / Storage* section, add the bucket there with mount path `/data`, read-write.
   - Otherwise use the command line (PowerShell):
     ```
     pip install -U huggingface_hub
     hf auth login
     hf spaces volumes set YOUR-HF-NAME/incubation-portal -v hf://buckets/YOUR-HF-NAME/incubation-data:/data
     hf spaces volumes ls YOUR-HF-NAME/incubation-portal
     ```
     `hf auth login` asks for a token: create one at https://huggingface.co/settings/tokens with **Write** permission.

How it works: the portal keeps its database on the fast local disk. It copies it to the bucket within seconds of every change, every 2 minutes, and when the Space stops. On start-up it copies the latest version back. You can check it on the admin **Dashboard** (💾 line) and with **Policy → Back up now**.

### A6. Email: set up Brevo (about 10 minutes)
1. Brevo → **Senders, domains & dedicated IPs → Senders → Add a sender**. Use the address students should see, e.g. `incubation@yourcollege.edu.in`, and click the verification link Brevo emails you.
   - A college address is best. Mail "from" a Gmail address sent through Brevo is more likely to land in spam.
   - For the best delivery, also add your college **domain** in Brevo and let your IT team add the DNS records Brevo shows.
2. Brevo → **SMTP & API → API keys → Generate a new API key**. Copy it (it starts with `xkeysib-`).

### A7. Space settings: secrets and variables
In your Space → **Settings → Variables and secrets**:

| Type | Name | Value (example) | Purpose |
|---|---|---|---|
| Secret | `ADMIN_PASSWORD` | a strong password | creates the `admin` login on the first start |
| Secret | `BREVO_API_KEY` | `xkeysib-...` | sends emails |
| Secret | `SIGNUP_CODE` | e.g. `INC-2026-7Q4` | only people with this code can create student accounts |
| Variable | `EMAIL_PROVIDER` | `brevo` | |
| Variable | `EMAIL_FROM` | `incubation@yourcollege.edu.in` (the verified Brevo sender) | |
| Variable | `EMAIL_FROM_NAME` | `Incubation Cell` | name shown in the inbox |
| Variable (optional) | `CHAT_MODEL` | `qwen2.5:1.5b` (default) | bigger models need paid bigger hardware |

The portal address used in emails is detected automatically (`https://YOUR-HF-NAME-incubation-portal.hf.space`).

### A8. Connect GitHub → Hugging Face (automatic deployment)
1. Hugging Face → https://huggingface.co/settings/tokens → **Create new token** → type **Write** → name `github-deploy` → copy it.
2. GitHub → your repository → **Settings → Secrets and variables → Actions**:
   - **Secrets** tab → *New repository secret*: name `HF_TOKEN`, value = the token.
   - **Variables** tab → *New repository variable*: name `HF_SPACE`, value `YOUR-HF-NAME/incubation-portal`.
3. GitHub → **Actions** tab → *Test and deploy to Hugging Face* → **Run workflow** (or just push any change).
4. Watch it: **test** runs the 650+ automatic tests (about 3 minutes), then **deploy** copies the code to the Space. Green ✓ = done. Red ✗ = click it to read the error (see Troubleshooting below).

### A9. First start
1. Open the Space page → it shows **Building**. Click **Logs → Build**. The first build downloads Ollama and the AI models (10–25 minutes); later builds are faster.
2. When it shows **Running**, the **Container** logs should include:
   ```
   [start] persistent storage: /data/incubation_backup
   [bootstrap] admin account 'admin' created.
   [start] portal starting on port 7860
   ```
   If you see `WARNING: no writable persistent storage`, step A5 isn't done. Fix it before students register.
3. Open `https://YOUR-HF-NAME-incubation-portal.hf.space`. This direct address works better than the embedded view on the Space page.
4. Sign in as `admin` with your `ADMIN_PASSWORD`. Then:
   - **Dashboard:** check the 💾 line.
   - **Users:** set your email.
   - **Notifications → Send test email.**
   - **Programme Setup** (tracks, template, deadline, checks, email settings) and the **Knowledge Base** (upload documents → **Rebuild index**), exactly as in the earlier guide.
5. Give students the address **and the sign-up code**.

### A10. Updating later
Change files on your PC, then:
```
git add .
git commit -m "describe the change"
git push
```
GitHub tests it and, if all tests pass, deploys it. The Space restarts (a few minutes offline); data is kept in the bucket.

### A11. Good to know
- **Sleeping:** on CPU-basic hardware a Space goes to sleep after a period without visitors. The next visitor wakes it (1–2 minutes). While asleep, reminders aren't sent; the latest due reminder goes out after it wakes.
- **Speed:** on 2 CPUs the policy assistant takes about 10–60 seconds per answer. Submission checks don't use the AI and stay fast.
- **Backups:** besides the bucket, regularly download a copy: open the bucket on huggingface.co and download `incubation_backup/db/incubation.db` and the `files` folder.

---

## Route B: your own PC or server with Docker (free)

### B1. Install Docker
- Windows 10/11: install **Docker Desktop** from https://www.docker.com/products/docker-desktop/ (it sets up WSL 2; restart when asked). Start Docker Desktop and wait for "Engine running".
- Ubuntu: follow https://docs.docker.com/engine/install/ubuntu/.
- Check: `docker --version` and `docker compose version`.

### B2. Settings
In the project folder:
```
copy .env.example .env        (Linux/macOS: cp .env.example .env)
notepad .env
```
Set at least:
```
INCUB_ADMIN_PASSWORD=a-strong-password
SIGNUP_CODE=INC-2026-7Q4
```
Also set the email settings (Gmail/Outlook SMTP works at home), and `OLLAMA_CHAT_MODEL=llama3.2` if the PC has 8 GB of RAM.

### B3. Start everything
```
docker compose up -d --build
```
The first time this downloads Python, Ollama and the AI models (about 5 GB). Then:
- `docker compose ps` shows `app`, `worker` and `ollama` as running/healthy.
- Open http://127.0.0.1:8501 and sign in as `admin`.
- Logs: `docker compose logs -f app` (press Ctrl+C to stop watching).

### B4. A public address
**Quick and free (testing, the address changes on every restart):**
```
docker compose --profile quick-tunnel up -d
docker compose logs tunnel-quick
```
Find the line with `https://....trycloudflare.com`. That's your public address. Put it in `.env` as `INCUB_PUBLIC_URL=...` and run `docker compose up -d` so emails contain it.

**Permanent (needs a domain on a free Cloudflare account):**
1. Cloudflare dashboard → **Zero Trust → Networks → Tunnels → Create a tunnel** (type *Cloudflared*). Copy the **token**.
2. In the tunnel, add a **Public hostname**: e.g. `portal.your-domain.in`, Service `HTTP`, URL `app:8501`.
3. In `.env`: `CLOUDFLARE_TUNNEL_TOKEN=<token>` and `INCUB_PUBLIC_URL=https://portal.your-domain.in`.
4. Run `docker compose --profile named-tunnel up -d`.

### B5. Everyday commands
| Task | Command |
|---|---|
| Stop (data kept) | `docker compose down` |
| Start again | `docker compose up -d` (add `--profile quick-tunnel` / `named-tunnel` if used) |
| After changing code or `.env` | `docker compose up -d --build` |
| Backup | `docker compose exec app python scripts/backup.py --out /app/storage/backups` then `docker compose cp app:/app/storage/backups ./backups` |
| Use an NVIDIA GPU | `docker compose -f docker-compose.yml -f docker-compose.gpu.yml up -d --build` |

⚠️ Never run `docker compose down -v` unless you want to **delete all data**. `-v` removes the volumes.

---

## Troubleshooting

| Problem | Fix |
|---|---|
| GitHub Action ✗ at **test** | Click it and read the failing test; fix the code and push again |
| GitHub Action ✗ "Set the HF_TOKEN secret and the HF_SPACE variable" | Step A8.2 |
| GitHub Action ✗ "Remove these binary files" / Hugging Face "rejected because it contains binary files" | Don't commit `.docx`/`.pdf`/images. Upload documents inside the portal instead; they're kept in the bucket |
| Push rejected: `403` / `Authentication failed` | The HF token needs **Write** permission; the `HF_SPACE` name must be exactly `name/space` |
| Space build fails while pulling the model | Check the `CHAT_MODEL` spelling (e.g. `qwen2.5:1.5b`); rebuild via Settings → **Factory rebuild** |
| Space log: `no writable persistent storage` / red banner on the Dashboard | Attach the bucket at `/data` (A5), then restart the Space |
| Space log: `no admin exists yet` | Add the `ADMIN_PASSWORD` secret (A7) and restart |
| Test email fails with a timeout on Hugging Face | SMTP is blocked there: use `EMAIL_PROVIDER=brevo` |
| Brevo `401` | Wrong `BREVO_API_KEY` |
| Brevo `400 ... sender` | `EMAIL_FROM` must be a sender verified in Brevo |
| File upload shows `403` | Open the direct `...hf.space` address; the image already turns off the conflicting check (`STREAMLIT_DISABLE_XSRF=1`) |
| "Too many failed sign-in attempts" | Wait 15 minutes (brute-force protection) |
| Docker Desktop: "WSL 2 installation is incomplete" | Run `wsl --install` in an admin PowerShell and restart |
| Port 8501 already in use | Stop the non-Docker portal (Ctrl+C in its window) |
