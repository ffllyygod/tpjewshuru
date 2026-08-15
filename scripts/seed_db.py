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


# ============================================================
# Admin-persona seed data — 12 months of history for analytics
# ============================================================
# Everything below runs AFTER the generators above and re-seeds the RNG at its
# own start. That ordering is load-bearing: tests/test_tools.py and
# tests/test_coupon_tools.py pin TPJ-10000..TPJ-10004 to exact statuses, and
# those come out of seed_orders' RNG stream. Appending here (rather than
# rewriting seed_orders) means the volume of history generated below can be
# tuned freely without ever shifting the stream that produces those fixtures.

HISTORY_SEED = 4242

# Order numbers MUST NOT collide with runtime orders. purchase_tools mints
# TPJ-{100000..999999} (6 digits) and the scenario orders are TPJ-1000x, so a
# numeric history range would eventually collide. The H prefix is provably
# disjoint from both, and instantly recognisable as seed data when debugging.
HISTORY_ORDER_PREFIX = "TPJ-H"

# Indian retail jewellery calendar. Volume multiplier by month (1 = January).
# This is the whole reason the admin reports are worth demoing: a flat random
# spread makes "revenue by month" a straight line, which tells you nothing.
MONTH_DEMAND = {
    1: 1.6,   # wedding season continues
    2: 1.6,   # wedding season
    3: 1.0,
    4: 2.2,   # Akshaya Tritiya — gold-buying is auspicious
    5: 2.0,   # Akshaya Tritiya tail + summer weddings
    6: 0.9,
    7: 0.8,   # monsoon lull
    8: 0.8,   # monsoon lull
    9: 0.5,   # Pitru Paksha — inauspicious for purchases, a real trough
    10: 3.0,  # Dhanteras / Diwali — the year's peak
    11: 2.4,  # Diwali tail into wedding season
    12: 1.6,  # wedding season
}

# Category mix shifts with the season, not just volume. Without this, a
# breakdown-by-category-by-month report looks synthetic the moment you read it.
WEDDING_MONTHS = {11, 12, 1, 2}
GOLD_FESTIVAL_MONTHS = {4, 5, 10}

INDIAN_CITIES = [
    ("Mumbai", "400001", "Maharashtra"), ("Delhi", "110001", "Delhi"),
    ("Bengaluru", "560001", "Karnataka"), ("Chennai", "600001", "Tamil Nadu"),
    ("Hyderabad", "500001", "Telangana"), ("Pune", "411001", "Maharashtra"),
    ("Kolkata", "700001", "West Bengal"), ("Ahmedabad", "380001", "Gujarat"),
    ("Jaipur", "302001", "Rajasthan"), ("Surat", "395003", "Gujarat"),
]

ADMIN_USERS = [
    ("Priya Nair", "admin@tpjewellers.com", "admin"),
    ("Store Manager", "manager@tpjewellers.com", "admin"),
]


def seed_admin_users(conn: psycopg.Connection) -> None:
    """Staff accounts. Deliberately NOT flipping arun@shurutech.com to admin —
    every orchestrator/auth test resolves through that email, and making it an
    admin would silently change which prompt and toolset those tests exercise."""
    with conn.cursor() as cur:
        for name, email, role in ADMIN_USERS:
            cur.execute(
                """
                INSERT INTO customers (name, email, role) VALUES (%s, %s, %s)
                ON CONFLICT (email) DO UPDATE SET role = EXCLUDED.role
                """,
                (name, email, role),
            )
    conn.commit()
    print(f"Seeded {len(ADMIN_USERS)} admin users ({', '.join(e for _, e, _ in ADMIN_USERS)}).")


def seed_extra_products(conn: psycopg.Connection, count: int = 26) -> list[dict]:
    """Widen the catalogue so category/product breakdowns aren't 6 rows wide."""
    products = []
    with conn.cursor() as cur:
        for i in range(count):
            category, base_name, has_sizes = random.choice(PRODUCT_TEMPLATES)
            metal = random.choice(METALS)
            stone = random.choice(STONES)
            name = f"{metal.replace('_', ' ').title()} {base_name}" + (
                f" with {stone.title()}" if stone != "none" else ""
            )
            sku = f"TPJ-{category[:3].upper()}-{2000 + i}"
            price_cents = random.randint(1_500_000, 80_000_000)
            # Margin varies by category so the margin report differentiates
            # rather than showing one flat percentage everywhere.
            cost_ratio = random.uniform(0.55, 0.75)
            sizes_available = RING_SIZES if has_sizes else None
            stock_by_size = (
                {s: random.randint(0, 8) for s in RING_SIZES}
                if has_sizes
                else {"_default": random.randint(0, 20)}
            )
            cur.execute(
                """
                INSERT INTO products
                    (sku, name, category, description, price_cents, cost_price_cents,
                     metal, stone, sizes_available, stock_by_size, low_stock_threshold)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id, sku, name, category, price_cents
                """,
                (
                    sku, name, category,
                    f"{name} — handcrafted, {stone if stone != 'none' else 'no stone'}, {metal.replace('_', ' ')}.",
                    price_cents, int(price_cents * cost_ratio), metal, stone,
                    Json(sizes_available) if sizes_available is not None else None,
                    Json(stock_by_size), random.choice([2, 2, 2, 5]),
                ),
            )
            row = cur.fetchone()
            products.append({
                "id": row[0], "sku": row[1], "name": row[2],
                "category": row[3], "price_cents": row[4],
                "sizes_available": sizes_available,
            })
    conn.commit()
    print(f"Seeded {len(products)} additional products (TPJ-*-2000+).")
    return products


