"""app/wake_word.py's WakeWordDetector — buffering/warm-up contract.

`_check()` (real faster-whisper inference) is monkeypatched out everywhere
here: these tests are about the buffering/scheduling logic around it (the
sliding window, the warm_up delegation), not about ASR accuracy, and
loading a real model is slow and needs network on a cold cache.
"""

from __future__ import annotations

from app.config import Settings
from app.wake_word import WakeWordDetector, _normalize

_SAMPLE_RATE_HZ = 16_000
_SAMPLE_WIDTH_BYTES = 2


def test_natural_g1_opening_lines_match_a_configured_default_phrase() -> None:
    """Ties app/config.py's default WAKE_WORD_PHRASES to the actual
    substring-match logic _check() uses (see WakeWordDetector._check) --
    without importing/running real Whisper. A visitor who was never told a
    trigger word, opening with an ordinary greeting or the exact phrase G1
    is listening for to start visit intake, must match something."""
    normalized_phrases = tuple(_normalize(p) for p in Settings().wake_word_phrases)
    natural_openings = [
        "Здравствуйте, оформите визит, пожалуйста",
        "Привет, подскажите пожалуйста где кабинет 305",
        "Добрый день, я посетитель, зарегистрируйте меня",
    ]
    for line in natural_openings:
        heard = _normalize(line)
        assert any(p in heard for p in normalized_phrases), f"nothing matched: {line!r}"


def _silence(seconds: float) -> bytes:
    return b"\x00\x00" * int(seconds * _SAMPLE_RATE_HZ)


async def test_feed_checks_on_a_sliding_window_not_a_chomp_and_clear_one() -> None:
    """UX audit finding, 2026-09-06: the old chomp-and-clear buffering
    checked window_s-long, NON-overlapping chunks -- a phrase spoken across
    that boundary was split between two checks and missed in both. The
    fixed version checks every window_s/2 seconds, using the last window_s
    seconds each time, so consecutive checks overlap by half a window."""
    detector = WakeWordDetector(window_s=1.0)
    checked_lengths: list[int] = []
    detector._check = lambda chunk: (checked_lengths.append(len(chunk)), False)[1]  # type: ignore[method-assign]

    window_bytes = int(1.0 * _SAMPLE_RATE_HZ) * _SAMPLE_WIDTH_BYTES
    hop_bytes = window_bytes // 2

    # Feed less than one window: no check yet.
    await detector.feed(_silence(0.4))
    assert checked_lengths == []

    # Cross the first full window: first check fires, using the whole
    # window's worth of audio buffered so far.
    await detector.feed(_silence(0.6))
    assert checked_lengths == [window_bytes]

    # Only half a window of NEW audio since the last check: fires again
    # (not waiting for a second full window), and still checks a full
    # window's worth (the sliding buffer, half of it carried over).
    await detector.feed(_silence(0.5))
    assert checked_lengths == [window_bytes, window_bytes]

    # Sanity: the hop really is half the window, not the whole thing --
    # feeding less than hop_bytes' worth must not trigger a third check.
    checked_lengths.clear()
    await detector.feed(b"\x00\x00" * (hop_bytes // 2 // _SAMPLE_WIDTH_BYTES))
    assert checked_lengths == []


async def test_a_phrase_spanning_the_old_chomp_boundary_is_still_caught() -> None:
    """Concrete regression: with a phrase that would have landed exactly on
    the old fixed-window cut, the sliding window must still present it
    whole to at least one check."""
    detector = WakeWordDetector(window_s=1.0)
    seen_chunks: list[bytes] = []

    def fake_check(chunk: bytes) -> bool:
        seen_chunks.append(chunk)
        return False

    detector._check = fake_check  # type: ignore[method-assign]

    # A distinctive 0.3s marker placed so it straddles where the old
    # implementation's first window boundary (1.0s) would have fallen: fed
    # starting at 0.85s, ending at 1.15s.
    marker = b"\x01\x02" * int(0.3 * _SAMPLE_RATE_HZ)
    await detector.feed(_silence(0.85))
    await detector.feed(marker)
    await detector.feed(_silence(2.0))  # enough trailing audio for later checks too

    assert any(marker in chunk for chunk in seen_chunks), (
        "the marker must appear intact (not split across two checks) in at "
        "least one of the sliding-window checks"
    )


async def test_recent_audio_holds_the_full_window_the_moment_a_hit_fires() -> None:
    """2026-09-06 audit finding, prompted by a direct question: "won't the
    robot miss the beginning of the sentence?" feed() needs a FULL
    window_s-second buffer to even attempt a check -- so by the time it
    returns True, the wake phrase has already been spoken, entirely inside
    that buffer. app/session_hub.py's _hw_mic_owns_wake_window replays
    `recent_audio` into the live session on a hit instead of forwarding
    only the single most-recently-fed chunk -- this pins down that
    `recent_audio` actually holds the full window (not just the latest
    chunk) at exactly that moment, using the REAL sliding-window buffering
    (not the session_hub tests' simplified FakeWakeDetector)."""
    detector = WakeWordDetector(window_s=1.0)
    hits: list[bytes] = []

    def fake_check(chunk: bytes) -> bool:
        # "Detect" on the first full window -- mirrors a visitor whose wake
        # phrase lands right at the start of the buffered audio.
        return len(hits) == 0

    detector._check = fake_check  # type: ignore[method-assign]

    window_bytes = int(1.0 * _SAMPLE_RATE_HZ) * _SAMPLE_WIDTH_BYTES
    marker = b"\x01\x02" * (int(0.2 * _SAMPLE_RATE_HZ))  # a distinctive "wake phrase" chunk

    # marker (0.2s) + 0.8s silence = exactly window_bytes worth of audio in
    # one feed() call -- anything MORE would truncate the marker off the
    # front of the sliding buffer before the check even runs.
    triggered = await detector.feed(marker + _silence(0.8))
    if triggered:
        hits.append(detector.recent_audio)

    assert hits, "the fake check must have fired on this first full window"
    assert len(hits[0]) == window_bytes
    assert hits[0].startswith(marker), (
        "recent_audio at the moment of detection must still contain the "
        "wake phrase itself, not just whatever chunk was fed most recently"
    )


async def test_warm_up_loads_the_model_off_thread_without_running_a_check() -> None:
    """UX audit finding, 2026-09-06: without this, the FIRST wake-word
    check of the process's life paid the full model-load cost inline,
    inside whichever visitor happened to say the wake phrase first."""
    detector = WakeWordDetector()
    calls = 0

    def fake_ensure_model() -> None:
        nonlocal calls
        calls += 1

    detector._ensure_model = fake_ensure_model  # type: ignore[method-assign]

    await detector.warm_up()

    assert calls == 1
