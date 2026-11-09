"""Import a tracks & problem-statements CSV/XLSX into the portal (same as Programme Setup -> Import, Replace mode).

  python scripts/import_catalogue_csv.py private\\ps_catalogue.csv"""
import argparse
import sys
from pathlib import Path

import _path  # noqa: F401
from workflow import setup
from workflow.context import open_app
from workflow.services import User

ap = argparse.ArgumentParser()
ap.add_argument("file")
a = ap.parse_args()
p = Path(a.file)
if not p.exists():
    sys.exit(f"File not found: {p}")
conn, s, policy = open_app()
r = conn.execute("SELECT * FROM users WHERE role='admin' AND active=1 ORDER BY id LIMIT 1").fetchone()
if not r:
    sys.exit("Create the admin first: python scripts/init_db.py --admin-user admin")
summary = setup.import_catalogue(conn, User(r["id"], r["username"], "admin", None), p.name, p.read_bytes(), True, s.kb_dir)
print(f"Imported: {summary['tracks']} tracks, {summary['problem_statements']} problem statements "
      f"({summary['deactivated_problem_statements']} sample ones hidden). Next: python scripts/ingest.py --force")
