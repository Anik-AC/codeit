"use client";

import type { AgentState } from "@/lib/types";
import { roleColor } from "@/lib/roles";
import { cn } from "@/lib/utils";

/*
 * An agent's face. Each role has its own glyph that comes alive while it works:
 * the Coder's terminal types, the Reviewer's lens sweeps a page, the Planner's cards stack.
 * Busy agents get a spinning ring in their colour; idle ones breathe; parked ones show a
 * moon (they are waiting for the night-time run window or a usage limit to reset).
 */

function CoderGlyph({ busy }: { busy: boolean }) {
  const lines = [16, 11, 14];
  return (
    <svg viewBox="0 0 28 28" className="size-7" aria-hidden>
      <rect x="2.5" y="4" width="23" height="20" rx="4" fill="none" stroke="currentColor" strokeWidth="1.8" />
      <path d="M2.5 9 H25.5" stroke="currentColor" strokeWidth="1.4" opacity="0.5" />
      <path d="M6.5 13 l2.2 2 -2.2 2" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" />
      {lines.map((w, i) => (
        <rect
          key={i}
          x="10.5"
          y={12.2 + i * 3.4}
          width={w - 4}
          height="1.8"
          rx="0.9"
          fill="currentColor"
          opacity={busy ? 0.9 : 0.45}
          style={
            busy
              ? { transformOrigin: "left center", transformBox: "fill-box", animation: `type 1.8s ease-out ${i * 0.45}s infinite` }
              : undefined
          }
        />
      ))}
      {busy && <rect x="21" y="18.6" width="1.8" height="3.2" fill="currentColor" className="anim-blink" />}
    </svg>
  );
}

function ReviewerGlyph({ busy }: { busy: boolean }) {
  return (
    <svg viewBox="0 0 28 28" className="size-7 overflow-visible" aria-hidden>
      <rect x="4" y="3" width="15" height="20" rx="3" fill="none" stroke="currentColor" strokeWidth="1.8" />
      {[8, 11.5, 15].map((y) => (
        <rect key={y} x="7.2" y={y} width="8.6" height="1.6" rx="0.8" fill="currentColor" opacity="0.45" />
      ))}
      {busy && (
        <rect x="5" y="6" width="13" height="1.4" rx="0.7" fill="currentColor" opacity="0.8" style={{ animation: "scan 2.2s ease-in-out infinite" }} />
      )}
      <g style={busy ? { animation: "sweep 2.2s ease-in-out infinite", transformBox: "fill-box", transformOrigin: "center" } : undefined}>
        <circle cx="18.5" cy="17.5" r="5" fill="var(--panel)" stroke="currentColor" strokeWidth="2" />
        <path d="M22.2 21.2 L25.5 24.5" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" />
      </g>
    </svg>
  );
}

function PlannerGlyph({ busy }: { busy: boolean }) {
  return (
    <svg viewBox="0 0 28 28" className="size-7" aria-hidden>
      {[0, 1, 2].map((i) => (
        <rect
          key={i}
          x={5 + i * 1.5}
          y={5 + i * 5.5}
          width="17"
          height="4.2"
          rx="1.6"
          fill="none"
          stroke="currentColor"
          strokeWidth="1.7"
          style={busy ? { animation: `breathe 1.8s ease-in-out ${i * 0.3}s infinite`, transformBox: "fill-box", transformOrigin: "center" } : undefined}
        />
      ))}
    </svg>
  );
}

function Moon() {
  return (
    <svg viewBox="0 0 16 16" className="anim-float absolute -right-1 -top-1 size-4" aria-hidden>
      <circle cx="8" cy="8" r="7.5" fill="var(--panel)" />
      <path d="M10.8 11.2 A5 5 0 1 1 6.2 3.4 A4 4 0 0 0 10.8 11.2 Z" fill="var(--warn)" />
    </svg>
  );
}

export function AgentAvatar({ role, state, size = 56 }: { role: string; state: AgentState; size?: number }) {
  const color = roleColor(role);
  const busy = state === "busy";
  const Glyph = role === "coder" ? CoderGlyph : role === "reviewer" ? ReviewerGlyph : PlannerGlyph;
  return (
    <div
      className={cn("relative grid shrink-0 place-items-center", state === "disabled" && "opacity-35 grayscale")}
      style={{ width: size, height: size, color }}
    >
      {busy && (
        <span
          className="anim-spin absolute inset-0 rounded-full"
          style={{
            background: `conic-gradient(from 0deg, transparent 0deg, ${color} 90deg, transparent 180deg, transparent 360deg)`,
            mask: "radial-gradient(farthest-side, transparent calc(100% - 2.5px), black calc(100% - 2px))",
            WebkitMask: "radial-gradient(farthest-side, transparent calc(100% - 2.5px), black calc(100% - 2px))",
          }}
        />
      )}
      <span
        className={cn("absolute inset-[4px] rounded-full border", state === "idle" && "anim-breathe")}
        style={{
          borderColor: `color-mix(in srgb, ${color} ${busy ? 55 : 28}%, transparent)`,
          background: `radial-gradient(circle at 35% 30%, color-mix(in srgb, ${color} ${busy ? 26 : 14}%, transparent), color-mix(in srgb, ${color} 4%, var(--panel)) 70%)`,
          boxShadow: busy ? `0 0 24px -4px ${color}` : undefined,
        }}
      />
      <span className="relative">
        <Glyph busy={busy} />
      </span>
      {state === "parked" && <Moon />}
    </div>
  );
}
