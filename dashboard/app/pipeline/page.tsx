"use client";

import { useQuery } from "@tanstack/react-query";
import Link from "next/link";
import { Badge } from "@/components/ui/badge";
import { useNow } from "@/components/use-now";
import { api } from "@/lib/api";
import type { AgentsResponse, Ticket } from "@/lib/types";
import { ago } from "@/lib/utils";

// Jira is the state machine (PRD 6.1); these are its columns, left to right.
const COLUMNS = ["Agent Draft", "Ready for Dev", "In Dev", "Agent Review", "Human Review", "Done"];

function TicketCard({ ticket, workingOn, now }: { ticket: Ticket; workingOn?: string; now: number }) {
  return (
    <li className="flex flex-col gap-2 rounded-md border border-line bg-panel p-3">
      <div className="flex items-center justify-between gap-2">
        <span className="font-mono text-xs text-muted">{ticket.key}</span>
        {ticket.points != null && <span className="tabular text-xs text-muted">{ticket.points} pt</span>}
      </div>
      <p className="text-sm leading-snug">{ticket.summary}</p>
      <div className="flex flex-wrap items-center gap-1.5">
        {workingOn && <Badge tone="busy">{workingOn}</Badge>}
        {ticket.review_loop > 0 && <Badge tone="warn">loop {ticket.review_loop}</Badge>}
        {ticket.human_returns > 0 && <Badge tone="warn">returned {ticket.human_returns}</Badge>}
        {ticket.pr_url && (
          <a
            className="font-mono text-xs text-accent hover:underline"
            href={ticket.pr_url}
            target="_blank"
            rel="noreferrer"
          >
            PR #{ticket.pr_url.split("/").pop()}
          </a>
        )}
        <Link className="ml-auto text-xs text-muted hover:text-ink" href={`/runs/?ticket=${ticket.key}`}>
          runs
        </Link>
      </div>
      <span className="text-xs text-muted">updated {ago(ticket.updated_at, now)}</span>
    </li>
  );
}

export default function PipelinePage() {
  const now = useNow(30_000);
  const tickets = useQuery({ queryKey: ["tickets"], queryFn: () => api.get<Ticket[]>("/api/tickets") });
  const agents = useQuery({ queryKey: ["agents"], queryFn: () => api.get<AgentsResponse>("/api/agents") });
  const working = new Map(
    (agents.data?.agents ?? []).filter((a) => a.ticket_key).map((a) => [a.ticket_key as string, a.name]),
  );
  const all = tickets.data ?? [];
  const other = all.filter((t) => !COLUMNS.includes(t.status));

  return (
    <>
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h1 className="text-xl font-semibold">Pipeline</h1>
        <p className="text-muted">Open tickets, and tickets changed in the last 7 days. Jira is the source of truth.</p>
      </div>
      {tickets.error && <p className="text-bad">{String(tickets.error)}</p>}
      <div className="overflow-x-auto pb-2">
        <div className="grid min-w-[1100px] grid-cols-6 gap-3">
          {COLUMNS.map((status) => {
            const column = all.filter((t) => t.status === status);
            return (
              <section key={status} className="flex flex-col gap-2 rounded-lg bg-code/60 p-2">
                <h2 className="flex items-center justify-between px-1 text-xs font-semibold uppercase tracking-wide text-muted">
                  {status}
                  <span className="tabular">{column.length}</span>
                </h2>
                <ul className="flex flex-col gap-2">
                  {column.map((t) => (
                    <TicketCard key={t.key} ticket={t} workingOn={working.get(t.key)} now={now} />
                  ))}
                </ul>
              </section>
            );
          })}
        </div>
      </div>
      {other.length > 0 && (
        <p className="text-sm text-muted">
          Also: {other.map((t) => `${t.key} (${t.status})`).join(", ")}
        </p>
      )}
    </>
  );
}
