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
const micSourceSelect = document.getElementById('mic-source');
const micSourceRobotOption = document.getElementById('mic-source-robot-option');
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

function listeningStatusLabel() {
  if (!micActive) return 'idle';
  return micSourceSelect.value === 'robot' ? 'listening (robot mic)' : 'listening';
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
//
// Two interchangeable sources feed the same session, picked by the
// mic-source dropdown: "browser" captures locally via getUserMedia and
// streams PCM16 over the WS (as before); "robot" sends no audio from the
// page at all — it just tells the backend to start forwarding the robot's
// own head-mic multicast (see app/main.py's `mic_source`). Either way the
// Start-mic button is the single control; sendMicSource() is what actually
// arms/disarms audio flowing into the Gemini session on the backend.

function sendMicSource(source) {
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({ type: 'mic_source', source }));
  }
}

async function startBrowserMic() {
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
}

function stopBrowserMic() {
  if (captureNode) captureNode.port.onmessage = null;
  if (micStream) micStream.getTracks().forEach((track) => track.stop());
  if (captureCtx) captureCtx.close();
  captureCtx = null;
  captureNode = null;
  micStream = null;
}

async function startMic() {
  const source = micSourceSelect.value;
  if (source === 'browser') {
    await startBrowserMic();
  }
  sendMicSource(source);
  micActive = true;
  micSourceSelect.disabled = true;
  micButton.textContent = 'Stop mic';
  setStatus(source === 'robot' ? 'listening (robot mic)' : 'listening');
}

function stopMic() {
  micActive = false;
  stopBrowserMic();
  sendMicSource('none');
  micSourceSelect.disabled = false;
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
      case 'capabilities':
        micSourceRobotOption.disabled = !message.robot_mic;
        if (!message.robot_mic && micSourceSelect.value === 'robot') {
          micSourceSelect.value = 'browser';
        }
        break;
      case 'status':
        setStatus(message.state);
        if (message.detail) appendToolLog(`[status] ${message.state}: ${message.detail}`);
        break;
      case 'turn_complete':
        setStatus(listeningStatusLabel());
        break;
      case 'interrupted':
        stopPlayback();
        setStatus(listeningStatusLabel());
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
      case 'robot_audio':
        // AUDIO_OUTPUT=robot: audio plays through the real robot speaker,
        // not this page — this just confirms a chunk was sent there.
        setStatus('speaking (robot)');
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
