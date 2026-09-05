"""Translates known raw log lines into plain-Russian one-liners for the
wizard's "Журнал" panel. DEPLOYMENT.md §12 ("Диагностика типовых проблем")
already documents which recurring lines are normal versus worth worrying
about — this surfaces that same knowledge to a non-technical customer
instead of only to whoever reads that file. Falls back to showing the raw
line untranslated for anything not recognised (never invents an
explanation for a line it doesn't actually know).
"""

from __future__ import annotations

import re

_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"gemini\.connection_state state=connecting"), "Подключение к Gemini… (обычное дело, идёт постоянно)"),
    (re.compile(r"gemini\.connection_state state=connected"), "Подключение к Gemini установлено."),
    (re.compile(r"proactive session refresh"), "Плановое обновление сессии — не ошибка."),
    (re.compile(r"no Gemini API key"), "❌ Не задан API-ключ Gemini — заполните его на шаге «Ключ Gemini»."),
    (re.compile(r"presence\.visitor_detected"), "👋 Обнаружен посетитель перед роботом."),
    (
        re.compile(r"session_hub\.wake_word_warm_up_failed"),
        "⚠️ Не удалось подготовить распознавание слова-будильника (не критично).",
    ),
    (re.compile(r"usb_mic\.arecord_exited"), "⚠️ USB-микрофон отключился или недоступен."),
    (re.compile(r"presence\.task_error"), "⚠️ Сбой в определении посетителя (не влияет на голосовой диалог)."),
    (re.compile(r"\bWARNING\b"), "⚠️ Предупреждение — подробности в строке ниже."),
    (re.compile(r"\bERROR\b|Traceback|Exception"), "❌ Ошибка — подробности в строке ниже."),
]


def friendly_summary(raw_log: str) -> list[dict[str, str | None]]:
    """One entry per non-empty raw line: {"raw": ..., "friendly": ... | None}."""
    entries: list[dict[str, str | None]] = []
    for line in raw_log.splitlines():
        if not line.strip():
            continue
        friendly = None
        for pattern, message in _PATTERNS:
            if pattern.search(line):
                friendly = message
                break
        entries.append({"raw": line, "friendly": friendly})
    return entries
