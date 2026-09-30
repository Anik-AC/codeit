"use client";

// Renders a Claude Code stream-json transcript as it streams in: the agent's messages,
// its tool calls and their results, and the final result line.

import { useEffect, useRef, useState } from "react";
import { cn } from "@/lib/utils";

type Line = Record<string, unknown>;
type Block = { type?: string; text?: string; name?: string; input?: unknown; content?: unknown; is_error?: boolean };

function blocks(line: Line): Block[] {
  const message = line.message as { content?: unknown } | undefined;
  return Array.isArray(message?.content) ? (message.content as Block[]) : [];
}

function short(value: unknown, max = 600): string {
  const text = typeof value === "string" ? value : JSON.stringify(value, null, 2);
  return text.length > max ? `${text.slice(0, max)}\n… (${text.length - max} more characters)` : text;
}

function toolResultText(content: unknown): string {
  if (Array.isArray(content)) {
    return content.map((c: { text?: string }) => c.text ?? "").join("\n");
  }
  return short(content);
}

function Entry({ line }: { line: Line }) {
  const type = String(line.type ?? "");
  if (type === "system" && line.subtype === "init") {
    const servers = (line.mcp_servers as { name: string; status: string }[] | undefined) ?? [];
    return (
      <div className="text-xs text-muted">
        Session started · model {String(line.model ?? "?")}
        {servers.map((s) => ` · MCP ${s.name}: ${s.status}`)}
      </div>
    );
  }
  if (type === "assistant") {
    return (
      <>
        {blocks(line).map((b, i) =>
          b.type === "text" && b.text ? (
            <p key={i} className="whitespace-pre-wrap">{b.text}</p>
          ) : b.type === "tool_use" ? (
            <div key={i} className="rounded-md bg-code px-3 py-2">
              <div className="font-mono text-xs font-semibold text-accent">{b.name}</div>
              <pre className="mt-1 overflow-x-auto whitespace-pre-wrap font-mono text-xs text-muted">{short(b.input, 400)}</pre>
            </div>
          ) : null,
        )}
      </>
    );
  }
  if (type === "user") {
    return (
      <>
        {blocks(line)
          .filter((b) => b.type === "tool_result")
          .map((b, i) => (
            <pre
              key={i}
              className={cn(
                "max-h-48 overflow-auto whitespace-pre-wrap border-l-2 pl-3 font-mono text-xs",
                b.is_error ? "border-bad text-bad" : "border-line text-muted",
              )}
            >
              {short(toolResultText(b.content), 1500)}
            </pre>
          ))}
      </>
    );
  }
  if (type === "result") {
    return (
      <div className="rounded-md border border-line px-3 py-2">
        <div className="text-xs text-muted">
          Finished: {String(line.subtype ?? "")} · {String(line.num_turns ?? "?")} turns
        </div>
        <p className="mt-1 whitespace-pre-wrap">{String(line.result ?? "")}</p>
      </div>
    );
  }
  return null; // rate-limit and other bookkeeping events
}

export function Transcript({ runId }: { runId: string }) {
  const [lines, setLines] = useState<Line[]>([]);
  const [state, setState] = useState<"streaming" | "done" | "error">("streaming");
  const [follow, setFollow] = useState(true);
  const bottom = useRef<HTMLDivElement>(null);

  // A new run id remounts this component (see `key` in app/run), so state starts fresh.
  useEffect(() => {
    const source = new EventSource(`/api/runs/${runId}/transcript/stream`);
    source.addEventListener("line", (e) => {
      const line = JSON.parse((e as MessageEvent<string>).data) as Line;
      setLines((old) => [...old, line]);
    });
    source.addEventListener("end", () => {
      setState("done");
      source.close();
    });
    source.onerror = () => {
      if (source.readyState === EventSource.CLOSED) setState("error");
    };
    return () => source.close();
  }, [runId]);

  useEffect(() => {
    if (follow) bottom.current?.scrollIntoView({ block: "end" });
  }, [lines, follow]);

  return (
    <div className="flex flex-col gap-3">
      <div className="flex items-center justify-between text-xs text-muted">
        <span>
          {lines.length} events ·{" "}
          {state === "streaming" ? "live" : state === "done" ? "complete" : "stream closed"}
        </span>
        <label className="flex items-center gap-2">
          <input type="checkbox" checked={follow} onChange={(e) => setFollow(e.target.checked)} />
          Follow
        </label>
      </div>
      <div className="flex max-h-[70vh] flex-col gap-3 overflow-y-auto text-sm">
        {lines.map((line, i) => (
          <Entry key={i} line={line} />
        ))}
        <div ref={bottom} />
      </div>
    </div>
  );
}
