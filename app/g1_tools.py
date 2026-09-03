"""G1 (security-desk) profile tools, plus a small relay-divergence logger
shared by both profiles' knowledge tools.

Kept separate from app/tools.py (the generic Tool/ToolRegistry protocol +
provider-agnostic tools) so that module doesn't need to import
visit_intake/guard_kb — mirrors app/wall/ already being its own package for
R1-only concerns.
"""

from __future__ import annotations

import difflib
import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.guard_kb import GuardKb
from app.knowledge import KnowledgeBase
from app.tools import ToolResult
from app.visit_intake import VisitIntake

_LOG = logging.getLogger("g1_tools")

# Tools whose result text the model is instructed to relay near-verbatim —
# see log_relay_divergence. control_video_wall/get_current_time results are
# NOT meant to be read back word-for-word, so they're excluded.
RELAY_SENSITIVE_TOOLS = frozenset({"visit_intake", "search_guard_kb", "search_knowledge"})


def log_relay_divergence(
    tool_name: str, tool_text: str, spoken_text: str, *, threshold: float = 0.6
) -> None:
    """Log (never block) when spoken_text looks like a paraphrase, not a
    relay, of tool_text. A cheap heuristic for eyeballing compliance on the
    test bench — verbatim relay is a system_instruction request, not an API
    guarantee (see app/scenarios.py), so this is diagnostics, not
    enforcement. Call with the FULL model turn text, accumulated across all
    Transcript(role="model") fragments up to TurnComplete — output
    transcription can arrive in pieces, and comparing a partial fragment
    against the whole tool text would just produce noise.
    """
    if tool_name not in RELAY_SENSITIVE_TOOLS or not tool_text or not spoken_text:
        return
    ratio = difflib.SequenceMatcher(None, tool_text, spoken_text).ratio()
    if ratio < threshold:
        _LOG.warning(
            "relay_divergence tool=%s ratio=%.2f tool_text=%r spoken_text=%r",
            tool_name,
            ratio,
            tool_text,
            spoken_text,
        )


@dataclass(slots=True)
class VisitJournal:
    """Append-only JSONL log of completed visit cards. Test-bench-simple: no
    DB, no locking beyond one process writing — matches this repo's existing
    "JSON file, no DB" precedent (app/knowledge.py's embeddings cache)."""

    path: Path

    def append(self, card: dict[str, object]) -> None:
        record = {"ts": datetime.now(UTC).isoformat(), **card}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")


@dataclass(slots=True)
class VisitIntakeTool:
    """Drives one VisitIntake state machine per session/connection. All
    ordering/confirmation/reset logic lives in VisitIntake — this class is
    just the Gemini-tool-calling adapter around it."""

    journal: VisitJournal
    _intake: VisitIntake = field(default_factory=VisitIntake, init=False)
    name: str = "visit_intake"
    description: str = (
        "Drive the visitor registration interview — a fixed script, not "
        "your own questions. Call with action='start' when a visitor asks "
        "to register a visit. Call with action='answer' and the visitor's "
        "utterance as `text` for every subsequent reply, including the "
        "final yes/no confirmation and 'отмена' (cancel) at any point. "
        "Always speak back the returned text with minimal changes — it is "
        "the exact next question, the confirmation read-back, or a "
        "cancellation/completion message. Never skip a step, never "
        "confirm on your own, never fix or reinterpret one field yourself."
    )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "OBJECT",
            "properties": {
                "action": {"type": "STRING", "enum": ["start", "answer"]},
                "text": {
                    "type": "STRING",
                    "description": "The visitor's utterance verbatim. Required for action='answer'.",
                },
            },
            "required": ["action"],
        }

    async def execute(self, args: dict[str, Any]) -> ToolResult:
        action = str(args.get("action", ""))
        if action == "start":
            return ToolResult(ok=True, text=self._intake.start())
        if action == "answer":
            reply, card = self._intake.consume(str(args.get("text", "")))
            if card is not None:
                self.journal.append(card)
                _LOG.info("visit_intake.completed full_name=%r", card.get("full_name"))
            return ToolResult(ok=True, text=reply)
        return ToolResult(ok=False, text=f"unknown action {action!r}, expected start or answer")


@dataclass(frozen=True, slots=True)
class SearchGuardKbTool:
    """Room/department lookup + venue FAQ for the security-desk profile.

    Exact match first (GuardKb.lookup — navigation must never be an LLM
    guess), semantic search over knowledge_guard/ as the fallback for
    prose/FAQ questions the deterministic lookup doesn't cover. Reuses the
    same KnowledgeBase machinery as R1's SearchKnowledgeTool — no second RAG
    stack.
    """

    guard_kb: GuardKb
    knowledge: KnowledgeBase
    top_k: int
    name: str = "search_guard_kb"
    description: str = (
        "Look up room/department locations and answer FAQ/venue questions "
        "for the security desk (opening hours, procedure, required "
        "documents, etc.). Call this BEFORE answering any such question — "
        "never guess a room, floor or rule. If nothing is found, say so "
        "honestly and suggest asking the guard."
    )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "OBJECT",
            "properties": {
                "question": {
                    "type": "STRING",
                    "description": "The visitor's question, in their own words.",
                }
            },
            "required": ["question"],
        }

    async def execute(self, args: dict[str, Any]) -> ToolResult:
        question = str(args.get("question", "")).strip()
        if not question:
            return ToolResult(ok=False, text="no question was given")

        exact = self.guard_kb.lookup(question)
        if exact is not None:
            return ToolResult(ok=True, text=exact)

        await self.knowledge.ensure_fresh()
        results = await self.knowledge.search(question, top_k=self.top_k)
        if not results:
            return ToolResult(
                ok=False, text="nothing in the reference material answers that"
            )
        excerpts = "\n\n".join(f"[{r.source}]\n{r.text}" for r in results)
        return ToolResult(ok=True, text=excerpts)
