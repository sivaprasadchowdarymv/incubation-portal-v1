"""Keep data safe on hosts whose own disk is wiped on restart (Hugging Face Spaces).

The live SQLite database and files stay on the fast local disk (SQLite must not run on a network mount).
A consistent snapshot is copied to INCUB_BACKUP_DIR (a Hugging Face Storage Bucket mounted at /data):
  * on a timer (python -m persist.snapshot loop 120),
  * shortly after every change made in the app (backup_soon),
  * when the container is stopped (docker/hf_start.sh calls `backup`).
On start-up `restore` copies the latest good snapshot back before the app opens the database.

  python -m persist.snapshot status | backup | restore | loop [seconds]

When INCUB_BACKUP_DIR is not set (e.g. docker compose with real volumes) every function is a no-op.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from config.settings import get_settings

_LOCK = threading.Lock()
_pending = threading.Event()
_worker: threading.Thread | None = None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _paths(settings=None) -> dict | None:
    s = settings or get_settings()
    if not s.backup_dir:
        return None
    root = Path(s.backup_dir)
    return {"root": root, "db": root / "db" / "incubation.db", "prev": root / "db" / "incubation.prev.db",
            "files": root / "files", "status": root / "status.json", "live_db": Path(s.db_path),
            "dirs": {"uploads": Path(s.storage_dir) / "uploads", "templates": Path(s.storage_dir) / "templates", "kb": Path(s.kb_dir)}}


def _sha(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _copy_atomic(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(dst.name + ".part")
    shutil.copyfile(src, tmp)
    os.replace(tmp, dst)


def _read_status(p: dict) -> dict:
    try:
        return json.loads(p["status"].read_text())
    except (OSError, ValueError):
        return {}


def status(settings=None) -> dict:
    p = _paths(settings)
    if p is None:
        return {"enabled": False, "message": "No separate backup location configured (normal for a PC/server install with real disks)."}
    st = _read_status(p)
    return {"enabled": True, "location": str(p["root"]), "writable": os.access(p["root"], os.W_OK) if p["root"].exists() else False,
            "last_backup_at": st.get("at"), "db_bytes": st.get("db_bytes"), "files": st.get("files_total"),
            "message": f"Data is copied to {p['root']} (last copy: {st.get('at') or 'not yet'})."}


def backup(settings=None) -> dict:
    """Consistent snapshot of the database + copy of new/changed files. Safe while the app is running."""
    p = _paths(settings)
    if p is None:
        return {"ok": False, "skipped": "backup location not configured"}
    with _LOCK:
        result = {"ok": True, "at": _now(), "db_copied": False, "files_copied": 0}
        st = _read_status(p)
        if p["live_db"].exists():
            with tempfile.TemporaryDirectory() as tmpd:
                snap = Path(tmpd) / "snap.db"
                src, dst = sqlite3.connect(p["live_db"]), sqlite3.connect(snap)
                try:
                    src.backup(dst)  # online, WAL-safe copy
                finally:
                    src.close()
                    dst.close()
                digest = _sha(snap)
                if digest != st.get("db_sha") or not p["db"].exists():
                    if p["db"].exists():
                        _copy_atomic(p["db"], p["prev"])  # keep one older generation
                    _copy_atomic(snap, p["db"])
                    result["db_copied"] = True
                result["db_sha"], result["db_bytes"] = digest, snap.stat().st_size
        total = 0
        for name, folder in p["dirs"].items():
            if not folder.exists():
                continue
            for f in folder.rglob("*"):
                if not f.is_file() or f.name.endswith(".part"):
                    continue
                total += 1
                dest = p["files"] / name / f.relative_to(folder)
                fs = f.stat()
                if dest.exists():
                    ds = dest.stat()
                    if ds.st_size == fs.st_size and ds.st_mtime >= fs.st_mtime:
                        continue
                _copy_atomic(f, dest)
                os.utime(dest, (fs.st_atime, fs.st_mtime))
                result["files_copied"] += 1
        result["files_total"] = total
        p["root"].mkdir(parents=True, exist_ok=True)
        status_out = {"at": result["at"], "db_sha": result.get("db_sha", st.get("db_sha")), "db_bytes": result.get("db_bytes"), "files_total": total}
        tmp = p["status"].with_name("status.json.part")
        tmp.write_text(json.dumps(status_out))
        os.replace(tmp, p["status"])
        return result


def _db_ok(path: Path) -> bool:
    try:
        c = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            return c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        finally:
            c.close()
    except sqlite3.DatabaseError:
        return False


def restore(settings=None, force: bool = False) -> dict:
    """Copy the latest good snapshot back into the live locations. Never overwrites an existing live database unless force=True."""
    p = _paths(settings)
    if p is None:
        return {"ok": False, "skipped": "backup location not configured"}
    with _LOCK:
        out = {"ok": True, "db_restored_from": None, "files_restored": 0}
        live = p["live_db"]
        if force or not live.exists() or live.stat().st_size == 0:
            for cand in (p["db"], p["prev"]):
                if cand.exists():
                    with tempfile.TemporaryDirectory(dir=str(live.parent) if live.parent.exists() else None) as tmpd:
                        tmp = Path(tmpd) / "restore.db"
                        shutil.copyfile(cand, tmp)
                        if _db_ok(tmp):
                            live.parent.mkdir(parents=True, exist_ok=True)
                            for side in (live.with_name(live.name + "-wal"), live.with_name(live.name + "-shm")):
                                with contextlib.suppress(FileNotFoundError):
                                    side.unlink()
                            shutil.copyfile(tmp, live)
                            out["db_restored_from"] = cand.name
                            break
                        out.setdefault("rejected", []).append(cand.name)  # damaged copy: try the older generation
        for name, folder in p["dirs"].items():
            src_root = p["files"] / name
            if not src_root.exists():
                continue
            for f in src_root.rglob("*"):
                if not f.is_file() or f.name.endswith(".part"):
                    continue
                dest = folder / f.relative_to(src_root)
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(f, dest)  # the backup is the newest state (admin-uploaded KB files, manifest, uploads)
                out["files_restored"] += 1
        return out


def _drain() -> None:
    global _worker
    while _pending.is_set():
        _pending.clear()
        time.sleep(2)  # let quick successive actions settle into one copy
        try:
            backup()
        except Exception as exc:  # never break the app because a backup failed; the timer loop retries
            print(f"[persist] backup failed: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
    _worker = None


def backup_soon() -> None:
    """Called by the app after a change; runs one background copy (coalesces bursts)."""
    global _worker
    if _paths() is None:
        return
    _pending.set()
    if _worker is None or not _worker.is_alive():
        _worker = threading.Thread(target=_drain, daemon=True, name="persist-backup")
        _worker.start()


def loop(seconds: int = 120) -> None:
    seconds = max(int(seconds), 15)
    while True:
        time.sleep(seconds)
        try:
            r = backup()
            if r.get("db_copied") or r.get("files_copied"):
                print(f"[persist] {json.dumps(r)}", flush=True)
        except Exception as exc:
            print(f"[persist] backup failed: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "backup":
        print(json.dumps(backup()))
    elif cmd == "restore":
        print(json.dumps(restore(force="--force" in sys.argv)))
    elif cmd == "loop":
        loop(int(sys.argv[2]) if len(sys.argv) > 2 else 120)
    else:
        print(json.dumps(status()))
