"""Pure, replay-safe Advisor lifecycle transitions.

The state value is ordinary JSON data.  A host may persist it in any durable
store, append the emitted events to its own log, and add timestamps externally.
No transition performs I/O or launches an agent.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Mapping

from .contracts import (
    AdvisorContractError,
    AdvisorCycleContext,
    AdvisorFinalization,
    HumanFeedback,
    ProblemAssignment,
    SelectionReport,
    build_problem_assignment,
)


ADVISOR_STATE_SCHEMA_VERSION = 1
STATUS_PROPOSING = "proposing"
STATUS_WAITING_FOR_HUMAN = "waiting_for_human"
STATUS_READY_TO_FINALIZE = "ready_to_finalize"
STATUS_FINALIZING = "finalizing"
ACTIVE_STATUSES = frozenset(
    {
        STATUS_PROPOSING,
        STATUS_WAITING_FOR_HUMAN,
        STATUS_READY_TO_FINALIZE,
        STATUS_FINALIZING,
    }
)


class AdvisorStateError(RuntimeError):
    """Raised when a requested transition is invalid for durable state."""

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class AdvisorTransition:
    """Result of one pure transition, including appendable domain events."""

    state: dict[str, Any]
    events: tuple[dict[str, Any], ...] = ()
    changed: bool = False
    value: Any = None


def initialize_advisor_state(*, session_key: str = "advisor:project") -> dict[str, Any]:
    """Create an empty persistent state value for one project Advisor."""

    if not isinstance(session_key, str) or not session_key.strip() or session_key != session_key.strip():
        raise AdvisorStateError("session_key must be canonical text", code="invalid_session_key")
    return {
        "schema_version": ADVISOR_STATE_SCHEMA_VERSION,
        "session_key": session_key,
        "session_id": None,
        "active": None,
        "history": [],
    }


def _copy_and_validate(state: Mapping[str, Any]) -> dict[str, Any]:
    value = deepcopy(dict(state))
    if value.get("schema_version") != ADVISOR_STATE_SCHEMA_VERSION:
        raise AdvisorStateError(
            "unsupported Advisor state schema", code="unsupported_state_schema"
        )
    if not isinstance(value.get("session_key"), str) or not value["session_key"]:
        raise AdvisorStateError("state has no session_key", code="invalid_state")
    if value.get("session_id") is not None and (
        not isinstance(value["session_id"], str) or not value["session_id"].strip()
    ):
        raise AdvisorStateError("state has an invalid session_id", code="invalid_state")
    if not isinstance(value.get("history"), list):
        raise AdvisorStateError("state history must be a list", code="invalid_state")
    active = value.get("active")
    if active is not None:
        if not isinstance(active, dict) or active.get("status") not in ACTIVE_STATUSES:
            raise AdvisorStateError("state has an invalid active round", code="invalid_state")
        try:
            AdvisorCycleContext.from_dict(active["context"])
        except (KeyError, TypeError, AdvisorContractError) as exc:
            raise AdvisorStateError(
                "state has an invalid active context", code="invalid_state"
            ) from exc
    return value


def current_status(state: Mapping[str, Any]) -> str:
    """Return ``idle`` or the active round's lifecycle status."""

    value = _copy_and_validate(state)
    active = value["active"]
    return "idle" if active is None else active["status"]


def bind_advisor_session(
    state: Mapping[str, Any], *, session_id: str
) -> AdvisorTransition:
    """Bind the one transport session that every later Advisor call resumes."""

    if not isinstance(session_id, str) or not session_id.strip() or session_id != session_id.strip():
        raise AdvisorStateError("session_id must be canonical text", code="invalid_session_id")
    value = _copy_and_validate(state)
    existing = value["session_id"]
    if existing is not None:
        if existing != session_id:
            raise AdvisorStateError(
                "the Advisor session is immutable once bound",
                code="advisor_session_replacement_forbidden",
            )
        return AdvisorTransition(value, changed=False, value=session_id)
    value["session_id"] = session_id
    event = {
        "event": "advisor_session_bound",
        "session_key": value["session_key"],
        "session_id": session_id,
    }
    return AdvisorTransition(value, (event,), True, session_id)


