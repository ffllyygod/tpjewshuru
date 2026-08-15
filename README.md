# TP Jewellers Chatbot

A tool-calling agent (not classic RAG) for an Indian jewellery e-commerce store,
serving **three audiences from one chat window**: anonymous browsers, logged-in
customers, and staff running the business. Product discovery, orders,
cancellations and returns with recorded reasons, coupon-instead-of-refund
settlement, Gold SIP schemes, policy Q&A — plus a staff console for sales and
inventory analytics and token-gated operational writes.

Runs on any OpenAI-compatible provider (currently `openai/gpt-4o-mini` via
OpenRouter) — no GPU or local model needed.

**Live deployment:**
- Frontend: https://tpjewellers-chatbot.vercel.app
- Backend API: https://api-production-dd5a.up.railway.app
- Database: Railway Postgres (`pgvector/pgvector:pg16`, same image as local)

See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for the system design and
[`JOURNAL.md`](JOURNAL.md) for the build log — decisions, bugs found live, and
why. Both are worth reading before touching this codebase: a lot of what looks
like an obvious design choice here is the second attempt after the first broke
in production.

## Quickstart (local)

```
docker compose up -d                  # Postgres+pgvector (:5433), SuperTokens core (:3567)
python -m venv .venv && .venv/Scripts/pip install -r requirements.txt
cp .env.example .env                  # fill in LLM_API_KEY from openrouter.ai
.venv/Scripts/python scripts/seed_db.py --reset
.venv/Scripts/python scripts/add_demo_user.py     # demo customer + orders in every state
.venv/Scripts/python scripts/add_admin_users.py   # staff accounts
.venv/Scripts/uvicorn src.api.main:app --reload --port 8000
```

Chat from a second terminal — this talks to the orchestrator directly, so you
don't need an OTP inbox to try a logged-in persona:

```
.venv/Scripts/python scripts/chat_cli.py                                # anonymous
.venv/Scripts/python scripts/chat_cli.py arun@shurutech.com             # as a customer
.venv/Scripts/python scripts/chat_cli.py admin@tpjewellers.com --admin  # staff console
```

The CLI bypasses HTTP auth by design (it's a local dev harness) but **not** any
tool guardrail — identity is still injected server-side and staff tools are
still gated. For a real login, use the frontend:

```
cd web && npm install && npm run dev     # http://localhost:3000
```

Login is passwordless OTP over email, so `SMTP_*` in `.env` needs to be real for
that path — see `.env.example`. Codes are only sent to addresses already in
`customers`, silently, to avoid account enumeration.

Run tests: `.venv/Scripts/python -m pytest tests/ -v` — **205 tests**, hitting a
live database with no mocking. Re-run `seed_db.py --reset` + `add_demo_user.py`
afterwards: several tests mutate real state (they genuinely cancel an order,
redeem a coupon, process a return). `tests/conftest.py` will tell you if you
forget.

Before a demo or after touching prompts, tools, or the model:

```
PYTHONIOENCODING=utf-8 .venv/Scripts/python scripts/hallucination_audit.py
```

31 adversarial scenarios asserting the agent never states a figure, order number
or SKU that no tool gave it, never reaches staff data from a customer session,
and never completes a write on a first request. It calls the real model, so it
costs money and is kept out of `pytest` on purpose.

> **Never run `seed_db.py --reset` against the deployed database.** It drops the
> schema. `scripts/migrate_to_admin_schema.py` is the non-destructive path for a
> live database — idempotent, strictly additive, and rehearsed against a scratch
> copy of the previous schema before it touches anything real.

## What it does

**For customers**
- Product browsing and recommendations; place an order, optionally applying a
  coupon or matured Gold SIP balance in the same call
- Order status, and cancellation behind a two-step confirmation + short-lived
  token — with a **recorded reason**, chosen from a store-configurable list
- **Returns** on delivered orders: 30-day window, final-sale items refused per
  the store's own policy, reason recorded, stock restored
- Cash-refund-vs-instant-coupon settlement on any cancelled or returned order
  (team-editable bonus %, DB-enforced one-coupon-per-order idempotency)
- Gold SIP schemes: start a plan, pay installments, mature, redeem against a
  purchase, or exit early — forfeiting the bonus plus a penalty, paid out as a
  coupon, never cash
- Live gold/silver spot rates converted to INR per gram

**For staff** (same chat window; role decided server-side, never by the client)
- Sales summaries and breakdowns by month, category, metal, product, customer
  or status, with period-over-period comparison
- Inventory: low stock, out of stock, stock value
- Order and customer lookup across the whole business
- "Why are people cancelling / returning" — counts, shares and lost value per
  reason
- Aggregate chatbot usage stats — deliberately **no** access to conversation
  content
- Operational writes, each a preview/apply pair: cancel or return any order,
  adjust stock, issue a goodwill coupon. Every write re-validates from scratch,
  burns a single-use confirmation, and lands in `admin_action_log` with
  before/after state and a mandatory reason.

## The parts worth reviewing

Each of these exists because the alternative failed, usually in production.
`docs/ARCHITECTURE.md` has the full list; the short version:

- **`customer_id` is never a tool parameter.** It's injected server-side from
  the verified session. The agent can't read someone else's orders because the
  capability doesn't exist — not because it was told not to.
- **Staff access is gated in code, before the auth branch**, so an anonymous
  caller invoking a staff tool gets `forbidden` rather than a message that leaks
  which tools exist.
- **Mutations are token-gated**, and the token carries the *previewed
  parameters* — otherwise a confirmation for "₹500 to this customer" would
  equally authorise "₹5,00,000 to this customer".
- **The model never does currency arithmetic.** Every `*_cents` field ships with
  a pre-formatted `₹` string. Per-row amounts are removed from staff lists
  entirely, because given a summable column the model sums it and reports the
  total as fact. Prompt rules reduced that; deleting the column stopped it.
- **A resolved order must say why** — enforced by a database CHECK, not
  application code.
- **Two audit logs, answering different questions.** `tool_call_log` records
  every call with its model-supplied arguments; `admin_action_log` records which
  staff member did what to whom.

Status: fully working end to end and deployed. Every flow above was verified
against real Postgres state, not just the model's claimed reply text.
