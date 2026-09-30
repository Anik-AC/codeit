import type { EvalRunSummary } from "./types";

export const isReview = (run: EvalRunSummary) => run.suite.endsWith("-review");

/** A summary number, or null. `pass^k` finds whichever k the run used. */
export function metric(run: EvalRunSummary, name: string): number | null {
  const s = run.summary ?? {};
  const key = name === "pass^k" ? Object.keys(s).find((k) => k.startsWith("pass^")) : name;
  const value = key ? s[key] : undefined;
  return typeof value === "number" ? value : null;
}

export function pct(value: number | null): string {
  return value == null ? "–" : `${Math.round(value * 100)}%`;
}
