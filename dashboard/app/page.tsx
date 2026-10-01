"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AnimatePresence, motion } from "motion/react";
import Link from "next/link";
import { useState, type FormEvent } from "react";
import { AgentAvatar } from "@/components/agent-avatar";
import { AnimatedNumber } from "@/components/animated-number";
import { FastLaneControl } from "@/components/fast-lane";
import { LogoMark } from "@/components/logo";
import { PipelineFlow } from "@/components/pipeline-flow";
import { Badge, runTone } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardBody, CardHeader, CardTitle } from "@/components/ui/card";
import { Input, Select } from "@/components/ui/input";
import { useNow } from "@/components/use-now";
import { api, ApiError } from "@/lib/api";
import { ROLES, roleColor } from "@/lib/roles";
import type { Agent, AgentsResponse, Budget, RunSummary, Ticket } from "@/lib/types";
import { ago, duration, usd } from "@/lib/utils";

const MAIN_SECTIONS = ["coder", "reviewer"] as const;
// Agents that work around the main flow: keeping PRs current, writing docs, learning.
const SUPPORT_SECTIONS = ["rebase", "docs", "learning"] as const;
type SectionRole = (typeof MAIN_SECTIONS)[number] | (typeof SUPPORT_SECTIONS)[number];
const STATE_TEXT: Record<Agent["state"], string> = {
  busy: "Working",
  idle: "Ready",
  parked: "Waiting",
  disabled: "Off",
};

const rise = {
  hidden: { opacity: 0, y: 14 },
  show: (i: number) => ({ opacity: 1, y: 0, transition: { delay: i * 0.06, duration: 0.5, ease: [0.16, 1, 0.3, 1] as const } }),
};

function windowText(budget: Budget | undefined): string {
  if (!budget) return "";
  const w = budget.claude.window.join(", ");
  if (budget.claude.state !== "ok") return "Claude is paused after a usage limit";
  if (w === "any time") return "Claude can run at any time";
  const [start, end] = (budget.claude.window[0] ?? "").split("-");
  return budget.claude.in_window ? `Claude's window is open until ${end}` : `Claude's window opens at ${start}`;
}

function Hero({ agents, tickets, budget }: { agents: Agent[]; tickets: Ticket[]; budget?: Budget }) {
  const working = agents.filter((a) => a.state === "busy");
  const waiting = tickets.filter((t) => t.status === "Human Review").length;
  const title =
    working.length === 0
      ? "All quiet on the night shift"
      : working.length === 1
        ? "One agent at work"
        : `${working.length} agents at work`;
  return (
    <motion.section
      initial={{ opacity: 0, y: 10 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.6, ease: [0.16, 1, 0.3, 1] }}
      className="surface relative overflow-hidden"
    >
      <div className="grid-backdrop pointer-events-none absolute inset-0 opacity-60" />
      <div className="relative flex flex-wrap items-center justify-between gap-8 px-6 py-8 sm:px-8">
        <div className="flex max-w-xl flex-col gap-3">
          <span className="font-mono text-[11px] uppercase tracking-[0.18em] text-faint">Control room</span>
          <AnimatePresence mode="wait">
            <motion.h1
              key={title}
              initial={{ opacity: 0, y: 8, filter: "blur(4px)" }}
              animate={{ opacity: 1, y: 0, filter: "blur(0px)" }}
              exit={{ opacity: 0, y: -8, filter: "blur(4px)" }}
              transition={{ duration: 0.35 }}
              className="font-display text-3xl font-semibold tracking-tight text-balance sm:text-4xl"
            >
              {title}
            </motion.h1>
          </AnimatePresence>
          <p className="text-muted">
            {working.length > 0 && (
              <>
                {working.map((a, i) => (
                  <span key={a.name}>
                    {i > 0 && ", "}
                    <span style={{ color: roleColor(a.role) }}>{a.name}</span> on{" "}
                    <span className="font-mono text-ink">{a.ticket_key}</span>
                  </span>
                ))}
                .{" "}
              </>
            )}
            {waiting > 0 ? `${waiting} ticket${waiting === 1 ? "" : "s"} waiting for your review. ` : ""}
            {windowText(budget)}.
          </p>
        </div>
        <div className="relative hidden sm:block">
          <div
            className="anim-breathe absolute inset-[-28px] rounded-full blur-2xl"
            style={{
              background:
                "conic-gradient(from 200deg, color-mix(in srgb, var(--planner) 45%, transparent), color-mix(in srgb, var(--coder) 45%, transparent), color-mix(in srgb, var(--reviewer) 45%, transparent), color-mix(in srgb, var(--planner) 45%, transparent))",
            }}
          />
          <LogoMark size={112} live className="relative" />
        </div>
      </div>
    </motion.section>
  );
}

