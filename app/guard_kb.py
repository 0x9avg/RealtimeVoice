"""Guard-desk knowledge base — customer reference data for consultations.

Loaded from a plain-text-editable YAML file (default: config/guard-kb.yaml):

    rooms:
      - room: "305"
        floor: "третий этаж"
        unit: "Отдел кадров"
        note: "рядом с лифтом"          # optional
    faq:
      - q: "режим работы"
        a: "Министерство работает с 9:00 до 18:30, обед с 13:00 до 14:30."
    info: |
      Свободный текст справки (порядок посещения, документы, контакты).

Same field shape as ../../humanoid-robot's apps/rag/src/humanoid_robot/rag/
guard_kb.py, reimplemented here (plain dataclass, no pydantic/observability
dependency) so real customer data can move between the two without
reformatting if this project's data ever needs to feed the production KB
or vice versa.

Two consumption paths, used by app/g1_tools.py's SearchGuardKbTool:

* ``lookup(text)`` — deterministic answers for room/unit questions
  («где кабинет 305», «где отдел кадров»): navigation must be exact, not
  an LLM guess.
* ``reference_block()`` — the whole KB rendered as a справка block, for a
  future static-prompt use case (not currently wired into the system
  instruction — search_guard_kb hands the caller kb facts as a tool result
  instead, matching how search_knowledge already works here).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

_LOG = logging.getLogger("guard_kb")

_ROOM_RE = re.compile(r"\b(?:кабинет|комнат\w*|каб\.?)\s*(?:номер\s*)?(\d+\w?)", re.IGNORECASE)
_WHERE_RE = re.compile(r"\b(где|как\s+пройти|как\s+найти|на\s+каком\s+этаже)\b", re.IGNORECASE)


@dataclass(slots=True)
class GuardKb:
    """Parsed customer reference data."""

    rooms: list[dict[str, str]] = field(default_factory=list)
    faq: list[dict[str, str]] = field(default_factory=list)
    info: str = ""

    @classmethod
    def load(cls, path: str | Path) -> GuardKb:
        try:
            raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        except FileNotFoundError:
            _LOG.info("guard_kb.absent path=%s", path)
            return cls()
        except Exception:
            _LOG.exception("guard_kb.unreadable path=%s", path)
            return cls()
        rooms = [
            {str(k): str(v) for k, v in item.items()}
            for item in raw.get("rooms", [])
            if isinstance(item, dict)
        ]
        faq = [
            {str(k): str(v) for k, v in item.items()}
            for item in raw.get("faq", [])
            if isinstance(item, dict) and item.get("q") and item.get("a")
        ]
        info = str(raw.get("info", "") or "")
        _LOG.info("guard_kb.loaded rooms=%d faq=%d info_chars=%d", len(rooms), len(faq), len(info))
        return cls(rooms=rooms, faq=faq, info=info)

    @property
    def empty(self) -> bool:
        return not (self.rooms or self.faq or self.info)

    def lookup(self, text: str) -> str | None:
        """Deterministic navigation answer, or None to let semantic search handle it."""
        room_match = _ROOM_RE.search(text)
        if room_match:
            return self._room_answer(room_match.group(1))
        if _WHERE_RE.search(text):
            unit = self._match_unit(text)
            if unit is not None:
                return self._unit_answer(unit)
        return None

    def _room_answer(self, number: str) -> str | None:
        for room in self.rooms:
            if room.get("room", "").lower() == number.lower():
                parts = [f"Кабинет {room['room']}"]
                if room.get("floor"):
                    parts.append(f"находится на этаже: {room['floor']}")
                if room.get("unit"):
                    parts.append(f"это {room['unit']}")
                if room.get("note"):
                    parts.append(room["note"])
                return ", ".join(parts) + "."
        if self.rooms:
            return (
                f"Извините, кабинет {number} не значится в моём справочнике — "
                "уточните, пожалуйста, у сотрудника охраны."
            )
        return None

    def _match_unit(self, text: str) -> dict[str, str] | None:
        lowered = text.lower()
        best: dict[str, str] | None = None
        best_len = 0
        for room in self.rooms:
            unit = room.get("unit", "").lower()
            if not unit:
                continue
            key = unit.removeprefix("отдел ").strip() or unit
            if (unit in lowered or key in lowered) and len(unit) > best_len:
                best = room
                best_len = len(unit)
        return best

    def _unit_answer(self, room: dict[str, str]) -> str:
        parts = [room.get("unit", "Подразделение")]
        if room.get("room"):
            parts.append(f"кабинет {room['room']}")
        if room.get("floor"):
            parts.append(f"этаж: {room['floor']}")
        if room.get("note"):
            parts.append(room["note"])
        return ", ".join(parts) + "."

    def reference_block(self) -> str:
        """The KB rendered as a static reference block (not currently wired
        into the system instruction — kept for a future static-prompt use)."""
        if self.empty:
            return ""
        lines: list[str] = ["", "СПРАВКА (отвечай ТОЛЬКО на её основе):"]
        if self.info.strip():
            lines.append(self.info.strip())
        lines.extend(f"Вопрос: {item['q']}\nОтвет: {item['a']}" for item in self.faq)
        if self.rooms:
            lines.append("Расположение кабинетов и подразделений:")
            for room in self.rooms:
                bits = [f"кабинет {room.get('room', '?')}"]
                if room.get("unit"):
                    bits.append(room["unit"])
                if room.get("floor"):
                    bits.append(room["floor"])
                if room.get("note"):
                    bits.append(room["note"])
                lines.append("- " + ", ".join(bits))
        return "\n".join(lines)


def _build_default() -> GuardKb:
    from app.config import settings

    return GuardKb.load(settings.guard_kb_path)


guard_kb = _build_default()
