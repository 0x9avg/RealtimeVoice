"""Domain model for the presentation video wall (MinTrans/Factories.exe).

Ported from `../../humanoid-robot/packages/humanoid-robot-domain/src/
humanoid_robot/domain/wall/commands.py` — that repo's the source of truth for
the `open_section`/`navigate` shape (it's what the real wall-agent, the
robot's core API, and the Gemini tool in `apps/voice-live` already use).
Section identifiers match the application's own screen/folder names
(`Datas/<section>`).

`open_category`/`WallCategory` diverges from that source: `strings` against
the real `MinTrans/PC/Factories_Data/level0` scene file confirmed
`MENU_AVTO`/`MENU_JD`/`MENU_AERO` exist as real, pipe-addressable category
screens in the compiled app — `humanoid-robot`'s own driver code already
half-anticipated this (`_CATEGORY_MENU` in its `drivers.py`) but never wired
it into `WallCommand`. Added here once confirmed against the actual binary,
not against that repo's (incomplete, on this point) domain model.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, model_validator


class WallSection(StrEnum):
    """A presentation section (screen) of the wall application."""

    AVTO1 = "Avto1"
    AVTO2 = "Avto2"
    AVTO3 = "Avto3"
    AVTO4 = "Avto4"
    AVTO5 = "Avto5"
    JD1 = "JD1"
    JD2 = "JD2"
    JD3 = "JD3"
    AERO1 = "Aero1"
    AERO2 = "Aero2"
    AERO3 = "Aero3"
    AERO4 = "Aero4"


class WallCategory(StrEnum):
    """Section category shown in the wall application's main menu."""

    AVTO = "avto"
    JD = "jd"
    AERO = "aero"


WALL_CATEGORY_SECTIONS: dict[WallCategory, tuple[WallSection, ...]] = {
    WallCategory.AVTO: (
        WallSection.AVTO1,
        WallSection.AVTO2,
        WallSection.AVTO3,
        WallSection.AVTO4,
        WallSection.AVTO5,
    ),
    WallCategory.JD: (WallSection.JD1, WallSection.JD2, WallSection.JD3),
    WallCategory.AERO: (
        WallSection.AERO1,
        WallSection.AERO2,
        WallSection.AERO3,
        WallSection.AERO4,
    ),
}


class WallNavAction(StrEnum):
    """Navigation actions that do not target a specific section.

    Used to include NEXT_SECTION/PREV_SECTION/NEXT_SLIDE/PREV_SLIDE — cut
    entirely (not just from the frontend): the real Factories.exe app has no
    way to step through sections or slides forward/back, so exposing those
    as callable actions (even the sim driver's best-effort emulation of
    them) was offering a capability the real wall doesn't have.
    """

    MAIN_MENU = "main_menu"


class WallCommandKind(StrEnum):
    """Discriminates the three shapes of a wall command."""

    OPEN_SECTION = "open_section"
    OPEN_CATEGORY = "open_category"
    NAVIGATE = "navigate"


class WallCommand(BaseModel):
    """A single command for the wall-control agent."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: WallCommandKind
    section: WallSection | None = None
    category: WallCategory | None = None
    nav: WallNavAction | None = None

    @model_validator(mode="after")
    def _check_shape(self) -> WallCommand:
        if self.kind is WallCommandKind.OPEN_SECTION and self.section is None:
            msg = "open_section command requires a section"
            raise ValueError(msg)
        if self.kind is WallCommandKind.OPEN_CATEGORY and self.category is None:
            msg = "open_category command requires a category"
            raise ValueError(msg)
        if self.kind is WallCommandKind.NAVIGATE and self.nav is None:
            msg = "navigate command requires a nav action"
            raise ValueError(msg)
        return self


class WallCommandOutcome(StrEnum):
    """Terminal state of a wall command."""

    ACCEPTED = "accepted"
    REJECTED = "rejected"
    UNREACHABLE = "unreachable"


class WallCommandResult(BaseModel):
    """Result reported by the wall-control agent (or the HTTP client)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    outcome: WallCommandOutcome
    detail: str = ""
