"use client";

import { useQuery } from "@tanstack/react-query";
import { Badge } from "@/components/ui/badge";
import { Card, CardBody, CardHeader, CardTitle } from "@/components/ui/card";
import { api } from "@/lib/api";
import type { Budget } from "@/lib/types";
import { usd } from "@/lib/utils";

function Meter({ used, cap, label }: { used: number; cap: number; label: string }) {
  const share = cap > 0 ? Math.min(1, used / cap) : 0;
  const tone = share >= 1 ? "bg-bad" : share >= 0.8 ? "bg-warn" : "bg-accent";
  return (
    <div className="flex flex-col gap-1">
      <div className="flex justify-between text-sm">
        <span className="capitalize">{label}</span>
        <span className="tabular text-muted">
          {label === "free requests" ? `${used} of ${cap}` : `${usd(used)} of ${usd(cap)}`}
        </span>
      </div>
      <div
        className="h-2 overflow-hidden rounded-full bg-code"
        role="meter"
        aria-label={label}
        aria-valuemin={0}
        aria-valuemax={cap}
        aria-valuenow={used}
      >
        <div className={`h-full ${tone}`} style={{ width: `${share * 100}%` }} />
      </div>
    </div>
  );
}

export default function BudgetPage() {
  const { data, error } = useQuery({ queryKey: ["budget"], queryFn: () => api.get<Budget>("/api/budget") });
  if (error) return <p className="text-bad">{String(error)}</p>;
  if (!data) return <p className="text-muted">Loading</p>;
  const c = data.claude;
  const o = data.openrouter;
  return (
    <>
      <h1 className="text-xl font-semibold">Budget</h1>
      <div className="grid gap-4 md:grid-cols-2">
        <Card>
          <CardHeader>
            <CardTitle>Claude (subscription)</CardTitle>
            <Badge tone={c.state === "ok" ? "ok" : "warn"}>{c.state}</Badge>
          </CardHeader>
          <CardBody className="flex flex-col gap-3 text-sm">
            <dl className="grid grid-cols-[max-content_1fr] gap-x-6 gap-y-2">
              <dt className="text-muted">Run window</dt>
              <dd>
                {c.window.join(", ")}{" "}
                <Badge tone={c.in_window ? "ok" : "neutral"}>{c.in_window ? "open now" : "closed now"}</Badge>
              </dd>
              <dt className="text-muted">Runs today</dt>
              <dd className="tabular">{c.runs_today}</dd>
              {c.parked_until && (
                <>
                  <dt className="text-muted">Parked until</dt>
                  <dd>{new Date(c.parked_until).toLocaleString()}</dd>
                </>
              )}
            </dl>
            <p className="text-xs text-muted">
              Coders start only inside the window, one at a time, and pause after a usage limit until it resets.
            </p>
          </CardBody>
        </Card>
        <Card>
          <CardHeader>
            <CardTitle>OpenRouter (today)</CardTitle>
            <Badge tone={o.key ? "ok" : "bad"}>{o.key ? "key set" : "no key"}</Badge>
          </CardHeader>
          <CardBody className="flex flex-col gap-4">
            {Object.entries(o.spend).map(([role, [spent, cap]]) => (
              <Meter key={role} label={role} used={spent} cap={cap} />
            ))}
            <Meter label="free requests" used={o.free_requests[0]} cap={o.free_requests[1]} />
          </CardBody>
        </Card>
      </div>
    </>
  );
}
