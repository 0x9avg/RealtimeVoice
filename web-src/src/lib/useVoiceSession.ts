import { useCallback, useEffect, useRef, useState } from "react";

// NEW — not ported from humanoid-robot. That dashboard's chat
// (useConversation.ts + eventStream.tsx) streams answers over a NATS-style
// event bus keyed by session_id, driven by a separate camera feed and
// battery telemetry this project has none of. This hook owns the
// /ws/session WS protocol directly — audio capture/playback, control
// messages, transcript/tool-log state — for app/main.py's shared session.

export interface TranscriptLine {
  role: "user" | "model";
  text: string;
}

// Item 5 of the 2026-09-04 fixes list: the hardware mic (robot/usb) is no
// longer something an operator "starts" — if configured, it's always
// locally listening for the wake word (app/wake_word.py) and only forwards
// to Gemini during the resulting window. "HwMicSource" here names WHICH
// hardware source is currently in that window (server truth, mirrored, not
// something this tab decides) — "browser" mic capture is a separate,
// genuinely per-tab, operator-actionable thing (see browserMicActive below).
export type HwMicSource = "robot" | "usb" | null;

// Item 2 of the 2026-09-05 fixes list: which hardware speaker OUTPUT target
// is active — a runtime toggle now (app/session_hub.py's
// handle_audio_output), not a fixed AUDIO_OUTPUT chosen once at deploy time.
export type SpeakerTarget = "robot" | "usb" | null;

// 2026-09-06 audit finding: this hook's own `status` string is ALSO used
// verbatim for the server-relayed Gemini connection lifecycle ("status"
// messages mirror app/gemini_live.py's ConnectionStateChanged — see
// app/session_hub.py's _pump_gemini), which routinely goes through
// "closed" every ~max_session_duration_s minutes as part of a normal,
// inaudible proactive session swap. Reusing the same field/string for
// "this BROWSER TAB's own WebSocket to our backend died" would make a
// real network drop indistinguishable from that routine, harmless Gemini
// housekeeping — so the browser<->backend socket's own lifecycle gets a
// separate field instead, never touching `status`.
export type WsConnectionState = "connecting" | "open" | "reconnecting";

