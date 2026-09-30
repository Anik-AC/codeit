"use client";

import { useQueryClient } from "@tanstack/react-query";
import { motion } from "motion/react";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { Logo } from "@/components/logo";
import { api } from "@/lib/api";
import { useLiveStatus } from "@/lib/live";
import { cn } from "@/lib/utils";

const links = [
  { href: "/", label: "Agents" },
  { href: "/pipeline/", label: "Pipeline" },
  { href: "/runs/", label: "Runs" },
  { href: "/evals/", label: "Evals" },
  { href: "/budget/", label: "Budget" },
];

const statusColor = { live: "var(--ok)", connecting: "var(--warn)", offline: "var(--bad)" };

function LiveDot() {
  const status = useLiveStatus();
  const label = { live: "Live", connecting: "Connecting", offline: "Offline" }[status];
  return (
    <span className="flex items-center gap-2 text-xs text-muted" aria-live="polite">
      <span className="relative flex size-2.5">
        {status === "live" && (
          <span className="anim-ping absolute inline-flex size-full rounded-full" style={{ background: statusColor.live }} />
        )}
        <span className="relative inline-flex size-2.5 rounded-full" style={{ background: statusColor[status] }} />
      </span>
      {label}
    </span>
  );
}

export function Nav() {
  const path = usePathname();
  const router = useRouter();
  const client = useQueryClient();
  const status = useLiveStatus();
  if (path.startsWith("/login")) return null;
  const active = (href: string) => (href === "/" ? path === "/" : path.startsWith(href.replace(/\/$/, "")));
  return (
    <header className="sticky top-0 z-20 border-b border-line bg-[color-mix(in_srgb,var(--bg)_78%,transparent)] backdrop-blur-xl">
      <div className="mx-auto flex max-w-6xl flex-wrap items-center gap-x-6 gap-y-2 px-4 py-3">
        <Link href="/" aria-label="CodeIt home">
          <Logo live={status === "live"} />
        </Link>
        <nav className="flex flex-wrap gap-1">
          {links.map((l) => (
            <Link
              key={l.href}
              href={l.href}
              className={cn(
                "relative rounded-lg px-3 py-1.5 text-sm transition-colors",
                active(l.href) ? "text-ink" : "text-muted hover:text-ink",
              )}
            >
              {active(l.href) && (
                <motion.span
                  layoutId="nav-pill"
                  className="absolute inset-0 rounded-lg border border-line bg-panel-2"
                  transition={{ type: "spring", stiffness: 420, damping: 34 }}
                />
              )}
              <span className="relative">{l.label}</span>
            </Link>
          ))}
        </nav>
        <div className="ml-auto flex items-center gap-4">
          <LiveDot />
          <button
            className="text-xs text-muted transition hover:text-ink"
            onClick={async () => {
              await api.post("/api/logout", {});
              client.clear();
              router.replace("/login/");
            }}
          >
            Log out
          </button>
        </div>
      </div>
    </header>
  );
}
