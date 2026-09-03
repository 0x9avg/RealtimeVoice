"""VisitIntake — the deterministic G1 visit-registration state machine.

Covers the invariant the customer's ТЗ is explicit about (a bare
"нет"/"жоқ"/"no" at confirmation restarts the WHOLE interview, not one
field) in all three supported languages, plus the explicit language-choice
step, cancellation, and yes/no reprompting.
"""

from __future__ import annotations

from app.visit_intake import VisitIntake


def test_full_flow_ru() -> None:
    vi = VisitIntake()
    vi.start()
    vi.consume("на русском")
    vi.consume("Иванов Иван Иванович")
    vi.consume("ООО Ромашка")
    vi.consume("деловая встреча")
    vi.consume("к Петрову")
    vi.consume("да")
    reply, card = vi.consume("нет")  # has_id -> triggers confirmation
    assert "Всё верно" in reply
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
    vi = VisitIntake()
    vi.start()
    vi.consume("қазақша")
    vi.consume("Ахметов Асхат Ахметович")
    vi.consume("жеке тұлға")
    vi.consume("кездесу")
    vi.consume("Петровқа")
    vi.consume("иә")
    reply, card = vi.consume("иә")
    assert "дұрыс па" in reply
    assert card is None

    reply, card = vi.consume("иә")
    assert card is not None
    assert card["language"] == "kk"
    assert "Рақмет" in reply


def test_no_at_confirmation_restarts_whole_interview_not_one_field() -> None:
    """The literal, deliberate customer requirement: 'нет' at confirmation
    goes back to field 1, not a point-fix of one field."""
    vi = VisitIntake()
    vi.start()
    vi.consume("на русском")
    vi.consume("Иванов Иван Иванович")
    vi.consume("ООО Ромашка")
    vi.consume("деловая встреча")
    vi.consume("к Петрову")
    vi.consume("да")
    vi.consume("нет")  # -> confirmation question

    reply, card = vi.consume("нет")  # reject the card
    assert card is None
    assert "заново" in reply
    assert vi._step == 1  # back to the first field
    assert vi._language == "ru"  # language choice is NOT re-asked

    # redo to completion -- only NOW should a record land
    vi.consume("Сидоров Сидор Сидорович")
    vi.consume("Частное лицо")
    vi.consume("визит к другу")
    vi.consume("к Петрову")
    vi.consume("да")
    vi.consume("да")
    reply, card = vi.consume("да")
    assert card is not None
    assert card["full_name"] == "Сидоров Сидор Сидорович"


def test_full_flow_en() -> None:
    vi = VisitIntake()
    vi.start()
    vi.consume("English please")
    vi.consume("John Smith")
    vi.consume("Acme Corp")
    vi.consume("business meeting")
    vi.consume("Mr. Petrov")
    vi.consume("yes")
    reply, card = vi.consume("no")
    assert "Is that correct" in reply
    assert card is None

    reply, card = vi.consume("yes")
    assert card is not None
    assert card["language"] == "en"
    assert card["full_name"] == "John Smith"
    assert card["has_pass"] is True
    assert card["has_id"] is False
    assert "Thank you" in reply


def test_no_at_confirmation_restarts_in_english_too() -> None:
    vi = VisitIntake()
    vi.start()
    vi.consume("english")
    for answer in ("A", "B", "C", "D", "yes", "yes"):
        vi.consume(answer)
    reply, card = vi.consume("no")
    assert card is None
    assert "start over" in reply
    assert vi._language == "en"


def test_cancel_speaks_english_when_english_was_chosen() -> None:
    vi = VisitIntake()
    vi.start()
    vi.consume("english")
    reply, card = vi.consume("cancel")
    assert card is None
    assert "cancelled" in reply


def test_no_at_confirmation_restarts_in_kazakh_too() -> None:
    vi = VisitIntake()
    vi.start()
    vi.consume("қазақша")
    for answer in ("А", "Б", "В", "Г", "иә", "иә"):
        vi.consume(answer)
    reply, card = vi.consume("жоқ")
    assert card is None
    assert "қайтадан" in reply
    assert vi._language == "kk"


def test_cancel_mid_language_step() -> None:
    vi = VisitIntake()
    vi.start()
    reply, card = vi.consume("отмена")
    assert card is None
    assert not vi.engaged


def test_cancel_speaks_in_the_chosen_language() -> None:
    """Regression: _reset() used to clear _language to "ru" BEFORE the
    cancel/completion messages read it, so a Kazakh interview would
    cancel/finish by speaking Russian."""
    vi = VisitIntake()
    vi.start()
    vi.consume("қазақша")
    reply, card = vi.consume("тоқта")
    assert card is None
    assert "рәсімдеу тоқтатылды" in reply


def test_reprompts_on_unrecognised_language_choice() -> None:
    vi = VisitIntake()
    vi.start()
    reply = vi.consume("не понял вопрос")[0]
    assert "орысша ма" in reply
    reply = vi.consume("на казахском")[0]
    assert "Тегіңізді" in reply


def test_reprompts_on_unrecognised_yes_no() -> None:
    vi = VisitIntake()
    vi.start()
    vi.consume("на русском")
    for _ in range(5):
        vi.consume("x")
    reply = vi.consume("может быть")[0]
    assert "не расслышал" in reply
