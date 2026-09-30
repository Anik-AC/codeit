"use client";

import { useQueryClient } from "@tanstack/react-query";
import { useRouter } from "next/navigation";
import { useState, type FormEvent } from "react";
import { Button } from "@/components/ui/button";
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
    <div className="mx-auto mt-16 w-full max-w-sm">
      <Card>
        <CardBody className="flex flex-col gap-4">
          <div>
            <h1 className="font-mono text-lg font-semibold">codeit</h1>
            <p className="text-muted">
              Paste <code className="rounded bg-code px-1 font-mono text-xs">CODEIT_API_TOKEN</code> from your
              <code className="ml-1 rounded bg-code px-1 font-mono text-xs">.env</code>. You stay logged in on this
              browser for 30 days.
            </p>
          </div>
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
            {error && <p className="text-sm text-bad">{error}</p>}
            <Button type="submit" variant="primary" disabled={busy || !token.trim()}>
              {busy ? "Checking" : "Log in"}
            </Button>
          </form>
        </CardBody>
      </Card>
    </div>
  );
}
