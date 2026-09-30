import type { Metadata } from "next";
import type { ReactNode } from "react";
import { Nav } from "@/components/nav";
import { Providers } from "./providers";
import "./globals.css";

export const metadata: Metadata = {
  title: "CodeIt",
  description: "Agents, pipeline, runs and budget for the CodeIt orchestrator.",
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en">
      <body>
        <Providers>
          <Nav />
          <main className="mx-auto flex max-w-6xl flex-col gap-6 px-4 py-6">{children}</main>
        </Providers>
      </body>
    </html>
  );
}
