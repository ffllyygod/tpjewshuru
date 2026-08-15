"use client";

import type { Mode } from "@/lib/api";

type Props = {
  name: string | null;
  onChoose: (mode: Mode) => void;
};

/**
 * Shown only to accounts the server resolved to `role: "admin"`.
 *
 * Staff have two genuinely different jobs — running the business and being a
 * customer of it — and the tools for each are disjoint. Making that an explicit
 * choice at the start, rather than something the model infers from wording, is
 * what stops "cancel my order" being ambiguous between `cancel_order` (theirs)
 * and `admin_cancel_order` (anyone's). The choice is fixed for the life of the
 * conversation; switching means starting a new one.
 */
export default function ModeChooser({ name, onChoose }: Props) {
  return (
    <div className="flex flex-1 items-center justify-center px-4">
      <div className="w-full max-w-sm">
        <div className="mb-8 text-center">
          <p className="mb-2 text-xs tracking-[0.3em] text-[var(--color-gold)] uppercase">
            TP Jewellers
          </p>
          <h1 className="font-display text-3xl text-[var(--color-fg)]">
            {name ? `Welcome, ${name.split(" ")[0]}` : "Welcome"}
          </h1>
          <p className="mt-2 text-sm text-[var(--color-fg-muted)]">
            You have staff access. What are you here to do?
          </p>
        </div>

        <div className="flex flex-col gap-3">
          <button
            onClick={() => onChoose("admin")}
            className="rounded-xl border border-[var(--color-gold)] bg-[var(--color-bg-elevated)] p-5 text-left transition hover:bg-[var(--color-gold)]/10"
          >
            <span className="block text-sm font-medium text-[var(--color-gold)]">
              Staff console
            </span>
            <span className="mt-1 block text-xs text-[var(--color-fg-muted)]">
              Sales and inventory reporting, customer and order lookups across the whole
              business, and operational changes.
            </span>
          </button>

          <button
            onClick={() => onChoose("customer")}
            className="rounded-xl border border-[var(--color-border)] bg-[var(--color-bg-elevated)] p-5 text-left transition hover:border-[var(--color-gold)]"
          >
            <span className="block text-sm font-medium text-[var(--color-fg)]">
              Shop as myself
            </span>
            <span className="mt-1 block text-xs text-[var(--color-fg-muted)]">
              Your own orders and purchases, exactly as any customer sees them.
            </span>
          </button>
        </div>
      </div>
    </div>
  );
}
