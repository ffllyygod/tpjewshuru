"""Shared pytest setup.

There was no conftest.py before this. The suite hits a live database with no
fixtures and no cleanup, and two of its fixtures are consumed by the tests that
use them — `test_full_cancellation_happy_path` really cancels TPJ-10000. So a
second run without reseeding produces five failures that look exactly like
regressions but aren't. That has now cost real debugging time more than once.

This asserts the DB is in the expected pre-test state and, if not, fails once
with the command to fix it — instead of five confusing assertion errors.
"""

from __future__ import annotations

import pytest

from src.db.connection import get_conn

_FIX = (
    "\n\nRun:\n"
    "    .venv/Scripts/python scripts/seed_db.py --reset\n"
    "    .venv/Scripts/python scripts/add_demo_user.py\n"
)


@pytest.fixture(scope="session", autouse=True)
def require_seeded_database():
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT status::text FROM orders WHERE order_number = 'TPJ-10000'")
        row = cur.fetchone()
        if row is None:
            pytest.exit("Database is not seeded — TPJ-10000 is missing." + _FIX, returncode=1)
        if row[0] != "PLACED":
            pytest.exit(
                f"Database is stale — TPJ-10000 is {row[0]}, expected PLACED. A previous run "
                "cancelled it (the cancellation happy-path test really cancels this order), so "
                "the suite is not idempotent without a reseed." + _FIX,
                returncode=1,
            )

        cur.execute("SELECT 1 FROM customers WHERE email = 'arun@shurutech.com'")
        if cur.fetchone() is None:
            pytest.exit("Demo customer arun@shurutech.com is missing." + _FIX, returncode=1)

        cur.execute("SELECT 1 FROM customers WHERE email = 'admin@tpjewellers.com' AND role = 'admin'")
        if cur.fetchone() is None:
            pytest.exit("Admin user admin@tpjewellers.com is missing (needed by the admin tests)." + _FIX, returncode=1)

        cur.execute("SELECT COUNT(*) FROM orders WHERE order_number LIKE 'TPJ-H%'")
        history = cur.fetchone()[0]
        if history < 500:
            pytest.exit(
                f"Only {history} history orders present; the analytics tests need the 12-month "
                "seed (expected ~1100)." + _FIX,
                returncode=1,
            )
