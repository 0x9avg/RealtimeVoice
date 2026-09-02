"""Settings for the RealtimeVoice prototype.

Mirrors the shape of `humanoid-robot`'s `GeminiLiveConfig`: the API key never
lives in code, only the env var name does. `.env` is loaded once, at import
time, from the project root next to `pyproject.toml`.
"""

from __future__ import annotations

import os
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

_DEFAULT_WS_HOST = "generativelanguage.googleapis.com"
# Confirmed working against the customer's key in ../../humanoid-robot on
# 13.08.2026 (see that repo's GeminiLiveConfig) — gemini-2.0-flash-live-001
# was NOT reachable with that key. Model availability is tied to the
# account/key, so override via GEMINI_LIVE_MODEL if this one 404s for you.
_DEFAULT_MODEL = "models/gemini-3.1-flash-live-preview"
_DEFAULT_VOICE = "Fenrir"
_DEFAULT_TIMEZONE = "Asia/Almaty"


class Settings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    ws_host: str = _DEFAULT_WS_HOST
    api_key_env: str = "GEMINI_API_KEY"
    model: str = os.environ.get("GEMINI_LIVE_MODEL", _DEFAULT_MODEL)
    voice_name: str = os.environ.get("GEMINI_LIVE_VOICE", _DEFAULT_VOICE)
    # BCP-47 code that PINS the spoken language. Empty (default) lets the
    # model answer in whatever language it was addressed in.
    language_code: str = ""
    system_instruction: str = (
        "You are a helpful voice assistant running in a developer prototype. "
        "Speak naturally and concisely. When asked for the current time or "
        "date, always call the get_current_time tool instead of guessing — "
        "you have no reliable sense of the current date on your own."
    )
    send_sample_rate_hz: int = 16_000  # what the browser sends TO Gemini
    receive_sample_rate_hz: int = 24_000  # what Gemini sends back
    default_timezone: str = _DEFAULT_TIMEZONE
    enable_transcription: bool = True
    open_timeout_s: float = 10.0
    reconnect_backoff_initial_s: float = 2.0
    reconnect_backoff_max_s: float = 30.0

    def resolved_api_key(self) -> str:
        return os.environ.get(self.api_key_env, "")


settings = Settings()
