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

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src.tools.address_tools import address_snapshot as _address_snapshot  # noqa: E402
from src.tools.billing import decompose_price  # noqa: E402  (needs the path above)

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

load_dotenv()

DEMO_NAME = "Arun"
DEMO_EMAIL = "arun@shurutech.com"

NEW_PRODUCTS = [
    {
        "sku": "DPJ-RIN-DEMO1",
        "name": "Rose Gold Diamond Solitaire Ring",
        "category": "ring",
        "description": "Rose Gold Diamond Solitaire Ring — handcrafted, a 1.2ct round-cut diamond set in 18k rose gold.",
        "price_cents": 32_500_000,  # ₹3,25,000 (price_cents = paise)
        "metal": "rose_gold",
        "stone": "diamond",
        "sizes_available": ["5", "6", "7", "8", "9", "10"],
        "stock_by_size": {"5": 3, "6": 4, "7": 5, "8": 4, "9": 2, "10": 1},
        # Invoice attributes. Unlike the generated catalogue (which derives its
        # price from these), these two are hand-priced showcase pieces — so the
        # price is fixed and the metal weight is solved out of it below, at the
        # same rate the seeder uses. That keeps the implied per-gram rate on the
        # demo invoice honest instead of arbitrary.
        "purity_karat": 18,
        "making_charge_percent": 14,
        "stone_value_cents": 27_000_000,   # the 1.2ct solitaire
        "occasion": ["engagement", "wedding", "anniversary"],
        "style": "classic",
    },
    {
        "sku": "DPJ-NEC-DEMO1",
        "name": "Gold Tennis Necklace",
        "category": "necklace",
        "description": "Gold Tennis Necklace — handcrafted, continuous line of diamonds in 18k gold.",
        "price_cents": 48_000_000,  # ₹4,80,000 (price_cents = paise)
        "metal": "gold",
        "stone": "diamond",
        "sizes_available": None,
        "stock_by_size": {"_default": 7},
        "purity_karat": 18,
        "making_charge_percent": 12,
        "stone_value_cents": 32_000_000,   # the continuous diamond line
        "occasion": ["wedding", "festive", "gifting"],
        "style": "statement",
    },
]

# Paise per gram, matching scripts/seed_db.py's seed-time rate table. Duplicated
# rather than imported because seed_db's module scope runs a --reset argument
# parser; a fixed constant in two places is the lesser evil, and a test asserts
# the demo products' implied rate lands on it.
_RATE_18K = 814_000

