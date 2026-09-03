"""Operator-panel REST API for web/wall.html and web/guard.html.

Kept separate from the WS voice pipeline (app/main.py's /ws/session) on
purpose: these are the "administrative functions" G1 ТЗ §7 wants gated by
`require_token` — the voice/visitor interaction itself is not, since a real
visitor never logs in.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query

from app.auth import require_token
from app.config import settings
from app.g1_tools import mark_visit_processed, read_visits
from app.wall.commands import WallCommand

router = APIRouter(prefix="/api")


@router.get("/profile")
async def profile() -> dict[str, str]:
    """Lets a static page ask "which robot am I" instead of hardcoding it —
    web/index.html uses this to show only the Видеостена (r1) or Охрана (g1)
    nav link, never both."""
    return {"profile": settings.robot_profile}


@router.get("/visits", dependencies=[Depends(require_token)])
async def list_visits(
    status: str | None = Query(default=None, pattern="^(new|processed)$"),
    limit: int = Query(default=100, ge=1, le=1000),
) -> dict[str, Any]:
    records = read_visits(Path(settings.visit_journal_path), status=status, limit=limit)
    return {"records": records}


@router.post("/visits/{visit_id}/processed", dependencies=[Depends(require_token)])
async def mark_processed(visit_id: str) -> dict[str, Any]:
    ok = mark_visit_processed(Path(settings.visit_journal_path), visit_id)
    if not ok:
        raise HTTPException(status_code=404, detail="visit not found")
    return {"id": visit_id, "status": "processed"}


@router.post("/wall/command", dependencies=[Depends(require_token)])
async def wall_command(command: WallCommand) -> dict[str, Any]:
    headers = {"X-Wall-Token": settings.wall_agent_token} if settings.wall_agent_token else {}
    try:
        async with httpx.AsyncClient(
            base_url=settings.wall_agent_url, timeout=settings.wall_agent_timeout_s, headers=headers
        ) as client:
            response = await client.post("/wall/command", json=command.model_dump(mode="json"))
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"wall agent unreachable: {exc}") from exc
    if response.status_code != httpx.codes.OK:
        raise HTTPException(status_code=502, detail=f"wall agent rejected the request ({response.status_code})")
    return response.json()


@router.get("/wall/health")
async def wall_health() -> dict[str, Any]:
    """No token — this only reveals connectivity, matching the R1 PDF's
    "На связи/Нет связи" chip that's visible without logging in."""
    try:
        async with httpx.AsyncClient(
            base_url=settings.wall_agent_url, timeout=settings.wall_agent_timeout_s
        ) as client:
            response = await client.get("/healthz")
        response.raise_for_status()
        return response.json()
    except httpx.HTTPError:
        return {"status": "unreachable", "driver": ""}
