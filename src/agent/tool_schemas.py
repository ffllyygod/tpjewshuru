"""Anthropic tool-use schemas for the agent's callable tools.

`customer_id` is deliberately NOT a parameter in any schema — the agent
never gets to choose whose orders it's looking at. The orchestrator injects
the session's authenticated customer_id when it actually calls the
underlying Python function. This is the single most important guardrail in
this codebase: it makes "look up someone else's order" not a prompt you can
talk the model out of, but a capability that doesn't exist.
"""

TOOLS = [
    {
        "name": "list_customer_orders",
        "description": "List the current customer's recent orders (most recent first). Use this when the customer asks about 'my orders' generally, or you need an order_number to work with.",
        "input_schema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "Max orders to return.", "default": 10},
            },
        },
    },
    {
        "name": "get_order_status",
        "description": "Get full status and item details for one order belonging to the current customer.",
        "input_schema": {
            "type": "object",
            "properties": {
                "order_number": {"type": "string", "description": "e.g. TPJ-10001"},
            },
            "required": ["order_number"],
        },
    },
    {
        "name": "check_cancellation_eligibility",
        "description": (
            "Check whether an order can be cancelled and, if so, obtain a confirmation_token. "
            "ALWAYS call this before cancel_order — never call cancel_order without a token from "
            "this call in the same conversation."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "order_number": {"type": "string"},
            },
            "required": ["order_number"],
        },
    },
    {
        "name": "cancel_order",
        "description": (
            "Cancel an order. You must have already called check_cancellation_eligibility for this "
            "exact order earlier in this conversation, AND the customer must have explicitly said "
            "yes/confirm to cancelling in their own words before you call this. Never call this "
            "speculatively or to 'check' something."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "order_number": {"type": "string"},
            },
            "required": ["order_number"],
        },
    },
    {
        "name": "offer_settlement_options",
        "description": (
            "Show the cash-refund vs. instant-coupon choice for an order that is ALREADY cancelled or "
            "returned. Read-only, creates nothing. Call this right after a successful cancel_order (or "
            "for an order already in RETURNED status) to present both options before the customer picks one."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"order_number": {"type": "string"}},
            "required": ["order_number"],
        },
    },
    {
        "name": "issue_coupon",
        "description": (
            "Issue the instant store-credit coupon for a cancelled/returned order. Only call this after "
            "offer_settlement_options AND the customer has explicitly chosen the coupon over a cash refund."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"order_number": {"type": "string"}},
            "required": ["order_number"],
        },
    },
    {
        "name": "request_cash_refund",
        "description": (
            "Record that the customer chose a cash refund (5-7 business days) instead of a coupon, for an "
            "already cancelled/returned order. Only call this after the customer has explicitly chosen cash."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"order_number": {"type": "string"}},
            "required": ["order_number"],
        },
    },
    {
        "name": "get_my_coupons",
        "description": "List the current customer's coupons (code, status, remaining balance, expiry).",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "redeem_coupon",
        "description": (
            "Apply a coupon's balance toward an order's total. Confirm the resulting total with the "
            "customer before finalizing anything else. Prefer passing coupon_code to place_order "
            "directly instead of calling this separately after — only use this for an order that "
            "already exists without a discount applied."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "code": {"type": "string", "description": "The coupon code, e.g. TPJ-CPN-XXXXXXXXXXXX"},
                "order_number": {"type": "string", "description": "The order's order_number, e.g. TPJ-793859 — this is NOT a product SKU (e.g. TPJ-RIN-1010 is a SKU, not an order_number)."},
            },
            "required": ["code", "order_number"],
        },
    },
    {
        "name": "place_order",
        "description": (
            "Place a new order for a product. Confirm the product, size (if applicable), and quantity back "
            "to the customer before calling this. Optionally applies a coupon code OR a matured Gold SIP "
            "code at the same time (not both — an order can only carry one discount source)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "sku": {"type": "string"},
                "quantity": {"type": "integer", "default": 1},
                "size": {"type": "string", "description": "Required for sized items (e.g. rings)."},
                "coupon_code": {"type": "string", "description": "Optional — apply an existing coupon to this order."},
                "gold_sip_code": {"type": "string", "description": "Optional — apply a matured Gold SIP subscription's balance to this order."},
            },
            "required": ["sku"],
        },
    },
    {
        "name": "search_products",
        "description": "Browse/recommend products from the catalog by category, metal, stone, and/or price range. Use this when the customer wants suggestions or is browsing (e.g. 'show me rings under ₹2,00,000', 'gold necklaces with diamonds').",
        "input_schema": {
            "type": "object",
            "properties": {
                "category": {"type": "string", "enum": ["ring", "necklace", "earring", "bracelet", "bangle", "pendant"]},
                "metal": {"type": "string", "enum": ["gold", "silver", "platinum", "rose_gold"]},
                "stone": {"type": "string", "enum": ["diamond", "ruby", "emerald", "sapphire", "none"]},
                "min_price": {"type": "number", "description": "Minimum price in rupees."},
                "max_price": {"type": "number", "description": "Maximum price in rupees."},
            },
        },
    },
    {
        "name": "get_product_details",
        "description": "Get full details (including description) for a single product by SKU.",
        "input_schema": {
            "type": "object",
            "properties": {
                "sku": {"type": "string"},
            },
            "required": ["sku"],
        },
    },
    {
        "name": "get_metal_rates",
        "description": "Get current gold (24K/22K/18K) and silver spot rates in INR per gram. Use when the customer asks about gold/silver price, rate, or 'what's gold worth today'. Takes no arguments.",
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "list_gold_sip_plans",
        "description": "List available Gold SIP (Systematic Investment Plan) tiers — tenure, maturity bonus %, early-exit penalty %. Use when the customer asks about gold savings/investment schemes.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "start_gold_sip",
        "description": "Start a new Gold SIP subscription. Confirm the plan name, tenure, and monthly amount back to the customer before calling — this commits them to a recurring plan.",
        "input_schema": {
            "type": "object",
            "properties": {
                "plan_name": {"type": "string", "description": "Must exactly match a 'name' value returned by list_gold_sip_plans, e.g. 'Classic 6-Month' — copy it verbatim, don't paraphrase or reformat it."},
                "monthly_amount": {"type": "number", "description": "Monthly installment amount in rupees."},
            },
            "required": ["plan_name", "monthly_amount"],
        },
    },
    {
        "name": "pay_sip_installment",
        "description": "Record the next monthly installment payment for a Gold SIP subscription (demo: callable on-demand, no calendar-month wait). Auto-matures the subscription if this completes the tenure.",
        "input_schema": {
            "type": "object",
            "properties": {"subscription_code": {"type": "string", "description": "e.g. TPJ-SIP-XXXXXXXXXXXX"}},
            "required": ["subscription_code"],
        },
    },
    {
        "name": "get_my_gold_sips",
        "description": "List the current customer's Gold SIP subscriptions — progress, status, redeemable/remaining balance.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "cancel_gold_sip",
        "description": (
            "Exit a Gold SIP subscription early. Forfeits the maturity bonus and a penalty %; the rest is "
            "issued as a coupon (never cash). MUST warn the customer clearly about the forfeited bonus and "
            "get explicit confirmation before calling this."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"subscription_code": {"type": "string"}},
            "required": ["subscription_code"],
        },
    },
    {
        "name": "redeem_gold_sip",
        "description": (
            "Apply a MATURED Gold SIP subscription's balance toward an order's total. Confirm the "
            "resulting total with the customer before finalizing. Prefer passing gold_sip_code to "
            "place_order directly instead of calling this separately after — only use this for an "
            "order that already exists without a discount applied."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "subscription_code": {"type": "string"},
                "order_number": {"type": "string", "description": "The order's order_number, e.g. TPJ-793859 — this is NOT a product SKU (e.g. TPJ-RIN-1010 is a SKU, not an order_number)."},
            },
            "required": ["subscription_code", "order_number"],
        },
    },
    {
        "name": "search_knowledge",
        "description": "Search store policies and guides (cancellation/return policy, ring sizing, care instructions, shipping, engraving). Use for any general policy/how-to question that isn't about a specific order.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
            },
            "required": ["query"],
        },
    },
]

