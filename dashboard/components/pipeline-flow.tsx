"use client";

import { AnimatePresence, motion } from "motion/react";
import Link from "next/link";
import { AnimatedNumber } from "@/components/animated-number";
import type { Agent, Ticket } from "@/lib/types";

/*
 * The whole pipeline on one line: Plan, Code, Review, You, Done. Each stage shows how many
 * tickets sit there; the links between stages flow while work moves, and a stage glows
 * while one of its agents is busy.
 */
const STAGES = [
  { id: "plan", label: "Plan", color: "var(--planner)", statuses: ["Agent Draft"], role: "planner", hint: "drafts to approve" },
  { id: "code", label: "Code", color: "var(--coder)", statuses: ["Ready for Dev", "In Dev"], role: "coder", hint: "ready or in dev" },
  { id: "review", label: "Review", color: "var(--reviewer)", statuses: ["Agent Review"], role: "reviewer", hint: "in agent review" },
  { id: "you", label: "You", color: "var(--human)", statuses: ["Human Review"], role: "human", hint: "waiting for you" },
  { id: "done", label: "Done", color: "var(--done)", statuses: ["Done"], role: "done", hint: "this week" },
] as const;

function StageIcon({ id }: { id: string }) {
  const common = { fill: "none", stroke: "currentColor", strokeWidth: 1.9, strokeLinecap: "round" as const, strokeLinejoin: "round" as const };
  switch (id) {
    case "plan":
      return (
        <svg viewBox="0 0 24 24" className="size-5" aria-hidden>
          <path d="M8 6h11M8 12h11M8 18h11" {...common} />
          <circle cx="4.5" cy="6" r="1.2" fill="currentColor" />
          <circle cx="4.5" cy="12" r="1.2" fill="currentColor" />
          <circle cx="4.5" cy="18" r="1.2" fill="currentColor" />
        </svg>
      );
    case "code":
      return (
        <svg viewBox="0 0 24 24" className="size-5" aria-hidden>
          <path d="M8 7l-5 5 5 5M16 7l5 5-5 5M14 4l-4 16" {...common} />
        </svg>
      );
    case "review":
      return (
        <svg viewBox="0 0 24 24" className="size-5" aria-hidden>
          <circle cx="10.5" cy="10.5" r="6" {...common} />
          <path d="M15 15l5 5" {...common} />
        </svg>
      );
    case "you":
      return (
        <svg viewBox="0 0 24 24" className="size-5" aria-hidden>
          <circle cx="12" cy="8" r="3.6" {...common} />
          <path d="M4.5 20c1.4-3.6 4.2-5.4 7.5-5.4s6.1 1.8 7.5 5.4" {...common} />
        </svg>
      );
    default:
      return (
        <svg viewBox="0 0 24 24" className="size-5" aria-hidden>
          <path d="M5 12.5l4.5 4.5L19 7.5" {...common} strokeWidth={2.4} />
        </svg>
      );
  }
}

function Connector({ id, from, to, active }: { id: string; from: string; to: string; active: boolean }) {
  return (
    <svg viewBox="0 0 100 12" preserveAspectRatio="none" className="h-3 w-full" aria-hidden>
      <defs>
        <linearGradient id={`flow-${id}`} gradientUnits="userSpaceOnUse" x1="0" x2="100" y1="6" y2="6">
          <stop offset="0%" style={{ stopColor: from }} />
          <stop offset="100%" style={{ stopColor: to }} />
        </linearGradient>
      </defs>
      <line x1="0" y1="6" x2="100" y2="6" stroke="var(--line-strong)" strokeWidth="2" />
      <line
        x1="0"
        y1="6"
        x2="100"
        y2="6"
        stroke={`url(#flow-${id})`}
        strokeWidth="2.4"
        className="anim-flow"
        style={{ opacity: active ? 1 : 0.35, animationDuration: active ? "0.7s" : "2.4s" }}
        vectorEffect="non-scaling-stroke"
      />
    </svg>
  );
}

