# DP Jewellers Chatbot — System Architecture

> **As-built, 2026-08-16.** This started as a pre-implementation design doc; it
> has been rewritten to describe what actually exists. The core "tool-calling
> agent, not RAG-first" argument survived contact with reality unchanged — most
> of what follows is the same reasoning, now with the built system underneath it.
>
> For *how* it got here — the LLM backend swaps, the currency bugs found in
> production, the hallucinations live testing caught — see `JOURNAL.md`. That's
> the narrative; this is the shape.

## The core design decision: tool-calling agent, not RAG-first

A customer asking "where is my order DPJ-123456" or "cancel my order" is not
asking a knowledge-retrieval question — they're asking for a live, deterministic
lookup or action against transactional data. Vector similarity search over
embedded text would retrieve *semantically similar text about orders in
general*, not the current status of that specific order. That's a category
error, not a tuning problem.

So every user turn is a **tool-calling agent loop**: the model receives the
conversation plus a set of typed tools, decides which to call, the backend
executes them against real data, and the model composes its answer from the
results. Retrieval is used only where it is genuinely the right tool.

The loop is hand-rolled (`src/agent/orchestrator.py`, ~400 lines) rather than
LangGraph. At this size the framework would have added indirection over a `for`
loop, and the guardrails below live *inside* that loop — the place a framework
would have owned.

## Three audiences, one chat window

| Persona | Identity | Sees |
|---|---|---|
| Anonymous | no session | products, policies, metal rates |
| Customer | verified OTP session | the above + **their own** orders, coupons, Gold SIPs |
| Staff | verified session with `role='admin'` | the above minus personal tools, **plus** business-wide analytics and operational writes |

```
                        Client (Next.js, web/)
                                  │
       POST /conversations {mode} │ POST /chat  — Authorization: Bearer <st-access-token>
                                  ▼
┌──────────────────────────────────────────────────────────────────────┐
│  API layer (FastAPI, src/api/)                                        │
│   - SuperTokens passwordless session → Principal(customer_id, role)   │
│   - role re-read from DB EVERY request; mode re-verified every turn   │
└──────────────────────────────────┬───────────────────────────────────┘
                                   ▼
┌──────────────────────────────────────────────────────────────────────┐
│  Orchestrator (src/agent/orchestrator.py)                             │
│   - persona-filtered tool list + persona prompt → model               │
│   - tool loop: execute, feed results back, repeat until final text    │
│   - INJECTS identity server-side; GATES staff tools; LOGS every call  │
└───┬───────────────┬────────────────┬────────────────┬────────────────┘
    ▼               ▼                ▼                ▼
┌────────┐  ┌──────────────┐  ┌────────────┐  ┌──────────────────┐
│ order  │  │ coupon /     │  │ product /  │  │ admin_tools.py   │
│ tools  │  │ gold_sip /   │  │ knowledge /│  │ (staff only,     │
│        │  │ purchase     │  │ market     │  │  cross-customer) │
└───┬────┘  └──────┬───────┘  └─────┬──────┘  └────────┬─────────┘
    └──────────────┴────────────────┴──────────────────┘
                                   ▼
                    Postgres 16 + pgvector (single instance)
```

## Why not classic RAG, and where retrieval still belongs

| Query type | Example | Handled by |
|---|---|---|
| Transactional lookup | "Where is DPJ-123456?" | `get_order_status` — direct DB read |
| Transactional action | "Cancel my order" | `check_cancellation_eligibility` → confirm → `cancel_order` |
| Account-scoped data | "What have I ordered?" | `list_customer_orders`, scoped to the session's customer_id |
| Business-wide analytics | "How were sales last month?" | `admin_sales_summary` — staff only |
| Unstructured knowledge | "What's your return policy?" | `search_knowledge` |
| Product browsing | "Rings under ₹30,000?" | `search_products` — structured filters, not RAG |
| Live market data | "Gold rate today?" | `get_metal_rates` — external API |

For the genuinely unstructured slice (policies, care guides, FAQs):

1. **Retrieval is a tool call, not a preprocessing step.** The model calls
   `search_knowledge` only when the query needs it; an order-status question
   never touches the knowledge base, which is faster and keeps irrelevant
   context out of the answer.
2. **No separate vector service.** One retailer's support corpus is small
   (currently 6 documents). `pgvector` in the same Postgres avoids a second
   stateful service for a workload that doesn't need one.
3. **Hybrid search was the plan; full-text is what's built.**
   `knowledge_docs.embedding` exists but nothing populates it yet, so retrieval
   is Postgres FTS only. Honest at 6 documents, inadequate by a few hundred.
   See "Known gaps".

## Guardrails — the load-bearing design decisions

These are the parts worth reading. Each exists because the alternative failed,
usually in production.

