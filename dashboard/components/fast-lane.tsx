"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AnimatePresence, motion } from "motion/react";
import { useState } from "react";
import { Button } from "@/components/ui/button";
import { api, ApiError } from "@/lib/api";
import type { AgentsResponse, RuntimeSettings } from "@/lib/types";
import { cn } from "@/lib/utils";

/** The fast-lane state, from the live agents feed. */
export function useFastLane(): boolean {
  const { data } = useQuery({ queryKey: ["agents"], queryFn: () => api.get<AgentsResponse>("/api/agents") });
  return data?.fast_lane ?? false;
}

function Bolt({ className }: { className?: string }) {
  return (
    <svg viewBox="0 0 24 24" className={className} aria-hidden>
      <path d="M13.5 2 4.5 13.5h6L9.5 22l9-12h-6l1-8Z" fill="currentColor" />
    </svg>
  );
}

/** A small chip for the nav bar while the fast lane is on. */
export function FastLaneChip() {
  const on = useFastLane();
  return (
    <AnimatePresence>
      {on && (
        <motion.span
          initial={{ opacity: 0, scale: 0.8 }}
          animate={{ opacity: 1, scale: 1 }}
          exit={{ opacity: 0, scale: 0.8 }}
          className="inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-[11px] font-medium"
          style={{ color: "var(--coder)", borderColor: "color-mix(in srgb, var(--coder) 40%, transparent)" }}
          title="Fast lane: the Reviewer agent is skipped"
        >
          <Bolt className="anim-breathe size-3" />
          Fast lane
        </motion.span>
      )}
    </AnimatePresence>
  );
}

/** The switch. Turning it on asks once, since it changes how every ticket is reviewed. */
export function FastLaneControl() {
  const client = useQueryClient();
  const on = useFastLane();
  const [confirming, setConfirming] = useState(false);
  const [error, setError] = useState("");
  const change = useMutation({
    mutationFn: (value: boolean) => api.patch<RuntimeSettings>("/api/settings", { fast_lane: value }),
    onMutate: (value) => {
      setError("");
      client.setQueryData<AgentsResponse>(["agents"], (old) => (old ? { ...old, fast_lane: value } : old));
    },
    onSuccess: (s) => client.setQueryData<AgentsResponse>(["agents"], (old) => (old ? { ...old, fast_lane: s.fast_lane } : old)),
    onError: (e) => {
      setError(e instanceof ApiError ? e.message : String(e));
      void client.invalidateQueries({ queryKey: ["agents"] });
    },
    onSettled: () => setConfirming(false),
  });

  function toggle() {
    if (on) change.mutate(false);
    else setConfirming(true);
  }

  return (
    <motion.section
      layout
      className={cn("surface relative overflow-hidden px-5 py-4")}
      style={on ? { borderColor: "color-mix(in srgb, var(--coder) 45%, var(--line))" } : undefined}
    >
      {on && (
        <span
          className="anim-shimmer pointer-events-none absolute inset-0 opacity-40"
          style={{ color: "var(--coder)" }}
        />
      )}
      <div className="relative flex flex-wrap items-center gap-4">
        <span
          className="grid size-10 shrink-0 place-items-center rounded-xl border"
          style={{
            color: on ? "var(--coder)" : "var(--muted)",
            borderColor: on ? "color-mix(in srgb, var(--coder) 45%, transparent)" : "var(--line)",
            background: on ? "color-mix(in srgb, var(--coder) 14%, var(--panel))" : "var(--panel-2)",
          }}
        >
          <Bolt className={cn("size-5", on && "anim-breathe")} />
        </span>
        <div className="min-w-0 flex-1">
          <h2 className="font-display text-[15px] font-semibold">Fast lane</h2>
          <p className="text-sm text-muted">
            {on
              ? "On: pull requests skip the Reviewer agent and go straight to you. CI still has to pass before you can merge."
              : "Off: every pull request is checked by the Reviewer agent before it reaches you."}
          </p>
        </div>
        <button
          type="button"
          role="switch"
          aria-checked={on}
          aria-label="Fast lane"
          disabled={change.isPending}
          onClick={toggle}
          className={cn(
            "relative h-7 w-12 shrink-0 rounded-full border transition-colors focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent disabled:opacity-60",
            on ? "border-transparent" : "border-line bg-panel-2",
          )}
          style={on ? { background: "var(--coder)" } : undefined}
        >
          <motion.span
            className="absolute top-0.5 size-5.5 rounded-full shadow"
            style={{ background: on ? "var(--accent-ink)" : "var(--muted)", width: 22, height: 22 }}
            animate={{ left: on ? 24 : 2 }}
            transition={{ type: "spring", stiffness: 520, damping: 32 }}
          />
        </button>
      </div>
      <AnimatePresence>
        {confirming && (
          <motion.div
            initial={{ opacity: 0, height: 0 }}
            animate={{ opacity: 1, height: "auto" }}
            exit={{ opacity: 0, height: 0 }}
            className="relative overflow-hidden"
          >
            <div className="mt-4 flex flex-wrap items-center gap-3 rounded-xl border border-line bg-panel-2 px-4 py-3 text-sm">
              <span className="min-w-0 flex-1">
                Skip agent review for every ticket until you turn this off? Tickets waiting in Agent Review move to
                you now, labelled <code className="font-mono text-xs">fast-lane</code>.
              </span>
              <Button variant="ghost" onClick={() => setConfirming(false)}>
                Cancel
              </Button>
              <Button variant="primary" onClick={() => change.mutate(true)} disabled={change.isPending}>
                Turn on
              </Button>
            </div>
          </motion.div>
        )}
      </AnimatePresence>
      {error && <p className="relative mt-2 text-sm text-bad">{error}</p>}
    </motion.section>
  );
}
