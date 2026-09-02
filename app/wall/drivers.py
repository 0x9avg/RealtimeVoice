r"""Drivers for the MinTrans (Factories.exe) wall application.

Two drivers live here:

* `SimWallDriver` — in-memory model, ported from `../../humanoid-robot/
  apps/wall-agent`'s driver of the same name. Cross-platform, no real app.
* `PipeWallDriver` — talks to the real app's own control interface: a
  Windows named pipe (`S.Networks.Pipes.Unity.MinTrans`) served by its
  `V0PipeServer`. This is plain Windows file I/O (`open(r"\\.\pipe\...")`),
  the same approach `humanoid-robot`'s own `PipeWallDriver` uses — it works
  wherever this process runs *as long as that process is on Windows*, since
  named pipes are OS-local. It does **not** shell out to PowerShell or
  depend on WSL in any way; the WSL/PowerShell bridge used earlier in this
  project was only a manual verification aid for a sandbox with no Windows
  Python available, never part of the shipped driver.

Deployment topology (this is what makes it work across machines): this
module is meant to run as its own process (`python -m app.wall --driver
pipe`, see `__main__.py`) **on the wall's own Windows PC**, next to
Factories.exe. The voice/tool-calling backend (`../tools.py`) talks to it
over plain HTTP via `WALL_AGENT_URL` and never needs to be on the same
machine, or even the same OS — that boundary already existed (see
`../server.py`) and nothing here changes it.

`sendinput` (mouse/key emulation, needed for slides — PgUp/PgDn — since
those have no pipe command at all, confirmed via `strings` on the real
`level0` scene) is not ported: it requires on-site coordinate calibration
against a real screen, which cannot be done from this repo. Run
`cortex-wall-agent --driver sendinput` from `../../humanoid-robot` for that.
"""

from __future__ import annotations

import logging
import platform
import threading
import time
from typing import Any, Protocol

from app.wall.commands import (
    WALL_CATEGORY_SECTIONS,
    WallCategory,
    WallCommand,
    WallCommandKind,
    WallCommandOutcome,
    WallCommandResult,
    WallNavAction,
    WallSection,
)

log = logging.getLogger(__name__)

_MAIN_SCREEN = "Main"
_MAX_SLIDE = 99

# Confirmed against the real scene (`strings level0`): MENU_AVTO/MENU_JD/
# MENU_AERO are real pipe-addressable category screens, distinct from any
# WallSection. The `Category:` prefix keeps this screen name from ever
# colliding with (or being mistaken for) a real WallSection value.
_CATEGORY_SCREEN_PREFIX = "Category:"


def _category_screen(category: WallCategory) -> str:
    return f"{_CATEGORY_SCREEN_PREFIX}{category.value}"


class WallDriver(Protocol):
    """A backend that executes wall commands."""

    name: str

    def execute(self, command: WallCommand) -> WallCommandResult:
        """Apply one command to the wall application."""
        ...

    def state(self) -> dict[str, Any]:
        """Best-effort view of the wall application's current state."""
        ...


def _section_category(section: WallSection) -> WallCategory:
    for category, sections in WALL_CATEGORY_SECTIONS.items():
        if section in sections:
            return category
    msg = f"section {section} is not in any category"  # pragma: no cover
    raise ValueError(msg)  # pragma: no cover


