import type { NextConfig } from "next";

// The dashboard is exported as static files and served by the CodeIt API (`codeit up`), so
// the browser talks to one origin and the session cookie just works. In `next dev`, API
// calls are proxied to the API instead.
const api = process.env.CODEIT_API_URL ?? "http://127.0.0.1:8770";
const dev = process.env.NODE_ENV === "development";

const config: NextConfig = dev
  ? { async rewrites() { return [{ source: "/api/:path*", destination: `${api}/api/:path*` }]; } }
  : { output: "export", trailingSlash: true, images: { unoptimized: true } };

export default config;
