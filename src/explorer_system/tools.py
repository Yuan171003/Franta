"""Portable Explorer skill names, schemas, and payload normalization.

Staging, capability authorization, and calls into a host memory broker are
host responsibilities.  Explorer owns the closed payload contracts for its
provisional scratch and attempt-summary records, plus the tool schemas that a
host may register with its agent transport.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from typing import Any
import uuid

from .contracts import (
    ExplorerAccessError,
    EXPLORER_SCRATCH_KINDS,
    SCRATCH_ID_RE,
    SUMMARY_ID_RE,
)


EXPLORER_SKILLS = (
    "explorer-search",
    "check-result",
    "portfolio-search",
    "record-scratch",
    "record-summary",
)
EXPLORER_STAGING_SKILLS = frozenset({"record-scratch", "record-summary"})
EXPLORER_WORKER_SKILLS = frozenset(
    {"record-scratch", "record-summary"}
)

SCRATCH_ALLOWED_FIELDS = frozenset(
    {
        "operation_id",
        "record_kind",
        "abstract",
        "content",
        "related_memory_ids",
        "cas_operation_ids",
    }
)
SUMMARY_ALLOWED_FIELDS = frozenset(
    {
        "operation_id",
        "abstract",
        "content",
        "directions_tried",
        "main_progress",
        "main_obstacles",
        "source_scratch_ids",
    }
)
CHECK_RESULT_ALLOWED_FIELDS = frozenset({"kind", "statement"})
PORTFOLIO_SEARCH_ALLOWED_FIELDS = frozenset({"query", "limit"})
PORTFOLIO_FETCH_ALLOWED_FIELDS = frozenset({"portfolio_item_id"})
CHECK_RESULT_KINDS = frozenset({"proved", "disproved", "computed"})
EXPLORER_ACCESS_MODES = frozenset(
    {"check-result", "portfolio", "full-memory"}
)


class ExplorerToolValidationError(ValueError):
    """An Explorer tool payload violates its portable closed contract."""


def explorer_worker_skills(
    *,
    access_mode: str | None = None,
    search_enabled: bool | None = None,
) -> frozenset[str]:
    """Return Explorer-owned skills for one launch-bound access mode.

    ``search_enabled`` is the recovery-only v1 adapter.  New calls must select
    one of the three explicit ``access_mode`` values.
    """

    skills = set(EXPLORER_WORKER_SKILLS)
    if access_mode is not None and search_enabled is not None:
        raise ExplorerToolValidationError(
            "choose access_mode or legacy search_enabled, not both"
        )
    if access_mode is not None:
        if not isinstance(access_mode, str):
            raise ExplorerToolValidationError("access_mode must be text")
        normalized = access_mode.strip().lower().replace("_", "-")
        if normalized not in EXPLORER_ACCESS_MODES:
            raise ExplorerToolValidationError(
                "access_mode must be check-result, portfolio, or full-memory"
            )
        if normalized == "check-result":
            skills.add("check-result")
        elif normalized == "portfolio":
            skills.update({"check-result", "portfolio-search"})
        else:
            skills.add("explorer-search")
    elif search_enabled is not None:
        if not isinstance(search_enabled, bool):
            raise ExplorerToolValidationError("search_enabled must be boolean")
        if search_enabled:
            skills.add("explorer-search")
    else:
        raise ExplorerToolValidationError("access_mode is required")
    return frozenset(skills)


def _bounded_text(value: Any, field: str, *, limit: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ExplorerToolValidationError(f"{field} must be nonempty text")
    if len(value.encode("utf-8")) > limit:
        raise ExplorerToolValidationError(f"{field} exceeds {limit} UTF-8 bytes")
    return value.strip()


def _id_list(value: Any, field: str, *, limit: int = 64) -> list[str]:
    if value is None:
        return []
    if (
        not isinstance(value, list)
        or len(value) > limit
        or not all(
            isinstance(item, str)
            and bool(item)
            and item == item.strip()
            and "/" not in item
            and "\\" not in item
            for item in value
        )
    ):
        raise ExplorerToolValidationError(
            f"{field} must be a path-free list of at most {limit} string IDs"
        )
    if len(set(value)) != len(value):
        raise ExplorerToolValidationError(
            f"{field} must not contain duplicate IDs"
        )
    return list(value)


def _closed_payload(
    payload: Mapping[str, Any],
    *,
    allowed: frozenset[str],
    tool: str,
) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        raise ExplorerToolValidationError(f"{tool} payload must be an object")
    value = dict(payload)
    unknown = set(value) - allowed
    if unknown:
        raise ExplorerToolValidationError(
            f"{tool} contains unsupported fields: "
            + ", ".join(sorted(unknown))
        )
    return value


def normalize_check_result_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Validate one closed proposition-status query.

    Mathematical truth cannot be recognized syntactically.  This contract
    nevertheless rules out lists and open-ended question payloads, while the
    launch prompt and skill require the remaining text to state one strict
    proposition.
    """

    value = _closed_payload(
        payload,
        allowed=CHECK_RESULT_ALLOWED_FIELDS,
        tool="check-result",
    )
    # Import lazily so basic scratch/summary tooling remains lightweight.  The
    # access module owns the single semantic validator used by both the tool
    # transport and the grant-bound broker API.
    from .access import validate_check_result_request

    try:
        kind, statement = validate_check_result_request(
            value.get("kind"), value.get("statement")
        )
    except ExplorerAccessError as exc:
        raise ExplorerToolValidationError(str(exc)) from exc
    return {"kind": kind, "statement": statement}


