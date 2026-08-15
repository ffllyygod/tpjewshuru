"""Add 12 months of synthetic sales history to an EXISTING database.

Why this isn't just `seed_db.py`: that script's history step is written for a
database it just created, and two of its behaviours are actively wrong against
one holding real data.

  1. `seed_history_orders` distributes orders across ALL customers. Run against a
     live database, a real customer would log in and find a thousand orders they
     never placed sitting in their account. Here, history is attached ONLY to the
     synthetic customers this script creates. Real customers' order lists are
     left exactly as they were.

  2. `seed_inventory_edge_cases` zeroes the stock of the first twelve products by
     SKU so the low/out-of-stock reports have something to show. Against a live
     catalogue that is a destructive inventory change. Here it is restricted to
     the synthetic products this script creates.

Everything else is genuinely additive: new products, new customers, new orders.
No existing row is modified except `products.cost_price_cents` where it is NULL,
which is backfilled metadata — without it, margin reporting says "cost data
incomplete" for the whole real catalogue.

Order numbers use the `TPJ-H` prefix, which cannot collide with the
`TPJ-{100000..999999}` that `purchase_tools._new_order_number` mints, so a real
order placed later can never clash with a seeded one.

    python scripts/seed_analytics_history.py --url ...

Refuses to run twice — history orders are not idempotent and a second pass would
double the numbers every report shows.
"""

from __future__ import annotations

import argparse
import os
import random
import sys
from pathlib import Path

import psycopg
from dotenv import load_dotenv
from psycopg.types.json import Json

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts.seed_db import (  # noqa: E402
    HISTORY_ORDER_PREFIX,
    backfill_cost_prices,
    seed_extra_customers,
    seed_extra_products,
    seed_history_orders,
)


def existing_history_count(conn: psycopg.Connection) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM orders WHERE order_number LIKE %s", (f"{HISTORY_ORDER_PREFIX}%",))
        return cur.fetchone()[0]


def synthetic_sku_conflicts(conn: psycopg.Connection) -> list[str]:
    """The extra-product SKUs are fixed (TPJ-XXX-2000+), so a partial previous
    run would collide on the UNIQUE index mid-way and abort with rows already
    written. Check up front instead."""
    with conn.cursor() as cur:
        cur.execute("SELECT sku FROM products WHERE sku ~ '^TPJ-[A-Z]{3}-2[0-9]{3}$' ORDER BY sku")
        return [r[0] for r in cur.fetchall()]


def all_products(conn: psycopg.Connection) -> list[dict]:
    """Every product, real and synthetic. History orders SHOULD reference real
    products — that's what makes 'top sellers' and 'revenue by category' say
    something about the actual catalogue — and doing so harms nothing, since
    seeding never decrements stock."""
    with conn.cursor() as cur:
        cur.execute("SELECT id, sku, name, category, price_cents, sizes_available FROM products")
        return [
            {"id": r[0], "sku": r[1], "name": r[2], "category": r[3],
             "price_cents": r[4], "sizes_available": r[5]}
            for r in cur.fetchall()
        ]


def force_inventory_edge_cases(conn: psycopg.Connection, products: list[dict]) -> None:
    """Same intent as seed_db's version — guarantee the low/out-of-stock reports
    return something — but scoped to the synthetic products passed in, so no real
    item's stock is altered."""
    with conn.cursor() as cur:
        for idx, product in enumerate(products[:10]):
            cur.execute("SELECT stock_by_size FROM products WHERE id = %s", (product["id"],))
            stock = cur.fetchone()[0] or {}
            if idx < 4:
                new_stock = {k: 0 for k in stock}
            else:
                new_stock = {k: random.choice([1, 2]) for k in stock}
            cur.execute(
                "UPDATE products SET stock_by_size = %s, updated_at = now() WHERE id = %s",
                (Json(new_stock), product["id"]),
            )
    conn.commit()
    print("Forced 4 out-of-stock and 6 low-stock SYNTHETIC products (real stock untouched).")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", help="target database URL (defaults to $DATABASE_URL)")
    parser.add_argument("--days", type=int, default=365)
    args = parser.parse_args()

    load_dotenv(ROOT / ".env")
    url = args.url or os.environ.get("DATABASE_URL")
    if not url:
        print("No target. Set DATABASE_URL or pass --url.", file=sys.stderr)
        sys.exit(1)

    host = url.split("@")[-1].split("/")[0] if "@" in url else url
    print(f"Target: {host}\n")

    with psycopg.connect(url) as conn:
        already = existing_history_count(conn)
        if already:
            print(
                f"Refusing to run: {already} history orders ({HISTORY_ORDER_PREFIX}*) already exist.\n"
                "Seeding again would double every figure the sales reports show. Delete them\n"
                f"first if you really want to re-seed:\n"
                f"    DELETE FROM orders WHERE order_number LIKE '{HISTORY_ORDER_PREFIX}%';",
                file=sys.stderr,
            )
            sys.exit(1)

        conflicts = synthetic_sku_conflicts(conn)
        if conflicts:
            print(
                f"Refusing to run: {len(conflicts)} synthetic SKUs already exist "
                f"({', '.join(conflicts[:3])}…).\nA previous run was interrupted; clean those up first.",
                file=sys.stderr,
            )
            sys.exit(1)

        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM orders")
            before_orders = cur.fetchone()[0]
            cur.execute("SELECT count(*) FROM customers")
            before_customers = cur.fetchone()[0]
        print(f"Before: {before_orders} orders, {before_customers} customers\n")

        new_products = seed_extra_products(conn)
        new_customers = seed_extra_customers(conn)
        # Synthetic customers only — this is the whole point of not reusing
        # seed_db's main(). Products are shared deliberately.
        seed_history_orders(conn, new_customers, all_products(conn), days=args.days)
        backfill_cost_prices(conn)
        force_inventory_edge_cases(conn, new_products)

        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM orders WHERE order_number NOT LIKE %s", (f"{HISTORY_ORDER_PREFIX}%",))
            real_after = cur.fetchone()[0]

    print(f"\nReal (non-{HISTORY_ORDER_PREFIX}) orders still present: {real_after} — expected {before_orders}.")
    print("Done." if real_after == before_orders else "WARNING: real order count changed — investigate.")


if __name__ == "__main__":
    main()