class SimWallDriver:
    """In-memory model of the wall application.

    Mirrors what the real app does: one main menu, 12 section screens, and a
    slide sequence inside each section (PgUp/PgDn in the real app).
    """

    name = "sim"

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._screen: str = _MAIN_SCREEN
        self._slide = 0
        self.history: list[WallCommand] = []

    def execute(self, command: WallCommand) -> WallCommandResult:
        with self._lock:
            self.history.append(command)
            if command.kind is WallCommandKind.OPEN_SECTION:
                assert command.section is not None  # noqa: S101 — validated model
                self._screen = command.section.value
                self._slide = 0
                log.info("sim: open section %s", command.section.value)
                return WallCommandResult(outcome=WallCommandOutcome.ACCEPTED)
            if command.kind is WallCommandKind.OPEN_CATEGORY:
                assert command.category is not None  # noqa: S101 — validated model
                self._screen = _category_screen(command.category)
                self._slide = 0
                log.info("sim: open category %s", command.category.value)
                return WallCommandResult(outcome=WallCommandOutcome.ACCEPTED)
            assert command.nav is not None  # noqa: S101 — validated model
            return self._navigate(command.nav)

    def _current_section(self) -> WallSection | None:
        """The open section, or ``None`` on Main / a category screen.

        A category screen (`MENU_AVTO` etc.) is a real, distinct screen in
        the app — not "no section open" in the sense of Main — but it is
        also not a project you can page through, so it behaves the same way
        here for next/prev section/slide purposes.
        """
        try:
            return WallSection(self._screen)
        except ValueError:
            return None

    def _navigate(self, nav: WallNavAction) -> WallCommandResult:
        if nav is WallNavAction.MAIN_MENU:
            self._screen = _MAIN_SCREEN
            self._slide = 0
            log.info("sim: main menu")
            return WallCommandResult(outcome=WallCommandOutcome.ACCEPTED)
        if nav in (WallNavAction.NEXT_SLIDE, WallNavAction.PREV_SLIDE):
            if self._current_section() is None:
                return WallCommandResult(
                    outcome=WallCommandOutcome.REJECTED,
                    detail="no section open",
                )
            step = 1 if nav is WallNavAction.NEXT_SLIDE else -1
            self._slide = min(max(self._slide + step, 0), _MAX_SLIDE)
            log.info("sim: slide -> %d", self._slide)
            return WallCommandResult(outcome=WallCommandOutcome.ACCEPTED)
        # next_section / prev_section — wrap within the current category.
        current = self._current_section()
        if current is None:
            return WallCommandResult(
                outcome=WallCommandOutcome.REJECTED,
                detail="no section open",
            )
        sections = WALL_CATEGORY_SECTIONS[_section_category(current)]
        index = sections.index(current)
        step = 1 if nav is WallNavAction.NEXT_SECTION else -1
        target = sections[(index + step) % len(sections)]
        self._screen = target.value
        self._slide = 0
        log.info("sim: section -> %s", target.value)
        return WallCommandResult(outcome=WallCommandOutcome.ACCEPTED)

    def state(self) -> dict[str, Any]:
        with self._lock:
            return {"screen": self._screen, "slide": self._slide, "exact": True}


# --- real app, native named pipe ------------------------------------------

# Reverse-engineered from Assembly-CSharp.dll (matches humanoid-robot's own
# finding) and confirmed live against the real Factories.exe on 02.09.2026:
# MENU_AERO1 -> "OK" switched the screen to the Zaysan-airport section,
# MENU_JD2 -> "OK" switched it to Moyynty-Kyzylzhar, screenshots checked
# both times. Avto5 has genuinely no MENU_ command (confirmed via `strings`
# on level0: the GameObject "Avto5" exists, "MENU_AVTO5" does not).
_PIPE_NAME = "S.Networks.Pipes.Unity.MinTrans"

_SECTION_TO_MENU: dict[WallSection, str] = {
    WallSection.AVTO1: "MENU_AVTO1",
    WallSection.AVTO2: "MENU_AVTO2",
    WallSection.AVTO3: "MENU_AVTO3",
    WallSection.AVTO4: "MENU_AVTO4",
    # Avto5 intentionally absent — see module docstring.
    WallSection.JD1: "MENU_JD1",
    WallSection.JD2: "MENU_JD2",
    WallSection.JD3: "MENU_JD3",
    WallSection.AERO1: "MENU_AERO1",
    WallSection.AERO2: "MENU_AERO2",
    WallSection.AERO3: "MENU_AERO3",
    WallSection.AERO4: "MENU_AERO4",
}

# Confirmed via `strings level0` (not in humanoid-robot's driver, which never
# wired these up even though it half-anticipated them — see commands.py).
_CATEGORY_TO_MENU: dict[WallCategory, str] = {
    WallCategory.AVTO: "MENU_AVTO",
    WallCategory.JD: "MENU_JD",
    WallCategory.AERO: "MENU_AERO",
}

_MENU_MAIN = "MENU_MAIN"


