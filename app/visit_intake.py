"""Visitor interview for the security desk — a deterministic state machine.

The ТЗ asks for a SEQUENTIAL intake (выбор языка → ФИО → организация → цель →
к кому → пропуск → удостоверение). A scripted flow is more dependable than
trusting the model to keep order/state: every answer lands in a known field,
yes/no questions are parsed strictly, and the visitor confirms the whole
card at the end. Free consultation Q&A (search_guard_kb) still handles
everything outside the interview.

G1 ТЗ §3.2 wants an explicit language choice ("на каком языке вам удобно?"),
not just per-utterance auto-detection — so unlike the rest of this
project (where Gemini picks the reply language per turn on its own), this
one scripted flow asks once, up front, and then stays in that language for
every question/readback until the interview ends. Русский/казахский/English —
matching app/scenarios.py's language rule (both ТЗ mark English as optional;
it's in scope now, same three-way choice as everywhere else the language
rule applies).

Placeholder Kazakh wording below — like config/guard-kb.yaml's placeholder
content, this hasn't been reviewed by a native speaker or the customer; the
mechanism (bilingual dict per string, chosen once, used throughout) is the
part that matters and is meant to be kept even if the exact phrasing changes.

The machine is pure text-in/text-out; app/g1_tools.py's VisitIntakeTool
wires it to the Gemini Live tool-calling loop (see that module for why a
tool call, not a bypass, is the only way to get the model to speak this
machine's text — there is no channel to make Gemini read arbitrary text
verbatim otherwise).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal

Language = Literal["ru", "kk", "en"]

# Field order per ТЗ, with the spoken question for each — one tuple per
# language, same field names/order, just translated prompts.
_STEPS: dict[Language, tuple[tuple[str, str], ...]] = {
    "ru": (
        ("full_name", "Назовите, пожалуйста, вашу фамилию, имя и отчество."),
        ("organization", "Из какой вы организации? Если вы частное лицо — так и скажите."),
        ("purpose", "Какова цель вашего визита?"),
        ("destination", "К кому или в какое подразделение вы направляетесь?"),
        ("has_pass", "У вас оформлен пропуск? Ответьте, пожалуйста: да или нет."),
        ("has_id", "У вас с собой удостоверение личности? Да или нет."),
    ),
    "kk": (
        ("full_name", "Тегіңізді, атыңызды және әкеңіздің атын айтып беріңізші."),
        ("organization", "Қай ұйымнан келдіңіз? Жеке тұлға болсаңыз, солай айтыңыз."),
        ("purpose", "Келу мақсатыңыз қандай?"),
        ("destination", "Кімге немесе қай бөлімге барасыз?"),
        ("has_pass", "Рұқсатыңыз (пропуск) ресімделген бе? Иә немесе жоқ деп жауап беріңіз."),
        ("has_id", "Жеке куәлігіңіз өзіңізде бар ма? Иә немесе жоқ."),
    ),
    "en": (
        ("full_name", "Please tell me your full name."),
        ("organization", "Which organization are you from? If you're a private individual, just say so."),
        ("purpose", "What is the purpose of your visit?"),
        ("destination", "Who or which department are you visiting?"),
        ("has_pass", "Do you have a pre-arranged pass? Please answer yes or no."),
        ("has_id", "Do you have identification with you? Yes or no."),
    ),
}

_BOOL_FIELDS = frozenset({"has_pass", "has_id"})

_YES_WORD: dict[Language, str] = {"ru": "да", "kk": "иә", "en": "yes"}
_NO_WORD: dict[Language, str] = {"ru": "нет", "kk": "жоқ", "en": "no"}

_LANGUAGE_PROMPT = (
    "Здравствуйте! Я помогу оформить ваш визит. На каком языке вам удобно — "
    "на русском, казахском или английском? / Сәлеметсіз бе! Сапарды "
    "рәсімдеуге көмектесемін. Қай тілде сөйлескіңіз келеді — орысша ма, "
    "қазақша ма, ағылшынша ма? / Hello! I'll help you register your visit. "
    "Which language would you prefer — Russian, Kazakh, or English?"
)

_RESTART: dict[Language, str] = {
    "ru": "Хорошо, давайте заново. ",
    "kk": "Жарайды, қайтадан бастайық. ",
    "en": "Okay, let's start over. ",
}
_CANCELLED: dict[Language, str] = {
    "ru": "Хорошо, оформление отменено. Чем ещё могу помочь?",
    "kk": "Жарайды, рәсімдеу тоқтатылды. Тағы немен көмектесе аламын?",
    "en": "Okay, registration cancelled. How else can I help?",
}
_COMPLETED: dict[Language, str] = {
    "ru": "Спасибо! Я передал информацию сотруднику охраны. Подойдите, пожалуйста, к стойке.",
    "kk": "Рақмет! Ақпаратты күзет қызметкеріне жеткіздім. Стойкаға келіңізші.",
    "en": "Thank you! I've passed the information to the security officer. Please come to the desk.",
}
_REPEAT_YES_NO: dict[Language, str] = {
    "ru": "Извините, не расслышал. Скажите, пожалуйста: да или нет.",
    "kk": "Кешіріңіз, түсінбей қалдым. Иә немесе жоқ деп айтыңызшы.",
    "en": "Sorry, I didn't catch that. Please say yes or no.",
}
_CONFIRM_TEMPLATE: dict[Language, str] = {
    "ru": (
        "Проверим, всё ли я записал верно. {full_name}, организация: {organization}, "
        "цель визита: {purpose}, направляетесь: {destination}, пропуск: {has_pass}, "
        "удостоверение: {has_id}. Всё верно? Да или нет."
    ),
    "kk": (
        "Дұрыс жаздым ба, тексерейік. {full_name}, ұйым: {organization}, "
        "мақсаты: {purpose}, кімге барасыз: {destination}, рұқсат: {has_pass}, "
        "куәлік: {has_id}. Бәрі дұрыс па? Иә немесе жоқ."
    ),
    "en": (
        "Let's confirm what I've recorded. {full_name}, organization: {organization}, "
        "purpose of visit: {purpose}, visiting: {destination}, pass: {has_pass}, "
        "ID: {has_id}. Is that correct? Yes or no."
    ),
}

_RU_LANGUAGE_CHOICE_RE = re.compile(r"рус|russian", re.IGNORECASE)
_KK_LANGUAGE_CHOICE_RE = re.compile(r"казах|қазақ|qazaq|kazakh", re.IGNORECASE)
_EN_LANGUAGE_CHOICE_RE = re.compile(r"englis|английск|ағылшын", re.IGNORECASE)

_YES_RE: dict[Language, re.Pattern[str]] = {
    "ru": re.compile(
        r"^\s*(да|есть|конечно|ага|угу|верно|точно|оформлен|имеется|с собой)\b", re.IGNORECASE
    ),
    "kk": re.compile(r"^\s*(иә|бар|дұрыс)\b", re.IGNORECASE),
    "en": re.compile(r"^\s*(yes|yeah|yep|sure|correct|right|i do)\b", re.IGNORECASE),
}
_NO_RE: dict[Language, re.Pattern[str]] = {
    "ru": re.compile(r"^\s*(нет|нету|не\b|отсутствует)", re.IGNORECASE),
    "kk": re.compile(r"^\s*(жоқ)\b", re.IGNORECASE),
    "en": re.compile(r"^\s*(no|nope|not|don't|do not)\b", re.IGNORECASE),
}

# Checked regardless of chosen (or not-yet-chosen) language — a visitor
# might say this in any of the three, and it costs nothing to accept all
# of them always.
_CANCEL_RE = re.compile(
    r"\b(отмена|отменить|отмени|стоп|хватит|не надо|передумал|бас тарт\w*|"
    r"тоқта\w*|cancel|never ?mind)\b",
    re.IGNORECASE,
)


def _parse_language(text: str) -> Language | None:
    if _KK_LANGUAGE_CHOICE_RE.search(text):
        return "kk"
    if _EN_LANGUAGE_CHOICE_RE.search(text):
        return "en"
    if _RU_LANGUAGE_CHOICE_RE.search(text):
        return "ru"
    return None


def _parse_yes_no(text: str, language: Language) -> bool | None:
    stripped = text.strip()
    # Check "no" before "yes" — anchored at the start, so e.g. "надо"/"неточно"
    # don't false-trigger, but a NO must be ruled out first regardless.
    if _NO_RE[language].search(stripped):
        return False
    if _YES_RE[language].search(stripped):
        return True
    return None


@dataclass(slots=True)
class VisitIntake:
    """One visitor interview: feed utterances in, get the next line out."""

    # -1 = idle, 0 = awaiting language choice, 1..len(steps) = fields,
    # len(steps)+1 = confirmation.
    _step: int = -1
    _language: Language = "ru"
    _card: dict[str, object] = field(default_factory=dict)

    @property
    def engaged(self) -> bool:
        return self._step >= 0

    @property
    def current_question(self) -> str:
        """The line the visitor is currently expected to answer.

        Used to put the model back on script after the Gemini session is
        recreated mid-interview (see app/session_hub.py) — the machine's own
        state survives a reconnect, but the model's memory of being
        mid-interview does not, so it has to be told where it left off.
        """
        if not self.engaged:
            return ""
        if self._step == 0:
            return _LANGUAGE_PROMPT
        steps = _STEPS[self._language]
        field_index = self._step - 1
        if field_index >= len(steps):
            return self._confirmation_question()
        return steps[field_index][1]

    def start(self) -> str:
        self._card = {}
        self._step = 0
        return _LANGUAGE_PROMPT

    def consume(self, text: str) -> tuple[str, dict[str, object] | None]:
        """Advance the interview with one visitor utterance.

        Returns (reply_to_speak, completed_card_or_None). The machine
        disengages when the card is completed or the visitor cancels.
        """
        if not self.engaged:
            return "", None
        if _CANCEL_RE.search(text):
            language = self._language
            self._reset()
            return _CANCELLED[language], None
        if self._step == 0:
            return self._consume_language(text), None
        steps = _STEPS[self._language]
        field_index = self._step - 1
        if field_index >= len(steps):
            return self._consume_confirmation(text)
        return self._consume_field(text, field_index), None

    def _consume_language(self, text: str) -> str:
        language = _parse_language(text)
        if language is None:
            return _LANGUAGE_PROMPT
        self._language = language
        self._step = 1
        return _STEPS[language][0][1]

    def _consume_field(self, text: str, field_index: int) -> str:
        steps = _STEPS[self._language]
        field_name, question = steps[field_index]
        if field_name in _BOOL_FIELDS:
            answer = _parse_yes_no(text, self._language)
            if answer is None:
                return _REPEAT_YES_NO[self._language]
            self._card[field_name] = answer
        else:
            cleaned = text.strip().rstrip(".!")
            if not cleaned:
                return question
            self._card[field_name] = cleaned

        self._step += 1
        next_index = self._step - 1
        if next_index < len(steps):
            return steps[next_index][1]
        return self._confirmation_question()

    def _consume_confirmation(self, text: str) -> tuple[str, dict[str, object] | None]:
        answer = _parse_yes_no(text, self._language)
        if answer is None:
            return _REPEAT_YES_NO[self._language], None
        if not answer:
            # Literal, deliberate customer requirement: "нет"/"жоқ" restarts
            # the WHOLE interview from field 1 (language choice already
            # made stays as-is — only the card resets).
            self._card = {}
            self._step = 1
            return _RESTART[self._language] + _STEPS[self._language][0][1], None
        language = self._language
        card = dict(self._card)
        card["language"] = language
        self._reset()
        return _COMPLETED[language], card

    def _confirmation_question(self) -> str:
        language = self._language

        def yn(value: object) -> str:
            return _YES_WORD[language] if value else _NO_WORD[language]

        return _CONFIRM_TEMPLATE[language].format(
            full_name=self._card.get("full_name", ""),
            organization=self._card.get("organization", ""),
            purpose=self._card.get("purpose", ""),
            destination=self._card.get("destination", ""),
            has_pass=yn(self._card.get("has_pass")),
            has_id=yn(self._card.get("has_id")),
        )

    def _reset(self) -> None:
        self._step = -1
        self._card = {}
        self._language = "ru"