/** Drawn over the Review stage while the fast lane is on: work goes from Code to You. */
function Bypass() {
  return (
    <motion.svg
      viewBox="0 0 300 40"
      preserveAspectRatio="none"
      className="pointer-events-none absolute -top-9 left-1/2 h-10 w-[290%] -translate-x-1/2"
      initial={{ opacity: 0 }}
      animate={{ opacity: 1 }}
      exit={{ opacity: 0 }}
      aria-hidden
    >
      <defs>
        <linearGradient id="bypass" gradientUnits="userSpaceOnUse" x1="0" x2="300" y1="0" y2="0">
          <stop offset="0%" style={{ stopColor: "var(--coder)" }} />
          <stop offset="100%" style={{ stopColor: "var(--human)" }} />
        </linearGradient>
      </defs>
      <path
        d="M20 38 C 90 2, 210 2, 280 38"
        fill="none"
        stroke="url(#bypass)"
        strokeWidth="2.6"
        className="anim-flow"
        style={{ animationDuration: "0.6s" }}
        vectorEffect="non-scaling-stroke"
      />
    </motion.svg>
  );
}

export function PipelineFlow({
  tickets,
  agents,
  fastLane = false,
}: {
  tickets: Ticket[];
  agents: Agent[];
  fastLane?: boolean;
}) {
  const busyRoles = new Set(agents.filter((a) => a.state === "busy").map((a) => a.role));
  const count = (statuses: readonly string[]) => tickets.filter((t) => statuses.includes(t.status)).length;
  return (
    <div className={fastLane ? "overflow-x-auto pt-11" : "overflow-x-auto pt-3"}>
      <div className="grid min-w-[620px] grid-cols-[repeat(4,minmax(0,1fr)_minmax(24px,0.5fr))_minmax(0,1fr)] items-center">
        {STAGES.map((stage, i) => {
          const skipped = fastLane && stage.id === "review";
          const active = busyRoles.has(stage.role) && !skipped;
          const n = count(stage.statuses);
          const next = STAGES[i + 1];
          return (
            <div key={stage.id} className="contents">
              <div className="relative">
              <AnimatePresence>{skipped && <Bypass />}</AnimatePresence>
              <Link
                href="/pipeline/"
                className="group flex flex-col items-center gap-2 text-center transition-opacity"
                style={{ opacity: skipped ? 0.4 : 1 }}
              >
                <motion.span
                  className="relative grid size-14 place-items-center rounded-2xl border"
                  style={{
                    color: stage.color,
                    borderColor: `color-mix(in srgb, ${stage.color} ${active ? 60 : 30}%, transparent)`,
                    background: `color-mix(in srgb, ${stage.color} ${active ? 18 : 8}%, var(--panel))`,
                  }}
                  animate={active ? { boxShadow: [`0 0 0px 0px ${stage.color}`, `0 0 28px -2px ${stage.color}`, `0 0 0px 0px ${stage.color}`] } : { boxShadow: "0 0 0 0 transparent" }}
                  transition={active ? { duration: 2.2, repeat: Infinity, ease: "easeInOut" } : { duration: 0.3 }}
                  whileHover={{ y: -2 }}
                >
                  <StageIcon id={stage.id} />
                  {n > 0 && (
                    <span
                      className="absolute -right-2 -top-2 grid min-w-5 place-items-center rounded-full px-1.5 font-mono text-[11px] font-semibold text-[var(--bg)]"
                      style={{ background: stage.color }}
                    >
                      <AnimatedNumber value={n} />
                    </span>
                  )}
                </motion.span>
                <span className="font-display text-sm font-semibold">{stage.label}</span>
                <span className="text-[11px] text-faint">{skipped ? "skipped: fast lane" : stage.hint}</span>
              </Link>
              </div>
              {next && (
                <Connector
                  id={stage.id}
                  from={stage.color}
                  to={next.color}
                  active={busyRoles.has(next.role) && !(fastLane && (stage.id === "code" || stage.id === "review"))}
                />
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}
