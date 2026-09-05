"""app/installer/logs.py's friendly_summary — must translate the known-
normal/known-bad lines documented in DEPLOYMENT.md §12 without inventing an
explanation for anything it doesn't actually recognise."""

from __future__ import annotations

from app.installer.logs import friendly_summary


def test_known_normal_line_gets_a_reassuring_translation() -> None:
    entries = friendly_summary("2026-09-05 INFO gemini: gemini.connection_state state=connecting")
    assert len(entries) == 1
    assert "Подключение" in entries[0]["friendly"]


def test_missing_api_key_line_gets_a_clear_actionable_translation() -> None:
    entries = friendly_summary("RuntimeError: no Gemini API key: set env GEMINI_API_KEY")
    assert "API-ключ" in entries[0]["friendly"]


def test_unrecognized_line_passes_through_with_no_friendly_text() -> None:
    entries = friendly_summary("some totally unrelated line nobody wrote a pattern for")
    assert entries[0]["friendly"] is None
    assert entries[0]["raw"] == "some totally unrelated line nobody wrote a pattern for"


def test_blank_lines_are_skipped() -> None:
    entries = friendly_summary("line one\n\n   \nline two")
    assert [e["raw"] for e in entries] == ["line one", "line two"]


def test_generic_error_line_is_flagged() -> None:
    entries = friendly_summary("Traceback (most recent call last):")
    assert "Ошибка" in entries[0]["friendly"]
