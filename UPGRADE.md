# Upgrading an existing installation (keeps all your data)

1. Stop the portal and the email worker (Ctrl + C in their windows).
2. Back up: copy the whole project folder somewhere safe (or at least `storage`, `kb` and `.env`).
3. Extract the new zip **into the same parent folder**, so its `incubation_rag` folder lands on top of your existing one.
   When Windows asks, choose **Replace the files in the destination**.
   Your `.env`, `storage` (database + uploads) and `.venv` are not in the zip, so they are untouched.
   The `kb` folder in the zip already contains your generated tracks/template documents.
4. In PowerShell in the project folder:
       .venv\Scripts\Activate.ps1
       pip install -r requirements.txt
       python scripts/init_db.py        -> "schema v4" (adds the organiser-messages table; data kept)
       python -m pytest                 -> all tests pass
5. Start: `streamlit run app.py` (window 1) and `python scripts/notify_worker.py --loop 300` (window 2).
6. Admin → Notifications: optionally **Discard** old test emails that were queued while email was not set up,
   then **Send test email**.
