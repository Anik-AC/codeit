import { cn } from "@/lib/utils";
import type { ReactNode } from "react";

export type Tone = "neutral" | "busy" | "ok" | "warn" | "bad";

const tones: Record<Tone, string> = {
  neutral: "bg-code text-muted",
  busy: "bg-busy-soft text-busy",
  ok: "bg-ok-soft text-ok",
  warn: "bg-warn-soft text-warn",
  bad: "bg-bad-soft text-bad",
};

export function Badge({ tone = "neutral", children, className }: { tone?: Tone; children: ReactNode; className?: string }) {
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1 whitespace-nowrap rounded-full px-2 py-0.5 font-mono text-xs",
        tones[tone],
        className,
      )}
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
