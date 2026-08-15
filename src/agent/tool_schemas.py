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
