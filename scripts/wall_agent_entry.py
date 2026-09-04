"""PyInstaller entry point for the standalone Windows `wall-agent.exe`.

PyInstaller needs a real script file to start its import analysis from —
`python -m app.wall` (the module invocation `app/wall/__main__.py` supports
for `uv run`) isn't something PyInstaller can target directly. This is a
thin wrapper, not a second implementation: it just calls the same
`app.wall.__main__.main()` that `uv run python -m app.wall` already runs.

See scripts/build_wall_agent_windows.bat for how this gets turned into
wall-agent.exe, and README.md's "Видеостена — установка на Windows" for how
an operator runs the result (no Python needed on that machine at all).
"""

from __future__ import annotations

from app.wall.__main__ import main

if __name__ == "__main__":
    main()
