"""Order creation — closes the "browse but can't buy" gap (search_products/
get_product_details existed with no way to actually purchase). Needed so
coupon redemption (src/tools/coupon_tools.py) has something real to
demonstrate against.

Security guardrails (same class of concern as coupon balances):
  - price and total are always computed server-side from `products.price_cents`
    — never accepted as a caller-supplied number.
  - Stock decrement is a single atomic guarded UPDATE (not read-check-write),
    so two concurrent orders for the last unit of something can't both
    succeed and oversell.
  - If a coupon_code is given, applying it reuses coupon_tools.redeem_coupon
    verbatim (ownership/expiry/atomic-balance checks included) rather than
    re-implementing any of that here.
"""

from __future__ import annotations

import secrets

from psycopg.rows import dict_row

from src.db.connection import get_conn
from src.tools.coupon_tools import redeem_coupon


def _new_order_number(cur) -> str:
    # Retry-on-conflict rather than assuming uniqueness — same defensive
    # posture as everything else touching money/state in this codebase.
    for _ in range(5):
        candidate = f"TPJ-{secrets.randbelow(900000) + 100000}"
        cur.execute("SELECT 1 FROM orders WHERE order_number = %s", (candidate,))
        if not cur.fetchone():
            return candidate
    raise RuntimeError("Could not generate a unique order number after 5 attempts.")


def place_order(customer_id: str, sku: str, quantity: int = 1, size: str | None = None, coupon_code: str | None = None) -> dict:
    """Place a new order for a product. Optionally applies a coupon's balance
    to the total in the same call (via coupon_tools.redeem_coupon)."""
    if not isinstance(quantity, int) or quantity < 1:
        return {"placed": False, "reason": "bad_quantity", "message": "Quantity must be a positive whole number."}

    with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT id, price_cents, sizes_available, stock_by_size FROM products WHERE sku = %s",
            (sku,),
        )
        product = cur.fetchone()
        if not product:
            return {"placed": False, "reason": "product_not_found", "message": f"No product with SKU {sku}."}

        sized = product["sizes_available"] is not None
        if sized:
            if not size or size not in product["sizes_available"]:
                return {
                    "placed": False,
                    "reason": "invalid_size",
                    "message": f"Choose a size from {product['sizes_available']}.",
                }
            size_key = size
        else:
            size_key = "_default"

        # Atomic, guarded stock decrement — the WHERE re-checks the current
        # count at write time so two concurrent orders can't both succeed
        # against the last unit.
        cur.execute(
            """
            UPDATE products
            SET stock_by_size = jsonb_set(
                stock_by_size, ARRAY[%s], to_jsonb((stock_by_size->>%s)::int - %s)
            )
            WHERE id = %s AND (stock_by_size->>%s)::int >= %s
            RETURNING id
            """,
            (size_key, size_key, quantity, product["id"], size_key, quantity),
        )
        if not cur.fetchone():
            return {"placed": False, "reason": "insufficient_stock", "message": "Not enough stock for that quantity/size."}

        total_amount_cents = product["price_cents"] * quantity  # server-computed, never caller-supplied
        order_number = _new_order_number(cur)

        cur.execute(
            """
            INSERT INTO orders (order_number, customer_id, status, total_amount_cents, payment_status)
            VALUES (%s, %s, 'PLACED', %s, 'PAID')
            RETURNING id, placed_at
            """,
            (order_number, customer_id, total_amount_cents),
        )
        order = cur.fetchone()

        cur.execute(
            "INSERT INTO order_items (order_id, product_id, quantity, unit_price_cents, size) VALUES (%s, %s, %s, %s, %s)",
            (order["id"], product["id"], quantity, product["price_cents"], size if sized else None),
        )
        cur.execute(
            "INSERT INTO order_status_history (order_id, from_status, to_status, reason) VALUES (%s, NULL, 'PLACED', 'order_placed')",
            (order["id"],),
        )
        conn.commit()

    result = {
        "placed": True,
        "currency": "USD",
        "order_number": order_number,
        "total_amount_cents": total_amount_cents,
        "placed_at": order["placed_at"].isoformat(),
    }

    if coupon_code:
        coupon_result = redeem_coupon(customer_id, coupon_code, order_number)
        result["coupon_applied"] = coupon_result.get("redeemed", False)
        if coupon_result.get("redeemed"):
            result["discount_cents"] = coupon_result["discount_cents"]
            result["final_amount_cents"] = total_amount_cents - coupon_result["discount_cents"]
        else:
            # Order still stands even if the coupon didn't apply — report why
            # separately rather than rolling back a successful purchase.
            result["coupon_error"] = coupon_result.get("message")

    return result
