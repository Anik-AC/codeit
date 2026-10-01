// Each agent's colour and words, used wherever that agent appears.

export type RoleName = "planner" | "coder" | "reviewer" | "rebase" | "docs" | "learning" | "human";

export const ROLES: Record<RoleName, { label: string; color: string; does: string }> = {
  planner: { label: "Planner", color: "var(--planner)", does: "Turns a plan into tickets" },
  coder: { label: "Coder", color: "var(--coder)", does: "Writes the code and tests, opens the PR" },
  reviewer: { label: "Reviewer", color: "var(--reviewer)", does: "Runs the checks and judges the PR" },
  rebase: { label: "Rebaser", color: "var(--rebase)", does: "Keeps open PRs on top of main" },
  docs: { label: "Scribe", color: "var(--docs)", does: "Writes the changelog and work log each morning" },
  learning: { label: "Learner", color: "var(--learning)", does: "Turns review feedback into steering rules, tested by evals" },
  human: { label: "You", color: "var(--human)", does: "Approve plans and merge PRs" },
};

export function roleColor(role: string): string {
  return (ROLES as Record<string, { color: string }>)[role]?.color ?? "var(--muted)";
}
