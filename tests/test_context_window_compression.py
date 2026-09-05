"""app/gemini_live.py's contextWindowCompression setup field — 2026-09-06,
added the same day MAX_SESSION_DURATION_S was tightened (G1 was hitting
Gemini's own forced 1008 policy-violation closures every ~150s). This is
Gemini's own native mechanism for the same problem: verified against
Google's BidiGenerateContentSetup reference that `contextWindowCompression`
is a SIBLING of `generationConfig` at the `setup` level, not nested inside
it, and that `triggerTokens`/`targetTokens` are plain JSON integers.
"""

from __future__ import annotations

from app.config import Settings
from app.gemini_live import GeminiLiveAdapter
from app.tools import ToolRegistry


def _setup(settings: Settings) -> dict:
    adapter = GeminiLiveAdapter(settings=settings, tools=ToolRegistry())
    return adapter._build_setup_message()["setup"]


def test_enabled_by_default_with_no_explicit_tokens() -> None:
    """Matches Google's own SDK examples: enable with an empty sliding
    window and no explicit trigger, letting the platform pick sensible
    defaults, rather than requiring a customer to tune token thresholds
    just to turn this on."""
    setup = _setup(Settings())

    assert setup["contextWindowCompression"] == {"slidingWindow": {}}


def test_is_a_sibling_of_generation_config_not_nested_inside_it() -> None:
    """The easy mistake to make -- confirmed against Google's
    BidiGenerateContentSetup reference that contextWindowCompression lives
    at the same level as generationConfig, not inside it."""
    setup = _setup(Settings())

    assert "contextWindowCompression" in setup
    assert "contextWindowCompression" not in setup["generationConfig"]


def test_explicit_trigger_and_target_tokens_are_plain_integers() -> None:
    settings = Settings(
        context_window_compression_trigger_tokens=16000,
        context_window_compression_target_tokens=12000,
    )

    setup = _setup(settings)

    assert setup["contextWindowCompression"] == {
        "triggerTokens": 16000,
        "slidingWindow": {"targetTokens": 12000},
    }
    assert isinstance(setup["contextWindowCompression"]["triggerTokens"], int)
    assert isinstance(setup["contextWindowCompression"]["slidingWindow"]["targetTokens"], int)


def test_trigger_tokens_alone_still_nests_an_empty_sliding_window() -> None:
    settings = Settings(context_window_compression_trigger_tokens=16000)

    setup = _setup(settings)

    assert setup["contextWindowCompression"] == {"triggerTokens": 16000, "slidingWindow": {}}


def test_disabled_omits_the_field_entirely() -> None:
    setup = _setup(Settings(context_window_compression_enabled=False))

    assert "contextWindowCompression" not in setup
