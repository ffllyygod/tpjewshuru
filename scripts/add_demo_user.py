"""Add a specific, memorable demo customer + orders on top of the random seed data.

Idempotent-ish: uses ON CONFLICT on customer email so re-running doesn't
duplicate the customer, but will add fresh orders each run (fine for demo
prep — just re-run seed_db.py --reset if you want a truly clean slate).
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg
from dotenv import load_dotenv
from psycopg.rows import dict_row
from psycopg.types.json import Json

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

load_dotenv()

DEMO_NAME = "Arun"
DEMO_EMAIL = "arun@shurutech.com"

NEW_PRODUCTS = [
    {
        "sku": "TPJ-RIN-DEMO1",
        "name": "Rose Gold Diamond Solitaire Ring",
        "category": "ring",
        "description": "Rose Gold Diamond Solitaire Ring — handcrafted, a 1.2ct round-cut diamond set in 18k rose gold.",
        "price_cents": 32_500_000,  # ₹3,25,000 (price_cents = paise)
        "metal": "rose_gold",
        "stone": "diamond",
        "sizes_available": ["5", "6", "7", "8", "9", "10"],
        "stock_by_size": {"5": 3, "6": 4, "7": 5, "8": 4, "9": 2, "10": 1},
    },
    {
        "sku": "TPJ-NEC-DEMO1",
        "name": "Gold Tennis Necklace",
        "category": "necklace",
        "description": "Gold Tennis Necklace — handcrafted, continuous line of diamonds in 18k gold.",
        "price_cents": 48_000_000,  # ₹4,80,000 (price_cents = paise)
        "metal": "gold",
        "stone": "diamond",
        "sizes_available": None,
        "stock_by_size": {"_default": 7},
    },
]


def upsert_customer(conn: psycopg.Connection) -> str:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            INSERT INTO customers (name, email)
            VALUES (%s, %s)
            ON CONFLICT (email) DO UPDATE SET name = EXCLUDED.name
            RETURNING id
            """,
            (DEMO_NAME, DEMO_EMAIL),
        )
        customer_id = cur.fetchone()["id"]
    conn.commit()
    return customer_id


def insert_products(conn: psycopg.Connection) -> list[dict]:
    products = []
    with conn.cursor(row_factory=dict_row) as cur:
        for p in NEW_PRODUCTS:
            cur.execute(
                """
                INSERT INTO products (sku, name, category, description, price_cents, metal, stone, sizes_available, stock_by_size)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (sku) DO UPDATE SET name = EXCLUDED.name
                RETURNING id, sku, name, price_cents
                """,
                (
                    p["sku"], p["name"], p["category"], p["description"], p["price_cents"],
                    p["metal"], p["stone"],
                    Json(p["sizes_available"]) if p["sizes_available"] is not None else None,
                    Json(p["stock_by_size"]),
                ),
            )
            products.append(cur.fetchone())
    conn.commit()
    return products


def insert_order(conn: psycopg.Connection, customer_id: str, order_number: str, status: str,
                  placed_hours_ago: float, product: dict, quantity: int = 1, size: str | None = None) -> None:
    placed_at = datetime.now(timezone.utc) - timedelta(hours=placed_hours_ago)
    unit_price = product["price_cents"]
    total = unit_price * quantity
    shipped_at = placed_at + timedelta(days=1) if status in ("SHIPPED", "DELIVERED") else None
    delivered_at = placed_at + timedelta(days=4) if status == "DELIVERED" else None

    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            INSERT INTO orders (order_number, customer_id, status, placed_at, shipped_at, delivered_at,
                                 total_amount_cents, shipping_address, payment_status)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'paid')
            ON CONFLICT (order_number) DO UPDATE SET status = EXCLUDED.status
            RETURNING id
            """,
            (
                order_number, customer_id, status, placed_at, shipped_at, delivered_at, total,
                Json({"line1": "221B Residency Road", "city": "Bengaluru", "state": "KA", "postal_code": "560025", "country": "IN"}),
            ),
        )
        order_id = cur.fetchone()["id"]

        cur.execute("DELETE FROM order_items WHERE order_id = %s", (order_id,))
        cur.execute(
            """
            INSERT INTO order_items (order_id, product_id, quantity, unit_price_cents, size)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (order_id, product["id"], quantity, unit_price, size),
        )

        cur.execute("DELETE FROM order_status_history WHERE order_id = %s", (order_id,))
        cur.execute(
            "INSERT INTO order_status_history (order_id, from_status, to_status, reason) VALUES (%s, NULL, 'PLACED', 'order_created')",
            (order_id,),
        )
        if status in ("SHIPPED", "DELIVERED"):
            cur.execute(
                "INSERT INTO order_status_history (order_id, from_status, to_status, reason) VALUES (%s, 'PLACED', 'SHIPPED', 'dispatched')",
                (order_id,),
            )
        if status == "DELIVERED":
            cur.execute(
                "INSERT INTO order_status_history (order_id, from_status, to_status, reason) VALUES (%s, 'SHIPPED', 'DELIVERED', 'delivered')",
                (order_id,),
            )
    conn.commit()
    print(f"  {order_number}: {status}, placed {placed_hours_ago}h ago -> {product['name']}")


def main():
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise SystemExit("DATABASE_URL not set (check .env)")

    with psycopg.connect(database_url) as conn:
        customer_id = upsert_customer(conn)
        print(f"Demo customer: {DEMO_NAME} <{DEMO_EMAIL}> ({customer_id})")

        products = insert_products(conn)
        print(f"Added/updated {len(products)} products.")

        print("Orders:")
        insert_order(conn, customer_id, "TPJ-DEMO01", "PLACED", placed_hours_ago=3, product=products[0], size="7")
        insert_order(conn, customer_id, "TPJ-DEMO02", "SHIPPED", placed_hours_ago=72, product=products[1])

    print("\nDone. Chat as this user via POST /conversations {\"email\": \"" + DEMO_EMAIL + "\"}")


if __name__ == "__main__":
    main()
