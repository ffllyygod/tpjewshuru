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
        "description": "Apply a coupon's balance toward an order's total. Confirm the resulting total with the customer before finalizing anything else.",
        "input_schema": {
            "type": "object",
            "properties": {
                "code": {"type": "string", "description": "The coupon code, e.g. TPJ-CPN-XXXXXXXXXXXX"},
                "order_number": {"type": "string"},
            },
            "required": ["code", "order_number"],
        },
    },
    {
        "name": "place_order",
        "description": (
            "Place a new order for a product. Confirm the product, size (if applicable), and quantity back "
            "to the customer before calling this. Optionally applies a coupon code at the same time."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "sku": {"type": "string"},
                "quantity": {"type": "integer", "default": 1},
                "size": {"type": "string", "description": "Required for sized items (e.g. rings)."},
                "coupon_code": {"type": "string", "description": "Optional — apply an existing coupon to this order."},
            },
            "required": ["sku"],
        },
    },
    {
        "name": "search_products",
        "description": "Browse/recommend products from the catalog by category, metal, stone, and/or price range. Use this when the customer wants suggestions or is browsing (e.g. 'show me rings under $2000', 'gold necklaces with diamonds').",
        "input_schema": {
            "type": "object",
            "properties": {
                "category": {"type": "string", "enum": ["ring", "necklace", "earring", "bracelet", "bangle", "pendant"]},
                "metal": {"type": "string", "enum": ["gold", "silver", "platinum", "rose_gold"]},
                "stone": {"type": "string", "enum": ["diamond", "ruby", "emerald", "sapphire", "none"]},
                "min_price": {"type": "number", "description": "Minimum price in dollars."},
                "max_price": {"type": "number", "description": "Maximum price in dollars."},
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
