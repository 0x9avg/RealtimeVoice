// Browser side of the /ws/session protocol.
//
// Binary WS frames = raw PCM16 audio (mic -> server @16kHz, server ->
// speaker @24kHz). JSON text frames = control/status messages. Both audio
// contexts are created with an explicit `sampleRate`, so the browser itself
// resamples mic input and model output — no PCM resampling code needed here
// or on the backend.

const statusEl = document.getElementById('status');
const transcriptEl = document.getElementById('transcript');
const toolLogEl = document.getElementById('tool-log');
const micButton = document.getElementById('mic-toggle');
const muteButton = document.getElementById('mute');
const textForm = document.getElementById('text-form');
const textInput = document.getElementById('text-input');

let ws = null;
let captureCtx = null;
let captureNode = null;
let micStream = null;
let playbackCtx = null;
let nextStartTime = 0;
let scheduledSources = [];
let micActive = false;

function setStatus(text) {
  statusEl.textContent = text;
}

function appendLine(container, className, text) {
  const line = document.createElement('div');
  if (className) line.className = className;
  line.textContent = text;
  container.appendChild(line);
  container.scrollTop = container.scrollHeight;
}

function appendTranscript(role, text) {
  appendLine(transcriptEl, `transcript-line ${role}`, `${role === 'user' ? 'You' : 'Robot'}: ${text}`);
}

function appendToolLog(text) {
  appendLine(toolLogEl, '', text);
}

// -- playback: server audio -> speaker ------------------------------------

function ensurePlaybackContext() {
  if (!playbackCtx) {
    playbackCtx = new AudioContext({ sampleRate: 24000 });
    nextStartTime = playbackCtx.currentTime;
  }
  return playbackCtx;
}

function stopPlayback() {
  for (const source of scheduledSources) {
    try {
      source.stop(0);
    } catch (err) {
      // already stopped/ended — fine
    }
  }
  scheduledSources = [];
  if (playbackCtx) {
    nextStartTime = playbackCtx.currentTime;
  }
}

function playChunk(arrayBuffer) {
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
  const startAt = Math.max(ctx.currentTime, nextStartTime);
  source.start(startAt);
  nextStartTime = startAt + buffer.duration;
  scheduledSources.push(source);
  source.onended = () => {
    scheduledSources = scheduledSources.filter((s) => s !== source);
  };
  setStatus('speaking');
}

// -- capture: mic -> server -------------------------------------------------

async function startMic() {
  micStream = await navigator.mediaDevices.getUserMedia({ audio: true });
  captureCtx = new AudioContext({ sampleRate: 16000 });
  await captureCtx.audioWorklet.addModule('/capture-worklet.js');
  const source = captureCtx.createMediaStreamSource(micStream);
  captureNode = new AudioWorkletNode(captureCtx, 'capture-processor');
  captureNode.port.onmessage = (event) => {
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(event.data);
    }
  };
  source.connect(captureNode);
  micActive = true;
  micButton.textContent = 'Stop mic';
  setStatus('listening');
}

function stopMic() {
  micActive = false;
  if (captureNode) captureNode.port.onmessage = null;
  if (micStream) micStream.getTracks().forEach((track) => track.stop());
  if (captureCtx) captureCtx.close();
  captureCtx = null;
  captureNode = null;
  micStream = null;
  micButton.textContent = 'Start mic';
  setStatus('idle');
}

// -- websocket ---------------------------------------------------------------

function connectSocket() {
  const protocol = location.protocol === 'https:' ? 'wss' : 'ws';
  ws = new WebSocket(`${protocol}://${location.host}/ws/session`);
  ws.binaryType = 'arraybuffer';

  ws.onmessage = (event) => {
    if (event.data instanceof ArrayBuffer) {
      playChunk(event.data);
      return;
    }
    const message = JSON.parse(event.data);
    switch (message.type) {
      case 'status':
        setStatus(message.state);
        if (message.detail) appendToolLog(`[status] ${message.state}: ${message.detail}`);
        break;
      case 'turn_complete':
        setStatus(micActive ? 'listening' : 'idle');
        break;
      case 'interrupted':
        stopPlayback();
        setStatus(micActive ? 'listening' : 'idle');
        break;
      case 'transcript':
        appendTranscript(message.role, message.text);
        break;
      case 'tool_call':
        appendToolLog(`→ ${message.name}(${JSON.stringify(message.args)})`);
        break;
      case 'tool_result':
        appendToolLog(`← ${message.name}: ${message.text}`);
        break;
      default:
        break;
    }
  };

  ws.onclose = () => setStatus('closed');
  ws.onerror = () => setStatus('error');
}

// -- UI wiring -----------------------------------------------------------

micButton.addEventListener('click', async () => {
  if (micActive) {
    stopMic();
    return;
  }
  try {
    await startMic();
  } catch (err) {
    setStatus('error');
    appendToolLog(`mic error: ${err}`);
  }
});

muteButton.addEventListener('click', () => {
  stopPlayback();
  setStatus(micActive ? 'listening' : 'idle');
});

textForm.addEventListener('submit', (event) => {
  event.preventDefault();
  const text = textInput.value.trim();
  if (!text) return;
  appendTranscript('user', text);
  setStatus('thinking');
  const send = () => ws.send(JSON.stringify({ type: 'text', text }));
  if (ws.readyState === WebSocket.OPEN) {
    send();
  } else {
    ws.addEventListener('open', send, { once: true });
  }
  textInput.value = '';
});

connectSocket();
