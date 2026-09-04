r"""R1's real microphone and speaker — used by app/robot_main.py (headless)
and by app/main.py when AUDIO_INPUT/AUDIO_OUTPUT=robot (browser status UI +
real hardware audio).

Ported from `../../humanoid-robot/packages/humanoid-robot-adapters-unitree-g1/
src/humanoid_robot/adapters/unitree_g1/{audio_in,audio_out,sdk}.py`, stripped
of that package's `AudioFrame`/`AudioFormat`/port abstractions — this module
just moves raw PCM16 `bytes`, matching what `GeminiLiveAdapter` already
speaks (see app/gemini_live.py).

Two very different dependency stories:

* **Mic (`RobotMic`)** — the head mic array multicasts raw UDP to a
  well-known group; this is plain `socket`, stdlib only, works anywhere.
* **Speaker (`RobotSpeaker`)** — playback goes through the vendor
  `unitree_sdk2py` SDK (`AudioClient.PlayStream`, a DDS RPC), which is not
  on PyPI. Import is lazy (only touched on first `play()`), so importing
  this module never fails off-robot; only actually driving the speaker
  requires the SDK + CycloneDDS to be installed (see ../README.md's
  "Запуск на роботе" section for the build recipe).

`HalfDuplexGate` also lives here: this hardware has no acoustic echo
cancellation, so whoever feeds `RobotMic` frames into the adapter must drop
them while the robot is speaking (plus a guard interval after) — otherwise
it reliably hears and reacts to its own voice.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import logging
import socket
import struct
import time
import warnings
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from app.dds import ensure_channel_factory

# audioop is stdlib on 3.12, removed in 3.13 (add audioop-lts then). Silence
# just this import's DeprecationWarning, matching the upstream adapter.
with warnings.catch_warnings():
    warnings.simplefilter("ignore", DeprecationWarning)
    import audioop

_SAMPLE_WIDTH_BYTES = 2  # PCM16 throughout this module
_LOG = logging.getLogger("robot_audio")

# -- mic ---------------------------------------------------------------


class UnitreeSdkNotAvailableError(RuntimeError):
    """Raised when `RobotSpeaker` is used without the vendor SDK installed."""

    def __init__(self) -> None:
        super().__init__(
            "unitree_sdk2py is not importable in this environment. Build it "
            "on the robot per this project's README.md (CycloneDDS "
            "0.10.2 + `uv pip install git+https://github.com/"
            "unitreerobotics/unitree_sdk2_python.git`)."
        )


@dataclass(slots=True)
class RobotMic:
    """Reads the head mic array's UDP multicast stream as mono PCM16 16kHz.

    The array emits `input_channels` interleaved PCM16 channels; channel 0
    tends to be the beamformed centre, so that's what gets kept.
    """

    multicast_group: str
    multicast_port: int
    interface_ip: str
    input_channels: int = 6
    recv_buffer_bytes: int = 4096
    _socket: socket.socket | None = field(default=None, init=False, repr=False)
    _closed: bool = field(default=False, init=False)

    def _open_socket(self) -> socket.socket:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        with contextlib.suppress(OSError):
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        # On a multi-homed host (this Jetson has eth10 + wlan0 + docker
        # bridges), IP_ADD_MEMBERSHIP's interface field controls the IGMP
        # join, but IP_MULTICAST_IF is what actually pins *this socket* to
        # that interface for receiving — without it the kernel can join
        # correctly and still not deliver packets. Confirmed against
        # unitreerobotics/unitree_sdk2_python#133 (community G1 mic
        # example), which sets both; our earlier port only set the former.
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(self.interface_ip))
        sock.bind(("0.0.0.0", self.multicast_port))  # noqa: S104 — multicast join, LAN-only
        mreq = struct.pack(
            "=4s4s",
            socket.inet_aton(self.multicast_group),
            socket.inet_aton(self.interface_ip),
        )
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
        sock.setblocking(False)
        return sock

    async def stream(self) -> AsyncIterator[bytes]:
        """Yield mono PCM16 16kHz chunks as multicast packets arrive."""
        loop = asyncio.get_running_loop()
        if self._socket is None:
            self._socket = self._open_socket()
        sock = self._socket
        while not self._closed:
            try:
                data = await loop.sock_recv(sock, self.recv_buffer_bytes)
            except (BlockingIOError, InterruptedError):
                await asyncio.sleep(0.005)
                continue
            if not data:
                return
            pcm = self._downmix(data)
            if pcm:
                yield pcm

    async def close(self) -> None:
        self._closed = True
        if self._socket is not None:
            with contextlib.suppress(OSError):
                self._socket.close()
            self._socket = None

    def _downmix(self, payload: bytes) -> bytes:
        ch = self.input_channels
        if ch == 1:
            return payload
        frame_bytes = ch * _SAMPLE_WIDTH_BYTES
        usable_len = (len(payload) // frame_bytes) * frame_bytes
        if usable_len == 0:
            return b""
        buf = memoryview(payload)[:usable_len]
        out = bytearray(usable_len // ch)
        for i in range(0, usable_len, frame_bytes):
            out_offset = (i // frame_bytes) * _SAMPLE_WIDTH_BYTES
            out[out_offset : out_offset + _SAMPLE_WIDTH_BYTES] = buf[
                i : i + _SAMPLE_WIDTH_BYTES
            ]
        return bytes(out)


# -- usb mic (external USB mic/speakerphone, e.g. a conference mic plugged
# into the robot's own onboard computer) ---------------------------------


class UsbMicNotAvailableError(RuntimeError):
    """Raised when `UsbMic` can't launch `arecord` for its configured device."""