def normalize_portfolio_search_payload(
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate a search confined to one immutable attempt-2 portfolio."""

    value = _closed_payload(
        payload,
        allowed=PORTFOLIO_SEARCH_ALLOWED_FIELDS,
        tool="portfolio-search",
    )
    query = _bounded_text(value.get("query"), "query", limit=32_768)
    limit = value.get("limit", 10)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 10:
        raise ExplorerToolValidationError(
            "portfolio-search limit must be an integer from 1 through 10"
        )
    return {"query": query, "limit": limit}


def normalize_portfolio_fetch_payload(
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate a fetch by an opaque launch-portfolio item ID."""

    value = _closed_payload(
        payload,
        allowed=PORTFOLIO_FETCH_ALLOWED_FIELDS,
        tool="portfolio-fetch",
    )
    item_id = _bounded_text(
        value.get("portfolio_item_id"),
        "portfolio_item_id",
        limit=512,
    )
    if "/" in item_id or "\\" in item_id:
        raise ExplorerToolValidationError(
            "portfolio_item_id must be a path-free opaque ID"
        )
    return {"portfolio_item_id": item_id}


def _default_record_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4()}"


def _record_id(
    prefix: str,
    factory: Callable[[str], str],
) -> str:
    value = factory(prefix)
    pattern = SCRATCH_ID_RE if prefix == "ES" else SUMMARY_ID_RE
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise ExplorerToolValidationError(
            f"record ID factory returned an invalid {prefix}-* identifier"
        )
    return value


def normalize_scratch_payload(
    payload: Mapping[str, Any],
    *,
    new_record_id: Callable[[str], str] = _default_record_id,
) -> dict[str, Any]:
    """Validate and normalize one provisional scratch payload."""

    value = dict(payload)
    unknown = set(value) - SCRATCH_ALLOWED_FIELDS
    if unknown:
        raise ExplorerToolValidationError(
            "record-scratch contains unsupported fields: "
            + ", ".join(sorted(unknown))
        )
    kind = str(value.get("record_kind") or "idea").strip().lower().replace("_", "-")
    if kind not in EXPLORER_SCRATCH_KINDS:
        raise ExplorerToolValidationError(
            "record-scratch has an invalid record_kind"
        )
    abstract = _bounded_text(value.get("abstract"), "abstract", limit=4096)
    content = _bounded_text(value.get("content"), "content", limit=262_144)
    result = {
        "record_id": _record_id("ES", new_record_id),
        "record_kind": kind,
        "abstract": abstract,
        "content": content,
        "related_memory_ids": _id_list(
            value.get("related_memory_ids"), "related_memory_ids"
        ),
        "cas_operation_ids": _id_list(
            value.get("cas_operation_ids"), "cas_operation_ids"
        ),
    }
    if value.get("operation_id") is not None:
        result["operation_id"] = value["operation_id"]
    return result


def normalize_summary_payload(
    payload: Mapping[str, Any],
    *,
    new_record_id: Callable[[str], str] = _default_record_id,
) -> dict[str, Any]:
    """Validate and normalize one provisional attempt-summary payload."""

    value = dict(payload)
    unknown = set(value) - SUMMARY_ALLOWED_FIELDS
    if unknown:
        raise ExplorerToolValidationError(
            "record-summary contains unsupported fields: "
            + ", ".join(sorted(unknown))
        )
    directions = value.get("directions_tried")
    if (
        not isinstance(directions, list)
        or not directions
        or len(directions) > 64
        or not all(isinstance(item, str) and item.strip() for item in directions)
    ):
        raise ExplorerToolValidationError(
            "directions_tried must be a nonempty list of at most 64 texts"
        )
    sources = _id_list(value.get("source_scratch_ids"), "source_scratch_ids")
    if not sources:
        raise ExplorerToolValidationError(
            "source_scratch_ids must cite at least one accepted ES-* scratch"
        )
    result = {
        "record_id": _record_id("ESUM", new_record_id),
        "abstract": _bounded_text(value.get("abstract"), "abstract", limit=4096),
        "content": _bounded_text(value.get("content"), "content", limit=262_144),
        "directions_tried": [item.strip() for item in directions],
        "main_progress": _bounded_text(
            value.get("main_progress"), "main_progress", limit=32_768
        ),
        "main_obstacles": _bounded_text(
            value.get("main_obstacles"), "main_obstacles", limit=32_768
        ),
        "source_scratch_ids": sources,
    }
    if value.get("operation_id") is not None:
        result["operation_id"] = value["operation_id"]
    return result