def _completed_assignments(value: Mapping[str, Any]) -> tuple[ProblemAssignment, ...]:
    try:
        return tuple(
            ProblemAssignment.from_dict(item["problem_assignment"])
            for item in value["history"]
        )
    except (KeyError, TypeError, AdvisorContractError) as exc:
        raise AdvisorStateError("state history is invalid", code="invalid_state") from exc


def _ensure_unique_call_id(value: Mapping[str, Any], call_id: str) -> None:
    if not isinstance(call_id, str) or not call_id.strip() or call_id != call_id.strip():
        raise AdvisorStateError("call_id must be canonical text", code="invalid_call_id")
    active = value.get("active")
    used = {
        candidate
        for item in value["history"]
        for candidate in (item.get("proposal_call_id"), item.get("finalize_call_id"))
        if candidate is not None
    }
    if active is not None:
        used.update(
            candidate
            for candidate in (active.get("proposal_call_id"), active.get("finalize_call_id"))
            if candidate is not None
        )
    if call_id in used:
        raise AdvisorStateError("call_id is already bound", code="duplicate_call_id")


def open_advisor_round(
    state: Mapping[str, Any],
    context: AdvisorCycleContext,
    *,
    proposal_call_id: str,
) -> AdvisorTransition:
    """Atomically bind inputs and open the proposal half of Advisor i."""

    value = _copy_and_validate(state)
    active = value["active"]
    if active is not None:
        if (
            active["status"] == STATUS_PROPOSING
            and active["context_digest"] == context.digest
            and active["proposal_call_id"] == proposal_call_id
        ):
            return AdvisorTransition(value, changed=False)
        raise AdvisorStateError(
            "another Advisor round is already active", code="advisor_round_active"
        )
    history_assignments = _completed_assignments(value)
    expected_index = len(history_assignments) + 1
    if context.advisor_index != expected_index:
        raise AdvisorStateError(
            "Advisor rounds must be opened in consecutive order",
            code="advisor_index_out_of_order",
        )
    expected_history = tuple(
        (item.assignment_id, item.digest) for item in history_assignments
    )
    actual_history = tuple(
        (item.assignment_id, item.digest) for item in context.previous_assignments
    )
    if actual_history != expected_history:
        raise AdvisorStateError(
            "context previous assignments do not equal completed Advisor history",
            code="previous_assignment_history_mismatch",
        )
    _ensure_unique_call_id(value, proposal_call_id)
    value["active"] = {
        "advisor_index": context.advisor_index,
        "status": STATUS_PROPOSING,
        "context": context.to_dict(),
        "context_digest": context.digest,
        "proposal_call_id": proposal_call_id,
        "selection_report": None,
        "selection_report_digest": None,
        "human_feedback": None,
        "human_feedback_digest": None,
        "finalize_call_id": None,
    }
    event = {
        "event": "advisor_round_opened",
        "advisor_index": context.advisor_index,
        "source_cycle": context.source_cycle,
        "target_cycle": context.target_cycle,
        "context_digest": context.digest,
        "proposal_call_id": proposal_call_id,
    }
    return AdvisorTransition(value, (event,), True)


