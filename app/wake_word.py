"""Local Russian-language wake-word gate for the hardware mic (item 5 of the
2026-09-04 fixes list, corrected the same day: "пробуждение по ключевому
слову должно работать на русском языке"). The robot used to forward EVERY
sound the physical mic picked up straight to Gemini, all day — meaning it
was transcribing/reacting to every nearby conversation, not just people
actually addressing it. This module answers "was the wake phrase just said"
locally, in-process, with no audio ever leaving the robot for that check —
only after a hit does app/session_hub.py start forwarding the hardware mic
to Gemini at all (see `_drain_hw_mic`'s ``wake_detector`` handling).

First attempt used openWakeWord (trained keyword-spotting models) — reverted
the same day: openWakeWord ships no Russian/Kazakh model, only English
placeholders ("hey jarvis", "alexa", ...), and training a custom one needs a
whole separate synthetic-data/training pipeline. This version instead runs a
small `faster-whisper` ASR model (CPU, int8, same engine already used
elsewhere on these robots for Russian/Kazakh speech — see
`~/models/faster-whisper-*` on G1) on short buffered windows of hardware-mic
audio and checks whether the transcript contains the configured phrase. Real
ASR, not a trained wake-word classifier, so it works in any language Whisper
understands (Russian by default) with zero extra training — the tradeoff is
latency (transcribing ~1.5s of audio takes a couple hundred ms on a tiny
model, not the ~10ms a dedicated wake-word model would take), which is why
`feed()` is async and offloads the actual inference to a thread (see
app/session_hub.py's `_drain_hw_mic`, which awaits it inline, but Whisper
work never blocks the event loop while it runs).
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from typing import Any

_LOG = logging.getLogger("wake_word")

_SAMPLE_RATE_HZ = 16_000
_SAMPLE_WIDTH_BYTES = 2
_PCM16_FULL_SCALE = 32768.0


def _normalize(text: str) -> str:
    """Lowercase, strip punctuation — good enough for a substring check
    against a single short trigger word/phrase, not meant to handle
    inflection (e.g. "робота" won't match "робот"). Pick a phrase whose
    dictionary form is what people would naturally say, or list a couple of
    likely forms and check all of them (see WakeWordDetector.phrases)."""
    return re.sub(r"[^\w\s]", "", text, flags=re.UNICODE).lower().strip()


@dataclass
class WakeWordDetector:
    """Feed it mono PCM16 16kHz audio; `await feed()` returns True the
    moment a buffered window's transcript contains ANY of `phrases` — more
    than one natural trigger word raises the odds a visitor who never read
    a sign near the robot happens to say something that works (item 2 of
    the 2026-09-06 UX list: "робот" alone isn't an obvious thing to say to
    a robot; "слушай"/"ассистент" etc. are reasonable alternates). Internally
    keeps a SLIDING `window_s`-second buffer and re-checks it every
    `window_s / 2` seconds of new audio — callers can feed any chunk size
    (both RobotMic and UsbMic use 50ms chunks).

    UX audit finding, 2026-09-06: this used to chomp-and-clear — buffer
    exactly `window_s` seconds, check it, then start the NEXT window from
    empty. A wake phrase spoken across that boundary (said just before, or
    straddling, the cut) got split between two windows and missed in BOTH,
    forcing the visitor to repeat themselves for no reason they could see.
    Overlapping checks mean any `window_s`-long phrase is fully contained in
    at least one check regardless of when it started — at roughly 2x the
    Whisper calls (still a tiny CPU cost on a `tiny` model), which is a
    trade worth making for "the robot didn't hear me" being a rare visitor
    complaint instead of a routine one.
    """

    phrases: tuple[str, ...] = ("робот",)
    language: str = "ru"
    model_name: str = "tiny"
    window_s: float = 1.5
    _model: Any = field(default=None, init=False, repr=False)
    _buffer: bytearray = field(default_factory=bytearray, init=False)
    _since_last_check: int = field(default=0, init=False)
    _normalized_phrases: tuple[str, ...] = field(default=(), init=False)

    def __post_init__(self) -> None:
        self._normalized_phrases = tuple(_normalize(p) for p in self.phrases if p.strip())

    def _ensure_model(self) -> Any:
        if self._model is None:
            # Lazy: importing faster_whisper pulls in ctranslate2/numpy, and
            # constructing WhisperModel loads real model weights (from a
            # local cache if already downloaded, else Hugging Face Hub —
            # first run needs internet) — no reason to pay that cost for a
            # process that never configured a hardware mic at all.
            from faster_whisper import WhisperModel

            self._model = WhisperModel(self.model_name, device="cpu", compute_type="int8")
            _LOG.info(
                "wake_word.model_loaded model=%s phrases=%r language=%s",
                self.model_name,
                self.phrases,
                self.language,
            )
        return self._model

    @property
    def recent_audio(self) -> bytes:
        """The current sliding window's raw PCM16 — up to `window_s`
        seconds, ending at the most recently fed chunk.

        Exposed so a caller can REPLAY it into the live session once a wake
        hit fires (see app/session_hub.py's `_hw_mic_owns_wake_window`).
        2026-09-06 audit finding, prompted by a direct user question ("won't
        the robot miss the beginning of the sentence?"): `feed()` needs a
        FULL `window_s`-second window to even attempt a check, so by the
        time it returns True, the wake phrase — and anything the visitor
        said in the same breath right up to that instant — has ALREADY
        been spoken, entirely inside this buffer. The old integration only
        forwarded the SINGLE ~50ms chunk being fed at the exact moment of
        detection onward to Gemini; everything before that (i.e. the
        actual "Робот, оформите визит" the visitor just said) was seen
        only by this local check and then silently discarded, never
        reaching the model at all. A visitor who said their whole request
        in one breath right after the wake word could have Gemini hear
        nothing but the tail end of it.
        """
        return bytes(self._buffer)

    async def warm_up(self) -> None:
        """Load the Whisper model now, off the event loop, instead of on
        whoever happens to say the wake phrase first. Without this the
        FIRST wake-word check of the process's life pays full model-load
        cost (weights already on disk: still tens to hundreds of ms;
        nothing cached yet / first run: a real network fetch) inline,
        inside that visitor's own `feed()` call — exactly the worst moment
        for a multi-second stall, since it's usually also the first thing a
        customer watching a demo ever says to the robot. Mirrors
        app/session_hub.py's `_warm_up` already doing the same for the
        speaker/knowledge-base one-off costs.
        """
        await asyncio.to_thread(self._ensure_model)

    async def feed(self, pcm16: bytes) -> bool:
        self._buffer.extend(pcm16)
        self._since_last_check += len(pcm16)
        window_bytes = int(self.window_s * _SAMPLE_RATE_HZ) * _SAMPLE_WIDTH_BYTES
        hop_bytes = max(1, window_bytes // 2)
        # Keep only the most recent `window_s` seconds — a SLIDING window,
        # not a chomp-and-clear one (see the class docstring).
        if len(self._buffer) > window_bytes:
            del self._buffer[: len(self._buffer) - window_bytes]
        if len(self._buffer) < window_bytes or self._since_last_check < hop_bytes:
            return False
        self._since_last_check = 0
        chunk = bytes(self._buffer)
        # Whisper inference (tens to hundreds of ms, even for a tiny model on
        # a short window) is real CPU work — never call it inline from the
        # event loop, or every wake-check window stalls all other sessions/
        # WS traffic for that long.
        return await asyncio.to_thread(self._check, chunk)

    def _check(self, chunk: bytes) -> bool:
        import numpy as np

        model = self._ensure_model()
        audio = np.frombuffer(chunk, dtype=np.int16).astype(np.float32) / _PCM16_FULL_SCALE
        segments, _info = model.transcribe(
            audio,
            language=self.language,
            beam_size=1,
            vad_filter=True,
            condition_on_previous_text=False,
        )
        heard = _normalize(" ".join(segment.text for segment in segments))
        detected = bool(heard) and any(p in heard for p in self._normalized_phrases)
        if detected:
            _LOG.info("wake_word.detected phrases=%r heard=%r", self.phrases, heard)
        elif heard:
            # Item 4 of the 2026-09-06 UX list ("барометр уверенности"): log
            # every NON-EMPTY miss too, not just hits — this is the raw
            # material for calibrating the phrase list/threshold later (grep
            # `wake_word.miss` in the logs to see what Whisper actually heard
            # near the mic that DIDN'T match, e.g. a visitor saying something
            # close but not quite "робот"). DEBUG, not INFO: this fires on
            # ordinary nearby conversation too, not just near-misses, and
            # would otherwise drown the log at the default level.
            _LOG.debug("wake_word.miss heard=%r", heard)
        return detected
