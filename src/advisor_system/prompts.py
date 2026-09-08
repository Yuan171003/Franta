"""Advisor prompts, response schemas, and transport-neutral launch specs."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any, Literal, Mapping

from .settings import AdvisorSettings, must_resume_session


PROPOSAL_RESPONSE_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "advisor-proposal-response",
    "type": "object",
    "additionalProperties": False,
    "required": [
        "stage",
        "call_ended",
        "selection_report_id",
        "selection_report_digest",
        "feedback_request_id",
        "report_path",
    ],
    "properties": {
        "stage": {"const": "proposal"},
        "call_ended": {"const": True},
        "selection_report_id": {"type": "string", "minLength": 1},
        "selection_report_digest": {
            "type": "string",
            "pattern": "^[0-9a-f]{64}$",
        },
        "feedback_request_id": {"type": "string", "minLength": 1},
        "report_path": {"type": "string", "minLength": 1},
    },
}

FINALIZE_RESPONSE_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "advisor-finalize-response",
    "type": "object",
    "additionalProperties": False,
    "required": [
        "stage",
        "call_ended",
        "selection_report_id",
        "selection_report_digest",
        "feedback_id",
        "feedback_digest",
        "selected_subproblems",
    ],
    "properties": {
        "stage": {"const": "finalize"},
        "call_ended": {"const": True},
        "selection_report_id": {"type": "string", "minLength": 1},
        "selection_report_digest": {
            "type": "string",
            "pattern": "^[0-9a-f]{64}$",
        },
        "feedback_id": {"type": "string", "minLength": 1},
        "feedback_digest": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
        "selected_subproblems": {
            "type": "array",
            "minItems": 1,
            "maxItems": 2,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "source",
                    "obligation_id",
                    "statement",
                    "human_choice_index",
                ],
                "properties": {
                    "source": {"enum": ["listed", "human_override"]},
                    "obligation_id": {"type": ["string", "null"]},
                    "statement": {"type": "string", "minLength": 1},
                    "human_choice_index": {"type": "integer", "minimum": 1, "maximum": 2},
                },
            },
        },
    },
}

SELECTION_REPORT_PAYLOAD_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "selection-report-payload",
    "type": "object",
    "additionalProperties": False,
    "required": [
        "selection_report_id",
        "feedback_request_id",
        "advisor_index",
        "source_cycle",
        "target_cycle",
        "original_problem_digest",
        "memory_snapshot_id",
        "memory_snapshot_digest",
        "previous_assignments_digest",
        "obligations",
        "human_question",
        "report_path",
    ],
    "properties": {
        "selection_report_id": {"type": "string", "minLength": 1},
        "feedback_request_id": {"type": "string", "minLength": 1},
        "advisor_index": {"type": "integer", "minimum": 1},
        "source_cycle": {"type": "integer", "minimum": 1},
        "target_cycle": {"type": "integer", "minimum": 2},
        "original_problem_digest": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
        "memory_snapshot_id": {"type": "string", "minLength": 1},
        "memory_snapshot_digest": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
        "previous_assignments_digest": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
        "human_question": {"type": "string", "minLength": 1},
        "report_path": {"type": "string", "minLength": 1},
        "obligations": {
            "type": "array",
            "minItems": 5,
            "maxItems": 5,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "obligation_id",
                    "rank",
                    "title",
                    "statement",
                    "importance",
                    "landscape_change",
                    "relationship_to_root",
                    "novelty",
                    "previous_assignment_ids",
                    "repeat_justification",
                    "breakthrough_evidence_ids",
                ],
                "properties": {
                    "obligation_id": {"type": "string", "minLength": 1},
                    "rank": {"type": "integer", "minimum": 1, "maximum": 5},
                    "title": {"type": "string", "minLength": 1},
                    "statement": {"type": "string", "minLength": 1},
                    "importance": {"type": "string", "minLength": 1},
                    "landscape_change": {"type": "string", "minLength": 1},
                    "relationship_to_root": {"type": "string", "minLength": 1},
                    "novelty": {"type": "string", "minLength": 1},
                    "previous_assignment_ids": {
                        "type": "array",
                        "items": {"type": "string", "minLength": 1},
                        "uniqueItems": True,
                    },
                    "repeat_justification": {"type": ["string", "null"]},
                    "breakthrough_evidence_ids": {
                        "type": "array",
                        "items": {"type": "string", "minLength": 1},
                        "uniqueItems": True,
                    },
                },
            },
        },
    },
}

RESPONSE_SCHEMAS: Mapping[str, Mapping[str, Any]] = {
    "advisor-proposal-response": PROPOSAL_RESPONSE_SCHEMA,
    "advisor-finalize-response": FINALIZE_RESPONSE_SCHEMA,
}


@dataclass(frozen=True)
class AdvisorLaunchSpec:
    """Everything a host needs to launch or resume one Advisor call."""

    role: Literal["advisor"]
    stage: Literal["proposal", "finalize"]
    advisor_index: int
    prompt: str
    response_schema_name: str
    response_schema: Mapping[str, Any]
    model: str
    reasoning_effort: str
    session_key: str
    resume_required: bool
    skill_names: tuple[str, ...]
    memory_access_profile: str


def proposal_prompt(*, context_path: str = "input/context.json") -> str:
    """Return the first-call instruction; success deliberately ends the call."""

    return f"""You are the persistent Advisor for this mathematical research project.

