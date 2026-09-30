"use client";

import { useQueryClient } from "@tanstack/react-query";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { api } from "@/lib/api";
import { useLiveStatus } from "@/lib/live";
import { cn } from "@/lib/utils";

const links = [
  { href: "/", label: "Agents" },
  { href: "/pipeline/", label: "Pipeline" },
  { href: "/runs/", label: "Runs" },
  { href: "/budget/", label: "Budget" },
];

function LiveDot() {
  const status = useLiveStatus();
  const label = { live: "Live", connecting: "Connecting", offline: "Offline" }[status];
  return (
    <span className="flex items-center gap-2 text-xs text-muted" aria-live="polite">
      <span
        className={cn(
          "size-2 rounded-full",
          status === "live" ? "bg-ok" : status === "connecting" ? "bg-warn" : "bg-bad",
        )}
      />
      {label}
    </span>
  );
}

export function Nav() {
  const path = usePathname();
  const router = useRouter();
  const client = useQueryClient();
  if (path.startsWith("/login")) return null;
  const active = (href: string) => (href === "/" ? path === "/" : path.startsWith(href.replace(/\/$/, "")));
  return (
    <header className="border-b border-line bg-panel">
      <div className="mx-auto flex max-w-6xl flex-wrap items-center gap-x-6 gap-y-2 px-4 py-3">
        <Link href="/" className="font-mono text-sm font-semibold tracking-tight">
          codeit
        </Link>
        <nav className="flex flex-wrap gap-1">
          {links.map((l) => (
            <Link
              key={l.href}
              href={l.href}
              className={cn(
                "rounded-md px-3 py-1.5 text-sm",
                active(l.href) ? "bg-accent-soft text-accent" : "text-muted hover:text-ink",
              )}
            >
              {l.label}
            </Link>
          ))}
        </nav>
        <div className="ml-auto flex items-center gap-4">
          <LiveDot />
          <button
            className="text-xs text-muted hover:text-ink"
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
