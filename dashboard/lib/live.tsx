"use client";

// One EventSource on /api/stream for the whole app. Each event updates the TanStack Query
// cache, so every page shows changes as they happen (PRD M8: within 2 seconds).

import { useQueryClient } from "@tanstack/react-query";
import { createContext, useContext, useEffect, useState, type ReactNode } from "react";
import type { AgentsResponse, Budget, RunStreamEvent, Ticket } from "./types";

type Status = "connecting" | "live" | "offline";
const LiveContext = createContext<Status>("connecting");

export function useLiveStatus(): Status {
  return useContext(LiveContext);
}

export function LiveProvider({ children }: { children: ReactNode }) {
  const client = useQueryClient();
  const [status, setStatus] = useState<Status>("connecting");

  useEffect(() => {
    const source = new EventSource("/api/stream");
    const on = <T,>(type: string, handle: (data: T) => void) =>
      source.addEventListener(type, (e) => handle(JSON.parse((e as MessageEvent<string>).data) as T));

    source.onopen = () => setStatus("live");
    source.onerror = () => setStatus(source.readyState === EventSource.CLOSED ? "offline" : "connecting");

    on<AgentsResponse>("agent_state", (data) =>
      client.setQueryData<AgentsResponse>(["agents"], (old) => ({
        running: data.running ?? old?.running ?? true,
        agents: data.agents,
        slots: data.slots ?? old?.slots ?? {},
      })),
    );
    on<Budget>("budget_update", (data) => client.setQueryData(["budget"], data));
    on<Ticket>("ticket_update", (ticket) =>
      client.setQueryData<Ticket[]>(["tickets"], (old) => {
        const rest = (old ?? []).filter((t) => t.key !== ticket.key);
        return [ticket, ...rest];
      }),
    );
    on<RunStreamEvent>("run_event", (run) => {
      void client.invalidateQueries({ queryKey: ["runs"] });
      void client.invalidateQueries({ queryKey: ["run", run.run_id] });
    });
    return () => source.close();
  }, [client]);

  return <LiveContext.Provider value={status}>{children}</LiveContext.Provider>;
}
