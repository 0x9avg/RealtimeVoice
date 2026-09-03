import { useEffect, useState } from "react";
import { KeyRound, LogOut } from "lucide-react";

import { getAuthToken, onUnauthorized, setAuthToken } from "../api/client";

// Ported near-verbatim from
// ../../humanoid-robot/web/dashboard/src/lib/authPrompt.tsx — global "the
// API just returned 401" listener + a modal that lets the operator paste in
// a bearer token. Renders inline in main.tsx so it covers every page
// without each page having to opt in. G1 ТЗ §7: admin functions
// (/api/visits*, /api/wall/command) must be authorized-only; if the server
// has no ACCESS_TOKEN configured (app/auth.py), every request just succeeds
// and this stays out of the way.

export function AuthPrompt() {
  const [open, setOpen] = useState<boolean>(() => getAuthToken() === null);
  const [draft, setDraft] = useState<string>("");
  const [existing, setExisting] = useState<string | null>(() => getAuthToken());

  useEffect(() => onUnauthorized(() => setOpen(true)), []);

  const save = () => {
    const trimmed = draft.trim();
    setAuthToken(trimmed || null);
    setExisting(trimmed || null);
    setOpen(false);
    setDraft("");
    // Reload once so every query re-fires with the fresh token; simpler
    // than plumbing invalidation into every hook.
    window.location.reload();
  };

  const clear = () => {
    setAuthToken(null);
    setExisting(null);
    setDraft("");
    window.location.reload();
  };

  if (!open && existing) {
    return (
      <button
        type="button"
        onClick={() => setOpen(true)}
        title="Токен сохранён — нажмите, чтобы сменить или очистить"
        className="fixed bottom-3 right-3 z-40 flex items-center gap-1.5 rounded-full border border-border bg-background/80 px-2.5 py-1 text-[10px] uppercase tracking-wide text-muted-foreground shadow backdrop-blur hover:bg-accent hover:text-accent-foreground"
      >
        <KeyRound className="h-3 w-3" />
        токен
      </button>
    );
  }
  if (!open) {
    return (
      <button
        type="button"
        onClick={() => setOpen(true)}
        className="fixed bottom-3 right-3 z-40 rounded-full border border-border bg-background/80 px-3 py-1 text-xs text-muted-foreground shadow backdrop-blur hover:bg-accent hover:text-accent-foreground"
      >
        Войти
      </button>
    );
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-background/80 backdrop-blur">
      <div className="w-full max-w-md rounded-lg border border-border bg-background p-6 shadow-2xl">
        <div className="mb-4 flex items-center gap-2 text-sm font-semibold">
          <KeyRound className="h-4 w-4" />
          Токен доступа
        </div>
        <p className="mb-4 text-xs text-muted-foreground">
          Нужен, только если на сервере задан <code>ACCESS_TOKEN</code>. Хранится в{" "}
          <code>localStorage</code> и переиспользуется между заходами. Оставьте пустым, если
          сервер не требует токен.
        </p>
        <input
          type="password"
          autoFocus
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          placeholder={existing ? "(токен сохранён)" : "токен"}
          className="mb-4 w-full rounded-md border border-border bg-background/60 px-3 py-2 text-sm outline-none focus:border-primary"
          onKeyDown={(e) => {
            if (e.key === "Enter") save();
          }}
        />
        <div className="flex items-center justify-end gap-2">
          {existing && (
            <button
              type="button"
              onClick={clear}
              className="inline-flex items-center gap-1 rounded-md border border-border px-3 py-1 text-xs text-muted-foreground hover:bg-accent hover:text-accent-foreground"
            >
              <LogOut className="h-3 w-3" />
              очистить
            </button>
          )}
          <button
            type="button"
            onClick={() => setOpen(false)}
            className="rounded-md border border-border px-3 py-1 text-xs text-muted-foreground hover:bg-accent hover:text-accent-foreground"
          >
            отмена
          </button>
          <button
            type="button"
            onClick={save}
            className="rounded-md bg-primary px-3 py-1 text-xs text-primary-foreground"
          >
            сохранить
          </button>
        </div>
      </div>
    </div>
  );
}
