# Dev Journal — TP Jewellers Chatbot

Running log of what was built, what broke, and why decisions were made.
Newest entries at the top. Written for two audiences at once: future-me
debugging this at 2am, and interview-me explaining design choices out loud.

---

## 2026-08-15 (later night) — Markdown rendering, live gold/silver rates, git init

**Markdown rendering:** DeepSeek (like most models) replies in markdown
(`**bold**`, numbered lists) but the chat UI was rendering it as literal
text. Added `react-markdown` + `remark-gfm` (`web/src/components/Markdown.tsx`),
themed to match the gold/dark palette. Only assistant bubbles parse
markdown — user's own typed messages stay plain text (no reason to
markdown-parse input you didn't generate).

**Live gold/silver rates (`src/tools/market_tools.py`, new `get_metal_rates`
tool):** two free, keyless public APIs — `gold-api.com` for XAU/XAG spot
price, `open.er-api.com` for USD→INR — converted to INR/gram for 24K/22K/18K
gold and fine silver. 5-minute in-memory cache (avoid hammering free APIs on
every turn) and fails soft (returns an error dict, orchestrator's existing
exception handling would have caught it anyway, but explicit is clearer).
**Important honesty constraint, stated in both the tool's own response and
the system prompt:** this is a spot-market estimate, not a showroom quote —
real Indian retail pricing adds import duty, GST, and making charges on top.
The system prompt explicitly forbids presenting spot rate as "our price."
Live-tested: "what's today's date and gold/silver rate in india" → correct
date, correct INR/gram figures for all three gold purities + silver, caveat
included unprompted, 13.8s.

**Git:** initialized, remote `https://github.com/ffllyygod/tpjewshuru.git`
added, not yet pushed. Local identity only (`ffllyygod` / same as the
`eopps` repo earlier this session) — never touched global git config.
Extended `.gitignore` for the new `web/` Next.js app (`node_modules/`,
`.next/`, `.env.local`) before the first commit — verified `.env` (real
secrets) and `node_modules` were excluded from `git status --short` before
committing, not just trusted the `.gitignore` to be correct.

## 2026-08-15 (night, continued) — NIM stalled on this network too → OpenRouter/DeepSeek

The NIM swap above was correct in design but hit a real environmental wall:
live requests took **2–5 minutes** even on the corrected model id — worse
than local Ollama. Diagnosed properly before assuming "cloud = slow was
wrong":
- NVIDIA's own response payload included `e2e_latency_seconds: 0.15` — their
  server processed the request in 150ms.
- A raw `curl` POST (bypassing our app entirely) reproduced the multi-minute
  stall — so it wasn't our code or the `openai` SDK.
- Auth-failing requests (bad key) to the *same* NIM endpoint came back in
  under a second; only requests that succeeded and returned real generated
  content stalled. Same test against Groq's endpoint showed the identical
  signature.
- Conclusion: something between this machine and NVIDIA's specific CDN was
  stalling delivery of successful AI-response payloads — likely Windows
  Defender's Network Inspection System (`NISEnabled: True`) doing deep
  packet inspection, though never fully confirmed (didn't need to be).

Tested OpenRouter (hosting DeepSeek) directly with the same
fast-fail-vs-real-response methodology: **2.07s** for a real completion,
**1.9s** for a real tool-call response with correct `tool_calls` shape.
Not a fluke — the stall was NIM/that-CDN-specific, not a general
"AI API traffic is throttled on this network" problem.

**Swapped again**, same pattern as before: only `_call_model()` and the
env vars changed. Renamed `NIM_*` → generic `LLM_*` (`LLM_API_KEY`,
`LLM_BASE_URL`, `LLM_MODEL`) since the code was never actually
NIM-specific — it's OpenAI-protocol-compatible, and NIM happened to be the
first provider tried. This is now the **third** backend this orchestrator
has run against (Anthropic → Ollama → NIM → OpenRouter/DeepSeek) with zero
changes to the tool-loop logic itself, which is the whole point of keeping
`_call_model()` as the one swap seam.

Live-reverified the full flow against OpenRouter/DeepSeek: "what are my
orders?" (8.4s) → "cancel TPJ-DEMO01" (5.9s, correctly paused for
confirmation) → "yes" (5.2s, actually cancelled — verified `TPJ-DEMO01`
flipped to `CANCELLED` in Postgres, clean 3-call `tool_call_log` trace) →
wrong-status rejection on TPJ-DEMO02 (5.7s, correctly refused). **Total
~19s for the 3-turn cancel flow**, versus 6+ minutes before. This is the
actual demo-ready state.

