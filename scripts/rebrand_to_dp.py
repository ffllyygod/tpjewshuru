"""Rebrand an EXISTING database from TP Jewellers to DP Jewellers.

The code, seeds and prompts were renamed wholesale, but a live database still
holds the old name in rows nobody redeploys: staff email addresses and the
customer-facing policy documents the assistant quotes back verbatim.

    python scripts/rebrand_to_dp.py --dry-run     # show what would change
    python scripts/rebrand_to_dp.py               # against $DATABASE_URL
    python scripts/rebrand_to_dp.py --url ...     # or an explicit target

Idempotent: re-running finds nothing left to do.

WHAT THIS DELIBERATELY DOES NOT TOUCH
-------------------------------------
Every already-issued identifier keeps its TPJ- prefix: order numbers, product
SKUs, coupon codes, Gold SIP codes, payment references. Those are not branding,
they are identifiers a customer may be holding — a coupon code printed in an
email, an order number on an invoice already sent. Rewriting them would
invalidate things people have in their hands, and would rewrite the historical
record of what was actually issued.

New identifiers generated after the rename use DPJ-. A mixed prefix in the
orders table is the correct outcome of a rebrand, not a defect: lookups are
exact-string, so both keep working forever.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import psycopg
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent

# (description, count SQL, update SQL). Counted first so --dry-run can report.
STEPS: list[tuple[str, str, str]] = [
    (
        "staff email addresses @tpjewellers.com -> @dpjewellers.com",
        "SELECT count(*) FROM customers WHERE email LIKE '%@tpjewellers.com'",
        """
        UPDATE customers
        SET email = replace(email, '@tpjewellers.com', '@dpjewellers.com')
        WHERE email LIKE '%@tpjewellers.com'
        """,
    ),
    (
        "knowledge doc titles — quoted to customers verbatim by search_knowledge",
        "SELECT count(*) FROM knowledge_docs WHERE title LIKE '%TP Jewellers%'",
        "UPDATE knowledge_docs SET title = replace(title, 'TP Jewellers', 'DP Jewellers') "
        "WHERE title LIKE '%TP Jewellers%'",
    ),
    (
        "knowledge doc bodies — the store policy text the assistant reads out",
        "SELECT count(*) FROM knowledge_docs WHERE content LIKE '%TP Jewellers%'",
        "UPDATE knowledge_docs SET content = replace(content, 'TP Jewellers', 'DP Jewellers') "
        "WHERE content LIKE '%TP Jewellers%'",
    ),
    (
        "resolution reason labels shown in the cancellation/return pick-list",
        "SELECT count(*) FROM resolution_reasons WHERE label LIKE '%TP Jewellers%'",
        "UPDATE resolution_reasons SET label = replace(label, 'TP Jewellers', 'DP Jewellers') "
        "WHERE label LIKE '%TP Jewellers%'",
    ),
]

# Reported, never changed — see the module docstring.
UNTOUCHED = [
    ("orders", "order_number LIKE 'TPJ-%'", "order numbers already issued"),
    ("products", "sku LIKE 'TPJ-%'", "product SKUs already in customers' order history"),
    ("coupons", "code LIKE 'TPJ-%'", "coupon codes customers may be holding"),
]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", help="target database URL (defaults to $DATABASE_URL)")
    parser.add_argument("--dry-run", action="store_true", help="report without changing anything")
    args = parser.parse_args()

    load_dotenv(ROOT / ".env")
    url = args.url or os.environ.get("DATABASE_URL")
    if not url:
        print("No target. Set DATABASE_URL or pass --url.", file=sys.stderr)
        sys.exit(1)

    # Never print credentials; the host is enough to confirm the right target.
    host = url.split("@")[-1].split("/")[0] if "@" in url else url
    print(f"{'Inspecting' if args.dry_run else 'Rebranding'} {host}\n")

    with psycopg.connect(url) as conn:
        for description, count_sql, update_sql in STEPS:
            with conn.cursor() as cur:
                cur.execute(count_sql)
                affected = cur.fetchone()[0]
                if affected and not args.dry_run:
                    cur.execute(update_sql)
                    conn.commit()
            verb = "would update" if args.dry_run else "updated"
            print(f"  {verb} {affected:>4}  {description}")

        print("\n  left alone, on purpose — issued identifiers, not branding:")
        for table, predicate, why in UNTOUCHED:
            with conn.cursor() as cur:
                try:
                    cur.execute(f"SELECT count(*) FROM {table} WHERE {predicate}")
                    print(f"  {cur.fetchone()[0]:>4} rows in {table:<9} — {why}")
                except psycopg.Error:
                    conn.rollback()

    print("\nDone." if not args.dry_run else "\nDry run — nothing was changed.")


if __name__ == "__main__":
    main()
