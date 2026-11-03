import type { ReactNode } from "react";
import { Loader2 } from "lucide-react";

export function Card({ title, actions, children, className = "" }: {
  title?: string;
  actions?: ReactNode;
  children: ReactNode;
  className?: string;
}) {
  return (
    <section className={`panel ${className}`}>
      {(title || actions) && (
        <header className="flex items-center justify-between gap-2 border-b border-slate-200
          px-4 py-2.5 dark:border-ink-700">
          <h2 className="text-sm font-semibold">{title}</h2>
          {actions}
        </header>
      )}
      <div className="p-4">{children}</div>
    </section>
  );
}

export function Badge({ tone = "neutral", children }: {
  tone?: "ok" | "warn" | "danger" | "accent" | "neutral";
  children: ReactNode;
}) {
  const tones: Record<string, string> = {
    ok: "bg-emerald-500/12 text-emerald-600 dark:text-emerald-400",
    warn: "bg-amber-500/12 text-amber-600 dark:text-amber-400",
    danger: "bg-red-500/12 text-red-600 dark:text-red-400",
    accent: "bg-accent-soft text-accent",
    neutral: "bg-slate-500/12 text-slate-600 dark:text-slate-300",
  };
  return (
    <span className={`inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-xs
      font-medium ${tones[tone]}`}>
      {children}
    </span>
  );
}

export function Spinner({ label }: { label?: string }) {
  return (
    <div className="flex items-center justify-center gap-2 py-10 text-sm text-slate-500">
      <Loader2 className="h-4 w-4 animate-spin" aria-hidden />
      {label}
    </div>
  );
}

export function Skeleton({ className = "" }: { className?: string }) {
  return (
    <div
      className={`animate-pulse rounded-lg bg-slate-200 dark:bg-ink-800 ${className}`}
      aria-hidden
    />
  );
}

export function ErrorState({ message, onRetry }: { message: string; onRetry?: () => void }) {
  return (
    <div className="py-10 text-center">
      <p className="text-sm text-danger">{message}</p>
      {onRetry && (
        <button
          type="button"
          onClick={onRetry}
          className="mt-3 text-xs font-medium text-accent hover:underline"
        >
          retry
        </button>
      )}
    </div>
  );
}

export function EmptyState({ title, hint }: { title: string; hint?: string }) {
  return (
    <div className="py-12 text-center">
      <p className="text-sm font-medium text-slate-600 dark:text-slate-300">{title}</p>
      {hint && <p className="mt-1 text-xs text-slate-400">{hint}</p>}
    </div>
  );
}