# ---------------------------------------------------------------------------
# Staff-only tools. Advertised ONLY to admin-mode conversations (see
# orchestrator._tools_for) and enforced by orchestrator._ADMIN_ONLY.
#
# `actor_customer_id` is deliberately absent from every schema below, for the
# same reason `customer_id` is absent from the customer schemas: the agent does
# not get to choose who it is acting as. The orchestrator injects it from the
# verified session.
# ---------------------------------------------------------------------------

_PERIOD_DESC = (
    "One of: today, week, month, quarter, year, last_month, last_12_months, custom. "
    "Use 'custom' with start_date and end_date (YYYY-MM-DD) for anything else."
)

ADMIN_TOOLS = [
    {
        "name": "admin_sales_summary",
        "description": (
            "Headline sales figures for a period — order count, net revenue, average order value, "
            "cancellations, returns — plus a comparison against the previous period of the same "
            "length. Use this for 'how were sales last month', 'how are we doing this week', "
            "'what's our revenue this quarter'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "period": {"type": "string", "description": _PERIOD_DESC},
                "start_date": {"type": "string", "description": "YYYY-MM-DD, only with period='custom'."},
                "end_date": {"type": "string", "description": "YYYY-MM-DD, only with period='custom'."},
            },
            "required": ["period"],
        },
    },
    {
        "name": "admin_sales_breakdown",
        "description": (
            "Revenue broken down by ONE dimension. dimension='category' answers 'revenue by "
            "category'; 'product' answers 'top selling products'; 'customer' answers 'who are our "
            "best customers'; 'month' answers 'monthly sales trend'; 'metal' answers 'gold vs "
            "silver'; 'status' answers 'how many orders were cancelled'. Pick the dimension that "
            "matches what was actually asked — don't substitute a different one."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "dimension": {
                    "type": "string",
                    "enum": ["month", "category", "metal", "product", "customer", "status"],
                },
                "period": {"type": "string", "description": _PERIOD_DESC},
                "start_date": {"type": "string"},
                "end_date": {"type": "string"},
                "limit": {"type": "integer", "description": "Max rows, default 10."},
            },
            "required": ["dimension", "period"],
        },
    },
    {
        "name": "admin_inventory_status",
        "description": (
            "Stock levels across the catalogue. filter='low_stock' (at or below each product's "
            "threshold), 'out_of_stock' (zero), or 'all'. Use for 'what's running low', 'what's "
            "out of stock', 'stock levels for rings'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "filter": {"type": "string", "enum": ["low_stock", "out_of_stock", "all"]},
                "category": {
                    "type": "string",
                    "enum": ["ring", "necklace", "earring", "bracelet", "bangle", "pendant"],
                },
                "limit": {"type": "integer"},
            },
            "required": ["filter"],
        },
    },
    {
        "name": "admin_find_orders",
        "description": (
            "Search orders across ALL customers, filtered by status, customer email, and/or date "
            "range. Use for 'show me cancelled orders this week', 'what has this customer ordered'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "status": {
                    "type": "string",
                    "enum": ["PLACED", "CONFIRMED", "SHIPPED", "DELIVERED", "CANCELLED", "RETURNED"],
                },
                "customer_email": {"type": "string"},
                "start_date": {"type": "string", "description": "YYYY-MM-DD"},
                "end_date": {"type": "string", "description": "YYYY-MM-DD"},
                "limit": {"type": "integer"},
            },
        },
    },
    {
        "name": "admin_order_detail",
        "description": (
            "Full detail for one order by order_number, including which customer it belongs to, "
            "its line items, and its complete status history. order_number looks like TPJ-123456 "
            "or TPJ-H00123 — it is NOT a product SKU (e.g. TPJ-RIN-1010 is a SKU)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"order_number": {"type": "string"}},
            "required": ["order_number"],
        },
    },
    {
        "name": "admin_find_customer",
        "description": (
            "Find customers by name, email or phone, with their order count and lifetime spend. "
            "Returns only a masked phone number — use admin_customer_profile for full details on "
            "one specific person."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Name, email, or phone fragment."},
                "limit": {"type": "integer"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "admin_customer_profile",
        "description": (
            "Everything about ONE customer, looked up by their exact email address: recent orders, "
            "coupons, Gold SIP subscriptions, and lifetime value. Use admin_find_customer first if "
            "you don't already have the exact email."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"customer_email": {"type": "string"}},
            "required": ["customer_email"],
        },
    },
    {
        "name": "admin_bot_stats",
        "description": (
            "Aggregate chatbot usage for a period: conversation count, tool-call volume, most-used "
            "tools, and error rate. Returns NO conversation content — customers' messages are not "
            "accessible through this assistant."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"period": {"type": "string", "description": _PERIOD_DESC}},
            "required": ["period"],
        },
    },

    # -----------------------------------------------------------------------
    # Writes. Each is a preview/apply pair; `conversation_id` is absent from
    # every schema below for the same reason `actor_customer_id` is — the
    # orchestrator injects it, and it is what scopes the confirmation to this
    # conversation. A model that could choose it could replay a confirmation
    # from someone else's session.
    # -----------------------------------------------------------------------
    {
        "name": "admin_preview_order_cancellation",
        "description": (
            "STEP 1 of cancelling any customer's order. Read-only: shows who the order belongs to, "
            "its status and total, and what would change. Call this FIRST, tell the staff member "
            "what it says, and wait for them to confirm before calling admin_cancel_order."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "order_number": {"type": "string", "description": "e.g. TPJ-123456. NOT a product SKU."},
                "reason": {
                    "type": "string",
                    "description": (
                        "Why this is being cancelled, in the staff member's own words (e.g. "
                        "'customer called, ordered the wrong size'). Written to the permanent "
                        "audit log. Ask them if they haven't said."
                    ),
                },
            },
            "required": ["order_number", "reason"],
        },
    },
    {
        "name": "admin_cancel_order",
        "description": (
            "STEP 2 — actually cancels any customer's order, overriding the 24-hour window "
            "customers are held to. Only call after admin_preview_order_cancellation in this same "
            "conversation AND an explicit yes from the staff member in a separate message. Pass "
            "the SAME order_number and the SAME reason you previewed; different values are "
            "rejected. Does not refund anything — settlement is separate."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "order_number": {"type": "string"},
                "reason": {"type": "string", "description": "Must match the reason given to the preview, verbatim."},
            },
            "required": ["order_number", "reason"],
        },
    },
    {
        "name": "admin_preview_stock_adjustment",
        "description": (
            "STEP 1 of correcting stock on hand. Read-only: shows the product's current quantity "
            "for that size and what it would become. Use admin_inventory_status first if you don't "
            "already have the exact SKU."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "sku": {"type": "string", "description": "Product SKU, e.g. TPJ-RIN-1010."},
                "new_quantity": {
                    "type": "integer",
                    "description": "The absolute quantity to set, NOT a delta to add or subtract.",
                },
                "size": {
                    "type": "string",
                    "description": (
                        "Which size to adjust. Omit for products stocked without sizes; required "
                        "when a product has several, and the error will list the valid ones."
                    ),
                },
                "reason": {
                    "type": "string",
                    "description": "Why (e.g. 'stock count found 3 extra'). Written to the audit log.",
                },
            },
            "required": ["sku", "new_quantity", "reason"],
        },
    },
    {
        "name": "admin_adjust_stock",
        "description": (
            "STEP 2 — sets the on-hand quantity for one product and size. Only call after "
            "admin_preview_stock_adjustment in this conversation AND an explicit yes from the "
            "staff member. Pass the SAME sku, size, quantity and reason you previewed."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "sku": {"type": "string"},
                "new_quantity": {"type": "integer"},
                "size": {"type": "string"},
                "reason": {"type": "string", "description": "Must match the reason given to the preview, verbatim."},
            },
            "required": ["sku", "new_quantity", "reason"],
        },
    },
    {
        "name": "admin_preview_goodwill_coupon",
        "description": (
            "STEP 1 of giving a customer store credit as a goodwill gesture (a delayed delivery, a "
            "service complaint) — NOT tied to any order and NOT a refund. Read-only: confirms who "
            "the customer is and what the coupon would be worth. Creates nothing. Use "
            "admin_find_customer first to get their exact email."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "customer_email": {"type": "string", "description": "The customer's exact email address."},
                "amount_rupees": {
                    "type": "integer",
                    "description": (
                        "Whole rupees, NOT paise — pass 5000 for ₹5,000. There is a per-coupon cap; "
                        "an amount over it is refused outright rather than trimmed."
                    ),
                },
                "reason": {
                    "type": "string",
                    "description": "Why the goodwill credit is being given. Written to the audit log.",
                },
            },
            "required": ["customer_email", "amount_rupees", "reason"],
        },
    },
    {
        "name": "admin_issue_goodwill_coupon",
        "description": (
            "STEP 2 — actually creates the store-credit coupon and returns its code. Only call "
            "after admin_preview_goodwill_coupon in this conversation AND an explicit yes from the "
            "staff member. Pass the SAME email, amount and reason you previewed — a different "
            "amount is rejected, not silently issued."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "customer_email": {"type": "string"},
                "amount_rupees": {"type": "integer", "description": "Whole rupees. Must match the previewed amount."},
                "reason": {"type": "string", "description": "Must match the reason given to the preview, verbatim."},
            },
            "required": ["customer_email", "amount_rupees", "reason"],
        },
    },
]
