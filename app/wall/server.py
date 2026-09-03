"""HTTP server exposing a `WallDriver` on the local network.

Same route shape as `../../humanoid-robot/apps/wall-agent` (`POST
/wall/command`, `GET /wall/state`, `GET /healthz`, optional `X-Wall-Token`),
built on FastAPI instead of that repo's stdlib `http.server` — this
project already depends on FastAPI for the voice bridge, so reusing it
here avoids a second HTTP stack for no benefit (that repo's choice of stdlib
was about a dependency-free Windows install, which doesn't apply to this
Linux dev simulator).
"""

from __future__ import annotations

from typing import Annotated

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, ConfigDict

from app.wall.commands import WallCommand, WallCommandResult
from app.wall.drivers import WallDriver


class WallStateResponse(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    screen: str
    exact: bool


class HealthResponse(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    status: str
    driver: str


def build_app(driver: WallDriver, *, token: str = "") -> FastAPI:
    app = FastAPI(title=f"wall-agent ({driver.name})")

    def _check_token(x_wall_token: Annotated[str, Header()] = "") -> None:
        if token and x_wall_token != token:
            raise HTTPException(status_code=401, detail="unauthorized")

    @app.get("/healthz", response_model=HealthResponse)
    def healthz(x_wall_token: Annotated[str, Header()] = "") -> HealthResponse:
        _check_token(x_wall_token)
        return HealthResponse(status="ok", driver=driver.name)

    @app.get("/wall/state")
    def wall_state(x_wall_token: Annotated[str, Header()] = "") -> dict[str, object]:
        _check_token(x_wall_token)
        return driver.state()

    @app.post("/wall/command", response_model=WallCommandResult)
    def wall_command(
        command: WallCommand, x_wall_token: Annotated[str, Header()] = ""
    ) -> WallCommandResult:
        _check_token(x_wall_token)
        return driver.execute(command)

    return app
