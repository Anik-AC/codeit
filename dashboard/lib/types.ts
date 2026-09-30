// Shapes returned by the CodeIt API (src/codeit/orchestrator/api.py).

export type AgentState = "busy" | "idle" | "parked" | "disabled";

export interface Agent {
  name: string;
  role: string;
  state: AgentState;
  run_id: string | null;
  ticket_key?: string | null;
  since?: string | null;
  reason?: string | null;
}

export interface AgentsResponse {
  running: boolean;
  agents: Agent[];
  slots: Record<string, number>;
}

export interface Ticket {
  key: string;
  status: string;
  summary: string;
  priority: string | null;
  points: number | null;
  review_loop: number;
  human_returns: number;
  pr_url: string | null;
  updated_at: string | null;
}

export interface RunSummary {
  id: string;
  role: string;
  instance: string;
  ticket_key: string | null;
  status: string;
  backend: string | null;
  model: string | null;
  started_at: string | null;
  ended_at: string | null;
  turns: number | null;
  cost_usd: number | null;
}

export interface RunEvent {
  ts: string;
  type: string;
  payload: Record<string, unknown>;
}

export interface RunDetail extends RunSummary {
  prompt_hash: string | null;
  input_tokens: number | null;
  output_tokens: number | null;
  result: Record<string, unknown> | null;
  error: string | null;
  has_transcript: boolean;
  events: RunEvent[];
}

export interface Budget {
  claude: {
    state: string;
    parked_until: string | null;
    window: string[];
    in_window: boolean;
    runs_today: number;
  };
  openrouter: {
    key: boolean;
    spend: Record<string, [number, number]>;
    free_requests: [number, number];
  };
}

export interface RunStreamEvent {
  run_id: string;
  role: string;
  ticket_key: string;
  instance: string;
  started: string;
  event: "started" | "finished" | "failed";
}
