"""Host ports for the standalone Advisor program."""

from __future__ import annotations

from typing import Any, Mapping, Protocol, runtime_checkable

from .contracts import (
    AdvisorCycleContext,
    AdvisorMemorySnapshot,
    HumanFeedback,
    ProblemAssignment,
    SelectionReport,
)
from .prompts import AdvisorLaunchSpec


@runtime_checkable
class AdvisorMemoryPort(Protocol):
    """Host-controlled projection with main-agent-equivalent read authority."""

    def freeze_advisor_memory(
        self,
        *,
        source_cycle: int,
        access_profile: str,
    ) -> AdvisorMemorySnapshot:
        """Freeze all eligible memories from the completed source cycle."""


@runtime_checkable
class AdvisorHost(Protocol):
    """Reusable integration surface required by :class:`AdvisorProgram`.

    Implementations own persistence, IDs, model transport, audited memory tools,
    the trusted ``selection_report`` tool, and human-feedback delivery.  Each
    mutating method must be idempotent for its durable logical call.
    """

    def advisor_state_snapshot(self) -> Mapping[str, Any]:
        """Return the current persisted Advisor reducer state."""

    def open_advisor_round(self, context: AdvisorCycleContext) -> str:
        """Persist a proposal call and return its stable logical call ID."""

    def execute_advisor_call(
        self, call_id: str, launch: AdvisorLaunchSpec
    ) -> Mapping[str, Any]:
        """Execute or recover one call, honoring session resume requirements."""

    def accept_proposal_response(
        self, call_id: str, response: Mapping[str, Any]
    ) -> SelectionReport:
        """Resolve and persist the trusted selection-report artifact."""

    def human_feedback_for(self, feedback_request_id: str) -> HumanFeedback | None:
        """Return immutable feedback if a human answered this exact request."""

    def bind_human_feedback(self, feedback: HumanFeedback) -> None:
        """Persist feedback through the portable binding transition."""

    def begin_finalize_call(self) -> str:
        """Persist and return the stable call ID for the resumed second call."""

    def accept_finalize_response(
        self, call_id: str, response: Mapping[str, Any]
    ) -> ProblemAssignment:
        """Validate feedback fidelity and commit the deterministic assignment."""


@runtime_checkable
class AdvisorAssignmentSink(Protocol):
    """Optional downstream handoff seam for a completed visible problem."""

    def accept_problem_assignment(self, assignment: ProblemAssignment) -> Any:
        """Idempotently expose the assignment to its target research cycle."""


__all__ = ["AdvisorAssignmentSink", "AdvisorHost", "AdvisorMemoryPort"]
