"""app/config.py — default values worth pinning down with a real test,
not just a comment someone could silently drift away from.
"""

from __future__ import annotations

from app.config import Settings


def test_default_wake_word_phrases_cover_natural_greetings_and_help_requests() -> None:
    """UX ask, 2026-09-06 follow-up: "чтобы человек мог интуитивно без
    подсказок его активировать" — a visitor who was never told a magic
    word should still be able to wake the robot. Pins down that the
    unconfigured (no WAKE_WORD_PHRASES env var) default actually includes
    both the original robot/assistant-style words and the plain-greeting/
    help-request ones added for this, not just whatever the code comment
    currently claims.
    """
    phrases = set(Settings().wake_word_phrases)
    assert {"робот", "ассистент", "слушай"} <= phrases, "original wake words must survive"
    assert {"здравствуйте", "привет", "подскажите", "помощник"} <= phrases, (
        "must include natural greetings/help requests a visitor would say "
        "without ever being told a specific trigger word"
    )


def test_default_wake_word_phrases_cover_g1_visit_intake_triggers() -> None:
    """A visitor who opens with exactly the phrase G1's own
    system_instruction is listening for to start visit intake (see
    app/scenarios.py's _G1_TOOL_RULES: "оформите визит"/"я посетитель"/
    "зарегистрируйте меня") must not go unheard because the hardware mic
    was never awake to forward it to the model in the first place."""
    phrases = set(Settings().wake_word_phrases)
    assert {"посетитель", "визит", "оформите", "зарегистрируйте"} <= phrases
