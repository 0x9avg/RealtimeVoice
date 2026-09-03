import { useQuery } from "@tanstack/react-query";
import { CheckCircle2, LayoutGrid, Loader2, MonitorPlay, XCircle } from "lucide-react";

import { api } from "../api/client";
import { MotionButton } from "../components/MotionButton";
import { cn } from "../lib/cn";
import { WALL_CATEGORIES, WALL_SECTIONS } from "../lib/labels";
import { useWallCommand } from "../lib/useWallCommand";

// Ported from ../../humanoid-robot/web/dashboard/src/pages/WallPage.tsx —
// JSX/layout kept close to the original; only the data hook underneath
// changed (see useWallCommand.ts) because our wall-command endpoint answers
// synchronously instead of needing a bus-event wait.
//
// No "Главное меню"/next-prev-section/next-prev-slide buttons: the real
// Factories.exe app has no way to step through sections or slides
// forward/back at all (cut from app/wall/commands.py's WallNavAction too,
// not just here), and the main-menu button was dropped from this page on
// request — main_menu is still reachable by voice, just not from a button.
export function WallPage() {
  const { status, send } = useWallCommand();
  const health = useQuery({
    queryKey: ["wall-health"],
    queryFn: api.wallHealth,
    refetchInterval: 15_000,
  });

  const busy = status.phase === "sending";
  const openSection = (section: string) => void send({ kind: "open_section", section });
  const openCategory = (category: string) => void send({ kind: "open_category", category });

  const reachable = health.data?.status === "ok";

  return (
    <div className="mx-auto h-full w-full max-w-lg space-y-4 overflow-y-auto p-4">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-semibold">Видеостена</h1>
        <div
          className={cn(
            "flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-xs",
            reachable
              ? "border-emerald-500/40 bg-emerald-500/10 text-emerald-300"
              : "border-red-500/40 bg-red-500/10 text-red-300",
          )}
        >
          <MonitorPlay className="h-3.5 w-3.5" />
          {reachable ? `На связи (${health.data?.driver})` : "Нет связи"}
        </div>
      </div>

      {status.phase !== "idle" && (
        <div
          className={cn(
            "flex items-center gap-2 rounded-lg border px-3 py-2 text-sm",
            status.phase === "done"
              ? status.ok
                ? "border-emerald-500/40 bg-emerald-500/10 text-emerald-200"
                : "border-red-500/40 bg-red-500/10 text-red-200"
              : "border-border bg-background/60 text-muted-foreground",
          )}
        >
          {busy ? (
            <Loader2 className="h-4 w-4 shrink-0 animate-spin" />
          ) : status.ok ? (
            <CheckCircle2 className="h-4 w-4 shrink-0" />
          ) : (
            <XCircle className="h-4 w-4 shrink-0" />
          )}
          <span>{busy ? "Выполняю…" : status.message}</span>
        </div>
      )}

      {WALL_CATEGORIES.map(({ title, prefix }) => (
        <section key={prefix} className="space-y-2">
          <h2 className="text-sm font-medium text-muted-foreground">{title}</h2>
          <div className="grid grid-cols-2 gap-2">
            <MotionButton
              label={`Обзор: ${title}`}
              icon={LayoutGrid}
              busy={busy}
              onPress={() => openCategory(prefix.toLowerCase())}
            />
            {WALL_SECTIONS.filter((s) => s.key.startsWith(prefix)).map((s) => (
              <MotionButton
                key={s.key}
                label={s.label}
                busy={busy}
                onPress={() => openSection(s.key)}
              />
            ))}
          </div>
        </section>
      ))}
    </div>
  );
}
