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
