"""Pure workflow terminology and transition contracts.

This module deliberately contains no storage or transport code.  Keeping the
transition tables here makes recovery code and tests use the same rules as the
live scheduler.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Iterable, Mapping


class WorkflowError(ValueError):
    """Raised when a requested durable transition is not permitted."""


class GateState(str, Enum):
    OPEN = "open"
    REVIEWING_TRIM = "reviewing_trim"
    TRIMMING = "trimming"
    WAITING_FOR_HUMAN = "waiting_for_human"
    RESOLUTION_PENDING = "resolution_pending"
    COMPLETED = "completed"


class TaskState(str, Enum):
    QUEUED = "queued"
    LAUNCHING = "launching"
    RUNNING = "running"
    ATTEMPT_ENDED = "attempt_ended"
    POSTPROCESSING = "postprocessing"
    REVISION_PENDING = "revision_pending"
    RETRY_PENDING = "retry_pending"
    STOPPING = "stopping"
    NEEDS_ATTENTION = "needs_attention"
    CLOSED = "closed"


class TaskOutcome(str, Enum):
    FINISHED = "finished"
    PROGRESS = "progress"
    FAILED = "failed"
    INTERRUPTED = "interrupted"


class CallState(str, Enum):
    PREPARED = "prepared"
    RUNNING = "running"
    RETRY_PENDING = "retry_pending"
    COMPLETED = "completed"
    COMMITTED = "committed"
    SUPERSEDED = "superseded"
    NEEDS_ATTENTION = "needs_attention"
    CANCELLED = "cancelled"


class OperationState(str, Enum):
    RECEIVED = "received"
    WAITING_PREDECESSORS = "waiting_predecessors"
    SYNTHESIZING = "synthesizing"
    VERIFYING = "verifying"
    REVISION_PENDING = "revision_pending"
    NEEDS_ATTENTION = "needs_attention"
    COMMITTED = "committed"
    REJECTED = "rejected"
    ABANDONED = "abandoned"


FINAL_OPERATION_STATES = frozenset(
    {OperationState.COMMITTED.value, OperationState.REJECTED.value, OperationState.ABANDONED.value}
)

MEMORY_OPERATION_KINDS = frozenset(
    {
        "fact",
        "route_update",
        "route_add",
        "memo",
        "claim_add",
        "claim_remove",
        "obligation_add",
        "obligation_update",
        "obligation_remove",
    }
)

NON_VERIFIER_MODES = frozenset(
    {
        "research",
        "brainstorm",
        "associate",
        "multi-discipline",
        "reformulate",
        "computation",
        "proof-writer",
    }
)

ISOLATED_MODES = frozenset({"brainstorm", "multi-discipline"})
SPRINT_LANE_MODES = ("brainstorm", "multi-discipline", "computation", "associate")


_GATE_TRANSITIONS: Mapping[GateState, frozenset[GateState]] = {
    GateState.OPEN: frozenset(
        {GateState.REVIEWING_TRIM, GateState.TRIMMING, GateState.RESOLUTION_PENDING}
    ),
    GateState.REVIEWING_TRIM: frozenset(
        {GateState.OPEN, GateState.TRIMMING, GateState.RESOLUTION_PENDING}
    ),
    GateState.TRIMMING: frozenset(
        {GateState.OPEN, GateState.WAITING_FOR_HUMAN, GateState.RESOLUTION_PENDING}
    ),
    GateState.WAITING_FOR_HUMAN: frozenset(
        {GateState.TRIMMING, GateState.RESOLUTION_PENDING}
    ),
    GateState.RESOLUTION_PENDING: frozenset({GateState.COMPLETED, GateState.OPEN}),
    GateState.COMPLETED: frozenset({GateState.OPEN}),
}


_TASK_TRANSITIONS: Mapping[TaskState, frozenset[TaskState]] = {
    TaskState.QUEUED: frozenset({TaskState.LAUNCHING, TaskState.STOPPING}),
    TaskState.LAUNCHING: frozenset(
        {
            TaskState.RUNNING,
            TaskState.RETRY_PENDING,
            TaskState.STOPPING,
            TaskState.NEEDS_ATTENTION,
        }
    ),
    TaskState.RUNNING: frozenset(
        {
            TaskState.ATTEMPT_ENDED,
            TaskState.RETRY_PENDING,
            TaskState.STOPPING,
            TaskState.NEEDS_ATTENTION,
        }
    ),
    TaskState.ATTEMPT_ENDED: frozenset(
        {TaskState.POSTPROCESSING, TaskState.REVISION_PENDING, TaskState.RETRY_PENDING}
    ),
    TaskState.POSTPROCESSING: frozenset(
        {TaskState.CLOSED, TaskState.REVISION_PENDING, TaskState.NEEDS_ATTENTION}
    ),
    TaskState.REVISION_PENDING: frozenset(
        {
            TaskState.LAUNCHING,
            TaskState.RUNNING,
            TaskState.POSTPROCESSING,
            TaskState.STOPPING,
            TaskState.NEEDS_ATTENTION,
        }
    ),
    TaskState.RETRY_PENDING: frozenset(
        {
            TaskState.LAUNCHING,
            TaskState.RUNNING,
            TaskState.REVISION_PENDING,
            TaskState.POSTPROCESSING,
            TaskState.STOPPING,
            TaskState.CLOSED,
            TaskState.NEEDS_ATTENTION,
        }
    ),
    TaskState.STOPPING: frozenset(
        {TaskState.ATTEMPT_ENDED, TaskState.RETRY_PENDING, TaskState.CLOSED}
    ),
    TaskState.NEEDS_ATTENTION: frozenset(
        {
            TaskState.POSTPROCESSING,
            TaskState.REVISION_PENDING,
            TaskState.RETRY_PENDING,
            TaskState.STOPPING,
            TaskState.CLOSED,
        }
    ),
    TaskState.CLOSED: frozenset(),
}


def require_gate_transition(current: str | GateState, target: str | GateState) -> None:
    current_state = GateState(current)
    target_state = GateState(target)
    if current_state == target_state:
        return
    if target_state not in _GATE_TRANSITIONS[current_state]:
        raise WorkflowError(f"invalid gate transition: {current_state.value} -> {target_state.value}")


def require_task_transition(current: str | TaskState, target: str | TaskState) -> None:
    current_state = TaskState(current)
    target_state = TaskState(target)
    if current_state == target_state:
        return
    if target_state not in _TASK_TRANSITIONS[current_state]:
        raise WorkflowError(f"invalid task transition: {current_state.value} -> {target_state.value}")


def is_operation_terminal(state: str | OperationState) -> bool:
    return OperationState(state).value in FINAL_OPERATION_STATES


def validate_sprint_modes(modes: Iterable[str]) -> None:
    values = tuple(modes)
    if values != SPRINT_LANE_MODES:
        raise WorkflowError(
            "a discovery sprint must contain lanes in order: " + ", ".join(SPRINT_LANE_MODES)
        )


@dataclass(frozen=True)
class RetryPolicy:
    worker: int = 3
    verifier: int = 3
    synthesizer: int = 3
    summarizer: int = 3
    main: int = 3
    trimmer: int = 3

    def __post_init__(self) -> None:
        for name, value in vars(self).items():
            if not isinstance(value, int) or value <= 0:
                raise WorkflowError(f"retry limit {name!r} must be a positive integer")

    def for_kind(self, kind: str) -> int:
        normalized = kind.replace("-", "_")
        if normalized in {"proof_writer", "worker", "discovery_lane"}:
            normalized = "worker"
        try:
            return int(getattr(self, normalized))
        except AttributeError as exc:
            raise WorkflowError(f"no retry policy for call kind {kind!r}") from exc


@dataclass(frozen=True)
class SchedulerLimits:
    max_non_verifier_workers: int = 4
    max_parallel_verifiers: int = 2
    portfolio_max_memories: int = 20
    search_max_results: int = 10
    explicit_worker_resumes: int = 6
    trim_assignment_interval: int = 8
    trimmer_rounds_per_session: int = 3

    def __post_init__(self) -> None:
        for name, value in vars(self).items():
            if not isinstance(value, int) or value <= 0:
                raise WorkflowError(f"scheduler limit {name!r} must be a positive integer")


__all__ = [
    "CallState",
    "FINAL_OPERATION_STATES",
    "GateState",
    "ISOLATED_MODES",
    "MEMORY_OPERATION_KINDS",
    "NON_VERIFIER_MODES",
    "OperationState",
    "RetryPolicy",
    "SPRINT_LANE_MODES",
    "SchedulerLimits",
    "TaskOutcome",
    "TaskState",
    "WorkflowError",
    "is_operation_terminal",
    "require_gate_transition",
    "require_task_transition",
    "validate_sprint_modes",
]