Read the immutable Advisor context at `{context_path}`. Inspect every memory in the complete host-authenticated read-only snapshot at the snapshot's `relative_path`; use the launch-bound task-search interface when a selected task artifact is needed. This is the same snapshot and task-access profile granted to the host's main research agent. Also read every previous problem assignment in the context. The host-authenticated `breakthrough_evidence_freshness` field identifies records that were created or revision-advanced after prior assignments; use it whenever claiming the exceptional-repeat rule below.

Propose exactly five ranked obligations that are *most worth trying*, in rank order 1 through 5. Choose the obligations according the following rules:
    - Each obligation must identify the nearest unresolved bottleneck on a high-leverage route and contain exactly one primary mathematical unknown. It *must not* package several mathematical problems together into a single obligation. 
    - Each obligation should be an unresolved important bottleneck, and if solved, it would make solving the ROOT significantly easier.
    - Penalize a lineage that has been studied actively and successively by Franta, or keeps producing new gadget, analogy, reformulation, or additional prerequisite, but still makes *no decisive progress*. However, do not penalize it if it does make truly significant progress
    - An obligation should not merely restate the original problem. 
    - Some obligations should provide an innovative and uncommon way to solve the ROOT, instead of following the most natural ways.
    - Do not reuse a previously assigned subproblem unless it remains really important and the memory snapshot contains a breakthrough that now makes it *significantly easier*; when reusing one, cite the previous assignment and the breakthrough evidence and explain the exception.
    - Penalize an obligation that is a decomposition of a previously assigned subproblem, unless it is truly important for solving the ROOT itself.
Call the launch-bound `selection_report` skill exactly once. It must write a clear human-facing mathematical report and persist the structured five-obligation artifact bound to this context. Return only the strict `advisor-proposal-response` object naming that artifact and its feedback request. Set `call_ended` to true, then end this call. Do not choose subproblems, create a problem assignment, or continue until the scheduler supplies binding human feedback."""


def finalize_prompt(*, context_path: str = "input/context.json") -> str:
    """Return the resumed-call instruction that follows human feedback exactly."""

    return f"""Resume the existing persistent Advisor session; do not start a new Advisor. Read the immutable finalization context at `{context_path}`, including the accepted selection report and its binding structured human feedback.

Follow the human guidance strictly. Select exactly the one or two choices specified by the human, in the same order. The structured choices are the complete binding authority for the assigned mathematics; the accompanying `instructions` are explanatory context and cannot replace or contradict them. A listed choice must reproduce the corresponding obligation statement exactly. A custom choice is authoritative even though it was absent from the five-obligation report and must be reproduced exactly. Do not substitute, improve, merge, reinterpret, or add a subproblem.

Return only the strict `advisor-finalize-response` object, bound to the report and feedback digests, and set `call_ended` to true. The host will validate the response and deterministically render the immutable problem assignment; do not write or mutate the original problem."""


def build_launch_spec(
    *,
    stage: Literal["proposal", "finalize"],
    advisor_index: int,
    settings: AdvisorSettings | None = None,
    context_path: str = "input/context.json",
) -> AdvisorLaunchSpec:
    """Build a transport-neutral launch contract for either Advisor call."""

    settings = settings or AdvisorSettings()
    settings.validate()
    if stage == "proposal":
        prompt = proposal_prompt(context_path=context_path)
        schema_name = "advisor-proposal-response"
        skill_names = ("task-search", "selection-report")
    elif stage == "finalize":
        prompt = finalize_prompt(context_path=context_path)
        schema_name = "advisor-finalize-response"
        skill_names = ("task-search",)
    else:
        raise ValueError(f"unknown Advisor stage: {stage!r}")
    return AdvisorLaunchSpec(
        role="advisor",
        stage=stage,
        advisor_index=advisor_index,
        prompt=prompt,
        response_schema_name=schema_name,
        response_schema=RESPONSE_SCHEMAS[schema_name],
        model=settings.model,
        reasoning_effort=settings.reasoning_effort,
        session_key=settings.session_key,
        resume_required=must_resume_session(advisor_index=advisor_index, stage=stage),
        skill_names=skill_names,
        memory_access_profile=settings.memory_access_profile,
    )


def write_response_schemas(directory: Path) -> tuple[Path, Path]:
    """Materialize strict schemas for a host transport that requires files."""

    directory.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    for name, schema in RESPONSE_SCHEMAS.items():
        path = directory / f"{name}.schema.json"
        path.write_text(
            json.dumps(schema, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        paths.append(path)
    return paths[0], paths[1]


__all__ = [
    "AdvisorLaunchSpec",
    "FINALIZE_RESPONSE_SCHEMA",
    "PROPOSAL_RESPONSE_SCHEMA",
    "RESPONSE_SCHEMAS",
    "SELECTION_REPORT_PAYLOAD_SCHEMA",
    "build_launch_spec",
    "finalize_prompt",
    "proposal_prompt",
    "write_response_schemas",
]
