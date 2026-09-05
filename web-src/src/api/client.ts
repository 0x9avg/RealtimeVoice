// Small hand-written client — only the 5 endpoints app/api_routes.py
// actually exposes. Open API, no auth (item 4 of the 2026-09-04 fixes list
// removed the whole ACCESS_TOKEN mechanism) — this used to also carry a
// Bearer token and a 401 -> "show the token prompt" flow, ported from
// ../../humanoid-robot/web/dashboard's api/client.ts; neither applies here
// anymore.

async function fetchJson<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, init);
  if (!response.ok) {
    throw new Error(`${init?.method ?? "GET"} ${path} -> HTTP ${response.status}`);
  }
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

export interface WallCommandBody {
  kind: "open_section" | "open_category" | "navigate";
  section?: string;
  category?: string;
  nav?: string;
}

export interface WallCommandResult {
  outcome: "accepted" | "rejected" | "unreachable";
  detail: string;
}

export interface WallHealth {
  status: string;
  driver: string;
}

export interface VisitRecord {
  id: string;
  ts: string;
  status: "new" | "processed";
  full_name: string;
  organization: string;
  purpose: string;
  destination: string;
  has_pass: boolean;
  has_id: boolean;
}

export const api = {
  profile: () => fetchJson<{ profile: string }>("/api/profile"),

  wallHealth: () => fetchJson<WallHealth>("/api/wall/health"),

  wallCommand: (body: WallCommandBody) =>
    fetchJson<WallCommandResult>("/api/wall/command", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }),

  visitsList: (status?: "new" | "processed") =>
    fetchJson<{ records: VisitRecord[] }>(`/api/visits${status ? `?status=${status}` : ""}`),

  visitMarkProcessed: (id: string) =>
    fetchJson<{ id: string; status: string }>(`/api/visits/${id}/processed`, {
      method: "POST",
    }),
};
