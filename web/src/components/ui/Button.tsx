import { Loader2 } from "lucide-react";
import type { ButtonHTMLAttributes, ReactNode } from "react";

type Variant = "primary" | "secondary" | "danger" | "ghost";
type Size = "sm" | "md";

interface Props extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: Variant;
  size?: Size;
  loading?: boolean;
  children: ReactNode;
}

const styles: Record<Variant, string> = {
  primary:
    "bg-accent text-white hover:bg-blue-600 focus-visible:ring-accent disabled:bg-accent/50",
  secondary:
    "border border-slate-300 bg-white text-slate-700 hover:bg-slate-100 dark:border-ink-600 dark:bg-ink-850 dark:text-slate-200 dark:hover:bg-ink-800 focus-visible:ring-accent",
  danger:
    "bg-danger text-white hover:bg-red-600 focus-visible:ring-danger disabled:bg-danger/50",
  ghost:
    "text-slate-600 hover:bg-slate-100 dark:text-slate-300 dark:hover:bg-ink-800 focus-visible:ring-slate-400",
};

export function Button({
  variant = "primary",
  size = "md",
  loading = false,
  disabled,
  className = "",
  children,
  ...rest
}: Props) {
  const pad = size === "sm" ? "px-2.5 py-1.5 text-xs" : "px-4 py-2 text-sm";
  return (
    <button
      className={`inline-flex items-center justify-center gap-1.5 rounded-lg font-medium
        transition-colors focus:outline-none focus-visible:ring-2 disabled:cursor-not-allowed
        disabled:opacity-60 ${pad} ${styles[variant]} ${className}`}
      disabled={disabled || loading}
      {...rest}
    >
      {loading && <Loader2 className="h-4 w-4 animate-spin" aria-hidden />}
      {children}
    </button>
  );
}
