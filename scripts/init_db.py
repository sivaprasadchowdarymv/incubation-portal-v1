"""Initialise/migrate the database, load the policy catalogue and create the first admin.
Usage: python scripts/init_db.py --admin-user admin   (password from INCUB_ADMIN_PASSWORD or prompt)"""
import argparse
import getpass
import os

import _path  # noqa: F401
from workflow.context import open_app
from workflow.services import bootstrap_admin

ap = argparse.ArgumentParser()
ap.add_argument("--admin-user", default="admin")
a = ap.parse_args()
conn, s, policy = open_app()
print(f"Database ready: {s.db_path} (schema v{conn.execute('SELECT MAX(version) FROM schema_migrations').fetchone()[0]}, policy {policy['version']})")
if conn.execute("SELECT 1 FROM users WHERE role='admin'").fetchone():
    print("Admin already exists; not changed.")
else:
    pw = os.environ.get("INCUB_ADMIN_PASSWORD") or getpass.getpass("New admin password (min 8 chars): ")
    bootstrap_admin(conn, a.admin_user, pw)
    print(f"Admin '{a.admin_user}' created.")
