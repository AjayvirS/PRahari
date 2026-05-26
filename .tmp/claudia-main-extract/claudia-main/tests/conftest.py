"""Shared pytest fixtures for the claudia test suite."""

import os
import sys
import uuid
from pathlib import Path

import pytest

# Make repo root importable so tests can `import db`, `import windows`, etc.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture
def pg_conn():
    """Yield a connection to a throwaway schema in the local claudia DB.

    Skips the test if Postgres isn't reachable. Uses a unique schema per test
    so parallel runs don't collide, and drops the schema on teardown.
    """
    try:
        import db as claudia_db
    except ImportError:
        pytest.skip("db module not importable")

    try:
        conn = claudia_db.connect()
    except Exception as exc:
        pytest.skip(f"Postgres not reachable: {exc}")

    schema = f"test_{uuid.uuid4().hex[:12]}"
    cur = conn.cursor()
    cur.execute(f'CREATE SCHEMA "{schema}"')
    cur.execute(f'SET search_path TO "{schema}", public')
    # Run schema against this schema. The enum types are global so CREATE TYPE
    # inside the DO block is a no-op on repeat runs.
    cur.execute(claudia_db.SCHEMA_SQL)
    conn.commit()
    cur.close()

    try:
        yield conn
    finally:
        try:
            cur = conn.cursor()
            cur.execute(f'DROP SCHEMA "{schema}" CASCADE')
            conn.commit()
            cur.close()
        except Exception:
            pass
        try:
            conn.close()
        except Exception:
            pass