**1. `customer_id` is never a tool parameter.** It is absent from every tool
schema and injected by the orchestrator from the verified session. The agent
cannot read another customer's orders — not because it's instructed not to, but
because the capability doesn't exist. This is the invariant everything else is
built around.

**2. Staff tools are gated in code, not in the prompt.** `_ADMIN_ONLY` is
checked in `_execute_tool` *before* the authentication branch, so an anonymous
caller invoking a staff tool gets `forbidden` rather than `not_authenticated` —
the latter would leak which tools exist. Tool lists are also filtered per
persona, but that is an *accuracy* measure (selection degrades with list
length); the gate is what makes it safe.

**3. Staff tools take `actor_customer_id`, never `customer_id`.** WHO is asking
versus WHOSE data. They live in their own module, and no existing tool ever
gained a nullable `customer_id` meaning "everyone" — that change would turn
every `None`-propagation bug in the codebase into a cross-customer leak. A test
asserts the two sets never intersect.

**4. Role vs mode.** `customers.role` is what you *may* do; `conversations.mode`
is what you're doing *now*. Without the second, an admin saying "cancel my
order" leaves the model choosing between `cancel_order` and `admin_cancel_order`
on vibes. Mode is granted at conversation start and re-verified every turn, so
revoking admin takes effect on the next message.

**5. Mutations are two-step and token-gated.** A preview tool mints a
single-use, short-lived row in `confirmation_tokens` scoped to
`(conversation_id, action, target_id)`; the write re-validates every
precondition from scratch and burns the token. The token proves the check ran —
it never substitutes for re-checking. A token from one conversation cannot be
replayed in another.

   `confirmation_tokens.params` binds the token to *what was previewed*.
   Scoping to the target is enough for cancellation, where the target fully
   describes the action, but "give this customer ₹500" and "give this customer
   ₹5,00,000" share a target id — so applies re-derive their effect from the
   stored params and reject drifted arguments.

   **Honest limitation:** the token proves the preview ran in this conversation,
   *not* that a human said yes in between. That step is prompt-enforced only.
   What the token buys is that a single confused turn cannot both discover a
   target and mutate it.

**6. Currency arithmetic never happens in the model.** Every `*_cents` field is
accompanied by a `*_display` string already formatted with Indian digit
grouping (`src/tools/formatting.py`). This exists because the model got the
arithmetic wrong in production more than once, after being explicitly told to be
careful. Per-row `*_cents` values are *removed* from staff list results
entirely: given a summable column the model sums it and reports the total as
fact. Prompt rules reduced that; deleting the column stopped it.

**7. Resolved orders must say why.** A `CANCELLED` order must carry a
cancellation reason and a `RETURNED` one a return reason — enforced by a DB
CHECK, not application code, so no future path can write a reasonless
resolution. Codes come from a team-editable table with two axes: `kind`
(cancellation | return) and `applies_to` (customer | staff | both). A customer
is never offered "suspected fraudulent order", and a cancellation code passed to
a return is rejected rather than silently recorded.

**8. Everything is audited, twice, for different questions.**
`tool_call_log` records every tool call with its arguments and result — but only
the *model-supplied* arguments, so the injected identity never appears in it.
`admin_action_log` answers the other question: which staff member did what to
whom, with before/after state and a mandatory reason.

## Data model (see `src/db/schema.sql` for the authoritative version)

**Identity and conversation**
- `customers` — id, name, email, phone, **role** (`customer|admin`, the sole
  source of truth for staff access), created_at. Admins live here rather than a
  separate table so they log in through the same OTP flow.
- `conversations` — id, customer_id (nullable pre-auth), **mode**
  (`customer|admin`), started_at
- `messages`, `tool_call_log`, `admin_action_log`
- `confirmation_tokens` — conversation_id, action, target_id, **params** (jsonb),
  token, expires_at, used_at

**Catalogue and orders**
- `products` — sku, name, category, price_cents, metal, stone, sizes_available,
  stock_by_size (jsonb, `{"7": 3}` or `{"_default": 12}`), **cost_price_cents**
  (nullable — margin is unknowable without it, and "cost data incomplete" beats
  a fabricated 100% margin), **low_stock_threshold**, **active** (soft delete —
  `order_items` FKs this, so DELETE is not an option), **returnable**
  (final-sale items, per the return policy)
