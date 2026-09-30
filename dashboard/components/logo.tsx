"use client";

import { useReducedMotion } from "motion/react";
import { useId } from "react";
import { cn } from "@/lib/utils";

/*
 * The CodeIt mark: a "C" drawn as three arcs, one per agent in pipeline order (Planner
 * across the top, Coder down the left, Reviewer along the bottom), closing into a check
 * mark: planned, coded, reviewed, done. When `live`, a spark travels the arc and the three
 * nodes pulse in turn.
 */
const ARCS = {
  planner: "M36.26 13.72 A16 16 0 0 0 13.02 12.35",
  coder: "M13.02 12.35 A16 16 0 0 0 13.02 35.65",
  reviewer: "M13.02 35.65 A16 16 0 0 0 36.26 34.28",
};
const PATH = "M36.26 13.72 A16 16 0 1 0 36.26 34.28";
const NODES: [number, number, string][] = [
  [13.02, 12.35, "var(--planner)"],
  [13.02, 35.65, "var(--coder)"],
  [36.26, 34.28, "var(--reviewer)"],
];

export function LogoMark({ size = 32, live = false, className }: { size?: number; live?: boolean; className?: string }) {
  const reduce = useReducedMotion();
  const id = useId();
  const animate = live && !reduce;
  return (
    <svg
      viewBox="0 0 48 48"
      width={size}
      height={size}
      className={className}
      role="img"
      aria-label="CodeIt"
    >
      <defs>
        <filter id={`${id}-glow`} x="-50%" y="-50%" width="200%" height="200%">
          <feGaussianBlur stdDeviation="1.6" result="blur" />
          <feMerge>
            <feMergeNode in="blur" />
            <feMergeNode in="SourceGraphic" />
          </feMerge>
        </filter>
      </defs>
      <path d={ARCS.planner} fill="none" stroke="var(--planner)" strokeWidth={4.2} strokeLinecap="round" />
      <path d={ARCS.coder} fill="none" stroke="var(--coder)" strokeWidth={4.2} strokeLinecap="round" />
      <path d={ARCS.reviewer} fill="none" stroke="var(--reviewer)" strokeWidth={4.2} strokeLinecap="round" />
      {NODES.map(([cx, cy, fill], i) => (
        <circle
          key={fill}
          cx={cx}
          cy={cy}
          r={3.1}
          fill="var(--panel)"
          stroke={fill}
          strokeWidth={2.2}
          style={animate ? { animation: `breathe 2.4s ease-in-out ${i * 0.8}s infinite`, transformOrigin: `${cx}px ${cy}px` } : undefined}
        />
      ))}
      <path
        d="M27.5 24.2 L32 28.8 L41 18.6"
        fill="none"
        stroke="var(--ink)"
        strokeWidth={4.2}
        strokeLinecap="round"
        strokeLinejoin="round"
      />
      {animate && (
        <circle r={2.2} fill="var(--ink)" filter={`url(#${id}-glow)`}>
          <animateMotion dur="3.2s" repeatCount="indefinite" path={PATH} />
        </circle>
      )}
    </svg>
  );
}

export function Logo({ live = false, className }: { live?: boolean; className?: string }) {
  return (
    <span className={cn("inline-flex items-center gap-2.5", className)}>
      <LogoMark live={live} />
      <span className="font-display text-[1.35rem] font-semibold leading-none tracking-tight">
        code<span className="text-accent">it</span>
      </span>
    </span>
  );
}
