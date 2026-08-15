# TP Jewellers Support Chatbot

A tool-calling agent (not classic RAG) for an Indian jewellery e-commerce store —
product browsing/recommendations, order status, guarded cancellation with a
coupon-instead-of-refund settlement flow, Gold SIP (systematic investment plan)
schemes, and store policy Q&A. Runs on OpenRouter (`openai/gpt-4o-mini`) — no
GPU/local model needed.

**Live deployment:**
- Frontend: https://tpjewellers-chatbot.vercel.app
- Backend API: https://api-production-dd5a.up.railway.app
- Database: Railway Postgres (`pgvector/pgvector:pg16`)

See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for the system design and rationale,
and [`JOURNAL.md`](JOURNAL.md) for the actual build log — decisions, bugs found, why.
Both are worth reading before touching this codebase; a lot of what looks like an
obvious design choice here is the second attempt after the first one broke live.

## Quickstart (local)

```
docker compose up -d
python -m venv .venv && .venv/Scripts/pip install -r requirements.txt
cp .env.example .env
.venv/Scripts/python scripts/seed_db.py --reset
.venv/Scripts/python scripts/add_demo_user.py
# get a free API key at openrouter.ai, put it in .env as LLM_API_KEY
.venv/Scripts/uvicorn src.api.main:app --port 8000
```

Then chat from a second terminal:

```
.venv/Scripts/python scripts/chat_cli.py arun@shurutech.com
```

There's also a Next.js frontend in `web/` — see its own quickstart in `CLAUDE.md`.

Run tests: `.venv/Scripts/python -m pytest tests/ -v` (43 passing — tool logic,
orchestrator mechanics, currency formatting regressions). Re-run
`seed_db.py --reset` + `add_demo_user.py` after, since several tests mutate
live data (cancel an order, redeem a coupon, etc.).

**Never run `seed_db.py --reset` against the Railway deployment** unless you
mean to wipe production data — point `DATABASE_URL` at Railway only when you
specifically intend to.

## What's actually built

- Order status/cancellation with a two-step confirm + short-lived token guard
- Cash-refund-vs-instant-coupon settlement on any cancelled/returned order
  (team-editable bonus %, DB-enforced one-coupon-per-order idempotency)
- Gold SIP schemes: start a plan, pay installments, auto-mature, redeem
  against a purchase, or exit early (forfeits bonus + a penalty %, paid out
  as a coupon — never cash)
- Product browsing/recommendation + order placement, with a coupon or SIP
  balance applicable in the same call
- Live gold/silver spot rates converted to INR/gram
- Full audit log of every tool call (`tool_call_log`)
- Every money-bearing tool response includes a pre-formatted `₹` string
  alongside the raw paise value — the model never does that arithmetic itself
  (it got this wrong in production more than once before this existed; see
  `JOURNAL.md`)

Status: fully working end-to-end, deployed, live-verified against real
Postgres state at every step described above — not just the model's claimed
reply text.
