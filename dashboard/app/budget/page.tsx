"use client";

import { useQuery } from "@tanstack/react-query";
import { motion } from "motion/react";
import { AnimatedNumber } from "@/components/animated-number";
import { Badge } from "@/components/ui/badge";
import { Card, CardBody, CardHeader, CardTitle } from "@/components/ui/card";
import { useNow } from "@/components/use-now";
import { api } from "@/lib/api";
import type { Budget } from "@/lib/types";
import { usd } from "@/lib/utils";

const ROLE_COLOR: Record<string, string> = {
  reviewer: "var(--reviewer)",
  coder: "var(--coder)",
  docs: "var(--planner)",
  ops: "var(--muted)",
};

/** A 24-hour dial: Claude's run window as a lit arc, the current local time as a needle. */
function WindowClock({ windows, now, open }: { windows: string[]; now: number; open: boolean }) {
  const R = 78;
  const C = 100;
  const angle = (minutes: number) => (minutes / 1440) * 360 - 90;
  const point = (deg: number, r: number): [number, number] => [C + r * Math.cos((deg * Math.PI) / 180), C + r * Math.sin((deg * Math.PI) / 180)];
  const toMin = (hhmm: string) => {
    const [h, m] = hhmm.split(":").map(Number);
    return (h ?? 0) * 60 + (m ?? 0);
  };
  const arcs = windows
    .filter((w) => w.includes("-"))
    .map((w) => {
      const [a, b] = w.split("-").map(toMin) as [number, number];
      const span = (b - a + 1440) % 1440 || 1440;
      const [x1, y1] = point(angle(a), R);
      const [x2, y2] = point(angle(a + span), R);
      return { d: `M${x1} ${y1} A${R} ${R} 0 ${span > 720 ? 1 : 0} 1 ${x2} ${y2}`, key: w };
    });
  const d = new Date(now);
  const minutes = d.getHours() * 60 + d.getMinutes();
  const [nx, ny] = point(angle(minutes), R - 16);
  const color = open ? "var(--coder)" : "var(--faint)";
  return (
    <svg viewBox="0 0 200 200" className="mx-auto w-full max-w-[220px]" role="img" aria-label={`Run window ${windows.join(", ")}`}>
      <circle cx={C} cy={C} r={R} fill="none" stroke="var(--line)" strokeWidth={12} />
      {arcs.map((a) => (
        <motion.path
          key={a.key}
          d={a.d}
          fill="none"
          stroke="var(--coder)"
          strokeWidth={12}
          strokeLinecap="round"
          initial={{ pathLength: 0, opacity: 0.4 }}
          animate={{ pathLength: 1, opacity: open ? 1 : 0.55 }}
          transition={{ duration: 1.4, ease: [0.16, 1, 0.3, 1] }}
        />
      ))}
      {Array.from({ length: 24 }, (_, h) => {
        const [x1, y1] = point(angle(h * 60), R + 10);
        const [x2, y2] = point(angle(h * 60), R + (h % 6 === 0 ? 16 : 13));
        return <line key={h} x1={x1} y1={y1} x2={x2} y2={y2} stroke="var(--faint)" strokeWidth={h % 6 === 0 ? 1.6 : 0.8} />;
      })}
      {[0, 6, 12, 18].map((h) => {
        const [x, y] = point(angle(h * 60), R - 26);
        return (
          <text key={h} x={x} y={y + 3.5} textAnchor="middle" fontSize={10} fill="var(--muted)" fontFamily="var(--font-mono)">
            {String(h).padStart(2, "0")}
          </text>
        );
      })}
      <motion.line
        x1={C}
        y1={C}
        x2={nx}
        y2={ny}
        stroke={color}
        strokeWidth={2.6}
        strokeLinecap="round"
        initial={{ opacity: 0 }}
        animate={{ opacity: 1 }}
        transition={{ delay: 0.6 }}
      />
      <circle cx={C} cy={C} r={5} fill={color} />
    </svg>
  );
}

