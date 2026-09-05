import { useState } from "react";
import { Ear, Mic, MicOff, Volume2, VolumeX } from "lucide-react";

import { CameraView } from "../components/CameraView";
import { cn } from "../lib/cn";
import { wsConnectionLabel } from "../lib/labels";
import { useVoiceSession } from "../lib/useVoiceSession";

// NEW component — not a port. humanoid-robot's HomePage.tsx is built around
// a camera feed, an e-stop toggle and battery telemetry (none of which this
// project has) and streams chat over a NATS-bus/session_id model this
// backend doesn't implement. The chat/mic/mute/text logic lives in
// useVoiceSession.ts, which talks directly to /ws/session.
export function HomePage() {
  const {
    status,
    wsConnected,
    browserMicActive,
    robotMicAvailable,
    usbMicAvailable,
    hwMicAwake,
    wakeHwMicNow,
    putHwMicToSleep,
    preferredMicInput,
    setMicInput,
    speakerTargets,
    activeSpeakerTarget,
    setAudioOutput,
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

      {/* This tab's own connection to OUR backend — deliberately separate
          from `status` above, which mirrors the server's Gemini session
          lifecycle and routinely says "closed" every few minutes as part
          of a normal, inaudible proactive refresh (see useVoiceSession.ts's
          comment on `wsConnected`). Only rendered while not fully open, so
          it never competes for attention on the happy path. */}
      {wsConnected !== "open" && (
        <div className="rounded-lg border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-sm text-amber-700 dark:text-amber-400">
          {wsConnectionLabel(wsConnected)}
        </div>
      )}

      <CameraView />

      {/* ONE unified hardware-mic control — 2026-09-06 ask: "объедини выбор
          [слушающего микрофона] ... в один выбор микрофона, который будет
          слушаться и этот же микрофон можно разбудить нажатием кнопки
          «Разбудить», без разделения". The selector (which mic — Встроенный/
          Внешний) sets `preferredMicInput` (app/session_hub.py's
          set_preferred_mic_input, live, no `.env`/restart) and ALSO doubles
          as the implicit target of the single "Разбудить" button below —
          previously two separate blocks with two separate "Разбудить (X)"
          buttons, one per source, which is exactly the split this removes.
          The selector buttons only appear when there's an actual choice to
          make (both a built-in AND a USB mic configured); status + Разбудить/
          Усыпить always show whenever any hardware mic is configured. */}
      {(robotMicAvailable || usbMicAvailable) && (
        <div className="flex flex-wrap items-center gap-2 rounded-lg border border-border bg-background/40 p-2 text-sm">
          <Ear className={cn("h-4 w-4", hwMicAwake ? "text-primary" : "text-muted-foreground")} />
          <span className="text-muted-foreground">Микрофон:</span>
          {robotMicAvailable && usbMicAvailable && (
            <div className="flex gap-1.5">
              <button
                type="button"
                onClick={() => setMicInput("robot")}
                disabled={preferredMicInput === "robot"}
                className={cn(
                  "rounded-full border border-border px-2.5 py-1 text-xs hover:bg-accent disabled:opacity-50",
                  preferredMicInput === "robot" ? "bg-primary/10 text-primary" : "bg-background/60",
                )}
              >
                Встроенный
              </button>
              <button
                type="button"
                onClick={() => setMicInput("usb")}
                disabled={preferredMicInput === "usb"}
                className={cn(
                  "rounded-full border border-border px-2.5 py-1 text-xs hover:bg-accent disabled:opacity-50",
                  preferredMicInput === "usb" ? "bg-primary/10 text-primary" : "bg-background/60",
                )}
              >
                Внешний (USB)
              </button>
            </div>
          )}
          <span className="text-muted-foreground">{hwMicAwake ? "слушает вас" : "ожидает обращения"}</span>
          <div className="ml-auto flex gap-1.5">
            <button
              type="button"
              onClick={() => wakeHwMicNow(preferredMicInput ?? (robotMicAvailable ? "robot" : "usb"))}
              disabled={Boolean(hwMicAwake)}
              className="rounded-full border border-border bg-background/60 px-2.5 py-1 text-xs hover:bg-accent disabled:opacity-50"
            >
              Разбудить
            </button>
            {hwMicAwake && (
              <button
                type="button"
                onClick={putHwMicToSleep}
                className="rounded-full border border-border bg-background/60 px-2.5 py-1 text-xs hover:bg-accent"
              >
                Усыпить
              </button>
            )}
          </div>
        </div>
      )}

      {/* Runtime toggle between hardware speaker OUTPUT targets — item 2 of
          the 2026-09-05 fixes list. Only shown when there's an actual
          choice to make (both a built-in AND a USB speaker configured) —
          see app/session_hub.py's handle_audio_output. */}
      {speakerTargets.length > 1 && (
        <div className="flex flex-wrap items-center gap-2 rounded-lg border border-border bg-background/40 p-2 text-sm">
          <Volume2 className="h-4 w-4 text-muted-foreground" />
          <span className="text-muted-foreground">Вывод звука:</span>
          <div className="ml-auto flex gap-1.5">
            {speakerTargets.includes("robot") && (
              <button
                type="button"
                onClick={() => setAudioOutput("robot")}
                disabled={activeSpeakerTarget === "robot"}
                className={cn(
                  "rounded-full border border-border px-2.5 py-1 text-xs hover:bg-accent disabled:opacity-50",
                  activeSpeakerTarget === "robot" ? "bg-primary/10 text-primary" : "bg-background/60",
                )}
              >
                Внутренний
              </button>
            )}
            {speakerTargets.includes("usb") && (
              <button
                type="button"
                onClick={() => setAudioOutput("usb")}
                disabled={activeSpeakerTarget === "usb"}
                className={cn(
                  "rounded-full border border-border px-2.5 py-1 text-xs hover:bg-accent disabled:opacity-50",
                  activeSpeakerTarget === "usb" ? "bg-primary/10 text-primary" : "bg-background/60",
                )}
              >
                Внешний (USB)
              </button>
            )}
          </div>
        </div>
      )}

      <div className="flex flex-wrap items-center gap-2">
        <button
          type="button"
          onClick={() => void (browserMicActive ? stopMic() : startMic())}
          className={cn(
            "flex items-center gap-1.5 rounded-full border border-border px-3 py-1.5 text-sm",
            browserMicActive ? "bg-red-500/10 text-red-200" : "bg-background/60 hover:bg-accent",
          )}
        >
          {browserMicActive ? <MicOff className="h-4 w-4" /> : <Mic className="h-4 w-4" />}
          {browserMicActive ? "Stop mic (браузер)" : "Start mic (браузер)"}
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
