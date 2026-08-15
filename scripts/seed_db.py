"""
Seed the TP Jewellers dev database with realistic fake data.

Usage:
    python scripts/seed_db.py           # seed (fails if schema not applied)
    python scripts/seed_db.py --reset   # drop + recreate schema, then seed

Requires DATABASE_URL in .env (see .env.example) and a running Postgres
(docker compose up -d).
"""

import argparse
import os
import random
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg
from dotenv import load_dotenv
from faker import Faker
from psycopg.types.json import Json

ROOT = Path(__file__).resolve().parent.parent
SCHEMA_PATH = ROOT / "src" / "db" / "schema.sql"

fake = Faker()
Faker.seed(42)
random.seed(42)

METALS = ["gold", "silver", "platinum", "rose_gold"]
STONES = ["diamond", "ruby", "emerald", "sapphire", "none"]
RING_SIZES = ["5", "6", "7", "8", "9", "10"]

PRODUCT_TEMPLATES = [
    ("ring", "Solitaire Ring", True),
    ("ring", "Eternity Band", True),
    ("ring", "Halo Engagement Ring", True),
    ("necklace", "Tennis Necklace", False),
    ("necklace", "Pendant Chain", False),
    ("earring", "Stud Earrings", False),
    ("earring", "Drop Earrings", False),
    ("bracelet", "Tennis Bracelet", False),
    ("bracelet", "Charm Bracelet", False),
    ("bangle", "Classic Bangle", False),
    ("pendant", "Solitaire Pendant", False),
]


def apply_schema(conn: psycopg.Connection, reset: bool) -> None:
    with conn.cursor() as cur:
        if reset:
            cur.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
        cur.execute(SCHEMA_PATH.read_text(encoding="utf-8"))
    conn.commit()
    print(f"Schema applied{' (reset)' if reset else ''}.")


def seed_products(conn: psycopg.Connection, count: int = 24) -> list[dict]:
    products = []
    with conn.cursor() as cur:
        for i in range(count):
            category, base_name, has_sizes = random.choice(PRODUCT_TEMPLATES)
            metal = random.choice(METALS)
            stone = random.choice(STONES)
            name = f"{metal.replace('_', ' ').title()} {base_name}" + (
                f" with {stone.title()}" if stone != "none" else ""
            )
            sku = f"TPJ-{category[:3].upper()}-{1000 + i}"
            # price_cents stores the smallest currency unit — paise, since this
            # store prices in INR. Range is realistic Indian retail jewellery
            # pricing (₹15,000 - ₹8,00,000), not a relabeled USD range.
            price_cents = random.randint(1_500_000, 80_000_000)  # ₹15,000 - ₹8,00,000

            sizes_available = RING_SIZES if has_sizes else None
            if has_sizes:
                stock_by_size = {s: random.randint(0, 8) for s in RING_SIZES}
            else:
                stock_by_size = {"_default": random.randint(0, 20)}

            cur.execute(
                """
                INSERT INTO products
                    (sku, name, category, description, price_cents, metal, stone,
                     sizes_available, stock_by_size)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id, sku, name, category, price_cents
                """,
                (
                    sku,
                    name,
                    category,
                    f"{name} — handcrafted, {stone if stone != 'none' else 'no stone'}, {metal.replace('_', ' ')}.",
                    price_cents,
                    metal,
                    stone,
                    Json(sizes_available) if sizes_available is not None else None,
                    Json(stock_by_size),
                ),
            )
            row = cur.fetchone()
            products.append(
                {
                    "id": row[0],
                    "sku": row[1],
                    "name": row[2],
                    "category": row[3],
                    "price_cents": row[4],
                    "sizes_available": sizes_available,
                }
            )
    conn.commit()
    print(f"Seeded {len(products)} products.")
    return products


def seed_customers(conn: psycopg.Connection, count: int = 10) -> list[dict]:
    customers = []
    with conn.cursor() as cur:
        for _ in range(count):
            name = fake.name()
            email = fake.unique.email()
            phone = fake.phone_number()
            cur.execute(
                "INSERT INTO customers (name, email, phone) VALUES (%s, %s, %s) RETURNING id, name, email",
                (name, email, phone),
            )
            row = cur.fetchone()
            customers.append({"id": row[0], "name": row[1], "email": row[2]})
    conn.commit()
    print(f"Seeded {len(customers)} customers.")
    return customers


STATUS_FLOW = ["PLACED", "CONFIRMED", "SHIPPED", "DELIVERED"]


