"""Thin wrapper around `systemctl --user` / `loginctl` for the wizard.

All USER-level systemd (`~/.config/systemd/user/`, no sudo) — same
convention as `deploy/realtimevoice-{r1,g1}.service`, see DEPLOYMENT.md §6
for why (no root needed at all, versus system-level units).
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

_UNIT_DIR = Path.home() / ".config" / "systemd" / "user"


async def _run(*args: str) -> tuple[int, str]:
    try:
        proc = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
        )
        out, _ = await proc.communicate()
        return proc.returncode or 0, out.decode("utf-8", errors="replace")
    except FileNotFoundError as exc:
        return 127, str(exc)


async def enable_linger() -> None:
    import getpass

    await _run("loginctl", "enable-linger", getpass.getuser())


def install_unit(template: Path, working_dir: Path) -> None:
    """Copies `template` into ~/.config/systemd/user/, rewriting its
    `WorkingDirectory=` line to the CUSTOMER'S ACTUAL checkout path.

    The checked-in templates (deploy/realtimevoice-{r1,g1}.service) hard-
    code `WorkingDirectory=%h/realtimevoice-r1` per DEPLOYMENT.md's
    documented convention — but a wizard aimed at a non-technical customer
    can't assume they cloned into exactly that directory name. Rewriting
    this one line makes the unit correct regardless of where the checkout
    actually lives, without touching the template file customers following
    the manual procedure in DEPLOYMENT.md still rely on verbatim.
    """
    _UNIT_DIR.mkdir(parents=True, exist_ok=True)
    text = template.read_text(encoding="utf-8")
    text = re.sub(
        r"^WorkingDirectory=.*$", f"WorkingDirectory={working_dir}", text, flags=re.MULTILINE
    )
    (_UNIT_DIR / template.name).write_text(text, encoding="utf-8")


async def daemon_reload() -> None:
    await _run("systemctl", "--user", "daemon-reload")


async def enable_now(unit_name: str) -> tuple[bool, str]:
    code, out = await _run("systemctl", "--user", "enable", "--now", unit_name)
    return code == 0, out


async def action(unit_name: str, verb: str) -> tuple[bool, str]:
    """verb: one of start / stop / restart."""
    code, out = await _run("systemctl", "--user", verb, unit_name)
    return code == 0, out


async def is_installed(unit_name: str) -> bool:
    return (_UNIT_DIR / unit_name).exists()


async def status(unit_name: str) -> dict[str, object]:
    active_code, active_out = await _run("systemctl", "--user", "is-active", unit_name)
    enabled_code, enabled_out = await _run("systemctl", "--user", "is-enabled", unit_name)
    _, detail = await _run("systemctl", "--user", "status", unit_name, "--no-pager", "-n", "5")
    return {
        "active": active_out.strip(),
        "is_active": active_code == 0,
        "enabled": enabled_out.strip(),
        "is_enabled": enabled_code == 0,
        "detail": detail,
    }


async def recent_logs(unit_name: str, lines: int = 60) -> str:
    _, out = await _run("journalctl", "--user", "-u", unit_name, "-n", str(lines), "--no-pager")
    return out
