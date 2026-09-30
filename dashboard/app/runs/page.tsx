"use client";

import { useQuery } from "@tanstack/react-query";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense } from "react";
import { Badge, runTone } from "@/components/ui/badge";
import { Card } from "@/components/ui/card";
import { Input, Select } from "@/components/ui/input";
import { useNow } from "@/components/use-now";
import { api } from "@/lib/api";
import type { RunSummary } from "@/lib/types";
import { ago, duration, usd } from "@/lib/utils";

function Runs() {
  const params = useSearchParams();
  const router = useRouter();
  const role = params.get("role") ?? "";
  const ticket = params.get("ticket") ?? "";
  const now = useNow(5_000);
  const query = new URLSearchParams({ limit: "100", ...(role && { role }), ...(ticket && { ticket }) });
  const { data, error } = useQuery({
    queryKey: ["runs", role, ticket],
    queryFn: () => api.get<RunSummary[]>(`/api/runs?${query.toString()}`),
  });

  function setFilter(name: string, value: string) {
    const next = new URLSearchParams(params.toString());
    if (value) next.set(name, value);
    else next.delete(name);
    router.replace(`/runs/?${next.toString()}`);
  }

  return (
    <>
      <div className="flex flex-wrap items-end justify-between gap-3">
        <h1 className="text-xl font-semibold">Runs</h1>
        <div className="flex flex-wrap gap-2">
          <Select aria-label="Role" value={role} onChange={(e) => setFilter("role", e.target.value)}>
            <option value="">All agents</option>
            <option value="coder">Coder</option>
            <option value="reviewer">Reviewer</option>
            <option value="planner">Planner</option>
          </Select>
          <Input
            aria-label="Ticket"
            placeholder="Ticket, e.g. CODEIT-12"
            defaultValue={ticket}
            className="w-48 font-mono"
            onKeyDown={(e) => {
              if (e.key === "Enter") setFilter("ticket", e.currentTarget.value.trim().toUpperCase());
            }}
          />
        </div>
      </div>
      {error && <p className="text-bad">{String(error)}</p>}
      <Card className="overflow-x-auto">
        <table className="w-full min-w-[760px] text-sm">
          <thead>
            <tr className="border-b border-line text-left text-xs uppercase tracking-wide text-muted">
              <th className="px-4 py-2 font-medium">Run</th>
              <th className="px-4 py-2 font-medium">Agent</th>
              <th className="px-4 py-2 font-medium">Ticket</th>
              <th className="px-4 py-2 font-medium">Status</th>
              <th className="px-4 py-2 font-medium">Started</th>
              <th className="px-4 py-2 text-right font-medium">Took</th>
              <th className="px-4 py-2 text-right font-medium">Cost</th>
            </tr>
          </thead>
          <tbody className="tabular">
            {(data ?? []).map((r) => (
              <tr key={r.id} className="border-b border-line last:border-0 hover:bg-accent-soft/40">
                <td className="px-4 py-2 font-mono text-xs">
                  <Link className="text-accent hover:underline" href={`/run/?id=${r.id}`}>
                    {r.id.slice(-8)}
                  </Link>
                </td>
                <td className="px-4 py-2">{r.instance || r.role}</td>
                <td className="px-4 py-2 font-mono text-xs">{r.ticket_key}</td>
                <td className="px-4 py-2">
                  <Badge tone={runTone(r.status)}>{r.status}</Badge>
                </td>
                <td className="px-4 py-2 text-muted">{ago(r.started_at, now)}</td>
                <td className="px-4 py-2 text-right">{duration(r.started_at, r.ended_at, now)}</td>
                <td className="px-4 py-2 text-right">{usd(r.cost_usd)}</td>
              </tr>
            ))}
            {data?.length === 0 && (
              <tr>
                <td colSpan={7} className="px-4 py-6 text-center text-muted">
                  No runs match.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </Card>
    </>
  );
}

export default function RunsPage() {
  return (
    <Suspense>
      <Runs />
    </Suspense>
  );
}
