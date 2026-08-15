"use client";

import { useState, FormEvent } from "react";
import { ApiError, requestOtp, verifyOtp } from "@/lib/api";

type Props = {
  onLoggedIn: () => void;
  onBrowseAnonymously: () => void;
};

type Step =
  | { name: "email" }
  | { name: "code"; email: string; deviceId: string; preAuthSessionId: string };

export default function LoginScreen({ onLoggedIn, onBrowseAnonymously }: Props) {
  const [step, setStep] = useState<Step>({ name: "email" });
  const [email, setEmail] = useState("arun@shurutech.com");
  const [code, setCode] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submitEmail(e: FormEvent) {
    e.preventDefault();
    setLoading(true);
    setError(null);
    try {
      const { deviceId, preAuthSessionId } = await requestOtp(email.trim());
      // Always advances to the code step, whether or not this email belongs
      // to a real customer — the API deliberately doesn't reveal that (see
      // src/api/auth.py), so the UI can't either.
      setStep({ name: "code", email: email.trim(), deviceId, preAuthSessionId });
    } catch {
      setError("Couldn't reach the API. Is it running on localhost:8000?");
    } finally {
      setLoading(false);
    }
  }

  async function submitCode(e: FormEvent) {
    e.preventDefault();
    if (step.name !== "code") return;
    setLoading(true);
    setError(null);
    try {
      const ok = await verifyOtp({ deviceId: step.deviceId, preAuthSessionId: step.preAuthSessionId, code: code.trim() });
      if (ok) {
        onLoggedIn();
      } else {
        setError("That code didn't work — check it and try again, or resend.");
      }
    } catch (err) {
      if (err instanceof ApiError) {
        setError("That code didn't work — check it and try again, or resend.");
      } else {
        setError("Couldn't reach the API. Is it running on localhost:8000?");
      }
    } finally {
      setLoading(false);
    }
  }

  async function resend() {
    if (step.name !== "code") return;
    setLoading(true);
    setError(null);
    try {
      const { deviceId, preAuthSessionId } = await requestOtp(step.email);
      setStep({ name: "code", email: step.email, deviceId, preAuthSessionId });
      setCode("");
    } catch {
      setError("Couldn't reach the API. Is it running on localhost:8000?");
    } finally {
      setLoading(false);
    }
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

        {step.name === "email" ? (
          <form
            onSubmit={submitEmail}
            className="rounded-xl border border-[var(--color-border)] bg-[var(--color-bg-elevated)] p-6 shadow-xl"
          >
            <label className="mb-2 block text-xs uppercase tracking-wide text-[var(--color-fg-muted)]">
              Email
            </label>
            <input
              type="email"
              required
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              placeholder="you@example.com"
              className="w-full rounded-lg border border-[var(--color-border)] bg-[var(--color-bg)] px-3 py-2.5 text-sm text-[var(--color-fg)] outline-none focus:border-[var(--color-gold)]"
            />
            <p className="mt-2 text-xs text-[var(--color-fg-muted)]">
              We&apos;ll email you a one-time code — no password needed.
            </p>

            {error && <p className="mt-3 text-xs text-red-400">{error}</p>}

            <button
              type="submit"
              disabled={loading}
              className="mt-5 w-full rounded-lg bg-[var(--color-gold)] py-2.5 text-sm font-medium text-[var(--color-user-bubble-fg)] transition hover:bg-[var(--color-gold-soft)] disabled:opacity-60"
            >
              {loading ? "Sending…" : "Send code"}
            </button>

            <button
              type="button"
              onClick={onBrowseAnonymously}
              className="mt-3 w-full rounded-lg border border-[var(--color-border)] py-2.5 text-sm text-[var(--color-fg-muted)] transition hover:text-[var(--color-fg)]"
            >
              Browse without logging in
            </button>
          </form>
        ) : (
          <form
            onSubmit={submitCode}
            className="rounded-xl border border-[var(--color-border)] bg-[var(--color-bg-elevated)] p-6 shadow-xl"
          >
            <label className="mb-2 block text-xs uppercase tracking-wide text-[var(--color-fg-muted)]">
              Code sent to {step.email}
            </label>
            <input
              type="text"
              inputMode="numeric"
              autoFocus
              required
              value={code}
              onChange={(e) => setCode(e.target.value)}
              placeholder="6-digit code"
              className="w-full rounded-lg border border-[var(--color-border)] bg-[var(--color-bg)] px-3 py-2.5 text-center text-lg tracking-[0.3em] text-[var(--color-fg)] outline-none focus:border-[var(--color-gold)]"
            />

            {error && <p className="mt-3 text-xs text-red-400">{error}</p>}

            <button
              type="submit"
              disabled={loading}
              className="mt-5 w-full rounded-lg bg-[var(--color-gold)] py-2.5 text-sm font-medium text-[var(--color-user-bubble-fg)] transition hover:bg-[var(--color-gold-soft)] disabled:opacity-60"
            >
              {loading ? "Verifying…" : "Verify"}
            </button>

            <div className="mt-3 flex justify-between text-xs text-[var(--color-fg-muted)]">
              <button
                type="button"
                onClick={() => setStep({ name: "email" })}
                className="hover:text-[var(--color-fg)]"
              >
                Change email
              </button>
              <button type="button" onClick={resend} disabled={loading} className="hover:text-[var(--color-fg)]">
                Resend code
              </button>
            </div>
          </form>
        )}
      </div>
    </div>
  );
}
