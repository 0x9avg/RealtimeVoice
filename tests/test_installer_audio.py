"""app/installer/audio.py's `_parse` — turns raw `arecord -l`/`aplay -l`
text into a friendly device list, without needing real ALSA hardware to
test against."""

from __future__ import annotations

import asyncio

from app.installer.audio import _parse, _run

_SAMPLE_ARECORD_L = """\
**** List of CAPTURE Hardware Devices ****
card 0: PCH [HDA Intel PCH], device 0: ALC256 Analog [ALC256 Analog]
  Subdevices: 1/1
  Subdevice #0: subdevice #0
card 2: MS [Jabra SPEAK2 40 MS], device 0: USB Audio [USB Audio]
  Subdevices: 1/1
  Subdevice #0: subdevice #0
"""


def test_parse_extracts_alsa_id_and_a_human_readable_label() -> None:
    devices = _parse(_SAMPLE_ARECORD_L)

    assert len(devices) == 2
    assert devices[1].alsa_id == "hw:MS,0"
    assert "Jabra SPEAK2 40 MS" in devices[1].label
    assert "MS" in devices[1].label


def test_parse_on_empty_output_returns_no_devices() -> None:
    assert _parse("") == []


def test_parse_ignores_non_device_lines() -> None:
    devices = _parse("**** List of CAPTURE Hardware Devices ****\n  Subdevices: 1/1\n")
    assert devices == []


async def test_run_times_out_and_kills_a_hung_process_instead_of_blocking_forever() -> None:
    """2026-09-06 audit finding: a busy/exclusively-locked ALSA device can
    make arecord/aplay hang trying to just open it, before any `-d`
    duration even starts counting -- without a timeout here, the
    installer's test-mic/test-speaker endpoints (and the browser spinner
    waiting on them) would block forever with no way for the customer to
    know it was stuck."""
    code, out = await asyncio.wait_for(
        _run("sleep", "5", timeout_s=0.1), timeout=2.0
    )
    assert code == 124
    assert "не отвечает" in out
