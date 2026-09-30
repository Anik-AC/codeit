"use client";

import { useQuery } from "@tanstack/react-query";
import Link from "next/link";
import { Badge } from "@/components/ui/badge";
import { Card, CardBody, CardHeader, CardTitle } from "@/components/ui/card";
import { api } from "@/lib/api";
import { isReview, metric, pct } from "@/lib/evals";
import type { EvalRunSummary } from "@/lib/types";

// pass@1 of each finished Coder eval over time; points share a colour per steering_sha.
function PassChart({ runs }: { runs: EvalRunSummary[] }) {
  const points = runs
    .filter((r) => !isReview(r) && r.ended_at && metric(r, "pass@1") != null)
    .slice()
    .reverse();
  if (points.length === 0) {
    return <p className="text-sm text-muted">No finished Coder evals yet.</p>;
  }
  const shas = [...new Set(points.map((p) => p.steering_sha ?? "none"))];
  const palette = ["var(--accent)", "var(--ok)", "var(--warn)", "var(--bad)", "var(--muted)"];
  const W = 640;
  const H = 200;
  const pad = { l: 40, r: 16, t: 12, b: 28 };
  const x = (i: number) => pad.l + (points.length === 1 ? (W - pad.l - pad.r) / 2 : (i * (W - pad.l - pad.r)) / (points.length - 1));
  const y = (v: number) => pad.t + (1 - v) * (H - pad.t - pad.b);
  const line = points.map((p, i) => `${i === 0 ? "M" : "L"}${x(i)},${y(metric(p, "pass@1") ?? 0)}`).join(" ");
  return (
    <div className="flex flex-col gap-3">
      <div className="overflow-x-auto">
        <svg viewBox={`0 0 ${W} ${H}`} className="w-full min-w-[480px]" role="img" aria-label="pass@1 over time">
          {[0, 0.25, 0.5, 0.75, 1].map((v) => (
            <g key={v}>
              <line x1={pad.l} x2={W - pad.r} y1={y(v)} y2={y(v)} stroke="var(--line)" strokeWidth={1} />
              <text x={pad.l - 8} y={y(v) + 4} textAnchor="end" fontSize={11} fill="var(--muted)">
                {Math.round(v * 100)}%
              </text>
            </g>
          ))}
          <path d={line} fill="none" stroke="var(--line)" strokeWidth={1.5} />
          {points.map((p, i) => (
            <circle
              key={p.id}
              cx={x(i)}
              cy={y(metric(p, "pass@1") ?? 0)}
              r={5}
              fill={palette[shas.indexOf(p.steering_sha ?? "none") % palette.length]}
            >
              <title>{`${p.config} · ${pct(metric(p, "pass@1"))} · ${p.steering_sha ?? ""}`}</title>
            </circle>
          ))}
          {points.map((p, i) => (
            <text key={`d${p.id}`} x={x(i)} y={H - 8} textAnchor="middle" fontSize={10} fill="var(--muted)">
              {p.started_at ? new Date(p.started_at).toLocaleDateString(undefined, { month: "short", day: "numeric" }) : ""}
            </text>
          ))}
        </svg>
      </div>
      <div className="flex flex-wrap gap-3 text-xs text-muted">
        {shas.map((sha, i) => (
          <span key={sha} className="flex items-center gap-1.5">
            <span className="size-2.5 rounded-full" style={{ background: palette[i % palette.length] }} />
            steering <code className="font-mono">{sha}</code>
          </span>
        ))}
      </div>
    </div>
  );
}

export default function EvalsPage() {
  const { data, error } = useQuery({ queryKey: ["evals"], queryFn: () => api.get<EvalRunSummary[]>("/api/evals") });
  const runs = data ?? [];
  return (
    <>
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h1 className="text-xl font-semibold">Evals</h1>
        <p className="text-muted">
          Run with <code className="font-mono text-xs">codeit eval run</code> and{" "}
          <code className="font-mono text-xs">codeit eval review</code>.
        </p>
      </div>
      {error && <p className="text-bad">{String(error)}</p>}
      <Card>
        <CardHeader>
          <CardTitle>Coder pass@1 over time</CardTitle>
        </CardHeader>
        <CardBody>
          <PassChart runs={runs} />
        </CardBody>
      </Card>
      <Card className="overflow-x-auto">
        <table className="w-full min-w-[760px] text-sm">
          <thead>
            <tr className="border-b border-line text-left text-xs uppercase tracking-wide text-muted">
              <th className="px-4 py-2 font-medium">Run</th>
              <th className="px-4 py-2 font-medium">Suite / config</th>
              <th className="px-4 py-2 font-medium">Model</th>
              <th className="px-4 py-2 font-medium">Steering</th>
              <th className="px-4 py-2 text-right font-medium">Result</th>
              <th className="px-4 py-2 font-medium">Started</th>
            </tr>
          </thead>
          <tbody className="tabular">
            {runs.map((r) => (
              <tr key={r.id} className="border-b border-line last:border-0 hover:bg-accent-soft/40">
                <td className="px-4 py-2 font-mono text-xs">
                  <Link className="text-accent hover:underline" href={`/eval/?id=${r.id}`}>
                    {r.id.slice(-8)}
                  </Link>
                  {!r.ended_at && <Badge tone="busy" className="ml-2">running</Badge>}
                </td>
                <td className="px-4 py-2">
                  {r.suite} / {r.config}
                </td>
                <td className="px-4 py-2 text-muted">{r.model ?? "default"}</td>
                <td className="px-4 py-2 font-mono text-xs text-muted">{r.steering_sha ?? "–"}</td>
                <td className="px-4 py-2 text-right">
                  {isReview(r)
                    ? `caught ${pct(metric(r, "critical_catch_rate"))} · false fails ${pct(metric(r, "false_fail_rate"))}`
                    : `pass@1 ${pct(metric(r, "pass@1"))} · pass^k ${pct(metric(r, "pass^k"))}`}
                </td>
                <td className="px-4 py-2 text-muted">{r.started_at && new Date(r.started_at).toLocaleString()}</td>
              </tr>
            ))}
            {data?.length === 0 && (
              <tr>
                <td colSpan={6} className="px-4 py-6 text-center text-muted">
                  No eval runs yet.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </Card>
    </>
  );
}
