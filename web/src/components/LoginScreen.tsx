"use client";

import { useState, FormEvent } from "react";

type Props = {
  onStart: (email: string | null) => void;
  loading: boolean;
  error: string | null;
};

export default function LoginScreen({ onStart, loading, error }: Props) {
  const [email, setEmail] = useState("arun@shurutech.com");

  function submit(e: FormEvent) {
    e.preventDefault();
    onStart(email.trim() || null);
  }

  return (
    <div className="flex flex-1 items-center justify-center px-4">
      <div className="w-full max-w-sm">
        <div className="mb-8 text-center">
          <p className="text-xs tracking-[0.3em] text-[var(--color-gold)] uppercase mb-2">
            TP Jewellers
          </p>
          <h1 className="font-display text-3xl text-[var(--color-fg)]">
            Concierge
          </h1>
          <p className="mt-2 text-sm text-[var(--color-fg-muted)]">
            Ask about your orders, cancellations, or find your next piece.
          </p>
        </div>

        <form
          onSubmit={submit}
          className="rounded-xl border border-[var(--color-border)] bg-[var(--color-bg-elevated)] p-6 shadow-xl"
        >
          <label className="mb-2 block text-xs uppercase tracking-wide text-[var(--color-fg-muted)]">
            Email (optional)
          </label>
          <input
            type="email"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            placeholder="you@example.com"
            className="w-full rounded-lg border border-[var(--color-border)] bg-[var(--color-bg)] px-3 py-2.5 text-sm text-[var(--color-fg)] outline-none focus:border-[var(--color-gold)]"
          />
          <p className="mt-2 text-xs text-[var(--color-fg-muted)]">
            Leave blank to browse anonymously — you can still ask about products and
            policies, just not your own orders.
          </p>

          {error && (
            <p className="mt-3 text-xs text-red-400">{error}</p>
          )}

          <button
            type="submit"
            disabled={loading}
            className="mt-5 w-full rounded-lg bg-[var(--color-gold)] py-2.5 text-sm font-medium text-[var(--color-user-bubble-fg)] transition hover:bg-[var(--color-gold-soft)] disabled:opacity-60"
          >
            {loading ? "Connecting…" : "Start chatting"}
          </button>
        </form>
      </div>
    </div>
  );
}
