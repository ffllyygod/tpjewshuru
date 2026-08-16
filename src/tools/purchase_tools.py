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
  - If a coupon_code or gold_sip_code is given, applying it reuses
    coupon_tools.redeem_coupon / gold_sip_tools.redeem_gold_sip verbatim
    (ownership/expiry/atomic-balance checks included) rather than
    re-implementing any of that here.
"""

from __future__ import annotations

import secrets

from psycopg.rows import dict_row
from psycopg.types.json import Json

from src.db.connection import get_conn
from src.tools.address_tools import address_snapshot, get_address_for_order
from src.tools.coupon_tools import redeem_coupon
from src.tools.formatting import format_address, format_inr
from src.tools.gold_sip_tools import redeem_gold_sip

# What the store accepts. COD is deliberately in the list and deliberately does
# NOT mark an order paid — see confirm_payment.
PAYMENT_METHODS = {"UPI", "CARD", "NETBANKING", "COD"}


def _new_order_number(cur) -> str:
    # Retry-on-conflict rather than assuming uniqueness — same defensive
    # posture as everything else touching money/state in this codebase.
    for _ in range(5):
        candidate = f"DPJ-{secrets.randbelow(900000) + 100000}"
        cur.execute("SELECT 1 FROM orders WHERE order_number = %s", (candidate,))
        if not cur.fetchone():
            return candidate
    raise RuntimeError("Could not generate a unique order number after 5 attempts.")


def place_order(
    customer_id: str,
    sku: str,
    quantity: int = 1,
    size: str | None = None,
    coupon_code: str | None = None,
    gold_sip_code: str | None = None,
    address_id: str | None = None,
) -> dict:
    """Place a new order for a product, awaiting payment.

    Creates the order with `payment_status = 'PENDING'`: placing is not paying.
    The customer sees an itemised bill (src/tools/billing.generate_invoice) and
    then confirms a method through `confirm_payment`. Before this, orders were
    born 'PAID' with no payment step having happened at all.

    Optionally applies a coupon's or a matured Gold SIP's balance to the total in
    the same call — not both (an order can only carry one discount source; see
    the DB CHECK on orders).
    """
    if not isinstance(quantity, int) or quantity < 1:
        return {"placed": False, "reason": "bad_quantity", "message": "Quantity must be a positive whole number."}

    with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
        # Resolve the delivery address FIRST, before the stock decrement below.
        # Order of operations matters: bailing out after decrementing would
        # destroy stock for an order that was never created.
        address, address_error = get_address_for_order(cur, customer_id, address_id)
        if address_error:
            return {"placed": False, **address_error}

        cur.execute(
            "SELECT id, name, price_cents, sizes_available, stock_by_size FROM products WHERE sku = %s",
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

        snapshot = address_snapshot(address)
        cur.execute(
            """
            INSERT INTO orders (order_number, customer_id, status, total_amount_cents,
                                payment_status, shipping_address)
            VALUES (%s, %s, 'PLACED', %s, 'PENDING', %s)
            RETURNING id, placed_at
            """,
            (order_number, customer_id, total_amount_cents, Json(snapshot)),
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
        "currency": "INR",
        "order_number": order_number,
        "total_amount_cents": total_amount_cents,
        "total_amount_display": format_inr(total_amount_cents),
        "placed_at": order["placed_at"].isoformat(),
        # What was actually bought, echoed back. JOURNAL.md records the model
        # describing the right product in prose while ordering a wrong-but-valid
        # SKU; the tool naming what it just did is how that becomes visible.
        "items": [{
            "sku": sku,
            "name": product["name"],
            "quantity": quantity,
            "size": size if sized else None,
            "unit_price_cents": product["price_cents"],
            "unit_price_display": format_inr(product["price_cents"]),
        }],
        "payment_status": "PENDING",
        "shipping_address_display": format_address(snapshot),
        "shipping_address_source": "chosen" if address_id else "default",
        "next_step": (
            "Nothing has been paid yet. Show the bill with generate_invoice, then ask "
            "which payment method they'd like before calling confirm_payment."
        ),
    }

    if coupon_code and gold_sip_code:
        result["discount_error"] = "Only one of coupon_code or gold_sip_code can be applied to an order — pick one."
    elif coupon_code:
        coupon_result = redeem_coupon(customer_id, coupon_code, order_number)
        result["coupon_applied"] = coupon_result.get("redeemed", False)
        if coupon_result.get("redeemed"):
            final = total_amount_cents - coupon_result["discount_cents"]
            result["discount_cents"] = coupon_result["discount_cents"]
            result["discount_display"] = format_inr(coupon_result["discount_cents"])
            result["final_amount_cents"] = final
            result["final_amount_display"] = format_inr(final)
        else:
            # Order still stands even if the coupon didn't apply — report why
            # separately rather than rolling back a successful purchase.
            result["coupon_error"] = coupon_result.get("message")
    elif gold_sip_code:
        sip_result = redeem_gold_sip(customer_id, gold_sip_code, order_number)
        result["gold_sip_applied"] = sip_result.get("redeemed", False)
        if sip_result.get("redeemed"):
            final = total_amount_cents - sip_result["discount_cents"]
            result["discount_cents"] = sip_result["discount_cents"]
            result["discount_display"] = format_inr(sip_result["discount_cents"])
            result["final_amount_cents"] = final
            result["final_amount_display"] = format_inr(final)
        else:
            result["gold_sip_error"] = sip_result.get("message")

    return result


def _new_payment_reference(cur) -> str:
    """Retry-on-conflict, same posture as _new_order_number."""
    for _ in range(5):
        candidate = f"DPJ-PAY-{secrets.randbelow(900000) + 100000}"
        cur.execute("SELECT 1 FROM orders WHERE payment_reference = %s", (candidate,))
        if not cur.fetchone():
            return candidate
    raise RuntimeError("Could not generate a unique payment reference after 5 attempts.")


def confirm_payment(customer_id: str, order_number: str, payment_method: str) -> dict:
    """Record payment for an order that is awaiting it.

    No confirmation token, deliberately. The token protocol on cancellation
    (src/tools/order_tools.py) exists because cancelling is destructive and
    irreversible; paying is additive and idempotent, like pay_sip_installment.
    What guards this instead is structural and re-checked here from scratch:
    ownership, that the order isn't cancelled or returned, and the atomic
    `WHERE payment_status = 'PENDING'` on the UPDATE — so two concurrent calls
    cannot both take payment, and an already-paid order reports the reference it
    already has rather than minting a second one.

    Cash on delivery records the METHOD and leaves the order PENDING. Marking it
    PAID would put in the database a fact nobody observed — the money is
    collected at the door, by someone else, later.
    """
    method = str(payment_method or "").strip().upper()
    if method not in PAYMENT_METHODS:
        return {
            "confirmed": False,
            "reason": "bad_payment_method",
            "message": (
                f"'{payment_method}' isn't a payment method we take. "
                f"Use one of: {', '.join(sorted(PAYMENT_METHODS))}."
            ),
        }

    with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT id, status::text AS status, payment_status, payment_method,
                   payment_reference, paid_at, total_amount_cents, discount_cents
            FROM orders WHERE order_number = %s AND customer_id = %s
            """,
            (order_number, customer_id),
        )
        order = cur.fetchone()
        if not order:
            return {
                "confirmed": False,
                "reason": "not_found",
                "message": f"No order {order_number} on your account.",
            }

        payable = int(order["total_amount_cents"]) - int(order["discount_cents"] or 0)

        if order["status"] in ("CANCELLED", "RETURNED"):
            return {
                "confirmed": False,
                "reason": "wrong_status",
                "message": f"Order {order_number} is {order['status'].lower()} — there's nothing to pay.",
            }
        if order["payment_status"] in ("REFUNDED", "REFUND_PENDING"):
            return {
                "confirmed": False,
                "reason": "wrong_status",
                "message": f"Order {order_number} is being refunded, not paid.",
            }
        if order["payment_status"] == "PAID":
            # Idempotent and honest: report the existing payment rather than
            # either erroring confusingly or claiming a fresh success.
            return {
                "confirmed": False,
                "reason": "already_paid",
                "message": f"Order {order_number} was already paid.",
                "payment_reference": order["payment_reference"],
                "payment_method": order["payment_method"],
                "paid_at": order["paid_at"].isoformat() if order["paid_at"] else None,
                "amount_paid_cents": payable,
                "amount_paid_display": format_inr(payable),
            }

        if method == "COD":
            cur.execute(
                """
                UPDATE orders SET payment_method = 'COD'
                WHERE id = %s AND payment_status = 'PENDING'
                RETURNING id
                """,
                (order["id"],),
            )
            if not cur.fetchone():
                return {"confirmed": False, "reason": "status_changed",
                        "message": "That order's payment status changed — check it again."}
            cur.execute(
                "INSERT INTO order_status_history (order_id, from_status, to_status, reason) "
                "VALUES (%s, %s::order_status, %s::order_status, 'payment_method_cod')",
                (order["id"], order["status"], order["status"]),
            )
            conn.commit()
            return {
                "confirmed": True,
                "order_number": order_number,
                "payment_status": "PENDING",
                "payment_method": "COD",
                "collected_on_delivery": True,
                "amount_due_cents": payable,
                "amount_due_display": format_inr(payable),
                "message": (
                    f"Cash on delivery recorded. {format_inr(payable)} is payable to the "
                    "courier when the order arrives — nothing has been charged now."
                ),
            }

        reference = _new_payment_reference(cur)
        cur.execute(
            """
            UPDATE orders
            SET payment_status = 'PAID', paid_at = now(),
                payment_method = %s, payment_reference = %s
            WHERE id = %s AND payment_status = 'PENDING'
            RETURNING paid_at
            """,
            (method, reference, order["id"]),
        )
        updated = cur.fetchone()
        if not updated:
            # Lost the race against a concurrent confirm_payment. Never report
            # a success this call didn't make.
            return {"confirmed": False, "reason": "status_changed",
                    "message": "That order was just paid by another request — check its status."}

        cur.execute(
            "INSERT INTO order_status_history (order_id, from_status, to_status, reason) "
            "VALUES (%s, %s::order_status, %s::order_status, %s)",
            (order["id"], order["status"], order["status"], f"payment_confirmed:{method}"),
        )
        conn.commit()

    return {
        "confirmed": True,
        "order_number": order_number,
        "payment_status": "PAID",
        "payment_method": method,
        "payment_reference": reference,
        "paid_at": updated["paid_at"].isoformat(),
        "amount_paid_cents": payable,
        "amount_paid_display": format_inr(payable),
        "message": f"Payment of {format_inr(payable)} recorded by {method}.",
    }
