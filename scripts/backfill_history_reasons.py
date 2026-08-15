"""Give the SYNTHETIC history orders realistic cancellation/return reasons.

Context: the migration backfills every already-resolved order to the inactive
`unspecified` codes, which report as "Not recorded". That is the honest answer
for a real customer's order — nobody asked them why, so nothing should be put in
their mouth. But it leaves the "why are people cancelling" report showing a
single meaningless row.

The seeded `TPJ-H` history orders are a different case: they are synthetic, and
their statuses were generated in the first place. Giving them a plausible reason
distribution invents nothing that wasn't already invented.

So this touches ONLY orders whose number starts with `TPJ-H`, and only those
still sitting on the legacy `unspecified` codes. Real orders keep "Not
recorded", and it verifies that at the end.

    python scripts/backfill_history_reasons.py --url ...
    python scripts/backfill_history_reasons.py --url ... --dry-run

Re-running is safe: rows that already have a real reason are skipped, so it
converges rather than reshuffling reasons every time.
"""

from __future__ import annotations

import argparse
import os
import random
import sys
from pathlib import Path

import psycopg
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts.seed_db import HISTORY_ORDER_PREFIX, _seed_resolution_reason  # noqa: E402

# Same split the history generator uses: most resolutions come from the
# customer, a minority are staff-side.
STAFF_SHARE = 0.2


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", help="target database URL (defaults to $DATABASE_URL)")
    parser.add_argument("--dry-run", action="store_true", help="report what would change, write nothing")
    args = parser.parse_args()

    load_dotenv(ROOT / ".env")
    url = args.url or os.environ.get("DATABASE_URL")
    if not url:
        print("No target. Set DATABASE_URL or pass --url.", file=sys.stderr)
        sys.exit(1)

    host = url.split("@")[-1].split("/")[0] if "@" in url else url
    print(f"Target: {host}{' (dry run)' if args.dry_run else ''}\n")

    # Deterministic, so a dry run and the real run agree on what they'd write.
    random.seed(20260816)

    with psycopg.connect(url) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT count(*) FROM orders
            WHERE status = 'CANCELLED' AND order_number NOT LIKE %s
            """,
            (f"{HISTORY_ORDER_PREFIX}%",),
        )
        real_cancelled_before = cur.fetchone()[0]

        for status, kind, column, legacy in (
            ("CANCELLED", "cancellation", "cancellation_reason_code", "unspecified"),
            ("RETURNED", "return", "return_reason_code", "unspecified_return"),
        ):
            note_column = column.replace("_code", "_note")
            cur.execute(
                f"""
                SELECT id FROM orders
                WHERE status = %s AND order_number LIKE %s AND {column} = %s
                ORDER BY order_number
                """,
                (status, f"{HISTORY_ORDER_PREFIX}%", legacy),
            )
            ids = [r[0] for r in cur.fetchall()]

            if args.dry_run:
                print(f"  would update {len(ids):5d} {status} history orders")
                continue

            for order_id in ids:
                code, note = _seed_resolution_reason(kind, random.random() > STAFF_SHARE)
                cur.execute(
                    f"UPDATE orders SET {column} = %s, {note_column} = %s WHERE id = %s",
                    (code, note, order_id),
                )
            print(f"  updated {len(ids):5d} {status} history orders")

        if args.dry_run:
            return
        conn.commit()

        # The whole point of the TPJ-H filter: a real customer's order must not
        # have acquired a reason nobody gave.
        cur.execute(
            """
            SELECT count(*) FROM orders
            WHERE status = 'CANCELLED' AND order_number NOT LIKE %s
              AND cancellation_reason_code <> 'unspecified'
            """,
            (f"{HISTORY_ORDER_PREFIX}%",),
        )
        touched_real = cur.fetchone()[0]

        print(f"\n  real cancelled orders: {real_cancelled_before} "
              f"({touched_real} now carry a non-legacy reason — expected 0 unless "
              "genuinely cancelled through the app)")

        for status, column in (("CANCELLED", "cancellation_reason_code"), ("RETURNED", "return_reason_code")):
            cur.execute(
                f"""
                SELECT COALESCE(cr.label, 'Not recorded'), count(*)
                FROM orders o LEFT JOIN resolution_reasons cr ON cr.code = o.{column}
                WHERE o.status = %s GROUP BY 1 ORDER BY 2 DESC LIMIT 5
                """,
                (status,),
            )
            print(f"\n  top {status.lower()} reasons now:")
            for label, n in cur.fetchall():
                print(f"    {n:5d}  {label}")


if __name__ == "__main__":
    main()