def accept_selection_report(
    state: Mapping[str, Any],
    *,
    proposal_call_id: str,
    report: SelectionReport,
) -> AdvisorTransition:
    """Validate the skill artifact and enter the durable human wait state."""

    value = _copy_and_validate(state)
    active = value["active"]
    if active is None:
        raise AdvisorStateError("there is no active Advisor round", code="no_active_round")
    if active["proposal_call_id"] != proposal_call_id:
        raise AdvisorStateError(
            "proposal result belongs to another call", code="proposal_call_mismatch"
        )
    if active["status"] != STATUS_PROPOSING:
        existing = active.get("selection_report_digest")
        if existing == report.digest:
            return AdvisorTransition(value, changed=False, value=report)
        raise AdvisorStateError(
            "selection report cannot be replaced", code="selection_report_replacement_forbidden"
        )
    context = AdvisorCycleContext.from_dict(active["context"])
    report.validate_for_context(context)
    prior_report_ids = {
        item.get("selection_report", {}).get("selection_report_id")
        for item in value["history"]
        if isinstance(item.get("selection_report"), Mapping)
    }
    prior_request_ids = {
        item.get("selection_report", {}).get("feedback_request_id")
        for item in value["history"]
        if isinstance(item.get("selection_report"), Mapping)
    }
    if report.selection_report_id in prior_report_ids:
        raise AdvisorStateError(
            "selection report ID was already used by an earlier round",
            code="duplicate_selection_report_id",
        )
    if report.feedback_request_id in prior_request_ids:
        raise AdvisorStateError(
            "feedback request ID was already used by an earlier round",
            code="duplicate_feedback_request_id",
        )
    active["selection_report"] = report.to_dict()
    active["selection_report_digest"] = report.digest
    active["status"] = STATUS_WAITING_FOR_HUMAN
    events = (
        {
            "event": "advisor_selection_report_accepted",
            "advisor_index": context.advisor_index,
            "selection_report_id": report.selection_report_id,
            "selection_report_digest": report.digest,
            "feedback_request_id": report.feedback_request_id,
        },
        {
            "event": "advisor_waiting_for_human",
            "advisor_index": context.advisor_index,
            "feedback_request_id": report.feedback_request_id,
        },
    )
    return AdvisorTransition(value, events, True, report)


def bind_human_feedback(
    state: Mapping[str, Any], feedback: HumanFeedback
) -> AdvisorTransition:
    """Bind one immutable human decision and make the second call eligible."""

    value = _copy_and_validate(state)
    active = value["active"]
    if active is None:
        raise AdvisorStateError("there is no active Advisor round", code="no_active_round")
    if active["status"] != STATUS_WAITING_FOR_HUMAN:
        existing = active.get("human_feedback_digest")
        if existing == feedback.digest:
            return AdvisorTransition(value, changed=False, value=feedback)
        raise AdvisorStateError(
            "human feedback cannot be accepted in this state",
            code="feedback_not_expected",
        )
    report = SelectionReport.from_dict(active["selection_report"])
    feedback.validate_for_report(report)
    active["human_feedback"] = feedback.to_dict()
    active["human_feedback_digest"] = feedback.digest
    active["status"] = STATUS_READY_TO_FINALIZE
    event = {
        "event": "advisor_feedback_bound",
        "advisor_index": active["advisor_index"],
        "feedback_id": feedback.feedback_id,
        "feedback_digest": feedback.digest,
        "feedback_request_id": feedback.feedback_request_id,
    }
    return AdvisorTransition(value, (event,), True, feedback)


def begin_finalize(
    state: Mapping[str, Any], *, finalize_call_id: str
) -> AdvisorTransition:
    """Plan the second call; it must resume the state's bound session."""

    value = _copy_and_validate(state)
    active = value["active"]
    if active is None:
        raise AdvisorStateError("there is no active Advisor round", code="no_active_round")
    if active["status"] == STATUS_FINALIZING:
        if active["finalize_call_id"] == finalize_call_id:
            return AdvisorTransition(value, changed=False, value=finalize_call_id)
        raise AdvisorStateError(
            "finalize call cannot be replaced", code="finalize_call_replacement_forbidden"
        )
    if active["status"] != STATUS_READY_TO_FINALIZE:
        raise AdvisorStateError(
            "binding human feedback is required before finalization",
            code="finalize_not_ready",
        )
    if value["session_id"] is None:
        raise AdvisorStateError(
            "the Advisor session must be bound before its second call",
            code="advisor_session_unbound",
        )
    _ensure_unique_call_id(value, finalize_call_id)
    active["finalize_call_id"] = finalize_call_id
    active["status"] = STATUS_FINALIZING
    event = {
        "event": "advisor_finalize_started",
        "advisor_index": active["advisor_index"],
        "finalize_call_id": finalize_call_id,
        "session_id": value["session_id"],
    }
    return AdvisorTransition(value, (event,), True, finalize_call_id)


