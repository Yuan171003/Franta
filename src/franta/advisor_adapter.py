"""The single Franta integration seam for the portable Advisor block.

The sibling :mod:`advisor_system` package owns the Advisor lifecycle, prompts,
schemas, mathematical contracts, report rendering contract, and two-call
program.  Franta supplies persistence, memory snapshots, model transport,
trusted skill receipts, human input, and the handoff into the next research
cycle only through this module.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from advisor_system import (
    AdvisorCycleContext,
    AdvisorFinalization,
    AdvisorMemorySnapshot,
    AdvisorProgram,
    AdvisorSettings as PortableAdvisorSettings,
    AdvisorSettingsError,
    FeedbackChoice,
    HumanFeedback,
    ProblemAssignment,
    SelectionReport,
    build_breakthrough_evidence_freshness,
    render_selection_report_markdown,
    validate_breakthrough_evidence_freshness,
)
from advisor_system.contracts import digest_value
from advisor_system.assets import advisor_assets_root
from advisor_system.prompts import AdvisorLaunchSpec, build_launch_spec, write_response_schemas
from advisor_system.state import (
    AdvisorTransition,
    accept_selection_report,
    begin_finalize,
    bind_advisor_session,
    bind_human_feedback,
    commit_problem_assignment,
    current_status,
    initialize_advisor_state,
    open_advisor_round,
)

from .prompts import ModelConfig


ADVISOR_CALL_KINDS = frozenset({"advisor-proposal", "advisor-finalize"})


@dataclass(frozen=True)
class AdvisorAgentCallSpec:
    """Franta transport representation of one portable Advisor launch."""

    prompt: str
    schema_name: str
    model_config: ModelConfig
    session_key: str
    resume_required: bool


def advisor_agent_call_spec(
    *,
    stage: str,
    advisor_index: int,
    settings: Mapping[str, Any],
) -> AdvisorAgentCallSpec:
    """Adapt a portable launch spec without moving Advisor policy into Franta."""

    portable = build_launch_spec(
        stage=stage,  # type: ignore[arg-type]
        advisor_index=advisor_index,
        settings=PortableAdvisorSettings(**dict(settings)),
        context_path="input/context.json",
    )
    return AdvisorAgentCallSpec(
        prompt=portable.prompt,
        schema_name=portable.response_schema_name,
        model_config=ModelConfig(
            model=portable.model,
            reasoning_effort=portable.reasoning_effort,
        ),
        session_key=portable.session_key,
        resume_required=portable.resume_required,
    )


def is_advisor_call_kind(kind: str) -> bool:
    return kind in ADVISOR_CALL_KINDS


def write_advisor_schemas(directory: str | Path) -> tuple[Path, Path]:
    """Install the portable response schemas into one Franta project."""

    return write_response_schemas(Path(directory))


def advisor_skill_source() -> Path:
    """Return the portable block's packaged skill directory."""

    return advisor_assets_root()


def new_advisor_state(*, session_key: str) -> dict[str, Any]:
    return initialize_advisor_state(session_key=session_key)


def advisor_status(state: Mapping[str, Any]) -> str:
    return current_status(state)


def bind_session_transition(
    state: Mapping[str, Any], *, session_id: str
) -> AdvisorTransition:
    return bind_advisor_session(state, session_id=session_id)


def open_round_transition(
    state: Mapping[str, Any],
    context: Mapping[str, Any],
    *,
    proposal_call_id: str,
) -> AdvisorTransition:
    return open_advisor_round(
        state,
        AdvisorCycleContext.from_dict(context),
        proposal_call_id=proposal_call_id,
    )


def accept_report_transition(
    state: Mapping[str, Any],
    *,
    proposal_call_id: str,
    report: Mapping[str, Any],
) -> AdvisorTransition:
    return accept_selection_report(
        state,
        proposal_call_id=proposal_call_id,
        report=SelectionReport.from_dict(report),
    )


def bind_feedback_transition(
    state: Mapping[str, Any], feedback: Mapping[str, Any]
) -> AdvisorTransition:
    return bind_human_feedback(state, HumanFeedback.from_dict(feedback))


