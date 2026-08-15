"use client";

import ReactMarkdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";

const components: Components = {
  p: ({ children }) => <p className="mb-2 last:mb-0">{children}</p>,
  strong: ({ children }) => (
    <strong className="font-semibold text-[var(--color-gold-soft)]">{children}</strong>
  ),
  em: ({ children }) => <em className="italic">{children}</em>,
  ul: ({ children }) => (
    <ul className="mb-2 ml-4 list-disc space-y-1 last:mb-0">{children}</ul>
  ),
  ol: ({ children }) => (
    <ol className="mb-2 ml-4 list-decimal space-y-1 last:mb-0">{children}</ol>
  ),
  li: ({ children }) => <li className="pl-1">{children}</li>,
  a: ({ children, href }) => (
    <a
      href={href}
      target="_blank"
      rel="noreferrer"
      className="text-[var(--color-gold)] underline underline-offset-2 hover:text-[var(--color-gold-soft)]"
    >
      {children}
    </a>
  ),
  code: ({ children }) => (
    <code className="rounded bg-black/30 px-1.5 py-0.5 font-mono text-[0.85em]">
      {children}
    </code>
  ),
  pre: ({ children }) => (
    <pre className="mb-2 overflow-x-auto rounded-lg bg-black/30 p-3 font-mono text-[0.85em] last:mb-0">
      {children}
    </pre>
  ),
  h1: ({ children }) => <p className="mb-1 font-display text-base font-semibold">{children}</p>,
  h2: ({ children }) => <p className="mb-1 font-display text-base font-semibold">{children}</p>,
  h3: ({ children }) => <p className="mb-1 font-semibold">{children}</p>,
  blockquote: ({ children }) => (
    <blockquote className="mb-2 border-l-2 border-[var(--color-gold)] pl-3 text-[var(--color-fg-muted)] last:mb-0">
      {children}
    </blockquote>
  ),
  hr: () => <hr className="my-2 border-[var(--color-border)]" />,
  table: ({ children }) => (
    <div className="mb-2 overflow-x-auto last:mb-0">
      <table className="w-full border-collapse text-left">{children}</table>
    </div>
  ),
  th: ({ children }) => (
    <th className="border-b border-[var(--color-border)] px-2 py-1 font-semibold">{children}</th>
  ),
  td: ({ children }) => (
    <td className="border-b border-[var(--color-border)]/50 px-2 py-1">{children}</td>
  ),
};

export default function Markdown({ content }: { content: string }) {
  return (
    <div className="text-sm leading-relaxed [&_p]:m-0">
      <ReactMarkdown remarkPlugins={[remarkGfm]} components={components}>
        {content}
      </ReactMarkdown>
    </div>
  );
}
