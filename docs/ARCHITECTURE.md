# TP Jewellers Support Chatbot — System Architecture

> This is the original design doc — still accurate on the core "tool-calling
> agent, not RAG" decision and the guardrail philosophy. For what's actually
> been built, what changed along the way (LLM backend swaps, currency bugs
> found live, etc.) and why, see `JOURNAL.md` — that's the as-built history.

## The core design decision: tool-calling agent, not RAG-first

A customer asking "where is my order #A1234" or "cancel my order" is not asking a
knowledge-retrieval question — they're asking for a live, deterministic lookup/action
against transactional data. Vector similarity search over embedded text would retrieve
*semantically similar text about orders in general*, not the actual current status of
that specific order. That's a category error, not a tuning problem.

So the architecture treats every user turn as a **tool-calling agent loop**: Claude
receives the conversation + a set of typed tools, decides which tool(s) to call, the
backend executes them against real data, and Claude composes the final answer from the
tool results. RAG is used only where it's actually the right tool — see below.

```
┌─────────────────────────────────────────────────────────────────────┐
│                            Client (web widget)                       │
└───────────────────────────────────┬───────────────────────────────────┘
                                     │ POST /chat  {session_id, message}
                                     ▼
┌─────────────────────────────────────────────────────────────────────┐
│                        API layer (FastAPI)                           │
│   - auth (customer session / verified identity)                      │
│   - loads conversation history for session_id                        │
└───────────────────────────────────┬───────────────────────────────────┘
                                     ▼
┌─────────────────────────────────────────────────────────────────────┐
│                    Agent orchestrator (agent/)                       │
│   - system prompt + conversation history + tool schemas → Claude     │
│   - tool_use loop: execute requested tool(s), feed results back,     │
│     repeat until Claude returns a final text response                │
│   - guardrails hook fires BEFORE any mutating tool executes          │
└──────┬───────────────────────┬───────────────────────┬───────────────┘
       ▼                       ▼                       ▼
┌─────────────┐      ┌──────────────────┐     ┌─────────────────────┐
│ Order tools  │      │ Knowledge tools   │     │ Account tools        │
│ (structured, │      │ (unstructured,    │     │ (identity-scoped)    │
│  DB reads +  │      │  hybrid search)   │     │                      │
│  guarded     │      │                   │     │                      │
│  writes)     │      │                   │     │                      │
└──────┬───────┘      └─────────┬─────────┘     └──────────┬───────────┘
       ▼                        ▼                           ▼
┌─────────────┐      ┌──────────────────┐     ┌─────────────────────┐
│  Postgres    │      │ Postgres FTS +    │     │  Postgres            │
│  orders,     │      │ pgvector          │     │  customers            │
│  order_items,│      │ (knowledge_docs)  │     │  (auth-scoped read)  │
│  customers,  │      │                   │     │                      │
│  products    │      │                   │     │                      │
└─────────────┘      └──────────────────┘     └─────────────────────┘
```

## Why not classic RAG, and where retrieval still belongs

| Query type | Example | Handled by |
|---|---|---|
| Transactional lookup | "Where is order #A1234?" | `get_order_status` tool → direct DB read |
| Transactional action | "Cancel my order" | `check_cancellation_eligibility` → confirm → `cancel_order` tool |
| Account-scoped data | "What have I ordered before?" | `list_customer_orders` tool, scoped to authenticated customer_id |
| Unstructured knowledge | "What's your return policy?" | `search_knowledge_base` — hybrid retrieval (see below) |
| Unstructured knowledge | "How do I clean gold jewellery?" | `search_knowledge_base` |
| Product knowledge | "Do you have this ring in size 7?" | `get_product_info` + `check_stock` tools (structured, not RAG) |

For the genuinely unstructured slice (policies, care guides, general FAQs), the
"modern, not old RAG" choice is:

1. **Hybrid retrieval, not pure cosine similarity.** Combine Postgres full-text search
   (`tsvector`/BM25-style ranking) with `pgvector` embedding similarity, then merge
   ranked results (reciprocal rank fusion). Keyword search alone misses paraphrases;
   embeddings alone miss exact terms (SKUs, policy section names) that customers often
   quote verbatim.
2. **No separate vector DB service.** The knowledge corpus for one jewellery retailer's
   support content is small (dozens to low hundreds of documents) — `pgvector` inside
   the same Postgres instance avoids running a second stateful service for a workload
   that doesn't need one. Revisit only if the corpus grows into the tens of thousands
   of documents.
3. **The retrieval step is itself a tool call**, not a pre-processing step bolted in
   front of every message. Claude only calls `search_knowledge_base` when the query
   actually needs it — an order-status question never touches the knowledge base at
   all, which is both faster and avoids irrelevant context polluting the answer.
4. **Reranking is a later optimization, not a day-one requirement.** Ship hybrid
   search first; add a cross-encoder reranking pass only if evaluation shows the
   fusion ranking is actually the bottleneck.

