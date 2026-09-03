// Small hand-written client — only the 5 endpoints app/api_routes.py
// actually exposes. Shaped after ../../humanoid-robot/web/dashboard's
// api/client.ts (getAuthToken/setAuthToken/onUnauthorized + a fetch wrapper
// that fires onUnauthorized on 401), but that file is 521 lines covering a
// dozen unrelated subsystems — this is the part worth keeping, rewritten
// small.

const TOKEN_KEY = "realtimevoice.auth.token";

let cachedToken: string | null = localStorage.getItem(TOKEN_KEY);
const unauthorizedListeners = new Set<() => void>();

export function getAuthToken(): string | null {
  return cachedToken;
}

export function setAuthToken(token: string | null): void {
  cachedToken = token;
  if (token) {
    localStorage.setItem(TOKEN_KEY, token);
  } else {
    localStorage.removeItem(TOKEN_KEY);
  }
}

export function onUnauthorized(listener: () => void): () => void {
  unauthorizedListeners.add(listener);
  return () => unauthorizedListeners.delete(listener);
}

async function fetchJson<T>(path: string, init?: RequestInit): Promise<T> {
  const headers = new Headers(init?.headers);
  if (cachedToken) headers.set("Authorization", `Bearer ${cachedToken}`);
  const response = await fetch(path, { ...init, headers });
  if (response.status === 401) {
    unauthorizedListeners.forEach((listener) => listener());
    throw new Error("unauthorized");
  }
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
