"""Validates a Gemini API key with a cheap REST call (`models.list`), not a
live BidiGenerateContent session — the wizard needs "is this key even valid"
answered in under a second, without opening a real voice session just to
test typing.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx

_MODELS_URL = "https://generativelanguage.googleapis.com/v1beta/models"


@dataclass(frozen=True)
class KeyCheckResult:
    ok: bool
    message: str


async def check_api_key(api_key: str) -> KeyCheckResult:
    if not api_key.strip():
        return KeyCheckResult(False, "Ключ пустой.")
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(_MODELS_URL, params={"key": api_key.strip()})
    except httpx.RequestError as exc:
        return KeyCheckResult(False, f"Нет связи с сервером Google, проверьте интернет: {exc}")
    if response.status_code == 200:
        return KeyCheckResult(True, "Ключ рабочий.")
    if response.status_code in (400, 401, 403):
        return KeyCheckResult(
            False, "Ключ не подходит — проверьте, что он скопирован полностью, без лишних пробелов."
        )
    return KeyCheckResult(False, f"Google ответил с ошибкой (код {response.status_code}).")
