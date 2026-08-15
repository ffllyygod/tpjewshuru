"""Create (or promote) the staff accounts, without touching anything else.

`seed_db.py` also seeds these, but only as part of a full seed — which is not
something you can point at a database holding real orders. This does that one
step alone, so a deployed environment can get staff accounts without a reset.

Strictly additive: one INSERT ... ON CONFLICT per account, and the only column
it ever updates is `role`. No other row is read or written.

    python scripts/add_admin_users.py            # against $DATABASE_URL
    python scripts/add_admin_users.py --url ...  # or an explicit target

Staff log in through the ordinary OTP flow — that's the point of keeping admins
in `customers` rather than a separate table (see schema.sql). So the email must
be one you can actually receive mail at, or you won't be able to sign in.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import psycopg
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts.seed_db import ADMIN_USERS  # noqa: E402  — single source of truth


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", help="target database URL (defaults to $DATABASE_URL)")
    parser.add_argument(
        "--email",
        action="append",
        help="promote an additional existing email to admin (repeatable)",
    )
    args = parser.parse_args()

    load_dotenv(ROOT / ".env")
    url = args.url or os.environ.get("DATABASE_URL")
    if not url:
        print("No target. Set DATABASE_URL or pass --url.", file=sys.stderr)
        sys.exit(1)

    host = url.split("@")[-1].split("/")[0] if "@" in url else url
    print(f"Target: {host}\n")

    with psycopg.connect(url) as conn, conn.cursor() as cur:
        for name, email, role in ADMIN_USERS:
            cur.execute(
                """
                INSERT INTO customers (name, email, role) VALUES (%s, %s, %s)
                ON CONFLICT (email) DO UPDATE SET role = EXCLUDED.role
                RETURNING (xmax = 0) AS inserted
                """,
                (name, email, role),
            )
            print(f"  {'created' if cur.fetchone()[0] else 'promoted'}  {email}")

        for email in args.email or []:
            cur.execute(
                "UPDATE customers SET role = 'admin' WHERE lower(email) = lower(%s) RETURNING email",
                (email,),
            )
            row = cur.fetchone()
            print(f"  {'promoted  ' + row[0] if row else 'NOT FOUND ' + email}")

        conn.commit()

        cur.execute("SELECT email FROM customers WHERE role = 'admin' ORDER BY email")
        admins = [r[0] for r in cur.fetchall()]

    print(f"\nStaff accounts now: {', '.join(admins)}")
    print("Sign in with the normal OTP flow — the code goes to that address.")


if __name__ == "__main__":
    main()