function Meter({ label, used, cap, color, i, money = true }: { label: string; used: number; cap: number; color: string; i: number; money?: boolean }) {
  const share = cap > 0 ? Math.min(1, used / cap) : 0;
  const fill = share >= 1 ? "var(--bad)" : share >= 0.8 ? "var(--warn)" : color;
  return (
    <div className="flex flex-col gap-1.5">
      <div className="flex items-baseline justify-between text-sm">
        <span className="capitalize">{label}</span>
        <span className="tabular text-muted">
          <AnimatedNumber value={used} format={money ? (n) => usd(n) || "$0.00" : undefined} className="text-ink" /> of{" "}
          {money ? usd(cap) : cap}
        </span>
      </div>
      <div className="h-2 overflow-hidden rounded-full bg-panel-2" role="meter" aria-label={label} aria-valuemin={0} aria-valuemax={cap} aria-valuenow={used}>
        <motion.div
          className="h-full rounded-full"
          style={{ background: fill, boxShadow: `0 0 12px -2px ${fill}` }}
          initial={{ width: 0 }}
          animate={{ width: `${Math.max(share * 100, used > 0 ? 2 : 0)}%` }}
          transition={{ delay: 0.15 + i * 0.08, duration: 1, ease: [0.16, 1, 0.3, 1] }}
        />
      </div>
    </div>
  );
}

export default function BudgetPage() {
  const now = useNow(30_000);
  const { data, error } = useQuery({ queryKey: ["budget"], queryFn: () => api.get<Budget>("/api/budget") });
  if (error) return <p className="text-bad">{String(error)}</p>;
  if (!data) return <p className="text-muted">Loading</p>;
  const c = data.claude;
  const o = data.openrouter;
  return (
    <>
      <motion.div initial={{ opacity: 0, y: 8 }} animate={{ opacity: 1, y: 0 }}>
        <h1 className="font-display text-3xl font-semibold tracking-tight">Budget</h1>
        <p className="text-muted">What the agents may spend, and when Claude may run.</p>
      </motion.div>
      <div className="grid gap-4 md:grid-cols-2">
        <motion.div initial={{ opacity: 0, y: 14 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: 0.5 }}>
          <Card className="h-full">
            <CardHeader>
              <CardTitle>Claude (subscription)</CardTitle>
              <Badge tone={c.state === "ok" ? "ok" : "warn"}>{c.state === "ok" ? "available" : c.state}</Badge>
            </CardHeader>
            <CardBody className="grid items-center gap-6 sm:grid-cols-[220px_1fr]">
              <WindowClock windows={c.window} now={now} open={c.in_window} />
              <dl className="grid grid-cols-[max-content_1fr] gap-x-5 gap-y-3 text-sm">
                <dt className="text-muted">Run window</dt>
                <dd>
                  <span className="font-mono">{c.window.join(", ")}</span>{" "}
                  <Badge tone={c.in_window ? "ok" : "neutral"}>{c.in_window ? "open" : "closed"}</Badge>
                </dd>
                <dt className="text-muted">Now</dt>
                <dd className="font-mono">{new Date(now).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}</dd>
                <dt className="text-muted">Runs today</dt>
                <dd>
                  <AnimatedNumber value={c.runs_today} className="tabular font-display text-xl font-semibold" />
                </dd>
                {c.parked_until && (
                  <>
                    <dt className="text-muted">Paused until</dt>
                    <dd>{new Date(c.parked_until).toLocaleString()}</dd>
                  </>
                )}
                <dd className="col-span-2 text-xs text-faint">
                  Coders start only inside the window, one at a time, and pause after a usage limit until it resets.
                </dd>
              </dl>
            </CardBody>
          </Card>
        </motion.div>
        <motion.div initial={{ opacity: 0, y: 14 }} animate={{ opacity: 1, y: 0 }} transition={{ delay: 0.08, duration: 0.5 }}>
          <Card className="h-full">
            <CardHeader>
              <CardTitle>OpenRouter (today)</CardTitle>
              <Badge tone={o.key ? "ok" : "bad"}>{o.key ? "key set" : "no key"}</Badge>
            </CardHeader>
            <CardBody className="flex flex-col gap-5">
              {Object.entries(o.spend).map(([role, [spent, cap]], i) => (
                <Meter key={role} i={i} label={role} used={spent} cap={cap} color={ROLE_COLOR[role] ?? "var(--muted)"} />
              ))}
              <Meter i={5} label="free-model requests" used={o.free_requests[0]} cap={o.free_requests[1]} color="var(--muted)" money={false} />
            </CardBody>
          </Card>
        </motion.div>
      </div>
    </>
  );
}
