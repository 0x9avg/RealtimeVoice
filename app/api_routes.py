"""Operator-panel REST API for the web-src/ (React) frontend.

Kept separate from the WS voice pipeline (app/main.py's /ws/session) — same
split as before, just no longer auth-gated (item 4 of the 2026-09-04 fixes
list removed the whole ACCESS_TOKEN mechanism customer-side: this is now an
intentionally open API, not "gated but the gate is usually left open").
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response, StreamingResponse

from app.camera_hub import camera_hub
from app.config import settings
from app.g1_tools import mark_visit_processed, read_visits
from app.wall.commands import WallCommand

router = APIRouter(prefix="/api")

_MJPEG_BOUNDARY = "frame"


def only_on(profile: str) -> Any:
    """Make a route exist only on the robot it belongs to.

    Half of this API is role-specific: a G1 at a security desk has no video
    wall, an R1 in a demo hall keeps no visit journal. Both halves used to
    be served by both profiles, which meant `POST /api/wall/command` on a
    G1 spent the wall-agent timeout trying to reach a machine that was
    never configured, and `GET /api/visits` on an R1 answered `{"records":
    []}` — an answer indistinguishable from "there are no visitors yet",
    for a journal that will never exist there.

    404, not 403: from the operator's side the route genuinely is not part
    of this robot, which is also what makes the frontend's routing simple
    (see web-src/src/App.tsx — it hides the same pair of pages).
    """

    async def guard() -> None:
        if settings.robot_profile != profile:
            raise HTTPException(
                status_code=404,
                detail=(
                    f"этот раздел доступен только на роботе {profile.upper()}; "
                    f"здесь запущен профиль {settings.robot_profile.upper()}"
                ),
            )

    return Depends(guard)


@router.get("/profile")
async def profile() -> dict[str, str]:
    """Lets the frontend ask "which robot am I" instead of hardcoding it.
    Everything role-specific hangs off this one answer: which tab the nav
    shows, which page routes exist at all (web-src/src/App.tsx), and — on
    the server side — which half of this API is reachable (see `only_on`).
    Available on both profiles, obviously: it is the question itself."""
    return {"profile": settings.robot_profile}


@router.get("/visits", dependencies=[only_on("g1")])
async def list_visits(
    status: str | None = Query(default=None, pattern="^(new|processed)$"),
    limit: int = Query(default=100, ge=1, le=1000),
) -> dict[str, Any]:
    records = read_visits(Path(settings.visit_journal_path), status=status, limit=limit)
    return {"records": records}


@router.post("/visits/{visit_id}/processed", dependencies=[only_on("g1")])
async def mark_processed(visit_id: str) -> dict[str, Any]:
    ok = mark_visit_processed(Path(settings.visit_journal_path), visit_id)
    if not ok:
        raise HTTPException(status_code=404, detail="visit not found")
    return {"id": visit_id, "status": "processed"}


@router.post("/wall/command", dependencies=[only_on("r1")])
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


@router.get("/wall/health", dependencies=[only_on("r1")])
async def wall_health() -> dict[str, Any]:
    """Reveals connectivity only, matching the R1 PDF's "На связи/Нет связи" chip."""
    try:
        async with httpx.AsyncClient(
            base_url=settings.wall_agent_url, timeout=settings.wall_agent_timeout_s
        ) as client:
            response = await client.get("/healthz")
        response.raise_for_status()
        return response.json()
    except httpx.HTTPError:
        return {"status": "unreachable", "driver": ""}


def _mjpeg_part(frame: bytes) -> bytes:
    return (
        f"--{_MJPEG_BOUNDARY}\r\nContent-Type: image/jpeg\r\nContent-Length: {len(frame)}\r\n\r\n"
    ).encode() + frame + b"\r\n"


@router.get("/camera/snapshot")
async def camera_snapshot() -> Response:
    frame = await camera_hub.snapshot()
    if frame is None:
        raise HTTPException(status_code=503, detail="no camera frame available")
    return Response(content=frame, media_type="image/jpeg")


@router.get("/camera/stream")
async def camera_stream() -> Response:
    """multipart/x-mixed-replace MJPEG — the browser-native way to show a
    live camera in a plain <img> tag, same as humanoid-robot's camera_mjpeg.py.

    Waits for the first frame (or a definitive "unavailable") via
    camera_hub.snapshot() *before* opening the streaming response — an
    immediately-started StreamingResponse that then closes with zero bytes
    is a 200 as far as the browser's <img> is concerned, so it never fires
    onError; a real 503 up front does.
    """
    first_frame = await camera_hub.snapshot()
    if first_frame is None:
        raise HTTPException(status_code=503, detail="no camera frame available")

    async def multipart() -> Any:
        yield _mjpeg_part(first_frame)
        async for frame in camera_hub.stream():
            yield _mjpeg_part(frame)

    return StreamingResponse(
        multipart(), media_type=f"multipart/x-mixed-replace; boundary={_MJPEG_BOUNDARY}"
    )