function Stat({ label, value, format, color, i }: { label: string; value: number; format?: (n: number) => string; color: string; i: number }) {
  return (
    <motion.div custom={i} variants={rise} initial="hidden" animate="show" className="surface flex flex-col gap-1 px-5 py-4">
      <span className="text-xs text-muted">{label}</span>
      <AnimatedNumber value={value} format={format} className="tabular font-display text-3xl font-semibold tracking-tight" />
      <span className="mt-1 h-1 w-10 rounded-full" style={{ background: color }} />
    </motion.div>
  );
}

function AgentCard({ agent, now, i }: { agent: Agent; now: number; i: number }) {
  const color = roleColor(agent.role);
  const busy = agent.state === "busy";
  return (
    <motion.li
      layout
      custom={i}
      variants={rise}
      initial="hidden"
      animate="show"
      whileHover={{ y: -2 }}
      className="relative flex items-center gap-4 overflow-hidden rounded-xl border bg-panel-2 px-4 py-3.5"
      style={{ borderColor: busy ? `color-mix(in srgb, ${color} 45%, var(--line))` : "var(--line)" }}
    >
      <AgentAvatar role={agent.role} state={agent.state} />
      <div className="flex min-w-0 flex-1 flex-col gap-1">
        <div className="flex items-center gap-2">
          <span className="font-mono text-sm font-medium">{agent.name}</span>
          <AnimatePresence mode="wait">
            <motion.span key={agent.state} initial={{ opacity: 0, scale: 0.9 }} animate={{ opacity: 1, scale: 1 }} exit={{ opacity: 0 }}>
              <Badge color={busy ? color : agent.state === "parked" ? "var(--warn)" : "var(--muted)"}>
                {STATE_TEXT[agent.state]}
              </Badge>
            </motion.span>
          </AnimatePresence>
        </div>
        <div className="truncate text-sm text-muted">
          {busy && agent.run_id ? (
            <>
              on{" "}
              <Link className="font-mono text-ink underline-offset-4 hover:underline" href={`/run/?id=${agent.run_id}`}>
                {agent.ticket_key}
              </Link>{" "}
              <span className="tabular">for {duration(agent.since ?? null, null, now)}</span>
            </>
          ) : agent.state === "parked" ? (
            agent.reason
          ) : agent.state === "disabled" ? (
            "Above the slot count"
          ) : (
            "Waiting for a ticket"
          )}
        </div>
      </div>
      {busy && (
        <span className="anim-shimmer absolute inset-x-0 bottom-0 h-0.5" style={{ color, background: `color-mix(in srgb, ${color} 25%, transparent)` }} />
      )}
    </motion.li>
  );
}

function SlotControl({ role, count }: { role: string; count: number }) {
  const client = useQueryClient();
  const change = useMutation({
    mutationFn: (n: number) => api.patch<{ slots: Record<string, number> }>("/api/agents/slots", { [role]: n }),
    onSuccess: (data) =>
      client.setQueryData<AgentsResponse>(["agents"], (old) => (old ? { ...old, slots: data.slots } : old)),
  });
  return (
    <div className="flex items-center gap-1 rounded-lg border border-line bg-panel-2 p-0.5">
      <Button variant="ghost" className="h-7 w-7 px-0" aria-label={`Fewer ${role} slots`} disabled={change.isPending || count <= 0} onClick={() => change.mutate(count - 1)}>
        −
      </Button>
      <span className="tabular w-12 text-center text-xs text-muted">
        <span className="font-mono text-ink">{count}</span> slot{count === 1 ? "" : "s"}
      </span>
      <Button variant="ghost" className="h-7 w-7 px-0" aria-label={`More ${role} slots`} disabled={change.isPending || count >= 10} onClick={() => change.mutate(count + 1)}>
        +
      </Button>
    </div>
  );
}

