"""app/installer/systemd.py's `install_unit` — must rewrite the shipped
unit template's `WorkingDirectory=` to the customer's ACTUAL checkout path
(deploy/realtimevoice-{r1,g1}.service hardcode `%h/realtimevoice-r1` per
DEPLOYMENT.md's documented convention, which a wizard aimed at a non-
technical customer can't assume they followed)."""

from __future__ import annotations

from pathlib import Path

from app.installer import systemd


def test_install_unit_rewrites_working_directory_to_actual_checkout(tmp_path: Path, monkeypatch) -> None:
    unit_dir = tmp_path / "unit-out"
    monkeypatch.setattr(systemd, "_UNIT_DIR", unit_dir)

    template = tmp_path / "realtimevoice-r1.service"
    template.write_text(
        "[Unit]\nDescription=x\n[Service]\nWorkingDirectory=%h/realtimevoice-r1\nExecStart=x\n",
        encoding="utf-8",
    )
    actual_checkout = tmp_path / "wherever-the-customer-put-it"

    systemd.install_unit(template, working_dir=actual_checkout)

    installed = (unit_dir / "realtimevoice-r1.service").read_text(encoding="utf-8")
    assert f"WorkingDirectory={actual_checkout}" in installed
    assert "%h/realtimevoice-r1" not in installed
    assert "ExecStart=x" in installed  # untouched lines survive
