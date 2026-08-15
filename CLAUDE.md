# TP Jewellers Chatbot

Tool-calling AI agent for TP Jewellers (jewellery e-commerce): product
browsing/recommendations, order status, cancellation (with guardrails), and
store policy Q&A. Built under a 48-hour demo deadline starting 2026-08-15.

**Read `JOURNAL.md` before making non-trivial changes.** It has the actual
reasoning behind decisions (why local Ollama instead of Claude API, why a
hand-rolled loop instead of LangGraph, bugs already found and fixed) —
`docs/ARCHITECTURE.md` has the target design, the journal has what actually
happened. After every important change, append an entry to `JOURNAL.md`
(newest on top) — what changed, why, and what broke if anything did.

## Local setup

```
docker compose up -d                        # Postgres+pgvector on :5433
python -m venv .venv && .venv/Scripts/pip install -r requirements.txt
cp .env.example .env                        # fill in DATABASE_URL (default is fine)
.venv/Scripts/python scripts/seed_db.py --reset
.venv/Scripts/python scripts/add_demo_user.py   # adds Arun + 2 orders for live demo
# get a free API key at openrouter.ai, put it in .env as LLM_API_KEY
.venv/Scripts/uvicorn src.api.main:app --reload --port 8000

# separate terminal — frontend
cd web && npm install && npm run dev        # http://localhost:3000
```

Model backend is OpenRouter (hosting DeepSeek, OpenAI-compatible API) — no
local model/GPU needed, ~5-8s/turn. Any OpenAI-compatible provider works via
`LLM_BASE_URL`/`LLM_API_KEY`/`LLM_MODEL` — this orchestrator has run against
Anthropic, local Ollama, NVIDIA NIM, and OpenRouter without the tool-loop
logic ever changing, only `_call_model()`. See `JOURNAL.md` 2026-08-15
(night) entries for the full story, including a real network-latency bug
(NIM stalled for minutes on this network for reasons unrelated to NIM
itself) and the OpenAI tool-calling protocol gotchas (tool-call-id linking,
string-encoded arguments) if swapping backends again.

Run tests: `.venv/Scripts/python -m pytest tests/ -v` — hits the live DB
directly, no mocking. Re-run `seed_db.py --reset` after, since the
cancellation happy-path test actually cancels an order.

## Architecture, in one paragraph

Tool-calling agent, not classic RAG — order/cancellation queries are
transactional DB lookups, not semantic retrieval. `src/agent/orchestrator.py`
is a hand-rolled tool-use loop (not LangGraph) talking to a local Ollama
model. `src/tools/` holds the actual logic: `order_tools.py` (status,
cancellation-eligibility + confirmation-token issuance, cancel), `product_tools.py`
(catalog browse/recommend), `knowledge_tools.py` (Postgres full-text search
over policy docs). `src/api/main.py` is the thin FastAPI surface the web app
talks to (`POST /conversations`, `POST /chat`).

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

## Known gaps (see JOURNAL.md "what's next" / roadmap)

- `knowledge_docs.embedding` column exists but nothing populates it yet —
  full-text search only for now, which is fine at 6 docs but won't scale.
- No conversation memory beyond raw message replay (no summarization).
- No rate limiting / abuse protection on the API layer — fine for a demo,
  not for anything real.
