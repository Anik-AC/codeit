# dashboard

The CodeIt web dashboard (PRD 14, ADR-0014): Next.js App Router, TypeScript (strict), Tailwind, TanStack Query.

It is exported as static files (`npm run build` writes `out/`) and served by the CodeIt API inside `codeit up`, so it talks to `/api` on the same origin. Build it from the repo root with `uv run codeit dashboard build`.

```bash
npm ci
npm run dev        # http://localhost:3000, /api proxied to http://127.0.0.1:8770 (CODEIT_API_URL)
npm run lint
npm run typecheck
npm run build
```

- `app/`: pages (Agents `/`, `/pipeline`, `/runs`, `/run?id=`, `/budget`, `/login`)
- `lib/live.tsx`: one `EventSource` on `/api/stream` that updates the query cache
- `components/transcript.tsx`: renders a Claude stream-json transcript as it streams
