"""Convert the PS dashboard HTML (const DOMAINS / ORIGINAL_DATA) into the portal catalogue.

  python scripts/import_dashboard.py "C:\\path\\PS_dashboard.html"            -> private/ps_catalogue.csv (check/edit it, then upload
                                                                               it in Programme Setup)
  python scripts/import_dashboard.py "C:\\path\\PS_dashboard.html" --import   -> also imports it straight into the portal
The private/ folder is excluded from Git and Docker: keep your problem statements confidential."""
import argparse
import sys

import _path  # noqa: F401
from config.settings import ROOT
from workflow import setup
from workflow.context import open_app
from workflow.dashboard_import import dashboard_rows, rows_to_csv
from workflow.services import User

ap = argparse.ArgumentParser()
ap.add_argument("html")
ap.add_argument("--import", dest="do_import", action="store_true")
a = ap.parse_args()
rows = dashboard_rows(open(a.html, encoding="utf-8").read())
out = ROOT / "private" / "ps_catalogue.csv"
out.parent.mkdir(exist_ok=True)
data = rows_to_csv(rows)
out.write_bytes(data)
print(f"{len(rows)} problem statements in {len({r['track_id'] for r in rows})} tracks -> {out}")
if a.do_import:
    conn, s, policy = open_app()
    r = conn.execute("SELECT * FROM users WHERE role='admin' AND active=1 ORDER BY id LIMIT 1").fetchone()
    if not r:
        sys.exit("Create the admin first: python scripts/init_db.py --admin-user admin")
    summary = setup.import_catalogue(conn, User(r["id"], r["username"], "admin", None), "ps_catalogue.csv", data, True, s.kb_dir)
    print("Imported:", summary, "-> now open Knowledge Base and click 'Rebuild index' (or run python scripts/ingest.py --force)")
