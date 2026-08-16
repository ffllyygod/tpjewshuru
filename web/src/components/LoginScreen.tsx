"use client";

import { useState, type FormEvent } from "react";
import { ApiError, requestOtp, verifyOtp } from "@/lib/api";

type Props = {
  onLoggedIn: () => void;
  onBrowseAnonymously: () => void;
};

type Step =
  | { name: "email" }
  | { name: "code"; email: string; deviceId: string; preAuthSessionId: string };

const FIELD =
  "w-full rounded-xl border border-[var(--color-border)] bg-[var(--color-bg)] px-3.5 py-3 text-base text-[var(--color-fg)] outline-none transition placeholder:text-[var(--color-fg-subtle)] focus:border-[var(--color-gold)] sm:text-[15px]";

const PRIMARY =
  "w-full rounded-xl bg-[var(--color-gold)] py-3 text-sm font-medium text-[var(--color-user-bubble-fg)] transition hover:bg-[var(--color-gold-soft)] disabled:cursor-not-allowed disabled:opacity-50";

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
      setError("Couldn't reach the store. Please try again in a moment.");
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
      const ok = await verifyOtp({
        deviceId: step.deviceId,
        preAuthSessionId: step.preAuthSessionId,
        code: code.trim(),
      });
      if (ok) {
        onLoggedIn();
      } else {
        setError("That code didn't work — check it and try again, or resend.");
      }
    } catch (err) {
      if (err instanceof ApiError) {
        setError("That code didn't work — check it and try again, or resend.");
      } else {
        setError("Couldn't reach the store. Please try again in a moment.");
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
      setError("Couldn't reach the store. Please try again in a moment.");
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="flex flex-1 items-center justify-center px-4 py-10">
      <div className="w-full max-w-[26rem]">
        <div className="mb-8 text-center">
          <span
            aria-hidden
            className="font-display mx-auto mb-5 grid h-14 w-14 place-items-center rounded-full border border-[var(--color-gold)]/40 bg-[var(--color-gold-wash)] text-lg text-[var(--color-gold)]"
          >
            DP
          </span>
          <p className="mb-2 text-[10px] tracking-[0.28em] text-[var(--color-gold)] uppercase">
            DP Jewellers
          </p>
          <h1 className="font-display text-[2rem] leading-tight tracking-tight">Concierge</h1>
          <p className="mx-auto mt-2.5 max-w-[22rem] text-sm text-[var(--color-fg-muted)]">
            Find a piece, follow an order, or design something of your own.
          </p>
        </div>

        <div className="rounded-[18px] border border-[var(--color-border)] bg-[var(--color-bg-elevated)] p-6 shadow-[var(--shadow-lift)]">
          {step.name === "email" ? (
            <form onSubmit={submitEmail} noValidate>
              <label
                htmlFor="email"
                className="mb-2 block text-[11px] font-medium tracking-wider text-[var(--color-fg-muted)] uppercase"
              >
                Email
              </label>
              <input
                id="email"
                type="email"
                required
                autoComplete="email"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                placeholder="you@example.com"
                className={FIELD}
              />
              <p className="mt-2 text-xs text-[var(--color-fg-muted)]">
                We&apos;ll email you a one-time code — no password needed.
              </p>

              {error && <Alert>{error}</Alert>}

              <button type="submit" disabled={loading} className={`mt-5 ${PRIMARY}`}>
                {loading ? "Sending…" : "Send code"}
              </button>

              <button
                type="button"
                onClick={onBrowseAnonymously}
                className="mt-2.5 w-full rounded-xl border border-[var(--color-border)] py-3 text-sm text-[var(--color-fg-muted)] transition hover:border-[var(--color-border-strong)] hover:text-[var(--color-fg)]"
              >
                Browse without signing in
              </button>
            </form>
          ) : (
            <form onSubmit={submitCode} noValidate>
              <label
                htmlFor="code"
                className="mb-2 block text-[11px] font-medium tracking-wider text-[var(--color-fg-muted)] uppercase"
              >
                Enter the code
              </label>
              <p className="mb-3 text-xs text-[var(--color-fg-muted)]">
                Sent to <span className="text-[var(--color-fg)]">{step.email}</span>
              </p>
              <input
                id="code"
                type="text"
                inputMode="numeric"
                // One-tap autofill from the OS on iOS and Android.
                autoComplete="one-time-code"
                autoFocus
                required
                maxLength={6}
                value={code}
                onChange={(e) => setCode(e.target.value.replace(/\D/g, ""))}
                placeholder="······"
                className={`${FIELD} text-center text-2xl tracking-[0.45em]`}
              />

              {error && <Alert>{error}</Alert>}

              <button
                type="submit"
                disabled={loading || code.length < 4}
                className={`mt-5 ${PRIMARY}`}
              >
                {loading ? "Verifying…" : "Verify"}
              </button>

              <div className="mt-3 flex justify-between text-xs text-[var(--color-fg-muted)]">
                <button
                  type="button"
                  onClick={() => setStep({ name: "email" })}
                  className="transition hover:text-[var(--color-fg)]"
                >
                  Change email
                </button>
                <button
                  type="button"
                  onClick={resend}
                  disabled={loading}
                  className="transition hover:text-[var(--color-fg)] disabled:opacity-50"
                >
                  Resend code
                </button>
              </div>
            </form>
          )}
        </div>
      </div>
    </div>
  );
}

function Alert({ children }: { children: React.ReactNode }) {
  return (
    <p
      role="alert"
      className="mt-3 rounded-lg border border-[var(--color-danger)]/30 bg-[var(--color-danger-wash)] px-3 py-2 text-xs text-[var(--color-danger)]"
    >
      {children}
    </p>
  );
}
