"""SQLite database setup and migration helpers."""
from __future__ import annotations

import sqlite3
from pathlib import Path

from app.config import settings
from app.logging_config import get_logger

logger = get_logger(__name__)

_MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"


def get_connection(database_path: str | None = None) -> sqlite3.Connection:
    """Open a SQLite connection with row access enabled."""
    db_path = Path(database_path or settings.database_path)
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def initialize_database(database_path: str | None = None) -> None:
    """Create the database file and apply any pending migrations."""
    db_path = Path(database_path or settings.database_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    with get_connection(str(db_path)) as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version TEXT PRIMARY KEY,
                applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )

        applied_versions = {
            row["version"]
            for row in connection.execute("SELECT version FROM schema_migrations")
        }

        for migration_path in sorted(_MIGRATIONS_DIR.glob("*.sql")):
            version = migration_path.stem
            if version in applied_versions:
                continue

            connection.executescript(migration_path.read_text(encoding="utf-8"))
            connection.execute(
                "INSERT INTO schema_migrations (version) VALUES (?)",
                (version,),
            )
            logger.info(
                "database.migration_applied",
                version=version,
                database_path=str(db_path),
            )

        connection.commit()

