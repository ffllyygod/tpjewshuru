"use client";

import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
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
  "Something for our anniversary, around ₹50,000",
  "Where's my last order?",
  "Show me gold rings under ₹30,000",
  "Can I get a ring made in 22K?",
];

const ADMIN_SUGGESTIONS = [
  "How were sales last month?",
  "What's running low on stock?",
  "Any cancellations this month?",
  "Custom design requests waiting",
];

export default function ChatWindow({ conversationId, customerName, mode }: Props) {
  const isAdmin = mode === "admin";
  const firstName = customerName?.split(" ")[0] ?? null;

  const [messages, setMessages] = useState<ChatMessage[]>([
    {
      id: "welcome",
      role: "assistant",
      content: isAdmin
        ? `Staff console${firstName ? ` — signed in as ${firstName}` : ""}. Ask about sales, inventory, orders or customers across the whole business. Anything that changes data is previewed before it's applied.`
        : firstName
          ? `Hello ${firstName}, lovely to see you. Are you looking for something in particular today, or shall I help with an order?`
          : "Hello! I can help you find a piece or answer questions about the store. Sign in with your email if you'd like to talk about your own orders.",
    },
  ]);
  const [input, setInput] = useState("");
  const [sending, setSending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [lastFailed, setLastFailed] = useState<string | null>(null);
  const [atBottom, setAtBottom] = useState(true);

  const scrollerRef = useRef<HTMLDivElement>(null);
  const bottomRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);

  const scrollToBottom = useCallback((behavior: ScrollBehavior = "smooth") => {
    bottomRef.current?.scrollIntoView({ behavior, block: "end" });
  }, []);

  // Only follow the conversation if the reader is already at the bottom.
  // Unconditional auto-scroll yanks the page away from someone who has scrolled
  // up to re-read an invoice — the exact moment they most need to stay put.
  useLayoutEffect(() => {
    if (atBottom) scrollToBottom(messages.length <= 1 ? "auto" : "smooth");
  }, [messages, sending, atBottom, scrollToBottom]);

  function onScroll() {
    const el = scrollerRef.current;
    if (!el) return;
    const distance = el.scrollHeight - el.scrollTop - el.clientHeight;
    setAtBottom(distance < 80);
  }

  const send = useCallback(
    async (text: string) => {
      const trimmed = text.trim();
      if (!trimmed || sending) return;

      setError(null);
      setLastFailed(null);
      setInput("");
      setAtBottom(true);
      setMessages((m) => [...m, { id: crypto.randomUUID(), role: "user", content: trimmed }]);
      setSending(true);

      try {
        const { reply } = await sendChatMessage(conversationId, trimmed);
        setMessages((m) => [...m, { id: crypto.randomUUID(), role: "assistant", content: reply }]);
      } catch {
        // Keep the failed text so Retry is one tap rather than a retype.
        setLastFailed(trimmed);
        setError("That didn't get through. Check your connection and try again.");
      } finally {
        setSending(false);
        // Hand focus back so the next message doesn't need a click.
        inputRef.current?.focus();
      }
    },
    [conversationId, sending],
  );

  // Grow the composer with its content, up to a ceiling, then scroll inside it.
  useEffect(() => {
    const el = inputRef.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, 160)}px`;
  }, [input]);

  const showStarters = messages.length <= 1 && !sending;

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <header className="sticky top-0 z-20 border-b border-[var(--color-border)] bg-[var(--color-bg)]/85 backdrop-blur-md">
        <div className="mx-auto flex max-w-3xl items-center justify-between gap-3 px-4 py-3 sm:px-6">
          <div className="flex items-center gap-3">
            <Monogram />
            <div className="leading-tight">
              <p className="text-[10px] tracking-[0.28em] text-[var(--color-gold)] uppercase">
                DP Jewellers
              </p>
              <h1 className="font-display text-lg tracking-tight">
                {isAdmin ? "Staff Console" : "Concierge"}
              </h1>
            </div>
          </div>

          {/* Cosmetic, but load-bearing for the person using it: staff can see
              any customer's data here, and that should never be ambiguous. */}
          {isAdmin && (
            <span className="rounded-full border border-[var(--color-gold)] px-2.5 py-1 text-[10px] font-medium tracking-[0.18em] text-[var(--color-gold)] uppercase">
              Staff
            </span>
          )}
        </div>
      </header>

      <div
        ref={scrollerRef}
        onScroll={onScroll}
        className="min-h-0 flex-1 overflow-y-auto overscroll-contain px-4 py-6 sm:px-6"
      >
        <div className="mx-auto flex max-w-3xl flex-col gap-5">
          {messages.map((m, i) => (
            <Message key={m.id} message={m} isLatest={i === messages.length - 1} />
          ))}
          {sending && <Thinking />}
          <div ref={bottomRef} className="h-px" />
        </div>
      </div>

      {/* Screen readers get replies announced without the whole log re-reading
          on every keystroke. */}
      <p aria-live="polite" className="sr-only">
        {sending ? "Concierge is replying" : messages.at(-1)?.role === "assistant" ? messages.at(-1)?.content : ""}
      </p>

      <div className="relative">
        {!atBottom && (
          <button
            type="button"
            onClick={() => {
              setAtBottom(true);
              scrollToBottom();
            }}
            className="absolute -top-12 left-1/2 z-10 -translate-x-1/2 rounded-full border border-[var(--color-border)] bg-[var(--color-bg-elevated)] px-3.5 py-1.5 text-xs text-[var(--color-fg-muted)] shadow-[var(--shadow-lift)] transition hover:text-[var(--color-fg)]"
          >
            ↓ Latest
          </button>
        )}

        {showStarters && (
          <div className="mx-auto max-w-3xl px-4 pb-2 sm:px-6">
            <div className="flex flex-wrap gap-2">
              {(isAdmin ? ADMIN_SUGGESTIONS : SUGGESTIONS).map((s) => (
                <button
                  key={s}
                  onClick={() => send(s)}
                  className="rounded-full border border-[var(--color-border)] bg-[var(--color-bg-elevated)] px-3 py-1.5 text-xs text-[var(--color-fg-muted)] transition hover:border-[var(--color-gold)] hover:text-[var(--color-fg)]"
                >
                  {s}
                </button>
              ))}
            </div>
          </div>
        )}

        {error && (
          <div className="mx-auto max-w-3xl px-4 pb-2 sm:px-6">
            <div
              role="alert"
              className="flex items-center gap-3 rounded-[var(--radius-card)] border border-[var(--color-danger)]/30 bg-[var(--color-danger-wash)] px-3.5 py-2.5 text-xs text-[var(--color-danger)]"
            >
              <span className="flex-1">{error}</span>
              {lastFailed && (
                <button
                  onClick={() => send(lastFailed)}
                  className="font-medium underline underline-offset-2"
                >
                  Retry
                </button>
              )}
              <button
                onClick={() => setError(null)}
                aria-label="Dismiss"
                className="text-base leading-none opacity-60 hover:opacity-100"
              >
                ×
              </button>
            </div>
          </div>
        )}

        <form
          onSubmit={(e) => {
            e.preventDefault();
            send(input);
          }}
          className="border-t border-[var(--color-border)] bg-[var(--color-bg)] px-4 pt-3 pb-[max(0.75rem,env(safe-area-inset-bottom))] sm:px-6"
        >
          <div className="mx-auto max-w-3xl">
            <div className="flex items-end gap-2 rounded-[22px] border border-[var(--color-border)] bg-[var(--color-bg-elevated)] p-1.5 pl-4 shadow-[var(--shadow-soft)] transition focus-within:border-[var(--color-border-strong)]">
              <textarea
                ref={inputRef}
                rows={1}
                value={input}
                onChange={(e) => setInput(e.target.value)}
                onKeyDown={(e) => {
                  // Enter sends, Shift+Enter breaks the line. A plain textarea
                  // would make Enter insert a newline, which is wrong for chat.
                  if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
                    e.preventDefault();
                    send(input);
                  }
                }}
                placeholder={
                  isAdmin
                    ? "Ask about sales, stock, orders or customers…"
                    : "Ask about an order, or find your next piece…"
                }
                aria-label="Message"
                /* 16px minimum: anything smaller makes iOS Safari zoom the page
                   on focus and never zoom back out. */
                className="max-h-40 flex-1 resize-none bg-transparent py-2 text-base leading-6 text-[var(--color-fg)] outline-none placeholder:text-[var(--color-fg-subtle)] sm:text-[15px]"
              />
              <button
                type="submit"
                disabled={sending || !input.trim()}
                aria-label="Send message"
                className="mb-0.5 grid h-9 w-9 shrink-0 place-items-center rounded-full bg-[var(--color-gold)] text-[var(--color-user-bubble-fg)] transition hover:bg-[var(--color-gold-soft)] disabled:cursor-not-allowed disabled:opacity-35"
              >
                <svg viewBox="0 0 20 20" className="h-4 w-4" fill="none" aria-hidden>
                  <path
                    d="M10 16V4m0 0L5 9m5-5 5 5"
                    stroke="currentColor"
                    strokeWidth="1.8"
                    strokeLinecap="round"
                    strokeLinejoin="round"
                  />
                </svg>
              </button>
            </div>
            <p className="mt-1.5 px-1 text-[11px] text-[var(--color-fg-subtle)]">
              <kbd className="font-sans">Enter</kbd> to send ·{" "}
              <kbd className="font-sans">Shift</kbd>+<kbd className="font-sans">Enter</kbd> for a new line
            </p>
          </div>
        </form>
      </div>
    </div>
  );
}

/**
 * Assistant replies are prose on the page, not bubbles.
 *
 * They routinely contain a full tax invoice table. Capping that at 80% width
 * inside a rounded bubble — as the previous build did — squeezed a document
 * into a speech balloon and forced it to scroll sideways. Only the customer's
 * own short messages keep bubble treatment, which is also the clearer visual
 * distinction: one side is a person talking, the other is the shop answering.
 */
function Message({ message, isLatest }: { message: ChatMessage; isLatest: boolean }) {
  if (message.role === "user") {
    return (
      <div className={`flex justify-end ${isLatest ? "rise" : ""}`}>
        <div className="max-w-[85%] rounded-[18px] rounded-br-md bg-[var(--color-user-bubble)] px-4 py-2.5 text-[14.5px] leading-relaxed whitespace-pre-wrap text-[var(--color-user-bubble-fg)] sm:max-w-[75%]">
          {message.content}
        </div>
      </div>
    );
  }

  return (
    <div className={`flex gap-3 ${isLatest ? "rise" : ""}`}>
      <Monogram small />
      <div className="min-w-0 flex-1 pt-0.5">
        <Markdown content={message.content} />
      </div>
    </div>
  );
}

function Thinking() {
  return (
    <div className="flex gap-3">
      <Monogram small />
      <div className="flex items-center gap-1.5 pt-3">
        <span className="typing-dot h-1.5 w-1.5 rounded-full bg-[var(--color-fg-subtle)]" style={{ animationDelay: "0ms" }} />
        <span className="typing-dot h-1.5 w-1.5 rounded-full bg-[var(--color-fg-subtle)]" style={{ animationDelay: "150ms" }} />
        <span className="typing-dot h-1.5 w-1.5 rounded-full bg-[var(--color-fg-subtle)]" style={{ animationDelay: "300ms" }} />
      </div>
    </div>
  );
}

/** A small gold "DP" mark — cheaper than a logo file and stays crisp anywhere. */
function Monogram({ small = false }: { small?: boolean }) {
  return (
    <span
      aria-hidden
      className={[
        "grid shrink-0 place-items-center rounded-full border border-[var(--color-gold)]/40 bg-[var(--color-gold-wash)] font-display text-[var(--color-gold)]",
        small ? "mt-0.5 h-7 w-7 text-[11px]" : "h-9 w-9 text-[13px]",
      ].join(" ")}
    >
      DP
    </span>
  );
}
