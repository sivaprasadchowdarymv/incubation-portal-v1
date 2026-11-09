"""Consistent backup of the database + uploaded files, safe while the app is running (works on Windows, Linux, Docker).

  python scripts/backup.py                 -> backup/incubation-YYYYmmdd-HHMMSS.zip
  python scripts/backup.py --out D:/bk     -> choose the folder
"""
import argparse
import sqlite3
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path

import _path  # noqa: F401
from config.settings import ROOT, get_settings

ap = argparse.ArgumentParser()
ap.add_argument("--out", default=str(ROOT / "backup"))
a = ap.parse_args()
s = get_settings()
out = Path(a.out)
out.mkdir(parents=True, exist_ok=True)
target = out / f"incubation-{datetime.now().strftime('%Y%m%d-%H%M%S')}.zip"
with tempfile.TemporaryDirectory() as tmp:
    snap = Path(tmp) / "incubation.db"
    src, dst = sqlite3.connect(s.db_path), sqlite3.connect(snap)
    src.backup(dst)  # online, consistent copy (WAL-safe)
    src.close()
    dst.close()
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as z:
        z.write(snap, "storage/incubation.db")
        for folder, arc in ((s.storage_dir / "uploads", "storage/uploads"), (s.storage_dir / "templates", "storage/templates"), (s.kb_dir, "kb")):
            if folder.exists():
                for f in folder.rglob("*"):
                    if f.is_file():
                        z.write(f, f"{arc}/{f.relative_to(folder).as_posix()}")
print(f"Backup written: {target} ({target.stat().st_size // 1024} KB)")
