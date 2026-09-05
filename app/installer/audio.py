"""ALSA device discovery + test record/playback for the wizard's audio step.

Shells out to `arecord`/`aplay` (alsa-utils — already on the robot per
README.md's note on app/robot_audio.py's `UsbMic`) instead of a Python ALSA
binding: no extra system package, and it degrades to an empty device list
(not a crash) on a dev machine with no ALSA at all — see `_run` below.
"""

from __future__ import annotations

import asyncio
import math
import re
import struct
import tempfile
import wave
from dataclasses import dataclass
from pathlib import Path

_DEVICE_LINE = re.compile(r"^card (\d+): (\S+) \[(.*?)\], device (\d+): (.*?)\s*$")


@dataclass(frozen=True)
class AlsaDevice:
    alsa_id: str  # e.g. "hw:MS,0" — what goes into USB_MIC_ALSA_DEVICE
    label: str  # e.g. "Jabra SPEAK2 40 MS — USB Audio (card MS)"


class AudioTestError(RuntimeError):
    pass


async def _run(*args: str, timeout_s: float = 10.0) -> tuple[int, str]:
    try:
        proc = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
        )
    except FileNotFoundError:
        return 127, ""
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
    except TimeoutError:
        # 2026-09-06 audit finding: a busy/exclusively-locked ALSA device
        # (most realistically: the main r1/g1 service is already holding
        # this same USB mic/speaker via AUDIO_INPUT/OUTPUT=usb while the
        # customer reopens this step from "maintenance mode") can make
        # arecord/aplay hang trying to just OPEN the device, before
        # record_sample's `-d` duration even starts counting. Without
        # this timeout the browser's "Говорите…"/"Воспроизвожу…" spinner
        # never resolved, with no way for the customer to know it was
        # stuck rather than still working.
        proc.kill()
        await proc.wait()
        return 124, "устройство не отвечает (возможно, занято другим процессом)"
    return proc.returncode or 0, out.decode("utf-8", errors="replace")


def _parse(output: str) -> list[AlsaDevice]:
    devices = []
    for line in output.splitlines():
        m = _DEVICE_LINE.match(line.strip())
        if not m:
            continue
        _card_num, card_id, card_name, device_num, device_name = m.groups()
        devices.append(
            AlsaDevice(
                alsa_id=f"hw:{card_id},{device_num}",
                label=f"{card_name} — {device_name} (card {card_id})",
            )
        )
    return devices


async def list_capture_devices() -> list[AlsaDevice]:
    _code, out = await _run("arecord", "-l")
    return _parse(out)


async def list_playback_devices() -> list[AlsaDevice]:
    _code, out = await _run("aplay", "-l")
    return _parse(out)


async def record_sample(alsa_device: str, seconds: float = 2.0) -> Path:
    """Records `seconds` of audio from `alsa_device` to a temp WAV — the
    caller streams it straight back to the browser so the customer can
    literally listen to their own test recording. A pass/fail check alone
    can't tell a working-but-too-quiet mic from a genuinely dead one; a
    "прослушать" button can."""
    out_path = Path(tempfile.mkstemp(suffix=".wav", prefix="realtimevoice-mic-test-")[1])
    code, out = await _run(
        "arecord", "-D", alsa_device, "-f", "S16_LE", "-r", "16000", "-c", "1",
        "-d", str(max(1, int(seconds))), str(out_path),
        timeout_s=seconds + 5.0,  # generous margin over -d itself for process startup/teardown
    )
    if code != 0 or not out_path.exists() or out_path.stat().st_size < 100:
        out_path.unlink(missing_ok=True)
        raise AudioTestError(f"Запись с устройства {alsa_device} не удалась. {out.strip()}")
    return out_path


def _generate_test_tone(seconds: float = 1.5, freq_hz: float = 440.0, sample_rate: int = 16_000) -> Path:
    """A short fading-in/out sine beep — stdlib-only (no numpy dependency
    needed just for this), used so `play_test_tone` has something audible
    and unambiguous to push through the chosen speaker."""
    out_path = Path(tempfile.mkstemp(suffix=".wav", prefix="realtimevoice-speaker-test-")[1])
    n_samples = int(seconds * sample_rate)
    fade_s = 0.05
    frames = bytearray()
    for i in range(n_samples):
        t = i / sample_rate
        fade = min(1.0, t / fade_s, (seconds - t) / fade_s)
        sample = int(fade * 12_000 * math.sin(2 * math.pi * freq_hz * t))
        frames += struct.pack("<h", sample)
    with wave.open(str(out_path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(bytes(frames))
    return out_path


async def play_test_tone(alsa_device: str) -> None:
    """Plays a short beep through `alsa_device`. Software can't detect
    whether the customer actually heard it — the wizard's UI asks them to
    confirm with a "да, слышал(а)" / "нет" choice after calling this."""
    tone_path = _generate_test_tone()
    try:
        code, out = await _run("aplay", "-D", alsa_device, str(tone_path))
        if code != 0:
            raise AudioTestError(f"Воспроизведение через {alsa_device} не удалось. {out.strip()}")
    finally:
        tone_path.unlink(missing_ok=True)
