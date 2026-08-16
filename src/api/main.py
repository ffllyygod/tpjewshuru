"""Minimal FastAPI surface for the chatbot brain.

Two endpoints:
  POST /conversations  -> start a session, identity from a verified SuperTokens
                           session if present, anonymous otherwise
  POST /chat            -> send a message, get the assistant's reply

Auth: self-hosted SuperTokens (Passwordless/OTP-over-email), mounted at
/auth by its own middleware — see src/api/auth.py for the full rationale.
customer_id is NEVER taken from a client-supplied field; it's always
resolved fresh from a verified session. This is the same "identity from a
verified source only" principle already used throughout src/tools/, now
applied at the HTTP boundary instead of just inside tool calls.
"""

from __future__ import annotations

import os
import uuid

from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from psycopg.rows import dict_row
from pydantic import BaseModel
from supertokens_python import get_all_cors_headers
from supertokens_python.framework.fastapi import get_middleware
from supertokens_python.recipe.session import SessionContainer
from supertokens_python.recipe.session.framework.fastapi import verify_session

from src.agent.orchestrator import run_turn
from src.api.auth import init_auth, resolve_customer_id_from_session, resolve_principal_from_session
from src.db.connection import get_conn

init_auth()

app = FastAPI(title="DP Jewellers Chatbot")

app.add_middleware(get_middleware())

# Real origins now instead of a wildcard — tightened alongside real auth
# (see JOURNAL.md). Header-based sessions (not cookies), so allow_credentials
# isn't needed; SuperTokens manages Access-Control-Expose-Headers itself for
# its own response headers.
_allowed_origins = [
    os.environ.get("WEBSITE_DOMAIN", "http://localhost:3000"),
    "http://localhost:3000",
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=list(set(_allowed_origins)),
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type"] + get_all_cors_headers(),
)


class StartConversationRequest(BaseModel):
    # 'admin' opens a staff-console conversation and is rejected with 403 unless
    # the verified session resolves to an admin. Note this is a *request*, never
    # an assertion of identity — the server decides.
    mode: str = "customer"


class StartConversationResponse(BaseModel):
    conversation_id: str
    customer_id: str | None
    customer_name: str | None
    role: str
    mode: str


class ChatRequest(BaseModel):
    conversation_id: str
    message: str


class ChatResponse(BaseModel):
    reply: str


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/conversations", response_model=StartConversationResponse)
def start_conversation(
    req: StartConversationRequest | None = None,
    session: SessionContainer | None = Depends(verify_session(session_required=False)),
):
    principal = resolve_principal_from_session(session)
    requested_mode = (req.mode if req else "customer") or "customer"

    if requested_mode not in ("customer", "admin"):
        raise HTTPException(status_code=422, detail="mode must be 'customer' or 'admin'.")
    # Staff mode is granted by the server from the verified session's role —
    # never by the client asking nicely.
    if requested_mode == "admin" and not principal.is_admin:
        raise HTTPException(status_code=403, detail="Staff access required.")

    conv_id = str(uuid.uuid4())
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO conversations (id, customer_id, mode) VALUES (%s, %s, %s)",
            (conv_id, principal.customer_id, requested_mode),
        )
        conn.commit()

    return StartConversationResponse(
        conversation_id=conv_id,
        customer_id=principal.customer_id,
        customer_name=principal.name,
        role=principal.role,
        mode=requested_mode,
    )


@app.post("/chat", response_model=ChatResponse)
def chat(req: ChatRequest, session: SessionContainer | None = Depends(verify_session(session_required=False))):
    with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT customer_id, mode FROM conversations WHERE id = %s", (req.conversation_id,))
        conv = cur.fetchone()
    if not conv:
        raise HTTPException(status_code=404, detail="Unknown conversation_id.")

    conv_customer_id = str(conv["customer_id"]) if conv["customer_id"] else None
    principal = resolve_principal_from_session(session)

    # A customer-owned conversation requires the caller's verified session to
    # resolve to that SAME customer — closes the "guess someone else's
    # conversation_id" gap. Anonymous conversations need no session, unchanged.
    if conv_customer_id is not None and conv_customer_id != principal.customer_id:
        raise HTTPException(status_code=403, detail="This conversation belongs to a different customer.")

    # Re-checked every turn, not just at creation: if this account's admin flag
    # was revoked since the conversation started, staff tools stop working on the
    # very next message rather than at token expiry.
    if conv["mode"] == "admin" and not principal.is_admin:
        raise HTTPException(status_code=403, detail="Staff access required for this conversation.")

    reply = run_turn(
        req.conversation_id,
        conv_customer_id,
        req.message,
        is_admin=(conv["mode"] == "admin"),
    )
    return ChatResponse(reply=reply)