def seed_extra_customers(conn: psycopg.Connection, count: int = 140) -> list[dict]:
    # Indian locale: this is an Indian store pricing in rupees, and a "top
    # customers" report full of Faker's default US names reads as obviously
    # synthetic the moment anyone looks at it.
    fake_in = Faker("en_IN")
    customers = []
    with conn.cursor() as cur:
        for _ in range(count):
            # Spread signup dates across ~2 years so "new customers this month"
            # is answerable rather than everyone joining on seed day.
            created_at = datetime.now(timezone.utc) - timedelta(days=random.randint(1, 730))
            cur.execute(
                "INSERT INTO customers (name, email, phone, created_at) VALUES (%s, %s, %s, %s) RETURNING id, name, email",
                (fake_in.name(), fake.unique.email(), fake_in.phone_number(), created_at),
            )
            row = cur.fetchone()
            customers.append({"id": row[0], "name": row[1], "email": row[2]})
    conn.commit()
    print(f"Seeded {len(customers)} additional customers.")
    return customers


def backfill_cost_prices(conn: psycopg.Connection) -> None:
    """Give the original 24 products a cost price too, so margin reports don't
    report most of the catalogue as 'cost data incomplete'."""
    with conn.cursor() as cur:
        cur.execute("SELECT id, price_cents FROM products WHERE cost_price_cents IS NULL")
        rows = cur.fetchall()
        for pid, price_cents in rows:
            cur.execute(
                "UPDATE products SET cost_price_cents = %s WHERE id = %s",
                (int(price_cents * random.uniform(0.55, 0.75)), pid),
            )
    conn.commit()
    print(f"Backfilled cost prices for {len(rows)} products.")


def seed_inventory_edge_cases(conn: psycopg.Connection) -> None:
    """Guarantee the low-stock and out-of-stock reports return something. Without
    this they can come back empty on a lucky RNG draw, which reads as a broken
    tool rather than a healthy warehouse."""
    with conn.cursor() as cur:
        cur.execute("SELECT id, stock_by_size FROM products ORDER BY sku LIMIT 12")
        rows = cur.fetchall()
        for idx, (pid, stock) in enumerate(rows):
            if idx < 4:                      # fully out of stock
                new_stock = {k: 0 for k in stock}
            elif idx < 10:                   # at or below the low-stock threshold
                new_stock = {k: random.choice([1, 2]) for k in stock}
            else:
                continue
            cur.execute(
                "UPDATE products SET stock_by_size = %s, updated_at = now() WHERE id = %s",
                (Json(new_stock), pid),
            )
    conn.commit()
    print("Forced 4 out-of-stock and 6 low-stock products for inventory reporting.")


def _history_status(age_days: int) -> str:
    """Status is derived from order age, never drawn at random per row — an
    order placed 8 months ago that is still 'PLACED' would make every ageing
    or fulfilment report nonsense."""
    if age_days > 30:
        return random.choices(["DELIVERED", "RETURNED", "CANCELLED"], weights=[88, 4, 8])[0]
    if age_days > 5:
        return random.choices(["SHIPPED", "DELIVERED"], weights=[45, 55])[0]
    return random.choices(["PLACED", "CONFIRMED", "SHIPPED"], weights=[40, 35, 25])[0]


def _seasonal_products(products: list[dict], month: int) -> list[dict]:
    """Weight the catalogue toward what actually sells in that season."""
    if month in WEDDING_MONTHS:
        preferred = {"bangle", "necklace"}
    elif month in GOLD_FESTIVAL_MONTHS:
        preferred = {"pendant", "bangle", "earring"}
    else:
        return products
    # Triple the weight of in-season categories by repeating them in the pool.
    return products + [p for p in products if p["category"] in preferred] * 2