function StartRun({ running }: { running: boolean }) {
  const [role, setRole] = useState<string>("reviewer");
  const [key, setKey] = useState("");
  const [message, setMessage] = useState<{ ok: boolean; text: string } | null>(null);
  const start = useMutation({
    mutationFn: () => api.post<{ run_id: string; instance: string }>(`/api/runs/${role}`, { ticket_key: key }),
    onSuccess: (r) => setMessage({ ok: true, text: `${r.instance} started run ${r.run_id.slice(-8)}` }),
    onError: (e) => setMessage({ ok: false, text: e instanceof ApiError ? e.message : String(e) }),
  });
  function submit(e: FormEvent) {
    e.preventDefault();
    setMessage(null);
    start.mutate();
  }
  return (
    <Card>
      <CardHeader>
        <CardTitle>Start a run now</CardTitle>
      </CardHeader>
      <CardBody>
        <form onSubmit={submit} className="flex flex-col gap-3">
          <div className="flex flex-wrap items-end gap-3">
            <div className="flex flex-col gap-1">
              <label htmlFor="role" className="text-xs text-muted">
                Agent
              </label>
              <Select id="role" value={role} onChange={(e) => setRole(e.target.value)}>
                <option value="reviewer">Reviewer</option>
                <option value="coder">Coder</option>
                <option value="rebase">Rebaser</option>
              </Select>
            </div>
            <div className="flex flex-col gap-1">
              <label htmlFor="ticket" className="text-xs text-muted">
                Ticket
              </label>
              <Input id="ticket" placeholder="CODEIT-12" value={key} onChange={(e) => setKey(e.target.value)} className="w-36 font-mono" required />
            </div>
            <Button type="submit" variant="primary" disabled={!running || start.isPending || !key.trim()}>
              Start
            </Button>
          </div>
          <p className="text-xs text-faint">Same limits as the loop: a free slot, the budget, and the ticket in the right status.</p>
          <AnimatePresence>
            {message && (
              <motion.p initial={{ opacity: 0, y: -4 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0 }} className="text-sm" style={{ color: message.ok ? "var(--ok)" : "var(--bad)" }}>
                {message.text}
              </motion.p>
            )}
          </AnimatePresence>
        </form>
      </CardBody>
    </Card>
  );
}

function RecentRuns({ runs, now }: { runs: RunSummary[]; now: number }) {
  return (
    <Card>
      <CardHeader>
        <CardTitle>Recent runs</CardTitle>
        <Link href="/runs/" className="text-xs text-muted hover:text-ink">
          All runs
        </Link>
      </CardHeader>
      <ul className="flex flex-col">
        <AnimatePresence initial={false}>
          {runs.slice(0, 6).map((r) => (
            <motion.li
              key={r.id}
              layout
              initial={{ opacity: 0, x: -12 }}
              animate={{ opacity: 1, x: 0 }}
              exit={{ opacity: 0 }}
              className="flex items-center gap-3 border-b border-line px-5 py-2.5 last:border-0"
            >
              <span className="size-2 shrink-0 rounded-full" style={{ background: roleColor(r.role) }} />
              <Link href={`/run/?id=${r.id}`} className="whitespace-nowrap font-mono text-xs text-ink hover:underline">
                {r.ticket_key ?? r.id.slice(-8)}
              </Link>
              <span className="hidden text-xs text-muted sm:inline">{r.instance || r.role}</span>
              <span className="ml-auto flex items-center gap-2">
                <Badge tone={runTone(r.status)}>{r.status}</Badge>
                <span className="tabular w-16 text-right text-xs text-faint">{ago(r.started_at, now)}</span>
              </span>
            </motion.li>
          ))}
        </AnimatePresence>
        {runs.length === 0 && <li className="px-5 py-6 text-center text-sm text-muted">No runs yet.</li>}
      </ul>
    </Card>
  );
}

