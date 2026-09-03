import { useState } from "react";
import { Mic, MicOff, VolumeX } from "lucide-react";

import { cn } from "../lib/cn";
import { useVoiceSession } from "../lib/useVoiceSession";

// NEW component — not a port. humanoid-robot's HomePage.tsx is built around
// a camera feed, an e-stop toggle and battery telemetry (none of which this
// prototype has) and streams chat over a NATS-bus/session_id model this
// backend doesn't implement. This is the same chat/mic/mute/text feature
// set already working in ../../web/app.js, just as a React page — see
// useVoiceSession.ts for the ported protocol logic.
export function HomePage() {
  const {
    status,
    micActive,
    micSource,
    setMicSource,
    robotMicAvailable,
    transcript,
    toolLog,
    startMic,
    stopMic,
    mute,
    sendText,
  } = useVoiceSession();
  const [draft, setDraft] = useState("");

  return (
    <div className="mx-auto flex h-full w-full max-w-lg flex-col gap-4 overflow-y-auto p-4">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-semibold">RealtimeVoice</h1>
        <span className="rounded-full bg-background/60 px-3 py-1 text-xs uppercase tracking-wide text-muted-foreground">
          {status}
        </span>
      </div>

      <div className="flex flex-wrap items-center gap-2">
        <select
          value={micSource}
          onChange={(e) => setMicSource(e.target.value as "browser" | "robot")}
          disabled={micActive}
          className="rounded-md border border-border bg-background/60 px-2 py-1.5 text-sm disabled:opacity-50"
        >
          <option value="browser">Микрофон браузера</option>
          <option value="robot" disabled={!robotMicAvailable}>
            Встроенный (робот)
          </option>
        </select>
        <button
          type="button"
          onClick={() => void (micActive ? stopMic() : startMic())}
          className={cn(
            "flex items-center gap-1.5 rounded-full border border-border px-3 py-1.5 text-sm",
            micActive ? "bg-red-500/10 text-red-200" : "bg-background/60 hover:bg-accent",
          )}
        >
          {micActive ? <MicOff className="h-4 w-4" /> : <Mic className="h-4 w-4" />}
          {micActive ? "Stop mic" : "Start mic"}
        </button>
        <button
          type="button"
          onClick={mute}
          className="flex items-center gap-1.5 rounded-full border border-border bg-background/60 px-3 py-1.5 text-sm hover:bg-accent"
        >
          <VolumeX className="h-4 w-4" />
          Замолчи
        </button>
      </div>

      <form
        onSubmit={(e) => {
          e.preventDefault();
          sendText(draft);
          setDraft("");
        }}
        className="flex gap-2"
      >
        <input
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          placeholder="Введите вопрос текстом..."
          autoComplete="off"
          className="flex-1 rounded-md border border-border bg-background/60 px-3 py-2 text-sm outline-none focus:border-primary"
        />
        <button
          type="submit"
          className="rounded-md bg-primary px-4 text-sm font-medium text-primary-foreground"
        >
          Send
        </button>
      </form>

      <div>
        <h2 className="mb-1 text-sm font-medium text-muted-foreground">Транскрипт</h2>
        <div className="h-48 overflow-y-auto rounded-lg border border-border bg-background/40 p-3 text-sm">
          {transcript.map((line, i) => (
            <div
              key={i}
              className={line.role === "user" ? "text-blue-300" : "text-foreground"}
            >
              {line.role === "user" ? "Вы" : "Робот"}: {line.text}
            </div>
          ))}
        </div>
      </div>

      <div>
        <h2 className="mb-1 text-sm font-medium text-muted-foreground">Tool calls</h2>
        <div className="h-36 overflow-y-auto rounded-lg border border-border bg-background/40 p-3 font-mono text-xs text-muted-foreground">
          {toolLog.map((line, i) => (
            <div key={i}>{line}</div>
          ))}
        </div>
      </div>
    </div>
  );
}
