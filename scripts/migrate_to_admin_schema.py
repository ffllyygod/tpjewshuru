"""Bring an EXISTING database up to the admin-persona schema without wiping it.

This project has no migration framework — `schema.sql` is applied wholesale by
`seed_db.py`, and `--reset` drops the schema first. That's fine locally and
catastrophic against the deployed database, which holds real orders someone may
be mid-way through testing.

So this script exists for exactly one job: take a database created by the
pre-admin-persona `schema.sql` and add everything the admin persona needs, in
place, preserving every existing row.

It is idempotent — every step is `IF NOT EXISTS` or checks first — so running it
twice is a no-op and running it against an already-current database does
nothing. It is also strictly additive: no DROP TABLE, no DELETE, and the single
constraint it drops is immediately replaced by a strictly more permissive one.

    python scripts/migrate_to_admin_schema.py            # against $DATABASE_URL
    python scripts/migrate_to_admin_schema.py --url ...  # or an explicit target

Run it BEFORE deploying the code that depends on these columns.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import psycopg
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent

# (description, SQL). Order matters where noted; everything is idempotent.
STEPS: list[tuple[str, str]] = [
    (
        "customers.role — the source of truth for staff access",
        """
        ALTER TABLE customers
          ADD COLUMN IF NOT EXISTS role TEXT NOT NULL DEFAULT 'customer';
        """,
    ),
    (
        "customers.role CHECK constraint",
        """
        DO $$ BEGIN
          IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'customers_role_check') THEN
            ALTER TABLE customers ADD CONSTRAINT customers_role_check
              CHECK (role IN ('customer', 'admin'));
          END IF;
        END $$;
        """,
    ),
    (
        "products — cost price, stock threshold, soft-delete, updated_at",
        """
        ALTER TABLE products
          ADD COLUMN IF NOT EXISTS cost_price_cents    BIGINT,
          ADD COLUMN IF NOT EXISTS low_stock_threshold INT NOT NULL DEFAULT 2,
          ADD COLUMN IF NOT EXISTS active              BOOLEAN NOT NULL DEFAULT true,
          ADD COLUMN IF NOT EXISTS updated_at          TIMESTAMPTZ NOT NULL DEFAULT now();
        """,
    ),
    (
        "orders.payment_status — normalise existing rows to uppercase",
        # Must run BEFORE the CHECK below, or the constraint fails to validate.
        # The live database really did hold both 'PAID' and 'paid', which
        # silently dropped rows from any revenue query filtering on 'PAID'.
        """
        UPDATE orders SET payment_status = upper(payment_status)
        WHERE payment_status <> upper(payment_status);
        """,
    ),
    (
        "orders.payment_status CHECK constraint",
        """
        DO $$ BEGIN
          IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'orders_payment_status_check') THEN
            ALTER TABLE orders ADD CONSTRAINT orders_payment_status_check
              CHECK (payment_status IN ('PAID', 'REFUNDED', 'PENDING', 'REFUND_PENDING'));
          END IF;
        END $$;
        """,
    ),
    (
        "conversations.mode — what this conversation is doing right now",
        """
        ALTER TABLE conversations
          ADD COLUMN IF NOT EXISTS mode TEXT NOT NULL DEFAULT 'customer';
        DO $$ BEGIN
          IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'conversations_mode_check') THEN
            ALTER TABLE conversations ADD CONSTRAINT conversations_mode_check
              CHECK (mode IN ('customer', 'admin'));
          END IF;
        END $$;
        """,
    ),
    (
        "confirmation_tokens.params — binds a token to what was previewed",
        """
        ALTER TABLE confirmation_tokens
          ADD COLUMN IF NOT EXISTS params JSONB NOT NULL DEFAULT '{}'::jsonb;
        """,
    ),
    (
        "admin_action_log — who did what to whom",
        """
        CREATE TABLE IF NOT EXISTS admin_action_log (
          id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
          actor_customer_id  UUID NOT NULL REFERENCES customers(id),
          conversation_id    UUID REFERENCES conversations(id) ON DELETE SET NULL,
          action             TEXT NOT NULL,
          target_table       TEXT NOT NULL,
          target_id          UUID NOT NULL,
          before_state       JSONB,
          after_state        JSONB,
          reason             TEXT NOT NULL,
          created_at         TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        CREATE INDEX IF NOT EXISTS admin_action_log_target_idx
          ON admin_action_log(target_table, target_id);
        CREATE INDEX IF NOT EXISTS admin_action_log_actor_idx
          ON admin_action_log(actor_customer_id, created_at DESC);
        """,
    ),
    (
        "analytics indexes",
        """
        CREATE INDEX IF NOT EXISTS orders_placed_at_idx ON orders(placed_at DESC);
        CREATE INDEX IF NOT EXISTS order_items_product_id_idx ON order_items(product_id);
        """,
    ),
    (
        "products.returnable — final-sale flag",
        """
        ALTER TABLE products
          ADD COLUMN IF NOT EXISTS returnable BOOLEAN NOT NULL DEFAULT true;
        """,
    ),
    (
        "resolution_reasons — the cancellation/return pick-list",
        """
        CREATE TABLE IF NOT EXISTS resolution_reasons (
          code           TEXT PRIMARY KEY,
          kind           TEXT NOT NULL CHECK (kind IN ('cancellation', 'return')),
          label          TEXT NOT NULL,
          applies_to     TEXT NOT NULL DEFAULT 'both' CHECK (applies_to IN ('customer', 'staff', 'both')),
          requires_note  BOOLEAN NOT NULL DEFAULT false,
          active         BOOLEAN NOT NULL DEFAULT true,
          sort_order     INT NOT NULL DEFAULT 100
        );
        """,
    ),
    (
        "orders — resolution reason columns",
        """
        ALTER TABLE orders
          ADD COLUMN IF NOT EXISTS cancellation_reason_code TEXT,
          ADD COLUMN IF NOT EXISTS cancellation_reason_note TEXT,
          ADD COLUMN IF NOT EXISTS return_reason_code       TEXT,
          ADD COLUMN IF NOT EXISTS return_reason_note       TEXT,
          ADD COLUMN IF NOT EXISTS returned_at              TIMESTAMPTZ;
        """,
    ),
]

# Reason rows the migration must guarantee exist before the FKs and CHECKs are
# added. Deliberately only the two inactive legacy codes: the real pick-list is
# seeded by seed_db.seed_resolution_reasons / add_admin_users-style scripts, but
# these two are load-bearing for the constraints below and so belong here.
_LEGACY_REASON_ROWS = [
    ("unspecified", "cancellation"),
    ("unspecified_return", "return"),
]

_REASON_CONSTRAINTS = """
ALTER TABLE orders
  ADD CONSTRAINT orders_cancellation_reason_code_fkey
    FOREIGN KEY (cancellation_reason_code) REFERENCES resolution_reasons(code),
  ADD CONSTRAINT orders_return_reason_code_fkey
    FOREIGN KEY (return_reason_code) REFERENCES resolution_reasons(code);

ALTER TABLE orders
  ADD CONSTRAINT orders_cancellation_reason_required CHECK (
    (status = 'CANCELLED' AND cancellation_reason_code IS NOT NULL)
    OR (status <> 'CANCELLED' AND cancellation_reason_code IS NULL)
  ),
  ADD CONSTRAINT orders_return_reason_required CHECK (
    (status = 'RETURNED' AND return_reason_code IS NOT NULL)
    OR (status <> 'RETURNED' AND return_reason_code IS NULL)
  );

CREATE INDEX IF NOT EXISTS orders_cancellation_reason_idx
  ON orders(cancellation_reason_code) WHERE cancellation_reason_code IS NOT NULL;
CREATE INDEX IF NOT EXISTS orders_return_reason_idx
  ON orders(return_reason_code) WHERE return_reason_code IS NOT NULL;
"""


def backfill_and_constrain_reasons(conn: psycopg.Connection) -> str:
    """Backfill already-resolved orders, THEN add the constraints.

    Ordering is load-bearing, the same trap as the payment_status step above: a
    database with cancelled orders predating this feature has rows that violate
    "a cancelled order must carry a reason" the moment the CHECK is added. They
    are backfilled to the inactive 'unspecified' codes first — which reports
    render as "Not recorded", an honest answer, rather than attributing a reason
    to a customer who never gave one.
    """
    with conn.cursor() as cur:
        for code, kind in _LEGACY_REASON_ROWS:
            cur.execute(
                """
                INSERT INTO resolution_reasons (code, kind, label, applies_to, active, sort_order)
                VALUES (%s, %s, 'Not recorded', 'both', false, 999)
                ON CONFLICT (code) DO NOTHING
                """,
                (code, kind),
            )
        cur.execute(
            "UPDATE orders SET cancellation_reason_code = 'unspecified' "
            "WHERE status = 'CANCELLED' AND cancellation_reason_code IS NULL"
        )
        backfilled_cancel = cur.rowcount
        cur.execute(
            "UPDATE orders SET return_reason_code = 'unspecified_return' "
            "WHERE status = 'RETURNED' AND return_reason_code IS NULL"
        )
        backfilled_return = cur.rowcount
        # Clear any reason sitting on a non-resolved order, which the CHECK also
        # forbids. Should be none, but a half-applied earlier run could leave one.
        cur.execute(
            "UPDATE orders SET cancellation_reason_code = NULL, cancellation_reason_note = NULL "
            "WHERE status <> 'CANCELLED' AND cancellation_reason_code IS NOT NULL"
        )
        cur.execute(
            "UPDATE orders SET return_reason_code = NULL, return_reason_note = NULL "
            "WHERE status <> 'RETURNED' AND return_reason_code IS NOT NULL"
        )
        conn.commit()

        cur.execute(
            "SELECT count(*) FROM pg_constraint WHERE conname = 'orders_cancellation_reason_required'"
        )
        if cur.fetchone()[0]:
            return f"already current (backfilled {backfilled_cancel}+{backfilled_return})"
        cur.execute(_REASON_CONSTRAINTS)
    conn.commit()
    return f"constraints added; backfilled {backfilled_cancel} cancelled / {backfilled_return} returned"


def relax_coupon_source_constraint(conn: psycopg.Connection) -> str:
    """Replace `num_nonnulls(...) = 1` with the goodwill-aware version.

    The original constraint is unnamed, so Postgres generated something like
    `coupons_check1` — it has to be found by its definition rather than assumed.
    The replacement is strictly more permissive (it admits exactly one new case:
    a sourceless coupon of source_type 'goodwill'), so no existing row can fail
    validation, and the ADD would error loudly rather than silently drop data if
    one somehow did.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT conname FROM pg_constraint
            WHERE conrelid = 'coupons'::regclass AND contype = 'c'
              AND pg_get_constraintdef(oid) LIKE '%num_nonnulls%'
            """
        )
        existing = [r[0] for r in cur.fetchall()]

        if existing == ["coupons_source_matches_type"]:
            return "already current"

        for name in existing:
            if name != "coupons_source_matches_type":
                cur.execute(f'ALTER TABLE coupons DROP CONSTRAINT "{name}"')

        cur.execute(
            """
            DO $$ BEGIN
              IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'coupons_source_matches_type') THEN
                ALTER TABLE coupons ADD CONSTRAINT coupons_source_matches_type CHECK (
                  CASE WHEN source_type = 'goodwill'
                       THEN num_nonnulls(source_order_id, source_subscription_id) = 0
                       ELSE num_nonnulls(source_order_id, source_subscription_id) = 1
                  END
                );
              END IF;
            END $$;
            """
        )
    conn.commit()
    return f"replaced {existing or ['(none)']}"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", help="target database URL (defaults to $DATABASE_URL)")
    args = parser.parse_args()

    load_dotenv(ROOT / ".env")
    url = args.url or os.environ.get("DATABASE_URL")
    if not url:
        print("No target. Set DATABASE_URL or pass --url.", file=sys.stderr)
        sys.exit(1)

    # Never print credentials; the host is enough to confirm the right target.
    host = url.split("@")[-1].split("/")[0] if "@" in url else url
    print(f"Migrating {host}\n")

    with psycopg.connect(url) as conn:
        for description, sql in STEPS:
            with conn.cursor() as cur:
                cur.execute(sql)
            conn.commit()
            print(f"  ok  {description}")
        print(f"  ok  coupons source constraint — {relax_coupon_source_constraint(conn)}")
        print(f"  ok  resolution reasons — {backfill_and_constrain_reasons(conn)}")

    print("\nDone. Every step is idempotent; re-running changes nothing.")


if __name__ == "__main__":
    main()