Uninstalled Ollama (`winget uninstall Ollama.Ollama`) and removed
`~/.ollama` (5.3GB of model blobs) — no longer needed.

**Lesson for the interview:** when something is "too slow," verify *where*
the time is actually going before picking a fix. The natural assumption
here would've been "the model is slow" or "cloud inference is slow" — both
wrong. It was a specific network path to a specific provider. Cheap
diagnostic (compare fast-failing vs. successful requests, check the
provider's own reported server-side latency) saved from either tuning the
wrong thing or giving up on cloud inference entirely.

## 2026-08-15 (night) — Local Ollama too slow: swapped to NVIDIA NIM (Qwen2.5-72B)

Live-tested the Next.js UI end to end against local `llama3.1:8b` and it
worked correctly (full cancel flow, wrong-status rejection, anonymous policy
Q&A — all verified against real DB state) but was **~2 minutes per turn**,
even warm, on this machine's CPU (i3-10100F, 4c/8t — the installed GPU, a
GT 610, is too old for CUDA and provides zero acceleration). That's not
viable for a live interview demo. User asked for a hosted/modern backend
instead, specifically naming "Qwen agents" and "NVIDIA models."

**Decision: NVIDIA NIM hosting Qwen2.5-72B-Instruct.** Satisfies both —
NVIDIA-hosted inference, serving a Qwen model, OpenAI-compatible
chat-completions API with tool calling, free developer API key. Deliberately
did **not** adopt the separate "Qwen-Agent" open-source orchestration
framework — that would mean rewriting the working, tested tool-use loop this
close to a deadline for no real benefit. The loop itself
(`src/agent/orchestrator.py`) is backend-agnostic by design (this is the
second time it's proven that — first Anthropic→Ollama, now Ollama→NIM — same
`_call_model()` swap point both times); a faster model behind the same loop
is a much smaller, safer change.

**The one real gotcha in the swap:** OpenAI's tool-calling protocol (which
NIM implements) differs from Ollama's client in two ways that will silently
break tool-result linking if missed:
1. Each tool call carries an `id`; the follow-up `role: "tool"` message must
   echo it back as `tool_call_id`. Ollama's client didn't require this.
2. `tool_call.function.arguments` arrives as a **JSON string**, not an
   already-parsed dict (Ollama parsed it for us) — needs `json.loads(...)`.

Updated `tests/test_orchestrator.py`'s fake-response factory to match this
shape (`response.choices[0].message`, tool calls with `.id`, string-encoded
`arguments`) so the mechanics tests keep accurately simulating the real
protocol. All 18 tests still pass post-swap, before ever calling a live
model. `requirements.txt`: `ollama` → `openai` (the official SDK works
against any OpenAI-compatible `base_url`, which is what makes this swap —
and any future one, e.g. Groq — cheap). New env vars: `NIM_API_KEY`,
`NIM_BASE_URL`, `NIM_MODEL` (default `qwen/qwen2.5-72b-instruct`).

Live end-to-end re-verification (full cancel flow + latency check) and the
actual Ollama uninstall are both pending the user's NIM API key.

## 2026-08-15 (evening) — Next.js frontend

Scaffolded `web/` with `create-next-app` (Next.js 16.3.1, App Router,
TypeScript, Tailwind 4). Client-side-only chat UI — no server components/API
routes needed since it just talks to the FastAPI backend directly via
`fetch` (CORS already wide-open there for the demo).

