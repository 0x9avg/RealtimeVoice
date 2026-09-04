"""UsbMic (app/robot_audio.py) — external USB mic captured via `arecord`.

No real ALSA device or sound card here: `asyncio.create_subprocess_exec` is
monkeypatched to a fake process with a controllable stdout, so these tests
run on any machine (dev laptop, CI) without hardware attached. The actual
capture — a Jabra Speak2 40 MS producing real, non-silent mono 16kHz PCM16
via `arecord -D hw:MS,0 ...` — was confirmed live against this R1 on
04.09.2026; that's not something a unit test can exercise, hence these
tests target the subprocess-driving contract (chunking, cleanup, the
missing-binary error) instead.
"""

from __future__ import annotations

import asyncio

import pytest

from app.robot_audio import UsbMic, UsbMicNotAvailableError


class FakeProcess:
    def __init__(self, stdout: asyncio.StreamReader) -> None:
        self.stdout = stdout
        self.stderr = asyncio.StreamReader()
        self.stderr.feed_eof()
        self.terminated = False

    def terminate(self) -> None:
        self.terminated = True

    async def wait(self) -> int:
        return 0


async def test_missing_arecord_raises_a_clear_error(monkeypatch) -> None:
    async def fake_exec(*args: object, **kwargs: object) -> None:
        raise FileNotFoundError

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    mic = UsbMic(alsa_device="hw:MS,0")
    with pytest.raises(UsbMicNotAvailableError):
        async for _ in mic.stream():
            pass


async def test_stream_yields_fixed_size_chunks_and_cleans_up_on_eof(monkeypatch) -> None:
    chunk_bytes = 16_000 * 2 * 50 // 1000  # sample_rate_hz * sample_width * chunk_ms / 1000
    reader = asyncio.StreamReader()
    reader.feed_data(b"\x01" * chunk_bytes * 2)
    reader.feed_eof()
    fake_proc = FakeProcess(reader)

    async def fake_exec(*args: object, **kwargs: object) -> FakeProcess:
        return fake_proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    mic = UsbMic(alsa_device="hw:MS,0")
    chunks = [pcm async for pcm in mic.stream()]

    assert len(chunks) == 2
    assert all(len(c) == chunk_bytes for c in chunks)
    # A short/partial final read (here: none at all past the 2 full chunks)
    # must end the stream gracefully, not raise — and the subprocess must
    # be torn down either way, not leaked.
    assert fake_proc.terminated


class StuckProcess:
    """A process that never dies from SIGTERM — the observed failure mode
    this project already documents for blocking vendor calls (see
    DEPLOYMENT.md's "Процесс не отвечает на systemctl stop"). Proves
    `UsbMic._terminate()` escalates to SIGKILL instead of giving up and
    leaving the ALSA device held open forever (every later UsbMic.stream()
    attempt would then fail to open the same hw: device as "busy")."""

    def __init__(self) -> None:
        self.terminate_called = False
        self.killed = False
        self._dead = asyncio.Event()

    def terminate(self) -> None:
        self.terminate_called = True  # sent, but the process ignores it

    def kill(self) -> None:
        self.killed = True
        self._dead.set()

    async def wait(self) -> int:
        await self._dead.wait()
        return -9


async def test_terminate_escalates_to_sigkill_when_sigterm_is_ignored() -> None:
    proc = StuckProcess()
    mic = UsbMic(alsa_device="hw:MS,0")
    mic._proc = proc  # exercise _terminate() directly, no need to go via stream()

    await mic._terminate()

    assert proc.terminate_called
    assert proc.killed, "SIGTERM alone was ignored — must escalate to SIGKILL"
    assert mic._proc is None
