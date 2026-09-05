"""VisitIntake — the deterministic G1 visit-registration state machine.

Covers the current behaviour (rewritten 2026-09-05, see app/visit_intake.py's
module docstring for the customer feedback behind each change):

* no explicit "which language?" question any more — the interview starts
  directly on ФИО, and the language is auto-detected (script-based
  heuristic) from the visitor's own answers as they come in;
* a bare "нет"/"жоқ"/"no" at confirmation opens a correction menu naming the
  six fields instead of restarting the whole interview — except "заново"/
  "start over" at that menu, which is the old full-restart behaviour kept as
  an explicit escape hatch;
* cancellation and yes/no reprompting, in all three supported languages.
"""

from __future__ import annotations

from app.visit_intake import VisitIntake, _detect_language


def test_full_flow_ru() -> None:
    vi = VisitIntake()
    vi.start()
    vi.consume("Иванов Иван Иванович")
    vi.consume("ООО Ромашка")
    vi.consume("деловая встреча")
    vi.consume("к Петрову")
    vi.consume("да")
    reply, card = vi.consume("нет")  # has_id -> triggers confirmation
    assert "Верно" in reply
    assert card is None

    reply, card = vi.consume("да")
    assert card is not None
    assert card["language"] == "ru"
    assert card["full_name"] == "Иванов Иван Иванович"
    assert card["has_pass"] is True
    assert card["has_id"] is False
    assert "Спасибо" in reply
    assert not vi.engaged


def test_full_flow_kk() -> None:
    """The full_name answer ("Ахметұлы Асхат") contains a Kazakh-only letter
    (ұ) -- language must flip to kk from that FIRST answer onward, even
    though start() always begins in Russian (there's no upfront choice)."""
    vi = VisitIntake()
    vi.start()
    vi.consume("Ахметұлы Асхат")
    reply = vi.consume("жеке тұлға")[0]
    assert "мақсаты" in reply  # organization question answered -> asks purpose, in kk
    vi.consume("кездесу")
    vi.consume("Петровқа")
    vi.consume("иә")
    reply, card = vi.consume("иә")
    assert "Дұрыс па" in reply
    assert card is None

    reply, card = vi.consume("иә")
    assert card is not None
    assert card["language"] == "kk"
    assert "Рақмет" in reply


def test_full_flow_en() -> None:
    """"John Smith" is Latin-only, no Cyrillic -- must be detected as
    English from the first answer, same mechanism as the kk test above."""
    vi = VisitIntake()
    vi.start()
    vi.consume("John Smith")
    vi.consume("Acme Corp")
    vi.consume("business meeting")
    vi.consume("Mr. Petrov")
    vi.consume("yes")
    reply, card = vi.consume("no")
    assert "Correct?" in reply
    assert card is None

    reply, card = vi.consume("yes")
    assert card is not None
    assert card["language"] == "en"
    assert card["full_name"] == "John Smith"
    assert card["has_pass"] is True
    assert card["has_id"] is False
    assert "Thank you" in reply


def test_no_at_confirmation_opens_correction_menu_and_preserves_other_fields() -> None:
    """Item 9, 2026-09-05: "нет" no longer restarts everything -- it opens a
    menu, and correcting ONE field must leave the other five untouched."""
    vi = VisitIntake()
    vi.start()
    vi.consume("Иванов Иван Иванович")
    vi.consume("ООО Ромашка")
    vi.consume("деловая встреча")
    vi.consume("к Петрову")
    vi.consume("да")
    vi.consume("да")  # -> confirmation question

    reply, card = vi.consume("нет")  # reject the card
    assert card is None
    assert "что именно поправить" in reply

    reply, card = vi.consume("организация")  # name the field to fix
    assert card is None
    assert "организации" in reply  # re-asks exactly the organization question

    reply, card = vi.consume("ИП Сидоров")  # give the corrected value
    assert card is None
    assert "Верно" in reply  # straight back to a re-read confirmation
    assert "ИП Сидоров" in reply
    assert "Иванов Иван Иванович" in reply  # untouched fields survived

    reply, card = vi.consume("да")
    assert card is not None
    assert card["organization"] == "ИП Сидоров"
    assert card["full_name"] == "Иванов Иван Иванович"  # never re-asked, still correct
    assert card["purpose"] == "деловая встреча"


