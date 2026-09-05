import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import React from "react";
import ReactDOM from "react-dom/client";
import { BrowserRouter } from "react-router-dom";

import { App } from "./App";
import "./index.css";

// Trimmed from ../../humanoid-robot/web/dashboard/src/main.tsx: no
// EventStreamProvider/BusToastBridge/ToastProvider/WatchdogHeartbeat — those
// all exist for that backend's NATS-style event bus and safety heartbeat,
// neither of which this project has. AuthPrompt (the token-gate for
// /api/visits*, /api/wall/command) is also gone — item 4 of the 2026-09-04
// fixes list made this an open API with no token at all.

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
    </QueryClientProvider>
  </React.StrictMode>,
);
