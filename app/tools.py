"""Tool registry for the voice session — the only thing the model can reach
besides talking.

Deliberately minimal for the first prototype: one read-only tool
(`get_current_time`), no allowlist/policy engine, no idempotency/`call_id`
dedup — those belong to a real Tool Broker once there is more than one
side-effect-free tool to gate (see ../CLAUDE.md and the overview doc's
Tool Broker section for what that grows into).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

from app.knowledge import KnowledgeBase
from app.wall.commands import WallCategory, WallCommand, WallCommandKind, WallCommandOutcome


@dataclass(frozen=True, slots=True)
class ToolResult:
    """What goes back to the model. `text` is spoken-language, not JSON."""

    ok: bool
    text: str


class Tool(Protocol):
    name: str
    description: str

    @property
    def parameters(self) -> dict[str, Any]: ...

    async def execute(self, args: dict[str, Any]) -> ToolResult: ...


@dataclass(slots=True)
class ToolRegistry:
    """Holds the tools the model is offered and dispatches calls to them."""

    _tools: dict[str, Tool] = field(default_factory=dict)

    def register(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def function_declarations(self) -> list[dict[str, Any]]:
        return [
            {"name": tool.name, "description": tool.description, "parameters": tool.parameters}
            for tool in self._tools.values()
        ]

    async def dispatch(self, name: str, args: dict[str, Any]) -> ToolResult:
        tool = self._tools.get(name)
        if tool is None:
            return ToolResult(ok=False, text=f"unknown tool {name!r}")
        try:
            return await tool.execute(args)
        except Exception as exc:  # a broken tool must not crash the session
            return ToolResult(ok=False, text=f"{name} failed: {exc}")


@dataclass(frozen=True, slots=True)
class GetCurrentTimeTool:
    """Demo tool: proves the full function-calling round trip works."""

    default_timezone: str
    name: str = "get_current_time"
    description: str = (
        "Return the current date and time. Always call this when asked what "
        "time or date it is right now — never guess or rely on training data, "
        "you have no other way to know the current date."
    )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "OBJECT",
            "properties": {
                "timezone": {
                    "type": "STRING",
                    "description": (
                        "IANA timezone name, e.g. 'Asia/Almaty' or 'Europe/Moscow'. "
                        "Omit to use the default timezone."
                    ),
                }
            },
            "required": [],
        }

    async def execute(self, args: dict[str, Any]) -> ToolResult:
        tz_name = str(args.get("timezone") or self.default_timezone)
        try:
            tz = ZoneInfo(tz_name)
        except ZoneInfoNotFoundError:
            return ToolResult(ok=False, text=f"unknown timezone {tz_name!r}")
        now = datetime.now(tz)
        formatted = now.strftime("%A, %d %B %Y, %H:%M")
        return ToolResult(ok=True, text=f"It is currently {formatted} ({tz_name}).")


# Section code -> human-readable project name (RU), from
# ../../humanoid-robot/deploy/config/presenter-kb.yaml, itself generated from
# MinTrans's own Datas/*/BlockInfoView — kept here only for the tool
# description the model reads, not as a source of truth for facts (that
# stays presenter-kb.yaml's job once this prototype grows a knowledge layer).
_SECTION_NAMES_RU: dict[str, str] = {
    "Avto1": "автодорога Кызылорда — Жезказган",
    "Avto2": "автодорога Актобе — Карабутак — Улгайсын",
    "Avto3": "мост через реку Иртыш",
    "Avto4": "обход города Сарыагаш",
    "Avto5": "автодорога Актобе — Улгайсын",
    "JD1": "железная дорога Дарбаза — Мактаарал",
    "JD2": "железная дорога Мойынты — Кызылжар",
    "JD3": "железная дорога Бахты — Аягоз",
    "Aero1": "аэропорт Зайсан",
    "Aero2": "аэропорт Катон-Карагай",
    "Aero3": "аэропорт Кендерли",
    "Aero4": "аэропорт Аркалык",
}

# Category code -> human label (RU), for the tool description. Confirmed via
# `strings` against the real MinTrans/PC/Factories_Data/level0 scene:
# MENU_AVTO/MENU_JD/MENU_AERO exist as real, pipe-addressable category
# overview screens in the compiled app (see app/wall/commands.py's docstring)
# — this is not a guess or a stand-in.
_CATEGORY_LABELS_RU: dict[str, str] = {
    WallCategory.AVTO.value: "автодороги",
    WallCategory.JD.value: "железные дороги",
    WallCategory.AERO.value: "аэропорты",
}

_NAV_ACTIONS = ("main_menu", "next_section", "prev_section", "next_slide", "prev_slide")


@dataclass(frozen=True, slots=True)
class ControlVideoWallTool:
    """Drives the demo video wall (Unity app «Factories», project MinTrans).

    Talks HTTP to a wall-agent (see app/wall/ for the local `sim` instance
    used in this prototype) — never touches the wall application directly,
    mirroring how the real presenter robot's Tool Broker is required to work
    (see ../CLAUDE.md).
    """

    base_url: str
    token: str = ""
    timeout_s: float = 5.0
    name: str = "control_video_wall"
    description: str = (
        "Control the presentation video wall showing infrastructure projects "
        "(highways, railways, airports). Use kind='open_section' with an "
        "exact `section` code to show one specific project. Use "
        "kind='open_category' with `category` to show that category's own "
        "overview screen (e.g. all airports at once) — this is a real "
        "screen in the app, not a substitute. Use kind='navigate' with `nav` "
        "for the main menu or next/previous section/slide — note next/prev "
        "slide only work once a specific section (not a category screen or "
        "the main menu) is open. Never claim a switch succeeded unless this "
        "tool reports it did."
    )

    @property
    def parameters(self) -> dict[str, Any]:
        section_list = ", ".join(f"{code} ({name})" for code, name in _SECTION_NAMES_RU.items())
        category_list = ", ".join(f"{code} ({name})" for code, name in _CATEGORY_LABELS_RU.items())
        return {
            "type": "OBJECT",
            "properties": {
                "kind": {
                    "type": "STRING",
                    "enum": ["open_section", "open_category", "navigate"],
                    "description": (
                        "'open_section' for one project, 'open_category' for a whole "
                        "category's overview screen, 'navigate' otherwise."
                    ),
                },
                "section": {
                    "type": "STRING",
                    "enum": list(_SECTION_NAMES_RU),
                    "description": f"Required for open_section. One of: {section_list}.",
                },
                "category": {
                    "type": "STRING",
                    "enum": list(_CATEGORY_LABELS_RU),
                    "description": f"Required for open_category. One of: {category_list}.",
                },
                "nav": {
                    "type": "STRING",
                    "enum": list(_NAV_ACTIONS),
                    "description": "Required for kind='navigate'.",
                },
            },
            "required": ["kind"],
        }

    async def execute(self, args: dict[str, Any]) -> ToolResult:
        kind = str(args.get("kind", ""))
        if kind == "open_section":
            return await self._open_section(args)
        if kind == "open_category":
            return await self._open_category(args)
        if kind == "navigate":
            return await self._navigate(args)
        return ToolResult(
            ok=False, text=f"unknown kind {kind!r}, expected open_section, open_category or navigate"
        )

    async def _open_section(self, args: dict[str, Any]) -> ToolResult:
        section = args.get("section")
        if not section:
            return ToolResult(ok=False, text="open_section requires 'section'")
        if section not in _SECTION_NAMES_RU:
            return ToolResult(ok=False, text=f"unknown section {section!r}")
        return await self._send(WallCommand(kind=WallCommandKind.OPEN_SECTION, section=section))

    async def _open_category(self, args: dict[str, Any]) -> ToolResult:
        category = args.get("category")
        if not category:
            return ToolResult(ok=False, text="open_category requires 'category'")
        if category not in _CATEGORY_LABELS_RU:
            return ToolResult(ok=False, text=f"unknown category {category!r}")
        return await self._send(WallCommand(kind=WallCommandKind.OPEN_CATEGORY, category=category))

    async def _navigate(self, args: dict[str, Any]) -> ToolResult:
        nav = args.get("nav")
        if nav not in _NAV_ACTIONS:
            return ToolResult(ok=False, text=f"unknown or missing nav action {nav!r}")
        return await self._send(WallCommand(kind=WallCommandKind.NAVIGATE, nav=nav))

    async def _send(self, command: WallCommand) -> ToolResult:
        headers = {"X-Wall-Token": self.token} if self.token else {}
        try:
            async with httpx.AsyncClient(
                base_url=self.base_url, timeout=self.timeout_s, headers=headers
            ) as client:
                response = await client.post(
                    "/wall/command", json=command.model_dump(mode="json")
                )
        except httpx.HTTPError as exc:
            return ToolResult(ok=False, text=f"the video wall agent is unreachable: {exc}")

        if response.status_code != httpx.codes.OK:
            return ToolResult(
                ok=False, text=f"the video wall agent rejected the request (HTTP {response.status_code})"
            )
        payload = response.json()
        outcome = payload.get("outcome")
        detail = payload.get("detail", "")
        if command.section is not None:
            target = command.section.value
        elif command.category is not None:
            target = command.category.value
        else:
            target = command.nav.value if command.nav is not None else ""
        if outcome == WallCommandOutcome.ACCEPTED.value:
            return ToolResult(ok=True, text=f"Wall command applied: {target}.")
        if outcome == WallCommandOutcome.UNREACHABLE.value:
            return ToolResult(ok=False, text="the video wall is not reachable right now")
        return ToolResult(ok=False, text=f"the wall rejected this command: {detail or outcome}")


@dataclass(frozen=True, slots=True)
class SearchKnowledgeTool:
    """RAG over app/knowledge.py's `KnowledgeBase` — drop .txt/.md files into
    the configured directory and this tool finds relevant passages in them.

    Re-indexes (cheaply — see KnowledgeBase.ensure_fresh) on every call, so
    dropping a new file in takes effect on the next question, no restart.
    """

    knowledge: KnowledgeBase
    top_k: int
    name: str = "search_knowledge"
    description: str = (
        "Search the uploaded reference documents for this venue — facts, "
        "procedures, names, numbers, anything specific to this place. Call "
        "this BEFORE answering any question that might be covered by those "
        "documents; your own general knowledge is not about this place. "
        "Returns the most relevant excerpts, or says nothing was found — if "
        "so, tell the person honestly instead of guessing."
    )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "OBJECT",
            "properties": {
                "question": {
                    "type": "STRING",
                    "description": "The question, in the visitor's own words.",
                }
            },
            "required": ["question"],
        }

    async def execute(self, args: dict[str, Any]) -> ToolResult:
        question = str(args.get("question", "")).strip()
        if not question:
            return ToolResult(ok=False, text="no question was given")
        try:
            await self.knowledge.ensure_fresh()
            results = await self.knowledge.search(question, top_k=self.top_k)
        except httpx.HTTPError as exc:
            return ToolResult(ok=False, text=f"knowledge search is unavailable right now: {exc}")
        if not results:
            return ToolResult(
                ok=False, text="nothing in the uploaded documents answers that"
            )
        excerpts = "\n\n".join(f"[{r.source}]\n{r.text}" for r in results)
        return ToolResult(ok=True, text=excerpts)
