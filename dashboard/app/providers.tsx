"use client";

import { MutationCache, QueryCache, QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MotionConfig } from "motion/react";
import { usePathname, useRouter } from "next/navigation";
import { useState, type ReactNode } from "react";
import { ApiError } from "@/lib/api";
import { LiveProvider } from "@/lib/live";

export function Providers({ children }: { children: ReactNode }) {
  const router = useRouter();
  const [client] = useState(() => {
    // Not logged in (or the token changed): go to the login page.
    const onError = (error: Error) => {
      if (error instanceof ApiError && error.status === 401) router.replace("/login/");
    };
    return new QueryClient({
      queryCache: new QueryCache({ onError }),
      mutationCache: new MutationCache({ onError }),
      defaultOptions: { queries: { staleTime: 10_000, retry: (n, e) => !(e instanceof ApiError) && n < 1 } },
    });
  });
  const onLogin = usePathname().startsWith("/login");
  return (
    <QueryClientProvider client={client}>
      <MotionConfig reducedMotion="user">
        {onLogin ? children : <LiveProvider>{children}</LiveProvider>}
      </MotionConfig>
    </QueryClientProvider>
  );
}
