import { cn } from "@/lib/utils";
import type { ReactNode } from "react";

export type Tone = "neutral" | "busy" | "ok" | "warn" | "bad";

const colors: Record<Tone, string> = {
  neutral: "var(--muted)",
  busy: "var(--coder)",
  ok: "var(--ok)",
  warn: "var(--warn)",
  bad: "var(--bad)",
};

/** A status pill tinted from one colour, so it reads in both themes. */
export function Badge({
  tone = "neutral",
  color,
  children,
  className,
}: {
  tone?: Tone;
  color?: string;
  children: ReactNode;
  className?: string;
}) {
  const c = color ?? colors[tone];
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1.5 whitespace-nowrap rounded-full border px-2 py-0.5 font-mono text-[11px] font-medium",
        className,
      )}
      style={{
        color: c,
        borderColor: `color-mix(in srgb, ${c} 35%, transparent)`,
        background: `color-mix(in srgb, ${c} 12%, transparent)`,
      }}
    >
      {children}
    </span>
  );
}

/** Colour for a run status or verdict, as stored in the `runs` table. */
export function runTone(status: string): Tone {
  if (status === "running") return "busy";
  if (["pr_opened", "pr_updated", "committed", "pass", "pass_with_notes", "skipped"].includes(status)) return "ok";
  if (["fail_critical", "failed", "error", "abandoned", "timeout"].includes(status)) return "bad";
  if (["blocked", "incomplete", "interrupted", "usage_limited", "max_turns"].includes(status)) return "warn";
  return "neutral";
}