# Two addresses on purpose: one default so the happy path is one confirmation,
# and a second so the "which address should this go to?" branch is demoable.
DEMO_ADDRESSES = [
    {
        "label": "Home", "recipient_name": "Arun Kumar", "phone": "9845012345",
        "line1": "221B Residency Road", "line2": "Ashok Nagar",
        "city": "Bengaluru", "state": "Karnataka", "postal_code": "560025",
        "is_default": True,
    },
    {
        "label": "Office", "recipient_name": "Arun Kumar", "phone": "9845012345",
        "line1": "14 Church Street", "line2": None,
        "city": "Bengaluru", "state": "Karnataka", "postal_code": "560001",
        "is_default": False,
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


def _implied_weight(p: dict) -> float:
    """Solve the metal weight out of a hand-set price.

    Uses the same decomposition the invoice does, so the weight printed on the
    bill and the rate implied by it are consistent with the price actually
    charged — rather than a round number picked to look good.
    """
    metal_value = decompose_price(
        p["price_cents"],
        making_charge_percent=p["making_charge_percent"],
        stone_value_cents=p["stone_value_cents"],
    )["metal_value_cents"]
    return round(metal_value / _RATE_18K, 3)


def insert_addresses(conn: psycopg.Connection, customer_id: str) -> None:
    """Arun's saved address book, so the demo confirms an address instead of
    dictating one. Idempotent like the rest of this script."""
    with conn.cursor() as cur:
        for addr in DEMO_ADDRESSES:
            cur.execute(
                "SELECT 1 FROM customer_addresses WHERE customer_id = %s AND label = %s",
                (customer_id, addr["label"]),
            )
            if cur.fetchone():
                continue
            cur.execute(
                """
                INSERT INTO customer_addresses
                    (customer_id, label, recipient_name, phone, line1, line2, city,
                     state, postal_code, country, is_default)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'IN', %s)
                """,
                (
                    customer_id, addr["label"], addr["recipient_name"], addr["phone"],
                    addr["line1"], addr["line2"], addr["city"], addr["state"],
                    addr["postal_code"], addr["is_default"],
                ),
            )
    conn.commit()
    print(f"  {len(DEMO_ADDRESSES)} saved addresses (Home is the default).")


def insert_products(conn: psycopg.Connection) -> list[dict]:
    products = []
    with conn.cursor(row_factory=dict_row) as cur:
        for p in NEW_PRODUCTS:
            cur.execute(
                """
                INSERT INTO products (sku, name, category, description, price_cents, metal, stone,
                                      sizes_available, stock_by_size,
                                      purity_karat, making_charge_percent, stone_value_cents,
                                      net_weight_grams, occasion, style)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (sku) DO UPDATE SET
                    name = EXCLUDED.name,
                    purity_karat = EXCLUDED.purity_karat,
                    making_charge_percent = EXCLUDED.making_charge_percent,
                    stone_value_cents = EXCLUDED.stone_value_cents,
                    net_weight_grams = EXCLUDED.net_weight_grams,
                    occasion = EXCLUDED.occasion,
                    style = EXCLUDED.style
                RETURNING id, sku, name, price_cents
                """,
                (
                    p["sku"], p["name"], p["category"], p["description"], p["price_cents"],
                    p["metal"], p["stone"],
                    Json(p["sizes_available"]) if p["sizes_available"] is not None else None,
                    Json(p["stock_by_size"]),
                    p["purity_karat"], p["making_charge_percent"], p["stone_value_cents"],
                    _implied_weight(p), p["occasion"], p["style"],
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
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'PAID')
            ON CONFLICT (order_number) DO UPDATE SET
              status = EXCLUDED.status,
              delivered_at = EXCLUDED.delivered_at,
              -- Reset the resolution columns too. Without this, re-running after
              -- someone has demoed a cancellation or return leaves a stale
              -- reason on an order being put back to PLACED/DELIVERED, which
              -- violates the "only a resolved order carries a reason" CHECK and
              -- makes the script fail on its second run.
              cancellation_reason_code = NULL,
              cancellation_reason_note = NULL,
              return_reason_code = NULL,
              return_reason_note = NULL,
              returned_at = NULL
            RETURNING id
            """,
            (
                order_number, customer_id, status, placed_at, shipped_at, delivered_at, total,
                # The same snapshot place_order would take of the default saved
                # address, rather than a second hand-written copy that could
                # drift from it.
                Json(_address_snapshot(DEMO_ADDRESSES[0])),
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

        print("Addresses:")
        insert_addresses(conn, customer_id)

        print("Orders:")
        insert_order(conn, customer_id, "DPJ-DEMO01", "PLACED", placed_hours_ago=3, product=products[0], size="7")
        insert_order(conn, customer_id, "DPJ-DEMO02", "SHIPPED", placed_hours_ago=72, product=products[1])
        # Delivered 6 days ago (placed 10, delivered +4), so it sits inside the
        # 30-day return window. Without a DELIVERED order there is nothing on
        # this account the return flow can be demonstrated against at all —
        # PLACED and SHIPPED orders are cancellation territory.
        insert_order(conn, customer_id, "DPJ-DEMO03", "DELIVERED", placed_hours_ago=24 * 10,
                     product=products[0], size="7")

    print("\nDone. Log in as this user with OTP-over-email: enter " + DEMO_EMAIL
          + " on the web login screen (or POST /auth/signinup/code {\"email\": ...},"
          " then /auth/signinup/code/consume). /conversations no longer accepts a"
          " client-supplied email — identity comes from the verified session only.")


if __name__ == "__main__":
    main()
