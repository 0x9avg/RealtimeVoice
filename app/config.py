"""Settings for the RealtimeVoice prototype.

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
_DEFAULT_VOICE = "Fenrir"
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
    voice_name: str = os.environ.get("GEMINI_LIVE_VOICE", _DEFAULT_VOICE)
    # BCP-47 code that PINS the spoken language. Empty (default) lets the
    # model answer in whatever language it was addressed in. Left empty on
    # purpose even though we restrict languages — speechConfig.languageCode
    # does not reliably constrain native-audio Live models' output language
    # (confirmed against Google's docs and a live google/adk-python issue);
    # the RU/KZ-only rule lives in system_instruction instead (see
    # app/scenarios.py's _LANGUAGE_RULE).
    language_code: str = ""
    send_sample_rate_hz: int = 16_000  # what the browser sends TO Gemini
    receive_sample_rate_hz: int = 24_000  # what Gemini sends back
    default_timezone: str = _DEFAULT_TIMEZONE
    enable_transcription: bool = True
    open_timeout_s: float = 10.0
    reconnect_backoff_initial_s: float = 2.0
    reconnect_backoff_max_s: float = 30.0

    # Video-wall control agent (see app/wall/ — a local, Linux-runnable port
    # of ../../humanoid-robot/apps/wall-agent's `sim` driver). Point this at
    # a real wall-agent (--driver pipe, on the wall's Windows PC) in
    # production; the tool's HTTP contract does not change.
    wall_agent_url: str = os.environ.get("WALL_AGENT_URL", "http://localhost:8093")
    wall_agent_token: str = os.environ.get("WALL_AGENT_TOKEN", "")
    wall_agent_timeout_s: float = 5.0

    # Robot mic/speaker (app/robot_main.py — the on-robot runner, no browser).
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
    # its own voice. Matches humanoid-robot's voice_live/runner.py.
    robot_post_speech_guard_ms: int = int(os.environ.get("ROBOT_POST_SPEECH_GUARD_MS", "750"))

    # Where app/main.py (the browser text/status UI) plays Gemini's audio
    # back: "browser" streams it to the page over the WS (default — works
    # anywhere); "robot" plays it through the real robot speaker instead
    # (app/robot_audio.py's RobotSpeaker) while text input/status/tool log
    # stay in the browser. Only meaningful on the robot itself — RobotSpeaker
    # needs unitree_sdk2py, which isn't installed off-robot.
    audio_output: str = os.environ.get("AUDIO_OUTPUT", "browser")
    # Symmetric input side: "browser" reads mic audio from the page's WS
    # binary frames (default); "robot" instead runs a background task
    # streaming the real head-mic multicast (app/robot_audio.py's RobotMic,
    # half-duplex-gated) into the session — the browser's own mic button
    # becomes irrelevant in this mode. Set both AUDIO_INPUT and AUDIO_OUTPUT
    # to "robot" for the full on-robot experience with browser-side status.
    audio_input: str = os.environ.get("AUDIO_INPUT", "browser")

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