def seed_orders(
    conn: psycopg.Connection,
    customers: list[dict],
    products: list[dict],
    count: int = 20,
) -> None:
    """
    Deliberately spans every cancellation-eligibility case:
    - recent PLACED order (should be cancellable)
    - CONFIRMED order older than the cancellation window (should be rejected on time)
    - SHIPPED / DELIVERED orders (should be rejected on status)
    - an already-CANCELLED order (idempotency check target)
    """
    now = datetime.now(timezone.utc)

    with conn.cursor() as cur:
        for i in range(count):
            customer = random.choice(customers)
            order_number = f"TPJ-{10000 + i}"

            # Force a spread of scenarios for the first few orders (deterministic
            # for manual testing); randomize the rest for volume.
            if i == 0:
                status, placed_offset_hours = "PLACED", 2          # cancellable
            elif i == 1:
                status, placed_offset_hours = "CONFIRMED", 24 * 10  # too old to cancel
            elif i == 2:
                status, placed_offset_hours = "SHIPPED", 24 * 3     # wrong status
            elif i == 3:
                status, placed_offset_hours = "DELIVERED", 24 * 20  # wrong status
            elif i == 4:
                status, placed_offset_hours = "CANCELLED", 24 * 5   # already cancelled
            else:
                status = random.choice(STATUS_FLOW + ["CANCELLED"])
                placed_offset_hours = random.randint(1, 24 * 30)

            placed_at = now - timedelta(hours=placed_offset_hours)
            shipped_at = placed_at + timedelta(days=1) if status in (
                "SHIPPED", "DELIVERED"
            ) else None
            delivered_at = placed_at + timedelta(days=4) if status == "DELIVERED" else None

            n_items = random.randint(1, 3)
            chosen_products = random.sample(products, n_items)
            total_cents = 0
            items = []
            for p in chosen_products:
                qty = random.randint(1, 2)
                size = random.choice(p["sizes_available"]) if p["sizes_available"] else None
                total_cents += p["price_cents"] * qty
                items.append((p["id"], qty, p["price_cents"], size))

            cur.execute(
                """
                INSERT INTO orders
                    (order_number, customer_id, status, placed_at, shipped_at,
                     delivered_at, total_amount_cents, shipping_address, payment_status)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (
                    order_number,
                    customer["id"],
                    status,
                    placed_at,
                    shipped_at,
                    delivered_at,
                    total_cents,
                    psycopg.types.json.Json(
                        {
                            "line1": fake.street_address(),
                            "city": fake.city(),
                            "postal_code": fake.postcode(),
                            "country": "US",
                        }
                    ),
                    "REFUNDED" if status == "CANCELLED" else "PAID",
                ),
            )
            order_id = cur.fetchone()[0]

            for product_id, qty, unit_price_cents, size in items:
                cur.execute(
                    """
                    INSERT INTO order_items (order_id, product_id, quantity, unit_price_cents, size)
                    VALUES (%s, %s, %s, %s, %s)
                    """,
                    (order_id, product_id, qty, unit_price_cents, size),
                )

            # status history: PLACED -> ... -> current status
            history = STATUS_FLOW[: STATUS_FLOW.index(status) + 1] if status in STATUS_FLOW else ["PLACED", "CANCELLED"]
            prev = None
            for step_status in history:
                cur.execute(
                    """
                    INSERT INTO order_status_history (order_id, from_status, to_status, reason)
                    VALUES (%s, %s, %s, %s)
                    """,
                    (
                        order_id,
                        prev,
                        step_status,
                        "Customer requested cancellation" if step_status == "CANCELLED" else None,
                    ),
                )
                prev = step_status

    conn.commit()
    print(f"Seeded {count} orders (order #TPJ-10000..TPJ-{10000 + count - 1}).")
    print("  TPJ-10000: PLACED, 2h ago      -> cancellable")
    print("  TPJ-10001: CONFIRMED, 10d ago  -> outside cancellation window")
    print("  TPJ-10002: SHIPPED             -> not cancellable (status)")
    print("  TPJ-10003: DELIVERED           -> not cancellable (status)")
    print("  TPJ-10004: CANCELLED           -> already cancelled")


KNOWLEDGE_DOCS = [
    (
        "Cancellation Policy",
        "policy",
        """Orders can be cancelled free of charge within 24 hours of being placed, as
long as the order has not yet been marked CONFIRMED for production or shipping.
Custom and engraved pieces cannot be cancelled once production has started —
production begins as soon as an order is CONFIRMED. Once an order ships, it can
no longer be cancelled; you may instead request a return within our 30-day
return window (see Return Policy). Refunds for cancelled orders are issued to
the original payment method within 5-7 business days.""",
    ),
    (
        "Return Policy",
        "policy",
        """We accept returns within 30 days of delivery for unworn items in their
original packaging with all tags and certificates of authenticity attached.
Custom-sized rings and engraved items are final sale and cannot be returned
unless defective. To start a return, contact support with your order number;
we'll email a prepaid return label. Refunds are processed within 5-7 business
days of us receiving the returned item.""",
    ),
    (
        "Ring Sizing Guide",
        "sizing_guide",
        """Not sure of your ring size? The most accurate method is visiting any
jeweller for a free sizing, but you can also measure an existing ring that
fits the intended finger: measure its inner diameter in millimetres and match
it against our size chart (available on the product page). If you're between
sizes, we recommend sizing up. We offer one free resizing within 60 days of
delivery for standard rings (not applicable to eternity bands, which cannot be
resized due to the continuous stone setting).""",
    ),
    (
        "Jewellery Care Guide",
        "care_guide",
        """Store pieces separately in soft pouches to prevent scratching. Avoid
contact with perfume, lotion, and chlorinated water — apply these before
putting on jewellery, not after. Clean gold and platinum pieces with warm
water, mild soap, and a soft brush; avoid ultrasonic cleaners on pieces with
emeralds or opals, as the vibration can damage these softer stones. We
recommend a professional cleaning and prong check once a year for
daily-worn rings.""",
    ),
    (
        "Shipping FAQ",
        "faq",
        """Standard shipping takes 3-5 business days within India; expedited
options are available at checkout. All orders over ₹40,000 ship fully insured
and require a signature on delivery. International shipping is available to
select countries and typically takes 7-14 business days, with customs duties
the responsibility of the recipient. You'll receive a tracking link by email
as soon as your order ships.""",
    ),
    (
        "Engraving FAQ",
        "faq",
        """Most rings and pendants can be engraved with up to 20 characters at
checkout for a flat ₹2,000 fee. Engraved items take an additional 2-3 business
days to produce and, because they're made to order, cannot be cancelled or
returned unless defective. Preview your engraving text carefully at
checkout — we are not able to modify it once production has started.""",
    ),
]


def seed_knowledge_docs(conn: psycopg.Connection) -> None:
    with conn.cursor() as cur:
        for title, category, content in KNOWLEDGE_DOCS:
            cur.execute(
                """
                INSERT INTO knowledge_docs (title, content, category)
                VALUES (%s, %s, %s)
                """,
                (title, content.strip(), category),
            )
    conn.commit()
    print(f"Seeded {len(KNOWLEDGE_DOCS)} knowledge docs (embeddings NOT generated —"
          " run scripts/index_knowledge.py separately once VOYAGE_API_KEY is set).")


def seed_coupon_policy(conn: psycopg.Connection) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO coupon_policy (name, cancellation_bonus_percent, return_bonus_percent, expiry_days)
            VALUES ('default', 10, 15, 180)
            ON CONFLICT (name) DO NOTHING
            """
        )
    conn.commit()
    print("Seeded default coupon policy (10% cancellation / 15% return bonus, 180-day expiry).")


