import React from "react";
import ReactDOM from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import "./globals.css";
import { App } from "./App";

// One client for the whole renderer. The sidecar runs in the same process
// graph so we don't need fancy retries — the network is loopback and either
// up or down. staleTime: 30s matches the cadence at which AgentPanel /
// Settings mutations should bubble back to the conversation list.
const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 30_000,
      retry: 1,
      refetchOnWindowFocus: true,
    },
  },
});

const root = document.getElementById("root");
if (!root) throw new Error("#root not found");

ReactDOM.createRoot(root).render(
  <React.StrictMode>
    <QueryClientProvider client={queryClient}>
      <App />
    </QueryClientProvider>
  </React.StrictMode>,
);