def seed_history_orders(
    conn: psycopg.Connection,
    customers: list[dict],
    products: list[dict],
    days: int = 365,
) -> None:
    random.seed(HISTORY_SEED)
    Faker.seed(HISTORY_SEED)

    # Pareto-ish weighting: a small set of repeat buyers should dominate the
    # "top customers" ranking, otherwise that report is a flat, meaningless list.
    vip_count = max(1, len(customers) // 10)
    weights = [8.0] * vip_count + [1.0] * (len(customers) - vip_count)

    now = datetime.now(timezone.utc)
    seq = 0
    per_month: dict[str, int] = {}

    with conn.cursor() as cur:
        for day_offset in range(days, 0, -1):
            day = now - timedelta(days=day_offset)
            mult = MONTH_DEMAND[day.month] * (1.4 if day.weekday() >= 5 else 1.0)
            expected = 2.0 * mult
            n_orders = max(0, int(random.gauss(expected, expected * 0.35)))

            season_pool = _seasonal_products(products, day.month)
            # Bigger baskets during festival/wedding peaks lifts AOV, not just
            # order count — the two move together in real retail.
            peak = day.month in GOLD_FESTIVAL_MONTHS or day.month in WEDDING_MONTHS

            for _ in range(n_orders):
                seq += 1
                order_number = f"{HISTORY_ORDER_PREFIX}{seq:05d}"
                customer = random.choices(customers, weights=weights)[0]
                placed_at = day.replace(
                    hour=random.randint(9, 21), minute=random.randint(0, 59)
                )
                age_days = (now - placed_at).days
                status = _history_status(age_days)

                n_items = random.randint(1, 3) if peak else random.randint(1, 2)
                chosen = random.sample(season_pool, min(n_items, len(season_pool)))
                # random.sample over a weighted pool can repeat a product; dedupe
                # so one order never carries the same product twice.
                seen, items, total_cents = set(), [], 0
                for p in chosen:
                    if p["id"] in seen:
                        continue
                    seen.add(p["id"])
                    qty = random.randint(1, 2)
                    size = random.choice(p["sizes_available"]) if p["sizes_available"] else None
                    total_cents += p["price_cents"] * qty
                    items.append((p["id"], qty, p["price_cents"], size))

                shipped_at = placed_at + timedelta(days=1) if status in ("SHIPPED", "DELIVERED", "RETURNED") else None
                delivered_at = placed_at + timedelta(days=4) if status in ("DELIVERED", "RETURNED") else None
                city, postcode, state = random.choice(INDIAN_CITIES)

                cur.execute(
                    """
                    INSERT INTO orders
                        (order_number, customer_id, status, placed_at, shipped_at,
                         delivered_at, total_amount_cents, shipping_address, payment_status)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING id
                    """,
                    (
                        order_number, customer["id"], status, placed_at, shipped_at,
                        delivered_at, total_cents,
                        Json({
                            "line1": fake.street_address(), "city": city,
                            "state": state, "postal_code": postcode, "country": "IN",
                        }),
                        "REFUNDED" if status in ("CANCELLED", "RETURNED") else "PAID",
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

                if status == "CANCELLED":
                    flow = ["PLACED", "CANCELLED"]
                elif status == "RETURNED":
                    flow = ["PLACED", "CONFIRMED", "SHIPPED", "DELIVERED", "RETURNED"]
                else:
                    flow = STATUS_FLOW[: STATUS_FLOW.index(status) + 1]
                prev = None
                for step in flow:
                    cur.execute(
                        """
                        INSERT INTO order_status_history (order_id, from_status, to_status, changed_at, reason)
                        VALUES (%s, %s, %s, %s, %s)
                        """,
                        (order_id, prev, step, placed_at,
                         "Customer requested cancellation" if step == "CANCELLED" else None),
                    )
                    prev = step

                key = placed_at.strftime("%Y-%m")
                per_month[key] = per_month.get(key, 0) + 1

    conn.commit()
    print(f"Seeded {seq} history orders ({HISTORY_ORDER_PREFIX}00001..{HISTORY_ORDER_PREFIX}{seq:05d}) over {days} days.")
    print("  monthly volume (seasonality check):")
    for month in sorted(per_month):
        print(f"    {month}: {per_month[month]:4d} {'#' * (per_month[month] // 5)}")


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

        # --- admin-persona data. Must stay LAST: these re-seed the RNG, so
        # nothing above them can be shifted by tuning anything below. ---
        seed_admin_users(conn)
        extra_products = seed_extra_products(conn)
        extra_customers = seed_extra_customers(conn)
        seed_history_orders(conn, customers + extra_customers, products + extra_products)
        backfill_cost_prices(conn)
        seed_inventory_edge_cases(conn)

    print("\nDone.")


if __name__ == "__main__":
    main()