def commit_problem_assignment(
    state: Mapping[str, Any],
    *,
    finalize_call_id: str,
    assignment_id: str,
    finalization: AdvisorFinalization,
) -> AdvisorTransition:
    """Commit the feedback-faithful problem file and close Advisor i."""

    value = _copy_and_validate(state)
    active = value["active"]
    if active is None:
        for item in value["history"]:
            assignment = ProblemAssignment.from_dict(item["problem_assignment"])
            if item.get("finalize_call_id") == finalize_call_id:
                if (
                    assignment.assignment_id != assignment_id
                    or item.get("finalization_digest") != finalization.digest
                ):
                    raise AdvisorStateError(
                        "a completed finalize call cannot change its finalization",
                        code="assignment_replacement_forbidden",
                    )
                context = AdvisorCycleContext.from_dict(item["context"])
                report = SelectionReport.from_dict(item["selection_report"])
                feedback = HumanFeedback.from_dict(item["human_feedback"])
                expected = build_problem_assignment(
                    assignment_id=assignment_id,
                    context=context,
                    report=report,
                    feedback=feedback,
                    finalization=finalization,
                )
                if expected.digest != assignment.digest:
                    raise AdvisorStateError(
                        "completed assignment does not match its durable bindings",
                        code="completed_assignment_mismatch",
                    )
                return AdvisorTransition(value, changed=False, value=assignment)
        raise AdvisorStateError("there is no active Advisor round", code="no_active_round")
    if active["status"] != STATUS_FINALIZING:
        raise AdvisorStateError(
            "the Advisor round is not finalizing", code="finalize_not_active"
        )
    if active["finalize_call_id"] != finalize_call_id:
        raise AdvisorStateError(
            "finalization belongs to another call", code="finalize_call_mismatch"
        )
    context = AdvisorCycleContext.from_dict(active["context"])
    report = SelectionReport.from_dict(active["selection_report"])
    feedback = HumanFeedback.from_dict(active["human_feedback"])
    assignment = build_problem_assignment(
        assignment_id=assignment_id,
        context=context,
        report=report,
        feedback=feedback,
        finalization=finalization,
    )
    history_item = deepcopy(active)
    history_item.update(
        {
            "status": "completed",
            "finalization": finalization.to_dict(),
            "finalization_digest": finalization.digest,
            "problem_assignment": assignment.to_dict(),
            "problem_assignment_digest": assignment.digest,
        }
    )
    value["history"].append(history_item)
    value["active"] = None
    event = {
        "event": "advisor_problem_assignment_committed",
        "advisor_index": assignment.advisor_index,
        "assignment_id": assignment.assignment_id,
        "assignment_digest": assignment.digest,
        "target_cycle": assignment.target_cycle,
        "finalize_call_id": finalize_call_id,
    }
    return AdvisorTransition(value, (event,), True, assignment)


__all__ = [
    "ACTIVE_STATUSES",
    "ADVISOR_STATE_SCHEMA_VERSION",
    "AdvisorStateError",
    "AdvisorTransition",
    "STATUS_FINALIZING",
    "STATUS_PROPOSING",
    "STATUS_READY_TO_FINALIZE",
    "STATUS_WAITING_FOR_HUMAN",
    "accept_selection_report",
    "begin_finalize",
    "bind_advisor_session",
    "bind_human_feedback",
    "commit_problem_assignment",
    "current_status",
    "initialize_advisor_state",
    "open_advisor_round",
]
