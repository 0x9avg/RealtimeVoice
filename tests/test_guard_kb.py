"""app/guard_kb.py — GuardKb.ensure_fresh()'s live-reload behaviour.

UX audit finding, 2026-09-06: GuardKb used to load ONLY ONCE, at process
start, unlike app/knowledge.py's KnowledgeBase, which already re-scans its
source files on every tool call -- a customer's admin editing
config/guard-kb.yaml (per the PDF's own instructions) would see no effect
until someone thought to restart the whole service.
"""

from __future__ import annotations

import os
from pathlib import Path

from app.guard_kb import GuardKb

_V1 = """
rooms:
  - room: "305"
    floor: "третий этаж"
    unit: "Отдел кадров"
"""

_V2 = """
rooms:
  - room: "305"
    floor: "четвёртый этаж"
    unit: "Отдел кадров"
  - room: "410"
    floor: "четвёртый этаж"
    unit: "Бухгалтерия"
"""


def _write(path: Path, content: str, *, mtime_offset: float = 0.0) -> None:
    path.write_text(content, encoding="utf-8")
    if mtime_offset:
        stat = path.stat()
        new_time = stat.st_mtime + mtime_offset
        os.utime(path, (new_time, new_time))


def test_ensure_fresh_reloads_after_the_file_changes(tmp_path: Path) -> None:
    path = tmp_path / "guard-kb.yaml"
    _write(path, _V1)
    kb = GuardKb.load(path)
    assert kb.lookup("где кабинет 305") is not None
    assert "третий этаж" in kb.lookup("где кабинет 305")
    assert "не значится" in kb.lookup("где кабинет 410")

    # Simulate an admin edit landing later, with a clearly later mtime (real
    # filesystem mtime resolution can be coarser than a fast test's wall
    # clock, so force it forward rather than relying on real elapsed time).
    _write(path, _V2, mtime_offset=5.0)

    kb.ensure_fresh()

    assert "четвёртый этаж" in kb.lookup("где кабинет 305"), "must pick up the edited floor"
    assert kb.lookup("где кабинет 410") is not None, "must pick up the newly added room"


def test_ensure_fresh_is_a_no_op_when_the_file_has_not_changed(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "guard-kb.yaml"
    _write(path, _V1)
    kb = GuardKb.load(path)

    reload_calls = 0
    original_reload = GuardKb._reload

    def counting_reload(self: GuardKb) -> None:
        nonlocal reload_calls
        reload_calls += 1
        original_reload(self)

    # GuardKb is a slots dataclass -- patch the CLASS method, not an
    # instance attribute (slots have no instance __dict__ to shadow it in).
    monkeypatch.setattr(GuardKb, "_reload", counting_reload)

    kb.ensure_fresh()
    kb.ensure_fresh()
    kb.ensure_fresh()

    assert reload_calls == 0, "a stat() with no mtime change must not reparse the YAML"


def test_ensure_fresh_on_a_kb_with_no_source_path_is_a_no_op() -> None:
    # The default-constructed GuardKb() (used when the file is absent at
    # startup) has no `_path` -- ensure_fresh() must tolerate that, not
    # crash the tool call that calls it defensively before every lookup.
    kb = GuardKb()
    kb.ensure_fresh()
    assert kb.empty
