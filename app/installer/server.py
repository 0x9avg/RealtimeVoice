"""FastAPI app for the guided setup wizard — see `app/installer/__init__.py`
for why this is a separate process from `app.main:app`.

Deliberately does NOT import `app.config.Settings` — that singleton is
built once at import time from whatever `.env` already contains, which is
exactly the thing this wizard is helping create/fix; if `.env` is missing
or half-written (the normal state on a first run), `Settings()` should not
be a precondition for the wizard itself to come up. All `.env` reads/writes
here go through `app.installer.env_file` instead, which tolerates a
missing or partially-filled file.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Literal

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel

from app.installer import audio, env_file, gemini_check, logs, network, settings_schema, systemd

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
_LOG = logging.getLogger("installer")

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_ENV_PATH = _PROJECT_ROOT / ".env"
_DEPLOY_DIR = _PROJECT_ROOT / "deploy"
_STATIC_DIR = Path(__file__).resolve().parent / "static"

_PROFILE_META: dict[str, dict[str, object]] = {
    "r1": {
        "template": _PROJECT_ROOT / ".env.r1.example",
        "unit": "realtimevoice-r1.service",
        "port": 8000,
        "label": "R1 — робот-презентатор (видеостена)",
    },
    "g1": {
        "template": _PROJECT_ROOT / ".env.g1.example",
        "unit": "realtimevoice-g1.service",
        # 2026-09-06: reverted to the same port as R1 (8000), by explicit
        # request -- see deploy/realtimevoice-g1.service's comment.
        "port": 8000,
        "label": "G1 — робот поста охраны",
    },
}

app = FastAPI(title="RealtimeVoice — мастер настройки")


def _current_profile() -> str | None:
    profile = env_file.read_values(_ENV_PATH).get("ROBOT_PROFILE")
    return profile if profile in _PROFILE_META else None


def _require_profile() -> str:
    profile = _current_profile()
    if profile is None:
        raise HTTPException(400, "Сначала выберите робота (шаг «Какой это робот»).")
    return profile


# --------------------------------------------------------------------------
# State / status
# --------------------------------------------------------------------------


@app.get("/api/state")
async def get_state() -> dict[str, object]:
    profile = _current_profile()
    values = env_file.read_values(_ENV_PATH)
    service_status: dict[str, object] | None = None
    service_installed = False
    if profile is not None:
        unit = str(_PROFILE_META[profile]["unit"])
        service_installed = await systemd.is_installed(unit)
        if service_installed:
            service_status = await systemd.status(unit)
    return {
        "profile": profile,
        "profiles": {key: meta["label"] for key, meta in _PROFILE_META.items()},
        "env_exists": _ENV_PATH.exists(),
        "api_key_set": bool(values.get("GEMINI_API_KEY", "").strip()),
        "wake_word_phrases": values.get("WAKE_WORD_PHRASES", ""),
        "presence_enabled": values.get("PRESENCE_ENABLED", "false").lower() == "true",
        "audio_input": values.get("AUDIO_INPUT", "browser"),
        "audio_output": values.get("AUDIO_OUTPUT", "browser"),
        "usb_mic_alsa_device": values.get("USB_MIC_ALSA_DEVICE", ""),
        "usb_speaker_alsa_device": values.get("USB_SPEAKER_ALSA_DEVICE", ""),
        "wall_agent_url": values.get("WALL_AGENT_URL", ""),
        "guard_kb_path": values.get("GUARD_KB_PATH", ""),
        "service_installed": service_installed,
        "service_status": service_status,
        "port": _PROFILE_META[profile]["port"] if profile else None,
        "lan_ip": network.detect_lan_ip(),
    }


# --------------------------------------------------------------------------
# Step 1 — which robot
# --------------------------------------------------------------------------


class ProfileRequest(BaseModel):
    profile: Literal["r1", "g1"]


@app.post("/api/profile")
async def set_profile(body: ProfileRequest) -> dict[str, object]:
    meta = _PROFILE_META[body.profile]
    template = Path(str(meta["template"]))
    env_file.set_values(_ENV_PATH, {"ROBOT_PROFILE": body.profile}, template=template)
    return {"profile": body.profile}


# --------------------------------------------------------------------------
# Step 2 — Gemini API key
# --------------------------------------------------------------------------


class ApiKeyRequest(BaseModel):
    api_key: str


@app.post("/api/api-key")
async def set_api_key(body: ApiKeyRequest) -> dict[str, object]:
    _require_profile()
    result = await gemini_check.check_api_key(body.api_key)
    env_file.set_values(_ENV_PATH, {"GEMINI_API_KEY": body.api_key.strip()})
    return {"ok": result.ok, "message": result.message}


# --------------------------------------------------------------------------
# Step 3 — audio
# --------------------------------------------------------------------------


@app.get("/api/audio/devices")
async def audio_devices() -> dict[str, list[dict[str, str]]]:
    capture = await audio.list_capture_devices()
    playback = await audio.list_playback_devices()
    return {
        "capture": [{"id": d.alsa_id, "label": d.label} for d in capture],
        "playback": [{"id": d.alsa_id, "label": d.label} for d in playback],
    }


class AudioRequest(BaseModel):
    audio_input: Literal["browser", "robot", "usb"]
    audio_output: Literal["browser", "robot", "usb"]
    usb_mic_alsa_device: str = ""
    usb_speaker_alsa_device: str = ""


@app.post("/api/audio")
async def set_audio(body: AudioRequest) -> dict[str, object]:
    _require_profile()
    env_file.set_values(
        _ENV_PATH,
        {
            "AUDIO_INPUT": body.audio_input,
            "AUDIO_OUTPUT": body.audio_output,
            "USB_MIC_ALSA_DEVICE": body.usb_mic_alsa_device,
            "USB_SPEAKER_ALSA_DEVICE": body.usb_speaker_alsa_device,
        },
    )
    return {"ok": True}


class MicTestRequest(BaseModel):
    alsa_device: str


@app.post("/api/audio/test-mic")
async def test_mic(body: MicTestRequest) -> Response:
    try:
        wav_path = await audio.record_sample(body.alsa_device)
    except audio.AudioTestError as exc:
        raise HTTPException(400, str(exc)) from exc
    try:
        data = wav_path.read_bytes()
    finally:
        wav_path.unlink(missing_ok=True)
    return Response(content=data, media_type="audio/wav")


class SpeakerTestRequest(BaseModel):
    alsa_device: str


@app.post("/api/audio/test-speaker")
async def test_speaker(body: SpeakerTestRequest) -> dict[str, object]:
    try:
        await audio.play_test_tone(body.alsa_device)
    except audio.AudioTestError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"ok": True}


# --------------------------------------------------------------------------
# Step 4 — wake words / presence
# --------------------------------------------------------------------------


class WakeWordsRequest(BaseModel):
    phrases: list[str]


@app.post("/api/wake-words")
async def set_wake_words(body: WakeWordsRequest) -> dict[str, object]:
    _require_profile()
    phrases = [p.strip() for p in body.phrases if p.strip()]
    env_file.set_values(_ENV_PATH, {"WAKE_WORD_PHRASES": ",".join(phrases)})
    return {"ok": True, "phrases": phrases}


class PresenceRequest(BaseModel):
    enabled: bool


@app.post("/api/presence")
async def set_presence(body: PresenceRequest) -> dict[str, object]:
    _require_profile()
    env_file.set_values(_ENV_PATH, {"PRESENCE_ENABLED": "true" if body.enabled else "false"})
    return {"ok": True}


# --------------------------------------------------------------------------
# Step 5 — profile-specific extras
# --------------------------------------------------------------------------


class WallAgentRequest(BaseModel):
    url: str


@app.post("/api/wall-agent")
async def set_wall_agent(body: WallAgentRequest) -> dict[str, object]:
    if _require_profile() != "r1":
        raise HTTPException(400, "Адрес видеостены нужен только для профиля R1.")
    env_file.set_values(_ENV_PATH, {"WALL_AGENT_URL": body.url.strip()})
    return {"ok": True}


@app.get("/api/guard-kb/status")
async def guard_kb_status() -> dict[str, object]:
    if _require_profile() != "g1":
        raise HTTPException(400, "Справочник нужен только для профиля G1.")
    values = env_file.read_values(_ENV_PATH)
    path_str = values.get("GUARD_KB_PATH") or str(_PROJECT_ROOT / "config" / "guard-kb.yaml")
    path = Path(path_str)
    if not path.exists():
        return {"path": path_str, "exists": False, "rooms": 0, "faq": 0}
    from app.guard_kb import GuardKb  # local import: keeps installer decoupled from app.config

    kb = GuardKb.load(path)
    return {"path": path_str, "exists": True, "rooms": len(kb.rooms), "faq": len(kb.faq)}


# --------------------------------------------------------------------------
# Advanced settings — every remaining .env knob (app/installer/
# settings_schema.py), for the customer/technician re-opening "maintenance
# mode" later rather than the linear first-run wizard above. Separate from
# the wizard's own per-step endpoints on purpose: those cover the essentials
# a non-technical customer needs on day one (profile, key, audio, wake
# words, presence, wall/guard-kb), this covers everything else that used to
# require hand-editing `.env` (2026-09-06 ask: "добавим ... настройку всех
# конфигов возможных роботов ... всё, что сейчас нужно менять вручную").
# --------------------------------------------------------------------------


@app.get("/api/advanced-settings")
async def get_advanced_settings() -> dict[str, object]:
    profile = _require_profile()
    current = env_file.read_values(_ENV_PATH)
    groups = []
    for group in settings_schema.visible_groups(profile):
        fields = []
        for field in group["fields"]:
            fields.append({**field, "value": current.get(field["key"], field.get("default", ""))})
        groups.append({**group, "fields": fields})
    return {"groups": groups}


class AdvancedSettingsRequest(BaseModel):
    values: dict[str, str]


@app.post("/api/advanced-settings")
async def set_advanced_settings(body: AdvancedSettingsRequest) -> dict[str, object]:
    profile = _require_profile()
    known_keys = {f["key"] for f in _flatten_fields(settings_schema.visible_groups(profile))}
    updates = {k: v for k, v in body.values.items() if k in known_keys}
    if updates:
        env_file.set_values(_ENV_PATH, updates)
    return {"ok": True, "saved": sorted(updates)}


def _flatten_fields(
    groups: list[settings_schema.SettingGroup],
) -> list[settings_schema.SettingField]:
    return [field for group in groups for field in group["fields"]]


# --------------------------------------------------------------------------
# Step 6 — finish: install + start the main service
# --------------------------------------------------------------------------


@app.post("/api/finish")
async def finish() -> dict[str, object]:
    profile = _require_profile()
    if not env_file.read_values(_ENV_PATH).get("GEMINI_API_KEY", "").strip():
        raise HTTPException(400, "Сначала укажите ключ Gemini API (шаг «Ключ Gemini»).")

    meta = _PROFILE_META[profile]
    unit = str(meta["unit"])
    port = int(meta["port"])  # type: ignore[arg-type]

    systemd.install_unit(_DEPLOY_DIR / unit, working_dir=_PROJECT_ROOT)
    await systemd.enable_linger()
    await systemd.daemon_reload()
    ok, detail = await systemd.enable_now(unit)
    if not ok:
        return {"ok": False, "message": "Не удалось запустить службу.", "detail": detail}

    alive = await _wait_until_alive(port)
    if alive:
        return {
            "ok": True,
            "message": f"Готово! Робот отвечает на порту {port}.",
        }
    return {
        "ok": False,
        "message": (
            "Служба запущена, но пока не отвечает — это иногда занимает до "
            "двух минут при первом запуске (загрузка модели распознавания "
            "слова-пробуждения). Подождите немного и откройте страницу "
            "заново, либо проверьте раздел «Журнал»."
        ),
    }


async def _wait_until_alive(port: int, attempts: int = 24, delay_s: float = 5.0) -> bool:
    # 2026-09-06 audit finding: the previous 16s total window (attempts=8,
    # delay_s=2.0) was short next to app/wake_word.py's real cold-start
    # cost -- WakeWordDetector.warm_up() loading a fresh faster-whisper
    # model can itself take close to a minute on a first run with no
    # local cache. A normal first install could report "не отвечает" here
    # even though nothing was actually wrong, alarming a first-time
    # customer right at the finish line. 24 * 5s = up to 2 minutes covers
    # that with real margin; polling still returns the instant the
    # service actually answers, so a healthy install isn't slowed down.
    async with httpx.AsyncClient(timeout=3.0) as client:
        for _ in range(attempts):
            try:
                response = await client.get(f"http://127.0.0.1:{port}/api/profile")
                if response.status_code == 200:
                    return True
            except httpx.RequestError:
                pass
            await asyncio.sleep(delay_s)
    return False


# --------------------------------------------------------------------------
# Maintenance mode — status / restart / logs, reused after first setup
# --------------------------------------------------------------------------


@app.post("/api/service/{verb}")
async def service_action(verb: Literal["start", "stop", "restart"]) -> dict[str, object]:
    profile = _require_profile()
    unit = str(_PROFILE_META[profile]["unit"])
    ok, detail = await systemd.action(unit, verb)
    return {"ok": ok, "detail": detail}


@app.get("/api/service/status")
async def service_status() -> dict[str, object]:
    profile = _require_profile()
    unit = str(_PROFILE_META[profile]["unit"])
    if not await systemd.is_installed(unit):
        return {"installed": False}
    result = await systemd.status(unit)
    result["installed"] = True
    return result


@app.get("/api/service/logs")
async def service_logs(lines: int = 80) -> dict[str, object]:
    profile = _require_profile()
    unit = str(_PROFILE_META[profile]["unit"])
    raw = await systemd.recent_logs(unit, lines=lines)
    return {"entries": logs.friendly_summary(raw)}


# --------------------------------------------------------------------------
# Static wizard page
# --------------------------------------------------------------------------


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(_STATIC_DIR / "index.html")
