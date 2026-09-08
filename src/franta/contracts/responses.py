"""Structured final-response contracts for Codex calls.

Skills carry the detailed artifacts.  Final responses only select the durable
control branch and therefore stay intentionally small.
"""

from __future__ import annotations

from itertools import combinations
from pathlib import Path
from typing import Any

from ..util import atomic_write_text, canonical_json


def _object(properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    if len(required) != len(set(required)) or set(required) != set(properties):
        raise ValueError("strict object schemas must require every declared property exactly once")
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


def _nullable_object(properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    """Return a strict object that may also be JSON null."""

    schema = _object(properties, required)
    schema["type"] = ["object", "null"]
    return schema


def _partial_object(
    properties: dict[str, Any], required: list[str] | None = None
) -> dict[str, Any]:
    """Represent an exact partial mapping as strict closed-object alternatives.

    OpenAI strict schemas require every declared property to be required.  Franta
    update patches intentionally distinguish an omitted field from an empty or
    null field, so enumerate the finite set of legal key subsets instead of
    changing that wire meaning.
    """

    required = list(required or [])
    if len(required) != len(set(required)) or not set(required) <= set(properties):
        raise ValueError("partial-object required keys must be distinct declared properties")
    optional = [key for key in properties if key not in required]
    alternatives: list[dict[str, Any]] = []
    for size in range(len(optional) + 1):
        for selected_optional in combinations(optional, size):
            selected = set(required) | set(selected_optional)
            selected_properties = {
                key: schema for key, schema in properties.items() if key in selected
            }
            alternatives.append(_object(selected_properties, list(selected_properties)))
    return {"anyOf": alternatives}


def _string_array(*, nonempty: bool = False) -> dict[str, Any]:
    schema: dict[str, Any] = {"type": "array", "items": {"type": "string"}}
    if nonempty:
        schema["minItems"] = 1
    return schema


_VALUE_ASSESSMENT = _object(
    {
        "confidence": {"type": "string"},
        "success_gain": {"type": "string"},
        "failure_gain": {"type": "string"},
        "relevance": {"type": "string"},
        "novelty": {"type": "string"},
    },
    ["confidence", "success_gain", "failure_gain", "relevance", "novelty"],
)

_RELATION = _partial_object(
    {
        "relation_type": {"type": "string"},
        "premise_memory_ids": _string_array(nonempty=True),
        "conclusion": {"type": "string"},
        "explanation": {"type": "string"},
        "supporting_fact_ids": _string_array(),
    },
    ["relation_type", "premise_memory_ids", "conclusion", "explanation"],
)

_ROUTE_SET = _partial_object(
    {
        "abstract": {"type": "string"},
        "value_assessment": _VALUE_ASSESSMENT,
        "progress": _string_array(),
        "next_steps": _string_array(),
        "obstacles": _string_array(),
    }
)
_ROUTE_APPEND = _partial_object(
    {
        "progress": _string_array(nonempty=True),
        "next_steps": _string_array(nonempty=True),
        "obstacles": _string_array(nonempty=True),
    }
)
_ROUTE_IDS = _partial_object(
    {
        "related_obligation_ids": _string_array(),
        "active_fact_ids": _string_array(),
        "relevant_memo_ids": _string_array(),
        "relevant_claim_ids": _string_array(),
    }
)
_OBLIGATION_SET = _partial_object(
    {
        "abstract": {"type": "string"},
        "importance": {"type": "string"},
        "partial_progress": _string_array(),
        "relations": {"type": "array", "items": _RELATION},
    }
)
_OBLIGATION_APPEND = _partial_object(
    {
        "partial_progress": _string_array(nonempty=True),
        "relations": {"type": "array", "items": _RELATION, "minItems": 1},
    }
)
_OBLIGATION_IDS = _partial_object({"related_route_ids": _string_array()})

_SYNTHESIZER_DEFS = {
    "route_set": _ROUTE_SET,
    "route_append": _ROUTE_APPEND,
    "route_ids": _ROUTE_IDS,
    "obligation_set": _OBLIGATION_SET,
    "obligation_append": _OBLIGATION_APPEND,
    "obligation_ids": _OBLIGATION_IDS,
}


def _update_patch(kind: str) -> dict[str, Any]:
    if kind == "route":
        set_ref = {"$ref": "#/$defs/route_set"}
        append_ref = {"$ref": "#/$defs/route_append"}
        ids_ref = {"$ref": "#/$defs/route_ids"}
    elif kind == "obligation":
        set_ref = {"$ref": "#/$defs/obligation_set"}
        append_ref = {"$ref": "#/$defs/obligation_append"}
        ids_ref = {"$ref": "#/$defs/obligation_ids"}
    else:  # pragma: no cover - module-owned constant construction
        raise ValueError(f"unsupported update-patch kind: {kind}")
    return _partial_object(
        {
            "target_id": {"type": "string"},
            "expected_base_revision": {"type": "integer", "minimum": 1},
            "set": set_ref,
            "append": append_ref,
            "add_ids": ids_ref,
            "remove_ids": ids_ref,
            "explanation": {"type": "string"},
            "supporting_memory_ids": _string_array(),
        },
        ["target_id", "expected_base_revision", "explanation"],
    )


_INTRODUCED_NOTATION = _object(
    {
        "symbol": {"type": "string"},
        "definition": {"type": "string"},
        "scope": {"type": "string"},
    },
    ["symbol", "definition", "scope"],
)
_EXTERNAL_REFERENCE = _object(
    {
        "source_type": {"type": "string"},
        "authors": {
            "anyOf": [
                {"type": "string"},
                _string_array(nonempty=True),
            ]
        },
        "title": {"type": "string"},
        "stable_identifier_or_url": {"type": "string"},
        "locator": {"type": "string"},
        "exact_result": {"type": "string"},
        "role": {"type": "string"},
    },
    [
        "source_type",
        "authors",
        "title",
        "stable_identifier_or_url",
        "locator",
        "exact_result",
        "role",
    ],
)
_ROOT_RESOLUTION = _nullable_object(
    {
        "target": {"type": "string", "enum": ["ROOT"]},
        "outcome": {"type": "string", "enum": ["proved", "disproved"]},
    },
    ["target", "outcome"],
)
_VERIFIER_ROOT_RESOLUTION = {
    **_ROOT_RESOLUTION,
    "description": (
        "Echo the submitted root-resolution field for either verdict; only a correct "
        "verdict confirms it."
    ),
}


_CATEGORY_MEMBERS = _object(
    {
        "fact": _string_array(),
        "route": _string_array(),
        "memo": _string_array(),
        "claim": _string_array(),
        "obligation": _string_array(),
    },
    ["fact", "route", "memo", "claim", "obligation"],
)

_CATEGORY_DEFINITION_PROPERTIES = {
    "proposal_id": {"type": "string"},
    "name": {"type": "string"},
    "description": {"type": "string"},
    "main_progress": {"type": "string"},
    "current_obstacles": {"type": "string"},
    "members": _CATEGORY_MEMBERS,
}
_CATEGORY_DEFINITION = _object(
    _CATEGORY_DEFINITION_PROPERTIES,
    list(_CATEGORY_DEFINITION_PROPERTIES),
)
_CATEGORY_MUTABLE_TEXT = _partial_object(
    {
        "name": {"type": "string"},
        "description": {"type": "string"},
        "main_progress": {"type": "string"},
        "current_obstacles": {"type": "string"},
    }
)
_CATEGORY_REVISION_ENTRY = _object(
    {
        "category_id": {"type": "string"},
        "category_revision": {"type": "integer", "minimum": 1},
    },
    ["category_id", "category_revision"],
)

_CATEGORY_CREATE = _object(
    {"kind": {"type": "string", "enum": ["create"]}, **_CATEGORY_DEFINITION_PROPERTIES},
    ["kind", *_CATEGORY_DEFINITION_PROPERTIES],
)
_CATEGORY_UPDATE = _object(
    {
        "kind": {"type": "string", "enum": ["update", "revise"]},
        "category_id": {"type": "string"},
        "expected_revision": {"type": "integer", "minimum": 1},
        "set": _CATEGORY_MUTABLE_TEXT,
        "add_members": _CATEGORY_MEMBERS,
        "remove_members": _CATEGORY_MEMBERS,
    },
    [
        "kind",
        "category_id",
        "expected_revision",
        "set",
        "add_members",
        "remove_members",
    ],
)
_CATEGORY_RENAME = _object(
    {
        "kind": {"type": "string", "enum": ["rename"]},
        "category_id": {"type": "string"},
        "expected_revision": {"type": "integer", "minimum": 1},
        "name": {"type": "string"},
    },
    ["kind", "category_id", "expected_revision", "name"],
)
_CATEGORY_MEMBERSHIP = _object(
    {
        "kind": {"type": "string", "enum": ["membership"]},
        "category_id": {"type": "string"},
        "expected_revision": {"type": "integer", "minimum": 1},
        "add_members": _CATEGORY_MEMBERS,
        "remove_members": _CATEGORY_MEMBERS,
    },
    ["kind", "category_id", "expected_revision", "add_members", "remove_members"],
)
_CATEGORY_MERGE = _object(
    {
        "kind": {"type": "string", "enum": ["merge"]},
        "source_category_ids": {
            "type": "array",
            "items": {"type": "string"},
            "minItems": 2,
        },
        "expected_revisions": {
            "type": "array",
            "items": _CATEGORY_REVISION_ENTRY,
            "minItems": 2,
        },
        "result": _CATEGORY_DEFINITION,
    },
    ["kind", "source_category_ids", "expected_revisions", "result"],
)
_CATEGORY_SPLIT = _object(
    {
        "kind": {"type": "string", "enum": ["split"]},
        "source_category_id": {"type": "string"},
        "expected_revision": {"type": "integer", "minimum": 1},
        "results": {
            "type": "array",
            "items": _CATEGORY_DEFINITION,
            "minItems": 2,
        },
    },
    ["kind", "source_category_id", "expected_revision", "results"],
)
_CATEGORY_CHANGE = {
    "anyOf": [
        _CATEGORY_CREATE,
        _CATEGORY_UPDATE,
        _CATEGORY_RENAME,
        _CATEGORY_MEMBERSHIP,
        _CATEGORY_MERGE,
        _CATEGORY_SPLIT,
    ]
}
_CATEGORY_PORTFOLIO = _object(
    {
        "expected_portfolio_revision": {"type": "integer", "minimum": 0},
        "categories": {
            "type": "array",
            "items": _CATEGORY_REVISION_ENTRY,
            "minItems": 1,
        },
        "flattened_members": _CATEGORY_MEMBERS,
        "base_event_id": {"type": ["string", "integer"]},
        "confirmed_through_event_id": {"type": ["string", "integer"]},
        "selection_rationale": {"type": "string"},
        "human_guidance_reference": {"type": ["string", "null"]},
    },
    [
        "expected_portfolio_revision",
        "categories",
        "flattened_members",
        "base_event_id",
        "confirmed_through_event_id",
        "selection_rationale",
        "human_guidance_reference",
    ],
)
_TRIM_PROPOSAL = _nullable_object(
    {
        "expected_state_revision": {"type": ["integer", "null"], "minimum": 0},
        "category_changes": {"type": "array", "items": _CATEGORY_CHANGE},
        "portfolio": {
            "anyOf": [
                {"type": "null"},
                _CATEGORY_PORTFOLIO,
            ]
        },
    },
    ["expected_state_revision", "category_changes", "portfolio"],
)

_VERIFICATION_ERROR = _object(
    {
        "location": {"type": "string"},
        "error": {"type": "string"},
        "suggested_repair": {"type": ["string", "null"]},
    },
    ["location", "error", "suggested_repair"],
)

_MECHANISM_FINGERPRINT = _object(
    {
        "lane_id": {"type": "string", "enum": ["A", "B", "C", "D"]},
        "lane_status": {
            "type": "string",
            "enum": ["finished", "progress", "failed", "interrupted", "missing"],
        },
        "principal_objects": {"type": "string"},
        "representation": {"type": "string"},
        "central_move": {"type": "string"},
        "required_bridge": {"type": "string"},
        "main_obstacle": {"type": "string"},
        "evidence_status": {"type": "string"},
    },
    [
        "lane_id",
        "lane_status",
        "principal_objects",
        "representation",
        "central_move",
        "required_bridge",
        "main_obstacle",
        "evidence_status",
    ],
)
_LANE_COMPARISON = _object(
    {
        "lane_ids": {
            "type": "array",
            "items": {"type": "string", "enum": ["A", "B", "C", "D"]},
            "minItems": 2,
        },
        "classification": {
            "type": "string",
            "enum": ["genuinely_different", "renamed_variant"],
        },
        "explanation": {"type": "string"},
    },
    ["lane_ids", "classification", "explanation"],
)
_SPRINT_FINDING = _object(
    {
        "lane_ids": {
            "type": "array",
            "items": {"type": "string", "enum": ["A", "B", "C", "D"]},
            "minItems": 1,
        },
        "finding": {"type": "string"},
        "evidence_status": {"type": "string"},
    },
    ["lane_ids", "finding", "evidence_status"],
)
_SPRINT_FOLLOW_UP = _object(
    {
        "objective": {"type": "string"},
        "suitable_mode": {
            "type": "string",
            "enum": [
                "research",
                "brainstorm",
                "associate",
                "multi-discipline",
                "reformulate",
                "computation",
                "proof-writer",
            ],
        },
        "source_lane_ids": {
            "type": "array",
            "items": {"type": "string", "enum": ["A", "B", "C", "D"]},
        },
        "reason": {"type": "string"},
    },
    ["objective", "suitable_mode", "source_lane_ids", "reason"],
)


# All production response schemas now use closed wire representations.  Keep
# this exported audit surface so tests detect any future accidental gap.
STRICT_SCHEMA_BLOCKERS: dict[str, str] = {}


SCHEMAS: dict[str, dict[str, Any]] = {
    "main": _object(
        {
            "decision": {
                "type": "string",
                "enum": ["assignments", "wait_for_results", "terminal"],
            },
            "batch_id": {"type": ["string", "null"]},
            "assignment_report_ids": {
                "type": "array",
                "items": {"type": "string"},
            },
            "wait_for_task_ids": {
                "type": "array",
                "items": {"type": "string"},
            },
            "decline_proof_writer": {"type": "boolean"},
        },
        [
            "decision",
            "batch_id",
            "assignment_report_ids",
            "wait_for_task_ids",
            "decline_proof_writer",
        ],
    ),
    "trimmer-review": _object(
        {
            "decision": {"type": "string", "enum": ["no_trim", "trim"]},
            "reason": {"type": "string"},
        },
        ["decision", "reason"],
    ),
    "trimmer": _object(
        {
            "decision": {
                "type": "string",
                "enum": ["commit", "human_guidance", "discovery_sprint"],
            },
            "proposal": _TRIM_PROPOSAL,
            "expected_portfolio_revision": {"type": "integer", "minimum": 0},
            "confirmed_through_event_id": {"type": "integer", "minimum": 0},
            "artifact_operation_id": {"type": ["string", "null"]},
        },
        [
            "decision",
            "proposal",
            "expected_portfolio_revision",
            "confirmed_through_event_id",
            "artifact_operation_id",
        ],
    ),
    "worker": _object(
        {
            "attempt_ended": {"type": "boolean"},
            "final_progress_id": {"type": ["string", "null"]},
        },
        ["attempt_ended", "final_progress_id"],
    ),
    "synthesizer": _object(
        {
            "resolution": {"type": "string", "enum": ["new", "duplicate", "update"]},
            "operation_digest": {"type": "string"},
            "explanation": {"type": "string"},
            "canonical_id": {"type": ["string", "null"]},
            "relied_on": {
                "type": "array",
                "items": _object(
                    {"id": {"type": "string"}, "revision": {"type": ["integer", "null"]}},
                    ["id", "revision"],
                ),
            },
            "patch": {
                "anyOf": [
                    {"type": "null"},
                    _update_patch("route"),
                    _update_patch("obligation"),
                ]
            },
        },
        ["resolution", "operation_digest", "explanation", "canonical_id", "relied_on", "patch"],
    ),
    "verifier": _object(
        {
            "verdict": {"type": "string", "enum": ["correct", "incorrect"]},
            "candidate_id": {"type": "string"},
            "candidate_version": {"type": "integer", "minimum": 1},
            "operation_id": {"type": "string"},
            "bundle_digest": {"type": "string"},
            "verifier_attempt_id": {"type": "string"},
            "predecessor_ids": {"type": "array", "items": {"type": "string"}},
            "introduced_notation": {"type": "array", "items": _INTRODUCED_NOTATION},
            "external_references": {"type": "array", "items": _EXTERNAL_REFERENCE},
            "root_resolution": _VERIFIER_ROOT_RESOLUTION,
            "errors": {"type": "array", "items": _VERIFICATION_ERROR},
        },
        [
            "verdict",
            "candidate_id",
            "candidate_version",
            "operation_id",
            "bundle_digest",
            "verifier_attempt_id",
            "predecessor_ids",
            "introduced_notation",
            "external_references",
            "root_resolution",
            "errors",
        ],
    ),
    "challenge-verifier": _object(
        {
            "challenge_id": {"type": "string"},
            "bundle_digest": {"type": "string"},
            "resolution": {
                "type": "string",
                "enum": ["confirmed_invalid", "challenge_rejected", "inconclusive"],
            },
            "justification": {"type": "string"},
        },
        ["challenge_id", "bundle_digest", "resolution", "justification"],
    ),
    "main-closure-review": _object(
        {
            "outcome": {
                "type": "string",
                "enum": ["finished", "progress", "failed"],
            },
            "summary": _object(
                {"summary": {"type": "string"}},
                ["summary"],
            ),
        },
        ["outcome", "summary"],
    ),
    "summarizer": _object(
        {
            "mechanism_fingerprints": {
                "type": "array",
                "items": _MECHANISM_FINGERPRINT,
                "minItems": 4,
                "maxItems": 4,
            },
            "differences": {"type": "array", "items": _LANE_COMPARISON},
            "shared_bottlenecks": {"type": "array", "items": _SPRINT_FINDING},
            "contradictions": {"type": "array", "items": _SPRINT_FINDING},
            "bridges": {"type": "array", "items": _SPRINT_FINDING},
            "negative_results": {"type": "array", "items": _SPRINT_FINDING},
            "follow_up_tasks": {"type": "array", "items": _SPRINT_FOLLOW_UP},
        },
        [
            "mechanism_fingerprints",
            "differences",
            "shared_bottlenecks",
            "contradictions",
            "bridges",
            "negative_results",
            "follow_up_tasks",
        ],
    ),
}

# Keep shared patch definitions on the schema root so every generated schema
# remains self-contained and references resolve after serialization.
SCHEMAS["synthesizer"]["$defs"] = _SYNTHESIZER_DEFS


def write_schemas(directory: str | Path) -> dict[str, Path]:
    root = Path(directory)
    root.mkdir(parents=True, exist_ok=True)
    written: dict[str, Path] = {}
    for name, schema in SCHEMAS.items():
        path = root / f"{name}.schema.json"
        atomic_write_text(path, canonical_json(schema) + "\n", mode=0o600)
        written[name] = path
    return written


__all__ = [
    "SCHEMAS",
    "STRICT_SCHEMA_BLOCKERS",
    "write_schemas",
]