function RoleSection({
  role,
  agents,
  running,
  slots,
  now,
}: {
  role: SectionRole;
  agents: Agent[];
  running: boolean;
  slots: Record<string, number>;
  now: number;
}) {
  return (
    <Card>
      <CardHeader>
        <div className="flex min-w-0 items-center gap-2.5">
          <span className="size-2.5 shrink-0 rounded-full" style={{ background: roleColor(role) }} />
          <div className="min-w-0">
            <CardTitle>{ROLES[role].label}s</CardTitle>
            <p className="truncate text-xs text-faint" title={ROLES[role].does}>
              {ROLES[role].does}
            </p>
          </div>
        </div>
        {running && <SlotControl role={role} count={slots[role] ?? 0} />}
      </CardHeader>
      <ul className="flex flex-col gap-2.5 p-4">
        {agents
          .filter((a) => a.role === role)
          .map((a, i) => (
            <AgentCard key={a.name} agent={a} now={now} i={i} />
          ))}
      </ul>
    </Card>
  );
}

export default function AgentsPage() {
  const now = useNow();
  const agents = useQuery({ queryKey: ["agents"], queryFn: () => api.get<AgentsResponse>("/api/agents") });
  const tickets = useQuery({ queryKey: ["tickets"], queryFn: () => api.get<Ticket[]>("/api/tickets") });
  const budget = useQuery({ queryKey: ["budget"], queryFn: () => api.get<Budget>("/api/budget") });
  const runs = useQuery({ queryKey: ["runs", "", ""], queryFn: () => api.get<RunSummary[]>("/api/runs?limit=100") });

  const list = agents.data?.agents ?? [];
  const ticketList = tickets.data ?? [];
  const runList = runs.data ?? [];
  const today = new Date().toDateString();
  const runsToday = runList.filter((r) => r.started_at && new Date(r.started_at).toDateString() === today).length;
  const spent = Object.values(budget.data?.openrouter.spend ?? {}).reduce((sum, [s]) => sum + s, 0);

  return (
    <>
      <Hero agents={list} tickets={ticketList} budget={budget.data} />
      {agents.error && <p className="text-bad">{String(agents.error)}</p>}
      {agents.data && !agents.data.running && (
        <p className="rounded-xl border px-4 py-3 text-sm" style={{ color: "var(--warn)", borderColor: "color-mix(in srgb, var(--warn) 35%, transparent)" }}>
          The orchestrator is not running. Start it with <code className="font-mono">uv run codeit up</code>.
        </p>
      )}

      <FastLaneControl />

      <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
        <Stat i={0} label="Working now" value={list.filter((a) => a.state === "busy").length} color="var(--coder)" />
        <Stat i={1} label="Waiting for you" value={ticketList.filter((t) => t.status === "Human Review").length} color="var(--human)" />
        <Stat i={2} label="Runs today" value={runsToday} color="var(--reviewer)" />
        <Stat i={3} label="Model spend today" value={spent} format={(n) => usd(n) || "$0.00"} color="var(--planner)" />
      </div>

      <Card>
        <CardHeader>
          <CardTitle>Pipeline</CardTitle>
          <Link href="/pipeline/" className="text-xs text-muted hover:text-ink">
            Open the board
          </Link>
        </CardHeader>
        <CardBody className="py-6">
          <PipelineFlow tickets={ticketList} agents={list} fastLane={agents.data?.fast_lane ?? false} />
        </CardBody>
      </Card>

      <div className="grid gap-4 md:grid-cols-2">
        {MAIN_SECTIONS.map((role) => (
          <RoleSection key={role} role={role} agents={list} running={agents.data?.running ?? false} slots={agents.data?.slots ?? {}} now={now} />
        ))}
      </div>
      <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-3">
        {SUPPORT_SECTIONS.map((role) => (
          <RoleSection key={role} role={role} agents={list} running={agents.data?.running ?? false} slots={agents.data?.slots ?? {}} now={now} />
        ))}
      </div>

      <div className="grid gap-4 md:grid-cols-2">
        <StartRun running={agents.data?.running ?? false} />
        <RecentRuns runs={runList} now={now} />
      </div>
    </>
  );
}
