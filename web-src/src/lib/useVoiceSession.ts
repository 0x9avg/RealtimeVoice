import { useCallback, useEffect, useRef, useState } from "react";

import { getAuthToken, notifyUnauthorized } from "../api/client";

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

export type MicSource = "browser" | "robot" | "none";

export function useVoiceSession() {
  const [status, setStatus] = useState("idle");
  const [micActive, setMicActive] = useState(false);
  const [micSource, setMicSourceState] = useState<MicSource>("browser");
  const [robotMicAvailable, setRobotMicAvailable] = useState(false);
  const [transcript, setTranscript] = useState<TranscriptLine[]>([]);
  const [toolLog, setToolLog] = useState<string[]>([]);

  const wsRef = useRef<WebSocket | null>(null);
  const micSourceRef = useRef<MicSource>("browser");
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

  const sendMicSourceMessage = useCallback((source: MicSource) => {
    const ws = wsRef.current;
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ type: "mic_source", source }));
    }
  }, []);

  const listeningLabel = useCallback(
    () => (micSourceRef.current === "robot" ? "listening (robot mic)" : "listening"),
    [],
  );

  useEffect(() => {
    const protocol = location.protocol === "https:" ? "wss" : "ws";
    // ?token=... — see app/main.py's /ws/session: a WS handshake can't carry
    // an Authorization header from browser JS, so this is the only way to
    // pass the same bearer token the REST calls send. Empty/no token is
    // fine when the server has no ACCESS_TOKEN configured (app/auth.py's
    // rule applies here too).
    const token = getAuthToken();
    const wsUrl = `${protocol}://${location.host}/ws/session${
      token ? `?token=${encodeURIComponent(token)}` : ""
    }`;
    const ws = new WebSocket(wsUrl);
    ws.binaryType = "arraybuffer";
    wsRef.current = ws;

    ws.onmessage = (event) => {
      if (event.data instanceof ArrayBuffer) {
        playChunk(event.data);
        return;
      }
      const message = JSON.parse(event.data);
      switch (message.type) {
        case "capabilities":
          setRobotMicAvailable(Boolean(message.robot_mic));
          if (!message.robot_mic && micSourceRef.current === "robot") {
            micSourceRef.current = "browser";
            setMicSourceState("browser");
          }
          // The robot mic is a GLOBAL switch (app/robot_mic_state.py on the
          // server) — a previous tab/operator may have already turned it
          // on, and this fresh connection must reflect that instead of
          // showing "off" and requiring a redundant Start click. Nothing to
          // capture locally for this case (the server reads the robot's own
          // hardware mic), so just mirror the UI state.
          if (message.robot_mic && message.robot_mic_active) {
            micSourceRef.current = "robot";
            setMicSourceState("robot");
            setMicActive(true);
            setStatus("listening (robot mic)");
          }
          break;
        case "status":
          setStatus(message.state);
          if (message.detail) appendTool(`[status] ${message.state}: ${message.detail}`);
          break;
        case "turn_complete":
          setStatus(micActive ? listeningLabel() : "idle");
          break;
        case "interrupted":
          stopPlayback();
          setStatus(micActive ? listeningLabel() : "idle");
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
    ws.onclose = (event) => {
      setStatus("closed");
      // 4401 — app/main.py's /ws/session rejected a missing/wrong token.
      // Reuse the exact same "show the token prompt" flow a REST 401
      // triggers, instead of just going silently dark.
      if (event.code === 4401) notifyUnauthorized();
    };
    ws.onerror = () => setStatus("error");

    return () => {
      ws.close();
      wsRef.current = null;
    };
    // Intentionally empty deps: one WS per mount, matching the previous
    // vanilla implementation (no auto-reconnect there either — the
    // server-side GeminiLiveAdapter reconnects to Gemini on its own, but the
    // browser socket to our own backend is not retried automatically).
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

  const stopBrowserMic = useCallback(() => {
    if (captureNodeRef.current) captureNodeRef.current.port.onmessage = null;
    if (micStreamRef.current) micStreamRef.current.getTracks().forEach((t) => t.stop());
    if (captureCtxRef.current) void captureCtxRef.current.close();
    captureCtxRef.current = null;
    captureNodeRef.current = null;
    micStreamRef.current = null;
  }, []);

  const startMic = useCallback(async () => {
    const source = micSourceRef.current;
    if (source === "browser") await startBrowserMic();
    sendMicSourceMessage(source);
    setMicActive(true);
    setStatus(source === "robot" ? "listening (robot mic)" : "listening");
  }, [startBrowserMic, sendMicSourceMessage]);

  const stopMic = useCallback(() => {
    setMicActive(false);
    stopBrowserMic();
    sendMicSourceMessage("none");
    setStatus("idle");
  }, [stopBrowserMic, sendMicSourceMessage]);

  const setMicSource = useCallback((source: MicSource) => {
    micSourceRef.current = source;
    setMicSourceState(source);
  }, []);

  const mute = useCallback(() => {
    // Local stop first, for an instant cut of whatever's already buffered
    // in this tab — but on its own this only clears past chunks. The rest
    // of the current turn is still in flight from the server (and, in
    // AUDIO_OUTPUT=robot mode, playing on hardware the browser can't touch
    // at all), so the server also needs telling to stop forwarding/playing
    // it — see app/main.py's "mute" message handling.
    stopPlayback();
    const ws = wsRef.current;
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ type: "mute" }));
    }
    setStatus(micActive ? "listening" : "idle");
  }, [stopPlayback, micActive]);

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
  };
}