- `src/lib/api.ts` — thin client for `POST /conversations` / `POST /chat`.
- `src/components/LoginScreen.tsx` — email entry (optional — blank = browse
  anonymously, product/policy questions only, matches the API's actual
  auth model of "email resolves to a customer or you get an anonymous
  session").
- `src/components/ChatWindow.tsx` — message list, typing indicator, suggested
  first prompts, send box. Session state (`conversation_id`) lives in
  `page.tsx`, nothing persisted beyond the tab — refreshing starts a new
  conversation, which is fine for a demo.
- Dark/gold jewellery-brand palette (`globals.css`), Playfair Display for
  headings + Inter for body via `next/font/google`.

`npm run build` clean (no type/lint errors). Dev server on
`localhost:3000`, backend on `localhost:8000` — both need to be running
for the UI to work (`.env.local` points `NEXT_PUBLIC_API_BASE` at the API).

## 2026-08-15 (later still) — Live end-to-end test found a real bug: token transcription

First live conversation through llama3.1:8b (via Ollama, no external API): asked
for orders, requested cancellation of TPJ-DEMO01, model correctly paused and
asked for explicit confirmation (prompt guardrail working). On "confirm", the
model called `cancel_order` with `confirmation_token: "<insert confirmation
token here>"` — a placeholder, not the actual 22-char token it had received
two tool calls earlier. `cancel_order` correctly rejected it (`invalid_token`),
but the model then hallucinated a recovery step instead of retrying properly.

**Root cause:** small local models are unreliable at verbatim recall of opaque
random strings across turns, even a few messages later. This isn't really an
"8B model is dumb" problem — any LLM (including frontier ones) is worse at
exact character-for-character transcription than at semantic tasks; making a
security-critical step depend on that was the actual design flaw.

**Fix:** removed `confirmation_token` from `cancel_order`'s parameters
entirely. It now looks up the most recent valid/unused/unexpired token for
`(conversation_id, order, cancel_order action)` itself. All the original
security properties are unchanged (must complete eligibility check first in
the same conversation, single-use, time-limited) — the model just no longer
has to be the one to carry a secret verbatim between tool calls. General
lesson: **don't make tool-calling correctness depend on an LLM perfectly
echoing back an opaque value you already have server-side access to** —
if the orchestrator can look it up itself, have it look it up itself.

Re-tested live after the fix: full flow (ask orders → request cancel →
model asks for confirmation → user confirms → cancel_order succeeds)
verified against the real DB (`TPJ-DEMO01` flipped to `CANCELLED`,
`tool_call_log` has the clean 2-call trace). Also verified: wrong-status
rejection (TPJ-DEMO02, already shipped, correctly refused + redirected to
return policy) and anonymous-session policy Q&A (no login, natural-language
ring-sizing question, correct answer from the FTS-fixed knowledge tool).

## 2026-08-15 (later) — Orchestrator mechanics tested without hitting the model

Added `tests/test_orchestrator.py` — patches `_call_model` with a fake client
so the tool-loop routing, server-side identity injection, and audit logging
are verified deterministically, without needing Ollama running or a real
model call. Notably: `test_customer_id_is_injected_not_agent_controlled`
proves that even if a (simulated, misbehaving) model tries to pass its own
`customer_id` in a tool call, the orchestrator's injected value silently
wins — the tool never sees an attacker-supplied one. This is the concrete
answer to "how do you stop prompt injection from leaking another customer's
data": the capability doesn't exist at the tool layer, so there's nothing
for an injected prompt to exploit. 18 tests passing total (14 tool-layer +
4 orchestrator-mechanics), all before the first real model call.

## 2026-08-15 — Local LLM backend (no external API), product recommendation tool

**Context:** 48-hour demo deadline. User (Arun) doesn't have an Anthropic API
key yet and specifically wants an in-house/local setup rather than a hosted
API — also wants product browsing/recommendation added, not just order
support.

**Decision: local Ollama model instead of Claude API.**
Training a model from scratch on our data was floated and rejected — not
feasible in 48h regardless of technique (no compute, no data volume: 46
products + 6 policy docs is nowhere near enough even for fine-tuning to be
worthwhile). What *is* real: swap the LLM backend to a local open-source
model via Ollama, keep the exact same tool-calling agent architecture.
Zero external API calls, zero per-token cost, works offline for the demo.

**Architecture note for interview articulation:** the orchestrator
(`src/agent/orchestrator.py`) is a hand-rolled loop, not LangGraph, but the
shape is identical to a state graph:
  - state = the running `messages` list
  - node A = call the model
  - conditional edge = "did the response include tool_calls?"
  - node B = execute each tool call (with server-side identity injection +
    audit logging), loop back to node A
  - terminal = model responds with no tool_calls

Chose not to bring in LangGraph for this: the guardrail logic (customer_id
injection, confirmation-token scoping to conversation_id, audit logging to
`tool_call_log`) needed to be visible and easy to reason about under time
pressure, not hidden behind a framework abstraction. This is a defensible
senior-level tradeoff to explain in an interview — "I know when to reach for
a framework and when a 40-line loop is the more honest solution."

**Added `search_products` / `get_product_details` tools** — was missing
from the original scope (order-support only). Not customer-scoped (catalog
browsing is public), unlike the order tools.

**Bugs found and fixed today (via tests, before ever touching the LLM):**
1. `seed_db.py` passed a Python list (`sizes_available`) straight to psycopg
   without wrapping in `Json(...)` — psycopg serialized it as a Postgres
   array literal (`{5,6,7}`), which is invalid JSON for a JSONB column.
   Fixed by wrapping explicitly. Lesson: JSONB columns need explicit `Json()`
   wrapping in psycopg3 — it does not infer JSON intent from a plain list/dict
   the way you'd hope.
2. `search_knowledge` used `websearch_to_tsquery`, which ANDs every
   non-stopword term. A completely natural question like "how do I know my
   ring size" tsquery's to `'know' & 'ring' & 'size'` — and the doc doesn't
   contain "know", so it matched nothing despite being an obvious match.
   Fixed by building an OR'd tsquery from the query's lexemes instead
   (`ts_rank` still favors more-term matches). This is the single most
   important fix for making the knowledge tool actually useful — an AND-only
   FTS query is close to useless for conversational input.
3. `confirmation_tokens.conversation_id` is `NOT NULL` in the schema (I'd
   forgotten to thread it through from `check_cancellation_eligibility` /
   `cancel_order`). Fixing this properly (not just satisfying the constraint)
   turned into a real security improvement: the token is now scoped to
   (conversation, action, order), not just (action, order) — so a leaked
   token can't be replayed from a different conversation. Added a test for
   exactly that (`test_token_rejected_from_different_conversation`).

**Verification approach:** wrote `tests/test_tools.py` hitting the live
seeded DB directly (no mocking) before ever wiring up an LLM. This validates
the guardrail-critical logic (ownership scoping, cancellation-window rules,
token replay protection) independent of prompt behavior — so once the model
is live, the only unknown left is "does it call tools in the right order and
phrase results well," not "is the underlying logic even correct."

**Added a personal demo customer** (`scripts/add_demo_user.py`) — Arun,
with two orders in different states (one cancellable, one already shipped)
and two new named products, on top of the randomized Faker seed data. Makes
the live demo conversation feel real instead of talking to `TPJ-10007`.

---

## 2026-08-15 (earlier) — Schema + seed verified end-to-end

Docker/WSL2 finished enabling after reboot. `docker compose up -d` brought
up `pgvector/pgvector:pg16` on port 5433 (deliberately not 5432, to avoid
clashing with an unrelated project's local Postgres). Ran
`scripts/seed_db.py --reset` — schema applied, 24 products / 10 customers /
20 orders / 6 knowledge docs seeded. First 5 orders hand-crafted to cover
every branch of the cancellation-eligibility logic (placed-recent,
confirmed-old, shipped, delivered, already-cancelled) rather than relying on
random data to happen to cover them.

---

## Earlier — Architecture decided

See `docs/ARCHITECTURE.md` for the full writeup. Core call: this is a
tool-calling agent, not classic RAG — order/cancellation queries are
transactional lookups against Postgres, not semantic retrieval. Retrieval
(hybrid FTS + pgvector, though pgvector isn't wired up yet — FTS alone is
enough for 6 policy docs) is one tool among several, not the backbone of the
system.
