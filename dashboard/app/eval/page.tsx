"use client";

import { useQuery } from "@tanstack/react-query";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { Suspense } from "react";
import { Badge } from "@/components/ui/badge";
import { Card, CardBody } from "@/components/ui/card";
import { api } from "@/lib/api";
import { isReview, metric, pct } from "@/lib/evals";
import type { EvalRunDetail } from "@/lib/types";
import { usd } from "@/lib/utils";

const CODER = ["pass@1", "pass^k", "hidden_pass_ratio", "reviewer_first_pass"];
const REVIEW = ["critical_catch_rate", "false_fail_rate"];

function Eval() {
  const id = useSearchParams().get("id") ?? "";
  const { data: run, error } = useQuery({
    queryKey: ["eval", id],
    queryFn: () => api.get<EvalRunDetail>(`/api/evals/${id}`),
    enabled: Boolean(id),
    refetchInterval: (q) => (q.state.data && !q.state.data.ended_at ? 15_000 : false),
  });
  if (!id) return <p className="text-muted">No eval selected.</p>;
  if (error) return <p className="text-bad">{String(error)}</p>;
  if (!run) return <p className="text-muted">Loading</p>;
  const review = isReview(run);
  const stopped = typeof run.summary?.stopped === "string" ? run.summary.stopped : null;
  return (
    <>
      <div className="flex flex-wrap items-center gap-3">
        <Link href="/evals/" className="text-sm text-muted hover:text-ink">
          Evals
        </Link>
        <span className="text-muted">/</span>
        <h1 className="font-mono text-xl font-semibold">{run.id}</h1>
        <Badge tone={run.ended_at ? "neutral" : "busy"}>{run.ended_at ? "finished" : "running"}</Badge>
      </div>
      {stopped && <p className="rounded-md border border-line px-3 py-2 text-sm text-warn">Stopped early: {stopped}</p>}
      <Card>
        <CardBody>
          <dl className="grid grid-cols-2 gap-4 sm:grid-cols-4">
            {(review ? REVIEW : CODER).map((name) => (
              <div key={name} className="flex flex-col gap-0.5">
                <dt className="text-xs text-muted">{name.replaceAll("_", " ")}</dt>
                <dd className="tabular font-display text-3xl font-semibold tracking-tight">{pct(metric(run, name))}</dd>
              </div>
            ))}
          </dl>
          <p className="mt-3 text-xs text-muted">
            {run.suite} / {run.config} · model {run.model ?? "default"} · steering {run.steering_sha ?? "–"}
          </p>
        </CardBody>
      </Card>
      <Card className="overflow-x-auto">
        <table className="w-full min-w-[760px] text-sm">
          <thead>
            <tr className="border-b border-line text-left text-xs uppercase tracking-wide text-muted">
              <th className="px-4 py-2 font-medium">{review ? "Task / variant" : "Task"}</th>
              {!review && <th className="px-4 py-2 font-medium">Repeat</th>}
              <th className="px-4 py-2 font-medium">{review ? "Correct" : "Passed"}</th>
              {!review && <th className="px-4 py-2 text-right font-medium">Hidden</th>}
              <th className="px-4 py-2 font-medium">Review verdict</th>
              <th className="px-4 py-2 text-right font-medium">Cost</th>
              <th className="px-4 py-2 font-medium">Notes</th>
            </tr>
          </thead>
          <tbody className="tabular">
            {run.results.map((r) => (
              <tr key={`${r.task_id}-${r.repeat}`} className="border-b border-line align-top last:border-0">
                <td className="px-4 py-2 font-mono text-xs">{r.task_id}</td>
                {!review && <td className="px-4 py-2">{r.repeat + 1}</td>}
                <td className="px-4 py-2">
                  <Badge tone={r.passed ? "ok" : "bad"}>{r.passed ? "yes" : "no"}</Badge>
                </td>
                {!review && <td className="px-4 py-2 text-right">{pct(r.hidden_pass_ratio)}</td>}
                <td className="px-4 py-2">{r.reviewer_verdict ?? "–"}</td>
                <td className="px-4 py-2 text-right">{usd(r.cost_usd)}</td>
                <td className="max-w-md px-4 py-2 text-xs text-muted">
                  {r.notes && (
                    <details>
                      <summary className="cursor-pointer">{String(r.notes.kind ?? r.notes.coder ?? "details")}</summary>
                      <pre className="mt-1 whitespace-pre-wrap font-mono">{JSON.stringify(r.notes, null, 2)}</pre>
                    </details>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </Card>
    </>
  );
}

export default function EvalPage() {
  return (
    <Suspense>
      <Eval />
    </Suspense>
  );
}
