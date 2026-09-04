@echo off
REM Builds wall-agent.exe -- a single-file Windows executable for app/wall,
REM the video-wall control API (see README.md's "Видеостена"). Run this ONCE
REM on ANY Windows machine with uv installed (https://docs.astral.sh/uv/) --
REM a developer's laptop is fine, it does NOT need to be the wall PC itself.
REM The wall PC that runs the resulting .exe needs neither uv nor Python.
REM
REM Output: dist\wall-agent.exe -- copy that one file (plus, optionally,
REM run_wall_agent.bat from this same scripts\ folder) to the wall PC.
REM Re-run this after pulling code changes to app/wall/ to refresh the exe.

setlocal

where uv >nul 2>nul
if errorlevel 1 (
    echo [ERROR] uv not found on PATH. Install it first:
    echo         https://docs.astral.sh/uv/getting-started/installation/
    echo         ^(one-time BUILD-machine requirement only -- the wall PC
    echo         that runs the resulting wall-agent.exe needs neither uv
    echo         nor Python at all^)
    exit /b 1
)

cd /d "%~dp0\.."

echo [1/2] Installing wall-agent's runtime deps + PyInstaller (uv sync)...
uv sync --group build-windows
if errorlevel 1 (
    echo [ERROR] uv sync failed -- see output above.
    exit /b 1
)

echo [2/2] Building wall-agent.exe (onefile)...
REM --collect-all instead of a hand-picked --hidden-import list: uvicorn
REM chooses its event loop / HTTP / websocket implementations via runtime
REM string imports (uvicorn.loops.auto etc.), invisible to PyInstaller's
REM static import analysis. Collecting the whole package is the reliable
REM fix for this well-known FastAPI/uvicorn packaging gotcha, not a
REM hand-maintained hidden-import list that silently rots as versions change.
uv run pyinstaller --onefile --name wall-agent --console ^
    --collect-all uvicorn ^
    --collect-all fastapi ^
    --collect-all pydantic ^
    --paths . ^
    scripts\wall_agent_entry.py

if errorlevel 1 (
    echo [ERROR] PyInstaller build failed -- see output above.
    exit /b 1
)

echo.
echo Done: dist\wall-agent.exe
echo Copy it (and scripts\run_wall_agent.bat, optionally) to the wall PC --
echo no Python/uv needed there. See README.md's "Видеостена — установка на
echo Windows" for how to run it.
endlocal
