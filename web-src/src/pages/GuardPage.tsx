import { useEffect, useRef } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { BadgeCheck, IdCard, Shield } from "lucide-react";

import { api, type VisitRecord } from "../api/client";
import { cn } from "../lib/cn";

// Ported from ../../humanoid-robot/web/dashboard/src/pages/GuardPage.tsx —
// VisitCard/Row/Flag/formatTime/chime() are close to verbatim. Two changes:
//
// 1. No "Оформить визит" button/mutation. The original's button calls
//    api.visitIntakeStart(), which works there because that backend's
//    guard-desk pipeline can speak a scripted greeting outside of any LLM
//    turn. Our voice pipeline is Gemini Live end-to-end — the model only
//    ever speaks text it gets back from a tool call *it* initiated (see
//    app/scenarios.py / app/g1_tools.py's VisitIntakeTool docstring) — there
//    is no channel to make it proactively greet a visitor from a REST call.
//    A button here would silently do nothing, so it's not built at all.
// 2. No event-bus subscription (this backend has no NATS-style bus) — the
//    "chime on newly-seen ids" effect below still works exactly like the
//    original's fallback path, just off a plain refetchInterval poll
//    instead of a bus-triggered invalidation.
export function GuardPage() {
  const queryClient = useQueryClient();
  const visits = useQuery({
    queryKey: ["visits"],
    queryFn: () => api.visitsList(),
    refetchInterval: 10_000,
  });
  const knownIds = useRef<Set<string> | null>(null);

  useEffect(() => {
    const records = visits.data?.records;
    if (!records) return;
    if (knownIds.current === null) {
      knownIds.current = new Set(records.map((r) => r.id));
      return;
    }
    const fresh = records.filter((r) => !knownIds.current?.has(r.id));
    if (fresh.length > 0) chime();
    knownIds.current = new Set(records.map((r) => r.id));
  }, [visits.data]);

  const processed = useMutation({
    mutationFn: (id: string) => api.visitMarkProcessed(id),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ["visits"] }),
  });

  const records = visits.data?.records ?? [];
  const fresh = records.filter((r) => r.status === "new");
  const done = records.filter((r) => r.status === "processed");

  return (
    <div className="mx-auto h-full w-full max-w-lg space-y-4 overflow-y-auto p-4">
      <h1 className="flex items-center gap-2 text-2xl font-semibold">
        <Shield className="h-6 w-6" /> Охрана
      </h1>

      <p className="text-xs text-muted-foreground">
        Робот опрашивает посетителя голосом (ФИО, организация, цель, к кому, пропуск,
        удостоверение) в ответ на «оформите визит» — готовая карточка появляется здесь.
      </p>

      <section className="space-y-3">
        <h2 className="text-xs uppercase tracking-wide text-muted-foreground">
          Новые ({fresh.length})
        </h2>
        {fresh.length === 0 && (
          <p className="rounded-xl border border-border bg-background/40 p-4 text-sm text-muted-foreground">
            Новых посетителей нет.
          </p>
        )}
        {fresh.map((v) => (
          <VisitCard key={v.id} visit={v} onProcessed={() => processed.mutate(v.id)} />
        ))}
      </section>

      {done.length > 0 && (
        <section className="space-y-3">
          <h2 className="text-xs uppercase tracking-wide text-muted-foreground">
            Обработанные ({done.length})
          </h2>
          {done.map((v) => (
            <VisitCard key={v.id} visit={v} />
          ))}
        </section>
      )}
    </div>
  );
}

function VisitCard({ visit, onProcessed }: { visit: VisitRecord; onProcessed?: () => void }) {
  const isNew = visit.status === "new";
  return (
    <div
      className={cn(
        "rounded-xl border p-4",
        isNew ? "border-primary/50 bg-primary/5" : "border-border bg-background/40",
      )}
    >
      <div className="flex items-start justify-between gap-2">
        <div>
          <div className="text-sm font-semibold">{visit.full_name || "Без имени"}</div>
          <div className="text-xs text-muted-foreground">
            {formatTime(visit.ts)} · {visit.organization || "организация не указана"}
          </div>
        </div>
        {isNew && onProcessed ? (
          <button
            type="button"
            onClick={onProcessed}
            className="flex items-center gap-1 rounded-full border border-border px-3 py-1 text-xs hover:bg-accent"
          >
            <BadgeCheck className="h-3.5 w-3.5" /> Обработан
          </button>
        ) : (
          <span className="text-xs text-muted-foreground">обработан</span>
        )}
      </div>
      <dl className="mt-3 space-y-1 text-sm">
        <Row label="Цель" value={visit.purpose} />
        <Row label="К кому" value={visit.destination} />
        <div className="flex gap-4 pt-1 text-xs">
          <Flag ok={visit.has_pass} label="Пропуск" />
          <Flag ok={visit.has_id} label="Удостоверение" icon />
        </div>
      </dl>
    </div>
  );
}

function Row({ label, value }: { label: string; value: string }) {
  if (!value) return null;
  return (
    <div className="flex gap-2">
      <dt className="w-16 shrink-0 text-muted-foreground">{label}</dt>
      <dd>{value}</dd>
    </div>
  );
}

function Flag({ ok, label, icon }: { ok: boolean | null; label: string; icon?: boolean }) {
  return (
    <span
      className={cn(
        "flex items-center gap-1",
        ok === true ? "text-green-400" : ok === false ? "text-red-300" : "text-muted-foreground",
      )}
    >
      {icon && <IdCard className="h-3.5 w-3.5" />}
      {label}: {ok === null ? "—" : ok ? "да" : "нет"}
    </span>
  );
}

function formatTime(iso: string): string {
  try {
    return new Date(iso).toLocaleString("ru-RU", {
      day: "2-digit",
      month: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
    });
  } catch {
    return iso;
  }
}

// Short two-tone chime via WebAudio — no asset files needed.
function chime() {
  try {
    const ctx = new AudioContext();
    const gain = ctx.createGain();
    gain.gain.value = 0.08;
    gain.connect(ctx.destination);
    [880, 1174].forEach((freq, i) => {
      const osc = ctx.createOscillator();
      osc.frequency.value = freq;
      osc.connect(gain);
      osc.start(ctx.currentTime + i * 0.18);
      osc.stop(ctx.currentTime + i * 0.18 + 0.16);
    });
    setTimeout(() => void ctx.close(), 800);
  } catch {
    // Audio may be blocked until the first user gesture — fine.
  }
}