export function useVoiceSession() {
  const [status, setStatus] = useState("idle");
  const [wsConnected, setWsConnected] = useState<WsConnectionState>("connecting");
  const [browserMicActive, setBrowserMicActive] = useState(false);
  const [robotMicAvailable, setRobotMicAvailable] = useState(false);
  const [usbMicAvailable, setUsbMicAvailable] = useState(false);
  const [hwMicAwake, setHwMicAwake] = useState<HwMicSource>(null);
  // 2026-09-06 ask: which hardware mic listens for the wake word is chosen
  // live from the website, not `.env` — see setMicInput below and
  // app/session_hub.py's set_preferred_mic_input. Distinct from hwMicAwake
  // above (the momentary "a wake window is open right now" state): this is
  // the standing preference, server truth, mirrored here.
  const [preferredMicInput, setPreferredMicInput] = useState<"robot" | "usb" | null>(null);
  const [speakerTargets, setSpeakerTargets] = useState<string[]>([]);
  const [activeSpeakerTarget, setActiveSpeakerTarget] = useState<SpeakerTarget>(null);
  const [transcript, setTranscript] = useState<TranscriptLine[]>([]);
  const [toolLog, setToolLog] = useState<string[]>([]);

  const wsRef = useRef<WebSocket | null>(null);
  // Mirrors state for use inside ws.onmessage (see the mount effect below):
  // that handler is created once, inside a `useEffect(..., [])`, so any
  // REACT STATE it reads is permanently the value from the first render (a
  // stale closure) — it must read refs instead for anything that can
  // change later.
  const browserMicActiveRef = useRef(false);
  const hwMicAwakeRef = useRef<HwMicSource>(null);
  const captureCtxRef = useRef<AudioContext | null>(null);
  const captureNodeRef = useRef<AudioWorkletNode | null>(null);
  const micStreamRef = useRef<MediaStream | null>(null);
  const playbackCtxRef = useRef<AudioContext | null>(null);
  const nextStartTimeRef = useRef(0);
  const scheduledSourcesRef = useRef<AudioBufferSourceNode[]>([]);

  const appendTool = useCallback((line: string) => {
    setToolLog((prev) => [...prev, line]);
  }, []);

  const ensurePlaybackContext = useCallback(() => {
    if (!playbackCtxRef.current) {
      playbackCtxRef.current = new AudioContext({ sampleRate: 24000 });
      nextStartTimeRef.current = playbackCtxRef.current.currentTime;
    }
    return playbackCtxRef.current;
  }, []);

  const stopPlayback = useCallback(() => {
    for (const source of scheduledSourcesRef.current) {
      try {
        source.stop(0);
      } catch {
        // already stopped/ended — fine
      }
    }
    scheduledSourcesRef.current = [];
    if (playbackCtxRef.current) {
      nextStartTimeRef.current = playbackCtxRef.current.currentTime;
    }
  }, []);

  const playChunk = useCallback(
    (arrayBuffer: ArrayBuffer) => {
      const ctx = ensurePlaybackContext();
      const int16 = new Int16Array(arrayBuffer);
      const float32 = new Float32Array(int16.length);
      for (let i = 0; i < int16.length; i++) {
        float32[i] = int16[i] / (int16[i] < 0 ? 0x8000 : 0x7fff);
      }
      const buffer = ctx.createBuffer(1, float32.length, 24000);
      buffer.copyToChannel(float32, 0);
      const source = ctx.createBufferSource();
      source.buffer = buffer;
      source.connect(ctx.destination);
      const startAt = Math.max(ctx.currentTime, nextStartTimeRef.current);
      source.start(startAt);
      nextStartTimeRef.current = startAt + buffer.duration;
      scheduledSourcesRef.current.push(source);
      source.onended = () => {
        scheduledSourcesRef.current = scheduledSourcesRef.current.filter((s) => s !== source);
      };
      setStatus("speaking");
    },
    [ensurePlaybackContext],
  );

  const sendMicSourceMessage = useCallback((source: "browser" | "robot" | "usb" | "none") => {
    const ws = wsRef.current;
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ type: "mic_source", source }));
    }
  }, []);

  const listeningLabel = useCallback(() => {
    if (browserMicActiveRef.current) return "listening";
    if (hwMicAwakeRef.current === "robot") return "listening (robot mic)";
    if (hwMicAwakeRef.current === "usb") return "listening (usb mic)";
    return "idle";
  }, []);

  useEffect(() => {
    let cancelled = false;
    let reconnectDelayMs = 1000;
    let reconnectTimer: ReturnType<typeof setTimeout> | null = null;

    const connect = () => {
      const protocol = location.protocol === "https:" ? "wss" : "ws";
      const wsUrl = `${protocol}://${location.host}/ws/session`;
      const ws = new WebSocket(wsUrl);
      ws.binaryType = "arraybuffer";
      wsRef.current = ws;

      ws.onopen = () => {
        reconnectDelayMs = 1000;
        setWsConnected("open");
        // The server treats every new /ws/session connection as a brand
        // new join, with mic_source reset to "none" (app/main.py's
        // `session()`) — if this tab's browser mic was already running
        // when the previous socket dropped, capture keeps going client-
        // side (startBrowserMic/captureNode are untouched by a WS drop),
        // but the server has no idea to route it anywhere until told
        // again on the new connection.
        if (browserMicActiveRef.current) {
          ws.send(JSON.stringify({ type: "mic_source", source: "browser" }));
        }
      };

      ws.onmessage = (event) => {
        if (event.data instanceof ArrayBuffer) {
          playChunk(event.data);
          return;
        }
        const message = JSON.parse(event.data);
        switch (message.type) {
          case "capabilities": {
            setRobotMicAvailable(Boolean(message.robot_mic));
            setUsbMicAvailable(Boolean(message.usb_mic));
            // Which hardware source (if any) is CURRENTLY inside its
            // wake-word window — server truth (app/session_hub.py's
            // _drain_hw_mic/handle_mic_source), broadcast live on every
            // transition, not just delivered on join. Purely informational
            // here: nothing in this tab decided this, it just mirrors it.
            const awake: HwMicSource = message.robot_mic && message.robot_mic_active
              ? "robot"
              : message.usb_mic && message.usb_mic_active
                ? "usb"
                : null;
            if (awake !== hwMicAwakeRef.current) {
              hwMicAwakeRef.current = awake;
              setHwMicAwake(awake);
              if (!browserMicActiveRef.current) setStatus(listeningLabel());
            }
            setPreferredMicInput(
              message.preferred_mic_input === "robot" || message.preferred_mic_input === "usb"
                ? message.preferred_mic_input
                : null,
            );
            setSpeakerTargets(Array.isArray(message.speaker_targets) ? message.speaker_targets : []);
            setActiveSpeakerTarget(
              message.active_speaker_target === "robot" || message.active_speaker_target === "usb"
                ? message.active_speaker_target
                : null,
            );
            break;
          }
          case "status":
            setStatus(message.state);
            if (message.detail) appendTool(`[status] ${message.state}: ${message.detail}`);
            break;
          case "turn_complete":
            setStatus(listeningLabel());
            break;
          case "interrupted":
            stopPlayback();
            setStatus(listeningLabel());
            break;
          case "transcript":
            setTranscript((prev) => [...prev, { role: message.role, text: message.text }]);
            break;
          case "tool_call":
            appendTool(`→ ${message.name}(${JSON.stringify(message.args)})`);
            break;
          case "tool_result":
            appendTool(`← ${message.name}: ${message.text}`);
            break;
          case "robot_audio":
            setStatus("speaking (robot)");
            break;
          default:
            break;
        }
      };
      ws.onclose = () => {
        wsRef.current = null;
        if (cancelled) return;
        // 2026-09-06 audit finding: this used to be the end of it -- no
        // retry, ever. A dropped browser<->backend socket (a Wi-Fi blip,
        // routine `systemctl --user restart` during a deploy, the process
        // supervisor restarting after a crash — all real, documented
        // occurrences, see DEPLOYMENT.md) left the tab showing a bare
        // "closed"/"error" string forever, mic/mute buttons silently
        // inert, with no way for a visitor or operator to tell the
        // difference between "temporarily reconnecting" and "broken,
        // reload the page" — indistinguishable from the site having
        // frozen. Retrying with backoff (capped at 10s) fixes the common
        // case (the backend comes back within seconds) without a manual
        // reload; `wsConnected` (separate from `status` — see its own
        // comment above) drives a "reconnecting" indicator in the UI.
        setWsConnected("reconnecting");
        reconnectTimer = setTimeout(connect, reconnectDelayMs);
        reconnectDelayMs = Math.min(reconnectDelayMs * 2, 10_000);
      };
      ws.onerror = () => {
        // A WebSocket that fails to connect or drops fires "error"
        // immediately followed by "close" — onclose above owns the actual
        // reconnect scheduling; this only exists so `wsConnected` doesn't
        // sit on a stale "open" for the instant between the two events.
        setWsConnected("reconnecting");
      };
    };

    connect();

    return () => {
      cancelled = true;
      if (reconnectTimer) clearTimeout(reconnectTimer);
      wsRef.current?.close();
      wsRef.current = null;
    };
    // Intentionally empty deps: one mount-lifetime reconnect loop, driven
    // entirely by `connect`'s own closures — see the "must never touch
    // status" comment on `wsConnected` above for why this doesn't also
    // reconnect the Gemini-side "status" messages, which are the
    // server's business (app/gemini_live.py already retries those).
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const startBrowserMic = useCallback(async () => {
    const micStream = await navigator.mediaDevices.getUserMedia({ audio: true });
    micStreamRef.current = micStream;
    const captureCtx = new AudioContext({ sampleRate: 16000 });
    captureCtxRef.current = captureCtx;
    await captureCtx.audioWorklet.addModule("/capture-worklet.js");
    const source = captureCtx.createMediaStreamSource(micStream);
    const captureNode = new AudioWorkletNode(captureCtx, "capture-processor");
    captureNode.port.onmessage = (event) => {
      const ws = wsRef.current;
      if (ws && ws.readyState === WebSocket.OPEN) ws.send(event.data);
    };
    captureNodeRef.current = captureNode;
    source.connect(captureNode);
  }, []);

  const stopBrowserMicCapture = useCallback(() => {
    if (captureNodeRef.current) captureNodeRef.current.port.onmessage = null;
    if (micStreamRef.current) micStreamRef.current.getTracks().forEach((t) => t.stop());
    if (captureCtxRef.current) void captureCtxRef.current.close();
    captureCtxRef.current = null;
    captureNodeRef.current = null;
    micStreamRef.current = null;
  }, []);

  const startMic = useCallback(async () => {
    await startBrowserMic();
    sendMicSourceMessage("browser");
    browserMicActiveRef.current = true;
    setBrowserMicActive(true);
    setStatus("listening");
  }, [startBrowserMic, sendMicSourceMessage]);

  const stopMic = useCallback(() => {
    browserMicActiveRef.current = false;
    setBrowserMicActive(false);
    stopBrowserMicCapture();
    sendMicSourceMessage("none");
    setStatus(listeningLabel());
  }, [stopBrowserMicCapture, sendMicSourceMessage, listeningLabel]);

  // Manual override for the hardware mic's wake window (see
  // app/session_hub.py's handle_mic_source docstring) — for an operator who
  // wants the robot listening right now without saying the wake phrase.
  const wakeHwMicNow = useCallback(
    (source: "robot" | "usb") => sendMicSourceMessage(source),
    [sendMicSourceMessage],
  );

  const putHwMicToSleep = useCallback(() => sendMicSourceMessage("browser"), [sendMicSourceMessage]);

  // Runtime toggle between hardware speaker OUTPUT targets — item 2 of the
  // 2026-09-05 fixes list. See app/session_hub.py's handle_audio_output.
  const setAudioOutput = useCallback((target: "robot" | "usb") => {
    const ws = wsRef.current;
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ type: "audio_output", target }));
    }
  }, []);

  // Runtime toggle between hardware mic INPUT sources for wake-word
  // listening — 2026-09-06 ask: chosen live from the website, not `.env`.
  // See app/session_hub.py's set_preferred_mic_input.
  const setMicInput = useCallback((source: "robot" | "usb") => {
    const ws = wsRef.current;
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ type: "mic_input", source }));
    }
  }, []);

  const mute = useCallback(() => {
    // Local stop first, for an instant cut of whatever's already buffered
    // in this tab — but on its own this only clears past chunks. The rest
    // of the current turn is still in flight from the server (and, in
    // AUDIO_OUTPUT=robot/usb mode, playing on hardware the browser can't
    // touch at all), so the server also needs telling to stop forwarding/
    // playing it — see app/main.py's "mute" message handling.
    stopPlayback();
    const ws = wsRef.current;
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ type: "mute" }));
    }
    setStatus(listeningLabel());
  }, [stopPlayback, listeningLabel]);

  const sendText = useCallback((text: string) => {
    const trimmed = text.trim();
    if (!trimmed) return;
    setTranscript((prev) => [...prev, { role: "user", text: trimmed }]);
    setStatus("thinking");
    const send = () => wsRef.current?.send(JSON.stringify({ type: "text", text: trimmed }));
    const ws = wsRef.current;
    if (ws && ws.readyState === WebSocket.OPEN) {
      send();
    } else {
      ws?.addEventListener("open", send, { once: true });
    }
  }, []);

  return {
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
  };
}