@dataclass(slots=True)
class UsbMic:
    """Reads a USB microphone plugged into the robot's own computer via ALSA.

    Unlike `RobotMic` (the head-mic array, reached over UDP multicast —
    vendor hardware, vendor protocol), this is any standard USB audio input
    device (e.g. a USB conference speakerphone) that Linux's own ALSA layer
    already sees as a normal sound card — no vendor SDK involved. Captured
    by shelling out to `arecord` (already present on the robot as part of
    alsa-utils) rather than a Python ALSA/PortAudio binding, so this needs
    no extra dependency beyond what the OS already ships. Confirmed live
    against a Jabra Speak2 40 MS on this R1 (04.09.2026): `arecord -D
    hw:MS,0 -f S16_LE -r 16000 -c 1` records real (non-silent) mono 16kHz
    audio directly — no resampling needed downstream, same contract as
    `RobotMic.stream()`.

    `alsa_device` should name the card by its ALSA-assigned ID (e.g.
    "hw:MS,0" for a card listed as "MS" in `arecord -l`/`/proc/asound/
    cards`), not by numeric index ("hw:2,0") — the index can shift if other
    USB audio devices are plugged in later or enumerate in a different
    order after reboot; the ID string does not.
    """

    alsa_device: str
    sample_rate_hz: int = 16_000
    chunk_ms: int = 50
    _proc: asyncio.subprocess.Process | None = field(default=None, init=False, repr=False)
    _closed: bool = field(default=False, init=False)

    async def stream(self) -> AsyncIterator[bytes]:
        """Yield mono PCM16 `sample_rate_hz` chunks for as long as `arecord` runs.

        Returns (does not raise) if `arecord` exits or the device disappears
        mid-stream — the caller (`SessionHub._usb_mic_task`, wrapped by
        `_supervised`) treats a returning stream as "this source stopped",
        not as a crash, so an unplugged mic doesn't take the whole process
        down with it.
        """
        chunk_bytes = self.sample_rate_hz * _SAMPLE_WIDTH_BYTES * self.chunk_ms // 1000
        try:
            self._proc = await asyncio.create_subprocess_exec(
                "arecord",
                "-D",
                self.alsa_device,
                "-f",
                "S16_LE",
                "-r",
                str(self.sample_rate_hz),
                "-c",
                "1",
                "-t",
                "raw",
                "-q",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            raise UsbMicNotAvailableError(
                "arecord not found on PATH — install alsa-utils"
            ) from exc
        proc = self._proc
        assert proc.stdout is not None  # noqa: S101 — PIPE requested above
        try:
            while not self._closed:
                try:
                    data = await proc.stdout.readexactly(chunk_bytes)
                except asyncio.IncompleteReadError:
                    if not self._closed:
                        stderr = b""
                        if proc.stderr is not None:
                            with contextlib.suppress(Exception):
                                stderr = await proc.stderr.read()
                        _LOG.warning(
                            "usb_mic.arecord_exited device=%s detail=%s",
                            self.alsa_device,
                            stderr.decode("utf-8", "replace").strip() or "(no output)",
                        )
                    return
                yield data
        finally:
            await self._terminate()

    async def close(self) -> None:
        self._closed = True
        await self._terminate()

    async def _terminate(self) -> None:
        if self._proc is None:
            return
        proc = self._proc
        # Cleared up front, not after wait() below — a concurrent call (e.g.
        # close() racing stream()'s own finally-block cleanup) must see this
        # as already-handled rather than issuing a second terminate()/kill()
        # against the same (possibly already-reaped) PID.
        self._proc = None
        with contextlib.suppress(ProcessLookupError):
            proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=2.0)
            return
        except TimeoutError:
            pass
        # SIGTERM ignored/stuck (observed elsewhere in this project with
        # blocking vendor calls — see ../DEPLOYMENT.md's "Процесс не отвечает
        # на systemctl stop"). Left alone, `arecord` keeps the ALSA device
        # open indefinitely — every future UsbMic.stream() attempt would then
        # fail to open the same hw: device as "busy", with no obvious cause.
        # systemd escalates the same way (TimeoutStopSec) for the main
        # process; do it ourselves here since this is OUR child process, not
        # something systemd's cgroup kill is guaranteed to reach in every
        # supervision context (e.g. a bare `uv run` with no systemd unit).
        _LOG.warning("usb_mic.sigterm_ignored device=%s escalating_to_sigkill", self.alsa_device)
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(proc.wait(), timeout=2.0)