class PipeWallDriver:
    """Drives the real wall app through its native named-pipe command server.

    This is the app's OWN interface (V0PipeServer): connect, send a line
    like ``MENU_AERO1\\n``, read the ``OK``/``FAIL`` reply. A fresh
    connection per command matches the server, which accepts one client,
    handles it, and loops back to listen — matches `humanoid-robot`'s own
    `PipeWallDriver`.

    `next_section`/`prev_section` have no dedicated pipe command either, but
    unlike slides they don't need one: this driver tracks which section it
    last successfully opened and just sends the *next* section's own
    `open_section` command — the same wrap-within-category logic
    `SimWallDriver` uses, now actually driving the real app. State tracking
    is best-effort (never read back from the app, only from what this
    driver itself has sent and had acknowledged) — `state()` reports
    `exact: False` for that reason.
    """

    name = "pipe"

    def __init__(self, pipe_name: str = _PIPE_NAME, *, timeout_s: float = 5.0) -> None:
        if platform.system() != "Windows":
            msg = "pipe driver requires Windows (named pipes are OS-local)"
            raise RuntimeError(msg)
        self._pipe_path = rf"\\.\pipe\{pipe_name}"
        self._timeout_s = timeout_s
        self._lock = threading.Lock()
        self._screen = "unknown"

    def _send_command(self, command_name: str) -> tuple[bool, str]:  # pragma: no cover — Windows I/O
        # Line-oriented UTF-8, matching the app's StreamReader/StreamWriter
        # (ReadLineAsync / WriteLine, AutoFlush=true).
        deadline = time.monotonic() + self._timeout_s
        last_err = "pipe not available"
        while time.monotonic() < deadline:
            try:
                with open(self._pipe_path, "r+b", buffering=0) as pipe:
                    pipe.write((command_name + "\n").encode("utf-8"))
                    reply = pipe.readline().decode("utf-8", "replace").strip()
                    return reply.upper() == "OK", reply or "(no reply)"
            except OSError as exc:  # pipe busy / not yet created — retry briefly
                last_err = str(exc)
                time.sleep(0.1)
        return False, last_err

    def execute(self, command: WallCommand) -> WallCommandResult:
        if command.kind is WallCommandKind.OPEN_SECTION:
            assert command.section is not None  # noqa: S101 — validated model
            return self._open_section(command.section)
        if command.kind is WallCommandKind.OPEN_CATEGORY:
            assert command.category is not None  # noqa: S101 — validated model
            return self._open_category(command.category)
        assert command.nav is not None  # noqa: S101 — validated model
        return self._navigate(command.nav)

    def _open_section(self, section: WallSection) -> WallCommandResult:
        menu = _SECTION_TO_MENU.get(section)
        if menu is None:
            return WallCommandResult(
                outcome=WallCommandOutcome.REJECTED,
                detail=f"section {section.value} has no wall command",
            )
        return self._apply(menu, on_success_screen=section.value)

    def _open_category(self, category: WallCategory) -> WallCommandResult:
        menu = _CATEGORY_TO_MENU[category]
        return self._apply(menu, on_success_screen=_category_screen(category))

    def _navigate(self, nav: WallNavAction) -> WallCommandResult:
        if nav is WallNavAction.MAIN_MENU:
            return self._apply(_MENU_MAIN, on_success_screen=_MAIN_SCREEN)
        if nav in (WallNavAction.NEXT_SLIDE, WallNavAction.PREV_SLIDE):
            return WallCommandResult(
                outcome=WallCommandOutcome.REJECTED,
                detail=f"nav {nav.value} has no pipe command (use the sendinput driver)",
            )
        # next_section / prev_section: computed from tracked state, then
        # issued as that section's own open_section command.
        with self._lock:
            current = self._current_section()
        if current is None:
            return WallCommandResult(outcome=WallCommandOutcome.REJECTED, detail="no section open")
        sections = WALL_CATEGORY_SECTIONS[_section_category(current)]
        index = sections.index(current)
        step = 1 if nav is WallNavAction.NEXT_SECTION else -1
        target = sections[(index + step) % len(sections)]
        return self._open_section(target)

    def _current_section(self) -> WallSection | None:
        try:
            return WallSection(self._screen)
        except ValueError:
            return None

    def _apply(self, menu: str, *, on_success_screen: str) -> WallCommandResult:
        with self._lock:
            ok, reply = self._send_command(menu)
            if ok:
                self._screen = on_success_screen
        if ok:
            log.info("pipe: %s -> OK", menu)
            return WallCommandResult(outcome=WallCommandOutcome.ACCEPTED)
        log.warning("pipe: %s -> %s", menu, reply)
        outcome = (
            WallCommandOutcome.UNREACHABLE
            if reply == "pipe not available"
            else WallCommandOutcome.REJECTED
        )
        return WallCommandResult(outcome=outcome, detail=reply)

    def state(self) -> dict[str, Any]:
        with self._lock:
            slide = 0 if self._current_section() is not None else None
            return {"screen": self._screen, "slide": slide, "exact": False}


def build_driver(name: str) -> WallDriver:
    """Factory used by `__main__.py`."""
    if name == "sim":
        return SimWallDriver()
    if name == "pipe":
        return PipeWallDriver()
    msg = f"unknown wall driver {name!r}"
    raise ValueError(msg)
