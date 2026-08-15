"""Thin Postgres connection helper shared by tools and API.

Uses a simple psycopg connection pool. Demo-scale (single process, low
concurrency) — no need for anything heavier right now.
"""

from __future__ import annotations

import os

import psycopg
from psycopg_pool import ConnectionPool
from dotenv import load_dotenv

load_dotenv()

_DATABASE_URL = os.environ.get("DATABASE_URL")
if not _DATABASE_URL:
    raise RuntimeError("DATABASE_URL is not set (check .env)")

# min_size=1 so the pool opens lazily but always has a warm connection ready.
pool = ConnectionPool(conninfo=_DATABASE_URL, min_size=1, max_size=10, open=True)


def get_conn() -> psycopg.Connection:
    """Borrow a connection from the pool as a context manager.

    Usage:
        with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(...)
    """
    return pool.connection()
