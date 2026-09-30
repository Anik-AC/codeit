"use client";

import { useQuery } from "@tanstack/react-query";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { Suspense } from "react";
import { Transcript } from "@/components/transcript";
import { Badge, runTone } from "@/components/ui/badge";
import { Card, CardBody, CardHeader, CardTitle } from "@/components/ui/card";
import { useNow } from "@/components/use-now";
import { api } from "@/lib/api";
import type { RunDetail } from "@/lib/types";
import { duration, usd } from "@/lib/utils";

function Fact({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex flex-col gap-0.5">
      <dt className="text-xs text-muted">{label}</dt>
      <dd className="tabular text-sm">{children || "–"}</dd>
    </div>
  );
}

function ReviewResult({ result }: { result: Record<string, unknown> }) {
  const checks = (result.checks as Record<string, string> | undefined) ?? {};
  const verdict = result.verdict as { verdict?: string; summary_md?: string } | null | undefined;
  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-wrap gap-2">
        {Object.entries(checks).map(([name, status]) => (
          <Badge key={name} tone={status === "pass" ? "ok" : status === "fail" ? "bad" : "neutral"}>
            {name}: {status}
          </Badge>
        ))}
      </div>
      {verdict?.summary_md && <p className="whitespace-pre-wrap">{verdict.summary_md}</p>}
      <p className="text-muted">
        Route: {String(result.route ?? "")}, review loop {String(result.review_loop ?? "")}
        {typeof result.review_url === "string" && result.review_url && (
          <>
            {" · "}
            <a className="text-ink underline-offset-4 hover:underline" href={result.review_url} target="_blank" rel="noreferrer">
              PR review
            </a>
          </>
        )}
      </p>
    </div>
  );
}

function CoderResult({ result }: { result: Record<string, unknown> }) {
  return (
    <div className="flex flex-col gap-2">
      {typeof result.pr_url === "string" && result.pr_url && (
        <a className="text-ink underline-offset-4 hover:underline" href={result.pr_url} target="_blank" rel="noreferrer">
          {result.pr_url}
        </a>
      )}
      {typeof result.comment === "string" && <p className="whitespace-pre-wrap">{result.comment}</p>}
    </div>
  );
}

function Run() {
  const id = useSearchParams().get("id") ?? "";
  const now = useNow();
  const { data: run, error } = useQuery({
    queryKey: ["run", id],
    queryFn: () => api.get<RunDetail>(`/api/runs/${id}`),
    enabled: Boolean(id),
    refetchInterval: (q) => (q.state.data?.status === "running" ? 5_000 : false),
  });
  if (!id) return <p className="text-muted">No run selected.</p>;
  if (error) return <p className="text-bad">{String(error)}</p>;
  if (!run) return <p className="text-muted">Loading</p>;

  return (
    <>
      <div className="flex flex-wrap items-center gap-3">
        <Link href="/runs/" className="text-sm text-muted hover:text-ink">
          Runs
        </Link>
        <span className="text-muted">/</span>
        <h1 className="font-display text-2xl font-semibold tracking-tight">
          <span className="font-mono text-xl">{run.id}</span>
        </h1>
        <Badge tone={runTone(run.status)}>{run.status}</Badge>
      </div>
      <Card>
        <CardBody>
          <dl className="grid grid-cols-2 gap-4 sm:grid-cols-4">
            <Fact label="Agent">{run.instance || run.role}</Fact>
            <Fact label="Ticket">
              {run.ticket_key && (
                <Link className="font-mono text-ink underline-offset-4 hover:underline" href={`/runs/?ticket=${run.ticket_key}`}>
                  {run.ticket_key}
                </Link>
              )}
            </Fact>
            <Fact label="Model">{run.model}</Fact>
            <Fact label="Took">{duration(run.started_at, run.ended_at, now)}</Fact>
            <Fact label="Turns or calls">{run.turns}</Fact>
            <Fact label="Tokens in / out">
              {run.input_tokens != null ? `${run.input_tokens} / ${run.output_tokens}` : ""}
            </Fact>
            <Fact label="Cost">{usd(run.cost_usd)}</Fact>
            <Fact label="Started">{run.started_at && new Date(run.started_at).toLocaleString()}</Fact>
          </dl>
        </CardBody>
      </Card>
      {run.error && <p className="rounded-md border border-line px-3 py-2 text-sm text-bad">{run.error}</p>}
      {run.result && (
        <Card>
          <CardHeader>
            <CardTitle>Result</CardTitle>
          </CardHeader>
          <CardBody className="text-sm">
            {run.role === "reviewer" ? <ReviewResult result={run.result} /> : <CoderResult result={run.result} />}
          </CardBody>
        </Card>
      )}
      {(run.has_transcript || run.status === "running") && run.role !== "reviewer" && (
        <Card>
          <CardHeader>
            <CardTitle>Transcript</CardTitle>
          </CardHeader>
          <CardBody>
            <Transcript key={run.id} runId={run.id} />
          </CardBody>
        </Card>
      )}
      {run.events.length > 0 && (
        <Card>
          <CardHeader>
            <CardTitle>Orchestrator events</CardTitle>
          </CardHeader>
          <ul className="divide-y divide-line text-sm">
            {run.events.map((e, i) => (
              <li key={i} className="flex flex-wrap gap-3 px-4 py-2">
                <span className="tabular text-muted">{new Date(e.ts).toLocaleTimeString()}</span>
                <Badge>{e.type}</Badge>
                <code className="font-mono text-xs">{JSON.stringify(e.payload)}</code>
              </li>
            ))}
          </ul>
        </Card>
      )}
    </>
  );
}

export default function RunPage() {
  return (
    <Suspense>
      <Run />
    </Suspense>
  );
}
