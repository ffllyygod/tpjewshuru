# TP Jewellers Chatbot

Tool-calling AI agent for TP Jewellers (jewellery e-commerce): product
browsing/recommendations, order status, cancellation (with guardrails),
coupon-instead-of-refund settlement, Gold SIP schemes, and store policy Q&A.
Built under a 48-hour demo deadline starting 2026-08-15.

**Live deployment**: frontend `https://tpjewellers-chatbot.vercel.app`,
backend `https://api-production-dd5a.up.railway.app`, Postgres on Railway
(`pgvector/pgvector:pg16`, same image as local). **Never run
`scripts/seed_db.py --reset` against the Railway `DATABASE_URL`** unless you
mean to wipe production data — it's real, persisted, and someone may be
mid-testing it.

**Read `JOURNAL.md` before making non-trivial changes.** It has the actual
reasoning behind decisions (why local Ollama instead of Claude API, why a
hand-rolled loop instead of LangGraph, bugs already found and fixed) —
`docs/ARCHITECTURE.md` has the target design, the journal has what actually
happened. After every important change, append an entry to `JOURNAL.md`
(newest on top) — what changed, why, and what broke if anything did.

## Local setup

```
docker compose up -d                        # Postgres+pgvector (:5433) + SuperTokens core (:3567)
python -m venv .venv && .venv/Scripts/pip install -r requirements.txt
cp .env.example .env                        # fill in DATABASE_URL (default is fine)
.venv/Scripts/python scripts/seed_db.py --reset
.venv/Scripts/python scripts/add_demo_user.py   # adds Arun + 2 orders for live demo
# get a free API key at openrouter.ai, put it in .env as LLM_API_KEY
# fill in SMTP_* in .env (Gmail + an App Password — see .env.example) for OTP login
.venv/Scripts/uvicorn src.api.main:app --reload --port 8000

# separate terminal — frontend
cd web && npm install && npm run dev        # http://localhost:3000
```

**Auth**: self-hosted SuperTokens (Passwordless/OTP-over-email), mounted at
`/auth` — `POST /auth/signinup/code` {email} sends a code (only to emails
already in `customers`, silently, to avoid enumeration), `POST
/auth/signinup/code/consume` verifies it and returns session tokens as
response headers (`st-access-token`/`st-refresh-token` — header-based
sessions, not cookies, since frontend/backend are on different domains).
Send the access token back as `Authorization: Bearer <token>` on
`/conversations` and `/chat`. `POST /conversations` no longer accepts a
client-supplied `email` — identity comes only from a verified session, or
the conversation stays anonymous. See `JOURNAL.md`'s "real auth" entry for
the full story, including a debugging saga that turned out not to be a bug.

Model backend is OpenRouter hosting `openai/gpt-4o-mini` (OpenAI-compatible
API) — no local model/GPU needed, ~5-8s/turn. Any OpenAI-compatible provider
works via `LLM_BASE_URL`/`LLM_API_KEY`/`LLM_MODEL` — this orchestrator has
run against Anthropic, local Ollama, NVIDIA NIM, and two different
OpenRouter-hosted models without the tool-loop logic ever changing, only
`_call_model()`. **Model choice matters more than you'd think for a
money-adjacent agent**: DeepSeek (tried first, cheaper) confabulated a
payment success without ever calling the tool on some repeated requests —
gpt-4o-mini reliably calls a tool for every state-changing request in the
same tests, at the cost of needing a firmer "wait for explicit confirmation"
instruction (it was initially too eager). See `JOURNAL.md`'s "gold-sip-schemes"
entry for the full comparison and two other real bugs found via live testing
(a plan-name identifier-ambiguity bug, and a serious one — the model can
select a wrong-but-valid product SKU while still describing the right
product in prose; mitigated, not proven eliminated). Also see the earlier
2026-08-15 (night) entries for the NIM network-latency story and OpenAI
tool-calling protocol gotchas (tool-call-id linking, string-encoded
arguments) if swapping backends again.

Run tests: `.venv/Scripts/python -m pytest tests/ -v` — hits the live DB
directly, no mocking. Re-run `seed_db.py --reset` after, since the
cancellation happy-path test actually cancels an order.

## Architecture, in one paragraph

Tool-calling agent, not classic RAG — order/cancellation queries are
transactional DB lookups, not semantic retrieval. `src/agent/orchestrator.py`
is a hand-rolled tool-use loop (not LangGraph) talking to a hosted OpenAI-
compatible model (currently gpt-4o-mini via OpenRouter). `src/tools/` holds
the actual logic: `order_tools.py` (status, cancellation-eligibility +
confirmation-token issuance, cancel), `coupon_tools.py` (settlement offer,
issuance, redemption — reused by Gold SIP's early-exit payout),
`gold_sip_tools.py` (plan tiers, subscription lifecycle), `product_tools.py`
(catalog browse/recommend), `purchase_tools.py` (place orders, optionally
applying a coupon/SIP discount), `knowledge_tools.py` (Postgres full-text
search over policy docs), `market_tools.py` (live gold/silver rates),
`formatting.py` (INR currency formatting — see Guardrails below).
`src/api/main.py` is the thin FastAPI surface the web app talks to
(`POST /conversations`, `POST /chat`); `src/api/auth.py` wires in self-hosted
SuperTokens for real OTP-over-email login (see Local setup below).

## Guardrails — the load-bearing design decisions

- **`customer_id` is never a tool parameter the LLM controls.** The
  orchestrator injects it server-side from the authenticated session before
  calling any order tool. The agent cannot look up someone else's orders —
  not because it's told not to, but because the capability doesn't exist.
- **Cancellation is two-step and token-gated.**
  `check_cancellation_eligibility` issues a single-use token scoped to
  `(conversation_id, action, order_id)`. `cancel_order` re-validates
  ownership/status/window from scratch — the token proves eligibility was
  checked, it doesn't bypass re-verification. A token from one conversation
  cannot be replayed in another (see `JOURNAL.md`, 2026-08-15).
- **Every tool call is audit-logged** to `tool_call_log` (name, args, result)
  by the orchestrator, not by individual tools.
- **Currency arithmetic never happens in the model.** Every tool response
  with a `*_cents` field also returns a matching `*_display` field
  (`src/tools/formatting.py`), already correctly converted to ₹ with Indian
  digit grouping. This exists because the model got this arithmetic wrong in
  production, more than once, even after being explicitly told to be careful
  — see `JOURNAL.md`. Same principle as every guardrail above: don't ask the
  LLM to reliably do something a deterministic function can just do for it.

## Known gaps (see JOURNAL.md "what's next" / roadmap)

- `knowledge_docs.embedding` column exists but nothing populates it yet —
  full-text search only for now, which is fine at 6 docs but won't scale.
- No conversation memory beyond raw message replay (no summarization).
- No rate limiting / abuse protection on the API layer — fine for a demo,
  not for anything real.