- `orders` — order_number, customer_id, status, placed_at/shipped_at/
  delivered_at/**returned_at**, total_amount_cents, payment_status (CHECK-
  constrained uppercase — both `PAID` and `paid` once existed live, silently
  splitting revenue queries), coupon_id / gold_sip_subscription_id +
  discount_cents (at most one discount source, DB-enforced),
  **cancellation_reason_code/_note**, **return_reason_code/_note**
- `order_items`, `order_status_history`
- `resolution_reasons` — code, kind, label, applies_to, requires_note, active,
  sort_order

**Settlement and savings**
- `coupon_policy` — team-editable bonus percentages and expiry
- `coupons` — source_type (`cancellation|return|gold_sip_cancellation|goodwill`),
  source_order_id / source_subscription_id. A CHECK ties the sourceless case to
  `goodwill` specifically: relaxing it to "at most one source" would also have
  permitted a *cancellation* coupon with no order behind it, detaching the audit
  trail. Balance is decremented atomically, supporting partial redemption.
- `gold_sip_plans`, `gold_sip_subscriptions`, `gold_sip_installments`
- `knowledge_docs` — content, content_tsv (generated), embedding (unpopulated)

## Stack

- **LLM**: any OpenAI-compatible provider via `LLM_BASE_URL`/`LLM_API_KEY`/
  `LLM_MODEL`; currently `openai/gpt-4o-mini` on OpenRouter. This orchestrator
  has run against Anthropic, local Ollama, NVIDIA NIM and two OpenRouter models
  without the loop logic changing — only `_call_model()`. `LLM_API_KEY` accepts
  a comma-separated list and fails over on auth/credit/rate-limit/outage errors.
  **Model choice matters more than expected for a money-adjacent agent**: see
  `JOURNAL.md` on DeepSeek confabulating a payment success.
- **Auth**: self-hosted SuperTokens (passwordless OTP over email), header-based
  sessions since frontend and backend are on different domains. Codes are only
  sent to emails already in `customers`, silently, to avoid enumeration.
- **Backend**: FastAPI — `POST /conversations`, `POST /chat`, plus SuperTokens
  at `/auth`
- **Frontend**: Next.js 16 + Tailwind (`web/`), role-aware
- **DB**: PostgreSQL 16 + pgvector, same image locally and in production
- **Deploy**: Railway (API + Postgres), Vercel (frontend)
- **Tests**: pytest against a live seeded DB, no mocking — 205 tests.
  `scripts/hallucination_audit.py` is separate on purpose: it calls the real
  model, costs money, and is non-deterministic, so it gates releases rather than
  running in CI.

## Folder layout

```
dpjewellers-chatbot/
├── docs/ARCHITECTURE.md      (this file)
├── JOURNAL.md                as-built history and reasoning — read this
├── src/
│   ├── api/         main.py (FastAPI surface), auth.py (SuperTokens, Principal)
│   ├── agent/       orchestrator.py (tool loop), tool_schemas.py, system_prompt.py
│   ├── tools/       order, coupon, gold_sip, product, purchase, knowledge,
│   │                market, admin, formatting
│   └── db/          schema.sql, connection.py
├── web/             Next.js app (LoginScreen, ModeChooser, ChatWindow)
├── tests/           205 tests; conftest.py asserts the DB is in a pre-test state
├── scripts/
│   ├── seed_db.py                    full seed (--reset drops the schema)
│   ├── add_demo_user.py              demo customer + orders in every state
│   ├── add_admin_users.py            staff accounts, additive
│   ├── migrate_to_admin_schema.py    non-destructive migration for a live DB
│   ├── seed_analytics_history.py     12 months of synthetic history, additive
│   ├── seed_resolution_reasons_only.py
│   ├── backfill_history_reasons.py
│   └── hallucination_audit.py        live adversarial audit
└── requirements.txt
```

`src/guardrails/` and `src/knowledge/` exist as empty placeholders from the
original design. Guardrail logic ended up in the orchestrator and the tool
modules, which is where it belongs — the checks are inseparable from the
operations they guard.

## Deployment notes

There is **no migration framework**: `schema.sql` is applied wholesale by
`seed_db.py`, and `--reset` drops the schema first. Never point that at the
deployed database. `migrate_to_admin_schema.py` is the non-destructive path —
idempotent, strictly additive, and validated by replaying against a scratch copy
of the previous schema before it touches anything live.

Two ordering traps it encodes, both learned the hard way:
- Normalise data *before* adding a CHECK that constrains it (`payment_status`).
- When a new constraint would break the currently-deployed code, add the columns
  with `--defer-constraints`, deploy, then re-run to add the constraints.

## Known gaps

- `knowledge_docs.embedding` is unpopulated — full-text search only. Fine at 6
  documents, inadequate at a few hundred.
- No conversation summarisation; history is raw message replay.
- No rate limiting on the API layer.
- No stock-movement ledger. Stock is a current-value jsonb column; returns and
  cancellations restore it correctly, but there is no history of movements.
- The human-confirmation step before a mutation is prompt-enforced, not
  structurally enforced (see guardrail 5).
- A staff member can select a wrong-but-valid SKU while describing the right
  product in prose — mitigated by requiring SKUs be copied from a fresh tool
  result, not proven eliminated.
