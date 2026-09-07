"""Comment-preserving `.env` reader/writer.

Lives at the top level, not under `app/installer/`, because there are two
writers: the setup wizard (`app/installer/server.py`), and the running
service itself when an operator changes an audio device on the robot's own
page — a choice that has to survive a restart, so it lands in `.env` rather
than only in process memory (see `app/session_hub.py`'s
`set_preferred_mic_input`/`handle_audio_output`).

`app/config.py`'s `.env.r1.example`/`.env.g1.example` carry extensive
explanatory comments next to every setting — the wizard must be able to set
`GEMINI_API_KEY=...` or `WAKE_WORD_PHRASES=...` in the customer's real
`.env` without silently deleting all of that documentation, which a naive
"parse into a dict, dump the dict back out" round-trip would do. So this
walks the file line by line and only ever touches the one line whose key
matches; everything else (comments, blank lines, commented-out example
settings) survives byte-for-byte.
"""

from __future__ import annotations

from pathlib import Path

_KEY_LINE = "^([A-Za-z_][A-Za-z0-9_]*)="

# The one `.env` this deployment runs on — same file `app/config.py` loads
# at import time (project root, next to `pyproject.toml`). Defined here so
# the running service and the setup wizard can never disagree about which
# file they are editing.
ENV_PATH = Path(__file__).resolve().parent.parent / ".env"


def read_values(path: Path) -> dict[str, str]:
    """Every uncommented `KEY=value` line in `path`. Missing file -> {}."""
    if not path.exists():
        return {}
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        key = key.strip()
        if key.replace("_", "").isalnum() and key[:1].isalpha():
            values[key] = value.strip()
    return values


def set_values(path: Path, updates: dict[str, str], *, template: Path | None = None) -> None:
    """Set each `key: value` in `updates` inside `path`, creating it from
    `template` first if it doesn't exist yet (so the customer's first save
    still gets the template's comments, not a blank file with just the keys
    they touched). An existing, uncommented `KEY=` line is replaced in
    place; a key with no existing line is appended at the end, after a
    "added by the setup wizard" marker so it's obvious later which lines
    were hand-edited versus wizard-written.
    """
    if not path.exists():
        path.write_text(template.read_text(encoding="utf-8") if template else "", encoding="utf-8")

    lines = path.read_text(encoding="utf-8").splitlines()
    remaining = dict(updates)
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, _ = stripped.partition("=")
        key = key.strip()
        if key in remaining:
            lines[i] = f"{key}={remaining.pop(key)}"

    if remaining:
        if lines and lines[-1].strip():
            lines.append("")
        lines.append("# --- Добавлено мастером настройки -----------------------------------------")
        for key, value in remaining.items():
            lines.append(f"{key}={value}")

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