def test_correction_menu_restart_everything_escape_hatch() -> None:
    """Saying "заново" at the correction menu (instead of naming one field)
    keeps the OLD full-restart behaviour available."""
    vi = VisitIntake()
    vi.start()
    vi.consume("Иванов Иван Иванович")
    vi.consume("ООО Ромашка")
    vi.consume("деловая встреча")
    vi.consume("к Петрову")
    vi.consume("да")
    vi.consume("да")
    vi.consume("нет")  # -> correction menu

    reply, card = vi.consume("заново, всё неправильно")
    assert card is None
    assert "заново" in reply
    assert vi._step == 1  # back to the first field
    assert vi._language == "ru"

    vi.consume("Сидоров Сидор Сидорович")
    vi.consume("Частное лицо")
    vi.consume("визит к другу")
    vi.consume("к Петрову")
    vi.consume("да")
    vi.consume("да")
    reply, card = vi.consume("да")
    assert card is not None
    assert card["full_name"] == "Сидоров Сидор Сидорович"


def test_correction_menu_reprompts_on_unrecognised_field_name() -> None:
    vi = VisitIntake()
    vi.start()
    for answer in ("Иванов Иван", "ООО Ромашка", "встреча", "к Петрову", "да", "да", "нет"):
        vi.consume(answer)
    reply = vi.consume("что-то непонятное")[0]
    assert "Не разобрал" in reply


def test_cancel_mid_interview() -> None:
    vi = VisitIntake()
    vi.start()
    reply, card = vi.consume("отмена")
    assert card is None
    assert not vi.engaged
    assert "отменено" in reply


def test_cancel_words_embedded_in_a_real_answer_do_not_abort_the_interview() -> None:
    """2026-09-06 audit finding: _CANCEL_RE used to be an unanchored
    substring search, so any of these words appearing ANYWHERE inside a
    genuine answer (not just as a standalone cancel command) silently
    cancelled the whole registration -- indistinguishable, to the
    visitor, from the robot randomly aborting mid-interview."""
    vi = VisitIntake()
    vi.start()
    vi.consume("Иванов Иван Иванович")
    vi.consume("ТОО Ромашка")
    reply, card = vi.consume("хочу уточнить про отмену старой брони")
    assert vi.engaged
    assert card is None
    assert "отменено" not in reply

    vi.consume("к директору")
    reply, card = vi.consume("нет, не надо продлевать пропуск")
    assert vi.engaged, "a 'no' answer containing 'не надо' must not cancel the intake"
    assert card is None


def test_cancel_speaks_in_the_detected_language() -> None:
    """Regression: _reset() used to clear _language to "ru" BEFORE the
    cancel/completion messages read it, so a Kazakh interview would
    cancel/finish by speaking Russian."""
    vi = VisitIntake()
    vi.start()
    vi.consume("Ахметұлы Асхат")  # kk-signalling answer
    reply, card = vi.consume("тоқта")
    assert card is None
    assert "рәсімдеу тоқтатылды" in reply


def test_reprompts_on_unrecognised_yes_no() -> None:
    vi = VisitIntake()
    vi.start()
    for _ in range(5):
        vi.consume("x")
    reply = vi.consume("может быть")[0]
    assert "не расслышал" in reply


def test_detect_language_prefers_explicit_name_over_script() -> None:
    assert _detect_language("говорю на английском", "ru") == "en"


def test_detect_language_falls_back_to_current_when_ambiguous() -> None:
    """A bare "да"/short answer with no distinguishing letters must not
    flip the language away from whatever's already active."""
    assert _detect_language("да", "kk") == "kk"
    assert _detect_language("42", "en") == "en"


def test_detect_language_kazakh_only_letters_win_over_plain_cyrillic() -> None:
    assert _detect_language("тұлға", "ru") == "kk"
