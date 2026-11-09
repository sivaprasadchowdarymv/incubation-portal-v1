"""Open the database, apply migrations and load the approved catalogue."""
from __future__ import annotations

from config.settings import Settings, get_settings, load_policy
from database.db import connect, migrate
from workflow.services import sync_catalog


def open_app(settings: Settings | None = None, policy: dict | None = None):
    settings = settings or get_settings()
    policy = policy or load_policy(settings.policy_file)
    settings.storage_dir.mkdir(parents=True, exist_ok=True)
    conn = connect(settings.db_path)
    migrate(conn)
    sync_catalog(conn, policy)
    return conn, settings, policy