# -- speaker -------------------------------------------------------------

_G1_SAMPLE_RATE = 16_000
_CHUNK_MS = 50
_CHUNK_BYTES = _G1_SAMPLE_RATE * _SAMPLE_WIDTH_BYTES * _CHUNK_MS // 1000  # 1600
# First ~200ms of an utterance is sent back-to-back to prime the vendor
# speaker's buffer before pacing engages — otherwise asyncio scheduling
# jitter can underrun it mid-word. Must stay under the 0.5s idle-reset
# threshold in `_pace_wait_for_next_slot` below.
_LEAD_S = 0.2


def _require_sdk() -> Any:
    try:
        return importlib.import_module("unitree_sdk2py.g1.audio.g1_audio_client")
    except ImportError as exc:
        raise UnitreeSdkNotAvailableError from exc


@dataclass(slots=True)
class RobotSpeaker:
    """Plays PCM16 audio (any sample rate) through the G1 head speaker.

    `GeminiLiveAdapter` yields 24kHz PCM16 (see app/config.py's
    `receive_sample_rate_hz`); this resamples to the speaker's fixed 16kHz
    and paces delivery to match the vendor pipeline's real-time expectation.
    """

    network_interface: str
    volume_pct: int = 100
    app_name: str = "realtimevoice"
    _client: Any = field(default=None, init=False, repr=False)
    _stream_id: str = field(default="", init=False)
    _next_send_monotonic: float = field(default=0.0, init=False)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False)
    _resample_state: Any = field(default=None, init=False)
    _resample_src_rate: int = field(default=0, init=False)
    # Bumped by stop() to cut an in-flight play() short — see both methods.
    _epoch: int = field(default=0, init=False)

    def _ensure_client(self) -> Any:
        if self._client is not None:
            return self._client
        audio_client_mod = _require_sdk()
        ensure_channel_factory(self.network_interface)
        client = audio_client_mod.AudioClient()
        client.SetTimeout(10.0)
        client.Init()
        client.SetVolume(self.volume_pct)
        self._client = client
        self._stream_id = str(int(time.time() * 1000))
        self._next_send_monotonic = time.monotonic()
        return client

    async def warm_up(self) -> None:
        """Open the vendor audio client ahead of the first utterance.

        `_ensure_client()` runs DDS discovery and `AudioClient.Init()` (a
        10s SetTimeout), which otherwise all lands on the FIRST thing the
        robot ever says — i.e. on the first visitor, who gets to wait
        through it. Failures are logged and ignored: this is an
        optimisation, and play() will retry the same path anyway.
        """
        try:
            await asyncio.to_thread(self._ensure_client)
            _LOG.info("robot_speaker.warmed_up")
        except Exception as exc:  # noqa: BLE001 — best effort only
            _LOG.warning("robot_speaker.warm_up_failed: %s", exc)

    async def play(self, pcm: bytes, *, sample_rate_hz: int) -> None:
        """Resample to 16kHz mono, chunk to 50ms, pace and send."""
        converted = self._resample(pcm, sample_rate_hz)
        async with self._lock:
            # Off-loop, like PlayStream/PlayStop below: ChannelFactoryInitialize
            # + AudioClient.Init() are blocking DDS/vendor-SDK calls (Init()
            # has a 10s SetTimeout) — calling them inline on the first play()
            # of a session freezes the whole event loop, including this
            # session's own WS receive loop, for however long DDS discovery
            # takes. That reads as "the browser session hung then closed"
            # with no server-side error, since nothing raised — it just never
            # got to run anything else while blocked.
            client = await asyncio.to_thread(self._ensure_client)
            epoch = self._epoch
            now = time.monotonic()
            if self._next_send_monotonic <= now:
                self._next_send_monotonic = now - _LEAD_S
            for offset in range(0, len(converted), _CHUNK_BYTES):
                # A stop() landed while this call was pacing (barge-in, or
                # the operator's "Замолчи"). Bail at the chunk boundary
                # instead of playing the rest of an utterance nobody wants
                # to hear — without this, silencing the robot takes as long
                # as whatever audio is already in flight.
                if self._epoch != epoch:
                    return
                chunk = converted[offset : offset + _CHUNK_BYTES]
                await self._pace_wait_for_next_slot()
                # DDS RPC — off-loop so a hung call can't freeze the runner.
                result = await asyncio.to_thread(
                    client.PlayStream, self.app_name, self._stream_id, chunk
                )
                # PlayStream returns (code, data) like the rest of this RPC
                # client; a non-zero code means the vendor side rejected or
                # dropped this chunk (e.g. a stale stream_id from a prior
                # session) — silently discarding it, as the ported code did,
                # made "robot stopped talking" undiagnosable from the log.
                code = result[0] if isinstance(result, tuple) else result
                if code:
                    _LOG.warning("PlayStream non-zero result: %r", result)

    def _resample(self, pcm: bytes, sample_rate_hz: int) -> bytes:
        if sample_rate_hz == _G1_SAMPLE_RATE:
            return pcm
        if sample_rate_hz != self._resample_src_rate:
            self._resample_state = None
            self._resample_src_rate = sample_rate_hz
        converted, self._resample_state = audioop.ratecv(
            pcm, _SAMPLE_WIDTH_BYTES, 1, sample_rate_hz, _G1_SAMPLE_RATE, self._resample_state
        )
        return converted

    async def stop(self) -> None:
        """Close out the current playback stream — call this both on
        barge-in (Interrupted) AND after a normal TurnComplete, not just
        interruption. The vendor audio service was observed going silent
        (PlayStream still returns code 0, no exception, just no sound) on
        the second+ utterance of a session — reusing the same stream_id
        without an intervening PlayStop appears to leave the vendor-side
        stream in a state where it silently drops further chunks. Rotating
        _stream_id here (not just clearing playback state) forces the next
        play() to start what the vendor sees as a brand-new stream.
        """
        # Signal BEFORE taking the lock: a play() that's mid-utterance holds
        # it and only releases between paced chunks, so waiting for the lock
        # first would mean waiting out the audio we're trying to cut off.
        self._epoch += 1
        async with self._lock:
            self._resample_state = None
            self._resample_src_rate = 0
            if self._client is None:
                return
            with contextlib.suppress(Exception):
                stop_result = await asyncio.wait_for(
                    asyncio.to_thread(self._client.PlayStop, self.app_name), timeout=5.0
                )
                code = stop_result[0] if isinstance(stop_result, tuple) else stop_result
                if code:
                    _LOG.warning("PlayStop non-zero result: %r", stop_result)
            self._stream_id = str(int(time.time() * 1000))
            self._next_send_monotonic = time.monotonic()

    async def _pace_wait_for_next_slot(self) -> None:
        now = time.monotonic()
        if self._next_send_monotonic < now - 0.5:
            # Fell far behind (long silence) — don't burst-play the backlog.
            self._next_send_monotonic = now
        else:
            wait = self._next_send_monotonic - now
            if wait > 0:
                await asyncio.sleep(wait)
        self._next_send_monotonic += _CHUNK_MS / 1000.0


class HalfDuplexGate:
    """Tracks whether `RobotMic` frames should be dropped right now.

    No AEC on this hardware (see module docstring) — mute the mic while the
    robot is speaking, and for `guard_s` after it stops, or it hears itself.
    """

    def __init__(self, guard_s: float) -> None:
        self._guard_s = guard_s
        self._speaking = False
        self._guard_until = 0.0

    def on_speaking_started(self) -> None:
        self._speaking = True

    def on_speaking_stopped(self) -> None:
        self._speaking = False
        self._guard_until = time.monotonic() + self._guard_s

    def open_now(self) -> None:
        """Re-open the mic with no guard interval.

        For a barge-in: the mic is picking up the visitor (that's what
        caused the interrupt), so the usual "don't hear our own tail"
        margin would only throw away the start of their sentence.
        """
        self._speaking = False
        self._guard_until = 0.0

    @property
    def mic_muted(self) -> bool:
        return self._speaking or time.monotonic() < self._guard_until
