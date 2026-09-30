"use client";

import { useQueryClient } from "@tanstack/react-query";
import { motion } from "motion/react";
import { useRouter } from "next/navigation";
import { useState, type FormEvent } from "react";
import { Button } from "@/components/ui/button";
import { LogoMark } from "@/components/logo";
import { Card, CardBody } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { api, ApiError } from "@/lib/api";

export default function LoginPage() {
  const [token, setToken] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const router = useRouter();
  const client = useQueryClient();

  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError("");
    try {
      await api.post("/api/login", { token: token.trim() });
      client.clear();
      router.replace("/");
    } catch (err) {
      setError(err instanceof ApiError && err.status === 401 ? "That token is not right." : String(err));
      setBusy(false);
    }
  }

  return (
    <div className="mx-auto mt-[12vh] flex w-full max-w-sm flex-col items-center gap-6">
      <motion.div
        initial={{ opacity: 0, scale: 0.8, rotate: -20 }}
        animate={{ opacity: 1, scale: 1, rotate: 0 }}
        transition={{ type: "spring", stiffness: 160, damping: 16 }}
        className="relative"
      >
        <div
          className="anim-breathe absolute inset-[-24px] rounded-full blur-2xl"
          style={{
            background:
              "conic-gradient(from 200deg, color-mix(in srgb, var(--planner) 45%, transparent), color-mix(in srgb, var(--coder) 45%, transparent), color-mix(in srgb, var(--reviewer) 45%, transparent), color-mix(in srgb, var(--planner) 45%, transparent))",
          }}
        />
        <LogoMark size={96} live className="relative" />
      </motion.div>
      <motion.div
        initial={{ opacity: 0, y: 10 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ delay: 0.15, duration: 0.5 }}
        className="text-center"
      >
        <h1 className="font-display text-3xl font-semibold tracking-tight">
          code<span className="text-accent">it</span>
        </h1>
        <p className="text-muted">Plan it. Code it. Review it. Ship it.</p>
      </motion.div>
      <motion.div
        initial={{ opacity: 0, y: 14 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ delay: 0.25, duration: 0.5 }}
        className="w-full"
      >
        <Card>
          <CardBody className="flex flex-col gap-4">
            <p className="text-sm text-muted">
              Paste <code className="rounded bg-code px-1 font-mono text-xs">CODEIT_API_TOKEN</code> from your{" "}
              <code className="rounded bg-code px-1 font-mono text-xs">.env</code>. This browser stays logged in for 30 days.
            </p>
            <form onSubmit={submit} className="flex flex-col gap-3">
              <label htmlFor="token" className="text-xs font-medium text-muted">
                API token
              </label>
              <Input
                id="token"
                type="password"
                autoComplete="current-password"
                value={token}
                onChange={(e) => setToken(e.target.value)}
                required
              />
              {error && (
                <motion.p initial={{ x: -6 }} animate={{ x: [6, -4, 2, 0] }} className="text-sm text-bad">
                  {error}
                </motion.p>
              )}
              <Button type="submit" variant="primary" disabled={busy || !token.trim()}>
                {busy ? "Checking" : "Log in"}
              </Button>
            </form>
          </CardBody>
        </Card>
      </motion.div>
    </div>
  );
}
