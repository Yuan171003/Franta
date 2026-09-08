"""Deterministic, rebuildable projections for Block 9 artifacts.

These renderers do not read or mutate canonical or scheduler state.  Their
Markdown output is a projection of an already composed category or portfolio
snapshot and never becomes mathematical evidence.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence


def _member_lines(entries: Sequence[Mapping[str, Any]]) -> str:
    if not entries:
        return "- _None_"
    lines: list[str] = []
    for entry in entries:
        status = str(entry["status"])
        suffix = "" if status == "active" else f" — **{status}**"
        lines.append(f"- `{entry['id']}`{suffix}")
    return "\n".join(lines)


def render_category(category: Mapping[str, Any]) -> str:
    """Render one composed category deterministically."""

    lines = [
        f"# {category['id']}: {category['name']}",
        "",
        f"- Revision: `{category['revision']}`",
        f"- Status: `{category['status']}`",
        f"- Created: `{category['created_at']}`",
        f"- Updated: `{category['updated_at']}`",
        "",
        "## Description",
        "",
        category["description"] or "_None._",
        "",
        "## Main progress",
        "",
        category["main_progress"] or "_None._",
        "",
        "## Current obstacles",
        "",
        category["current_obstacles"] or "_None._",
    ]
    if category.get("superseded_by"):
        lines.extend(
            [
                "",
                "## Superseded by",
                "",
                *[f"- `{item}`" for item in sorted(category["superseded_by"])],
            ]
        )
    entries = category.get("member_entries", {})
    lines.extend(["", "## Members"])
    for key, title in (
        ("fact", "Facts"),
        ("route", "Routes"),
        ("memo", "Memos"),
        ("claim", "Claims"),
        ("obligation", "Obligations"),
    ):
        lines.extend(["", f"### {title}", "", _member_lines(entries.get(key, []))])
    return "\n".join(lines).rstrip() + "\n"


def render_portfolio(snapshot: Mapping[str, Any]) -> str:
    """Render an exact category-portfolio snapshot with current overlays."""

    lines = [
        f"# Category portfolio {snapshot['revision']}",
        "",
        f"- Snapshot ID: `{snapshot['snapshot_id']}`",
        f"- Created: `{snapshot['created_at']}`",
        f"- Base event: `{snapshot['base_event_id']}`",
        f"- Confirmed through event: `{snapshot['confirmed_through_event_id']}`",
        "",
        "## Exact category revisions",
        "",
    ]
    for category in snapshot["categories"]:
        status = category.get("status", "active")
        suffix = "" if status == "active" else f" — **{status}**"
        lines.append(
            f"- `{category['category_id']}` at revision "
            f"`{category['category_revision']}`{suffix}"
        )
    lines.extend(
        [
            "",
            "## Selection rationale",
            "",
            snapshot["selection_rationale"],
        ]
    )
    if snapshot.get("human_guidance_reference"):
        lines.extend(
            [
                "",
                "## Human guidance reference",
                "",
                f"`{snapshot['human_guidance_reference']}`",
            ]
        )
    entries = snapshot.get("flattened_member_entries", {})
    lines.extend(["", "## Flattened typed members"])
    for key, title in (
        ("fact", "Facts"),
        ("route", "Routes"),
        ("memo", "Memos"),
        ("claim", "Claims"),
        ("obligation", "Obligations"),
    ):
        lines.extend(["", f"### {title}", "", _member_lines(entries.get(key, []))])
    return "\n".join(lines).rstrip() + "\n"


__all__ = ["render_category", "render_portfolio"]
