"""Visitor interview for the security desk — a deterministic state machine.

The ТЗ asks for a SEQUENTIAL intake (ФИО → организация → цель → к кому →
пропуск → удостоверение). A scripted flow is more dependable than trusting
the model to keep order/state: every answer lands in a known field, yes/no
questions are parsed strictly, and the visitor confirms the whole card at
the end. Free consultation Q&A (search_guard_kb) still handles everything
outside the interview.

Field order, question wording and regexes are carried over from
../../humanoid-robot's apps/rag/src/humanoid_robot/rag/visit_intake.py —
already-proven text/patterns, reimplemented here as a plain dataclass (no
pydantic) to match this prototype's style, since intake state never leaves
the process.

The machine is pure text-in/text-out; app/g1_tools.py's VisitIntakeTool
wires it to the Gemini Live tool-calling loop (see that module for why a
tool call, not a bypass, is the only way to get the model to speak this
machine's text — there is no channel to make Gemini read arbitrary text
verbatim otherwise).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# Fields asked in ТЗ order, with the spoken question for each.
_STEPS: tuple[tuple[str, str], ...] = (
    ("full_name", "Назовите, пожалуйста, вашу фамилию, имя и отчество."),
    ("organization", "Из какой вы организации? Если вы частное лицо — так и скажите."),
    ("purpose", "Какова цель вашего визита?"),
    ("destination", "К кому или в какое подразделение вы направляетесь?"),
    ("has_pass", "У вас оформлен пропуск? Ответьте, пожалуйста: да или нет."),
    ("has_id", "У вас с собой удостоверение личности? Да или нет."),
)

_BOOL_FIELDS = frozenset({"has_pass", "has_id"})

_GREETING = "Здравствуйте! Я помогу оформить ваш визит. "
_RESTART = "Хорошо, давайте заново. "
_CANCELLED = "Хорошо, оформление отменено. Чем ещё могу помочь?"
_COMPLETED = "Спасибо! Я передал информацию сотруднику охраны. Подойдите, пожалуйста, к стойке."
_REPEAT_YES_NO = "Извините, не расслышал. Скажите, пожалуйста: да или нет."

_YES_RE = re.compile(
    r"^\s*(да|есть|конечно|ага|угу|верно|точно|оформлен|имеется|с собой)\b", re.IGNORECASE
)
_NO_RE = re.compile(r"^\s*(нет|нету|не\b|отсутствует)", re.IGNORECASE)

_CANCEL_RE = re.compile(
    r"\b(отмена|отменить|отмени|стоп|хватит|не надо|передумал)\b", re.IGNORECASE
)

# Phrases that start the interview from voice.
_TRIGGER_RE = re.compile(
    r"\b(оформ\w*\s+(визит|пропуск)|зарегистрир\w*|я\s+посетитель|"
    r"записать\s+на\s+визит|на\s+при[её]м)\b",
    re.IGNORECASE,
)


def wants_intake(text: str) -> bool:
    """Does this utterance ask to register a visit?"""
    return bool(_TRIGGER_RE.search(text))


def _parse_yes_no(text: str) -> bool | None:
    stripped = text.strip()
    # Check "нет" before "да" — anchored at the start, so "надо"/"неточно"
    # don't false-trigger, but a NO must be ruled out first regardless.
    if _NO_RE.search(stripped):
        return False
    if _YES_RE.search(stripped):
        return True
    return None


@dataclass(slots=True)
class VisitIntake:
    """One visitor interview: feed utterances in, get the next line out."""

    _step: int = -1  # -1 = idle, len(_STEPS) = confirmation
    _card: dict[str, object] = field(default_factory=dict)

    @property
    def engaged(self) -> bool:
        return self._step >= 0

    def start(self) -> str:
        self._card = {}
        self._step = 0
        return _GREETING + _STEPS[0][1]

    def consume(self, text: str) -> tuple[str, dict[str, object] | None]:
        """Advance the interview with one visitor utterance.

        Returns (reply_to_speak, completed_card_or_None). The machine
        disengages when the card is completed or the visitor cancels.
        """
        if not self.engaged:
            return "", None
        if _CANCEL_RE.search(text):
            self._reset()
            return _CANCELLED, None
        if self._step >= len(_STEPS):
            return self._consume_confirmation(text)
        return self._consume_field(text), None

    def _consume_field(self, text: str) -> str:
        field_name, question = _STEPS[self._step]
        if field_name in _BOOL_FIELDS:
            answer = _parse_yes_no(text)
            if answer is None:
                return _REPEAT_YES_NO
            self._card[field_name] = answer
        else:
            cleaned = text.strip().rstrip(".!")
            if not cleaned:
                return question
            self._card[field_name] = cleaned

        self._step += 1
        if self._step < len(_STEPS):
            return _STEPS[self._step][1]
        return self._confirmation_question()

    def _consume_confirmation(self, text: str) -> tuple[str, dict[str, object] | None]:
        answer = _parse_yes_no(text)
        if answer is None:
            return _REPEAT_YES_NO, None
        if not answer:
            # Literal, deliberate customer requirement: "нет" restarts the
            # WHOLE interview from field 1, it does not fix one field.
            self._card = {}
            self._step = 0
            return _RESTART + _STEPS[0][1], None
        card = dict(self._card)
        self._reset()
        return _COMPLETED, card

    def _confirmation_question(self) -> str:
        def yn(value: object) -> str:
            return "да" if value else "нет"

        return (
            "Проверим, всё ли я записал верно. "
            f"{self._card.get('full_name', '')}, "
            f"организация: {self._card.get('organization', '')}, "
            f"цель визита: {self._card.get('purpose', '')}, "
            f"направляетесь: {self._card.get('destination', '')}, "
            f"пропуск: {yn(self._card.get('has_pass'))}, "
            f"удостоверение: {yn(self._card.get('has_id'))}. "
            "Всё верно? Да или нет."
        )

    def _reset(self) -> None:
        self._step = -1
        self._card = {}
