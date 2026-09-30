"use client";

import { useQuery } from "@tanstack/react-query";
import { AnimatePresence, LayoutGroup, motion } from "motion/react";
import Link from "next/link";
import { Badge } from "@/components/ui/badge";
import { useNow } from "@/components/use-now";
import { api } from "@/lib/api";
import { roleColor } from "@/lib/roles";
import type { AgentsResponse, Ticket } from "@/lib/types";
import { ago } from "@/lib/utils";

// Jira is the state machine (PRD 6.1); these are its columns, left to right, each in the
// colour of whoever owns that step.
const COLUMNS = [
  { status: "Agent Draft", color: "var(--planner)", owner: "You approve" },
  { status: "Ready for Dev", color: "var(--coder)", owner: "Coder picks up" },
  { status: "In Dev", color: "var(--coder)", owner: "Coder working" },
  { status: "Agent Review", color: "var(--reviewer)", owner: "Reviewer checks" },
  { status: "Human Review", color: "var(--human)", owner: "You review" },
  { status: "Done", color: "var(--done)", owner: "Merged" },
];

function TicketCard({ ticket, holder, now }: { ticket: Ticket; holder?: { name: string; role: string }; now: number }) {
  const color = holder ? roleColor(holder.role) : undefined;
  return (
    <motion.li
      layout
      layoutId={ticket.key}
      initial={{ opacity: 0, scale: 0.94 }}
      animate={{ opacity: 1, scale: 1 }}
      exit={{ opacity: 0, scale: 0.94 }}
      transition={{ type: "spring", stiffness: 380, damping: 32 }}
      whileHover={{ y: -2 }}
      className="relative flex flex-col gap-2 rounded-xl border bg-panel p-3 shadow-[var(--shadow)]"
      style={{ borderColor: color ? `color-mix(in srgb, ${color} 55%, var(--line))` : "var(--line)" }}
    >
      {color && (
        <motion.span
          className="pointer-events-none absolute inset-0 rounded-xl"
          animate={{ boxShadow: [`0 0 0 0 ${color}`, `0 0 22px -6px ${color}`, `0 0 0 0 ${color}`] }}
          transition={{ duration: 2.4, repeat: Infinity, ease: "easeInOut" }}
        />
      )}
      <div className="flex items-center justify-between gap-2">
        <span className="font-mono text-[11px] text-faint">{ticket.key}</span>
        {ticket.points != null && (
          <span className="tabular rounded-md bg-panel-2 px-1.5 py-0.5 font-mono text-[10px] text-muted">{ticket.points} pt</span>
        )}
      </div>
      <p className="text-[13px] leading-snug">{ticket.summary}</p>
      <div className="flex flex-wrap items-center gap-1.5">
        {holder && <Badge color={color}>{holder.name}</Badge>}
        {ticket.review_loop > 0 && <Badge tone="warn">loop {ticket.review_loop}</Badge>}
        {ticket.human_returns > 0 && <Badge tone="warn">returned {ticket.human_returns}</Badge>}
        {ticket.pr_url && (
          <a className="font-mono text-[11px] text-muted hover:text-ink" href={ticket.pr_url} target="_blank" rel="noreferrer">
            PR #{ticket.pr_url.split("/").pop()}
          </a>
        )}
      </div>
      <div className="flex items-center justify-between text-[11px] text-faint">
        <span>{ago(ticket.updated_at, now)}</span>
        <Link className="hover:text-ink" href={`/runs/?ticket=${ticket.key}`}>
          runs
        </Link>
      </div>
    </motion.li>
  );
}

export default function PipelinePage() {
  const now = useNow(30_000);
  const tickets = useQuery({ queryKey: ["tickets"], queryFn: () => api.get<Ticket[]>("/api/tickets") });
  const agents = useQuery({ queryKey: ["agents"], queryFn: () => api.get<AgentsResponse>("/api/agents") });
  const holders = new Map(
    (agents.data?.agents ?? []).filter((a) => a.ticket_key).map((a) => [a.ticket_key as string, { name: a.name, role: a.role }]),
  );
  const all = tickets.data ?? [];
  const other = all.filter((t) => !COLUMNS.some((c) => c.status === t.status));

  return (
    <>
      <motion.div initial={{ opacity: 0, y: 8 }} animate={{ opacity: 1, y: 0 }} className="flex flex-wrap items-end justify-between gap-2">
        <div>
          <h1 className="font-display text-3xl font-semibold tracking-tight">Pipeline</h1>
          <p className="text-muted">Open tickets and tickets changed in the last 7 days. Jira is the source of truth.</p>
        </div>
      </motion.div>
      {tickets.error && <p className="text-bad">{String(tickets.error)}</p>}
      <div className="overflow-x-auto pb-2">
        <LayoutGroup>
          <div className="grid min-w-[1060px] grid-cols-6 gap-3">
            {COLUMNS.map((col, i) => {
              const column = all.filter((t) => t.status === col.status);
              return (
                <motion.section
                  key={col.status}
                  initial={{ opacity: 0, y: 16 }}
                  animate={{ opacity: 1, y: 0 }}
                  transition={{ delay: i * 0.05, duration: 0.45, ease: [0.16, 1, 0.3, 1] }}
                  className="flex min-h-64 flex-col gap-2.5 rounded-2xl border border-line bg-[color-mix(in_srgb,var(--panel)_55%,transparent)] p-2.5"
                >
                  <header className="flex flex-col gap-1.5 px-1 pt-1">
                    <div className="flex items-center justify-between">
                      <h2 className="flex items-center gap-2 text-[13px] font-semibold">
                        <span className="size-2 rounded-full" style={{ background: col.color }} />
                        {col.status}
                      </h2>
                      <span className="tabular rounded-full bg-panel-2 px-2 font-mono text-[11px] text-muted">{column.length}</span>
                    </div>
                    <span className="text-[11px] text-faint">{col.owner}</span>
                    <span className="h-0.5 rounded-full" style={{ background: `linear-gradient(90deg, ${col.color}, transparent)` }} />
                  </header>
                  <ul className="flex flex-col gap-2">
                    <AnimatePresence mode="popLayout">
                      {column.map((t) => (
                        <TicketCard key={t.key} ticket={t} holder={holders.get(t.key)} now={now} />
                      ))}
                    </AnimatePresence>
                  </ul>
                </motion.section>
              );
            })}
          </div>
        </LayoutGroup>
      </div>
      {other.length > 0 && (
        <p className="text-sm text-muted">Also: {other.map((t) => `${t.key} (${t.status})`).join(", ")}</p>
      )}
    </>
  );
}
