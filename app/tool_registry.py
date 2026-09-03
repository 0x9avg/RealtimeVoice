"""Single place that decides which tools a session gets, based on
settings.robot_profile. Used by both app/main.py (browser WS sessions) and
app/robot_main.py (headless on-robot runner) — previously each defined its
own near-identical build_tool_registry(); this is the de-duplication point.
"""

from __future__ import annotations

from pathlib import Path

from app.config import Settings
from app.g1_tools import SearchGuardKbTool, VisitIntakeTool, VisitJournal
from app.guard_kb import guard_kb
from app.knowledge import knowledge_base
from app.tools import ControlVideoWallTool, GetCurrentTimeTool, SearchKnowledgeTool, ToolRegistry


def build_tool_registry(settings: Settings) -> ToolRegistry:
    """Assemble the tool set for settings.robot_profile ("r1" or "g1")."""
    registry = ToolRegistry()
    registry.register(GetCurrentTimeTool(default_timezone=settings.default_timezone))

    if settings.robot_profile == "r1":
        registry.register(
            ControlVideoWallTool(
                base_url=settings.wall_agent_url,
                token=settings.wall_agent_token,
                timeout_s=settings.wall_agent_timeout_s,
            )
        )
        registry.register(
            SearchKnowledgeTool(knowledge=knowledge_base, top_k=settings.knowledge_top_k)
        )
    elif settings.robot_profile == "g1":
        registry.register(
            VisitIntakeTool(journal=VisitJournal(path=Path(settings.visit_journal_path)))
        )
        registry.register(
            SearchGuardKbTool(
                guard_kb=guard_kb, knowledge=knowledge_base, top_k=settings.knowledge_top_k
            )
        )
    else:  # pragma: no cover — Settings.robot_profile is a validated Literal
        msg = f"unknown robot_profile {settings.robot_profile!r}"
        raise ValueError(msg)

    return registry
