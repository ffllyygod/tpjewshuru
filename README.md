# TP Jewellers Support Chatbot

A tool-calling agent (not classic RAG) that answers customer queries for a jewellery
e-commerce store — product browsing/recommendations, order status, cancellation
(with guardrails), and store policy Q&A. Runs on OpenRouter (hosted DeepSeek) —
free-tier API key, no GPU/local model needed, ~5-8s per turn.

See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for the system design and rationale,
and [`JOURNAL.md`](JOURNAL.md) for the actual build log — decisions, bugs found, why.

## Quickstart

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

Run tests: `.venv/Scripts/python -m pytest tests/ -v`

There's also a Next.js frontend in `web/` — see its own quickstart in `CLAUDE.md`.

Status: fully working end-to-end. DB schema, seed data, tool layer, agent
orchestrator, and frontend all built and tested (18 passing tests). Full
cancel flow verified live through the actual UI against real DB state —
~5-8s per turn on the current OpenRouter/DeepSeek backend.
