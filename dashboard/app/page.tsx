"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import Link from "next/link";
import { useState, type FormEvent } from "react";
import { Badge, type Tone } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardBody, CardHeader, CardTitle } from "@/components/ui/card";
import { Input, Select } from "@/components/ui/input";
import { useNow } from "@/components/use-now";
import { api, ApiError } from "@/lib/api";
import type { Agent, AgentsResponse } from "@/lib/types";
import { duration } from "@/lib/utils";

const ROLES = ["reviewer", "coder"] as const;
const stateTone: Record<Agent["state"], Tone> = { busy: "busy", idle: "neutral", parked: "warn", disabled: "neutral" };

function AgentRow({ agent, now }: { agent: Agent; now: number }) {
  return (
    <li className="flex flex-wrap items-center gap-x-4 gap-y-1 px-4 py-3">
      <span className="w-24 font-mono text-sm">{agent.name}</span>
      <Badge tone={stateTone[agent.state]}>{agent.state}</Badge>
      {agent.state === "busy" && agent.run_id ? (
        <span className="flex flex-wrap items-center gap-x-3 text-sm">
          <Link className="font-mono text-accent hover:underline" href={`/run/?id=${agent.run_id}`}>
            {agent.ticket_key ?? agent.run_id}
          </Link>
          <span className="tabular text-muted">for {duration(agent.since ?? null, null, now)}</span>
        </span>
      ) : agent.state === "parked" ? (
        <span className="text-sm text-muted">{agent.reason}</span>
      ) : agent.state === "disabled" ? (
        <span className="text-sm text-muted">above the slot count</span>
      ) : (
        <span className="text-sm text-muted">waiting for a ticket</span>
      )}
    </li>
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
    <div className="flex items-center gap-2">
      <span className="text-xs text-muted">slots</span>
      <Button
        variant="ghost"
        aria-label={`Fewer ${role} slots`}
        disabled={change.isPending || count <= 0}
        onClick={() => change.mutate(count - 1)}
      >
        −
      </Button>
      <span className="tabular w-4 text-center font-mono">{count}</span>
      <Button
        variant="ghost"
        aria-label={`More ${role} slots`}
        disabled={change.isPending || count >= 10}
        onClick={() => change.mutate(count + 1)}
      >
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
    onSuccess: (r) => setMessage({ ok: true, text: `${r.instance} started run ${r.run_id}` }),
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
        <form onSubmit={submit} className="flex flex-wrap items-end gap-3">
          <div className="flex flex-col gap-1">
            <label htmlFor="role" className="text-xs text-muted">
              Agent
            </label>
            <Select id="role" value={role} onChange={(e) => setRole(e.target.value)}>
              <option value="reviewer">Reviewer</option>
              <option value="coder">Coder</option>
            </Select>
          </div>
          <div className="flex flex-col gap-1">
            <label htmlFor="ticket" className="text-xs text-muted">
              Ticket
            </label>
            <Input
              id="ticket"
              placeholder="CODEIT-12"
              value={key}
              onChange={(e) => setKey(e.target.value)}
              className="w-36 font-mono"
              required
            />
          </div>
          <Button type="submit" variant="primary" disabled={!running || start.isPending || !key.trim()}>
            Start
          </Button>
          <p className="basis-full text-xs text-muted">
            Uses the same limits as the loop: a free slot, the budget, and the ticket in the right status.
          </p>
          {message && <p className={message.ok ? "text-sm text-ok" : "text-sm text-bad"}>{message.text}</p>}
        </form>
      </CardBody>
    </Card>
  );
}

export default function AgentsPage() {
  const now = useNow();
  const { data, error } = useQuery({
    queryKey: ["agents"],
    queryFn: () => api.get<AgentsResponse>("/api/agents"),
  });
  const agents = data?.agents ?? [];
  const busy = agents.filter((a) => a.state === "busy").length;

  return (
    <>
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h1 className="text-xl font-semibold">Agents</h1>
        <p className="text-muted">
          {data ? `${busy} of ${agents.filter((a) => a.state !== "disabled").length} working` : "Loading"}
        </p>
      </div>
      {error && <p className="text-bad">{String(error)}</p>}
      {data && !data.running && (
        <p className="rounded-md bg-warn-soft px-3 py-2 text-sm text-warn">
          The orchestrator is not running. Start it with <code className="font-mono">uv run codeit up</code>.
        </p>
      )}
      <div className="grid gap-4 md:grid-cols-2">
        {ROLES.map((role) => (
          <Card key={role}>
            <CardHeader>
              <CardTitle className="capitalize">{role}s</CardTitle>
              {data?.running && <SlotControl role={role} count={data.slots[role] ?? 0} />}
            </CardHeader>
            <ul className="divide-y divide-line">
              {agents
                .filter((a) => a.role === role)
                .map((a) => (
                  <AgentRow key={a.name} agent={a} now={now} />
                ))}
            </ul>
          </Card>
        ))}
      </div>
      <StartRun running={data?.running ?? false} />
    </>
  );
}
