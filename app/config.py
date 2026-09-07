"""Settings for the RealtimeVoice service.

Mirrors the shape of `humanoid-robot`'s `GeminiLiveConfig`: the API key never
lives in code, only the env var name does. `.env` is loaded once, at import
time, from the project root next to `pyproject.toml`.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

from dotenv import load_dotenv

from app.scenarios import build_system_instruction

_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(_ROOT / ".env")

_DEFAULT_WS_HOST = "generativelanguage.googleapis.com"
# Confirmed working against the customer's key in ../../humanoid-robot on
# 13.08.2026 (see that repo's GeminiLiveConfig) — gemini-2.0-flash-live-001
# was NOT reachable with that key. Model availability is tied to the
# account/key, so override via GEMINI_LIVE_MODEL if this one 404s for you.
_DEFAULT_MODEL = "models/gemini-3.1-flash-live-preview"
# Item 10 of the 2026-09-04 fixes list: both profiles shared one voice
# (Fenrir) — picked per-role instead: Puck (upbeat, per Google's own voice
# descriptions) for R1's presenter role, Charon (informative/measured) for
# G1's security-desk role. Neither has been vetted by ear against this
# account's actual audio output — override with GEMINI_LIVE_VOICE if either
# reads wrong in practice; see https://ai.google.dev/gemini-api/docs/live
# for the full prebuilt voice list.
_DEFAULT_VOICE_BY_PROFILE = {"r1": "Puck", "g1": "Charon"}
_DEFAULT_TIMEZONE = "Asia/Almaty"
# Not live-verified against this account the way the Live model above was —
# same caveat applies: override via GEMINI_EMBEDDING_MODEL if this 404s.
_DEFAULT_EMBEDDING_MODEL = "models/gemini-embedding-001"


class Settings(BaseModel):
    # validate_default=True: robot_profile below is a Literal fed from an
    # env var, not a passed-in kwarg — pydantic v2 skips validating field
    # *defaults* unless asked to, so without this a typo'd ROBOT_PROFILE
    # would silently pass through instead of failing fast at startup.
    model_config = ConfigDict(frozen=True, extra="forbid", validate_default=True)

    # "r1" (presenter) or "g1" (security-desk guard) — one shared codebase,
    # different tool set/knowledge/system-instruction per profile (see
    # app/tool_registry.py, app/scenarios.py). Settings is a module-level
    # singleton read once at import time, so switching profiles means
    # restarting the process with a different ROBOT_PROFILE — there is no
    # runtime hot-swap, by design (this is a test bench, not a fleet).
    robot_profile: Literal["r1", "g1"] = os.environ.get("ROBOT_PROFILE", "r1")  # type: ignore[assignment]

    ws_host: str = _DEFAULT_WS_HOST
    api_key_env: str = "GEMINI_API_KEY"
    model: str = os.environ.get("GEMINI_LIVE_MODEL", _DEFAULT_MODEL)
    voice_name: str = os.environ.get(
        "GEMINI_LIVE_VOICE",
        _DEFAULT_VOICE_BY_PROFILE.get(os.environ.get("ROBOT_PROFILE", "r1"), "Puck"),
    )
    # BCP-47 code that PINS the spoken language. Empty (default) lets the
    # model answer in whatever language it was addressed in. Left empty on
    # purpose even though we restrict languages — speechConfig.languageCode
    # does not reliably constrain native-audio Live models' output language
    # (confirmed against Google's docs and a live google/adk-python issue);
    # the RU/KZ/EN-only rule lives in system_instruction instead (see
    # app/scenarios.py's _LANGUAGE_RULE).
    language_code: str = ""
    send_sample_rate_hz: int = 16_000  # what the browser sends TO Gemini
    receive_sample_rate_hz: int = 24_000  # what Gemini sends back
    default_timezone: str = _DEFAULT_TIMEZONE
    enable_transcription: bool = True
    open_timeout_s: float = 10.0
    reconnect_backoff_initial_s: float = 2.0
    reconnect_backoff_max_s: float = 30.0
    # Gemini Live itself closes a long-lived BidiGenerateContent session with
    # a 1008 "policy violation" once it's been open too long / accumulated
    # too much context. Two costs of hitting that instead of refreshing
    # first: (1) the hard cutoff itself loses the in-flight turn — audible
    # as the robot stuttering or going silent mid-answer — and (2) per-turn
    # latency creeps up as the same session's context keeps growing right up
    # until then, which is exactly the "иногда запинается и очень долго
    # отвечает" a customer reported 2026-09-06. Proactively reconnecting
    # BETWEEN turns before that point (see GeminiLiveAdapter.events())
    # avoids both — conversational memory is already not preserved across a
    # reconnect either way (no session-resumption token is used), so a
    # clean scheduled swap costs nothing extra (measured live: ~150-300ms
    # connecting -> connected, imperceptible between two turns).
    #
    # 360s was the original default (observed failure point on this project
    # was ~9m22s at the time). Tightened hard to 90s after live evidence,
    # same day, that it was nowhere near conservative enough for G1's real
    # usage: G1's logs showed Gemini forcing 1008 closures roughly every
    # 150-153s — CONSISTENTLY, three times in a row, never once reaching
    # the old 360s proactive refresh at all — while R1, in the same window,
    # hit zero involuntary closures and refreshed cleanly at its own
    # schedule. The exact reason G1 accumulates whatever Gemini is capping
    # on faster than R1 is unconfirmed (more/larger tool calls per minute
    # from search_guard_kb and visit_intake are the leading suspect), but
    # the mitigation doesn't require knowing why: refresh comfortably
    # before ANY observed forced closure, on both profiles, rather than
    # tuning this per-profile with no visibility into the next deployment's
    # real usage pattern. Tune further via env if a given site's logs still
    # show 1008s creeping in under 90s.
    max_session_duration_s: float = float(os.environ.get("MAX_SESSION_DURATION_S", "90"))

    # Gemini Live's OWN native answer to the same problem the setting above
    # works around from the outside — 2026-09-06, added the same day
    # MAX_SESSION_DURATION_S was tightened. Once a session's context passes
    # `context_window_compression_trigger_tokens`, Gemini compresses/
    # summarizes older turns internally instead of eventually forcing a
    # hard 1008 close — unlike our own proactive reconnect above, which
    # deliberately throws away ALL conversational memory every
    # max_session_duration_s seconds (no session-resumption token is used,
    # by design, since a clean swap was cheaper to reason about than
    # resumption), this lets a genuinely long conversation actually keep
    # its context. The two are complementary, not either/or: keep the
    # periodic reconnect too, as a safety net for whatever compression
    # doesn't cover (e.g. non-token limits) — this doesn't replace it, and
    # turning this on is not a reason to raise max_session_duration_s back
    # up without new evidence.
    context_window_compression_enabled: bool = (
        os.environ.get("CONTEXT_WINDOW_COMPRESSION_ENABLED", "true").lower() == "true"
    )
    # Both empty (default) — Google's own SDK examples enable compression
    # with no explicit values at all and let the platform pick sensible
    # ones (target defaults to trigger/2 server-side). Only set these if
    # logs show compression firing too late (context still grows unbounded)
    # or too often (choppy-feeling summarization) for a real deployment's
    # actual usage pattern.
    context_window_compression_trigger_tokens: int | None = (
        int(os.environ["CONTEXT_WINDOW_COMPRESSION_TRIGGER_TOKENS"])
        if os.environ.get("CONTEXT_WINDOW_COMPRESSION_TRIGGER_TOKENS")
        else None
    )
    context_window_compression_target_tokens: int | None = (
        int(os.environ["CONTEXT_WINDOW_COMPRESSION_TARGET_TOKENS"])
        if os.environ.get("CONTEXT_WINDOW_COMPRESSION_TARGET_TOKENS")
        else None
    )

    # Video-wall control agent (see app/wall/ — a local, Linux-runnable port
    # of ../../humanoid-robot/apps/wall-agent's `sim` driver). Point this at
    # a real wall-agent (--driver pipe, on the wall's Windows PC) in
    # production; the tool's HTTP contract does not change.
    wall_agent_url: str = os.environ.get("WALL_AGENT_URL", "http://localhost:8093")
    wall_agent_token: str = os.environ.get("WALL_AGENT_TOKEN", "")
    wall_agent_timeout_s: float = 5.0

    # Hard cap on ONE tool call (app/session_hub.py's _run_tool). The model
    # holds its turn until the tool answers, so this is dead air the visitor
    # actually stands through — past this point it's better to tell the
    # model the tool failed (it can say so) than to keep waiting out a
    # network timeout. Sits below the tools' own timeouts on purpose
    # (wall agent 5s, embeddings 20s), so it is the one that bites first.
    tool_timeout_s: float = float(os.environ.get("TOOL_TIMEOUT_S", "8"))

    # Robot mic/speaker (app/robot_audio.py, driven by app/main.py's SessionHub).
    # Defaults match this R1's /etc/humanoid-robot/voice-live.yaml exactly:
    # head-mic multicast group/port and the DDS control interface/IP.
    robot_mic_multicast_group: str = os.environ.get("ROBOT_MIC_GROUP", "239.168.123.161")
    robot_mic_multicast_port: int = int(os.environ.get("ROBOT_MIC_PORT", "5555"))
    robot_mic_interface_ip: str = os.environ.get("ROBOT_MIC_INTERFACE_IP", "192.168.123.164")
    # Unitree's own docs and every other implementation we found (humanoid-
    # robot, g1_gemini, unitree_converse, the official C++ SDK example)
    # assume a 6-channel far-field mix here. Measured live against this R1
    # on 02.09.2026 (per-packet wall-clock timing, not just byte-rate math):
    # the real stream is plain mono 16kHz PCM16. Treating it as 6-channel
    # silently kept 1 sample in 6, which played back sped-up and unintelligi-
    # ble — sounded like "noise", not a connectivity problem. If a genuinely
    # 6-channel G1 shows up, override with ROBOT_MIC_CHANNELS=6.
    robot_mic_input_channels: int = int(os.environ.get("ROBOT_MIC_CHANNELS", "1"))
    robot_speaker_network_interface: str = os.environ.get("ROBOT_SPEAKER_IFACE", "eth10")
    robot_speaker_volume_pct: int = int(os.environ.get("ROBOT_SPEAKER_VOLUME", "100"))
    # No AEC on this hardware: mic frames are dropped while the robot speaks,
    # plus this long after it stops, or it reliably hears (and reacts to)
    # its own voice.
    #
    # This is dead time on EVERY turn — the visitor typically starts
    # answering the moment the robot stops, and whatever they say inside
    # this window is thrown away, which both loses words and pushes back
    # the model's end-of-speech detection. It was 750ms (inherited from
    # humanoid-robot's voice_live/runner.py) back when the gate re-opened
    # on TurnComplete — i.e. while the tail of the utterance could still be
    # playing, so the margin was covering for that. app/session_hub.py now
    # re-opens it only once playback has actually finished
    # (_drain_speaker's _END_OF_TURN), so the margin only needs to cover
    # the speaker's own decay. Raise it again if the robot starts
    # interrupting itself; lower it further to shave the turn.
    robot_post_speech_guard_ms: int = int(os.environ.get("ROBOT_POST_SPEECH_GUARD_MS", "300"))

    # Presence detection (app/presence.py, app/robot_camera.py) — greets a
    # visitor once they appear, per R1 ТЗ §4.1 / G1 ТЗ §3.1. Camera access
    # itself is confirmed working on the real G1 (VideoClient.GetImageSample
    # returns real JPEG frames, no segfault) — but the auto-greet behaviour
    # is still off by default pending a live end-to-end check, and pending
    # resolving whether this camera's fisheye/downward field of view (it's
    # the go2-namespace "front video" service, likely a navigation camera,
    # not an eye-level one) is actually suitable for spotting an approaching
    # visitor. See README.
    presence_enabled: bool = os.environ.get("PRESENCE_ENABLED", "false").lower() == "true"
    presence_camera_network_interface: str = os.environ.get("PRESENCE_CAMERA_IFACE", "eth10")
    # Fraction of the (downscaled, grayscale) frame that must change to count
    # as "someone's here" — same default as humanoid-robot's apps/presence,
    # tuned for a mostly-static exhibition hall.
    presence_threshold: float = float(os.environ.get("PRESENCE_THRESHOLD", "0.02"))
    # Minimum seconds between one greeting and the next being possible —
    # single cooldown layer (see app/presence.py's module docstring for why
    # this project doesn't need humanoid-robot's second, service-level one).
    presence_rearm_s: float = float(os.environ.get("PRESENCE_REARM_S", "60.0"))
    # How often app/presence.py's detector samples the shared camera hub
    # (app/camera_hub.py) — independent of the hub's own DDS poll rate below,
    # since detecting "someone's here" doesn't need every frame.
    presence_fps: float = float(os.environ.get("PRESENCE_FPS", "2.0"))
    # The shared hub's actual DDS poll rate (app/camera_hub.py) — this is
    # what web/'s <img> camera view gets, so it's tuned for "looks live in a
    # browser" the way humanoid-robot's camera_mjpeg.py bridge is (its
    # default is 12fps; lower here since we don't know yet how this exact
    # VideoClient/DDS link tolerates sustained polling).
    presence_camera_fps: float = float(os.environ.get("PRESENCE_CAMERA_FPS", "8.0"))
    # "process" (default) runs the frame-diff scorer in its own OS process
    # (app/presence_worker.py, spawned by app/presence.py's
    # SubprocessMotionDetector) instead of a thread in this one — 2026-09-06
    # UX ask: presence detection must be "отдельный процесс от генерации
    # речи и слушания", isolated enough that a hiccup there (a slow frame
    # decode, a crash) can never add latency to or take down the voice
    # pipeline. "thread" keeps the old asyncio.to_thread-in-process behaviour
    # (app/presence.py's LocalMotionDetector) — lower overhead, useful for
    # local dev/tests where spawning a second `python -m` process per test
    # is unwanted.
    presence_isolation: str = os.environ.get("PRESENCE_ISOLATION", "process")

    # Where app/main.py (the browser text/status UI) plays Gemini's audio
    # back: "browser" streams it to the page over the WS (default — works
    # anywhere); "robot" plays it through the real robot speaker instead
    # (app/robot_audio.py's RobotSpeaker) while text input/status/tool log
    # stay in the browser; "usb" plays it through an external USB speaker
    # via ALSA (app/robot_audio.py's UsbSpeaker — item 6 of the 2026-09-04
    # fixes list) — needs usb_speaker_alsa_device set. Only "browser" works
    # off-robot; "robot" needs unitree_sdk2py, "usb" needs the actual USB
    # device plugged in.
    audio_output: str = os.environ.get("AUDIO_OUTPUT", "browser")
    # Symmetric input side: "browser" (default) reads mic audio from the
    # page's WS binary frames — no hardware mic is touched at all in this
    # mode. "robot" or "usb" turn on hardware-input mode: BOTH the built-in
    # head-mic (app/robot_audio.py's RobotMic) and, if usb_mic_alsa_device
    # below is set, an external USB mic (UsbMic) get constructed together
    # (see app/session_hub.py's __init__ — same pattern AUDIO_OUTPUT already
    # uses for _speakers), so an operator can switch between them LIVE, from
    # the website, with no restart (2026-09-06 ask, explicit: "микрофон...
    # выбирается не посредством .env файла, а напрямую на сайте" — the
    # CHOICE is made on the website, never by hand-editing this file).
    # That live switch writes its result back here (SessionHub's
    # _remember_audio_choice, 2026-09-07) so it survives a restart: this
    # field is both the starting preference for a fresh process AND the
    # record of what the operator last selected — see
    # SessionHub.set_preferred_mic_input.
    #
    # What this field does NOT decide at runtime is whether hardware audio
    # exists at all: "browser" versus "robot"/"usb" is read exactly once, in
    # SessionHub.__init__, so turning the robot's own mic and speaker on or
    # off is a restart-level change and stays with the setup panel.
    #
    # Exactly ONE of the constructed mics is ever allowed to run wake-word
    # detection at a time — 2026-09-06 ask, explicit: "не должно быть
    # [разделения и параллельного слушания]. Работает или/или. Оператор сам
    # выбирает." (this used to NOT be true: both ran their own independent
    # WakeWordDetector/faster-whisper pass concurrently, racing to "own" the
    # wake window — wasted CPU and an ambiguous answer to "which mic actually
    # woke the robot". See app/session_hub.py's `_hw_mic_owns_wake_window`'s
    # `_preferred_mic_input` gate for the fix.
    audio_input: str = os.environ.get("AUDIO_INPUT", "browser")

    # ALSA device for the USB mic (an external USB microphone, e.g. a USB
    # conference speakerphone, plugged into the robot's own onboard computer,
    # captured via ALSA/`arecord` rather than the vendor SDK — see
    # app/robot_audio.py's UsbMic). Name the ALSA card by its ID from
    # `arecord -l`/`/proc/asound/cards` (e.g. "hw:MS,0"), not its numeric
    # index, which can shift when other USB audio devices are plugged in.
    # Takes effect whenever AUDIO_INPUT is "robot" or "usb" (hardware-input
    # mode is on at all) — not only when it's specifically "usb", since both
    # hardware mics are always constructed together (see above); leave empty
    # to not offer a USB mic at all — most deployments don't have one.
    usb_mic_alsa_device: str = os.environ.get("USB_MIC_ALSA_DEVICE", "")

    # USB speaker for AUDIO_OUTPUT=usb (see above) — defaults to the SAME
    # device as usb_mic_alsa_device, since the common case (item 6 of the
    # 2026-09-04 fixes list) is one conference speakerphone (e.g. a Jabra
    # Speak2) serving as both mic and speaker; override separately only if
    # they're genuinely different devices.
    usb_speaker_alsa_device: str = os.environ.get(
        "USB_SPEAKER_ALSA_DEVICE", os.environ.get("USB_MIC_ALSA_DEVICE", "")
    )
    # ALSA mixer target set once at startup (UsbSpeaker.warm_up) — max by
    # default per that same fixes-list item ("сделаем вывод звука громче,
    # желательно на максимум").
    usb_speaker_volume_pct: int = int(os.environ.get("USB_SPEAKER_VOLUME", "100"))
    # How much audio RobotSpeaker pushes ahead of real time when it starts
    # an utterance — the buffer that absorbs a slow PlayStream RPC without
    # the robot audibly hitching between words. Measured on both real
    # robots (2026-09-07): the RPC costs ~3 ms at the median but 60-85 ms
    # at p99, so 200 ms of lead covers roughly six consecutive worst-case
    # spikes. Raise it if stutter is still audible on longer answers; the
    # cost is a potentially longer tail of already-buffered speech after a
    # barge-in or «Замолчи», since that only stops what has not been handed
    # to the vendor yet.
    robot_speaker_lead_ms: int = int(os.environ.get("ROBOT_SPEAKER_LEAD_MS", "200"))
    # 2026-09-06 audit finding: a real Jabra Speak2 40 MS mis-times playback
    # declared at Gemini's native 24000 Hz — a precisely-generated 10-second
    # tone finished in ~7.66s (audibly faster and higher-pitched: "голос
    # будто слишком быстрый и высокий"), while the SAME test at 32000 Hz
    # came back accurate to within normal process-start noise. UsbSpeaker
    # now always resamples to this rate before playing (see its docstring)
    # instead of trusting a USB device to honor an arbitrary declared rate.
    # 32000 is empirically verified correct for this exact device, not a
    # universal constant — override if a different USB speaker needs a
    # different one (test the same way: time a known-duration tone against
    # the wall clock at a few candidate rates and pick the one that comes
    # back accurate).
    usb_speaker_sample_rate_hz: int = int(os.environ.get("USB_SPEAKER_SAMPLE_RATE", "32000"))

    # Local wake-word gate for the hardware mic (app/wake_word.py, item 5 of
    # the 2026-09-04 fixes list) — on by default whenever a hardware mic
    # (robot or usb) is configured; set to false to fall back to the OLD
    # "manually pick a source" behaviour if it proves unreliable in the
    # field. Runs a small faster-whisper model on buffered audio windows and
    # checks the transcript for any of wake_word_phrases — real ASR, not a
    # trained wake-word classifier, so this works in Russian (or any
    # language Whisper knows) out of the box, unlike the first
    # (openWakeWord-based, English-only) attempt at this same feature.
    # wake_word_window_s is how much audio it buffers before each check —
    # larger catches longer phrases more reliably but adds latency;
    # wake_active_window_s (below) is the SEPARATE, much longer post-wake
    # forwarding duration.
    wake_word_enabled: bool = os.environ.get("WAKE_WORD_ENABLED", "true").lower() == "true"
    # Comma-separated — item 2 of the 2026-09-06 UX list: more than one
    # natural trigger word raises the odds a visitor who never read a sign
    # near the robot happens to say something that works. Whitespace around
    # each phrase is trimmed; empty entries are dropped.
    #
    # Expanded 2026-09-06 (follow-up: "чтобы человек мог интуитивно без
    # подсказок его активировать") past the original robot/assistant-style
    # trigger words to cover what a visitor would plausibly say WITHOUT
    # ever being told a magic word: an ordinary greeting approaching either
    # robot ("здравствуйте"/"привет"), a generic request for help
    # ("подскажите", "помощник"), and — specifically for G1 — the same
    # words its own system_instruction already recognises as visit-intake
    # triggers once it's listening (see app/scenarios.py's _G1_TOOL_RULES:
    # "оформите визит"/"я посетитель"/"зарегистрируйте меня"). Those last
    # ones matter here for a reason beyond convenience: before this, a
    # visitor who opened with exactly the phrase the robot is SUPPOSED to
    # recognise for visit intake ("оформите визит") would get no response
    # at all, because the hardware mic was never awake to hear it in the
    # first place — the wake gate ran before the model ever saw the audio.
    # Harmless to include on the R1 profile too (nobody says "оформите
    # визит" to a video-wall presenter; it just never fires there).
    #
    # Trade-off, worth knowing before tuning this further: broader/more
    # common words raise recall (robot wakes more reliably on natural
    # speech) at the cost of more false wakes from ambient conversation
    # that was never addressed to the robot ("привет"/"здравствуйте" are
    # the biggest offenders here — busy exhibition-hall chatter will say
    # these constantly). A false wake costs one wake_active_window_s
    # (45s default — see that field's own docstring for why it's no longer
    # 12s) of forwarded audio, not a spoken interruption by
    # itself — Gemini only actually replies if it decides something in
    # that window was addressed to it — but it's not free. If false wakes
    # turn out to be a real nuisance on a given deployment, trim this list
    # (or drop the two plain greetings) via WAKE_WORD_PHRASES in `.env`
    # rather than editing code.
    wake_word_phrases: tuple[str, ...] = tuple(
        p.strip()
        for p in os.environ.get(
            "WAKE_WORD_PHRASES",
            "робот,ассистент,помощник,слушай,здравствуйте,привет,подскажите,"
            "посетитель,визит,оформите,зарегистрируйте",
        ).split(",")
        if p.strip()
    )
    wake_word_language: str = os.environ.get("WAKE_WORD_LANGUAGE", "ru")
    wake_word_whisper_model: str = os.environ.get("WAKE_WORD_WHISPER_MODEL", "tiny")
    wake_word_window_s: float = float(os.environ.get("WAKE_WORD_WINDOW_S", "1.5"))
    # How long the hardware mic keeps forwarding to Gemini after a wake hit
    # (or the last robot turn, whichever is later — see
    # app/session_hub.py's TurnComplete handling) before it goes back to
    # wake-word-only listening.
    #
    # Raised from 12.0 to 45.0 on 2026-09-06, by explicit request: "нужно
    # достаточно большое окно, чтобы люди могли нормально с ним
    # коммуницировать на презентации". 12s was tuned for quick back-and-
    # forth, not for a visitor who pauses to read the wall, think about
    # their next question, or point at something before speaking again —
    # at 12s that pause alone was enough to put the mic back to sleep,
    # forcing them to repeat the wake phrase mid-conversation, which reads
    # as the robot losing interest or malfunctioning, not as a timeout
    # doing its job. The window still resets on every robot turn, so an
    # ACTIVE back-and-forth never approaches this ceiling regardless of its
    # value — this only matters for the length of a single silent pause
    # within one still-open conversation. Trade-off, same as before: a
    # longer window means more time a false wake (or a visitor who
    # wandered off without saying "goodbye") keeps forwarding ambient audio
    # to Gemini before falling back asleep on its own — 45s is a deliberate
    # middle ground, not "keep it open indefinitely".
    wake_active_window_s: float = float(os.environ.get("WAKE_ACTIVE_WINDOW_S", "45.0"))

    # RAG over app/../knowledge/ (see app/knowledge.py) — drop .txt/.md files
    # in there and control_video_wall's neighbour tool, search_knowledge,
    # embeds and searches them. Not a Structured Facts Store (see
    # ../CLAUDE.md's Structured Facts Store vs. RAG distinction) — for facts
    # that must never be wrong, a typed lookup is still the right answer;
    # this is for prose/explanatory material.
    knowledge_embedding_model: str = os.environ.get(
        "GEMINI_EMBEDDING_MODEL", _DEFAULT_EMBEDDING_MODEL
    )
    knowledge_top_k: int = int(os.environ.get("KNOWLEDGE_TOP_K", "3"))

    # G1 profile only: guard_kb.py's room/FAQ reference data, and the
    # append-only JSONL log of completed visit cards (app/g1_tools.py's
    # VisitJournal). Harmless if unused on the r1 profile.
    guard_kb_path: str = os.environ.get("GUARD_KB_PATH", str(_ROOT / "config" / "guard-kb.yaml"))
    visit_journal_path: str = os.environ.get(
        "VISIT_JOURNAL_PATH", str(_ROOT / "data" / "visits.jsonl")
    )

    @property
    def system_instruction(self) -> str:
        """Profile-specific instruction (see app/scenarios.py) — not a plain
        field because it depends on robot_profile, which is fixed at
        startup but known only once Settings() has actually run."""
        return build_system_instruction(self.robot_profile)

    @property
    def knowledge_dir(self) -> str:
        """RAG source directory (app/knowledge.py) — r1 keeps the original
        default; g1 gets its own directory. KNOWLEDGE_DIR still overrides
        either, e.g. to point a given profile at custom content."""
        default_name = "knowledge" if self.robot_profile == "r1" else "knowledge_guard"
        return os.environ.get("KNOWLEDGE_DIR", str(_ROOT / default_name))

    def resolved_api_key(self) -> str:
        return os.environ.get(self.api_key_env, "")


settings = Settings()
