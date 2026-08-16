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
    <div className="flex flex-1 items-center justify-center px-4 py-10">
      <div className="w-full max-w-[26rem]">
        <div className="mb-8 text-center">
          <p className="mb-2 text-[10px] tracking-[0.28em] text-[var(--color-gold)] uppercase">
            DP Jewellers
          </p>
          <h1 className="font-display text-[2rem] leading-tight tracking-tight">
            {name ? `Welcome, ${name.split(" ")[0]}` : "Welcome"}
          </h1>
          <p className="mt-2.5 text-sm text-[var(--color-fg-muted)]">
            You have staff access. What are you here to do?
          </p>
        </div>

        <div className="flex flex-col gap-3">
          <Choice
            onClick={() => onChoose("admin")}
            title="Staff console"
            body="Sales and inventory reporting, customer and order lookups across the whole business, and operational changes."
            accent
          />
          <Choice
            onClick={() => onChoose("customer")}
            title="Shop as myself"
            body="Your own orders and purchases, exactly as any customer sees them."
          />
        </div>

        <p className="mt-5 text-center text-xs text-[var(--color-fg-subtle)]">
          This choice lasts for the conversation — switching means starting a new one.
        </p>
      </div>
    </div>
  );
}

function Choice({
  onClick,
  title,
  body,
  accent = false,
}: {
  onClick: () => void;
  title: string;
  body: string;
  accent?: boolean;
}) {
  return (
    <button
      onClick={onClick}
      className={[
        "group rounded-[18px] border bg-[var(--color-bg-elevated)] p-5 text-left transition",
        "hover:-translate-y-0.5 hover:shadow-[var(--shadow-lift)]",
        accent
          ? "border-[var(--color-gold)]/50 hover:border-[var(--color-gold)]"
          : "border-[var(--color-border)] hover:border-[var(--color-border-strong)]",
      ].join(" ")}
    >
      <span className="flex items-center justify-between gap-3">
        <span
          className={`font-display text-base ${accent ? "text-[var(--color-gold)]" : "text-[var(--color-fg)]"}`}
        >
          {title}
        </span>
        <span
          aria-hidden
          className="text-[var(--color-fg-subtle)] transition group-hover:translate-x-0.5 group-hover:text-[var(--color-fg-muted)]"
        >
          →
        </span>
      </span>
      <span className="mt-1.5 block text-xs leading-relaxed text-[var(--color-fg-muted)]">
        {body}
      </span>
    </button>
  );
}
