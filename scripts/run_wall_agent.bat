@echo off
REM Convenience launcher for wall-agent.exe -- double-click this (or a
REM shortcut to it, e.g. in shell:startup for autostart) instead of typing
REM the command line by hand every time. Keep this .bat in the SAME folder
REM as wall-agent.exe (scripts\build_wall_agent_windows.bat's dist\ output).
REM
REM --driver pipe drives the real Factories.exe via its own named pipe --
REM see README.md's "Видеостена" for what that requires (Factories.exe
REM itself already running on this same PC). Use --driver sim instead only
REM for a quick connectivity smoke test with no real Factories.exe running.
REM
REM Set a shared secret below (must match WALL_AGENT_TOKEN in the robot's
REM .env) if this wall PC's network isn't fully trusted -- leave blank to
REM run with no auth.
set WALL_AGENT_TOKEN=

cd /d "%~dp0"
wall-agent.exe --driver pipe --port 8093 --token "%WALL_AGENT_TOKEN%"
pause