def explorer_tool_definitions(
    published_record_types: Iterable[str],
    *,
    host_name: str = "host",
) -> dict[str, dict[str, Any]]:
    """Build Explorer MCP definitions for a host's published record types."""

    record_types = set(published_record_types) | {"scratch", "summary"}
    return {
        "check_result": {
            "name": "check_result",
            "description": (
                "Return at most three closest established results for one strict "
                "mathematical proposition, without exposing source-memory types."
            ),
            "inputSchema": {
                "type": "object",
                "required": ["kind", "statement"],
                "properties": {
                    "kind": {"enum": sorted(CHECK_RESULT_KINDS)},
                    "statement": {"type": "string", "minLength": 1},
                },
                "additionalProperties": False,
            },
        },
        "portfolio_search": {
            "name": "portfolio_search",
            "description": (
                "Search only the immutable launch-bound attempt portfolio; "
                "results expose opaque IDs, abstracts, and main content."
            ),
            "inputSchema": {
                "type": "object",
                "required": ["query"],
                "properties": {
                    "query": {"type": "string", "minLength": 1},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 10},
                },
                "additionalProperties": False,
            },
        },
        "portfolio_fetch": {
            "name": "portfolio_fetch",
            "description": (
                "Fetch one opaque item already authorized by the immutable "
                "launch-bound attempt portfolio."
            ),
            "inputSchema": {
                "type": "object",
                "required": ["portfolio_item_id"],
                "properties": {
                    "portfolio_item_id": {"type": "string", "minLength": 1}
                },
                "additionalProperties": False,
            },
        },
        "explorer_search": {
            "name": "explorer_search",
            "description": (
                f"Search one launch-bound corpus of published {host_name} summaries "
                "and provisional Explorer scratch/summary records."
            ),
            "inputSchema": {
                "type": "object",
                "required": ["query", "record_types"],
                "properties": {
                    "query": {"type": "string", "minLength": 1},
                    "record_types": {
                        "type": "array",
                        "minItems": 1,
                        "items": {"enum": sorted(record_types)},
                    },
                    "limit": {"type": "integer", "minimum": 1, "maximum": 10},
                    "include_inactive": {"type": "boolean"},
                    "include_withdrawn": {"type": "boolean"},
                },
                "additionalProperties": False,
            },
        },
        "explorer_fetch": {
            "name": "explorer_fetch",
            "description": (
                "Fetch one authorized published or provisional Explorer record "
                "after inspecting search summaries."
            ),
            "inputSchema": {
                "type": "object",
                "required": ["record_id"],
                "properties": {
                    "record_id": {"type": "string", "minLength": 1}
                },
                "additionalProperties": False,
            },
        },
        "record_scratch": {
            "name": "record_scratch",
            "description": (
                "Append one provisional Explorer scratch record; this never "
                f"publishes or changes authoritative {host_name} memory."
            ),
            "inputSchema": {
                "type": "object",
                "required": ["payload"],
                "properties": {
                    "payload": {"type": "object", "additionalProperties": True}
                },
                "additionalProperties": False,
            },
        },
        "record_summary": {
            "name": "record_summary",
            "description": (
                "Append the provisional summary required at the end of one "
                "Explorer attempt."
            ),
            "inputSchema": {
                "type": "object",
                "required": ["payload"],
                "properties": {
                    "payload": {"type": "object", "additionalProperties": True}
                },
                "additionalProperties": False,
            },
        },
    }


__all__ = [
    "CHECK_RESULT_ALLOWED_FIELDS",
    "CHECK_RESULT_KINDS",
    "EXPLORER_ACCESS_MODES",
    "EXPLORER_SKILLS",
    "EXPLORER_STAGING_SKILLS",
    "EXPLORER_WORKER_SKILLS",
    "ExplorerToolValidationError",
    "PORTFOLIO_FETCH_ALLOWED_FIELDS",
    "PORTFOLIO_SEARCH_ALLOWED_FIELDS",
    "SCRATCH_ALLOWED_FIELDS",
    "SUMMARY_ALLOWED_FIELDS",
    "explorer_tool_definitions",
    "explorer_worker_skills",
    "normalize_check_result_payload",
    "normalize_portfolio_fetch_payload",
    "normalize_portfolio_search_payload",
    "normalize_scratch_payload",
    "normalize_summary_payload",
]
