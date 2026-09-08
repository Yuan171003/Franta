"""Deterministic Markdown projections of canonical SQLite records.

Projections are disposable read views.  They deliberately contain derived
status and reciprocal links, but never become the source of canonical state.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable, Mapping

from .contracts.canonical import MemoryRecord, MemoryType


def _record_dict(record: MemoryRecord | Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(record, MemoryRecord):
        return record.to_dict()
    return dict(record)


def _text(value: Any) -> str:
    if value is None:
        return "_None._"
    if isinstance(value, str):
        stripped = value.strip()
        return stripped if stripped else "_Empty._"
    return str(value)


def _bullet_list(values: Iterable[Any]) -> str:
    materialized = list(values)
    if not materialized:
        return "- _None_"
    return "\n".join(f"- `{value}`" for value in materialized)


def _json_block(value: Any) -> str:
    return "```json\n" + json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        indent=2,
        separators=(",", ": "),
    ) + "\n```"


def _header(data: Mapping[str, Any]) -> list[str]:
    return [
        f"# {data['id']}",
        "",
        f"- Type: `{data['type']}`",
        f"- Revision: `{data['revision']}`",
        f"- Metadata version: `{data.get('metadata_version', 1)}`",
        f"- Status: `{data['status']}`",
        f"- Created: `{data['created_at']}`",
        f"- Updated: `{data['updated_at']}`",
    ]


def _section(lines: list[str], title: str, body: str) -> None:
    lines.extend(["", f"## {title}", "", body])


def _render_fact(data: Mapping[str, Any], lines: list[str]) -> None:
    _section(lines, "Abstract", _text(data["abstract"]))
    _section(lines, "Statement", _text(data["statement"]))
    _section(lines, "Proof", _text(data["proof"]))
    _section(
        lines,
        "Predecessor facts",
        _bullet_list(sorted(data.get("predecessor_fact_ids", []))),
    )
    _section(lines, "Originating task", f"`{data['originating_task_id']}`")
    _section(
        lines,
        "Foundation policy version",
        f"`{data['foundation_policy_version']}`",
    )
    _section(lines, "Introduced notation", _json_block(data.get("introduced_notation", [])))
    _section(lines, "External references", _json_block(data.get("external_references", [])))
    _section(lines, "Root resolution", _json_block(data.get("root_resolution")))
    _section(lines, "Keywords", _bullet_list(sorted(data.get("keywords", []))))
    _section(
        lines,
        "Related routes",
        _bullet_list(sorted(data.get("related_route_ids", []))),
    )


def _render_route(data: Mapping[str, Any], lines: list[str]) -> None:
    _section(lines, "Abstract", _text(data["abstract"]))
    _section(lines, "Strategy", _text(data["strategy_description"]))
    _section(lines, "Qualitative value", _json_block(data["value_assessment"]))
    _section(lines, "Progress", _bullet_list(data.get("progress", [])))
    _section(lines, "Most promising next steps", _bullet_list(data.get("next_steps", [])))
    _section(lines, "Main obstacles", _bullet_list(data.get("obstacles", [])))
    for title, key in (
        ("Related obligations", "related_obligation_ids"),
        ("Active verified facts", "active_fact_ids"),
        ("Relevant memos", "relevant_memo_ids"),
        ("Relevant claims", "relevant_claim_ids"),
    ):
        _section(lines, title, _bullet_list(sorted(data.get(key, []))))
    _section(lines, "Task history", _bullet_list(data.get("task_history_ids", [])))


def _render_memo_or_claim(data: Mapping[str, Any], lines: list[str]) -> None:
    _section(lines, "Abstract", _text(data["abstract"]))
    if data["type"] == MemoryType.MEMO.value:
        _section(lines, "Genre", f"`{data['genre']}`")
    _section(lines, "Content", _text(data["content"]))
    _section(
        lines,
        "Related routes",
        _bullet_list(sorted(data.get("related_route_ids", []))),
    )


def _render_obligation(data: Mapping[str, Any], lines: list[str]) -> None:
    _section(lines, "Abstract", _text(data["abstract"]))
    _section(lines, "Statement", _text(data["statement"]))
    _section(lines, "Importance", _text(data["importance"]))
    _section(
        lines,
        "Predecessor facts",
        _bullet_list(sorted(data.get("predecessor_fact_ids", []))),
    )
    _section(lines, "Partial progress", _bullet_list(data.get("partial_progress", [])))
    _section(
        lines,
        "Related routes",
        _bullet_list(sorted(data.get("related_route_ids", []))),
    )
    _section(lines, "Relations", _json_block(data.get("relations", [])))


def _render_task(data: Mapping[str, Any], lines: list[str]) -> None:
    _section(lines, "Final status", f"`{data['final_status']}`")
    _section(lines, "Final summary", _text(data["final_summary"]))
    _section(lines, "Assignment record", _json_block(data["assign_record"]))
    _section(lines, "Artifact references", _json_block(data.get("artifact_references", [])))
    _section(
        lines,
        "Computation IDs",
        _bullet_list(sorted(data.get("computation_ids", []))),
    )


def _render_computation(data: Mapping[str, Any], lines: list[str]) -> None:
    _section(lines, "Abstract", _text(data["abstract"]))
    _section(lines, "Task", f"`{data['task_id']}`")
    _section(lines, "Computed object and assumptions", _text(data["description"]))
    _section(lines, "Assumptions", _text(data.get("assumptions", "")))
    _section(lines, "Exact input", "```text\n" + data["exact_input"].rstrip() + "\n```")
    _section(lines, "Software", _json_block(data["software"]))
    _section(lines, "Environment", _json_block(data.get("environment_versions", {})))
    _section(lines, "Random seed", _text(data.get("random_seed")))
    _section(lines, "Output", _json_block(data.get("output")))
    _section(lines, "Output artifact", _json_block(data.get("output_artifact")))
    _section(lines, "Exit status", _text(data["exit_status"]))
    _section(lines, "Error output", _text(data.get("error_output")))
    _section(lines, "Worker interpretation", _text(data["interpretation"]))
    _section(lines, "Related memories", _json_block(data.get("related_memory_ids", {})))
    _section(
        lines,
        "Fact-candidate operation IDs",
        _bullet_list(sorted(data.get("fact_candidate_operation_ids", []))),
    )


def render_record(record: MemoryRecord | Mapping[str, Any]) -> str:
    """Return a byte-for-byte deterministic Markdown projection."""

    data = _record_dict(record)
    lines = _header(data)
    kind = MemoryType(data["type"])
    if kind is MemoryType.FACT:
        _render_fact(data, lines)
    elif kind is MemoryType.ROUTE:
        _render_route(data, lines)
    elif kind in (MemoryType.MEMO, MemoryType.CLAIM):
        _render_memo_or_claim(data, lines)
    elif kind is MemoryType.OBLIGATION:
        _render_obligation(data, lines)
    elif kind is MemoryType.TASK:
        _render_task(data, lines)
    elif kind is MemoryType.COMPUTATION:
        _render_computation(data, lines)
    else:  # pragma: no cover - the Enum makes this unreachable.
        raise ValueError(f"unsupported memory type: {kind}")
    if data.get("pending_references"):
        _section(
            lines,
            "Pending typed references",
            _json_block(data["pending_references"]),
        )
    return "\n".join(lines).rstrip() + "\n"


def projection_relative_path(record: MemoryRecord | Mapping[str, Any]) -> Path:
    data = _record_dict(record)
    return Path(f"{data['type']}s") / f"{data['id']}.md"