GOLD_SIP_PLANS = [
    # (name, tenure_months, bonus_percent, early_exit_penalty_percent)
    # `name` is the ONLY identifier — deliberately already human-readable
    # (not a separate slug + display-label pair) so there's no ambiguity
    # between what the agent shows the customer and what it passes back to
    # start_gold_sip. A slug/display-name split caused the model to invent
    # its own display string and then pass that (not the real key) as the
    # tool argument — see JOURNAL.md.
    ("Classic 6-Month", 6, 5, 5),
    ("Standard 11-Month", 11, 9, 8),
    ("Premium 12-Month", 12, 10, 10),
]


def seed_gold_sip_plans(conn: psycopg.Connection) -> None:
    with conn.cursor() as cur:
        for name, tenure, bonus, penalty in GOLD_SIP_PLANS:
            cur.execute(
                """
                INSERT INTO gold_sip_plans (name, tenure_months, bonus_percent, early_exit_penalty_percent)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (name) DO NOTHING
                """,
                (name, tenure, bonus, penalty),
            )
    conn.commit()
    print(f"Seeded {len(GOLD_SIP_PLANS)} Gold SIP plan tiers.")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--reset", action="store_true", help="drop and recreate the schema first")
    args = parser.parse_args()

    load_dotenv(ROOT / ".env")
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        print("DATABASE_URL not set. Copy .env.example to .env and fill it in.", file=sys.stderr)
        sys.exit(1)

    with psycopg.connect(database_url) as conn:
        apply_schema(conn, reset=args.reset)
        products = seed_products(conn)
        customers = seed_customers(conn)
        seed_orders(conn, customers, products)
        seed_knowledge_docs(conn)
        seed_coupon_policy(conn)
        seed_gold_sip_plans(conn)

    print("\nDone.")


if __name__ == "__main__":
    main()
