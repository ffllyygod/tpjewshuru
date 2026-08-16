"use client";

import { useRef, useState, type ReactNode } from "react";
import ReactMarkdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";

/** Flatten a cell's children back to text so we can decide how to align it. */
function textOf(node: ReactNode): string {
  if (node === null || node === undefined || typeof node === "boolean") return "";
  if (typeof node === "string" || typeof node === "number") return String(node);
  if (Array.isArray(node)) return node.map(textOf).join("");
  if (typeof node === "object" && "props" in node) {
    return textOf((node as { props: { children?: ReactNode } }).props.children);
  }
  return "";
}

/**
 * Money and quantities go right, words go left.
 *
 * The backend renders the whole tax invoice as a GFM table, so this component
 * is what the customer actually reads a bill in. Amounts left-aligned in a
 * ragged column is the difference between something that looks like a receipt
 * and something that looks like a chat message about a receipt.
 */
function isNumeric(text: string): boolean {
  const t = text.trim();
  if (!t) return false;
  return /^[-+₹(]?\s*[\d,.\s]+(%|g|\)|\s*\/g)?$/.test(t) || /^₹/.test(t);
}

/** The bold "Total payable" row the invoice ends on. */
function isEmphasisedRow(children: ReactNode): boolean {
  const first = Array.isArray(children) ? children[0] : children;
  if (typeof first === "object" && first && "props" in first) {
    const cells = (first as { props: { children?: ReactNode } }).props.children;
    const firstCell = Array.isArray(cells) ? cells[0] : cells;
    if (typeof firstCell === "object" && firstCell && "type" in firstCell) {
      return (firstCell as { type?: unknown }).type === "strong";
    }
  }
  return false;
}

function Cell({ children, header }: { children: ReactNode; header?: boolean }) {
  const numeric = isNumeric(textOf(children));
  const Tag = header ? "th" : "td";
  return (
    <Tag
      className={[
        "px-3 py-2 align-top",
        header
          ? "border-b border-[var(--color-border-strong)] text-[11px] font-semibold uppercase tracking-wider text-[var(--color-fg-muted)]"
          : "border-b border-[var(--color-border)] text-[var(--color-fg)]",
        numeric ? "text-right whitespace-nowrap tabular-nums" : "text-left",
      ].join(" ")}
    >
      {children}
    </Tag>
  );
}

/**
 * Tables render as a document card rather than bare rows, with a copy button —
 * people screenshot or forward invoices, and "select the exact rows in a chat
 * bubble" is a miserable way to do it.
 */
function TableCard({ children }: { children: ReactNode }) {
  const ref = useRef<HTMLDivElement>(null);
  const [copied, setCopied] = useState(false);

  async function copy() {
    const text = ref.current?.innerText;
    if (!text) return;
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      setTimeout(() => setCopied(false), 1600);
    } catch {
      // Clipboard is permission-gated and can simply refuse. Staying silent is
      // right: the table is still on screen and selectable.
    }
  }

  return (
    <div className="group relative my-3 first:mt-0 last:mb-0">
      <button
        type="button"
        onClick={copy}
        aria-label={copied ? "Copied" : "Copy table"}
        className="absolute -top-2 right-2 z-10 rounded-full border border-[var(--color-border)] bg-[var(--color-bg-elevated)] px-2.5 py-1 text-[11px] text-[var(--color-fg-muted)] opacity-0 shadow-[var(--shadow-soft)] transition focus-visible:opacity-100 group-hover:opacity-100 hover:text-[var(--color-fg)]"
      >
        {copied ? "Copied" : "Copy"}
      </button>
      <div
        ref={ref}
        className="overflow-x-auto rounded-[var(--radius-card)] border border-[var(--color-border)] bg-[var(--color-bg-elevated)]"
      >
        <table className="w-full min-w-full border-collapse text-[13px]">{children}</table>
      </div>
    </div>
  );
}

const components: Components = {
  p: ({ children }) => <p className="mb-2.5 last:mb-0">{children}</p>,
  strong: ({ children }) => (
    <strong className="font-semibold text-[var(--color-fg)]">{children}</strong>
  ),
  em: ({ children }) => <em className="text-[var(--color-fg-muted)] italic">{children}</em>,
  ul: ({ children }) => (
    <ul className="mb-2.5 ml-1 list-none space-y-1.5 last:mb-0">{children}</ul>
  ),
  ol: ({ children }) => (
    <ol className="mb-2.5 ml-4 list-decimal space-y-1.5 last:mb-0 marker:text-[var(--color-fg-subtle)]">
      {children}
    </ol>
  ),
  li: ({ children, ...rest }) => {
    // Unordered items get a gold hairline bullet instead of a disc; it reads as
    // a considered list of recommendations rather than a dumped array.
    const ordered = "value" in rest;
    return ordered ? (
      <li className="pl-1">{children}</li>
    ) : (
      <li className="relative pl-4 before:absolute before:top-[0.6em] before:left-0 before:h-px before:w-2 before:bg-[var(--color-gold)]">
        {children}
      </li>
    );
  },
  a: ({ children, href }) => (
    <a
      href={href}
      target="_blank"
      rel="noreferrer"
      className="text-[var(--color-gold)] underline decoration-[var(--color-gold)]/40 underline-offset-2 hover:decoration-[var(--color-gold)]"
    >
      {children}
    </a>
  ),
  code: ({ children }) => (
    <code className="rounded bg-[var(--color-surface)] px-1.5 py-0.5 font-mono text-[0.85em] text-[var(--color-fg)]">
      {children}
    </code>
  ),
  pre: ({ children }) => (
    <pre className="mb-2.5 overflow-x-auto rounded-[var(--radius-card)] border border-[var(--color-border)] bg-[var(--color-surface)] p-3 font-mono text-[0.85em] last:mb-0">
      {children}
    </pre>
  ),
  // h1/h2 stay as paragraphs — a chat reply shouldn't open a document outline —
  // but they keep the display face so a heading still reads as one.
  h1: ({ children }) => (
    <p className="font-display mb-1.5 text-[15px] font-semibold tracking-tight">{children}</p>
  ),
  h2: ({ children }) => (
    <p className="font-display mb-1.5 text-[15px] font-semibold tracking-tight">{children}</p>
  ),
  h3: ({ children }) => <p className="mb-1 font-semibold">{children}</p>,
  blockquote: ({ children }) => (
    <blockquote className="mb-2.5 border-l-2 border-[var(--color-gold)] pl-3 text-[var(--color-fg-muted)] last:mb-0">
      {children}
    </blockquote>
  ),
  hr: () => <hr className="my-3 border-[var(--color-border)]" />,
  table: ({ children }) => <TableCard>{children}</TableCard>,
  thead: ({ children }) => <thead className="bg-[var(--color-surface)]">{children}</thead>,
  tr: ({ children }) => (
    <tr
      className={
        isEmphasisedRow(children)
          ? "bg-[var(--color-gold-wash)] font-semibold [&>td]:border-b-0 [&>td]:py-2.5"
          : "last:[&>td]:border-b-0"
      }
    >
      {children}
    </tr>
  ),
  th: ({ children }) => <Cell header>{children}</Cell>,
  td: ({ children }) => <Cell>{children}</Cell>,
};

export default function Markdown({ content }: { content: string }) {
  return (
    <div className="text-[14.5px] leading-[1.65] [&_p]:m-0">
      <ReactMarkdown remarkPlugins={[remarkGfm]} components={components}>
        {content}
      </ReactMarkdown>
    </div>
  );
}
