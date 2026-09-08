"""Pure Block 9 trim and category-portfolio control transitions.

The functions in this module accept JSON-shaped snapshots and return copied
snapshots plus requested scheduler effects.  They never persist state, append
timestamps, allocate durable IDs, launch calls, or execute discovery/guidance
workflows.  Scheduler adapters apply their results inside the pre-existing
``scheduler.v1`` compare-and-swap transactions.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from ..contracts.workflows import GateState


class TrimControlError(RuntimeError):
    """Base class for a mechanically rejected Block 9 transition."""


class TrimStateError(TrimControlError):
    """A persisted trim record or supplied value is invalid."""


class TrimWorkflowError(TrimControlError):
    """The transition is not permitted from the current assignment gate."""


class TrimIdempotencyError(TrimControlError):
    """An idempotency key was replayed with different input."""


@dataclass(frozen=True)
class TrimEffect:
    """One scheduler audit event requested by a pure transition."""

    event_type: str
    payload: Mapping[str, Any]


@dataclass(frozen=True)
class TrimTransition:
    """A copied trim snapshot and its scheduler-level consequences."""

    trim: Mapping[str, Any]
    effects: tuple[TrimEffect, ...] = ()
    gate_target: GateState | None = None
    value: Any = None
    changed: bool = True


@dataclass(frozen=True)
class TrimCommitPlan:
    """A validated portfolio commit, not yet installed by the scheduler."""

    trim: Mapping[str, Any]
    active_trim: Mapping[str, Any]
    revision: int
    effect: TrimEffect


def _copy_trim(trim: Mapping[str, Any]) -> dict[str, Any]:
    return copy.deepcopy(dict(trim))


def _gate(value: GateState | str) -> GateState:
    return value if isinstance(value, GateState) else GateState(value)


def accumulate_assignment_reports(
    trim: Mapping[str, Any],
    reports: Sequence[Mapping[str, Any]],
    *,
    interval: int,
    base_event_id: int,
) -> TrimTransition:
    """Append one accepted atomic batch and trigger review at the boundary.

    The historical behavior intentionally sends *all* accumulated reports
    when a batch crosses the boundary; it does not retain a remainder.
    """

    working = _copy_trim(trim)
    working.setdefault("assignment_reports", []).extend(
        copy.deepcopy([dict(report) for report in reports])
    )
    if len(working["assignment_reports"]) < int(interval):
        return TrimTransition(working)
    working["active_review"] = {
        "trigger": "assignment_interval",
        "reports": copy.deepcopy(working["assignment_reports"]),
        "base_event_id": int(base_event_id),
    }
    return TrimTransition(working, gate_target=GateState.REVIEWING_TRIM)


def receive_stuck_report(
    trim: Mapping[str, Any],
    *,
    gate: GateState | str,
    batch_id: str,
    report: Mapping[str, Any],
    report_digest: str,
    base_event_id: int,
) -> TrimTransition:
    """Create or idempotently replay a stuck-triggered review."""

    working = _copy_trim(trim)
    active = working.get("active_review")
    if active and active.get("batch_id") == batch_id:
        if active.get("report_digest") != report_digest:
            raise TrimIdempotencyError(
                "stuck report replayed with different content"
            )
        return TrimTransition(working, changed=False)
    current_gate = _gate(gate)
    if current_gate != GateState.OPEN:
        raise TrimWorkflowError("stuck report requires open assignment gate")
    working["active_review"] = {
        "trigger": "stuck",
        "batch_id": batch_id,
        "report": copy.deepcopy(dict(report)),
        "report_digest": report_digest,
        "reports": copy.deepcopy(working.get("assignment_reports", [])),
        "base_event_id": int(base_event_id),
    }
    return TrimTransition(
        working,
        effects=(TrimEffect("stuck_report_received", {"batch_id": batch_id}),),
        gate_target=GateState.REVIEWING_TRIM,
    )


def needs_new_trimmer_session(
    trim: Mapping[str, Any], *, rounds_per_session: int
) -> bool:
    """Return the existing three-round session rollover predicate."""

    return not trim.get("session_id") or int(trim.get("session_rounds", 0)) >= int(
        rounds_per_session
    )


def start_review_round(
    trim: Mapping[str, Any],
    *,
    gate: GateState | str,
    rounds_per_session: int,
    new_session_id: str | None = None,
) -> TrimTransition:
    """Bind a top-level review to its persisted three-round session."""

    if _gate(gate) != GateState.REVIEWING_TRIM:
        raise TrimWorkflowError("no trim review is active")
    working = _copy_trim(trim)
    review = working.get("active_review")
    if not review:
        raise TrimStateError("reviewing_trim gate has no persisted review")
    if review.get("session_id"):
        return TrimTransition(
            working,
            value=str(review["session_id"]),
            changed=False,
        )
    if needs_new_trimmer_session(
        working, rounds_per_session=rounds_per_session
    ):
        if not new_session_id:
            raise TrimStateError("a new trimmer session ID is required")
        working["session_id"] = str(new_session_id)
        working["session_rounds"] = 0
    working["session_rounds"] = int(working["session_rounds"]) + 1
    working["round"] = int(working.get("round", 0)) + 1
    review["session_id"] = working["session_id"]
    review["session_round"] = working["session_rounds"]
    review["round"] = working["round"]
    session_id = str(working["session_id"])
    return TrimTransition(
        working,
        effects=(
            TrimEffect(
                "trim_review_round_started",
                {
                    "session_id": session_id,
                    "session_round": working["session_rounds"],
                    "round": working["round"],
                },
            ),
        ),
        value=session_id,
    )


def validate_review_decision(decision: str) -> None:
    if decision not in {"no_trim", "trim"}:
        raise TrimStateError("trim review decision must be no_trim or trim")


def apply_review_decision(
    trim: Mapping[str, Any],
    *,
    gate: GateState | str,
    decision: str,
    reason: str,
    cutoff_event_id: int,
) -> TrimTransition:
    """Apply the semantic Trimmer decision without making that decision."""

    validate_review_decision(decision)
    if _gate(gate) != GateState.REVIEWING_TRIM:
        raise TrimWorkflowError("no trim review is active")
    working = _copy_trim(trim)
    review = working.get("active_review")
    if not review:
        raise TrimStateError("reviewing_trim gate has no persisted review")
    review["decision"] = decision
    review["reason"] = reason
    session_id: str | None = None
    if decision == "no_trim":
        working["last_review"] = review
        working["active_review"] = None
        working["assignment_reports"] = []
        gate_target = GateState.OPEN
    else:
        session_id = str(review["session_id"])
        working["active_trim"] = {
            "round": review["round"],
            "session_id": session_id,
            "session_round": review["session_round"],
            "cutoff_event_id": int(cutoff_event_id),
            "review": copy.deepcopy(review),
            "phase": "maintain",
        }
        working["active_review"] = None
        gate_target = GateState.TRIMMING
    return TrimTransition(
        working,
        effects=(
            TrimEffect(
                "trim_review_decided",
                {
                    "decision": decision,
                    "reason": reason,
                    "session_id": session_id,
                },
            ),
        ),
        gate_target=gate_target,
        value=session_id,
    )


def set_trim_phase(
    trim: Mapping[str, Any], *, gate: GateState | str, phase: str
) -> TrimTransition:
    """Apply the established maintain/select phase rule."""

    if phase not in {"maintain", "select"}:
        raise TrimStateError("trim phase must be maintain or select")
    if _gate(gate) != GateState.TRIMMING:
        raise TrimWorkflowError("trim phase requires trimming gate")
    working = _copy_trim(trim)
    active = working.get("active_trim")
    if not active:
        raise TrimStateError("no active trim")
    if phase == "select" and active.get("phase") != "maintain":
        raise TrimWorkflowError("trimmer must run maintain before select")
    active["phase"] = phase
    return TrimTransition(
        working,
        effects=(TrimEffect("trim_phase", {"phase": phase}),),
    )


def commit_initial_portfolio(
    trim: Mapping[str, Any], portfolio: Mapping[str, Any]
) -> TrimTransition:
    """Install the first portfolio while retaining the scheduler JSON shape."""

    working = _copy_trim(trim)
    working["portfolio_revision"] = int(working.get("portfolio_revision", 0)) + 1
    working["portfolio"] = copy.deepcopy(dict(portfolio))
    revision = int(working["portfolio_revision"])
    return TrimTransition(
        working,
        effects=(
            TrimEffect(
                "initial_portfolio_committed",
                {"portfolio_revision": revision},
            ),
        ),
        gate_target=GateState.OPEN,
        value=revision,
    )


def plan_portfolio_commit(
    trim: Mapping[str, Any],
    portfolio: Mapping[str, Any],
    *,
    gate: GateState | str,
    expected_portfolio_revision: int,
    confirmed_through_event_id: Any,
    commit_event_cursor: int,
) -> TrimCommitPlan:
    """Validate and build the Block 9 part of an ordinary trim commit."""

    if _gate(gate) != GateState.TRIMMING:
        raise TrimWorkflowError("trim commit requires trimming gate")
    working = _copy_trim(trim)
    active = working.get("active_trim")
    if not active or active.get("phase") != "select":
        raise TrimWorkflowError(
            "trim must complete maintain and enter select before commit"
        )
    current_revision = int(working["portfolio_revision"])
    if int(expected_portfolio_revision) != current_revision:
        raise TrimStateError("stale category portfolio revision")
    if int(confirmed_through_event_id) < int(active["cutoff_event_id"]):
        raise TrimStateError("trimmer has not confirmed through its trim cutoff")
    revision = current_revision + 1
    working["portfolio_revision"] = revision
    working["portfolio"] = copy.deepcopy(dict(portfolio))
    active["confirmed_through_event_id"] = int(confirmed_through_event_id)
    active["commit_event_cursor"] = int(commit_event_cursor)
    active_snapshot = copy.deepcopy(active)
    working["last_trim"] = active
    working["active_trim"] = None
    working["assignment_reports"] = []
    return TrimCommitPlan(
        trim=working,
        active_trim=active_snapshot,
        revision=revision,
        effect=TrimEffect(
            "trim_committed",
            {
                "portfolio_revision": revision,
                # The legacy audit payload retained the caller's exact scalar
                # even though the persisted cutoff field is normalized to int.
                "confirmed_through": copy.deepcopy(confirmed_through_event_id),
            },
        ),
    )


def supersede_for_root_resolution(trim: Mapping[str, Any]) -> TrimTransition:
    """Clear active Block 9 work under the approved root-resolution rule."""

    working = _copy_trim(trim)
    if working.get("active_review"):
        working["active_review"]["cancelled"] = "root_resolution"
        working["active_review"] = None
    if working.get("active_trim"):
        working["active_trim"]["cancelled"] = "root_resolution"
        working["active_trim"] = None
    return TrimTransition(working)


__all__ = [
    "TrimCommitPlan",
    "TrimControlError",
    "TrimEffect",
    "TrimIdempotencyError",
    "TrimStateError",
    "TrimTransition",
    "TrimWorkflowError",
    "accumulate_assignment_reports",
    "apply_review_decision",
    "commit_initial_portfolio",
    "needs_new_trimmer_session",
    "plan_portfolio_commit",
    "receive_stuck_report",
    "set_trim_phase",
    "start_review_round",
    "supersede_for_root_resolution",
    "validate_review_decision",
]
