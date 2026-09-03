import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import React from "react";
import ReactDOM from "react-dom/client";
import { BrowserRouter } from "react-router-dom";

import { App } from "./App";
import { AuthPrompt } from "./lib/authPrompt";
import "./index.css";

// Trimmed from ../../humanoid-robot/web/dashboard/src/main.tsx: no
// EventStreamProvider/BusToastBridge/ToastProvider/WatchdogHeartbeat — those
// all exist for that backend's NATS-style event bus and safety heartbeat,
// neither of which this project has. QueryClientProvider + AuthPrompt
// (the token-gate for /api/visits*, /api/wall/command) are what's actually
// load-bearing here.

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 15_000,
      refetchOnWindowFocus: false,
    },
  },
});

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <App />
      </BrowserRouter>
      <AuthPrompt />
    </QueryClientProvider>
  </React.StrictMode>,
);
