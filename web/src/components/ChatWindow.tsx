"use client";

import { useEffect, useRef, useState } from "react";
import { sendChatMessage, type Mode } from "@/lib/api";
import Markdown from "@/components/Markdown";

export type ChatMessage = {
  id: string;
  role: "user" | "assistant";
  content: string;
};

type Props = {
  conversationId: string;
  customerName: string | null;
  mode: Mode;
};

const SUGGESTIONS = [
  "What are my orders?",
  "Cancel my most recent order",
  // Was "$3000" — the store prices everything in rupees, and the backend has a
  // whole formatting convention devoted to that. Handing the model a dollar sign
  // in the very first message was the one place the UI contradicted it.
  "Show me rings under ₹30,000",
  "What's your cancellation policy?",
];

const ADMIN_SUGGESTIONS = [
  "How were sales last month?",
  "What's running low on stock?",
  "Break down revenue by category this year",
  "Who are our top customers?",
];

export default function ChatWindow({ conversationId, customerName, mode }: Props) {
  const isAdmin = mode === "admin";
  const [messages, setMessages] = useState<ChatMessage[]>([
    {
      id: "welcome",
      role: "assistant",
      content: isAdmin
        ? `Staff console${customerName ? ` — signed in as ${customerName.split(" ")[0]}` : ""}. Ask about sales, inventory, orders or customers across the whole business. Changes are previewed before anything is applied.`
        : customerName
          ? `Hi ${customerName.split(" ")[0]}, welcome back. How can I help — orders, cancellations, or finding something new?`
          : "Hi! I can help with product recommendations and store policies. Log in with your email to ask about your own orders.",
    },
  ]);
  const [input, setInput] = useState("");
  const [sending, setSending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const bottomRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages, sending]);

  async function send(text: string) {
    const trimmed = text.trim();
    if (!trimmed || sending) return;

    setError(null);
    setInput("");
    setMessages((m) => [...m, { id: crypto.randomUUID(), role: "user", content: trimmed }]);
    setSending(true);

    try {
      const { reply } = await sendChatMessage(conversationId, trimmed);
      setMessages((m) => [...m, { id: crypto.randomUUID(), role: "assistant", content: reply }]);
    } catch {
      setError("Something went wrong reaching the concierge. Is the API server running?");
    } finally {
      setSending(false);
    }
  }

  return (
    <div className="flex flex-1 flex-col overflow-hidden">
      <header className="flex items-center justify-between border-b border-[var(--color-border)] px-6 py-4">
        <div>
          <p className="text-xs tracking-[0.3em] text-[var(--color-gold)] uppercase">
            DP Jewellers
          </p>
          <h1 className="font-display text-xl text-[var(--color-fg)]">
            {isAdmin ? "Staff Console" : "Concierge"}
          </h1>
        </div>
        {/* Cosmetic, but load-bearing for the person using it: staff can see any
            customer's data here, and that should never be ambiguous on screen. */}
        {isAdmin && (
          <span className="rounded-full border border-[var(--color-gold)] px-3 py-1 text-[10px] font-medium tracking-[0.2em] text-[var(--color-gold)] uppercase">
            Staff
          </span>
        )}
      </header>

      <div className="flex-1 overflow-y-auto px-4 py-6 sm:px-8">
        <div className="mx-auto flex max-w-2xl flex-col gap-4">
          {messages.map((m) => (
            <Bubble key={m.id} message={m} />
          ))}
          {sending && <TypingBubble />}
          {error && (
            <p className="self-center text-xs text-red-400">{error}</p>
          )}
          <div ref={bottomRef} />
        </div>
      </div>

      {messages.length <= 1 && (
        <div className="mx-auto mb-3 flex max-w-2xl flex-wrap gap-2 px-4 sm:px-8">
          {(isAdmin ? ADMIN_SUGGESTIONS : SUGGESTIONS).map((s) => (
            <button
              key={s}
              onClick={() => send(s)}
              className="rounded-full border border-[var(--color-border)] px-3 py-1.5 text-xs text-[var(--color-fg-muted)] transition hover:border-[var(--color-gold)] hover:text-[var(--color-gold-soft)]"
            >
              {s}
            </button>
          ))}
        </div>
      )}

      <form
        onSubmit={(e) => {
          e.preventDefault();
          send(input);
        }}
        className="border-t border-[var(--color-border)] px-4 py-4 sm:px-8"
      >
        <div className="mx-auto flex max-w-2xl items-center gap-2">
          <input
            value={input}
            onChange={(e) => setInput(e.target.value)}
            placeholder={
              isAdmin
                ? "Ask about sales, stock, orders or customers…"
                : "Ask about an order, or find your next piece…"
            }
            className="flex-1 rounded-full border border-[var(--color-border)] bg-[var(--color-bg-elevated)] px-4 py-2.5 text-sm text-[var(--color-fg)] outline-none focus:border-[var(--color-gold)]"
          />
          <button
            type="submit"
            disabled={sending || !input.trim()}
            className="rounded-full bg-[var(--color-gold)] px-5 py-2.5 text-sm font-medium text-[var(--color-user-bubble-fg)] transition hover:bg-[var(--color-gold-soft)] disabled:opacity-40"
          >
            Send
          </button>
        </div>
      </form>
    </div>
  );
}

function Bubble({ message }: { message: ChatMessage }) {
  const isUser = message.role === "user";
  return (
    <div className={`flex ${isUser ? "justify-end" : "justify-start"}`}>
      <div
        className={`max-w-[80%] rounded-2xl px-4 py-2.5 ${
          isUser
            ? "rounded-br-sm whitespace-pre-wrap bg-[var(--color-user-bubble)] text-sm leading-relaxed text-[var(--color-user-bubble-fg)]"
            : "rounded-bl-sm bg-[var(--color-bot-bubble)] text-[var(--color-fg)]"
        }`}
      >
        {isUser ? message.content : <Markdown content={message.content} />}
      </div>
    </div>
  );
}

function TypingBubble() {
  return (
    <div className="flex justify-start">
      <div className="flex items-center gap-1 rounded-2xl rounded-bl-sm bg-[var(--color-bot-bubble)] px-4 py-3">
        <span className="typing-dot h-1.5 w-1.5 rounded-full bg-[var(--color-fg-muted)]" style={{ animationDelay: "0ms" }} />
        <span className="typing-dot h-1.5 w-1.5 rounded-full bg-[var(--color-fg-muted)]" style={{ animationDelay: "150ms" }} />
        <span className="typing-dot h-1.5 w-1.5 rounded-full bg-[var(--color-fg-muted)]" style={{ animationDelay: "300ms" }} />
      </div>
    </div>
  );
}
