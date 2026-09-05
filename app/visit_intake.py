"""Visitor interview for the security desk — a deterministic state machine.

The ТЗ asks for a SEQUENTIAL intake (ФИО → организация → цель → к кому →
пропуск → удостоверение). A scripted flow is more dependable than trusting
the model to keep order/state: every answer lands in a known field, yes/no
questions are parsed strictly, and the visitor confirms the whole card at
the end. Free consultation Q&A (search_guard_kb) still handles everything
outside the interview.

Language handling (changed 2026-09-05, "убери обязательный шаг с выбором
языка"): this used to open with an explicit "на каком языке вам удобно?"
question per G1 ТЗ §3.2, before anything else. Removed per direct customer
feedback — it read as an interrogation before the interview even started,
the same complaint that already got the equivalent general-greeting question
removed from app/scenarios.py the day before. The interview now just STARTS
(first question is ФИО, in Russian), and `_detect_language` re-checks the
visitor's own answer after every field for a script-based signal (Kazakh-only
Cyrillic letters, or a Latin-only answer) and switches the template language
for every question/readback from then on — no explicit choice, same
auto-follow principle as the rest of this project's ЯЗЫК rule. This is a
heuristic, not a real language-ID model: a short or ambiguous answer (a
name with no distinguishing letters, a bare "да") keeps whatever language was
already active rather than guessing wrong.

Confirmation "no" handling (changed 2026-09-05, item 9 of that day's fixes):
used to unconditionally restart the WHOLE interview from field 1 — a literal
ТЗ line, kept until the customer explicitly asked to relax it (see the
2026-09-04 CLAUDE.md note this project inherited: "не просто угадать...
нужен sign-off заказчика" — that sign-off is this instruction). "No" now
opens a short correction menu naming the six fields; the visitor names ONE,
answers it again, and lands back on a re-read confirmation with only that
field changed — everything else already recorded survives. Saying something
that isn't one of the six field names (e.g. "всё неправильно"/"заново") still
falls back to the OLD full-restart behaviour, so that escape hatch isn't lost.

Placeholder Kazakh wording below — like config/guard-kb.yaml's placeholder
content, this hasn't been reviewed by a native speaker or the customer; the
mechanism (bilingual dict per string, switched by `_detect_language`, used
throughout) is the part that matters and is meant to be kept even if the
exact phrasing changes.

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
_FIELD_COUNT = len(_STEPS["ru"])

_BOOL_FIELDS = frozenset({"has_pass", "has_id"})

_YES_WORD: dict[Language, str] = {"ru": "да", "kk": "иә", "en": "yes"}
_NO_WORD: dict[Language, str] = {"ru": "нет", "kk": "жоқ", "en": "no"}

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
    # Item 5, 2026-09-06 UX list: the old readout opened with a full
    # sentence of preamble ("Проверим, всё ли я записал верно...") before
    # any actual data — cut, since it's the same six fields every time and
    # visitors were tuning out before the useful part started. Every field
    # is still read back (nothing dropped, that's still the point of this
    # step), just without the wind-up.
    "ru": (
        "Итак: {full_name}, организация — {organization}, цель — {purpose}, "
        "к кому — {destination}, пропуск — {has_pass}, удостоверение — {has_id}. "
        "Верно? Да или нет."
    ),
    "kk": (
        "Ендеше: {full_name}, ұйым — {organization}, мақсаты — {purpose}, "
        "кімге — {destination}, рұқсат — {has_pass}, куәлік — {has_id}. "
        "Дұрыс па? Иә немесе жоқ."
    ),
    "en": (
        "So: {full_name}, organization — {organization}, purpose — {purpose}, "
        "visiting — {destination}, pass — {has_pass}, ID — {has_id}. "
        "Correct? Yes or no."
    ),
}
# Item 9, 2026-09-05: what to say instead of an unconditional restart when
# the visitor says "no" at confirmation.
_CORRECTION_MENU: dict[Language, str] = {
    "ru": (
        "Хорошо, что именно поправить: ФИО, организация, цель визита, "
        "к кому вы направляетесь, пропуск или удостоверение? Если неверно "
        "почти всё — скажите «заново», и мы пройдём интервью с начала."
    ),
    "kk": (
        "Жарайды, нені түзетейік: аты-жөні, ұйым, келу мақсаты, кімге "
        "барасыз, рұқсат немесе куәлік? Дерлік бәрі қате болса — «қайтадан» "
        "деп айтыңыз, сұхбатты басынан бастаймыз."
    ),
    "en": (
        "Okay, which field should I correct: full name, organization, "
        "purpose of visit, who you're visiting, pass, or ID? If almost "
        "everything is wrong, say \"start over\" and we'll redo the whole "
        "interview."
    ),
}
_CORRECTION_MENU_REPEAT: dict[Language, str] = {
    "ru": "Не разобрал, какое поле. " + _CORRECTION_MENU["ru"],
    "kk": "Қай өріс екенін түсінбедім. " + _CORRECTION_MENU["kk"],
    "en": "I didn't catch which field. " + _CORRECTION_MENU["en"],
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

# Checked regardless of the currently active language — a visitor might say
# this in any of the three, and it costs nothing to accept all of them
# always.
#
# Anchored at the start (same convention as _YES_RE/_NO_RE below), NOT a
# bare substring search — a real bug found in a 2026-09-06 audit: an
# unanchored `\b...\b` matched these words ANYWHERE inside every free-text
# field answer too (organization/purpose/destination as well as the
# has_pass/has_id yes-no answers), so a visitor answering e.g. "нет, не
# надо продлевать пропуск" (a normal "no" to "do you have a pass?") or
# naming an organization/purpose that happens to contain one of these
# words silently cancelled the WHOLE registration — indistinguishable, to
# the visitor, from the robot randomly aborting mid-interview. Anchoring
# still satisfies the PDF's literal "'отмена' cancels at any step"
# requirement (a visitor saying it as their actual answer still starts
# their utterance with it at any step), it just stops treating the word
# as a landmine buried inside unrelated content.
_CANCEL_RE = re.compile(
    r"^\s*(отмена|отменить|отмени|стоп|хватит|не надо|передумал|бас тарт\w*|"
    r"тоқта\w*|cancel|never ?mind)\b",
    re.IGNORECASE,
)
_RESTART_EVERYTHING_RE = re.compile(
    r"\b(занов|сначал|всё непра|все непра|заново|қайтадан|басынан|"
    r"start over|from (the )?(beginning|start)|restart)\b",
    re.IGNORECASE,
)

# Kazakh-Cyrillic-only letters (not in the Russian alphabet) — the strongest
# available signal for "this is Kazakh, not Russian" from plain script alone.
_KK_ONLY_LETTERS_RE = re.compile(r"[әғқңөұүһі]", re.IGNORECASE)
_CYRILLIC_RE = re.compile(r"[а-яё]", re.IGNORECASE)
_LATIN_RE = re.compile(r"[a-z]", re.IGNORECASE)

# One or two keywords per field, per language, matched as a substring against
# whatever the visitor names at the correction menu — deliberately loose
# (e.g. "фамили" also matches "фамилия", "фамилию") rather than requiring an
# exact field label.
_FIELD_KEYWORDS: dict[Language, tuple[tuple[str, str], ...]] = {
    "ru": (
        ("full_name", "фамили"),
        ("full_name", "имя"),
        ("full_name", "фио"),
        ("organization", "организац"),
        ("purpose", "цель"),
        ("destination", "кому"),
        ("destination", "подразделен"),
        ("has_pass", "пропуск"),
        ("has_id", "удостовер"),
    ),
    "kk": (
        ("full_name", "тег"),
        ("full_name", "аты"),
        ("organization", "ұйым"),
        ("purpose", "мақсат"),
        ("destination", "кімге"),
        ("destination", "бөлім"),
        ("has_pass", "рұқсат"),
        ("has_id", "куәлік"),
    ),
    "en": (
        ("full_name", "name"),
        ("organization", "organization"),
        ("organization", "company"),
        ("purpose", "purpose"),
        ("destination", "visiting"),
        ("destination", "department"),
        ("has_pass", "pass"),
        ("has_id", "id"),
        ("has_id", "identification"),
    ),
}


def _parse_language(text: str) -> Language | None:
    if _KK_LANGUAGE_CHOICE_RE.search(text):
        return "kk"
    if _EN_LANGUAGE_CHOICE_RE.search(text):
        return "en"
    if _RU_LANGUAGE_CHOICE_RE.search(text):
        return "ru"
    return None


def _detect_language(text: str, current: Language) -> Language:
    """Script-based heuristic, not a real language-ID model — see the
    module docstring's caveat. Prefers an explicit language NAME if the
    visitor happens to say one (e.g. answering "на русском" out of habit
    even though nothing asked); otherwise looks at the script actually
    used. Falls back to `current` when the answer is too short/ambiguous to
    tell (a bare "да", a name with no Kazakh-only letters and no Latin
    letters either) — a missing signal must never flip the language
    away from what's already working.
    """
    named = _parse_language(text)
    if named is not None:
        return named
    if _KK_ONLY_LETTERS_RE.search(text):
        return "kk"
    has_cyrillic = bool(_CYRILLIC_RE.search(text))
    has_latin = bool(_LATIN_RE.search(text))
    if has_latin and not has_cyrillic:
        return "en"
    if has_cyrillic:
        # Plain Cyrillic with no Kazakh-only letters can't distinguish
        # ru from kk on its own — stay on whichever of those two is
        # already active instead of defaulting to "ru" and silently
        # overriding an established Kazakh conversation the moment
        # someone says a short word ("да") that happens to have no
        # kk-only letters.
        return current if current in ("ru", "kk") else "ru"
    return current


def _parse_yes_no(text: str, language: Language) -> bool | None:
    stripped = text.strip()
    # Check "no" before "yes" — anchored at the start, so e.g. "надо"/"неточно"
    # don't false-trigger, but a NO must be ruled out first regardless.
    if _NO_RE[language].search(stripped):
        return False
    if _YES_RE[language].search(stripped):
        return True
    return None


def _parse_field_choice(text: str, language: Language) -> str | None:
    lowered = text.lower()
    for field_name, keyword in _FIELD_KEYWORDS[language]:
        if keyword in lowered:
            return field_name
    return None


@dataclass(slots=True)
class VisitIntake:
    """One visitor interview: feed utterances in, get the next line out."""

    # -1 = idle, 1..len(steps) = fields, len(steps)+1 = confirmation,
    # len(steps)+2 = correction menu (which field?), len(steps)+3 = awaiting
    # the corrected value for `_correcting_field`.
    _step: int = -1
    _language: Language = "ru"
    _card: dict[str, object] = field(default_factory=dict)
    _correcting_field: str | None = field(default=None, init=False)

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
        steps = _STEPS[self._language]
        if self._step <= _FIELD_COUNT:
            return steps[self._step - 1][1]
        if self._step == _FIELD_COUNT + 1:
            return self._confirmation_question()
        if self._step == _FIELD_COUNT + 2:
            return _CORRECTION_MENU[self._language]
        # _FIELD_COUNT + 3: awaiting the corrected value — same question as
        # the field normally uses.
        assert self._correcting_field is not None  # noqa: S101 — only reachable via _consume_correction_choice
        return dict(steps)[self._correcting_field]

    def start(self) -> str:
        self._card = {}
        self._step = 1
        self._language = "ru"
        self._correcting_field = None
        return _STEPS["ru"][0][1]

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
        if self._step <= _FIELD_COUNT:
            self._language = _detect_language(text, self._language)
            return self._consume_field(text, self._step - 1), None
        if self._step == _FIELD_COUNT + 1:
            return self._consume_confirmation(text)
        if self._step == _FIELD_COUNT + 2:
            return self._consume_correction_choice(text), None
        return self._consume_correction_value(text), None

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
            # Item 9, 2026-09-05: targeted correction, not an unconditional
            # restart — see the module docstring for the customer sign-off
            # this replaces. _card is kept as-is; only the correction menu
            # opens.
            self._step = _FIELD_COUNT + 2
            return _CORRECTION_MENU[self._language], None
        language = self._language
        card = dict(self._card)
        card["language"] = language
        self._reset()
        return _COMPLETED[language], card

    def _consume_correction_choice(self, text: str) -> str:
        if _RESTART_EVERYTHING_RE.search(text):
            # Escape hatch: the OLD full-restart behaviour, still available
            # for "almost everything is wrong" instead of naming six fields
            # one at a time.
            self._card = {}
            self._step = 1
            return _RESTART[self._language] + _STEPS[self._language][0][1]
        field_name = _parse_field_choice(text, self._language)
        if field_name is None:
            return _CORRECTION_MENU_REPEAT[self._language]
        self._correcting_field = field_name
        self._step = _FIELD_COUNT + 3
        return dict(_STEPS[self._language])[field_name]

    def _consume_correction_value(self, text: str) -> str:
        assert self._correcting_field is not None  # noqa: S101 — only reachable via _consume_correction_choice
        field_name = self._correcting_field
        if field_name in _BOOL_FIELDS:
            answer = _parse_yes_no(text, self._language)
            if answer is None:
                return _REPEAT_YES_NO[self._language]
            self._card[field_name] = answer
        else:
            cleaned = text.strip().rstrip(".!")
            if not cleaned:
                return dict(_STEPS[self._language])[field_name]
            self._card[field_name] = cleaned
        self._correcting_field = None
        self._step = _FIELD_COUNT + 1
        return self._confirmation_question()

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
        self._correcting_field = None
