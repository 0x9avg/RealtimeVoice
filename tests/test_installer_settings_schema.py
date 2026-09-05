"""app/installer/settings_schema.py — the catalogue backing the wizard's
"Расширенные настройки" page.
"""

from __future__ import annotations

from app.installer import settings_schema


def test_every_field_has_a_unique_key_and_a_non_empty_label() -> None:
    keys = settings_schema.all_keys()
    assert len(keys) == len(set(keys)), "duplicate keys would silently shadow each other in a form"
    for group in settings_schema.GROUPS:
        for field in group["fields"]:
            assert field["label"].strip()


def test_select_fields_declare_at_least_two_options() -> None:
    for group in settings_schema.GROUPS:
        for field in group["fields"]:
            if field["type"] == "select":
                assert len(field.get("options", [])) >= 2, field["key"]


def test_visible_groups_hides_the_other_profiles_fields() -> None:
    r1_keys = {f["key"] for g in settings_schema.visible_groups("r1") for f in g["fields"]}
    g1_keys = {f["key"] for g in settings_schema.visible_groups("g1") for f in g["fields"]}
    assert "WALL_AGENT_TOKEN" in r1_keys
    assert "WALL_AGENT_TOKEN" not in g1_keys
    assert "GUARD_KB_PATH" in g1_keys
    assert "GUARD_KB_PATH" not in r1_keys
    # Shared (profile=None) fields must appear for both.
    assert "MAX_SESSION_DURATION_S" in r1_keys
    assert "MAX_SESSION_DURATION_S" in g1_keys


def test_visible_groups_never_returns_an_empty_group() -> None:
    for profile in ("r1", "g1"):
        for group in settings_schema.visible_groups(profile):
            assert group["fields"], f"group {group['id']!r} has no fields for profile {profile!r}"


def test_defaults_cover_every_field() -> None:
    keys = set(settings_schema.all_keys())
    assert set(settings_schema.defaults()) == keys
