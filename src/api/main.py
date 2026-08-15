"""Minimal FastAPI surface for the chatbot brain.

Two endpoints:
  POST /conversations  -> start a session, resolve identity by email
  POST /chat            -> send a message, get the assistant's reply

This is deliberately thin — it's the contract the web app talks to. Auth in
this demo is "email exists in customers table"; swap _resolve_identity for
real session auth later without touching the agent loop.
"""

from __future__ import annotations

import uuid

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from psycopg.rows import dict_row
from pydantic import BaseModel

from src.agent.orchestrator import run_turn
from src.db.connection import get_conn
from src.tools.account_tools import get_customer_by_email

app = FastAPI(title="TP Jewellers Chatbot")

# Wide-open CORS for demo purposes — tighten before anything real.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class StartConversationRequest(BaseModel):
    email: str | None = None  # None = anonymous browsing (knowledge questions only)


class StartConversationResponse(BaseModel):
    conversation_id: str
    customer_id: str | None
    customer_name: str | None


class ChatRequest(BaseModel):
    conversation_id: str
    message: str


class ChatResponse(BaseModel):
    reply: str


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/conversations", response_model=StartConversationResponse)
def start_conversation(req: StartConversationRequest):
    customer = get_customer_by_email(req.email) if req.email else None
    if req.email and not customer:
        raise HTTPException(status_code=404, detail="No customer found for that email.")

    conv_id = str(uuid.uuid4())
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO conversations (id, customer_id) VALUES (%s, %s)",
            (conv_id, customer["id"] if customer else None),
        )
        conn.commit()

    return StartConversationResponse(
        conversation_id=conv_id,
        customer_id=str(customer["id"]) if customer else None,
        customer_name=customer["name"] if customer else None,
    )


@app.post("/chat", response_model=ChatResponse)
def chat(req: ChatRequest):
    with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT customer_id FROM conversations WHERE id = %s", (req.conversation_id,))
        conv = cur.fetchone()
    if not conv:
        raise HTTPException(status_code=404, detail="Unknown conversation_id.")

    customer_id = str(conv["customer_id"]) if conv["customer_id"] else None
    reply = run_turn(req.conversation_id, customer_id, req.message)
    return ChatResponse(reply=reply)
