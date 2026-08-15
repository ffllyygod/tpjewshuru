"""Customer identity resolution.

Not exposed as an agent tool — identity must come from the authenticated
session, never from something the LLM decides to look up mid-conversation
(that would let a prompt-injected transcript pivot to someone else's
orders). This is called once by the API layer to resolve email -> customer_id
before the agent loop ever starts.
"""

from __future__ import annotations

from psycopg.rows import dict_row

from src.db.connection import get_conn


def get_customer_by_email(email: str) -> dict | None:
    with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT id, name, email FROM customers WHERE email = %s", (email,))
        row = cur.fetchone()
    return dict(row) if row else None
