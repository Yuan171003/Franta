"""Discover packaged Advisor skill assets without relying on a host layout."""

from __future__ import annotations

from pathlib import Path


def advisor_assets_root() -> Path:
    """Return the source/package directory containing Advisor skill assets."""

    return Path(__file__).resolve().parent / "skills"


def selection_report_skill_root() -> Path:
    """Return the materializable ``selection-report`` skill directory."""

    path = advisor_assets_root() / "selection-report"
    if not (path / "SKILL.md").is_file():
        raise FileNotFoundError("packaged selection-report skill is unavailable")
    return path


__all__ = ["advisor_assets_root", "selection_report_skill_root"]
