"""UsbSpeaker (app/robot_audio.py) — audit finding, 2026-09-06.

No real ALSA device/`aplay` here: a fake process stands in for it, so this
test runs on any machine without hardware attached.
"""

from __future__ import annotations

import asyncio
import audioop
import contextlib
import math
import struct

from app.robot_audio import UsbSpeaker


class StallingProcess:
    """Simulates a stalled `aplay` write: `drain()` blocks until something
    calls `terminate()` — standing in for a stuck ALSA/pipe buffer, the
    exact situation `UsbSpeaker.stop()` needs to cut short fast, not wait
    out."""

    def __init__(self) -> None:
        self.terminated = False
        self._unblocked = asyncio.Event()
        self.stdin = self

    def write(self, data: bytes) -> None:  # noqa: ARG002 — content doesn't matter
        pass

    async def drain(self) -> None:
        await self._unblocked.wait()

    def terminate(self) -> None:
        self.terminated = True
        self._unblocked.set()  # simulates the pipe breaking once the child dies

    async def wait(self) -> int:
        return -15


class RecordingProcess:
    """Captures whatever bytes play() writes, standing in for aplay's stdin."""

    def __init__(self) -> None:
        self.written: list[bytes] = []
        self.stdin = self

    def write(self, data: bytes) -> None:
        self.written.append(data)

    async def drain(self) -> None:
        pass

    def terminate(self) -> None:
        pass

    async def wait(self) -> int:
        return 0


async def test_play_upmixes_mono_pcm_to_stereo_before_writing() -> None:
    """2026-09-06 audit finding, confirmed live against a real Jabra Speak2
    40 MS: its playback substream advertises a FIXED `CHANNELS: 2` (not a
    range) -- `aplay -D hw:MS,0 -c 1` fails outright with "Channels count
    non available" before playing a single sample, so USB speaker output
    never worked on this hardware at all. Gemini's audio arrives mono; it
    must be duplicated onto both channels before reaching aplay's stdin."""
    # target_sample_rate_hz matches the input rate on purpose -- isolates
    # the stereo-upmix behaviour under test from the separate resampling
    # behaviour (see test_play_resamples_to_the_devices_verified_safe_rate).
    speaker = UsbSpeaker(alsa_device="hw:TEST,0", target_sample_rate_hz=16000)
    proc = RecordingProcess()
    speaker._proc = proc

    mono = b"\x01\x00\x02\x00\x03\x00"  # three mono PCM16 samples: 1, 2, 3

    await speaker.play(mono, sample_rate_hz=16000)

    assert len(proc.written) == 1
    written = proc.written[0]
    assert len(written) == len(mono) * 2, "stereo output must be twice the mono byte length"
    assert written == b"\x01\x00\x01\x00\x02\x00\x02\x00\x03\x00\x03\x00", (
        "each mono sample must be duplicated onto both channels, in order"
    )


async def test_ensure_proc_requests_two_channels_from_aplay(monkeypatch) -> None:
    speaker = UsbSpeaker(alsa_device="hw:TEST,0")
    captured_args: list[str] = []

    async def fake_exec(*args: str, **kwargs: object):  # noqa: ANN202 — matches asyncio.create_subprocess_exec's shape loosely
        captured_args.extend(args)
        return RecordingProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    await speaker._ensure_proc()

    assert "-c" in captured_args
    assert captured_args[captured_args.index("-c") + 1] == "2"


async def test_ensure_proc_requests_the_devices_verified_safe_rate_not_geminis(monkeypatch) -> None:
    """`aplay` must always be told target_sample_rate_hz (the rate verified
    safe for THIS device), never Gemini's raw 24000 -- see the class
    docstring for the measured mismatch (24000 played ~30% fast)."""
    speaker = UsbSpeaker(alsa_device="hw:TEST,0", target_sample_rate_hz=32000)
    captured_args: list[str] = []

    async def fake_exec(*args: str, **kwargs: object):  # noqa: ANN202
        captured_args.extend(args)
        return RecordingProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    await speaker._ensure_proc()

    assert "-r" in captured_args
    assert captured_args[captured_args.index("-r") + 1] == "32000"


async def test_play_resamples_to_the_devices_verified_safe_rate() -> None:
    """2026-09-06 audit finding: a real Jabra Speak2 40 MS mis-times
    playback declared at Gemini's native 24000 Hz -- a precisely-generated
    10-second tone finished in ~7.66s (audibly faster/higher-pitched,
    matching a customer report), while the same test at 32000 Hz came back
    accurate. play() must resample 24000 -> target_sample_rate_hz
    (audioop.ratecv) before the bytes ever reach aplay, exactly matching
    what plain audioop.ratecv would produce for the same input."""
    speaker = UsbSpeaker(alsa_device="hw:TEST,0", target_sample_rate_hz=32000)
    proc = RecordingProcess()
    speaker._proc = proc

    # A few hundred ms of a 24000 Hz mono tone -- long enough for ratecv's
    # internal fractional-position math to do real work, not a 1-sample
    # edge case.
    mono = b"".join(
        struct.pack("<h", int(1000 * math.sin(2 * math.pi * 440 * i / 24000))) for i in range(2400)
    )
    expected_resampled, _ = audioop.ratecv(mono, 2, 1, 24000, 32000, None)
    expected_stereo = audioop.tostereo(expected_resampled, 2, 1.0, 1.0)

    await speaker.play(mono, sample_rate_hz=24000)

    assert len(proc.written) == 1
    assert proc.written[0] == expected_stereo


async def test_resample_is_a_no_op_when_source_already_matches_the_target() -> None:
    speaker = UsbSpeaker(alsa_device="hw:TEST,0", target_sample_rate_hz=32000)
    pcm = b"\x01\x02\x03\x04"

    assert speaker._resample(pcm, 32000) == pcm


async def test_stop_does_not_wait_for_a_stalled_play_to_release_the_lock() -> None:
    """Regression: stop() used to go `async with self._lock` before doing
    anything — but play() holds that SAME lock across write()+drain(), and
    drain() can block indefinitely on backpressure. Waiting for the lock
    first would mean waiting out exactly the audio a barge-in/"Замолчи" is
    trying to cut off, instead of interrupting it."""
    speaker = UsbSpeaker(alsa_device="hw:TEST,0", target_sample_rate_hz=16000)
    proc = StallingProcess()
    speaker._proc = proc

    play_task = asyncio.create_task(speaker.play(b"\x00\x00" * 100, sample_rate_hz=16000))
    await asyncio.sleep(0.05)  # let play() grab the lock and block inside drain()
    assert not play_task.done()

    await asyncio.wait_for(speaker.stop(), timeout=1.0)  # must not hang behind the lock

    assert proc.terminated is True

    play_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await play_task
