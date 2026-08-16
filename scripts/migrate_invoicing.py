"""Add the invoicing, address, payment and custom-design schema in place.

Same job and same rules as `scripts/migrate_to_admin_schema.py`: this project
has no migration framework, `schema.sql` is applied wholesale by `seed_db.py`,
and `--reset` drops the schema first — fine locally, catastrophic against the
deployed database, which holds real orders.

Every step is `IF NOT EXISTS` or checks first, so running it twice is a no-op.
Strictly additive: no DROP, no DELETE, no UPDATE of existing rows.

    python scripts/migrate_invoicing.py            # against $DATABASE_URL
    python scripts/migrate_invoicing.py --url ...  # or an explicit target

Run it BEFORE deploying the code that reads these columns — src/tools/billing.py
selects the product invoicing columns unconditionally, so the deploy fails on
every invoice call until this has run.

Note on backfill: this adds the columns but deliberately does NOT invent weights
or making charges for existing catalogue rows. billing.py degrades honestly on
NULLs ("weight not on record") and that is the correct outcome for real products
nobody has measured. Only the seeded demo catalogue gets values, from seed_db.py.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import psycopg
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent

# (description, SQL). Order matters: customer_addresses must exist before
# anything references it. Everything is idempotent.
STEPS: list[tuple[str, str]] = [
    (
        "products — invoicing attributes (weight, purity, making charge, stone value)",
        """
        ALTER TABLE products
          ADD COLUMN IF NOT EXISTS net_weight_grams      NUMERIC(8,3),
          ADD COLUMN IF NOT EXISTS purity_karat          SMALLINT,
          ADD COLUMN IF NOT EXISTS making_charge_percent NUMERIC(5,2),
          ADD COLUMN IF NOT EXISTS stone_value_cents     BIGINT;
        """,
    ),
    (
        "products — consultative attributes (occasion, style)",
        """
        ALTER TABLE products
          ADD COLUMN IF NOT EXISTS occasion TEXT[],
          ADD COLUMN IF NOT EXISTS style    TEXT;
        """,
    ),
    (
        "products — occasion/style indexes",
        """
        CREATE INDEX IF NOT EXISTS products_occasion_idx ON products USING GIN (occasion);
        CREATE INDEX IF NOT EXISTS products_style_idx    ON products(style);
        """,
    ),
    (
        "customer_addresses — the reusable address book",
        """
        CREATE TABLE IF NOT EXISTS customer_addresses (
          id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
          customer_id    UUID NOT NULL REFERENCES customers(id),
          label          TEXT,
          recipient_name TEXT NOT NULL,
          phone          TEXT NOT NULL,
          line1          TEXT NOT NULL,
          line2          TEXT,
          city           TEXT NOT NULL,
          state          TEXT NOT NULL,
          postal_code    TEXT NOT NULL,
          country        TEXT NOT NULL DEFAULT 'IN',
          is_default     BOOLEAN NOT NULL DEFAULT false,
          created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        CREATE INDEX IF NOT EXISTS customer_addresses_customer_id_idx
          ON customer_addresses(customer_id);
        CREATE UNIQUE INDEX IF NOT EXISTS customer_addresses_one_default_idx
          ON customer_addresses(customer_id) WHERE is_default;
        """,
    ),
    (
        "orders — payment method, reference and paid_at",
        """
        ALTER TABLE orders
          ADD COLUMN IF NOT EXISTS payment_method    TEXT,
          ADD COLUMN IF NOT EXISTS payment_reference TEXT,
          ADD COLUMN IF NOT EXISTS paid_at           TIMESTAMPTZ;
        """,
    ),
    (
        "orders.payment_method CHECK constraint",
        """
        DO $$ BEGIN
          IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'orders_payment_method_check') THEN
            ALTER TABLE orders ADD CONSTRAINT orders_payment_method_check
              CHECK (payment_method IN ('UPI', 'CARD', 'NETBANKING', 'COD'));
          END IF;
        END $$;
        """,
    ),
    (
        "design_requests — custom design briefs",
        """
        CREATE TABLE IF NOT EXISTS design_requests (
          id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
          request_number       TEXT NOT NULL UNIQUE,
          customer_id          UUID NOT NULL REFERENCES customers(id),
          reference_product_id UUID REFERENCES products(id),
          spec                 JSONB NOT NULL,
          estimate_low_cents   BIGINT,
          estimate_high_cents  BIGINT,
          estimate_basis       TEXT,
          status               TEXT NOT NULL DEFAULT 'NEW'
                               CHECK (status IN ('NEW', 'REVIEWED', 'QUOTED', 'CLOSED')),
          created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
          reviewed_by          UUID REFERENCES customers(id),
          reviewed_at          TIMESTAMPTZ,
          CHECK (estimate_high_cents IS NULL OR estimate_low_cents IS NULL
                 OR estimate_high_cents >= estimate_low_cents)
        );
        CREATE INDEX IF NOT EXISTS design_requests_customer_id_idx
          ON design_requests(customer_id);
        CREATE INDEX IF NOT EXISTS design_requests_new_idx
          ON design_requests(created_at DESC) WHERE status = 'NEW';
        """,
    ),
]


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

    print("\nDone. Every step is idempotent; re-running changes nothing.")
    print("Existing products keep NULL weight/making data — invoices for them")
    print("say so rather than inventing figures. Backfill deliberately omitted.")


if __name__ == "__main__":
    main()
