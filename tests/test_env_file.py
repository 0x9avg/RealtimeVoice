"""app/env_file.py — must edit `.env` without destroying the
extensive documentation comments in .env.r1.example/.env.g1.example."""

from __future__ import annotations

from pathlib import Path

from app.env_file import read_values, set_values


def test_set_values_creates_file_from_template_when_missing(tmp_path: Path) -> None:
    template = tmp_path / "template.example"
    template.write_text("# a helpful comment\nROBOT_PROFILE=r1\n", encoding="utf-8")
    env_path = tmp_path / ".env"

    set_values(env_path, {"GEMINI_API_KEY": "abc123"}, template=template)

    text = env_path.read_text(encoding="utf-8")
    assert "# a helpful comment" in text
    assert "ROBOT_PROFILE=r1" in text
    assert "GEMINI_API_KEY=abc123" in text


def test_set_values_replaces_existing_key_in_place_preserving_comments(tmp_path: Path) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text(
        "# important explanation of this setting\n"
        "GEMINI_API_KEY=old-value\n"
        "# another comment\n"
        "ROBOT_PROFILE=r1\n",
        encoding="utf-8",
    )

    set_values(env_path, {"GEMINI_API_KEY": "new-value"})

    lines = env_path.read_text(encoding="utf-8").splitlines()
    assert "# important explanation of this setting" in lines
    assert "# another comment" in lines
    assert "GEMINI_API_KEY=new-value" in lines
    assert "ROBOT_PROFILE=r1" in lines
    assert "GEMINI_API_KEY=old-value" not in lines


def test_set_values_appends_new_key_under_a_marker(tmp_path: Path) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text("ROBOT_PROFILE=g1\n", encoding="utf-8")

    set_values(env_path, {"WAKE_WORD_PHRASES": "робот,привет"})

    text = env_path.read_text(encoding="utf-8")
    assert "ROBOT_PROFILE=g1" in text
    assert "WAKE_WORD_PHRASES=робот,привет" in text


def test_read_values_ignores_comments_blank_lines_and_commented_out_settings(tmp_path: Path) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text(
        "# GEMINI_LIVE_MODEL=models/should-be-ignored\n"
        "\n"
        "GEMINI_API_KEY=real-key\n"
        "ROBOT_PROFILE=r1\n",
        encoding="utf-8",
    )

    values = read_values(env_path)

    assert values == {"GEMINI_API_KEY": "real-key", "ROBOT_PROFILE": "r1"}


def test_read_values_on_missing_file_is_empty(tmp_path: Path) -> None:
    assert read_values(tmp_path / "does-not-exist.env") == {}