def begin_finalize_transition(
    state: Mapping[str, Any], *, finalize_call_id: str
) -> AdvisorTransition:
    return begin_finalize(state, finalize_call_id=finalize_call_id)


def commit_assignment_transition(
    state: Mapping[str, Any],
    *,
    finalize_call_id: str,
    assignment_id: str,
    finalization: Mapping[str, Any],
) -> AdvisorTransition:
    return commit_problem_assignment(
        state,
        finalize_call_id=finalize_call_id,
        assignment_id=assignment_id,
        finalization=AdvisorFinalization.from_dict(finalization),
    )


def advisor_context(
    *,
    advisor_index: int,
    original_problem: str,
    memory_snapshot: Mapping[str, Any],
    previous_assignments: list[Mapping[str, Any]],
) -> AdvisorCycleContext:
    """Build the portable round input from authenticated Franta descriptors."""

    return AdvisorCycleContext(
        advisor_index=advisor_index,
        source_cycle=advisor_index,
        target_cycle=advisor_index + 1,
        original_problem=original_problem,
        memory_snapshot=AdvisorMemorySnapshot.from_dict(memory_snapshot),
        previous_assignments=tuple(
            ProblemAssignment.from_dict(item) for item in previous_assignments
        ),
    )


def advisor_breakthrough_evidence_freshness(
    context: AdvisorCycleContext,
    *,
    current_revisions: Mapping[str, Any],
    previous_revisions: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    """Build the portable freshness proof from host-authenticated catalogs."""

    return build_breakthrough_evidence_freshness(
        context,
        current_revisions=current_revisions,
        previous_revisions=previous_revisions,
    )


def validate_advisor_breakthrough_evidence(
    report: SelectionReport,
    context: AdvisorCycleContext,
    *,
    current_revisions: Mapping[str, Any],
    freshness: Mapping[str, Any],
) -> None:
    """Check that every repeated obligation cites post-assignment evidence."""

    validate_breakthrough_evidence_freshness(
        report,
        context,
        current_revisions=current_revisions,
        freshness=freshness,
    )


def feedback_from_operator(
    *,
    report: Mapping[str, Any],
    response: Mapping[str, Any],
) -> HumanFeedback:
    """Bind a minimal operator response to the exact pending report."""

    selection = SelectionReport.from_dict(report)
    supplied = copy.deepcopy(dict(response))
    unknown = set(supplied).difference({"feedback_id", "choices", "instructions"})
    if unknown:
        raise ValueError(
            "Advisor feedback has unknown fields: "
            + ", ".join(sorted(str(item) for item in unknown))
        )
    if "choices" not in supplied:
        raise ValueError("Advisor feedback requires choices")
    choices = tuple(FeedbackChoice.from_dict(item) for item in supplied["choices"])
    instructions = str(supplied.get("instructions") or "Follow these choices exactly.").strip()
    identity_payload = {
        "feedback_request_id": selection.feedback_request_id,
        "selection_report_id": selection.selection_report_id,
        "selection_report_digest": selection.digest,
        "choices": [item.to_dict() for item in choices],
        "instructions": instructions,
    }
    feedback_id = str(
        supplied.get("feedback_id")
        or f"ADVISOR-FEEDBACK-{digest_value(identity_payload)[:24]}"
    )
    feedback = HumanFeedback(
        feedback_id=feedback_id,
        feedback_request_id=selection.feedback_request_id,
        selection_report_id=selection.selection_report_id,
        selection_report_digest=selection.digest,
        choices=choices,
        instructions=instructions,
    )
    feedback.validate_for_report(selection)
    return feedback


def effective_problem_descriptor(
    state: Mapping[str, Any] | None,
    *,
    original_problem: str,
    cycle: int,
) -> dict[str, Any]:
    """Resolve the immutable visible problem for one research cycle."""

    if cycle < 1:
        raise ValueError("research cycle must be positive")
    if state is None or cycle == 1:
        core = {
            "assignment_id": "ORIGINAL-ROOT",
            "target_cycle": 1,
            "problem_text": original_problem,
            "original": True,
        }
        return {**core, "assignment_digest": digest_value(core)}
    matches = []
    for item in state.get("history", []):
        assignment = item.get("problem_assignment")
        if isinstance(assignment, Mapping) and assignment.get("target_cycle") == cycle:
            matches.append(ProblemAssignment.from_dict(assignment))
    if len(matches) != 1:
        raise ValueError(
            f"Advisor has {len(matches)} problem assignments for research cycle {cycle}"
        )
    assignment = matches[0]
    return {
        **assignment.to_dict(),
        "assignment_digest": assignment.digest,
        "original": False,
    }


def render_selection_report(report: SelectionReport) -> str:
    """Delegate human-facing rendering to the portable Advisor block."""

    return render_selection_report_markdown(report)


def build_advisor_program(runtime: Any) -> AdvisorProgram:
    """Construct the portable program against the Franta host adapter."""

    return AdvisorProgram(
        FrantaAdvisorHost(runtime),
        settings=PortableAdvisorSettings(**dict(runtime.config["advisor"])),
    )


class FrantaAdvisorHost:
    """The only lifecycle bridge from Advisor into a Franta runtime."""

    def __init__(self, runtime: Any) -> None:
        self.runtime = runtime

    def advisor_state_snapshot(self) -> Mapping[str, Any]:
        return self.runtime.scheduler.advisor_state

    def open_advisor_round(self, context: AdvisorCycleContext) -> str:
        return self.runtime._prepare_advisor_proposal(context)

    def execute_advisor_call(
        self, call_id: str, launch: AdvisorLaunchSpec
    ) -> Mapping[str, Any]:
        return self.runtime._execute_advisor_call(call_id, launch)

    def accept_proposal_response(
        self, call_id: str, response: Mapping[str, Any]
    ) -> SelectionReport:
        return self.runtime._commit_advisor_proposal(call_id, response)

    def human_feedback_for(self, feedback_request_id: str) -> HumanFeedback | None:
        state = self.runtime.scheduler.advisor_state
        active = state.get("active")
        if not isinstance(active, Mapping):
            return None
        raw = active.get("human_feedback")
        if not isinstance(raw, Mapping):
            return None
        feedback = HumanFeedback.from_dict(raw)
        if feedback.feedback_request_id != feedback_request_id:
            raise ValueError("Advisor feedback belongs to another request")
        return feedback

    def bind_human_feedback(self, feedback: HumanFeedback) -> None:
        self.runtime.scheduler.bind_advisor_feedback(feedback.to_dict())

    def begin_finalize_call(self) -> str:
        return self.runtime.scheduler.prepare_advisor_finalize(
            retry_limit=int(self.runtime.config["retries"]["main_transport"]),
        )

    def accept_finalize_response(
        self, call_id: str, response: Mapping[str, Any]
    ) -> ProblemAssignment:
        return self.runtime._commit_advisor_finalize(call_id, response)


__all__ = [
    "ADVISOR_CALL_KINDS",
    "AdvisorAgentCallSpec",
    "AdvisorCycleContext",
    "AdvisorFinalization",
    "AdvisorLaunchSpec",
    "AdvisorMemorySnapshot",
    "AdvisorSettingsError",
    "FrantaAdvisorHost",
    "FeedbackChoice",
    "HumanFeedback",
    "PortableAdvisorSettings",
    "ProblemAssignment",
    "SelectionReport",
    "accept_report_transition",
    "advisor_agent_call_spec",
    "advisor_breakthrough_evidence_freshness",
    "advisor_context",
    "advisor_skill_source",
    "advisor_status",
    "begin_finalize_transition",
    "bind_feedback_transition",
    "bind_session_transition",
    "build_advisor_program",
    "commit_assignment_transition",
    "effective_problem_descriptor",
    "feedback_from_operator",
    "is_advisor_call_kind",
    "new_advisor_state",
    "open_round_transition",
    "render_selection_report",
    "validate_advisor_breakthrough_evidence",
    "write_advisor_schemas",
]
