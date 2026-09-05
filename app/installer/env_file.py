"""Comment-preserving `.env` reader/writer.

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
