"use client";

import { TooltipProvider } from "@scout/design-system";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { useState } from "react";
import { Toaster } from "sonner";

import { ApiError } from "@/lib/api";
import { useTheme } from "@/lib/theme";

export function Providers({ children }: { children: React.ReactNode }) {
  const [, , theme] = useTheme();
  const [client] = useState(
    () =>
      new QueryClient({
        defaultOptions: {
          queries: {
            refetchOnWindowFocus: false,
            retry: (count, err) => !(err instanceof ApiError && err.status < 500) && count < 2,
          },
        },
      }),
  );
  return (
    <QueryClientProvider client={client}>
      <TooltipProvider delayDuration={350} skipDelayDuration={150}>
        {children}
        <Toaster
          position="bottom-left"
          theme={theme}
          toastOptions={{
            className: "!bg-surface-3 !text-fg !border-0 !shadow-popover !rounded-md !text-body !font-sans",
          }}
        />
      </TooltipProvider>
    </QueryClientProvider>
  );
}