## Guardrails (the part that matters most for a commerce chatbot)

Order cancellation is a destructive, irreversible-ish action against real money and
real inventory. The agent loop is not trusted to gate this alone:

1. **Identity verification precedes any order-specific tool call.** The API layer
   authenticates the session before the agent ever runs; tools that touch order/account
   data take `customer_id` from the verified session, never from the LLM's parsed
   understanding of what the user typed ("I'm customer 123" is not proof of identity).
2. **Business-rule check before mutation.** `check_cancellation_eligibility` is a
   separate, non-optional tool call the agent must invoke before `cancel_order` is even
   offered as callable — eligibility depends on order status (not yet shipped),
   cancellation window, and payment state. The `cancel_order` tool itself re-validates
   these server-side; it does not trust that the agent checked.
3. **Explicit confirmation turn for any mutating action.** The agent is instructed
   (system prompt) to summarize the action and ask the customer to confirm before
   calling a mutating tool a second time; the backend additionally requires a
   short-lived confirmation token from step 1 so a single ambiguous message can't
   trigger a cancellation.
4. **Full audit log.** Every tool call (name, arguments, result, timestamp,
   session_id) is persisted — this is what makes "the bot cancelled my order and I
   don't know why" debuggable after the fact.

## Data model (initial cut — see `src/db/schema.sql`)

- `customers` — id, name, email, phone, created_at
- `products` — id, sku, name, category, description, price, metal, stone, sizes_available (jsonb)
- `orders` — id, customer_id, status (enum: PLACED, CONFIRMED, SHIPPED, DELIVERED, CANCELLED, RETURNED), placed_at, total_amount
- `order_items` — id, order_id, product_id, quantity, unit_price, size
- `order_status_history` — id, order_id, from_status, to_status, changed_at, reason
- `knowledge_docs` — id, title, content, content_tsv (generated tsvector column), embedding (vector), category, updated_at
- `conversations` — id, customer_id (nullable pre-auth), started_at
- `messages` — id, conversation_id, role, content, created_at
- `tool_call_log` — id, conversation_id, tool_name, arguments (jsonb), result (jsonb), created_at
- `confirmation_tokens` — id, conversation_id, action, target_id, token, expires_at, used_at
- `coupon_policy` — team-editable incentive settings (cancellation/return bonus %, expiry days);
  no code deploy needed to retune the coupon-vs-cash-refund incentive
- `coupons` — id, code, customer_id, source_type (cancellation|return), source_order_id
  (UNIQUE — one coupon per order, DB-enforced), amount_cents, bonus_percent_applied (snapshot
  at issuance), total_cents, remaining_cents (atomically decremented — supports partial/
  multi-order redemption), status, issued_at, expires_at
- `orders` also has `coupon_id` + `discount_cents` — set when an order was placed using
  coupon balance (see `src/tools/coupon_tools.py`, `src/tools/purchase_tools.py`)

## Stack

- **LLM**: Claude API (`anthropic` Python SDK), native tool use
- **Backend**: FastAPI (async; supports streaming the final response back to the widget)
- **DB**: PostgreSQL + `pgvector` extension (SQLite acceptable for local dev, but
  `pgvector`/`tsvector` hybrid search needs real Postgres — dev should target Postgres
  from the start via Docker to avoid a dev/prod split on the one component that matters most)
- **Embeddings**: Voyage AI (Anthropic's recommended embedding provider) for
  `knowledge_docs.embedding`; only re-embedded when a doc changes, not per-query beyond
  the query embedding itself
- **Tests**: pytest, with the tool layer unit-tested against a seeded test DB
  independent of the LLM (deterministic — no need to call Claude to test that
  `cancel_order` correctly rejects a shipped order)

## Folder layout

```
tpjewellers-chatbot/
├── docs/
│   └── ARCHITECTURE.md          (this file)
├── src/
│   ├── api/            FastAPI app, /chat endpoint, session auth
│   ├── agent/           system prompt, Claude client wrapper, tool_use loop
│   ├── tools/            order_tools.py, knowledge_tools.py, account_tools.py
│   ├── db/               schema.sql, models, connection pool
│   ├── knowledge/         seed knowledge documents (markdown source, indexed into DB)
│   └── guardrails/         eligibility rules, confirmation-token logic
├── tests/
├── scripts/              seed_db.py, index_knowledge.py
├── requirements.txt
└── .env.example
```

## What's next (not yet built)

This document is step 1: architecture only. Nothing has been implemented yet.
Proposed next steps, in order:
1. `src/db/schema.sql` + seed script with realistic fake TP Jewellers data
2. Tool layer (`order_tools.py` etc.) — pure functions against the DB, unit-testable
   without any LLM involved
3. Knowledge base: a handful of real-sounding policy/FAQ docs + the hybrid search tool
4. Agent orchestrator wiring Claude's tool use to the tool layer
5. Minimal FastAPI `/chat` endpoint + a bare-bones test client (curl/simple HTML page)
