// Video-wall sections (MinTrans app). Keys mirror WallSection
// (app/wall/commands.py) — the app's own screen names. Ported verbatim from
// ../../humanoid-robot/web/dashboard/src/lib/labels.ts — this project only
// needs the wall-related exports, not the gesture/posture/deny-reason ones
// that page owns for a robot with hands and an e-stop.
export const WALL_SECTIONS: { key: string; label: string }[] = [
  { key: "Avto1", label: "Кызылорда — Жезказган" },
  { key: "Avto2", label: "Актобе — Карабутак — Улгайсын" },
  { key: "Avto3", label: "Мост через Иртыш" },
  { key: "Avto4", label: "Обход Сарыагаша" },
  { key: "Avto5", label: "Актобе — Улгайсын" },
  { key: "JD1", label: "Дарбаза — Мактаарал" },
  { key: "JD2", label: "Мойынты — Кызылжар" },
  { key: "JD3", label: "Бахты — Аягоз" },
  { key: "Aero1", label: "Аэропорт Зайсан" },
  { key: "Aero2", label: "Аэропорт Катон-Карагай" },
  { key: "Aero3", label: "Аэропорт Кендерли" },
  { key: "Aero4", label: "Аэропорт Аркалык" },
];

export const WALL_CATEGORIES: { title: string; prefix: string }[] = [
  { title: "Автодороги", prefix: "Avto" },
  { title: "Железные дороги", prefix: "JD" },
  { title: "Аэропорты", prefix: "Aero" },
];

export function wallOutcomeLabel(outcome: string): string {
  switch (outcome) {
    case "accepted":
      return "Выполнено";
    case "rejected":
      return "Отклонено агентом стены";
    case "unreachable":
      return "Видеостена недоступна";
    default:
      return outcome;
  }
}

// 2026-09-06 audit finding: HomePage used to render this tab's own
// WebSocket connection state (useVoiceSession.ts's `wsConnected`) as a bare
// English word ("connecting"/"open"/"reconnecting") — indistinguishable
// from the site having frozen when it actually meant "a Wi-Fi blip or a
// backend restart just happened, retrying automatically".
export function wsConnectionLabel(state: "connecting" | "open" | "reconnecting"): string {
  switch (state) {
    case "connecting":
      return "Подключение…";
    case "open":
      return "";
    case "reconnecting":
      return "Нет связи с роботом — переподключаюсь…";
    default:
      return state;
  }
}
