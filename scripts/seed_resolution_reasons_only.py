"""Seed the cancellation/return reason pick-list, and nothing else.

`seed_db.py` seeds these as part of a full seed, which you can't point at a
database holding real orders. But the reason list isn't sample data — it is
reference data the feature cannot function without: `cancel_order` and
`request_return` both reject any code that isn't in this table, so on a
freshly-migrated database with an empty pick-list, nothing can be cancelled or
returned at all.

Strictly additive and idempotent: one INSERT ... ON CONFLICT DO UPDATE per
reason. No order, product or customer row is read or written.

    python scripts/seed_resolution_reasons_only.py            # against $DATABASE_URL
    python scripts/seed_resolution_reasons_only.py --url ...  # or an explicit target

Safe to re-run after editing the list in scripts/seed_db.py — labels, ordering
and audience are refreshed, and codes already recorded on existing orders keep
working because the code itself is the primary key.
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

from scripts.seed_db import seed_resolution_reasons  # noqa: E402  — single source of truth


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", help="target database URL (defaults to $DATABASE_URL)")
    args = parser.parse_args()

    load_dotenv(ROOT / ".env")
    url = args.url or os.environ.get("DATABASE_URL")
    if not url:
        print("No target. Set DATABASE_URL or pass --url.", file=sys.stderr)
        sys.exit(1)

    host = url.split("@")[-1].split("/")[0] if "@" in url else url
    print(f"Target: {host}\n")

    with psycopg.connect(url) as conn:
        seed_resolution_reasons(conn)

        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT kind, applies_to, count(*) FROM resolution_reasons
                WHERE active = true GROUP BY 1, 2 ORDER BY 1, 2
                """
            )
            for kind, applies_to, n in cur.fetchall():
                print(f"  {kind:13} {applies_to:9} {n}")


if __name__ == "__main__":
    main()
