"""Durable deterministic control plane for Franta research workflows.

The scheduler owns control state and is the only component allowed to ask the
canonical :class:`franta.store.MemoryStore` to commit records.  Mathematical
decisions are supplied as agent results; this module only validates and moves
them through persisted workflows.
"""

from __future__ import annotations

import copy
import hashlib
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, MutableMapping, Sequence

from .execution_gateway import call_state as call_machine
from .trim_category import control as trim_control
from .exploration_control import snapshots as exploration_snapshots
from .exploration_control import state as exploration_state
from .exploration_control import validation as exploration_validation
from .explorer_control import state as explorer_controller
from .explorer_adapter import (
    main_sort_computation_has_exact_provenance,
    main_sort_operation_has_exact_provenance,
    main_sort_operation_kinds,
)
from .advisor_adapter import (
    accept_report_transition as advisor_accept_report_transition,
    advisor_status as portable_advisor_status,
    begin_finalize_transition as advisor_begin_finalize_transition,
    bind_feedback_transition as advisor_bind_feedback_transition,
    bind_session_transition as advisor_bind_session_transition,
    commit_assignment_transition as advisor_commit_assignment_transition,
    effective_problem_descriptor as portable_effective_problem_descriptor,
    new_advisor_state,
    open_round_transition as advisor_open_round_transition,
)
from .phase_control import state as phase_controller
from .recovery import (
    ControlRepository,
    RecoveryPlan,
    append_event,
    canonical_json,
    default_scheduler_state,
    infrastructure_retry_supplement,
    preserved_attempt_supplement,
    reconcile_after_full_stop,
    stable_digest,
    utc_now,
)
from .contracts.references import (
    ReferenceValidationError,
    contains_identifier_token,
    substitute_nonfact_typed_ids,
)
from .contracts.canonical import NotFoundError
from .read_access.snapshots import (
    SnapshotValidationError,
    validate_assignment_portfolio,
    validate_record_reference,
)
from .read_access.main_memory_snapshot import (
    MainMemorySnapshot,
    build_main_memory_snapshot,
)
from .contracts.workflows import (
    FINAL_OPERATION_STATES,
    ISOLATED_MODES,
    MEMORY_OPERATION_KINDS,
    NON_VERIFIER_MODES,
    CallState,
    GateState,
    OperationState,
    RetryPolicy,
    SchedulerLimits,
    TaskOutcome,
    TaskState,
    WorkflowError,
    require_gate_transition,
    require_task_transition,
    validate_sprint_modes,
)


RUNTIME_EVENT_LOOP_CONTRACT = """\
On fresh start or resume, construct Scheduler and call recover() exactly once
after process-liveness reconciliation.  The runtime wrapper then (1) commits
completed call results through their typed commit methods, (2) relaunches the
exact persisted retry calls and task launch intents returned by the recovery
plan, (3) calls reconcile_pending_ingestion(), and (4) reacts to new durable
events only when the assignment gate permits them.  It must pass call IDs and
lease epochs to transports, reject stale output through Scheduler, never edit
the state snapshot itself, and never infer mathematical verdicts from process
failure.  Before exiting it leaves all uncompleted work in its persisted state;
the next invocation repeats this same procedure.
"""

_SPRINT_PORTFOLIO_TYPES = exploration_validation.SPRINT_PORTFOLIO_TYPES
_SPRINT_LANE_LABELS = exploration_validation.SPRINT_LANE_LABELS

_PROPOSAL_MEMORY_TYPES = {
    "fact": "fact",
    "route_add": "route",
    "memo": "memo",
    "claim_add": "claim",
    "obligation_add": "obligation",
}

_ALL_CANONICAL_MEMORY_TYPES = (
    "fact",
    "route",
    "memo",
    "claim",
    "obligation",
    "task",
    "computation",
)

_EXPLORER_GUIDANCE_VARIANTS_BY_MODE = {
    "check-result": frozenset(
        {"check-result-promising", "check-result-uncommon"}
    ),
    "portfolio": frozenset(
        {
            "portfolio-synthesize",
            "portfolio-select-best",
            "portfolio-diversify",
            "portfolio-unconstrained",
        }
    ),
    "full-memory": frozenset({"full-memory-adaptive"}),
}

_HUMAN_GUIDANCE_FIELDS = (
    "guidance_id",
    "text",
    "sha256",
    "relative_path",
    "received_at",
)

# These are the only worker-controlled slots whose values are canonical-memory
# references.  Each item gives the accepted canonical type(s) and the type of
# a temporary proposal ID allowed in that slot; ``None`` means canonical only.
# Wildcards walk list members without inspecting any mathematical prose.
_OPERATION_ACCESS_PATHS: dict[
    str,
    tuple[
        tuple[
            tuple[str, ...],
            tuple[str, ...],
            str | tuple[str, ...] | None,
        ],
        ...,
    ],
] = {
    "fact": (
        (("predecessor_fact_ids", "*"), ("fact",), "fact"),
        (("related_route_ids", "*"), ("route",), "route"),
    ),
    "route_add": (
        (("related_obligation_ids", "*"), ("obligation",), "obligation"),
        (("active_fact_ids", "*"), ("fact",), "fact"),
        (("relevant_memo_ids", "*"), ("memo",), "memo"),
        (("relevant_claim_ids", "*"), ("claim",), "claim"),
    ),
    "memo": ((("related_route_ids", "*"), ("route",), "route"),),
    "claim_add": ((("related_route_ids", "*"), ("route",), "route"),),
    "obligation_add": (
        (("predecessor_fact_ids", "*"), ("fact",), "fact"),
        (("related_route_ids", "*"), ("route",), "route"),
        (
            ("relations", "*", "premise_memory_ids", "*"),
            ("fact", "obligation"),
            ("fact", "obligation"),
        ),
        (("relations", "*", "supporting_fact_ids", "*"), ("fact",), "fact"),
        (("relations", "*", "conclusion"), ("obligation",), "obligation"),
    ),
    "route_update": (
        (("target_id",), ("route",), None),
        (("supporting_memory_ids", "*"), _ALL_CANONICAL_MEMORY_TYPES, None),
        (("add_ids", "related_obligation_ids", "*"), ("obligation",), "obligation"),
        (("add_ids", "active_fact_ids", "*"), ("fact",), "fact"),
        (("add_ids", "relevant_memo_ids", "*"), ("memo",), "memo"),
        (("add_ids", "relevant_claim_ids", "*"), ("claim",), "claim"),
        (("remove_ids", "related_obligation_ids", "*"), ("obligation",), "obligation"),
        (("remove_ids", "active_fact_ids", "*"), ("fact",), "fact"),
        (("remove_ids", "relevant_memo_ids", "*"), ("memo",), "memo"),
        (("remove_ids", "relevant_claim_ids", "*"), ("claim",), "claim"),
    ),
    "obligation_update": (
        (("target_id",), ("obligation",), None),
        (("supporting_memory_ids", "*"), _ALL_CANONICAL_MEMORY_TYPES, None),
        (("add_ids", "related_route_ids", "*"), ("route",), "route"),
        (("remove_ids", "related_route_ids", "*"), ("route",), "route"),
        (
            ("set", "relations", "*", "premise_memory_ids", "*"),
            ("fact", "obligation"),
            ("fact", "obligation"),
        ),
        (("set", "relations", "*", "supporting_fact_ids", "*"), ("fact",), "fact"),
        (("set", "relations", "*", "conclusion"), ("obligation",), "obligation"),
        (
            ("append", "relations", "*", "premise_memory_ids", "*"),
            ("fact", "obligation"),
            ("fact", "obligation"),
        ),
        (("append", "relations", "*", "supporting_fact_ids", "*"), ("fact",), "fact"),
        (("append", "relations", "*", "conclusion"), ("obligation",), "obligation"),
    ),
    "claim_remove": (
        (("target_id",), ("claim",), None),
        (("replacement_id",), ("claim", "fact"), None),
    ),
    "obligation_remove": (
        (("target_id",), ("obligation",), None),
        (("resolving_fact_ids", "*"), ("fact",), None),
        (("refuting_fact_ids", "*"), ("fact",), None),
        (("replacement_obligation_id",), ("obligation",), None),
    ),
}

_COMPUTATION_ACCESS_PATHS = tuple(
    (("related_memory_ids", memory_type, "*"), (memory_type,), memory_type)
    for memory_type in ("fact", "route", "memo", "claim", "obligation")
)

# Existing canonical records in these slots must also be active.  This mirrors
# the store's established operation schema; generic supporting_memory_ids are
# deliberately absent because they may cite inactive non-fact history.
_ACTIVE_OPERATION_REFERENCE_TYPES: dict[
    str, dict[tuple[str, ...], tuple[str, ...]]
] = {
    "fact": {
        ("predecessor_fact_ids", "*"): ("fact",),
    },
    "route_add": {
        ("related_obligation_ids", "*"): ("obligation",),
        ("active_fact_ids", "*"): ("fact",),
        ("relevant_claim_ids", "*"): ("claim",),
    },
    "obligation_add": {
        ("predecessor_fact_ids", "*"): ("fact",),
        ("relations", "*", "premise_memory_ids", "*"): ("fact",),
        ("relations", "*", "supporting_fact_ids", "*"): ("fact",),
        ("relations", "*", "conclusion"): ("obligation",),
    },
    "route_update": {
        ("target_id",): ("route",),
        ("add_ids", "related_obligation_ids", "*"): ("obligation",),
        ("add_ids", "active_fact_ids", "*"): ("fact",),
        ("add_ids", "relevant_claim_ids", "*"): ("claim",),
    },
    "obligation_update": {
        ("target_id",): ("obligation",),
        ("set", "relations", "*", "premise_memory_ids", "*"): ("fact",),
        ("set", "relations", "*", "supporting_fact_ids", "*"): ("fact",),
        ("set", "relations", "*", "conclusion"): ("obligation",),
        ("append", "relations", "*", "premise_memory_ids", "*"): ("fact",),
        ("append", "relations", "*", "supporting_fact_ids", "*"): ("fact",),
        ("append", "relations", "*", "conclusion"): ("obligation",),
    },
    "claim_remove": {
        ("target_id",): ("claim",),
        ("replacement_id",): ("claim", "fact"),
    },
    "obligation_remove": {
        ("target_id",): ("obligation",),
        ("resolving_fact_ids", "*"): ("fact",),
        ("refuting_fact_ids", "*"): ("fact",),
        ("replacement_obligation_id",): ("obligation",),
    },
}


class SchedulerError(RuntimeError):
    """Base class for deterministic scheduler rejections."""


class IdempotencyConflict(SchedulerError):
    """An idempotency key was replayed with different input."""


class CapacityError(SchedulerError):
    """An atomic batch cannot reserve all required worker slots."""


class StaleLeaseError(SchedulerError):
    """Output belongs to a fenced or superseded call epoch."""


class TransportFailure(SchedulerError):
    """A call ended without a valid semantic result."""


def _raise_trim_control_error(error: trim_control.TrimControlError) -> None:
    """Preserve Scheduler's established public exception taxonomy."""

    if isinstance(error, trim_control.TrimWorkflowError):
        raise WorkflowError(str(error)) from error
    if isinstance(error, trim_control.TrimIdempotencyError):
        raise IdempotencyConflict(str(error)) from error
    raise SchedulerError(str(error)) from error


def _append_trim_effects(
    state: MutableMapping[str, Any], effects: Sequence[trim_control.TrimEffect]
) -> None:
    for effect in effects:
        append_event(state, effect.event_type, effect.payload)


_PERSISTED_CALL_EFFECTS = frozenset(
    {
        "call_running",
        "call_result_received",
        "call_result_committed",
        "call_transport_failure",
        "call_invalid_output",
        "call_lease_fenced",
    }
)

_ATTENTION_SCOPES = frozenset({"project", "task"})

_TERMINAL_WORKER_ATTEMPT_STATES = frozenset(
    {
        "ended",
        "interrupted",
        "stopped",
        "needs_attention",
        "ended_for_identical_bundle_correction",
        "ended_for_verifier_revision",
    }
)


def _attention_scope(item: Mapping[str, Any]) -> str:
    """Return the persisted scope, failing closed for legacy attention records."""

    scope = item.get("scope")
    if scope in _ATTENTION_SCOPES:
        return str(scope)
    return "project"


def _halt_required(state: Mapping[str, Any]) -> bool:
    """Derive the global halt flag from unresolved control-call failures."""

    return any(
        call.get("kind")
        in {"main", "trimmer", "advisor-proposal", "advisor-finalize"}
        and call.get("status") == CallState.NEEDS_ATTENTION.value
        for call in state.get("calls", {}).values()
    )


def _apply_call_event(
    state: MutableMapping[str, Any],
    call: MutableMapping[str, Any],
    event: call_machine.CallEvent,
    *,
    persist_effects: bool = True,
) -> call_machine.CallTransition:
    """Apply a Block-6 call transition inside the caller's CAS transaction."""

    try:
        transition = call_machine.reduce_call(call, event)
    except call_machine.CallLeaseRejected as exc:
        raise StaleLeaseError(str(exc)) from exc
    except call_machine.CallResultConflict as exc:
        raise IdempotencyConflict(str(exc)) from exc
    except call_machine.CallTransitionError as exc:
        raise WorkflowError(str(exc)) from exc
    call.clear()
    call.update(copy.deepcopy(dict(transition.call)))
    if persist_effects:
        _append_call_effects(state, transition)
    return transition


def _append_call_effects(
    state: MutableMapping[str, Any], transition: call_machine.CallTransition
) -> None:
    for effect in transition.effects:
        if effect.event_type in _PERSISTED_CALL_EFFECTS:
            append_event(state, effect.event_type, effect.payload)


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return copy.deepcopy(dict(value))
    if hasattr(value, "to_dict"):
        return copy.deepcopy(dict(value.to_dict()))
    if hasattr(value, "__dict__"):
        return copy.deepcopy(vars(value))
    raise TypeError(f"expected mapping-like result, got {type(value).__name__}")


def _canonical_id_from_result(result: Any) -> str | None:
    if isinstance(result, Mapping):
        for key in ("canonical_id", "record_id", "memory_id", "id"):
            if result.get(key):
                return str(result[key])
        return None
    for key in ("canonical_id", "record_id", "memory_id", "id"):
        value = getattr(result, key, None)
        if value:
            return str(value)
    return None


def _validated_completion_evidence(
    payload: Mapping[str, Any], summary: Mapping[str, Any]
) -> list[str]:
    evidence = payload.get("completion_evidence_ids")
    summary_evidence = summary.get("completion_evidence_operation_ids")
    for field, value in (
        ("completion_evidence_ids", evidence),
        ("attempt_summary completion_evidence_operation_ids", summary_evidence),
    ):
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise SchedulerError(f"{field} must be a list of string IDs")
        if len(set(value)) != len(value):
            raise SchedulerError(f"{field} must not contain duplicate IDs")
    if set(evidence) != set(summary_evidence):
        raise SchedulerError(
            "attempt_summary completion evidence must match completion_evidence_ids"
        )
    return list(evidence)


def _status_from_result(result: Any) -> str:
    if isinstance(result, Mapping):
        return str(result.get("status", "committed"))
    return str(getattr(result, "status", "committed"))


def _error_from_result(result: Any) -> str | None:
    if isinstance(result, Mapping):
        value = result.get("error")
    else:
        value = getattr(result, "error", None)
    return None if value is None else str(value)


def _operation_body(payload: Mapping[str, Any]) -> dict[str, Any]:
    body = copy.deepcopy(dict(payload))
    for key in (
        "operation_id",
        "kind",
        "operation_type",
        "proposal_id",
        "candidate_id",
        "candidate_version",
        "predecessor_core_hashes",
        "staging_id",
        "explorer_provenance",
    ):
        body.pop(key, None)
    return body


class Scheduler:
    """Persistent state machine coordinating calls, tasks, and ingestion.

    ``store`` must expose the scheduler-private control-state CAS API and the
    canonical memory operations described by ``MemoryStore``.  ``transport``
    is optional; callers may persist calls and deliver their results through
    :meth:`accept_call_result` themselves.
    """

    CONTROL_KEY = "scheduler.v1"

    def __init__(
        self,
        store: Any,
        transport: Any | None = None,
        *,
        control_key: str = CONTROL_KEY,
        retry_policy: RetryPolicy | None = None,
        limits: SchedulerLimits | None = None,
    ) -> None:
        self.store = store
        self.transport = transport
        self._repository = ControlRepository(store, control_key)
        self._lock = threading.RLock()
        revision, state = self._repository.load()
        if state is None:
            state = default_scheduler_state(retry_policy=retry_policy, limits=limits)
            revision = self._repository.save(None, state)
        state["halt_requested"] = _halt_required(state)
        self._state = state
        self._revision = revision

    @property
    def state(self) -> dict[str, Any]:
        return copy.deepcopy(self._state)

    @property
    def revision(self) -> int:
        return int(self._revision or 0)

    @property
    def gate(self) -> GateState:
        return GateState(self._state["gate"])

    @property
    def event_cursor(self) -> int:
        events = self._state.get("events", [])
        return int(events[-1]["event_id"]) if events else 0

    @contextmanager
    def _mutate(self) -> Iterator[MutableMapping[str, Any]]:
        with self._lock:
            before = self._state
            working = copy.deepcopy(before)
            try:
                yield working
                new_revision = self._repository.save(self._revision, working)
            except BaseException:
                self._state = before
                raise
            self._state = working
            self._revision = new_revision

    def reload(self) -> None:
        with self._lock:
            revision, state = self._repository.load()
            if state is None:
                raise SchedulerError("scheduler control state disappeared")
            state["halt_requested"] = _halt_required(state)
            self._revision, self._state = revision, state

    @staticmethod
    def _human_guidance_snapshot(record: Mapping[str, Any]) -> dict[str, str]:
        return {field: record[field] for field in _HUMAN_GUIDANCE_FIELDS}

    def enqueue_human_guidance(
        self, records: Sequence[Mapping[str, Any]]
    ) -> tuple[str, ...]:
        """Import immutable Markdown snapshots without rewriting known records."""

        normalized: dict[str, dict[str, str]] = {}
        for record in records:
            if set(record) != set(_HUMAN_GUIDANCE_FIELDS) or any(
                not isinstance(record[field], str) or not record[field].strip()
                for field in _HUMAN_GUIDANCE_FIELDS
            ):
                raise SchedulerError("human guidance requires five nonempty text fields")
            snapshot = self._human_guidance_snapshot(record)
            content_digest = hashlib.sha256(snapshot["text"].encode("utf-8")).hexdigest()
            if content_digest != snapshot["sha256"]:
                raise SchedulerError("human guidance content digest does not match")
            path = Path(snapshot["relative_path"])
            if path.is_absolute() or ".." in path.parts or path.suffix != ".md":
                raise SchedulerError("human guidance must name a relative Markdown file")
            try:
                received_at = datetime.fromisoformat(snapshot["received_at"])
            except ValueError as exc:
                raise SchedulerError(
                    "human guidance received_at must be an ISO timestamp"
                ) from exc
            if received_at.tzinfo is None:
                raise SchedulerError("human guidance received_at must include a timezone")
            guidance_id = snapshot["guidance_id"]
            if guidance_id in normalized and normalized[guidance_id] != snapshot:
                raise IdempotencyConflict(f"human guidance {guidance_id} changed")
            normalized[guidance_id] = snapshot
        with self._lock:
            existing = self._state.get("human_guidance_inbox", {})
            for guidance_id, snapshot in normalized.items():
                prior = existing.get(guidance_id)
                if prior is not None and self._human_guidance_snapshot(prior) != snapshot:
                    raise IdempotencyConflict(f"human guidance {guidance_id} changed")
            additions = {
                guidance_id: snapshot
                for guidance_id, snapshot in normalized.items()
                if guidance_id not in existing
            }
            if not additions:
                return ()
            with self._mutate() as state:
                inbox = state.setdefault("human_guidance_inbox", {})
                for guidance_id, snapshot in additions.items():
                    inbox[guidance_id] = {**snapshot, "status": "pending"}
                    append_event(
                        state, "human_guidance_enqueued", {"guidance_id": guidance_id}
                    )
            return tuple(additions)

    @staticmethod
    def _reclaimable_human_guidance_ids(state: Mapping[str, Any]) -> tuple[str, ...]:
        reclaimable: list[str] = []
        for guidance_id, record in state.get("human_guidance_inbox", {}).items():
            if record.get("status") == "claimed":
                call = state.get("calls", {}).get(record.get("call_id"), {})
                if call.get("status") not in {
                    CallState.CANCELLED.value,
                    CallState.SUPERSEDED.value,
                }:
                    continue
                # A cancelled Main has not delivered its assignment. An
                # Explorer that already ran has received the guidance itself.
                if call.get("kind") == "main" or (
                    call.get("kind") == "explorer-worker"
                    and int(call.get("attempt", 0)) == 0
                ):
                    reclaimable.append(str(guidance_id))
            elif record.get("status") == "assigned":
                task = state.get("tasks", {}).get(record.get("task_id"), {})
                if task and not task.get("attempts") and (
                    task.get("state") == TaskState.CLOSED.value
                    or (task.get("mechanical_summary") or {}).get("kind")
                    == "alternation_drain"
                ):
                    reclaimable.append(str(guidance_id))
        return tuple(reclaimable)

    def _reconcile_human_guidance_locked(
        self, state: MutableMapping[str, Any]
    ) -> tuple[str, ...]:
        reclaimed = self._reclaimable_human_guidance_ids(state)
        for guidance_id in reclaimed:
            record = state["human_guidance_inbox"][guidance_id]
            append_event(
                state,
                "human_guidance_requeued",
                {
                    "guidance_id": guidance_id,
                    "call_id": record.pop("call_id", None),
                    "task_id": record.pop("task_id", None),
                },
            )
            record["status"] = "pending"
        return reclaimed

    def reconcile_human_guidance(self) -> tuple[str, ...]:
        """Release only claims whose frozen delivery was permanently fenced."""

        with self._lock:
            if not self._reclaimable_human_guidance_ids(self._state):
                return ()
            with self._mutate() as state:
                return self._reconcile_human_guidance_locked(state)

    def _claim_human_guidance_locked(
        self, state: MutableMapping[str, Any], call_id: str
    ) -> dict[str, str] | None:
        self._reconcile_human_guidance_locked(state)
        pending = [
            record
            for record in state.get("human_guidance_inbox", {}).values()
            if record.get("status") == "pending"
        ]
        if not pending:
            return None
        record = min(
            pending,
            key=lambda item: (
                datetime.fromisoformat(item["received_at"]), item["guidance_id"]
            ),
        )
        record.update(status="claimed", call_id=call_id)
        append_event(
            state,
            "human_guidance_claimed",
            {"guidance_id": record["guidance_id"], "call_id": call_id},
        )
        return self._human_guidance_snapshot(record)

    def _allocate_id(self, state: MutableMapping[str, Any], prefix: str) -> str:
        ids = state.setdefault("ids", {})
        ids[prefix] = int(ids.get(prefix, 0)) + 1
        return f"{prefix}-{ids[prefix]:08d}"

    def _allocate_memory_id(self, state: MutableMapping[str, Any], kind: str) -> str:
        if hasattr(self.store, "allocate_id"):
            return str(self.store.allocate_id(kind))
        prefix = {
            "fact": "F",
            "route": "R",
            "memo": "M",
            "claim": "CL",
            "obligation": "O",
            "task": "T",
            "computation": "C",
            "category": "CAT",
        }[kind]
        return self._allocate_id(state, prefix)

    def _transition_gate(self, state: MutableMapping[str, Any], target: GateState) -> None:
        current = GateState(state["gate"])
        require_gate_transition(current, target)
        if current != target:
            state["gate"] = target.value
            append_event(
                state,
                "gate_transition",
                {"from": current.value, "to": target.value},
            )

    def _transition_task(
        self, state: MutableMapping[str, Any], task: MutableMapping[str, Any], target: TaskState
    ) -> None:
        current = TaskState(task["state"])
        require_task_transition(current, target)
        if current != target:
            task["state"] = target.value
            append_event(
                state,
                "task_transition",
                {"task_id": task["task_id"], "from": current.value, "to": target.value},
            )

    @staticmethod
    def _alternation_phase_of(state: Mapping[str, Any]) -> str | None:
        control = state.get("phase_control")
        if not isinstance(control, Mapping) or control.get("enabled") is not True:
            return None
        return str(control.get("phase") or "")

    @classmethod
    def _require_franta_admission_locked(
        cls,
        state: Mapping[str, Any],
        *,
        sort_task: bool = False,
    ) -> None:
        """Enforce alternation at the scheduler boundary, not only selectors."""

        phase = cls._alternation_phase_of(state)
        if phase is None:
            return
        required = "franta_sort" if sort_task else "franta_run"
        if phase != required:
            raise WorkflowError(
                f"Franta {'sort' if sort_task else 'worker'} admission is closed "
                f"during Explorer phase {phase!r}"
            )

    def bootstrap(
        self,
        *,
        root_problem: str,
        root_obligation_id: str | None = None,
        foundation_policy: Mapping[str, Any] | None = None,
    ) -> str:
        payload = {
            "root_problem": root_problem,
            "root_obligation_id": root_obligation_id,
            "foundation_policy": dict(foundation_policy or {}),
        }
        digest = stable_digest(payload)
        with self._mutate() as state:
            if state.get("bootstrapped"):
                if state.get("bootstrap_digest") != digest:
                    raise IdempotencyConflict("bootstrap replayed with different input")
                return str(state["root"]["obligation_id"])
            obligation_id = root_obligation_id or self._allocate_memory_id(state, "obligation")
            state["root"].update(
                {
                    "problem": root_problem,
                    "obligation_id": obligation_id,
                    "obligation_status": "active",
                }
            )
            state["foundation_policy"] = copy.deepcopy(dict(foundation_policy or {}))
            state["foundation_policy_version"] = 1
            state["bootstrap_digest"] = digest
            state["bootstrapped"] = True
            append_event(state, "bootstrap_committed", {"root_obligation_id": obligation_id})
            return obligation_id

    # ------------------------------------------------------ Explorer alternation

    @staticmethod
    def _reducer_now(now: datetime | None = None) -> datetime:
        value = now or datetime.now(timezone.utc)
        if value.tzinfo is None or value.utcoffset() is None:
            raise SchedulerError("alternation clock must be timezone-aware")
        return value.astimezone(timezone.utc)

    @staticmethod
    def _append_reducer_events_locked(
        state: MutableMapping[str, Any], events: Iterable[Mapping[str, Any]]
    ) -> None:
        for event in events:
            payload = copy.deepcopy(dict(event.get("payload") or {}))
            if event.get("time"):
                payload["reducer_time"] = str(event["time"])
            append_event(state, str(event.get("type") or "alternation_event"), payload)

    def configure_alternation(
        self,
        settings: Mapping[str, Any],
        *,
        now: datetime | None = None,
        defer_start: bool = False,
    ) -> None:
        """Install the opt-in phase state without altering legacy projects."""

        at = self._reducer_now(now)
        explorer_seconds = int(settings["explorer_admission_seconds"])
        franta_seconds = int(settings["franta_admission_seconds"])
        attempt_limit = int(settings["attempts_per_worker"])
        attempt_seconds = int(settings["attempt_seconds"])
        with self._mutate() as state:
            transition = phase_controller.install_phase_control(
                state,
                enabled=True,
                now=at,
                explorer_admission_seconds=explorer_seconds,
                franta_admission_seconds=franta_seconds,
            )
            if transition.changed:
                state.clear()
                state.update(copy.deepcopy(transition.state))
                self._append_reducer_events_locked(state, transition.events)
            expected = explorer_controller.initialize_explorer_state(
                attempt_limit=attempt_limit,
                attempt_seconds=attempt_seconds,
            )
            current = state.get("explorer_control")
            if current is None:
                state["explorer_control"] = expected
                append_event(state, "explorer_control_enabled", {})
            elif current.get("settings") != expected.get("settings"):
                raise IdempotencyConflict(
                    "Explorer attempt settings differ from persisted configuration"
                )
            phase = state["phase_control"]
            if defer_start and "alternation_clock_started_at" not in phase:
                # `franta init` may precede the first foreground run by days.
                # Defer only a pristine first turn; an already-used project is
                # never rewound or granted a fresh admission window.
                pristine = bool(
                    phase.get("phase")
                    == phase_controller.Phase.EXPLORER_ADMISSION.value
                    and int(phase.get("cycle", 0)) == 1
                    and not state["explorer_control"].get("admission_order")
                    and not phase.get("history")
                )
                if pristine:
                    phase["deferred_start"] = True

    def configure_advisor(self, settings: Mapping[str, Any]) -> None:
        """Install the optional peer Advisor control through its adapter port."""

        session_key = str(settings.get("session_key") or "")
        if not session_key:
            raise SchedulerError("Advisor configuration requires a session key")
        with self._mutate() as state:
            phase = state.get("phase_control")
            if not isinstance(phase, Mapping) or phase.get("enabled") is not True:
                raise WorkflowError("Advisor requires Explorer alternation")
            expected = new_advisor_state(session_key=session_key)
            current = state.get("advisor_control")
            if current is None:
                state["advisor_control"] = expected
                append_event(
                    state,
                    "advisor_control_enabled",
                    {"session_key": session_key},
                )
            elif current.get("session_key") != session_key:
                raise IdempotencyConflict(
                    "Advisor session key differs from persisted configuration"
                )

    @property
    def advisor_state(self) -> dict[str, Any]:
        control = self._state.get("advisor_control")
        if not isinstance(control, Mapping):
            raise WorkflowError("Advisor is not enabled")
        return copy.deepcopy(dict(control))

    @staticmethod
    def _research_cycle_of(state: Mapping[str, Any]) -> int:
        phase = state.get("phase_control")
        if isinstance(phase, Mapping) and phase.get("enabled") is True:
            return int(phase.get("cycle", 1))
        return 1

    @classmethod
    def _effective_problem_locked(cls, state: Mapping[str, Any]) -> dict[str, Any]:
        root = str(state.get("root", {}).get("problem") or "")
        if not root:
            raise SchedulerError("the original ROOT problem is not initialized")
        try:
            return portable_effective_problem_descriptor(
                state.get("advisor_control"),
                original_problem=root,
                cycle=cls._research_cycle_of(state),
            )
        except (TypeError, ValueError) as exc:
            raise SchedulerError(str(exc)) from exc

    def effective_problem(self) -> dict[str, Any]:
        """Return the immutable problem assignment visible in this cycle."""

        with self._lock:
            return copy.deepcopy(self._effective_problem_locked(self._state))

    @classmethod
    def _advisor_problem_binding_locked(
        cls,
        state: Mapping[str, Any],
        *,
        task_id: str | None = None,
    ) -> dict[str, Any]:
        """Freeze the visible problem on every call in an Advisor turn.

        Task-owned continuations inherit the task card's assignment so replay
        cannot drift even if a later cycle is eventually opened.  Calls with
        no task owner use the assignment for the currently active cycle.
        Legacy projects deliberately receive no additional input fields.
        """

        if "advisor_control" not in state:
            return {}
        descriptor: Mapping[str, Any] | None = None
        root_problem: object | None = None
        if task_id:
            task = state.get("tasks", {}).get(task_id)
            if isinstance(task, Mapping):
                card = task.get("task_card")
                if isinstance(card, Mapping):
                    candidate = card.get("problem_assignment")
                    if isinstance(candidate, Mapping):
                        descriptor = candidate
                    root_problem = card.get("root_problem")
        if descriptor is None:
            descriptor = cls._effective_problem_locked(state)
        expected_text = descriptor.get("problem_text")
        if not isinstance(expected_text, str) or not expected_text:
            raise SchedulerError("Advisor problem assignment is malformed")
        if root_problem is None:
            root_problem = expected_text
        if root_problem != expected_text:
            raise SchedulerError("task problem text drifted from its Advisor assignment")
        return {
            "root_problem": str(root_problem),
            "problem_assignment": copy.deepcopy(dict(descriptor)),
        }

    def activate_alternation(self, *, now: datetime | None = None) -> bool:
        """Start a pristine deferred Explorer clock at actual run admission."""

        at = self._reducer_now(now)
        with self._mutate() as state:
            phase = state.get("phase_control")
            if not isinstance(phase, MutableMapping) or not phase.get("enabled"):
                return False
            if not phase.get("deferred_start") or phase.get(
                "alternation_clock_started_at"
            ):
                return False
            control = state.get("explorer_control")
            if (
                phase.get("phase")
                != phase_controller.Phase.EXPLORER_ADMISSION.value
                or not isinstance(control, Mapping)
                or control.get("admission_order")
            ):
                raise WorkflowError(
                    "a deferred Explorer clock can start only on a pristine first turn"
                )
            explorer_seconds = int(
                phase["settings"]["explorer_admission_seconds"]
            )
            stamp = at.isoformat()
            phase["entered_at"] = stamp
            phase["explorer"]["admission_started_at"] = stamp
            phase["explorer"]["admission_deadline"] = (
                at + timedelta(seconds=explorer_seconds)
            ).isoformat()
            phase["alternation_clock_started_at"] = stamp
            append_event(
                state,
                "alternation_clock_started",
                {"phase": phase["phase"], "cycle": int(phase["cycle"])},
            )
            return True

    @property
    def alternation_phase(self) -> str | None:
        return self._alternation_phase_of(self._state)

    def tick_alternation(self, *, now: datetime | None = None) -> str | None:
        if self._alternation_phase_of(self._state) is None:
            return None
        phase_snapshot = self._state.get("phase_control", {})
        if phase_snapshot.get("deferred_start") and not phase_snapshot.get(
            "alternation_clock_started_at"
        ):
            return str(phase_snapshot.get("phase") or "")
        at = self._reducer_now(now)
        with self._mutate() as state:
            prior_phase = str(state["phase_control"].get("phase") or "")
            transition = phase_controller.tick(state["phase_control"], now=at)
            if transition.changed:
                state["phase_control"] = copy.deepcopy(transition.state)
                self._append_reducer_events_locked(state, transition.events)
            current_phase = str(state["phase_control"].get("phase") or "")
            if (
                prior_phase == phase_controller.Phase.FRANTA_RUN.value
                and current_phase == phase_controller.Phase.FRANTA_DRAIN.value
            ):
                # Once Franta admission closes, stale planning/trim results must
                # not be recovered in a later Explorer turn or create new
                # assignments after the deadline.  Worker and downstream calls
                # keep their separate graceful-drain semantics.
                for call_id, call in state.get("calls", {}).items():
                    if call.get("kind") not in {"main", "trimmer"} or call.get(
                        "status"
                    ) in {
                        CallState.COMMITTED.value,
                        CallState.CANCELLED.value,
                        CallState.SUPERSEDED.value,
                    }:
                        continue
                    _apply_call_event(
                        state,
                        call,
                        call_machine.Cancel(
                            authorized_by="alternation-controller",
                            reason="Franta admission deadline elapsed",
                        ),
                    )
                    self._resolve_attention(state, f"call:{call_id}")
                    append_event(
                        state,
                        "franta_control_call_cancelled",
                        {"call_id": call_id, "kind": call.get("kind")},
                    )
                state["halt_requested"] = _halt_required(state)
            return current_phase

    def admit_explorer_lineage(self, *, now: datetime | None = None) -> str:
        at = self._reducer_now(now)
        with self._mutate() as state:
            phase = state.get("phase_control")
            control = state.get("explorer_control")
            if not isinstance(phase, Mapping) or not isinstance(control, Mapping):
                raise WorkflowError("Explorer alternation is not enabled")
            if phase.get("deferred_start") and not phase.get(
                "alternation_clock_started_at"
            ):
                raise WorkflowError("Explorer admission clock has not started")
            if not phase_controller.explorer_admission_open(phase, now=at):
                raise WorkflowError("Explorer lineage admission is closed")
            lineage_id = self._allocate_id(state, "XLINEAGE")
            transition = explorer_controller.admit_lineage(
                control,
                lineage_id=lineage_id,
                session_key=f"explorer:{lineage_id}",
                phase_cycle=int(phase["cycle"]),
                phase_epoch=int(phase["phase_epoch"]),
                admission_allowed=True,
                now=at,
                max_slots=int(state["limits"]["max_non_verifier_workers"]),
            )
            state["explorer_control"] = copy.deepcopy(transition.state)
            self._append_reducer_events_locked(state, transition.events)
            return lineage_id

    def start_explorer_attempt(
        self,
        lineage_id: str,
        *,
        source_high_water_seq: int,
        guidance_variant: str | None = None,
        access_grant: Mapping[str, Any] | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Prepare one planned attempt; infrastructure retries stay in its call."""

        at = self._reducer_now(now)
        with self._mutate() as state:
            phase = state.get("phase_control")
            control = state.get("explorer_control")
            if not isinstance(phase, Mapping) or not isinstance(control, Mapping):
                raise WorkflowError("Explorer alternation is not enabled")
            if phase.get("phase") not in {
                phase_controller.Phase.EXPLORER_ADMISSION.value,
                phase_controller.Phase.EXPLORER_DRAIN.value,
            }:
                raise WorkflowError("Explorer attempts cannot start outside Explorer")
            lineage = control.get("lineages", {}).get(lineage_id)
            if not isinstance(lineage, Mapping):
                raise SchedulerError(f"unknown Explorer lineage {lineage_id}")
            attempt_number = int(lineage.get("attempts_started", 0)) + 1
            call_id = self._allocate_id(state, "CALL-EXPLORER")
            transition = explorer_controller.start_attempt(
                control,
                lineage_id=lineage_id,
                call_id=call_id,
                now=at,
                attempt_limit=int(control["settings"]["attempt_limit"]),
                attempt_seconds=int(control["settings"]["attempt_seconds"]),
            )
            turn_id = f"XTURN-{int(phase['cycle']):08d}"
            access_fields: dict[str, Any] = {}
            guidance_fields: dict[str, Any] = {}
            if access_grant is not None:
                required = {
                    "access_policy_version",
                    "access_mode",
                    "grant_id",
                    "grant_digest",
                }
                if set(access_grant) != required:
                    raise SchedulerError(
                        "Explorer access grant metadata has an invalid shape"
                    )
                expected_mode = {
                    1: "check-result",
                    2: "portfolio",
                    3: "full-memory",
                }.get(attempt_number)
                if (
                    access_grant.get("access_policy_version") != 2
                    or access_grant.get("access_mode") != expected_mode
                    or not isinstance(access_grant.get("grant_id"), str)
                    or not access_grant.get("grant_id")
                    or not isinstance(access_grant.get("grant_digest"), str)
                    or len(str(access_grant.get("grant_digest"))) != 64
                ):
                    raise SchedulerError(
                        "Explorer access grant does not match the planned attempt"
                    )
                if guidance_variant not in _EXPLORER_GUIDANCE_VARIANTS_BY_MODE[
                    str(expected_mode)
                ]:
                    raise SchedulerError(
                        "Explorer guidance variant does not match the planned attempt"
                    )
                access_fields = copy.deepcopy(dict(access_grant))
                guidance_fields = {"guidance_variant": str(guidance_variant)}
            elif guidance_variant is not None:
                raise SchedulerError(
                    "legacy Explorer attempts do not accept a guidance variant"
                )
            problem_assignment = self._effective_problem_locked(state)
            exact_input = {
                "root_problem": problem_assignment["problem_text"],
                "explorer_turn_id": turn_id,
                "worker_session_id": lineage_id,
                "attempt_number": attempt_number,
                "first_attempt_clean_room": (
                    access_grant is None and attempt_number == 1
                ),
                "source_high_water_seq": int(source_high_water_seq),
                "prior_attempts": copy.deepcopy(lineage.get("attempts", [])),
                **access_fields,
                **guidance_fields,
            }
            if "advisor_control" in state:
                exact_input["problem_assignment"] = problem_assignment
            if attempt_number == 1:
                human_guidance = self._claim_human_guidance_locked(state, call_id)
                if human_guidance is not None:
                    exact_input["human_guidance"] = human_guidance
            self._prepare_call_locked(
                state,
                "explorer-worker",
                exact_input,
                call_id=call_id,
                retry_limit=int(state["retry_policy"]["worker"]),
                continuation={
                    "lineage_id": lineage_id,
                    "attempt_number": attempt_number,
                    "turn_id": turn_id,
                    "session_key": str(lineage["session_key"]),
                },
            )
            state["explorer_control"] = copy.deepcopy(transition.state)
            self._append_reducer_events_locked(state, transition.events)
            return {
                "call_id": call_id,
                "lineage_id": lineage_id,
                "attempt_number": attempt_number,
                "turn_id": turn_id,
                "session_key": str(lineage["session_key"]),
                "first_attempt_clean_room": (
                    access_grant is None and attempt_number == 1
                ),
                "source_high_water_seq": int(source_high_water_seq),
                **access_fields,
                **guidance_fields,
            }

    def commit_explorer_attempt(
        self,
        call_id: str,
        *,
        outcome: str,
        root_candidate: Mapping[str, Any] | None = None,
        now: datetime | None = None,
    ) -> tuple[str, ...]:
        """Commit an attempt result and optionally fence a root candidate."""

        at = self._reducer_now(now)
        with self._mutate() as state:
            call = state["calls"].get(call_id)
            if not call or call.get("kind") != "explorer-worker":
                raise SchedulerError("Explorer attempt requires an Explorer call")
            if call.get("status") == CallState.COMMITTED.value:
                return ()
            if call.get("status") != CallState.COMPLETED.value:
                raise WorkflowError("Explorer call has no completed result")
            continuation = call.get("continuation") or {}
            lineage_id = str(continuation.get("lineage_id") or "")
            control = state["explorer_control"]
            cancellations: tuple[str, ...] = ()
            if root_candidate is not None:
                candidate_id = str(root_candidate["candidate_id"])
                scratch_id = str(root_candidate["scratch_id"])
                candidate_outcome = str(root_candidate["candidate_outcome"])
                claimed = explorer_controller.claim_root_candidate(
                    control,
                    candidate_id=candidate_id,
                    scratch_id=scratch_id,
                    lineage_id=lineage_id,
                    call_id=call_id,
                    candidate_outcome=candidate_outcome,
                    now=at,
                )
                control = claimed.state
                cancellations = claimed.cancel_call_ids
                self._append_reducer_events_locked(state, claimed.events)
                phase_transition = phase_controller.accept_root_candidate(
                    state["phase_control"],
                    candidate_id=candidate_id,
                    scratch_id=scratch_id,
                    lineage_id=lineage_id,
                    attempt_number=int(continuation["attempt_number"]),
                    candidate_outcome=candidate_outcome,
                    now=at,
                )
                state["phase_control"] = copy.deepcopy(phase_transition.state)
                self._append_reducer_events_locked(state, phase_transition.events)
                outcome = "stopped"
            ended = explorer_controller.acknowledge_attempt_end(
                control,
                lineage_id=lineage_id,
                call_id=call_id,
                outcome=outcome,
                now=at,
            )
            state["explorer_control"] = copy.deepcopy(ended.state)
            self._append_reducer_events_locked(state, ended.events)
            _apply_call_event(state, call, call_machine.CommitResult())
            for other_id in cancellations:
                if other_id == call_id:
                    continue
                other = state["calls"].get(other_id)
                if other and other.get("status") in {
                    CallState.PREPARED.value,
                    CallState.RUNNING.value,
                    CallState.RETRY_PENDING.value,
                    CallState.COMPLETED.value,
                    CallState.NEEDS_ATTENTION.value,
                }:
                    _apply_call_event(
                        state,
                        other,
                        call_machine.Cancel(
                            authorized_by="explorer-controller",
                            reason="another Explorer worker recorded a root candidate",
                        ),
                    )
                    self._resolve_attention(state, f"call:{other_id}")
                    other_continuation = other.get("continuation") or {}
                    other_lineage_id = str(
                        other_continuation.get("lineage_id") or ""
                    )
                    if other_lineage_id:
                        stopped = explorer_controller.acknowledge_attempt_end(
                            state["explorer_control"],
                            lineage_id=other_lineage_id,
                            call_id=other_id,
                            outcome="stopped",
                            now=at,
                        )
                        state["explorer_control"] = copy.deepcopy(stopped.state)
                        self._append_reducer_events_locked(state, stopped.events)
            return tuple(other_id for other_id in cancellations if other_id != call_id)

    def explorer_is_drained(self) -> bool:
        control = self._state.get("explorer_control")
        return bool(
            isinstance(control, Mapping) and explorer_controller.drained(control)
        )

    def fail_explorer_attempt(
        self,
        call_id: str,
        *,
        reason: str,
        now: datetime | None = None,
    ) -> None:
        """Contain one exhausted Explorer call and release only its lineage.

        Transport and invalid-output retries belong to the logical call.  Once
        that retry budget is exhausted, the failed call still consumes exactly
        one of the lineage's three planned attempts; it must not strand the
        reserved Explorer slot or turn the failure into a project-wide pause.
        """

        at = self._reducer_now(now)
        with self._mutate() as state:
            call = state.get("calls", {}).get(call_id)
            if not isinstance(call, MutableMapping) or call.get("kind") != (
                "explorer-worker"
            ):
                raise SchedulerError("Explorer failure requires an Explorer call")
            continuation = call.get("continuation") or {}
            lineage_id = str(continuation.get("lineage_id") or "")
            if not lineage_id:
                raise SchedulerError("Explorer call lacks its lineage owner")

            control = state.get("explorer_control")
            if not isinstance(control, Mapping):
                raise WorkflowError("Explorer alternation is not enabled")
            lineage = control.get("lineages", {}).get(lineage_id, {})
            matching = [
                item
                for item in lineage.get("attempts", [])
                if item.get("call_id") == call_id
            ]
            if len(matching) != 1:
                raise SchedulerError("Explorer failure does not own one planned attempt")
            already_ended = matching[0].get("status") in {"ended", "stopped"}
            if already_ended:
                if matching[0].get("outcome") != "failed":
                    raise IdempotencyConflict(
                        "Explorer failure was replayed with another outcome"
                    )
                self._resolve_attention(state, f"call:{call_id}")
                return

            if call.get("status") not in {
                CallState.NEEDS_ATTENTION.value,
                CallState.RETRY_PENDING.value,
                CallState.RUNNING.value,
                CallState.PREPARED.value,
            }:
                raise WorkflowError(
                    "Explorer failed attempt is not in a containable call state"
                )
            _apply_call_event(
                state,
                call,
                call_machine.Cancel(
                    authorized_by="explorer-controller",
                    reason=str(reason),
                ),
            )
            ended = explorer_controller.acknowledge_attempt_end(
                control,
                lineage_id=lineage_id,
                call_id=call_id,
                outcome="failed",
                now=at,
            )
            state["explorer_control"] = copy.deepcopy(ended.state)
            self._append_reducer_events_locked(state, ended.events)
            self._resolve_attention(state, f"call:{call_id}")
            append_event(
                state,
                "explorer_attempt_failure_contained",
                {
                    "call_id": call_id,
                    "lineage_id": lineage_id,
                    "reason": str(reason),
                },
            )

    def expire_explorer_attempts(
        self, *, now: datetime | None = None
    ) -> tuple[str, ...]:
        """Fence hard-deadline calls and release their lineages atomically."""

        at = self._reducer_now(now)
        with self._mutate() as state:
            control = state.get("explorer_control")
            if not isinstance(control, Mapping):
                return ()
            requested = explorer_controller.request_expired_attempt_stops(
                control, now=at
            )
            state["explorer_control"] = copy.deepcopy(requested.state)
            self._append_reducer_events_locked(state, requested.events)
            expired: list[str] = []
            for call_id in requested.cancel_call_ids:
                call = state.get("calls", {}).get(call_id)
                if not isinstance(call, MutableMapping):
                    continue
                continuation = call.get("continuation") or {}
                lineage_id = str(continuation.get("lineage_id") or "")
                _apply_call_event(
                    state,
                    call,
                    call_machine.Cancel(
                        authorized_by="explorer-controller",
                        reason="three-hour attempt deadline",
                    ),
                )
                ended = explorer_controller.acknowledge_attempt_end(
                    state["explorer_control"],
                    lineage_id=lineage_id,
                    call_id=call_id,
                    outcome="timed_out",
                    now=at,
                )
                state["explorer_control"] = copy.deepcopy(ended.state)
                self._append_reducer_events_locked(state, ended.events)
                expired.append(call_id)
            return tuple(expired)

    def begin_main_sort_task_attempt(
        self,
        task_id: str,
        *,
        sort_run_id: str,
        session_key: str,
        now: datetime | None = None,
    ) -> str:
        """Atomically enter the sort phase and create its task-bound call."""

        at = self._reducer_now(now)
        with self._mutate() as state:
            task = state["tasks"].get(task_id)
            if not task or task.get("agent_system") != "franta-sort":
                raise SchedulerError("main-sort task is missing")
            if task.get("attempts"):
                return str(task["attempts"][-1]["call_id"])
            if not explorer_controller.drained(state["explorer_control"]):
                raise WorkflowError("main-sort requires Explorer drain")
            call_id = self._allocate_id(state, "CALL-SORT")
            phase_transition = phase_controller.begin_franta_sort(
                state["phase_control"],
                explorer_drained=True,
                sort_id=str(sort_run_id),
                sort_call_id=call_id,
                main_session_key=str(session_key),
                now=at,
            )
            state["phase_control"] = copy.deepcopy(phase_transition.state)
            self._append_reducer_events_locked(state, phase_transition.events)
            attempt_no = 1
            task["attempts"].append(
                {
                    "attempt": attempt_no,
                    "state": "running",
                    "started_at": at.isoformat(),
                    "last_sequence": 0,
                    "final_progress_id": None,
                    "summary": None,
                    "supplement": None,
                    "kind": "main_sort",
                    "call_id": call_id,
                    "lease_epoch": 1,
                    "call_attempt": 0,
                }
            )
            task["current_attempt"] = attempt_no
            task["launch_intent"] = False
            self._transition_task(state, task, TaskState.RUNNING)
            call_input = {
                "task_card": copy.deepcopy(task["task_card"]),
                "attempt": attempt_no,
                "supplement": None,
            }
            state["calls"][call_id] = call_machine.prepare_call_record(
                call_id=call_id,
                kind="main-sort",
                payload=call_input,
                input_digest=stable_digest(call_input),
                retry_limit=int(state["retry_policy"]["main"]),
                event_cursor=self._event_cursor_of(state),
                continuation={
                    "task_id": task_id,
                    "task_attempt": attempt_no,
                    "sort_run_id": str(sort_run_id),
                    "session_key": str(session_key),
                },
            )
            append_event(
                state,
                "main_sort_call_prepared",
                {"task_id": task_id, "call_id": call_id, "sort_run_id": sort_run_id},
            )
            return call_id

    def open_franta_run(
        self,
        *,
        sort_call_id: str,
        planning_call_id: str,
        session_key: str,
        now: datetime | None = None,
    ) -> None:
        at = self._reducer_now(now)
        with self._mutate() as state:
            transition = phase_controller.complete_sort_barrier(
                state["phase_control"],
                sort_call_id=sort_call_id,
                planning_call_id=planning_call_id,
                main_session_key=session_key,
                now=at,
            )
            state["phase_control"] = copy.deepcopy(transition.state)
            self._append_reducer_events_locked(state, transition.events)

    def complete_franta_drain(self, *, now: datetime | None = None) -> None:
        at = self._reducer_now(now)
        with self._mutate() as state:
            if "advisor_control" in state:
                raise WorkflowError(
                    "Advisor-enabled Franta drain must commit its problem assignment"
                )
            transition = phase_controller.complete_franta_drain(
                state["phase_control"], franta_drained=True, now=at
            )
            state.setdefault("explorer_history", []).append(
                copy.deepcopy(state["explorer_control"])
            )
            settings = state["explorer_control"]["settings"]
            state["explorer_control"] = explorer_controller.initialize_explorer_state(
                attempt_limit=int(settings["attempt_limit"]),
                attempt_seconds=int(settings["attempt_seconds"]),
            )
            state["phase_control"] = copy.deepcopy(transition.state)
            self._append_reducer_events_locked(state, transition.events)

    @staticmethod
    def _append_advisor_events_locked(
        state: MutableMapping[str, Any], events: Iterable[Mapping[str, Any]]
    ) -> None:
        for event in events:
            payload = copy.deepcopy(dict(event))
            event_type = str(payload.pop("event", "advisor_event"))
            append_event(state, event_type, payload)

    def prepare_advisor_proposal(
        self,
        context: Mapping[str, Any],
        *,
        retry_limit: int,
    ) -> str:
        """Atomically bind one Advisor round and its proposal call."""

        exact_context = copy.deepcopy(dict(context))
        with self._mutate() as state:
            control = state.get("advisor_control")
            if not isinstance(control, Mapping):
                raise WorkflowError("Advisor is not enabled")
            if self._alternation_phase_of(state) != "franta_drain":
                raise WorkflowError("Advisor proposal requires a drained Franta turn")
            source_cycle = self._research_cycle_of(state)
            if (
                exact_context.get("advisor_index") != source_cycle
                or exact_context.get("source_cycle") != source_cycle
                or exact_context.get("target_cycle") != source_cycle + 1
                or exact_context.get("original_problem") != state["root"]["problem"]
            ):
                raise SchedulerError("Advisor context does not match the active cycle")
            call_id = self._allocate_id(state, "CALL-ADVISOR")
            transition = advisor_open_round_transition(
                control,
                exact_context,
                proposal_call_id=call_id,
            )
            self._prepare_call_locked(
                state,
                "advisor-proposal",
                exact_context,
                call_id=call_id,
                retry_limit=retry_limit,
                continuation={
                    "stage": "proposal",
                    "advisor_index": source_cycle,
                    "session_key": str(control["session_key"]),
                },
            )
            state["advisor_control"] = copy.deepcopy(transition.state)
            self._append_advisor_events_locked(state, transition.events)
            return call_id

    def commit_advisor_proposal(
        self,
        call_id: str,
        report: Mapping[str, Any],
        *,
        session_id: str,
        archived_report_path: str,
    ) -> None:
        """Commit one authenticated report and enter the hard human pause."""

        with self._mutate() as state:
            call = state.get("calls", {}).get(call_id)
            if not isinstance(call, MutableMapping) or call.get(
                "kind"
            ) != "advisor-proposal":
                raise SchedulerError("unknown Advisor proposal call")
            control = state.get("advisor_control")
            if not isinstance(control, Mapping):
                raise WorkflowError("Advisor is not enabled")
            session_transition = advisor_bind_session_transition(
                control, session_id=session_id
            )
            report_transition = advisor_accept_report_transition(
                session_transition.state,
                proposal_call_id=call_id,
                report=report,
            )
            updated = copy.deepcopy(report_transition.state)
            active = updated.get("active")
            if isinstance(active, MutableMapping):
                existing_path = active.get("archived_report_path")
                if existing_path not in {None, archived_report_path}:
                    raise IdempotencyConflict("Advisor report archive path changed")
                active["archived_report_path"] = archived_report_path
            _apply_call_event(state, call, call_machine.CommitResult())
            state["advisor_control"] = updated
            self._append_advisor_events_locked(state, session_transition.events)
            self._append_advisor_events_locked(state, report_transition.events)

    def bind_advisor_feedback(self, feedback: Mapping[str, Any]) -> None:
        """Persist an operator's exact one-or-two-choice Advisor response."""

        with self._mutate() as state:
            control = state.get("advisor_control")
            if not isinstance(control, Mapping):
                raise WorkflowError("Advisor is not enabled")
            transition = advisor_bind_feedback_transition(control, feedback)
            state["advisor_control"] = copy.deepcopy(transition.state)
            self._append_advisor_events_locked(state, transition.events)

    def prepare_advisor_finalize(self, *, retry_limit: int) -> str:
        """Atomically bind the post-feedback call to the active Advisor round."""

        with self._mutate() as state:
            control = state.get("advisor_control")
            if not isinstance(control, Mapping):
                raise WorkflowError("Advisor is not enabled")
            if self._alternation_phase_of(state) != "franta_drain":
                raise WorkflowError("Advisor finalization requires Franta drain")
            active = control.get("active")
            if not isinstance(active, Mapping):
                raise WorkflowError("there is no active Advisor round")
            proposal_call_id = str(active.get("proposal_call_id") or "")
            proposal_call = state.get("calls", {}).get(proposal_call_id)
            if not isinstance(proposal_call, Mapping):
                raise SchedulerError("Advisor proposal call is unavailable")
            call_id = self._allocate_id(state, "CALL-ADVISOR")
            transition = advisor_begin_finalize_transition(
                control, finalize_call_id=call_id
            )
            portable_context = copy.deepcopy(dict(active["context"]))
            exact_input = {
                **portable_context,
                "stage": "finalize",
                "root_problem": copy.deepcopy(
                    proposal_call.get("input", {}).get("root_problem")
                ),
                "event_cursor": copy.deepcopy(
                    proposal_call.get("input", {}).get("event_cursor")
                ),
                "selection_report": copy.deepcopy(active["selection_report"]),
                "human_feedback": copy.deepcopy(active["human_feedback"]),
                "host_memory_snapshot": copy.deepcopy(
                    proposal_call.get("input", {}).get("host_memory_snapshot")
                ),
                "problem_assignment": copy.deepcopy(
                    proposal_call.get("input", {}).get("problem_assignment")
                ),
            }
            self._prepare_call_locked(
                state,
                "advisor-finalize",
                exact_input,
                call_id=call_id,
                retry_limit=retry_limit,
                continuation={
                    "stage": "finalize",
                    "advisor_index": int(active["advisor_index"]),
                    "proposal_call_id": proposal_call_id,
                    "session_key": str(control["session_key"]),
                },
            )
            state["advisor_control"] = copy.deepcopy(transition.state)
            self._append_advisor_events_locked(state, transition.events)
            return call_id

    def commit_advisor_assignment_and_complete_drain(
        self,
        call_id: str,
        finalization: Mapping[str, Any],
        *,
        assignment_id: str,
        assignment_file: str,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Publish the next problem and open Explorer atomically."""

        at = self._reducer_now(now)
        with self._mutate() as state:
            call = state.get("calls", {}).get(call_id)
            if not isinstance(call, MutableMapping) or call.get(
                "kind"
            ) != "advisor-finalize":
                raise SchedulerError("unknown Advisor finalize call")
            if call.get("status") == CallState.COMMITTED.value:
                control = state.get("advisor_control", {})
                for item in control.get("history", []):
                    if item.get("finalize_call_id") == call_id:
                        return copy.deepcopy(dict(item["problem_assignment"]))
                raise SchedulerError("committed Advisor call lost its assignment")
            if call.get("status") != CallState.COMPLETED.value:
                raise WorkflowError("Advisor finalize call has no completed result")
            if self._alternation_phase_of(state) != "franta_drain":
                raise WorkflowError("Advisor assignment requires Franta drain")
            control = state.get("advisor_control")
            if not isinstance(control, Mapping):
                raise WorkflowError("Advisor is not enabled")
            advisor_transition = advisor_commit_assignment_transition(
                control,
                finalize_call_id=call_id,
                assignment_id=assignment_id,
                finalization=finalization,
            )
            assignment = advisor_transition.value
            if assignment.target_cycle != self._research_cycle_of(state) + 1:
                raise SchedulerError("Advisor assignment targets the wrong cycle")
            updated = copy.deepcopy(advisor_transition.state)
            history_item = updated.get("history", [])[-1]
            existing_file = history_item.get("assignment_file")
            if existing_file not in {None, assignment_file}:
                raise IdempotencyConflict("Advisor assignment file changed")
            history_item["assignment_file"] = assignment_file
            _apply_call_event(state, call, call_machine.CommitResult())
            phase_transition = phase_controller.complete_franta_drain(
                state["phase_control"], franta_drained=True, now=at
            )
            state.setdefault("explorer_history", []).append(
                copy.deepcopy(state["explorer_control"])
            )
            settings = state["explorer_control"]["settings"]
            state["explorer_control"] = explorer_controller.initialize_explorer_state(
                attempt_limit=int(settings["attempt_limit"]),
                attempt_seconds=int(settings["attempt_seconds"]),
            )
            state["advisor_control"] = updated
            state["phase_control"] = copy.deepcopy(phase_transition.state)
            self._append_advisor_events_locked(state, advisor_transition.events)
            self._append_reducer_events_locked(state, phase_transition.events)
            return copy.deepcopy(assignment.to_dict())

    def stop_unlaunched_franta_tasks_for_drain(self) -> tuple[str, ...]:
        """Close accepted continuations that had no running process at cutoff.

        A graceful Franta drain lets calls already executing finish, but it must
        not carry launching/retry/revision work through an Explorer turn and
        silently relaunch it in the next Franta window.  Existing staged memory
        operations retain their normal terminalization before task closure.
        """

        stopped_ids: list[str] = []
        pending_states = {
            TaskState.QUEUED,
            TaskState.LAUNCHING,
            TaskState.RETRY_PENDING,
            TaskState.REVISION_PENDING,
        }
        with self._mutate() as state:
            if self._alternation_phase_of(state) != "franta_drain":
                return ()
            for task_id, task in state.get("tasks", {}).items():
                if task.get("agent_system") == "franta-sort" or task.get(
                    "non_slot_task"
                ):
                    continue
                current = TaskState(task["state"])
                if current not in pending_states:
                    continue
                # A RUNNING attempt is the graceful tail and is handled by its
                # owning worker call, never by this unlaunched-work fence.
                if task.get("attempts") and task["attempts"][-1].get(
                    "state"
                ) == "running":
                    continue
                task["slot_reserved"] = False
                task.pop("pending_attempt_supplement", None)
                task["forced_outcome"] = TaskOutcome.INTERRUPTED.value
                task["mechanical_summary"] = {
                    "kind": "alternation_drain",
                    "reason": "Franta admission deadline elapsed before launch",
                    "last_progress_id": task.get("last_progress_id"),
                }
                self._transition_task(state, task, TaskState.STOPPING)
                self._transition_task(state, task, TaskState.ATTEMPT_ENDED)
                self._transition_task(state, task, TaskState.POSTPROCESSING)
                append_event(
                    state,
                    "franta_unlaunched_task_stopped",
                    {"task_id": task_id, "prior_state": current.value},
                )
                stopped_ids.append(str(task_id))
        for task_id in stopped_ids:
            self._maybe_close_task(task_id)
        return tuple(stopped_ids)

    def commit_initial_trim(self, portfolio: Mapping[str, Any]) -> None:
        """Explicitly install a first category portfolio.

        Franta now starts with an open assignment gate, so runtime startup never
        calls this method.  Keeping the explicit category operation lets the
        dormant category subsystem remain testable without making it part of
        Main's control path.
        """
        with self._mutate() as state:
            if not state.get("bootstrapped"):
                raise SchedulerError("bootstrap must complete before initial trim")
            gate = GateState(state["gate"])
            if gate not in {GateState.OPEN, GateState.TRIMMING}:
                raise WorkflowError("initial trim requires an open or trimming gate")
            existing = state["trim"].get("portfolio")
            if existing is not None:
                if existing != dict(portfolio):
                    raise IdempotencyConflict(
                        "initial trim replayed with a different portfolio"
                    )
                return
            transition = trim_control.commit_initial_portfolio(
                state["trim"], portfolio
            )
            state["trim"] = copy.deepcopy(dict(transition.trim))
            _append_trim_effects(state, transition.effects)
            assert transition.gate_target is not None
            self._transition_gate(state, transition.gate_target)

    def open_assignment_without_trim(self) -> None:
        """Open initial Main admission without creating a category portfolio."""

        with self._mutate() as state:
            if not state.get("bootstrapped"):
                raise SchedulerError("bootstrap must complete before Main admission")
            gate = GateState(state["gate"])
            if gate == GateState.OPEN:
                return
            trim = state["trim"]
            if (
                gate != GateState.TRIMMING
                or trim.get("portfolio") is not None
                or trim.get("active_review") is not None
                or trim.get("active_trim") is not None
            ):
                raise WorkflowError(
                    "only an untouched initial trimming gate may open without trim"
                )
            self._transition_gate(state, GateState.OPEN)
            append_event(state, "assignment_opened_without_trim", {})

    def create_main_memory_snapshot(
        self,
        destination: str | Path,
        *,
        snapshot_id: str,
        expected_event_cursor: int,
    ) -> MainMemorySnapshot:
        """Freeze complete canonical memory at one scheduler wake boundary."""

        _, snapshot = self.capture_main_memory_snapshot(
            destination,
            snapshot_id=snapshot_id,
            expected_event_cursor=expected_event_cursor,
        )
        return snapshot

    def capture_main_memory_snapshot(
        self,
        destination: str | Path,
        *,
        snapshot_id: str,
        expected_event_cursor: int | None = None,
    ) -> tuple[dict[str, Any], MainMemorySnapshot]:
        """Atomically copy control state and its complete canonical-memory view."""

        with self._lock:
            current_cursor = self.event_cursor
            if (
                expected_event_cursor is not None
                and int(expected_event_cursor) != current_cursor
            ):
                raise SchedulerError(
                    "Main snapshot request does not match the scheduler event cursor"
                )
            state = copy.deepcopy(self._state)
            snapshot = build_main_memory_snapshot(
                self.store,
                destination,
                source_event_cursor=current_cursor,
                snapshot_id=snapshot_id,
            )
            return state, snapshot

    # ------------------------------------------------------------------ calls

    def prepare_call(
        self,
        kind: str,
        payload: Mapping[str, Any],
        *,
        call_id: str | None = None,
        retry_limit: int | None = None,
        continuation: Mapping[str, Any] | None = None,
        expected_event_cursor: int | None = None,
    ) -> str:
        exact_input = copy.deepcopy(dict(payload))
        with self._mutate() as state:
            if expected_event_cursor is not None:
                events = state.get("events", [])
                current_cursor = int(events[-1]["event_id"]) if events else 0
                if int(expected_event_cursor) != current_cursor:
                    raise SchedulerError(
                        "call input no longer matches the scheduler event cursor"
                    )
            if (
                kind == "main"
                and state.get("human_guidance_inbox")
                and (call_id is None or call_id not in state["calls"])
                and exact_input.get("reserved_batch_id")
                and not exact_input.get("terminal_resolution_call")
                and state["gate"] == GateState.OPEN.value
                and int(exact_input.get("free_non_verifier_slots", 0)) > 0
                and self._running_count_of(state)
                < int(state["limits"]["max_non_verifier_workers"])
            ):
                self._reconcile_human_guidance_locked(state)
                if any(
                    record.get("status") == "pending"
                    for record in state.get("human_guidance_inbox", {}).values()
                ):
                    call_id = call_id or self._allocate_id(state, "CALL")
                    exact_input["human_guidance"] = self._claim_human_guidance_locked(
                        state, call_id
                    )
            return self._prepare_call_locked(
                state,
                kind,
                exact_input,
                call_id=call_id,
                retry_limit=retry_limit,
                continuation=continuation,
            )

    def _prepare_call_locked(
        self,
        state: MutableMapping[str, Any],
        kind: str,
        exact_input: Mapping[str, Any],
        *,
        call_id: str | None = None,
        retry_limit: int | None = None,
        continuation: Mapping[str, Any] | None = None,
    ) -> str:
        """Create one call inside its owner's control-state transaction."""

        payload = copy.deepcopy(dict(exact_input))
        digest = stable_digest(payload)
        calls = state["calls"]
        control_family = (
            "advisor"
            if kind in {"advisor-proposal", "advisor-finalize"}
            else kind
        )
        if control_family in {"main", "trimmer", "advisor"} and any(
            (
                "advisor"
                if call["kind"] in {"advisor-proposal", "advisor-finalize"}
                else call["kind"]
            )
            == control_family
            and call["status"]
            in {
                CallState.PREPARED.value,
                CallState.RUNNING.value,
                CallState.RETRY_PENDING.value,
                CallState.COMPLETED.value,
            }
            for call in calls.values()
        ):
            raise WorkflowError(f"only one live {control_family} call is allowed")
        if kind in {"verifier", "challenge-verifier"}:
            active_verifiers = sum(
                1
                for call in calls.values()
                if call["kind"] in {"verifier", "challenge-verifier"}
                and call["status"]
                in {
                    CallState.PREPARED.value,
                    CallState.RUNNING.value,
                    CallState.RETRY_PENDING.value,
                    CallState.COMPLETED.value,
                }
            )
            if active_verifiers >= int(state["limits"]["max_parallel_verifiers"]):
                raise CapacityError("max_parallel_verifiers is reached")
        call_id = call_id or self._allocate_id(state, "CALL")
        existing = calls.get(call_id)
        if existing:
            if existing["input_digest"] != digest or existing["kind"] != kind:
                raise IdempotencyConflict(f"call {call_id} replayed with different input")
            return call_id
        retry_key = {
            "challenge-verifier": "verifier",
            "main-closure-review": "main",
        }.get(kind, kind.replace("-", "_"))
        configured = int(state["retry_policy"].get(retry_key, 0))
        if retry_limit is None:
            if configured <= 0:
                raise SchedulerError(f"retry limit required for call kind {kind!r}")
            retry_limit = configured
        if retry_limit <= 0:
            raise SchedulerError("retry limit must be positive")
        calls[call_id] = call_machine.prepare_call_record(
            call_id=call_id,
            kind=kind,
            payload=payload,
            input_digest=digest,
            retry_limit=int(retry_limit),
            event_cursor=self._event_cursor_of(state),
            continuation=continuation,
        )
        append_event(state, "call_prepared", {"call_id": call_id, "kind": kind})
        return call_id

    def prepare_reconciled_call(
        self,
        kind: str,
        payload: Mapping[str, Any],
        *,
        continuation: Mapping[str, Any],
        identity_fields: Sequence[str] = (),
    ) -> str:
        """Reuse one exact cross-store continuation call before creating it.

        Bootstrap proposal ownership is stored under a separate canonical-store
        CAS key, so it cannot share the scheduler transaction used by ordinary
        operation owners.  Exact-input reconciliation prevents that unavoidable
        cross-key window from creating or relaunching a second logical review.
        """

        exact_input = copy.deepcopy(dict(payload))
        exact_continuation = copy.deepcopy(dict(continuation))
        digest = stable_digest(exact_input)
        identity = {
            str(field): copy.deepcopy(exact_input.get(str(field)))
            for field in identity_fields
        }
        eligible = {
            CallState.PREPARED.value,
            CallState.RUNNING.value,
            CallState.RETRY_PENDING.value,
            CallState.COMPLETED.value,
            CallState.COMMITTED.value,
            CallState.NEEDS_ATTENTION.value,
        }
        priority = {
            CallState.COMMITTED.value: 0,
            CallState.COMPLETED.value: 1,
            CallState.RUNNING.value: 2,
            CallState.RETRY_PENDING.value: 3,
            CallState.PREPARED.value: 4,
            CallState.NEEDS_ATTENTION.value: 5,
        }
        with self._mutate() as state:
            candidates = [
                (str(call_id), call)
                for call_id, call in state["calls"].items()
                if call.get("kind") == kind
                and call.get("continuation") == exact_continuation
                and call.get("status") in eligible
                and (
                    all(
                        call.get("input", {}).get(field) == value
                        for field, value in identity.items()
                    )
                    if identity
                    else call.get("input_digest") == digest
                )
            ]
            if not candidates:
                return self._prepare_call_locked(
                    state,
                    kind,
                    exact_input,
                    continuation=exact_continuation,
                )
            candidates.sort(
                key=lambda item: (priority[str(item[1]["status"])], item[0])
            )
            retained_id = candidates[0][0]
            for duplicate_id, duplicate in candidates[1:]:
                _apply_call_event(
                    state,
                    duplicate,
                    call_machine.Fence(CallState.SUPERSEDED),
                )
                append_event(
                    state,
                    "duplicate_continuation_call_superseded",
                    {
                        "call_id": duplicate_id,
                        "retained_call_id": retained_id,
                        "continuation": exact_continuation,
                    },
                )
            append_event(
                state,
                "continuation_call_reconciled",
                {"call_id": retained_id, "continuation": exact_continuation},
            )
            return retained_id

    def _prepare_owned_call(
        self,
        kind: str,
        payload: Mapping[str, Any],
        *,
        owner_collection: str,
        owner_id: str,
        pointer_field: str,
        continuation: Mapping[str, Any],
        owner_updates: Mapping[str, Any] | None = None,
    ) -> str:
        """Atomically attach a logical call, reusing a legacy orphan if present."""

        exact_input = copy.deepcopy(dict(payload))
        exact_continuation = copy.deepcopy(dict(continuation))
        digest = stable_digest(exact_input)
        active_states = {
            CallState.PREPARED.value,
            CallState.RUNNING.value,
            CallState.RETRY_PENDING.value,
            CallState.COMPLETED.value,
            CallState.NEEDS_ATTENTION.value,
        }
        with self._mutate() as state:
            owner = state.get(owner_collection, {}).get(owner_id)
            if owner is None:
                raise SchedulerError(f"unknown continuation owner {owner_id}")
            existing_id = owner.get(pointer_field)
            if existing_id:
                call = state["calls"].get(str(existing_id))
                if not call:
                    raise SchedulerError(
                        f"continuation owner {owner_id} references missing call {existing_id}"
                    )
                if (
                    call.get("kind") != kind
                    or call.get("input_digest") != digest
                    or call.get("continuation") != exact_continuation
                ):
                    raise IdempotencyConflict(
                        f"continuation owner {owner_id} references a different call"
                    )
                if owner_updates:
                    owner.update(copy.deepcopy(dict(owner_updates)))
                return str(existing_id)

            candidates = sorted(
                str(call_id)
                for call_id, call in state["calls"].items()
                if call.get("kind") == kind
                and call.get("input_digest") == digest
                and call.get("continuation") == exact_continuation
                and call.get("status") in active_states
            )
            if candidates:
                call_id = candidates[0]
                for duplicate_id in candidates[1:]:
                    duplicate = state["calls"][duplicate_id]
                    _apply_call_event(
                        state,
                        duplicate,
                        call_machine.Fence(CallState.SUPERSEDED),
                    )
                    append_event(
                        state,
                        "duplicate_continuation_call_superseded",
                        {
                            "call_id": duplicate_id,
                            "retained_call_id": call_id,
                            "owner_id": owner_id,
                        },
                    )
                append_event(
                    state,
                    "continuation_call_reconciled",
                    {"call_id": call_id, "owner_id": owner_id},
                )
            else:
                call_id = self._prepare_call_locked(
                    state,
                    kind,
                    exact_input,
                    continuation=exact_continuation,
                )
            owner[pointer_field] = call_id
            if owner_updates:
                owner.update(copy.deepcopy(dict(owner_updates)))
            append_event(
                state,
                "continuation_call_attached",
                {
                    "call_id": call_id,
                    "owner_collection": owner_collection,
                    "owner_id": owner_id,
                    "pointer_field": pointer_field,
                },
            )
            return call_id

    @staticmethod
    def _event_cursor_of(state: Mapping[str, Any]) -> int:
        events = state.get("events", [])
        return int(events[-1]["event_id"]) if events else 0

    def mark_call_running(self, call_id: str) -> tuple[int, int]:
        with self._mutate() as state:
            call = state["calls"].get(call_id)
            if not call:
                raise SchedulerError(f"unknown call {call_id}")
            if call.get("kind") == "verifier":
                operation_id = str(
                    call.get("continuation", {}).get("operation_id") or ""
                )
                operation = state.get("operations", {}).get(operation_id)
                if (
                    not isinstance(operation, Mapping)
                    or operation.get("state") != OperationState.VERIFYING.value
                    or not self._source_attempt_is_terminal_locked(state, operation)
                ):
                    raise WorkflowError(
                        f"operation {operation_id} cannot be verified before its worker attempt ends"
                    )
            _apply_call_event(state, call, call_machine.Launch())
            return int(call["lease_epoch"]), int(call["attempt"])

    def _require_current_explorer_write_locked(
        self,
        call_id: str,
        lease_epoch: int,
        launch_attempt: int,
    ) -> None:
        call = self._state.get("calls", {}).get(call_id)
        if not isinstance(call, Mapping) or call.get("kind") != "explorer-worker":
            raise SchedulerError("unknown Explorer worker call")
        if (
            call.get("status") != CallState.RUNNING.value
            or int(call.get("lease_epoch", 0)) != int(lease_epoch)
            or int(call.get("attempt", 0)) != int(launch_attempt)
        ):
            raise SchedulerError(
                "Explorer write capability is stale or its call is not running"
            )

    def validate_explorer_skill_write(
        self,
        call_id: str,
        lease_epoch: int,
        launch_attempt: int,
    ) -> None:
        """Reject stale broker callbacks before inspecting their staged bytes."""

        with self._lock:
            self._require_current_explorer_write_locked(
                call_id, lease_epoch, launch_attempt
            )

    @contextmanager
    def explorer_skill_write_guard(
        self,
        call_id: str,
        lease_epoch: int,
        launch_attempt: int,
    ) -> Iterator[None]:
        """Linearize Explorer prepare/receipt/trust against call fencing.

        Deadline, root-candidate, recovery, and retry transitions all acquire
        the same scheduler lock through ``_mutate``.  Holding it across the
        short trusted commit tail makes either the callback or the fence win
        completely; a cancelled call can never make a prepared record visible.
        """

        with self._lock:
            self._require_current_explorer_write_locked(
                call_id, lease_epoch, launch_attempt
            )
            yield

    def accept_call_result(self, call_id: str, lease_epoch: int, result: Any) -> None:
        exact_result = _as_dict(result)
        digest = stable_digest(exact_result)
        with self._mutate() as state:
            call = state["calls"].get(call_id)
            if not call:
                raise SchedulerError(f"unknown call {call_id}")
            _apply_call_event(
                state,
                call,
                call_machine.ReceiveResult(
                    lease_epoch=int(lease_epoch),
                    result=exact_result,
                    result_digest=digest,
                ),
            )

    def mark_call_committed(self, call_id: str) -> None:
        with self._mutate() as state:
            call = state["calls"].get(call_id)
            if not call:
                raise SchedulerError(f"unknown call {call_id}")
            _apply_call_event(state, call, call_machine.CommitResult())

    @staticmethod
    def _call_attention_owner(
        state: Mapping[str, Any], call: Mapping[str, Any]
    ) -> tuple[str, str | None]:
        kind = str(call.get("kind") or "")
        continuation = call.get("continuation")
        if not isinstance(continuation, Mapping):
            return "project", None
        if kind in {"synthesizer", "verifier"}:
            operation = state.get("operations", {}).get(
                continuation.get("operation_id")
            )
            if isinstance(operation, Mapping) and operation.get("task_id"):
                return "task", str(operation["task_id"])
        elif kind in {"main-closure-review", "worker"}:
            task_id = continuation.get("task_id")
            if task_id in state.get("tasks", {}):
                return "task", str(task_id)
        # Challenge verification protects a canonical fact that may be used by
        # every task. Sprint controls likewise remain project-scoped, so a
        # degraded lane cannot advance into summary or trimming.
        return "project", None

    def mark_call_failed(self, call_id: str, lease_epoch: int, reason: str) -> bool:
        """Record infrastructure failure; return whether another retry is allowed."""
        with self._mutate() as state:
            call = state["calls"].get(call_id)
            if not call:
                raise SchedulerError(f"unknown call {call_id}")
            transition = _apply_call_event(
                state,
                call,
                call_machine.RecordTransportFailure(
                    lease_epoch=int(lease_epoch),
                    reason=str(reason),
                    occurred_at=utc_now(),
                ),
                persist_effects=False,
            )
            can_retry = bool(transition.retry_allowed)
            if not can_retry:
                scope, owner_id = self._call_attention_owner(state, call)
                self._add_attention(
                    state,
                    f"call:{call_id}",
                    "transport_retry_exhausted",
                    {"kind": call["kind"], "reason": reason},
                    scope=scope,
                    owner_id=owner_id,
                )
                if call["kind"] in {
                    "main",
                    "trimmer",
                    "advisor-proposal",
                    "advisor-finalize",
                }:
                    state["halt_requested"] = True
                continuation = call.get("continuation", {})
                operation_id = continuation.get("operation_id")
                if operation_id and operation_id in state["operations"]:
                    state["operations"][operation_id]["state"] = (
                        OperationState.NEEDS_ATTENTION.value
                    )
                    task_id = state["operations"][operation_id]["task_id"]
                    task = state["tasks"].get(task_id)
                    if task and task["state"] == TaskState.POSTPROCESSING.value:
                        self._transition_task(state, task, TaskState.NEEDS_ATTENTION)
                exploration_state.mark_summarizer_needs_attention(state, call)
                challenge_id = continuation.get("challenge_id")
                if (
                    call["kind"] == "challenge-verifier"
                    and challenge_id in state["challenges"]
                ):
                    state["challenges"][challenge_id]["state"] = (
                        OperationState.NEEDS_ATTENTION.value
                    )
            _append_call_effects(state, transition)
            return can_retry

    def reject_call_result(self, call_id: str, reason: str) -> bool:
        """Reject a structurally or semantically invalid agent result.

        Invalid control output consumes the same persisted retry budget as a
        transport failure.  It never becomes a mathematical verdict.  The
        rejected value and its digest remain in ``invalid_results`` for audit,
        while the next launch uses the exact persisted call input under a new
        fenced lease epoch.
        """

        with self._mutate() as state:
            call = state["calls"].get(call_id)
            if not call:
                raise SchedulerError(f"unknown call {call_id}")
            transition = _apply_call_event(
                state,
                call,
                call_machine.RejectInvalidResult(
                    reason=str(reason),
                    occurred_at=utc_now(),
                ),
                persist_effects=False,
            )
            can_retry = bool(transition.retry_allowed)
            if not can_retry:
                scope, owner_id = self._call_attention_owner(state, call)
                self._add_attention(
                    state,
                    f"call:{call_id}",
                    "invalid_output_retry_exhausted",
                    {"kind": call["kind"], "reason": str(reason)},
                    scope=scope,
                    owner_id=owner_id,
                )
                if call["kind"] in {
                    "main",
                    "trimmer",
                    "advisor-proposal",
                    "advisor-finalize",
                }:
                    state["halt_requested"] = True
                continuation = call.get("continuation", {})
                operation_id = continuation.get("operation_id")
                if operation_id and operation_id in state["operations"]:
                    operation = state["operations"][operation_id]
                    operation["state"] = OperationState.NEEDS_ATTENTION.value
                    task = state["tasks"].get(operation.get("task_id"))
                    if task and task["state"] == TaskState.POSTPROCESSING.value:
                        self._transition_task(state, task, TaskState.NEEDS_ATTENTION)
                challenge_id = continuation.get("challenge_id")
                if challenge_id and challenge_id in state["challenges"]:
                    state["challenges"][challenge_id]["state"] = (
                        OperationState.NEEDS_ATTENTION.value
                    )
                exploration_state.mark_summarizer_needs_attention(state, call)
                task_id = continuation.get("task_id")
                if call["kind"] == "main-closure-review" and task_id in state["tasks"]:
                    task = state["tasks"][task_id]
                    if task["state"] == TaskState.POSTPROCESSING.value:
                        self._transition_task(state, task, TaskState.NEEDS_ATTENTION)
            _append_call_effects(state, transition)
            return can_retry

    def invoke_call(self, call_id: str) -> dict[str, Any]:
        """Run a persisted call synchronously, retrying only transport failures."""
        if self.transport is None:
            raise SchedulerError("no transport configured")
        while True:
            lease_epoch, attempt = self.mark_call_running(call_id)
            call = self._state["calls"][call_id]
            try:
                result = self.transport.call(
                    call["kind"],
                    copy.deepcopy(call["input"]),
                    call_id=call_id,
                    lease_epoch=lease_epoch,
                    attempt=attempt,
                )
                if not isinstance(result, Mapping):
                    raise TransportFailure("transport returned a non-mapping result")
                self.accept_call_result(call_id, lease_epoch, result)
                return copy.deepcopy(dict(result))
            except Exception as exc:
                if not self.mark_call_failed(call_id, lease_epoch, str(exc)):
                    raise TransportFailure(f"retry limit exhausted for {call_id}") from exc

    # ------------------------------------------------------------ assignments

    def running_non_verifier_count(self) -> int:
        return sum(
            1
            for task in self._state["tasks"].values()
            if task.get("slot_reserved") and task.get("state") != TaskState.CLOSED.value
        )

    def free_non_verifier_slots(self) -> int:
        return max(
            0,
            int(self._state["limits"]["max_non_verifier_workers"])
            - self.running_non_verifier_count(),
        )

    @staticmethod
    def _portfolio_size(report: Mapping[str, Any]) -> int:
        portfolio = report.get("portfolio", report.get("assignment_portfolio", {})) or {}
        return sum(len(value or []) for value in portfolio.values() if isinstance(value, list))

    def _validate_assign_report(
        self,
        state: Mapping[str, Any],
        report: Mapping[str, Any],
        *,
        sprint: bool,
    ) -> None:
        mode = str(report.get("mode", report.get("work_mode", "")))
        if mode not in NON_VERIFIER_MODES:
            raise SchedulerError(f"unknown non-verifier worker mode {mode!r}")
        if self._portfolio_size(report) > int(state["limits"]["portfolio_max_memories"]):
            raise SchedulerError("assignment portfolio exceeds configured hard limit")
        obligations = report.get("main_obligation_ids") or (
            [report["main_obligation_id"]] if report.get("main_obligation_id") else []
        )
        routes = report.get("main_route_ids") or (
            [report["main_route_id"]] if report.get("main_route_id") else []
        )
        portfolio = report.get("portfolio", report.get("assignment_portfolio", {})) or {}
        if mode == "research" and not routes:
            raise SchedulerError("research assignment requires a main route")
        if mode == "brainstorm":
            if len(obligations) != 1:
                raise SchedulerError("brainstorm requires exactly one main obligation")
            forbidden = {k for k, v in portfolio.items() if k != "fact" and v}
            if forbidden:
                raise SchedulerError("brainstorm portfolio may contain only facts")
        if mode == "multi-discipline":
            if not obligations or not str(report.get("perspective", "")).strip():
                raise SchedulerError("multi-discipline requires obligations and a perspective")
            forbidden = {k for k, v in portfolio.items() if k not in {"fact", "obligation"} and v}
            if forbidden:
                raise SchedulerError("multi-discipline portfolio may contain only facts/targets")
        if mode == "computation" and not (routes or obligations):
            raise SchedulerError("computation requires a route or obligation")
        if mode == "proof-writer":
            root_id = state["root"].get("solution_fact_id")
            if not root_id or GateState(state["gate"]) != GateState.RESOLUTION_PENDING:
                raise SchedulerError("proof-writer requires an active root solution")
        if sprint and mode in {"brainstorm", "multi-discipline", "computation", "associate"}:
            policy = report.get("access_policy", {})
            if policy.get("canonical_memory", False) or policy.get("internal_search", False):
                raise SchedulerError("discovery sprint lanes must be sealed from canonical memory")
        for route_id in routes:
            self._validate_memory_id(str(route_id), "route", active_fact=False)
        for obligation_id in obligations:
            self._validate_memory_id(str(obligation_id), "obligation", active_fact=False)
        try:
            validate_assignment_portfolio(
                portfolio,
                record_lookup=self.store.get if hasattr(self.store, "get") else None,
            )
        except SnapshotValidationError as exc:
            raise SchedulerError(str(exc)) from exc

    def _validate_memory_id(
        self, memory_id: str, expected_type: str, *, active_fact: bool
    ) -> None:
        if not hasattr(self.store, "get"):
            raise SchedulerError("Store must provide get() for portfolio validation")
        try:
            record = self.store.get(memory_id)
        except Exception as exc:
            raise SchedulerError(f"invalid {expected_type} ID {memory_id}: {exc}") from exc
        if record is None:
            raise SchedulerError(f"unknown {expected_type} ID {memory_id}")
        try:
            validate_record_reference(
                record,
                memory_id=memory_id,
                expected_type=expected_type,
                active_fact=active_fact,
            )
        except SnapshotValidationError as exc:
            raise SchedulerError(str(exc)) from exc

    @staticmethod
    def _sprint_portfolio(item: Mapping[str, Any], *, lane: str) -> dict[str, list[str]]:
        try:
            return exploration_validation.sprint_portfolio(item, lane=lane)
        except exploration_validation.ExplorationValidationError as exc:
            raise SchedulerError(str(exc)) from exc

    @staticmethod
    def _sprint_perspective(item: Mapping[str, Any], *, lane: str) -> Any:
        try:
            return exploration_validation.sprint_perspective(item, lane=lane)
        except exploration_validation.ExplorationValidationError as exc:
            raise SchedulerError(str(exc)) from exc

    def _validate_sprint_target(self, target: Mapping[str, Any]) -> str:
        try:
            return exploration_validation.validate_sprint_target(
                target,
                record_lookup=lambda memory_id: self.store.get(memory_id),
            )
        except exploration_validation.ExplorationValidationError as exc:
            raise SchedulerError(str(exc)) from exc

    def _validate_sprint_lane_blueprints(
        self, target_id: str, lanes: Sequence[Mapping[str, Any]]
    ) -> None:
        try:
            exploration_validation.validate_sprint_lane_blueprints(
                target_id,
                lanes,
                validate_memory_id=lambda memory_id, memory_type, active_fact: (
                    self._validate_memory_id(
                        memory_id,
                        memory_type,
                        active_fact=active_fact,
                    )
                ),
            )
        except exploration_validation.ExplorationValidationError as exc:
            raise SchedulerError(str(exc)) from exc

    def _validate_sprint_reports_against_plan(
        self,
        plan: Mapping[str, Any],
        reports: Sequence[Mapping[str, Any]],
    ) -> None:
        try:
            exploration_validation.validate_sprint_reports_against_plan(plan, reports)
        except exploration_validation.ExplorationValidationError as exc:
            raise SchedulerError(str(exc)) from exc

    def _fact_dependency_closure_ids(self, root_fact_id: str) -> set[str]:
        """Return the exact active fact closure exposed to a proof-writer."""

        pending = [str(root_fact_id)]
        closure: set[str] = set()
        while pending:
            fact_id = pending.pop()
            if fact_id in closure:
                continue
            if not hasattr(self.store, "get"):
                raise SchedulerError("Store must provide get() for proof-writer access")
            try:
                record = self.store.get(fact_id)
            except Exception as exc:
                raise SchedulerError(
                    f"proof-writer fact closure contains unavailable fact {fact_id}: {exc}"
                ) from exc
            if record is None:
                raise SchedulerError(
                    f"proof-writer fact closure contains unavailable fact {fact_id}"
                )
            value = _as_dict(record)
            actual = str(value.get("type", value.get("memory_type", "fact")))
            if actual.startswith("MemoryType."):
                actual = actual.rsplit(".", 1)[-1].lower()
            status = value.get("status", "active")
            if actual != "fact" or value.get("active", True) is False or status in {
                "inactive",
                "revoked",
            }:
                raise SchedulerError(
                    f"proof-writer fact closure contains inactive/non-fact {fact_id}"
                )
            closure.add(fact_id)
            pending.extend(
                str(item)
                for item in value.get(
                    "predecessor_fact_ids", value.get("predecessor_ids", [])
                )
            )
        return closure

    def _worker_access_descriptor(self, card: Mapping[str, Any]) -> dict[str, Any]:
        """Describe project-memory exposure retained by one worker session.

        Session resumption may change worker mode, but it must never carry a
        broader prior memory view into a narrower launch.  Ordinary searchable
        modes are project-wide.  Isolated/sprint workspaces and proof-writer
        closures have exact finite memory-ID exposure.
        """

        mode = str(card.get("mode") or "")
        sprint_id = card.get("sprint_id")
        if mode == "proof-writer":
            root_fact_id = str(card.get("root_solution_fact_id") or "")
            if not root_fact_id:
                raise SchedulerError("proof-writer access requires the root solution fact")
            return {
                "project_wide": False,
                "memory_ids": sorted(self._fact_dependency_closure_ids(root_fact_id)),
            }
        if mode in ISOLATED_MODES or sprint_id is not None:
            portfolio = card.get("portfolio") or {}
            exposed: set[str] = set()
            if isinstance(portfolio, Mapping):
                for values in portfolio.values():
                    if isinstance(values, list):
                        exposed.update(str(item) for item in values)
            exposed.update(str(item) for item in card.get("main_route_ids", []) or [])
            exposed.update(str(item) for item in card.get("main_obligation_ids", []) or [])
            return {"project_wide": False, "memory_ids": sorted(exposed)}
        return {"project_wide": True, "memory_ids": []}

    @staticmethod
    def _merge_access_history(
        history: Mapping[str, Any], exposure: Mapping[str, Any]
    ) -> dict[str, Any]:
        project_wide = bool(history.get("project_wide")) or bool(
            exposure.get("project_wide")
        )
        memory_ids = set(str(item) for item in history.get("memory_ids", []) or [])
        memory_ids.update(str(item) for item in exposure.get("memory_ids", []) or [])
        return {
            "project_wide": project_wide,
            "memory_ids": sorted(memory_ids),
        }

    @staticmethod
    def _access_history_is_compatible(
        history: Mapping[str, Any], exposure: Mapping[str, Any]
    ) -> bool:
        if exposure.get("project_wide"):
            return True
        if history.get("project_wide"):
            return False
        return set(str(item) for item in history.get("memory_ids", []) or []).issubset(
            str(item) for item in exposure.get("memory_ids", []) or []
        )

    def _ensure_worker_session_lineages_locked(
        self, state: MutableMapping[str, Any]
    ) -> MutableMapping[str, Any]:
        """Migrate/reconcile durable lineage indexes from authoritative tasks."""

        lineages = state.setdefault("worker_session_lineages", {})
        members: dict[str, list[tuple[str, Mapping[str, Any]]]] = {}
        for task_id, task in state.get("tasks", {}).items():
            lineage_id = task.get("session_lineage_id")
            if lineage_id:
                members.setdefault(str(lineage_id), []).append((str(task_id), task))
        for lineage_id, tasks in members.items():
            entry = lineages.setdefault(
                lineage_id,
                {
                    "lineage_id": lineage_id,
                    "task_ids": [],
                    "explicit_resume_launches": 0,
                    "active_task_id": None,
                    "access_history": {"project_wide": False, "memory_ids": []},
                },
            )
            entry["task_ids"] = sorted(task_id for task_id, _task in tasks)
            entry["explicit_resume_launches"] = sum(
                1
                for _task_id, task in tasks
                if (task.get("task_card") or {}).get("if_resume")
                or (task.get("assign_record") or {}).get("if_resume")
            )
            active = [
                task_id
                for task_id, task in tasks
                if task.get("state") != TaskState.CLOSED.value
            ]
            entry["active_task_id"] = active[0] if len(active) == 1 else None
            history: dict[str, Any] = {"project_wide": False, "memory_ids": []}
            for _task_id, task in sorted(tasks):
                exposure = task.get("session_access")
                if not isinstance(exposure, Mapping):
                    card = task.get("task_card") or {}
                    try:
                        exposure = self._worker_access_descriptor(card)
                    except SchedulerError:
                        # Old snapshots did not persist exact proof-writer
                        # closure exposure.  Treat unknown history as broad
                        # rather than risk resuming it into a narrower sandbox.
                        exposure = {"project_wide": True, "memory_ids": []}
                    task["session_access"] = copy.deepcopy(exposure)
                history = self._merge_access_history(history, exposure)
            entry["access_history"] = history
        return lineages

    @staticmethod
    def _release_lineage_task_locked(
        state: MutableMapping[str, Any], task: Mapping[str, Any]
    ) -> None:
        lineage_id = task.get("session_lineage_id")
        entry = state.get("worker_session_lineages", {}).get(lineage_id)
        if entry and entry.get("active_task_id") == task.get("task_id"):
            entry["active_task_id"] = None

    def submit_batch(
        self,
        batch_id: str,
        assign_reports: Sequence[Mapping[str, Any]],
        *,
        sprint_id: str | None = None,
        terminal_proof_writer: bool = False,
        human_guidance_call_id: str | None = None,
    ) -> list[str]:
        reports = [copy.deepcopy(dict(report)) for report in assign_reports]
        digest = stable_digest(reports)
        with self._mutate() as state:
            human_guidance = None
            guidance_record = None
            if human_guidance_call_id is not None:
                if sprint_id is not None or terminal_proof_writer or not reports:
                    raise SchedulerError("human guidance requires an ordinary nonempty batch")
                call = state["calls"].get(human_guidance_call_id, {})
                call_input = call.get("input", {})
                snapshot = call_input.get("human_guidance")
                if (
                    call.get("kind") != "main"
                    or call_input.get("terminal_resolution_call")
                    or call_input.get("reserved_batch_id") != batch_id
                    or call.get("status") in {
                        CallState.CANCELLED.value,
                        CallState.SUPERSEDED.value,
                    }
                    or not isinstance(snapshot, Mapping)
                ):
                    raise SchedulerError("human guidance batch does not match its Main call")
                guidance_record = state.get("human_guidance_inbox", {}).get(
                    snapshot.get("guidance_id"), {}
                )
                if (
                    guidance_record.get("status") not in {"claimed", "assigned"}
                    or guidance_record.get("call_id") != human_guidance_call_id
                    or self._human_guidance_snapshot(guidance_record) != dict(snapshot)
                ):
                    raise SchedulerError("human guidance is not owned by this Main call")
                human_guidance = copy.deepcopy(dict(snapshot))
            existing = state["batches"].get(batch_id)
            if existing:
                if existing["input_digest"] != digest:
                    raise IdempotencyConflict(f"batch {batch_id} replayed with different input")
                if existing.get("human_guidance_call_id") != human_guidance_call_id:
                    raise IdempotencyConflict(f"batch {batch_id} changed its human guidance owner")
                if sprint_id is not None:
                    if existing.get("sprint_id") != sprint_id:
                        raise IdempotencyConflict(
                            f"batch {batch_id} belongs to another discovery sprint"
                        )
                    self._attach_sprint_batch_locked(state, sprint_id, batch_id)
                return list(existing["task_ids"])
            if guidance_record is not None and guidance_record.get("status") != "claimed":
                raise SchedulerError("human guidance already has an assigned task")
            self._require_franta_admission_locked(state)
            gate = GateState(state["gate"])
            if sprint_id is not None:
                if gate != GateState.TRIMMING:
                    raise WorkflowError("sprint batch requires trimming gate")
                sprint = state["sprints"].get(sprint_id)
                if not sprint:
                    raise SchedulerError(f"unknown sprint {sprint_id}")
                if sprint.get("status") != "waiting_for_slots":
                    raise WorkflowError(
                        f"sprint cannot launch from {sprint.get('status')}"
                    )
                self._validate_sprint_target(sprint["plan"]["target_obligation"])
                if len(reports) != 4:
                    raise SchedulerError("discovery sprint requires exactly four reports")
                validate_sprint_modes(
                    str(r.get("mode", r.get("work_mode", ""))) for r in reports
                )
                self._validate_sprint_reports_against_plan(sprint["plan"], reports)
            elif terminal_proof_writer:
                if gate != GateState.RESOLUTION_PENDING or len(reports) != 1:
                    raise WorkflowError("terminal proof-writer requires one resolution-pending task")
            elif gate != GateState.OPEN:
                raise WorkflowError(f"assignment gate is {gate.value}, not open")
            free = int(state["limits"]["max_non_verifier_workers"]) - self._running_count_of(
                state
            )
            if len(reports) > free:
                raise CapacityError(f"batch needs {len(reports)} slots but only {free} are free")
            lineages = self._ensure_worker_session_lineages_locked(state)
            task_ids: list[str] = []
            for report_index, report in enumerate(reports):
                self._validate_assign_report(state, report, sprint=sprint_id is not None)
                task_id = self._allocate_memory_id(state, "task")
                task_ids.append(task_id)
                mode = str(report.get("mode", report.get("work_mode")))
                prior_id = report.get("if_resume")
                if prior_id:
                    prior = state["tasks"].get(str(prior_id))
                    if not prior:
                        raise SchedulerError(f"if_resume task {prior_id} does not exist")
                    if (
                        TaskState(prior["state"]) is not TaskState.CLOSED
                        or prior.get("slot_reserved")
                    ):
                        raise SchedulerError(
                            f"if_resume source task {prior_id} must be closed and non-active"
                        )
                    lineage_id = str(prior["session_lineage_id"])
                    lineage = lineages.get(lineage_id)
                    if lineage is None:
                        raise SchedulerError(
                            f"worker-session lineage {lineage_id} is unavailable"
                        )
                    active_members = [
                        member_id
                        for member_id in lineage.get("task_ids", [])
                        if state["tasks"].get(member_id, {}).get("state")
                        != TaskState.CLOSED.value
                    ]
                    if active_members:
                        raise SchedulerError(
                            "worker-session lineage already has an active successor: "
                            + ", ".join(sorted(active_members))
                        )
                    resume_count = int(lineage.get("explicit_resume_launches", 0))
                    if resume_count >= int(state["limits"]["explicit_worker_resumes"]):
                        raise SchedulerError("fresh_start_required")
                    explicit_resume_count = resume_count + 1
                else:
                    lineage_id = self._allocate_id(state, "LINEAGE")
                    explicit_resume_count = 0
                    lineage = {
                        "lineage_id": lineage_id,
                        "task_ids": [],
                        "explicit_resume_launches": 0,
                        "active_task_id": None,
                        "access_history": {"project_wide": False, "memory_ids": []},
                    }
                    lineages[lineage_id] = lineage
                isolated = mode in ISOLATED_MODES or sprint_id is not None
                access_policy = copy.deepcopy(dict(report.get("access_policy") or {}))
                if isolated:
                    access_policy.update(
                        {
                            "canonical_memory": False,
                            "internal_search": False,
                            "sealed_workspace": True,
                        }
                    )
                problem_assignment = self._effective_problem_locked(state)
                card = {
                    "task_id": task_id,
                    "if_resume": prior_id,
                    "root_problem": problem_assignment["problem_text"],
                    "mode": mode,
                    "objective": report.get("objective", report.get("task_objective")),
                    "main_route_ids": report.get("main_route_ids")
                    or ([report["main_route_id"]] if report.get("main_route_id") else []),
                    "main_obligation_ids": report.get("main_obligation_ids")
                    or (
                        [report["main_obligation_id"]]
                        if report.get("main_obligation_id")
                        else []
                    ),
                    "perspective": (
                        report.get("selected_new_perspective")
                        if report.get("selected_new_perspective") is not None
                        else report.get("perspective")
                    ),
                    "computation_portfolio": copy.deepcopy(
                        report.get("computation_portfolio")
                    ),
                    "portfolio": copy.deepcopy(
                        report.get("portfolio", report.get("assignment_portfolio", {})) or {}
                    ),
                    "portfolio_revision": state["trim"].get("portfolio_revision"),
                    "access_policy": access_policy,
                    "sprint_id": sprint_id,
                    "sprint_lane": (
                        _SPRINT_LANE_LABELS[report_index]
                        if sprint_id is not None
                        else None
                    ),
                }
                if "advisor_control" in state:
                    card["problem_assignment"] = problem_assignment
                if human_guidance is not None and report_index == 0:
                    card["human_guidance"] = copy.deepcopy(human_guidance)
                if mode == "proof-writer":
                    # Freeze the exact verified root fact whose proof this
                    # expository task is allowed to use.
                    card["root_solution_fact_id"] = state["root"]["solution_fact_id"]
                session_access = self._worker_access_descriptor(card)
                if prior_id and not self._access_history_is_compatible(
                    lineage.get("access_history", {}), session_access
                ):
                    raise SchedulerError(
                        "if_resume is incompatible with the worker session's prior memory access"
                    )
                state["tasks"][task_id] = {
                    "task_id": task_id,
                    "batch_id": batch_id,
                    "sprint_id": sprint_id,
                    "sprint_lane": card["sprint_lane"],
                    "assign_record": copy.deepcopy(report),
                    "task_card": card,
                    "task_card_digest": stable_digest(card),
                    "state": TaskState.LAUNCHING.value,
                    "slot_reserved": True,
                    "launch_intent": True,
                    "attempts": [],
                    "current_attempt": 0,
                    "interruption_retry_count": 0,
                    "session_lineage_id": lineage_id,
                    "explicit_resume_count": explicit_resume_count,
                    "session_access": copy.deepcopy(session_access),
                    "operation_ids": [],
                    "computation_ids": [],
                    "challenge_ids": [],
                    "completion_evidence_ids": [],
                    "proposed_outcome": None,
                    "final_status": None,
                    "final_summary": None,
                    "closure_review_required": False,
                    "close_intent": None,
                    "after_close_applied": False,
                }
                if self._alternation_phase_of(state) is not None:
                    state["tasks"][task_id]["agent_system"] = "franta"
                    state["tasks"][task_id]["origin_phase_epoch"] = int(
                        state["phase_control"].get("phase_epoch", 0)
                    )
                lineage["task_ids"].append(task_id)
                lineage["task_ids"] = sorted(set(lineage["task_ids"]))
                lineage["explicit_resume_launches"] = explicit_resume_count
                lineage["active_task_id"] = task_id
                lineage["access_history"] = self._merge_access_history(
                    lineage.get("access_history", {}), session_access
                )
                append_event(
                    state,
                    "task_launch_intent_committed",
                    {
                        "task_id": task_id,
                        "batch_id": batch_id,
                        "sprint_id": sprint_id,
                        "session_lineage_id": lineage_id,
                        "explicit_resume_launch": bool(prior_id),
                    },
                )
            state["batches"][batch_id] = {
                "batch_id": batch_id,
                "input_digest": digest,
                "reports": reports,
                "task_ids": task_ids,
                "sprint_id": sprint_id,
                "terminal_proof_writer": terminal_proof_writer,
            }
            if guidance_record is not None:
                guidance_record.update(status="assigned", task_id=task_ids[0])
                state["batches"][batch_id]["human_guidance_call_id"] = human_guidance_call_id
                append_event(
                    state,
                    "human_guidance_assigned",
                    {
                        "guidance_id": guidance_record["guidance_id"],
                        "call_id": human_guidance_call_id,
                        "task_id": task_ids[0],
                    },
                )
            if sprint_id is not None:
                self._attach_sprint_batch_locked(state, sprint_id, batch_id)
            if terminal_proof_writer:
                state["proof_writer_task_id"] = task_ids[0]
            return task_ids

    @staticmethod
    def _running_count_of(state: Mapping[str, Any]) -> int:
        return sum(
            1
            for task in state["tasks"].values()
            if task.get("slot_reserved")
            and not task.get("non_slot_task")
            and task.get("state") != TaskState.CLOSED.value
        )

    def prepare_main_sort_task(
        self,
        *,
        sort_run_id: str,
        turn_id: str,
        source_high_water_seq: int,
        source_set_digest: str,
        snapshot_format_version: int,
        snapshot_digest: str,
        snapshot_relative_path: str = "input/explorer_snapshot",
    ) -> str:
        """Create the task-bound, project-wide Franta sorting owner idempotently."""

        identity = {
            "sort_run_id": str(sort_run_id),
            "turn_id": str(turn_id),
            "source_high_water_seq": int(source_high_water_seq),
            "source_set_digest": str(source_set_digest),
            "snapshot_format_version": snapshot_format_version,
            "snapshot_digest": snapshot_digest,
        }
        snapshot_path = Path(snapshot_relative_path)
        if (
            not identity["sort_run_id"]
            or not identity["turn_id"]
            or identity["source_high_water_seq"] < 0
            or not identity["source_set_digest"]
            or not isinstance(identity["snapshot_format_version"], int)
            or isinstance(identity["snapshot_format_version"], bool)
            or identity["snapshot_format_version"] < 1
            or not isinstance(identity["snapshot_digest"], str)
            or len(identity["snapshot_digest"]) != 64
            or any(
                character not in "0123456789abcdef"
                for character in identity["snapshot_digest"]
            )
            or snapshot_path.is_absolute()
            or ".." in snapshot_path.parts
            or snapshot_path.as_posix() != snapshot_relative_path
        ):
            raise SchedulerError("main-sort task requires a frozen Explorer source set")
        digest = stable_digest(identity)
        with self._mutate() as state:
            owners = state.setdefault("main_sort_tasks", {})
            existing_id = owners.get(identity["sort_run_id"])
            if existing_id:
                task = state["tasks"].get(existing_id)
                if not task:
                    raise IdempotencyConflict(
                        "main-sort owner references a missing task"
                    )
                if task.get("sort_input_digest") != digest:
                    raise IdempotencyConflict(
                        "main-sort run was replayed with different frozen input"
                    )
                if (
                    (task.get("task_card") or {}).get("explorer_snapshot_path")
                    != snapshot_relative_path
                ):
                    raise IdempotencyConflict(
                        "main-sort run was replayed with a different snapshot path"
                    )
                return str(existing_id)
            if self._alternation_phase_of(state) != "explorer_drain":
                raise WorkflowError(
                    "main-sort task may be prepared only after Explorer admission closes"
                )
            explorer_phase = state.get("phase_control", {}).get("explorer") or {}
            root_candidate = copy.deepcopy(explorer_phase.get("root_candidate"))
            task_id = self._allocate_memory_id(state, "task")
            problem_assignment = self._effective_problem_locked(state)
            card = {
                "task_id": task_id,
                "root_problem": problem_assignment["problem_text"],
                "mode": "main-sort",
                "objective": (
                    "Select and synthesize useful noncanonical Explorer records "
                    "from the frozen turn into Franta proposals."
                ),
                "sort_run_id": identity["sort_run_id"],
                "explorer_turn_id": identity["turn_id"],
                "source_high_water_seq": identity["source_high_water_seq"],
                "source_set_digest": identity["source_set_digest"],
                "source_access_mode": (
                    "explorer-snapshot-v"
                    f"{identity['snapshot_format_version']}"
                ),
                "explorer_snapshot_path": snapshot_relative_path,
                "explorer_snapshot_format_version": identity[
                    "snapshot_format_version"
                ],
                "explorer_snapshot_digest": identity["snapshot_digest"],
                # This is only an unverified pointer.  Surfacing it prevents a
                # ROOT candidate from being lost in a large scratch corpus.
                "root_candidate": root_candidate,
                "portfolio": {},
                "main_route_ids": [],
                "main_obligation_ids": [],
                "foundation_policy": copy.deepcopy(state.get("foundation_policy", {})),
            }
            if "advisor_control" in state:
                card["problem_assignment"] = problem_assignment
            assign_record = {
                "task_id": task_id,
                "mode": "main-sort",
                "objective": card["objective"],
                "explorer_turn_id": identity["turn_id"],
                "source_high_water_seq": identity["source_high_water_seq"],
                "source_set_digest": identity["source_set_digest"],
                "explorer_snapshot_format_version": identity[
                    "snapshot_format_version"
                ],
                "explorer_snapshot_digest": identity["snapshot_digest"],
                "root_candidate": root_candidate,
            }
            state["tasks"][task_id] = {
                "task_id": task_id,
                "batch_id": f"SORT-{identity['sort_run_id']}",
                "sprint_id": None,
                "sprint_lane": None,
                "assign_record": assign_record,
                "task_card": card,
                "task_card_digest": stable_digest(card),
                "state": TaskState.LAUNCHING.value,
                "slot_reserved": False,
                "non_slot_task": True,
                "launch_intent": True,
                "attempts": [],
                "current_attempt": 0,
                "interruption_retry_count": 0,
                "session_lineage_id": f"SORT-LINEAGE-{identity['sort_run_id']}",
                "explicit_resume_count": 0,
                "session_access": {"project_wide": True, "memory_ids": []},
                "operation_ids": [],
                "computation_ids": [],
                "challenge_ids": [],
                "completion_evidence_ids": [],
                "proposed_outcome": None,
                "final_status": None,
                "final_summary": None,
                "closure_review_required": False,
                "close_intent": None,
                "after_close_applied": False,
                "agent_system": "franta-sort",
                "origin_phase_epoch": int(
                    state.get("phase_control", {}).get("phase_epoch", 0)
                ),
                "sort_run_id": identity["sort_run_id"],
                "sort_input_digest": digest,
            }
            owners[identity["sort_run_id"]] = task_id
            append_event(
                state,
                "main_sort_task_prepared",
                {
                    "task_id": task_id,
                    "sort_run_id": identity["sort_run_id"],
                    "explorer_turn_id": identity["turn_id"],
                    "source_high_water_seq": identity["source_high_water_seq"],
                },
            )
            return task_id

    def start_task_attempt(self, task_id: str) -> int:
        with self._mutate() as state:
            task = state["tasks"].get(task_id)
            if not task:
                raise SchedulerError(f"unknown task {task_id}")
            self._require_franta_admission_locked(
                state, sort_task=task.get("agent_system") == "franta-sort"
            )
            current = TaskState(task["state"])
            if current not in {
                TaskState.LAUNCHING,
                TaskState.RETRY_PENDING,
                TaskState.REVISION_PENDING,
            }:
                raise WorkflowError(f"task {task_id} cannot start from {current.value}")
            if not task.get("slot_reserved") and not task.get("non_slot_task"):
                if self._running_count_of(state) >= int(
                    state["limits"]["max_non_verifier_workers"]
                ):
                    raise CapacityError("no worker slot is free")
                task["slot_reserved"] = True
            attempt_no = int(task.get("current_attempt", 0)) + 1
            supplement = task.pop("pending_attempt_supplement", None)
            semantic_supplement = preserved_attempt_supplement(supplement)
            effective_supplement = semantic_supplement or supplement
            if (effective_supplement or {}).get("kind") == "predecessor_rejected":
                task.pop("dependency_repair_required", None)
            elif (effective_supplement or {}).get("kind") in {
                "verifier_revision",
                "fact_concession",
                "identical_rejected_bundle",
            }:
                task.pop("dependency_repair_required", None)
            task["attempts"].append(
                {
                    "attempt": attempt_no,
                    "state": "running",
                    "started_at": utc_now(),
                    "last_sequence": 0,
                    "final_progress_id": None,
                    "summary": None,
                    "supplement": supplement,
                    "kind": (effective_supplement or {}).get("kind", "ordinary"),
                }
            )
            is_sort = task.get("agent_system") == "franta-sort"
            call_id = self._allocate_id(
                state, "CALL-SORT" if is_sort else "CALL-WORKER"
            )
            call_input = {
                "task_card": copy.deepcopy(task["task_card"]),
                "attempt": attempt_no,
                "supplement": copy.deepcopy(supplement),
            }
            state["calls"][call_id] = call_machine.prepare_call_record(
                call_id=call_id,
                kind="main-sort" if is_sort else "worker",
                payload=call_input,
                input_digest=stable_digest(call_input),
                retry_limit=int(
                    state["retry_policy"]["main" if is_sort else "worker"]
                ),
                event_cursor=self._event_cursor_of(state),
                continuation={"task_id": task_id, "task_attempt": attempt_no},
                status=CallState.RUNNING,
                attempt=1,
                retry_count=int(task.get("interruption_retry_count", 0)),
            )
            task["attempts"][-1]["call_id"] = call_id
            task["attempts"][-1]["lease_epoch"] = 1
            task["attempts"][-1]["call_attempt"] = 1
            task["current_attempt"] = attempt_no
            task["launch_intent"] = False
            self._transition_task(state, task, TaskState.RUNNING)
            return attempt_no

    def worker_call_lease(self, task_id: str) -> dict[str, Any]:
        task = self._state["tasks"].get(task_id)
        if not task or not task.get("attempts"):
            raise SchedulerError(f"task {task_id} has no worker attempt")
        attempt = task["attempts"][-1]
        call = self._state["calls"][attempt["call_id"]]
        return {
            "call_id": call["call_id"],
            "lease_epoch": call["lease_epoch"],
            "attempt": attempt["attempt"],
            "input": copy.deepcopy(call["input"]),
            "input_digest": call["input_digest"],
        }

    def register_task_artifacts(
        self,
        task_id: str,
        references: Sequence[Mapping[str, Any]],
    ) -> None:
        """Attach scheduler-archived artifact references before task closure.

        Raw artifacts remain outside canonical task memory.  The immutable task
        record contains only these validated relative references and hashes.
        Replaying the same reference is harmless; changing an existing path's
        hash is rejected.
        """

        normalized: list[dict[str, Any]] = []
        for raw in references:
            item = copy.deepcopy(dict(raw))
            relative_path = str(item.get("relative_path") or "")
            sha256 = str(item.get("sha256") or "")
            if not relative_path or relative_path.startswith("/") or ".." in relative_path.split("/"):
                raise SchedulerError("artifact reference must be a safe relative path")
            if len(sha256) != 64 or any(ch not in "0123456789abcdef" for ch in sha256.lower()):
                raise SchedulerError("artifact reference requires a SHA-256 digest")
            normalized.append(
                {
                    "relative_path": relative_path,
                    "sha256": sha256.lower(),
                    "kind": str(item.get("kind") or "artifact"),
                }
            )
        with self._lock:
            task = self._require_task(self._state, task_id)
            if TaskState(task["state"]) == TaskState.CLOSED:
                raise WorkflowError("cannot attach artifacts after task closure")
            if task.get("close_intent") is not None:
                raise WorkflowError("cannot attach artifacts after task close intent")
            existing = {
                item["relative_path"]: item
                for item in task.get("artifact_references", [])
            }
            has_new_reference = False
            for item in normalized:
                prior = existing.get(item["relative_path"])
                if prior is not None and prior != item:
                    raise IdempotencyConflict(
                        f"artifact {item['relative_path']} was replayed with different content"
                    )
                if prior is None:
                    existing[item["relative_path"]] = item
                    has_new_reference = True
            if not has_new_reference:
                return
            with self._mutate() as state:
                task = self._require_task(state, task_id)
                references = task.setdefault("artifact_references", [])
                existing_paths = {
                    item["relative_path"] for item in references
                }
                for item in normalized:
                    if item["relative_path"] in existing_paths:
                        continue
                    references.append(item)
                    existing_paths.add(item["relative_path"])
                references.sort(key=lambda item: item["relative_path"])

    def record_worker_interruption(self, task_id: str, reason: str) -> None:
        with self._mutate() as state:
            task = self._require_task(state, task_id)
            if TaskState(task["state"]) not in {TaskState.RUNNING, TaskState.LAUNCHING}:
                raise WorkflowError(f"task {task_id} is not running or launching")
            if task["attempts"] and task["attempts"][-1]["state"] == "running":
                attempt = task["attempts"][-1]
                attempt["state"] = "interrupted"
                attempt["ended_at"] = utc_now()
                attempt["ended_reason"] = reason
                worker_call = state["calls"].get(attempt.get("call_id"))
                if worker_call:
                    _apply_call_event(
                        state,
                        worker_call,
                        call_machine.Fence(CallState.SUPERSEDED),
                    )
            task["slot_reserved"] = False
            retry_count = int(task["interruption_retry_count"])
            limit = int(state["retry_policy"]["worker"])
            if retry_count < limit:
                task["interruption_retry_count"] = retry_count + 1
                self._transition_task(state, task, TaskState.RETRY_PENDING)
                prior_supplement = (
                    task["attempts"][-1].get("supplement")
                    if task.get("attempts")
                    else None
                )
                task["pending_attempt_supplement"] = infrastructure_retry_supplement(
                    reason,
                    prior_supplement,
                )
            else:
                # Existing durable operations still finish ingestion before the
                # task receives its mechanical interrupted closure.
                task["forced_outcome"] = TaskOutcome.INTERRUPTED.value
                task["mechanical_summary"] = {
                    "kind": "mechanical_interruption",
                    "reason": reason,
                    "last_progress_id": task.get("last_progress_id"),
                    "retry_count": retry_count,
                }
                if TaskState(task["state"]) != TaskState.ATTEMPT_ENDED:
                    self._transition_task(state, task, TaskState.ATTEMPT_ENDED)
                self._transition_task(state, task, TaskState.POSTPROCESSING)
            append_event(state, "worker_interrupted", {"task_id": task_id, "reason": reason})
        self._maybe_close_task(task_id)

    def mark_worker_resume_unavailable(self, task_id: str, reason: str) -> None:
        """Fail closed when an explicit ``if_resume`` thread cannot be proven."""

        with self._mutate() as state:
            task = self._require_task(state, task_id)
            current = TaskState(task["state"])
            if current == TaskState.NEEDS_ATTENTION:
                return
            if current not in {TaskState.RUNNING, TaskState.LAUNCHING}:
                raise WorkflowError(
                    f"task {task_id} cannot pause for unavailable resume from "
                    f"{current.value}"
                )
            call_id: str | None = None
            if task.get("attempts") and task["attempts"][-1]["state"] == "running":
                attempt = task["attempts"][-1]
                attempt["state"] = "needs_attention"
                attempt["ended_at"] = utc_now()
                attempt["ended_reason"] = str(reason)
                call_id = str(attempt.get("call_id") or "") or None
                worker_call = state["calls"].get(call_id)
                if worker_call:
                    _apply_call_event(
                        state,
                        worker_call,
                        call_machine.Fence(CallState.NEEDS_ATTENTION),
                    )
            task["slot_reserved"] = False
            self._transition_task(state, task, TaskState.NEEDS_ATTENTION)
            attention_id = f"task:{task_id}:worker-resume"
            self._add_attention(
                state,
                attention_id,
                "worker_resume_thread_unavailable",
                {"task_id": task_id, "call_id": call_id, "reason": str(reason)},
                scope="task",
                owner_id=task_id,
            )
            append_event(
                state,
                "worker_resume_unavailable",
                {"task_id": task_id, "call_id": call_id, "reason": str(reason)},
            )

    def record_worker_stopped(self, task_id: str, reason: str) -> None:
        """Finish a scheduler-requested stop after ingesting staged artifacts.

        Unlike an infrastructure interruption this path never retries the
        invalidated assignment.  Its already received operations continue to
        terminal dispositions before the task closes mechanically.
        """

        with self._mutate() as state:
            task = self._require_task(state, task_id)
            current = TaskState(task["state"])
            if current not in {TaskState.RUNNING, TaskState.STOPPING}:
                raise WorkflowError(f"task {task_id} has no running stop request")
            if current == TaskState.RUNNING:
                self._transition_task(state, task, TaskState.STOPPING)
            if task.get("attempts") and task["attempts"][-1]["state"] == "running":
                attempt = task["attempts"][-1]
                attempt["state"] = "stopped"
                attempt["ended_at"] = utc_now()
                attempt["ended_reason"] = reason
                worker_call = state["calls"].get(attempt.get("call_id"))
                if worker_call:
                    _apply_call_event(
                        state,
                        worker_call,
                        call_machine.Fence(CallState.SUPERSEDED),
                    )
            task["slot_reserved"] = False
            task["forced_outcome"] = TaskOutcome.INTERRUPTED.value
            task["mechanical_summary"] = {
                "kind": "scheduler_stop",
                "reason": reason,
                "last_progress_id": task.get("last_progress_id"),
            }
            self._transition_task(state, task, TaskState.ATTEMPT_ENDED)
            self._transition_task(state, task, TaskState.POSTPROCESSING)
            append_event(
                state,
                "worker_stopped",
                {"task_id": task_id, "reason": reason},
            )
        self._maybe_close_task(task_id)

    @staticmethod
    def _require_task(state: Mapping[str, Any], task_id: str) -> MutableMapping[str, Any]:
        task = state["tasks"].get(task_id)
        if not task:
            raise SchedulerError(f"unknown task {task_id}")
        return task

    @staticmethod
    def _looks_canonical_memory_id(value: str) -> bool:
        return value.startswith(("F-", "R-", "M-", "CL-", "O-", "T-", "C-"))

    def _canonical_memory_info(
        self, memory_id: str
    ) -> tuple[bool, str | None, bool]:
        if not hasattr(self.store, "get"):
            return False, None, False
        try:
            record = self.store.get(memory_id)
        except NotFoundError:
            return False, None, False
        except Exception as exc:
            raise SchedulerError(
                f"cannot validate canonical memory ID {memory_id}: {exc}"
            ) from exc
        if record is None:
            return False, None, False
        value = _as_dict(record)
        raw_type = value.get("type", value.get("memory_type"))
        memory_type = None if raw_type is None else str(raw_type)
        if memory_type and memory_type.startswith("MemoryType."):
            memory_type = memory_type.rsplit(".", 1)[-1].lower()
        status = str(value.get("status") or "")
        active = bool(
            value.get(
                "active",
                status not in {"inactive", "revoked", "withdrawn", "removed"},
            )
        )
        return True, memory_type, active

    def _canonical_memory_exists(self, memory_id: str) -> bool:
        exists, _memory_type, _active = self._canonical_memory_info(memory_id)
        return exists

    @staticmethod
    def _typed_path_values(
        value: Any, path: Sequence[str]
    ) -> list[tuple[str, str]]:
        """Read only schema-declared ID slots, never prose or arbitrary keys."""

        found: list[tuple[str, str]] = []

        def walk(current: Any, offset: int, rendered: str) -> None:
            if offset == len(path):
                if isinstance(current, str):
                    found.append((rendered, current))
                return
            part = path[offset]
            if part == "*":
                if isinstance(current, Sequence) and not isinstance(
                    current, (str, bytes, bytearray)
                ):
                    for index, item in enumerate(current):
                        walk(item, offset + 1, f"{rendered}[{index}]")
                return
            if not isinstance(current, Mapping) or part not in current:
                return
            label = f"{rendered}.{part}" if rendered else part
            walk(current[part], offset + 1, label)

        walk(value, 0, "")
        return found

    def _temporary_reference_claims(
        self, operation: Mapping[str, Any]
    ) -> dict[str, set[str]]:
        """Return typed, noncanonical IDs reserved by one operation.

        The reservation is derived only from schema-declared relationship
        slots.  This lets a source publish before a target is declared in a
        later progress record without letting prose mint capabilities.
        """

        kind = str(operation.get("kind") or operation.get("operation_type") or "")
        claims: dict[str, set[str]] = {}
        for path, _canonical_types, temporary_type in _OPERATION_ACCESS_PATHS.get(
            kind, ()
        ):
            if temporary_type is None:
                continue
            allowed_types = (
                {temporary_type}
                if isinstance(temporary_type, str)
                else {str(item) for item in temporary_type}
            )
            for _rendered, memory_id in self._typed_path_values(operation, path):
                if (
                    not memory_id
                    or memory_id != memory_id.strip()
                    or memory_id == "ROOT"
                    or self._looks_canonical_memory_id(memory_id)
                    or self._canonical_memory_exists(memory_id)
                ):
                    continue
                if memory_id not in claims:
                    claims[memory_id] = set(allowed_types)
                else:
                    claims[memory_id].intersection_update(allowed_types)
        return claims

    @staticmethod
    def _operation_created_canonical_memory(operation: Mapping[str, Any]) -> bool:
        """Distinguish new task output from a duplicate/update of old memory."""

        if operation.get("state") != OperationState.COMMITTED.value:
            return False
        kind = str(operation.get("kind") or "")
        if kind in {"memo", "claim_add"}:
            return True
        synthesis = operation.get("synthesizer_result")
        resolution = (
            str(synthesis.get("resolution") or "")
            if isinstance(synthesis, Mapping)
            else ""
        )
        if kind in {"route_add", "obligation_add"}:
            return resolution == "new"
        if kind == "fact":
            return resolution == "new" or bool(operation.get("duplicate_root_exception"))
        return False

    def _task_ingest_access_context_locked(
        self,
        state: MutableMapping[str, Any],
        task: MutableMapping[str, Any],
        raw_operations: Sequence[Any],
    ) -> dict[str, Any]:
        """Build the immutable grant and task-owned proposal namespace."""

        card = task.get("task_card")
        if not isinstance(card, Mapping):
            raise SchedulerError("task has no immutable task card")
        mode = str(card.get("mode") or "")
        card_is_restricted = (
            mode == "proof-writer"
            or mode in ISOLATED_MODES
            or task.get("sprint_id") is not None
            or card.get("sprint_id") is not None
        )
        exposure = task.get("session_access")
        if not isinstance(exposure, Mapping) or (
            card_is_restricted and bool(exposure.get("project_wide"))
        ):
            repair_card = copy.deepcopy(dict(card))
            if task.get("sprint_id") is not None:
                repair_card["sprint_id"] = task.get("sprint_id")
            try:
                exposure = self._worker_access_descriptor(repair_card)
            except SchedulerError as exc:
                raise SchedulerError(
                    "cannot reconstruct exact task access authorization"
                ) from exc
            if card_is_restricted and bool(exposure.get("project_wide")):
                raise SchedulerError("restricted task cannot have project-wide access")
            task["session_access"] = copy.deepcopy(exposure)
        project_wide = exposure.get("project_wide")
        memory_ids = exposure.get("memory_ids", [])
        if not isinstance(project_wide, bool):
            raise SchedulerError("task session_access.project_wide must be boolean")
        if not isinstance(memory_ids, list) or not all(
            isinstance(item, str) and item for item in memory_ids
        ):
            raise SchedulerError("task session_access.memory_ids must be a list of IDs")
        if card_is_restricted and project_wide:
            raise SchedulerError("restricted task cannot have project-wide access")

        allowed_canonical_ids = set(memory_ids)
        for operation in state.get("operations", {}).values():
            if operation.get("task_id") != task["task_id"]:
                continue
            canonical_id = operation.get("canonical_id")
            if canonical_id and self._operation_created_canonical_memory(operation):
                allowed_canonical_ids.add(str(canonical_id))
        for computation in state.get("computations", {}).values():
            if (
                computation.get("task_id") == task["task_id"]
                and computation.get("state") == OperationState.COMMITTED.value
                and computation.get("canonical_id")
            ):
                allowed_canonical_ids.add(str(computation["canonical_id"]))

        proposal_owners: dict[str, set[str]] = {}
        proposal_types: dict[str, set[str]] = {}
        proposal_type_conflicts: set[str] = set()

        def merge_proposal_types(proposal_id: str, allowed: Iterable[str]) -> None:
            constraint = {str(item) for item in allowed}
            prior = proposal_types.get(proposal_id)
            if prior is None:
                proposal_types[proposal_id] = constraint
                return
            merged = prior & constraint
            if not merged:
                proposal_type_conflicts.add(proposal_id)
                proposal_types[proposal_id] = prior | constraint
            else:
                proposal_types[proposal_id] = merged

        def type_spec(types: Iterable[str]) -> str | tuple[str, ...]:
            values = sorted({str(item) for item in types})
            return values[0] if len(values) == 1 else tuple(values)

        def spec_types(value: str | Sequence[str]) -> set[str]:
            return {value} if isinstance(value, str) else {str(item) for item in value}

        for operation in state.get("operations", {}).values():
            if (
                operation.get("access_authorized") is False
                or "requested_operation_id" in operation
            ):
                continue
            kind = str(operation.get("kind") or "")
            memory_type = _PROPOSAL_MEMORY_TYPES.get(kind)
            payload = operation.get("payload", {})
            proposal_id = str(
                payload.get("proposal_id") or ""
                if isinstance(payload, Mapping)
                else ""
            )
            owner_task_id = str(operation.get("task_id") or "")
            if memory_type and proposal_id:
                proposal_owners.setdefault(proposal_id, set()).add(owner_task_id)
                if owner_task_id == task["task_id"]:
                    merge_proposal_types(proposal_id, {memory_type})
            if isinstance(payload, Mapping):
                for temporary_id, types in self._temporary_reference_claims(
                    payload
                ).items():
                    proposal_owners.setdefault(temporary_id, set()).add(owner_task_id)
                    if owner_task_id == task["task_id"]:
                        merge_proposal_types(temporary_id, types)

        prior_owned_proposal_types = {
            proposal_id: type_spec(types)
            for proposal_id, types in proposal_types.items()
            if proposal_id not in proposal_type_conflicts
            and proposal_owners.get(proposal_id, set()) == {str(task["task_id"])}
        }

        declaration_errors: dict[int, str] = {}
        current_declarations: dict[int, tuple[str, str]] = {}
        declaration_counts: dict[str, int] = {}
        identity_invalid_declarations: set[int] = set()
        seen_operation_ids: set[str] = set()
        for index, raw_operation in enumerate(raw_operations):
            if not isinstance(raw_operation, Mapping):
                continue
            raw_operation_id = raw_operation.get("operation_id")
            exact_operation_id = (
                raw_operation_id
                if isinstance(raw_operation_id, str)
                and bool(raw_operation_id)
                and raw_operation_id == raw_operation_id.strip()
                else ""
            )
            if not exact_operation_id or exact_operation_id in seen_operation_ids:
                identity_invalid_declarations.add(index)
            if exact_operation_id:
                seen_operation_ids.add(exact_operation_id)
            existing_operation = state.get("operations", {}).get(exact_operation_id)
            if existing_operation:
                try:
                    operation_digest = stable_digest(dict(raw_operation))
                except Exception:
                    identity_invalid_declarations.add(index)
                    continue
                if (
                    existing_operation.get("input_digest") != operation_digest
                    or existing_operation.get("task_id") != task["task_id"]
                ):
                    identity_invalid_declarations.add(index)
        for raw_operation in raw_operations:
            if not isinstance(raw_operation, Mapping):
                continue
            kind = str(
                raw_operation.get("kind") or raw_operation.get("operation_type") or ""
            )
            raw_proposal_id = raw_operation.get("proposal_id")
            proposal_id = (
                raw_proposal_id
                if isinstance(raw_proposal_id, str)
                and bool(raw_proposal_id)
                and raw_proposal_id == raw_proposal_id.strip()
                else ""
            )
            if proposal_id and kind in _PROPOSAL_MEMORY_TYPES:
                declaration_counts[proposal_id] = (
                    declaration_counts.get(proposal_id, 0) + 1
                )
        for index, raw_operation in enumerate(raw_operations):
            if not isinstance(raw_operation, Mapping):
                continue
            kind = str(
                raw_operation.get("kind") or raw_operation.get("operation_type") or ""
            )
            memory_type = _PROPOSAL_MEMORY_TYPES.get(kind)
            raw_proposal_id = raw_operation.get("proposal_id")
            if raw_proposal_id is None or raw_proposal_id == "":
                continue
            if (
                not isinstance(raw_proposal_id, str)
                or raw_proposal_id != raw_proposal_id.strip()
            ):
                declaration_errors[index] = (
                    "outside_task_access: proposal_id must be an exact nonempty string"
                )
                continue
            proposal_id = raw_proposal_id
            if not memory_type:
                declaration_errors[index] = (
                    f"outside_task_access: proposal_id is not valid for {kind or 'invalid'}"
                )
                continue
            requested_id = str(raw_operation.get("operation_id") or "")
            existing_operation = state.get("operations", {}).get(requested_id)
            owners = proposal_owners.get(proposal_id, set())
            owned_types = proposal_types.get(proposal_id, set())
            error: str | None = None
            collides_with_canonical = self._canonical_memory_exists(proposal_id)
            if declaration_counts.get(proposal_id, 0) > 1:
                error = f"proposal_id {proposal_id} is duplicated within this progress file"
            elif collides_with_canonical:
                error = f"proposal_id {proposal_id} collides with canonical memory"
            elif existing_operation and existing_operation.get("task_id") != task["task_id"]:
                error = f"operation_id {requested_id} belongs to another task"
            elif owners - {str(task["task_id"])}:
                error = f"proposal_id {proposal_id} belongs to another task"
            elif (
                proposal_id in state.get("proposal_mappings", {})
                and str(task["task_id"]) not in owners
            ):
                error = f"proposal_id {proposal_id} is not owned by this task"
            elif proposal_id in proposal_type_conflicts or (
                owned_types and memory_type not in owned_types
            ):
                error = (
                    f"proposal_id {proposal_id} is already typed as "
                    f"{', '.join(sorted(owned_types))}"
                )
            if error is not None:
                declaration_errors[index] = f"outside_task_access: {error}"
                continue
            if index in identity_invalid_declarations:
                continue
            current_declarations[index] = (proposal_id, memory_type)
            proposal_owners.setdefault(proposal_id, set()).add(str(task["task_id"]))
            merge_proposal_types(proposal_id, {memory_type})

        # A typed relationship is itself a durable reservation.  It may name
        # a target first declared in a later progress record, but the ID stays
        # owned by this task and one memory type.  Current-receipt reservations
        # are provisional: an access-denied operation cannot grant a sibling a
        # capability.
        current_reservations: dict[
            int, dict[str, str | tuple[str, ...]]
        ] = {}
        for index, (proposal_id, memory_type) in current_declarations.items():
            current_reservations.setdefault(index, {})[proposal_id] = memory_type
        receipt_declared_ids = {
            str(raw_operation.get("proposal_id"))
            for raw_operation in raw_operations
            if isinstance(raw_operation, Mapping)
            and raw_operation.get("proposal_id") is not None
            and raw_operation.get("proposal_id") != ""
        }
        reservation_errors: dict[int, str] = {}
        reservation_claimants: dict[
            str, list[tuple[int, str | tuple[str, ...]]]
        ] = {}
        for index, raw_operation in enumerate(raw_operations):
            if not isinstance(raw_operation, Mapping) or index in identity_invalid_declarations:
                continue
            for temporary_id, types in self._temporary_reference_claims(
                raw_operation
            ).items():
                # A declaration in this receipt, even a malformed or
                # access-denied one, is authoritative for the receipt's
                # fixed-point dependency check.  Dependents must fail with it
                # rather than silently reclassifying the ID as an absent late
                # target.  Self-reservation applies only when B is truly not
                # declared until a later progress record.
                if temporary_id in receipt_declared_ids:
                    continue
                if not types:
                    reservation_errors[index] = (
                        "outside_task_access: temporary ID "
                        f"{temporary_id} is used with multiple relationship types"
                    )
                    continue
                allowed_types = set(types)
                known_types = proposal_types.get(temporary_id, set())
                if known_types and temporary_id not in proposal_type_conflicts:
                    allowed_types.intersection_update(known_types)
                if not allowed_types:
                    reservation_errors[index] = (
                        "outside_task_access: temporary ID "
                        f"{temporary_id} has incompatible relationship types"
                    )
                    continue
                memory_type = type_spec(allowed_types)
                prior = current_reservations.setdefault(index, {}).get(temporary_id)
                if prior is not None:
                    intersection = spec_types(prior) & spec_types(memory_type)
                    if not intersection:
                        reservation_errors[index] = (
                            "outside_task_access: temporary ID "
                            f"{temporary_id} has incompatible relationship types"
                        )
                        continue
                    memory_type = type_spec(intersection)
                current_reservations[index][temporary_id] = memory_type
        for index, reservations in current_reservations.items():
            for temporary_id, memory_type in reservations.items():
                reservation_claimants.setdefault(temporary_id, []).append(
                    (index, memory_type)
                )
        for temporary_id, claimants in reservation_claimants.items():
            current_types: set[str] | None = None
            for _index, memory_type in claimants:
                allowed = spec_types(memory_type)
                current_types = (
                    allowed
                    if current_types is None
                    else current_types & allowed
                )
            current_types = current_types or set()
            owners = proposal_owners.get(temporary_id, set())
            known_types = proposal_types.get(temporary_id, set())
            error: str | None = None
            if owners - {str(task["task_id"])}:
                error = f"temporary ID {temporary_id} belongs to another task"
            elif not current_types:
                error = (
                    f"temporary ID {temporary_id} is used with multiple relationship types"
                )
            elif temporary_id in proposal_type_conflicts or (
                known_types and not bool(current_types & known_types)
            ):
                error = (
                    f"temporary ID {temporary_id} is already typed as "
                    f"{', '.join(sorted(known_types))}"
                )
            elif hasattr(self.store, "temporary_reference_status"):
                status = self.store.temporary_reference_status(temporary_id)
                if status is not None and status.get("memory_type") not in current_types:
                    error = (
                        f"temporary ID {temporary_id} is already typed as "
                        f"{status.get('memory_type')}"
                    )
            if error is not None:
                for index, _memory_type in claimants:
                    reservation_errors[index] = f"outside_task_access: {error}"

        # Recompute to a deterministic fixed point so transitive dependents
        # fail regardless of list order, while valid forward references and
        # cycles and A-before-B late binding remain available together.
        # Non-access schema/mathematical rejection does not revoke ownership,
        # permitting a later corrected target with the same proposal ID.
        active_reservations = set(current_reservations)
        while True:
            owned_proposal_types = dict(prior_owned_proposal_types)
            for index in sorted(active_reservations):
                for proposal_id, memory_type in current_reservations[index].items():
                    prior = owned_proposal_types.get(proposal_id)
                    if prior is None:
                        owned_proposal_types[proposal_id] = memory_type
                    else:
                        intersection = spec_types(prior) & spec_types(memory_type)
                        if intersection:
                            owned_proposal_types[proposal_id] = type_spec(intersection)
            context = {
                "project_wide": project_wide,
                "allowed_canonical_ids": allowed_canonical_ids,
                "owned_proposal_types": owned_proposal_types,
            }
            operation_access_errors: dict[int, str] = dict(declaration_errors)
            operation_access_errors.update(reservation_errors)
            for index, raw_operation in enumerate(raw_operations):
                if index in operation_access_errors or not isinstance(
                    raw_operation, Mapping
                ):
                    continue
                error = self._operation_access_error(
                    raw_operation,
                    task_id=str(task["task_id"]),
                    context=context,
                )
                if error is not None:
                    operation_access_errors[index] = error
            denied_reservations = {
                index
                for index in active_reservations
                if operation_access_errors.get(index, "").startswith(
                    "outside_task_access:"
                )
            }
            if not denied_reservations:
                recognized_fact_proposal_ids = {
                    proposal_id
                    for proposal_id, memory_type in prior_owned_proposal_types.items()
                    if spec_types(memory_type) == {"fact"}
                }
                recognized_fact_proposal_ids.update(
                    proposal_id
                    for index, (proposal_id, memory_type) in current_declarations.items()
                    if memory_type == "fact"
                    and index not in identity_invalid_declarations
                )
                return {
                    **context,
                    "recognized_fact_proposal_ids": sorted(
                        recognized_fact_proposal_ids
                    ),
                    "declaration_errors": declaration_errors,
                    "operation_access_errors": operation_access_errors,
                }
            active_reservations.difference_update(denied_reservations)

    def _typed_reference_access_error(
        self,
        context: Mapping[str, Any],
        *,
        path: str,
        memory_id: str,
        canonical_types: Sequence[str],
        temporary_type: str | Sequence[str] | None,
        active_types: Sequence[str] = (),
    ) -> str | None:
        if not memory_id or memory_id != memory_id.strip():
            return (
                f"outside_task_access: {path} requires an exact nonempty ID "
                "without surrounding whitespace"
            )
        try:
            canonical_exists, canonical_type, active = self._canonical_memory_info(
                memory_id
            )
        except SchedulerError:
            return (
                f"outside_task_access: {path} could not validate memory ID "
                f"{memory_id}"
            )
        if canonical_exists:
            if (
                not context.get("project_wide")
                and memory_id not in context.get("allowed_canonical_ids", set())
            ):
                return f"outside_task_access: {path} references unsupplied {memory_id}"
            if canonical_type not in set(canonical_types):
                expected = "|".join(canonical_types)
                return (
                    f"outside_task_access: {path} requires canonical type "
                    f"{expected}, not {canonical_type or 'unknown'}"
                )
            if canonical_type in set(active_types) and not active:
                return (
                    f"outside_task_access: {path} requires an active "
                    f"{canonical_type}"
                )
            return None
        actual_type = context.get("owned_proposal_types", {}).get(memory_id)
        expected_temporary_types = (
            {temporary_type}
            if isinstance(temporary_type, str)
            else set(temporary_type or ())
        )
        actual_temporary_types = (
            {actual_type}
            if isinstance(actual_type, str)
            else set(actual_type or ())
        )
        if temporary_type is not None and actual_temporary_types and (
            actual_temporary_types <= expected_temporary_types
        ):
            return None
        if self._looks_canonical_memory_id(memory_id) or temporary_type is None:
            return f"outside_task_access: {path} requires an authorized canonical ID"
        if not actual_temporary_types or not (
            actual_temporary_types <= expected_temporary_types
        ):
            expected = "|".join(sorted(expected_temporary_types))
            return (
                f"outside_task_access: {path} references temporary ID {memory_id} "
                f"not owned by this task as {expected}"
            )
        return None

    def _operation_access_error(
        self,
        operation: Mapping[str, Any],
        *,
        task_id: str,
        context: Mapping[str, Any],
    ) -> str | None:
        kind = str(operation.get("kind") or operation.get("operation_type") or "")
        if kind == "fact":
            field = "predecessor_fact_ids"
            if field in operation:
                predecessors = operation[field]
                if not isinstance(predecessors, list) or not all(
                    isinstance(item, str)
                    and bool(item)
                    and item == item.strip()
                    for item in predecessors
                ):
                    return (
                        f"invalid {field}: expected a list of nonempty string IDs "
                        "without surrounding whitespace"
                    )
            originating_task_id = operation.get("originating_task_id")
            if originating_task_id is not None and str(originating_task_id) != task_id:
                return (
                    "outside_task_access: originating_task_id must equal the current task ID"
                )
        active_by_path = _ACTIVE_OPERATION_REFERENCE_TYPES.get(kind, {})
        for path, canonical_types, temporary_type in _OPERATION_ACCESS_PATHS.get(
            kind, ()
        ):
            for rendered, memory_id in self._typed_path_values(operation, path):
                if path[-1] == "conclusion" and memory_id == "ROOT":
                    continue
                error = self._typed_reference_access_error(
                    context,
                    path=rendered,
                    memory_id=memory_id,
                    canonical_types=canonical_types,
                    temporary_type=temporary_type,
                    active_types=active_by_path.get(path, ()),
                )
                if error is not None:
                    return error
        return None

    def _computation_access_error(
        self,
        computation: Mapping[str, Any],
        *,
        task_id: str,
        context: Mapping[str, Any],
        state: Mapping[str, Any],
    ) -> str | None:
        for path, canonical_types, temporary_type in _COMPUTATION_ACCESS_PATHS:
            for rendered, memory_id in self._typed_path_values(computation, path):
                error = self._typed_reference_access_error(
                    context,
                    path=rendered,
                    memory_id=memory_id,
                    canonical_types=canonical_types,
                    temporary_type=temporary_type,
                )
                if error is not None:
                    return error
        for rendered, operation_id in self._typed_path_values(
            computation, ("fact_candidate_operation_ids", "*")
        ):
            operation = state.get("operations", {}).get(operation_id)
            if (
                not operation
                or operation.get("task_id") != task_id
                or operation.get("kind") != "fact"
            ):
                return (
                    f"outside_task_access: {rendered} must name a fact operation "
                    "owned by this task"
                )
        return None

    # --------------------------------------------------------- progress/ingest

    def ingest_progress(
        self,
        progress: Mapping[str, Any],
        *,
        interrupted_salvage: bool = False,
        authenticated_computations: bool = False,
    ) -> dict[str, str]:
        """Persist a progress file, then route its independent valid operations.

        The receipt transaction completes before any canonical-store call or
        model call.  A crash therefore replays only idempotent pending work.
        """

        payload = copy.deepcopy(dict(progress))
        raw_computations = payload.get("computations", [])
        if not isinstance(raw_computations, list):
            raise SchedulerError("progress computations must be a list")
        raw_challenges = payload.get("fact_challenges", [])
        if not isinstance(raw_challenges, list):
            raise SchedulerError("progress fact_challenges must be a list")
        if raw_computations and not authenticated_computations:
            raise SchedulerError(
                "progress computation bodies require authenticated CAS provenance"
            )
        progress_id = str(payload.get("progress_id") or "")
        task_id = str(payload.get("task_id") or "")
        if not progress_id or not task_id:
            raise SchedulerError("progress_id and task_id are required")
        source_was_final = bool(payload.get("is_final", False))
        if interrupted_salvage and not source_was_final:
            raise SchedulerError("only a staged final progress file can be salvaged")
        if interrupted_salvage and isinstance(payload.get("attempt_summary"), Mapping):
            _validated_completion_evidence(payload, payload["attempt_summary"])
        progress_digest = stable_digest(payload)
        operation_ids: list[str] = []
        with self._mutate() as state:
            existing = state["progress"].get(progress_id)
            if existing:
                if existing["input_digest"] != progress_digest:
                    raise IdempotencyConflict(
                        f"progress {progress_id} replayed with different content"
                    )
                return {
                    operation_id: state["operations"][operation_id]["state"]
                    for operation_id in existing["operation_ids"]
                }
            task = self._require_task(state, task_id)
            if TaskState(task["state"]) not in {TaskState.RUNNING, TaskState.STOPPING}:
                raise WorkflowError(f"task {task_id} is not running")
            attempt_no = int(payload.get("attempt", 0))
            if attempt_no != int(task["current_attempt"]):
                raise SchedulerError("progress has the wrong attempt number")
            attempt = task["attempts"][-1]
            sequence = int(payload.get("sequence", 0))
            if sequence != int(attempt["last_sequence"]) + 1:
                raise SchedulerError("progress sequence must increase by exactly one")
            # A successfully staged final file survives a lost process, but it
            # cannot end the attempt normally without the matching final agent
            # response.  Its operations remain durable while recovery records
            # the attempt as interrupted and retries it.
            is_final = source_was_final and not interrupted_salvage
            if is_final and attempt.get("final_progress_id"):
                raise SchedulerError("at most one final progress file is allowed per attempt")
            operations = list(payload.get("operations", []))
            access_context = self._task_ingest_access_context_locked(
                state, task, operations
            )
            seen_local: set[str] = set()
            for index, raw_operation in enumerate(operations):
                validation_error: str | None = None
                access_error: str | None = None
                try:
                    operation = copy.deepcopy(dict(raw_operation))
                except Exception:
                    operation = {"raw": repr(raw_operation)}
                    validation_error = "operation must be an object"
                raw_requested_id = operation.get("operation_id")
                requested_id = (
                    raw_requested_id if isinstance(raw_requested_id, str) else ""
                )
                kind = str(operation.get("kind") or operation.get("operation_type") or "")
                exact_operation_id = bool(requested_id) and requested_id == requested_id.strip()
                operation_id = requested_id if exact_operation_id else ""
                if validation_error is None and not exact_operation_id:
                    validation_error = (
                        "operation requires a nonempty exact string operation_id"
                    )
                elif validation_error is None and operation_id in seen_local:
                    validation_error = "operation_id is duplicated within this progress file"
                if operation_id:
                    seen_local.add(operation_id)
                if validation_error is None and kind not in MEMORY_OPERATION_KINDS:
                    validation_error = f"unknown memory operation kind {kind!r}"
                provenance = operation.get("explorer_provenance")
                is_main_sort = task.get("agent_system") == "franta-sort"
                if (
                    validation_error is None
                    and is_main_sort
                    and kind not in main_sort_operation_kinds()
                ):
                    validation_error = (
                        "main-sort may promote only routes, obligations, memos, and claims"
                    )
                if validation_error is None and not is_main_sort and provenance is not None:
                    validation_error = (
                        "Explorer provenance is authorized only for the main-sort task"
                    )
                if validation_error is None and is_main_sort:
                    if not main_sort_operation_has_exact_provenance(
                        operation,
                        sort_run_id=str(task.get("sort_run_id") or ""),
                    ):
                        validation_error = (
                            "main-sort operation lacks exact frozen Explorer provenance"
                        )
                op_digest = stable_digest(operation)
                existing_op = state["operations"].get(operation_id) if operation_id else None
                if (
                    validation_error is None
                    and existing_op
                    and existing_op["input_digest"] != op_digest
                ):
                    validation_error = "operation ID was replayed with different content"
                if (
                    validation_error is None
                    and existing_op
                    and existing_op.get("task_id") != task_id
                ):
                    validation_error = "operation ID belongs to another task"
                if validation_error is None:
                    access_error = access_context["operation_access_errors"].get(index)
                    if access_error is not None:
                        validation_error = access_error
                if validation_error is not None:
                    # Preserve a sibling's valid use of a duplicate ID and give
                    # this malformed item its own terminal validation artifact.
                    if not operation_id or operation_id in state["operations"]:
                        operation_id = f"{progress_id}:invalid:{index}"
                    state["operations"][operation_id] = {
                        "operation_id": operation_id,
                        "requested_operation_id": requested_id or None,
                        "task_id": task_id,
                        "attempt": attempt_no,
                        "progress_id": progress_id,
                        "kind": kind or "invalid",
                        "payload": operation,
                        "input_digest": op_digest,
                        "state": OperationState.REJECTED.value,
                        "canonical_id": None,
                        "error": validation_error,
                        "access_authorized": access_error is None,
                    }
                elif not existing_op:
                    record = {
                        "operation_id": operation_id,
                        "task_id": task_id,
                        "attempt": attempt_no,
                        "progress_id": progress_id,
                        "kind": kind,
                        "payload": operation,
                        "input_digest": op_digest,
                        "state": OperationState.RECEIVED.value,
                        "canonical_id": None,
                        "error": None,
                        "access_authorized": True,
                    }
                    try:
                        if kind == "fact":
                            self._initialize_fact_operation(
                                state,
                                record,
                                known_fact_proposal_ids=access_context.get(
                                    "recognized_fact_proposal_ids", ()
                                ),
                            )
                    except SchedulerError as exc:
                        record["state"] = OperationState.REJECTED.value
                        record["error"] = str(exc)
                    state["operations"][operation_id] = record
                operation_ids.append(operation_id)
                if operation_id not in task["operation_ids"]:
                    task["operation_ids"].append(operation_id)
            challenge_ids: list[str] = []
            seen_challenges: set[str] = set()
            for index, raw_challenge in enumerate(raw_challenges):
                challenge_error: str | None = None
                try:
                    challenge = copy.deepcopy(dict(raw_challenge))
                except Exception:
                    challenge = {"raw": repr(raw_challenge)}
                    challenge_error = "fact challenge must be an object"
                requested_challenge_id = str(challenge.get("challenge_id") or "")
                challenge_id = requested_challenge_id
                challenge_digest = stable_digest(challenge)
                existing_challenge = state["challenges"].get(challenge_id)
                if challenge_error is None and not challenge_id:
                    challenge_error = "fact challenge requires challenge_id"
                elif challenge_error is None and challenge_id in seen_challenges:
                    challenge_error = "challenge_id is duplicated within this progress file"
                elif (
                    challenge_error is None
                    and existing_challenge
                    and existing_challenge.get("task_id") != task_id
                ):
                    challenge_error = "challenge ID belongs to another task"
                elif (
                    challenge_error is None
                    and existing_challenge
                    and existing_challenge["input_digest"] != challenge_digest
                ):
                    raise IdempotencyConflict("challenge replayed with different content")
                if challenge_id:
                    seen_challenges.add(challenge_id)
                fact_id = challenge.get("fact_id")
                if challenge_error is None and (
                    not isinstance(fact_id, str) or not fact_id.strip()
                ):
                    challenge_error = "fact challenge requires a nonempty string fact_id"
                elif challenge_error is None:
                    challenge_error = self._typed_reference_access_error(
                        access_context,
                        path="fact_id",
                        memory_id=fact_id,
                        canonical_types=("fact",),
                        temporary_type=None,
                        active_types=("fact",),
                    )
                    if challenge_error is None:
                        try:
                            self._validate_memory_id(
                                fact_id, "fact", active_fact=True
                            )
                        except SchedulerError as exc:
                            challenge_error = f"invalid fact challenge fact_id: {exc}"
                access_authorized = challenge_error is None
                if challenge_error is not None:
                    if not challenge_id or challenge_id in state["challenges"]:
                        challenge_id = f"{progress_id}:invalid-challenge:{index}"
                    state["challenges"][challenge_id] = {
                        "challenge_id": challenge_id,
                        "requested_challenge_id": requested_challenge_id or None,
                        "task_id": task_id,
                        "attempt": attempt_no,
                        "progress_id": progress_id,
                        "payload": challenge,
                        "input_digest": challenge_digest,
                        "state": OperationState.REJECTED.value,
                        "error": challenge_error,
                        "access_authorized": access_authorized,
                    }
                elif not existing_challenge:
                    state["challenges"][challenge_id] = {
                        "challenge_id": challenge_id,
                        "task_id": task_id,
                        "attempt": attempt_no,
                        "progress_id": progress_id,
                        "payload": challenge,
                        "input_digest": challenge_digest,
                        "state": OperationState.VERIFYING.value,
                        "access_authorized": True,
                    }
                challenge_ids.append(challenge_id)
                if challenge_id not in task["challenge_ids"]:
                    task["challenge_ids"].append(challenge_id)
            computation_ids: list[str] = []
            for raw_computation in raw_computations:
                computation = copy.deepcopy(dict(raw_computation))
                staging_id = str(
                    computation.get("staging_id")
                    or computation.get("operation_id")
                    or self._allocate_id(state, "COMP-STAGE")
                )
                computation_digest = stable_digest(computation)
                existing_computation = state["computations"].get(staging_id)
                computation_error = self._computation_access_error(
                    computation,
                    task_id=task_id,
                    context=access_context,
                    state=state,
                )
                computation_provenance = computation.get("explorer_provenance")
                is_main_sort = task.get("agent_system") == "franta-sort"
                if not is_main_sort and computation_provenance is not None:
                    computation_error = (
                        "Explorer computation provenance is authorized only for main-sort"
                    )
                elif is_main_sort and not main_sort_computation_has_exact_provenance(
                    computation,
                    sort_run_id=str(task.get("sort_run_id") or ""),
                ):
                    computation_error = (
                        "main-sort computation lacks trusted Explorer provenance"
                    )
                if existing_computation:
                    if (
                        existing_computation.get("input_digest") != computation_digest
                        or existing_computation.get("task_id") != task_id
                        or int(existing_computation.get("source_attempt", 0)) != attempt_no
                        or existing_computation.get("source_kind") != "worker-progress"
                    ):
                        raise IdempotencyConflict(
                            f"computation {staging_id} replayed from a different source"
                        )
                else:
                    state["computations"][staging_id] = {
                        "staging_id": staging_id,
                        "task_id": task_id,
                        "payload": computation,
                        "input_digest": computation_digest,
                        "state": (
                            OperationState.REJECTED.value
                            if computation_error is not None
                            else OperationState.RECEIVED.value
                        ),
                        "canonical_id": None,
                        "error": computation_error,
                        "access_authorized": computation_error is None,
                        "source_kind": "worker-progress",
                        "source_attempt": attempt_no,
                    }
                computation_ids.append(staging_id)
                if staging_id not in task["computation_ids"]:
                    task["computation_ids"].append(staging_id)
            attempt["last_sequence"] = sequence
            task["last_progress_id"] = progress_id
            state["progress"][progress_id] = {
                "progress_id": progress_id,
                "task_id": task_id,
                "attempt": attempt_no,
                "sequence": sequence,
                "is_final": is_final,
                "source_was_final": source_was_final,
                "interrupted_salvage": bool(interrupted_salvage),
                "input_digest": progress_digest,
                "payload": payload,
                "operation_ids": operation_ids,
                "challenge_ids": challenge_ids,
                "computation_ids": computation_ids,
            }
            append_event(
                state,
                "progress_received",
                {
                    "progress_id": progress_id,
                    "task_id": task_id,
                    "is_final": is_final,
                    "source_was_final": source_was_final,
                    "interrupted_salvage": bool(interrupted_salvage),
                },
            )
            if is_final:
                self._end_attempt_from_progress(state, task, attempt, payload, progress_id)

        # Store calls happen only after the durable receipt is committed.
        for operation_id in operation_ids:
            received = self._state["operations"].get(operation_id, {})
            if (
                received.get("kind") == "fact"
                and received.get("state") == OperationState.WAITING_PREDECESSORS.value
            ):
                for temporary_id in self._temporary_fact_predecessor_ids(received):
                    canonical_id = self._state["proposal_mappings"].get(str(temporary_id))
                    if canonical_id:
                        self.resolve_temporary_predecessor(
                            str(temporary_id), str(canonical_id)
                        )
            self._route_operation(operation_id)
            operation = self._state["operations"].get(operation_id, {})
            if (
                operation.get("kind") == "fact"
                and operation.get("state") == OperationState.REJECTED.value
                and self._operation_may_terminalize_fact_proposal(
                    self._state, operation
                )
            ):
                for rejected_gate_id in operation.get(
                    "rejected_fact_gate_ids", []
                ):
                    self.reject_temporary_predecessor(
                        str(rejected_gate_id),
                        reason=str(
                            operation.get("error")
                            or "temporary Fact publication gate was rejected"
                        ),
                        predecessor_operation_id=operation_id,
                    )
                self.reject_temporary_predecessor(
                    self._fact_proposal_id_of(operation),
                    reason=str(operation.get("error") or "fact proposal rejected"),
                    predecessor_operation_id=operation_id,
                )
        for staging_id in computation_ids:
            self._commit_computation(staging_id)
        if is_final:
            self._terminalize_unpublishable_final_fact_gates(task_id)
        self._maybe_close_task(task_id)
        return {operation_id: self._state["operations"][operation_id]["state"] for operation_id in operation_ids}

    @staticmethod
    def _value_contains(value: Any, targets: set[str]) -> set[str]:
        if isinstance(value, str):
            return {value} & targets
        if isinstance(value, Mapping):
            found: set[str] = set()
            for item in value.values():
                found.update(Scheduler._value_contains(item, targets))
            return found
        if isinstance(value, (list, tuple)):
            found = set()
            for item in value:
                found.update(Scheduler._value_contains(item, targets))
            return found
        return set()

    def _register_referential_operation_groups(
        self,
        state: MutableMapping[str, Any],
        progress_id: str,
        operation_ids: Sequence[str],
    ) -> None:
        allowed = {"route_add", "memo", "claim_add", "obligation_add"}
        candidates = {
            operation_id: state["operations"][operation_id]
            for operation_id in operation_ids
            if state["operations"][operation_id]["state"] == OperationState.RECEIVED.value
            and state["operations"][operation_id]["kind"] in allowed
            and state["operations"][operation_id]["payload"].get("proposal_id")
        }
        proposal_to_operation = {
            str(operation["payload"]["proposal_id"]): operation_id
            for operation_id, operation in candidates.items()
        }
        proposal_ids = set(proposal_to_operation)
        adjacency: dict[str, set[str]] = {operation_id: set() for operation_id in candidates}
        has_internal_reference: set[str] = set()
        for operation_id, operation in candidates.items():
            references = self._value_contains(_operation_body(operation["payload"]), proposal_ids)
            for proposal_id in references:
                other = proposal_to_operation[proposal_id]
                adjacency[operation_id].add(other)
                adjacency[other].add(operation_id)
                has_internal_reference.update({operation_id, other})
        seen: set[str] = set()
        groups = state.setdefault("operation_groups", {})
        index = 0
        for seed in sorted(has_internal_reference):
            if seed in seen:
                continue
            stack = [seed]
            component: list[str] = []
            while stack:
                current = stack.pop()
                if current in seen:
                    continue
                seen.add(current)
                component.append(current)
                stack.extend(sorted(adjacency[current] - seen, reverse=True))
            index += 1
            group_id = f"GROUP:{progress_id}:{index}"
            groups[group_id] = {
                "group_id": group_id,
                "operation_ids": sorted(component),
                "state": "waiting_synthesis",
            }
            for operation_id in component:
                operation = state["operations"][operation_id]
                operation["group_id"] = group_id
                if operation["kind"] in {"memo", "claim_add"}:
                    operation["state"] = OperationState.WAITING_PREDECESSORS.value

    @staticmethod
    def _fact_payload_predecessors(payload: Mapping[str, Any]) -> list[str]:
        """Read the one authoritative Fact dependency field."""

        if "predecessor_ids" in payload:
            raise SchedulerError(
                "fact operations must use predecessor_fact_ids; predecessor_ids is not authoritative"
            )
        raw = payload.get("predecessor_fact_ids", [])
        if not isinstance(raw, list) or not all(
            isinstance(item, str) and bool(item) and item == item.strip()
            for item in raw
        ):
            raise SchedulerError(
                "predecessor_fact_ids must be a list of exact nonempty string IDs"
            )
        predecessors = list(raw)
        if len(set(predecessors)) != len(predecessors):
            raise SchedulerError("predecessor_fact_ids must not contain duplicates")
        return predecessors

    @staticmethod
    def _recognized_fact_body_gates(
        payload: Mapping[str, Any], known_fact_proposal_ids: Iterable[str]
    ) -> list[str]:
        """Freeze exact-token temporary Fact citations at receipt time.

        Prose is never rewritten and never becomes graph-dependency metadata.
        Only proposal IDs already recognized as task-owned Facts at this
        receipt boundary can become publication gates.
        """

        statement = payload.get("statement")
        proof = payload.get("proof")
        texts = [value for value in (statement, proof) if isinstance(value, str)]
        gates: list[str] = []
        for proposal_id in sorted({str(item) for item in known_fact_proposal_ids}):
            if proposal_id.startswith("F-"):
                continue
            if any(contains_identifier_token(text, proposal_id) for text in texts):
                gates.append(proposal_id)
        return gates

    @staticmethod
    def _temporary_fact_predecessor_ids(
        operation: Mapping[str, Any],
    ) -> list[str]:
        """Derive temporary dependency IDs from the authoritative payload field."""

        payload = operation.get("payload")
        if not isinstance(payload, Mapping):
            return []
        raw = payload.get("predecessor_fact_ids", [])
        if not isinstance(raw, list):
            return []
        return [
            item
            for item in raw
            if isinstance(item, str) and item and not item.startswith("F-")
        ]

    @staticmethod
    def _operation_fact_gate_ids(
        state: Mapping[str, Any], operation: Mapping[str, Any]
    ) -> list[str]:
        """Return unresolved explicit and frozen-body Fact publication gates."""

        mapping = state.get("proposal_mappings", {})
        gates = {
            str(item)
            for item in Scheduler._temporary_fact_predecessor_ids(operation)
            if not mapping.get(str(item))
        }
        gates.update(
            str(item)
            for item in operation.get("body_temporary_fact_ids", [])
            if not mapping.get(str(item))
        )
        return sorted(gates)

    @staticmethod
    def _fact_repair_context_locked(
        state: Mapping[str, Any], record: Mapping[str, Any]
    ) -> tuple[str, int, dict[str, Any] | None]:
        task = state.get("tasks", {}).get(record.get("task_id"), {})
        supplement: Any = None
        for attempt in reversed(task.get("attempts", [])):
            if int(attempt.get("attempt", -1)) == int(record.get("attempt", -2)):
                supplement = preserved_attempt_supplement(attempt.get("supplement"))
                break
        if not isinstance(supplement, Mapping) or supplement.get("kind") not in {
            "verifier_revision",
            "identical_rejected_bundle",
        }:
            proposal_id = str(record.get("payload", {}).get("proposal_id") or "")
            return proposal_id, 0, None
        chain_id = str(
            supplement.get("repair_chain_id")
            or supplement.get("proposal_id")
            or supplement.get("candidate_id")
            or record.get("payload", {}).get("proposal_id")
            or ""
        )
        request = int(supplement.get("revision_request", 0))
        report = supplement.get("verification_report")
        return (
            chain_id,
            request,
            copy.deepcopy(dict(report)) if isinstance(report, Mapping) else None,
        )

    def _initialize_fact_operation(
        self,
        state: MutableMapping[str, Any],
        record: MutableMapping[str, Any],
        *,
        known_fact_proposal_ids: Iterable[str] = (),
    ) -> None:
        payload = record["payload"]
        candidate_id = str(payload.get("candidate_id") or "")
        version = int(payload.get("candidate_version", 0))
        proposal_id = str(payload.get("proposal_id") or "")
        if not candidate_id or version != 1:
            raise SchedulerError(
                "each worker Fact proposal requires a fresh candidate_id at candidate_version 1"
            )
        if not proposal_id:
            raise SchedulerError("fact operation requires a fresh proposal_id")
        existing_lineage = state["fact_lineages"].get(candidate_id)
        if existing_lineage:
            raise SchedulerError(
                "each worker Fact correction requires a fresh candidate_id"
            )
        proposals = state.setdefault("fact_proposals", {})
        if proposal_id in proposals:
            raise SchedulerError(
                "each worker Fact correction requires a fresh proposal_id"
            )
        predecessors = self._fact_payload_predecessors(payload)
        unresolved = [p for p in predecessors if not str(p).startswith("F-")]
        body_gates = self._recognized_fact_body_gates(
            payload, known_fact_proposal_ids
        )
        repair_chain_id, repair_request, prior_report = self._fact_repair_context_locked(
            state, record
        )
        lineage = {
            "candidate_id": candidate_id,
            "task_id": record["task_id"],
            "proposal_id": proposal_id,
            "versions": {"1": record["operation_id"]},
            "revision_requests": repair_request,
            "rejected_bundle_digests": [],
            "concession_required": False,
            "closed": False,
            "repair_chain_id": repair_chain_id,
        }
        state["fact_lineages"][candidate_id] = lineage
        record["candidate_id"] = candidate_id
        record["candidate_version"] = version
        record["unresolved_predecessors"] = unresolved
        record["body_temporary_fact_ids"] = body_gates
        record["repair_chain_id"] = repair_chain_id
        record["repair_request"] = repair_request
        if prior_report is not None:
            record["prior_verification_report"] = prior_report
        record["verification_bundle_digest"] = None
        proposals[proposal_id] = {
            "proposal_id": proposal_id,
            "candidate_id": candidate_id,
            "task_id": record["task_id"],
            "current_operation_id": record["operation_id"],
            "state": "pending",
            "canonical_id": None,
            "terminal_reason": None,
            "terminal_operation_id": None,
            "store_rejection_applied": False,
            "repair_chain_id": repair_chain_id,
            "repair_request": repair_request,
        }
        rejected_gates = [
            gate
            for gate in sorted(set(unresolved) | set(body_gates))
            if state.get("fact_proposals", {}).get(gate, {}).get("state")
            == "rejected"
        ]
        if proposal_id in set(unresolved) | set(body_gates):
            record["state"] = OperationState.REJECTED.value
            record["error"] = "fact proposal cannot cite itself as a publication gate"
        elif rejected_gates:
            record["state"] = OperationState.REJECTED.value
            record["rejected_fact_gate_ids"] = rejected_gates
            record["error"] = (
                "temporary Fact publication gate was already rejected: "
                + ", ".join(rejected_gates)
            )
        elif unresolved or self._operation_fact_gate_ids(state, record):
            record["state"] = OperationState.WAITING_PREDECESSORS.value

    def _end_attempt_from_progress(
        self,
        state: MutableMapping[str, Any],
        task: MutableMapping[str, Any],
        attempt: MutableMapping[str, Any],
        payload: Mapping[str, Any],
        progress_id: str,
    ) -> None:
        summary = payload.get("attempt_summary")
        if not isinstance(summary, Mapping):
            # A normal exit without the required durable summary is an
            # infrastructure interruption, not a mathematical failure.
            attempt["state"] = "interrupted"
            attempt["ended_reason"] = "missing_attempt_summary"
            worker_call = state["calls"].get(attempt.get("call_id"))
            if worker_call:
                _apply_call_event(
                    state,
                    worker_call,
                    call_machine.Fence(CallState.SUPERSEDED),
                )
            task["slot_reserved"] = False
            retry_count = int(task["interruption_retry_count"])
            if retry_count < int(state["retry_policy"]["worker"]):
                task["interruption_retry_count"] = retry_count + 1
                self._transition_task(state, task, TaskState.RETRY_PENDING)
                task["pending_attempt_supplement"] = infrastructure_retry_supplement(
                    "missing_attempt_summary",
                    attempt.get("supplement"),
                )
            else:
                self._transition_task(state, task, TaskState.ATTEMPT_ENDED)
                self._transition_task(state, task, TaskState.POSTPROCESSING)
                task["forced_outcome"] = TaskOutcome.INTERRUPTED.value
            return
        if attempt.get("kind") in {"fact_concession", "concession_memo_correction"}:
            attempt_operations = [
                operation
                for operation in state["operations"].values()
                if operation.get("task_id") == task["task_id"]
                and int(operation.get("attempt", -1)) == int(attempt["attempt"])
            ]
            kinds = {operation.get("kind") for operation in attempt_operations}
            if "memo" not in kinds:
                raise SchedulerError("fact concession requires a failure memo")
            forbidden = kinds - {"memo", "claim_add"}
            if forbidden:
                raise SchedulerError(
                    "fact concession may record only the required memo and surviving claims"
                )
        proposed = str(payload.get("outcome_status", summary.get("proposed_outcome", "")))
        if proposed not in {
            TaskOutcome.FINISHED.value,
            TaskOutcome.PROGRESS.value,
            TaskOutcome.FAILED.value,
        }:
            raise SchedulerError("normal final progress requires finished/progress/failed outcome")
        evidence = _validated_completion_evidence(payload, summary)
        evidence_ids = set(evidence)
        owned_operation_ids = set(task.get("operation_ids", []))
        owned_computation_ids = set(task.get("computation_ids", []))
        ambiguous = evidence_ids & owned_operation_ids & owned_computation_ids
        if ambiguous:
            raise SchedulerError(
                f"ambiguous completion evidence IDs: {sorted(ambiguous)}"
            )
        unknown = evidence_ids - (owned_operation_ids | owned_computation_ids)
        if unknown:
            raise SchedulerError(f"unknown completion evidence IDs: {sorted(unknown)}")
        attempt["state"] = "ended"
        attempt["ended_at"] = utc_now()
        attempt["final_progress_id"] = progress_id
        attempt["summary"] = copy.deepcopy(dict(summary))
        worker_call = state["calls"].get(attempt.get("call_id"))
        if worker_call:
            worker_result = {
                "final_progress_id": progress_id,
                "attempt_summary": copy.deepcopy(dict(summary)),
            }
            _apply_call_event(
                state,
                worker_call,
                call_machine.CommitWorkerResult(
                    result=worker_result,
                    result_digest=stable_digest(worker_result),
                ),
            )
        task["slot_reserved"] = False
        task["proposed_outcome"] = proposed
        task["completion_evidence_ids"] = evidence
        task["closing_summary"] = copy.deepcopy(dict(summary))
        self._transition_task(state, task, TaskState.ATTEMPT_ENDED)
        self._transition_task(state, task, TaskState.POSTPROCESSING)

    def _route_operation(self, operation_id: str) -> None:
        operation = self._state["operations"].get(operation_id)
        if not operation or operation["state"] != OperationState.RECEIVED.value:
            return
        kind = operation["kind"]
        if kind == "fact":
            # Explicit temporary predecessors and frozen exact-token prose
            # citations are publication gates.  Only the explicit dependency
            # list is ever normalized.
            if self._operation_fact_gate_ids(self._state, operation):
                with self._mutate() as state:
                    state["operations"][operation_id]["state"] = (
                        OperationState.WAITING_PREDECESSORS.value
                    )
                return
            with self._mutate() as state:
                state["operations"][operation_id]["state"] = OperationState.SYNTHESIZING.value
                append_event(state, "fact_ready_for_synthesis", {"operation_id": operation_id})
            return
        if kind in {"route_add", "obligation_add"}:
            with self._mutate() as state:
                state["operations"][operation_id]["state"] = OperationState.SYNTHESIZING.value
                append_event(state, "proposal_ready_for_synthesis", {"operation_id": operation_id})
            return
        self._commit_memory_operation(operation_id)

    def _commit_memory_operation(
        self,
        operation_id: str,
        *,
        operation_kind: str | None = None,
        payload: Mapping[str, Any] | None = None,
    ) -> str | None:
        operation = self._state["operations"].get(operation_id)
        if operation is None:
            raise SchedulerError(f"unknown operation {operation_id}")
        if operation["state"] == OperationState.COMMITTED.value:
            return operation.get("canonical_id")
        kind = operation_kind or operation["kind"]
        exact_payload = _operation_body(payload or operation["payload"])
        replayed = False
        try:
            result = self.store.apply_operation(
                operation_id,
                kind,
                exact_payload,
                proposal_id=operation["payload"].get("proposal_id"),
                actor="scheduler",
            )
            canonical_id = _canonical_id_from_result(result)
            if _status_from_result(result) != "committed":
                raise SchedulerError(_error_from_result(result) or "canonical operation rejected")
            replayed = bool(_as_dict(result).get("replayed", False))
        except Exception as exc:
            with self._mutate() as state:
                current = state["operations"][operation_id]
                current["state"] = OperationState.REJECTED.value
                current["error"] = str(exc)
                append_event(
                    state,
                    "operation_rejected",
                    {"operation_id": operation_id, "error": str(exc)},
                )
            self._on_operation_terminal(operation_id)
            self._recheck_reference_blocked_tasks()
            return None
        with self._mutate() as state:
            current = state["operations"][operation_id]
            current["state"] = OperationState.COMMITTED.value
            current["canonical_id"] = canonical_id
            proposal_id = current["payload"].get("proposal_id")
            if proposal_id and canonical_id:
                state["proposal_mappings"][str(proposal_id)] = canonical_id
            if (
                proposal_id
                and kind in {"route_update", "obligation_update"}
                and not replayed
            ):
                current["update_reference_resolution_applied"] = True
            append_event(
                state,
                "operation_committed",
                {"operation_id": operation_id, "canonical_id": canonical_id},
            )
        if canonical_id:
            self.resolve_temporary_predecessor(
                str(operation["payload"].get("proposal_id") or operation_id), canonical_id
            )
        if (
            replayed
            and operation["payload"].get("proposal_id")
            and kind in {"route_update", "obligation_update"}
        ):
            self._reconcile_committed_update_resolution(operation_id)
        self._on_operation_terminal(operation_id)
        self._recheck_reference_blocked_tasks()
        return canonical_id

    def _reconcile_committed_update_resolution(self, operation_id: str) -> bool:
        """Finish a pre-fix synthesized-update mapping tail without repatching."""

        operation = self._state["operations"].get(operation_id)
        if (
            not operation
            or operation.get("state") != OperationState.COMMITTED.value
            or operation.get("kind") not in {"route_add", "obligation_add"}
            or operation.get("synthesizer_result", {}).get("resolution") != "update"
            or operation.get("update_reference_resolution_applied")
            or not operation.get("payload", {}).get("proposal_id")
            or not operation.get("canonical_id")
            or not hasattr(self.store, "reconcile_update_resolution")
        ):
            return False
        proposal_id = str(operation["payload"]["proposal_id"])
        canonical_id = str(operation["canonical_id"])
        attention_key = f"operation:{operation_id}:update-reference-resolution"
        try:
            affected = tuple(
                self.store.reconcile_update_resolution(
                    operation_id,
                    proposal_id,
                    canonical_id,
                    actor="scheduler-recovery",
                )
            )
        except Exception as exc:
            with self._mutate() as state:
                self._add_attention(
                    state,
                    attention_key,
                    "update_reference_resolution_failed",
                    {
                        "operation_id": operation_id,
                        "proposal_id": proposal_id,
                        "canonical_id": canonical_id,
                        "error": str(exc),
                    },
                )
            return False
        with self._mutate() as state:
            current = state["operations"].get(operation_id)
            if (
                not current
                or current.get("state") != OperationState.COMMITTED.value
                or str(current.get("canonical_id") or "") != canonical_id
                or str(current.get("payload", {}).get("proposal_id") or "")
                != proposal_id
            ):
                raise IdempotencyConflict(
                    "synthesized update changed during reference reconciliation"
                )
            current["update_reference_resolution_applied"] = True
            self._resolve_attention(state, attention_key)
            append_event(
                state,
                "update_reference_resolution_reconciled",
                {
                    "operation_id": operation_id,
                    "proposal_id": proposal_id,
                    "canonical_id": canonical_id,
                    "affected_source_ids": list(affected),
                },
            )
        self._recheck_reference_blocked_tasks()
        return True

    def ingest_review_computations(
        self,
        call_id: str,
        computations: Iterable[Mapping[str, Any]],
    ) -> dict[str, str]:
        """Persist optional verifier CAS records without changing its verdict.

        The review call, originating task/attempt, and computation bytes have
        already been authenticated by the runtime.  This method only records
        provenance and routes each record through the ordinary computation
        publication path.  Replays are exact and idempotent.
        """

        values = [copy.deepcopy(dict(value)) for value in computations]
        staging_ids: list[str] = []
        with self._mutate() as state:
            call = state["calls"].get(call_id)
            if not call or call.get("kind") not in {"verifier", "challenge-verifier"}:
                raise SchedulerError("review computations require a verifier call")
            if call.get("status") not in {
                CallState.COMPLETED.value,
                CallState.COMMITTED.value,
            }:
                raise WorkflowError("verifier call has no completed result")
            if call["kind"] == "verifier":
                source_id = str(call.get("continuation", {}).get("operation_id") or "")
                source = state["operations"].get(source_id)
                if not source:
                    raise SchedulerError("verifier computation has no originating operation")
                if (
                    call.get("status") != CallState.COMMITTED.value
                    and not self._source_attempt_is_terminal_locked(state, source)
                ):
                    raise WorkflowError(
                        f"operation {source_id} cannot be verified before its worker attempt ends"
                    )
                task_id = str(source.get("task_id") or "")
                attempt = int(source.get("attempt", 0))
            else:
                source_id = str(call.get("continuation", {}).get("challenge_id") or "")
                source = state["challenges"].get(source_id)
                if not source:
                    raise SchedulerError("verifier computation has no originating challenge")
                task_id = str(source.get("task_id") or "")
                attempt = int(source.get("attempt", 0))
                if attempt <= 0:
                    matching_progress = [
                        progress
                        for progress in state["progress"].values()
                        if source_id in progress.get("challenge_ids", [])
                    ]
                    if len(matching_progress) != 1:
                        raise SchedulerError(
                            "challenge computation has ambiguous originating attempt"
                        )
                    attempt = int(matching_progress[0].get("attempt", 0))
            task = state["tasks"].get(task_id)
            if not task or attempt <= 0:
                raise SchedulerError("verifier computation has no originating task attempt")

            seen: set[str] = set()
            for computation in values:
                staging_id = str(
                    computation.get("staging_id")
                    or computation.get("operation_id")
                    or ""
                )
                if not staging_id:
                    raise SchedulerError("verifier computation requires a staging ID")
                if staging_id in seen:
                    raise SchedulerError("verifier computation staging ID is duplicated")
                seen.add(staging_id)
                digest = stable_digest(computation)
                existing = state["computations"].get(staging_id)
                if existing:
                    if (
                        existing.get("input_digest") != digest
                        or existing.get("task_id") != task_id
                        or existing.get("source_call_id") != call_id
                        or int(existing.get("source_attempt", 0)) != attempt
                    ):
                        raise IdempotencyConflict(
                            f"computation {staging_id} replayed from a different source"
                        )
                else:
                    state["computations"][staging_id] = {
                        "staging_id": staging_id,
                        "task_id": task_id,
                        "payload": computation,
                        "input_digest": digest,
                        "state": OperationState.RECEIVED.value,
                        "canonical_id": None,
                        "source_call_id": call_id,
                        "source_kind": str(call["kind"]),
                        "source_id": source_id,
                        "source_attempt": attempt,
                    }
                    append_event(
                        state,
                        "verifier_computation_received",
                        {
                            "staging_id": staging_id,
                            "call_id": call_id,
                            "task_id": task_id,
                            "attempt": attempt,
                            "source_id": source_id,
                        },
                    )
                staging_ids.append(staging_id)
                if staging_id not in task["computation_ids"]:
                    task["computation_ids"].append(staging_id)
                call.setdefault("computation_ids", [])
                if staging_id not in call["computation_ids"]:
                    call["computation_ids"].append(staging_id)

        for staging_id in staging_ids:
            self._commit_computation(staging_id)
        return {
            staging_id: str(self._state["computations"][staging_id]["state"])
            for staging_id in staging_ids
        }

    def _commit_computation(self, staging_id: str) -> None:
        computation = self._state["computations"].get(staging_id)
        if not computation or computation["state"] != OperationState.RECEIVED.value:
            return
        computation_payload = _operation_body(computation["payload"])
        # Provenance is scheduler-owned; a verifier workspace has no task card
        # and an agent-supplied task ID must never override the originating one.
        computation_payload["task_id"] = computation["task_id"]
        try:
            if hasattr(self.store, "add_computation"):
                result = self.store.add_computation(
                    f"computation:{staging_id}",
                    computation_payload,
                    actor="scheduler",
                )
            else:
                result = self.store.apply_operation(
                    f"computation:{staging_id}",
                    "computation",
                    computation_payload,
                    proposal_id=staging_id,
                    actor="scheduler",
                )
            canonical_id = _canonical_id_from_result(result)
            if _status_from_result(result) != "committed":
                raise SchedulerError(_error_from_result(result) or "computation rejected")
            state_value = OperationState.COMMITTED.value
            error = None
        except Exception as exc:
            canonical_id = None
            state_value = OperationState.REJECTED.value
            error = str(exc)
        with self._mutate() as state:
            current = state["computations"][staging_id]
            current["state"] = state_value
            current["canonical_id"] = canonical_id
            current["error"] = error
            append_event(
                state,
                "computation_terminal",
                {"staging_id": staging_id, "state": state_value, "canonical_id": canonical_id},
            )

    def resolve_temporary_predecessor(self, temporary_id: str, canonical_fact_id: str) -> None:
        """Resolve one Fact proposal and release every publication gate.

        Explicit dependency metadata is normalized into a new scheduler-owned
        candidate version.  Frozen citations in ``statement`` and ``proof``
        only gate publication: their text is deliberately never rewritten.
        """
        if not canonical_fact_id.startswith("F-"):
            return
        newly_ready: list[str] = []
        with self._mutate() as state:
            self._ensure_fact_proposals_locked(state)
            proposal = state.setdefault("fact_proposals", {}).get(temporary_id)
            if proposal is not None and proposal.get("state") == "rejected":
                raise IdempotencyConflict(
                    f"rejected Fact proposal {temporary_id} cannot later resolve"
                )
            state["proposal_mappings"][temporary_id] = canonical_fact_id
            if proposal is not None:
                proposal["state"] = "resolved"
                proposal["canonical_id"] = canonical_fact_id
                proposal["resolved_at"] = utc_now()
            blocked = [
                op
                for op in state["operations"].values()
                if op["kind"] == "fact"
                and op["state"] == OperationState.WAITING_PREDECESSORS.value
                and (
                    temporary_id in self._temporary_fact_predecessor_ids(op)
                    or temporary_id in op.get("body_temporary_fact_ids", [])
                )
                and self._is_current_fact_proposal_operation_locked(state, op)
            ]
            for old in blocked:
                predecessors = self._fact_payload_predecessors(old["payload"])
                changed = temporary_id in predecessors
                if not changed:
                    if not self._operation_fact_gate_ids(state, old):
                        old["state"] = OperationState.RECEIVED.value
                        newly_ready.append(str(old["operation_id"]))
                    append_event(
                        state,
                        "fact_body_publication_gate_resolved",
                        {
                            "operation_id": old["operation_id"],
                            "temporary_id": temporary_id,
                            "canonical_id": canonical_fact_id,
                        },
                    )
                    continue
                normalized_predecessors: list[str] = []
                seen_predecessors: set[str] = set()
                for predecessor in predecessors:
                    normalized = (
                        canonical_fact_id
                        if predecessor == temporary_id
                        else predecessor
                    )
                    # A duplicate/update may merge a temporary proposal into
                    # an already-listed canonical Fact.  Collapse that
                    # relationship deterministically instead of rejecting the
                    # dependent Fact.
                    if normalized in seen_predecessors:
                        continue
                    seen_predecessors.add(normalized)
                    normalized_predecessors.append(normalized)
                old_payload = copy.deepcopy(dict(old["payload"]))
                old_payload["predecessor_fact_ids"] = normalized_predecessors
                unresolved = [
                    item
                    for item in normalized_predecessors
                    if not item.startswith("F-")
                    and not state.get("proposal_mappings", {}).get(item)
                ]
                old["state"] = OperationState.ABANDONED.value
                old["normalized_to"] = None
                candidate_id = old["candidate_id"]
                lineage = state["fact_lineages"][candidate_id]
                next_version = max(int(v) for v in lineage["versions"]) + 1
                new_operation_id = f"{old['operation_id']}:normalized:{next_version}"
                old_payload["candidate_version"] = next_version
                old_payload["operation_id"] = new_operation_id
                new_record = {
                    "operation_id": new_operation_id,
                    "task_id": old["task_id"],
                    "attempt": old["attempt"],
                    "progress_id": old["progress_id"],
                    "kind": "fact",
                    "payload": old_payload,
                    "input_digest": stable_digest(old_payload),
                    "state": OperationState.WAITING_PREDECESSORS.value,
                    "canonical_id": None,
                    "error": None,
                    "candidate_id": candidate_id,
                    "candidate_version": next_version,
                    "unresolved_predecessors": unresolved,
                    "body_temporary_fact_ids": copy.deepcopy(
                        old.get("body_temporary_fact_ids", [])
                    ),
                    "repair_chain_id": old.get("repair_chain_id"),
                    "repair_request": int(old.get("repair_request", 0)),
                    "verification_bundle_digest": None,
                    "normalized_from": old["operation_id"],
                }
                if old.get("prior_verification_report") is not None:
                    new_record["prior_verification_report"] = copy.deepcopy(
                        old["prior_verification_report"]
                    )
                old["normalized_to"] = new_operation_id
                state["operations"][new_operation_id] = new_record
                lineage["versions"][str(next_version)] = new_operation_id
                dependent_proposal_id = str(old_payload.get("proposal_id") or "")
                if dependent_proposal_id:
                    dependent_proposal = state.setdefault("fact_proposals", {}).get(
                        dependent_proposal_id
                    )
                    if dependent_proposal is not None:
                        dependent_proposal["current_operation_id"] = new_operation_id
                task = state["tasks"][old["task_id"]]
                task["operation_ids"].append(new_operation_id)
                if not self._operation_fact_gate_ids(state, new_record):
                    new_record["state"] = OperationState.RECEIVED.value
                    newly_ready.append(new_operation_id)
                append_event(
                    state,
                    "fact_candidate_normalized",
                    {
                        "from_operation_id": old["operation_id"],
                        "to_operation_id": new_operation_id,
                    },
                )
        for operation_id in newly_ready:
            self._route_operation(operation_id)

    @staticmethod
    def _fact_proposal_id_of(operation: Mapping[str, Any]) -> str:
        payload = operation.get("payload")
        if not isinstance(payload, Mapping):
            return ""
        return str(payload.get("proposal_id") or "")

    def _operation_may_terminalize_fact_proposal(
        self,
        state: Mapping[str, Any],
        operation: Mapping[str, Any],
    ) -> bool:
        """Return whether a rejected receipt owns the exact Fact proposal.

        An access-denied operation may reuse an ID owned by another task (or
        another memory type).  Such a receipt is terminal itself, but it must
        never acquire authority to reject the legitimate proposal and its
        dependent graph during ingestion or recovery.
        """

        payload = operation.get("payload")
        if not isinstance(payload, Mapping):
            return False
        raw_proposal_id = payload.get("proposal_id")
        if (
            not isinstance(raw_proposal_id, str)
            or not raw_proposal_id
            or raw_proposal_id != raw_proposal_id.strip()
        ):
            return False
        proposal_id = raw_proposal_id
        if proposal_id in state.get("proposal_mappings", {}):
            return False
        if self._canonical_memory_exists(proposal_id):
            return False
        proposal = state.get("fact_proposals", {}).get(proposal_id)
        if isinstance(proposal, Mapping):
            return bool(
                proposal.get("current_operation_id") == operation.get("operation_id")
                and proposal.get("task_id") == operation.get("task_id")
            )
        return not any(
            other.get("operation_id") != operation.get("operation_id")
            and isinstance(other.get("payload"), Mapping)
            and other["payload"].get("proposal_id") == proposal_id
            for other in state.get("operations", {}).values()
        )

    def _ensure_fact_proposals_locked(
        self, state: MutableMapping[str, Any]
    ) -> None:
        """Lazily reconstruct proposal control records for older snapshots."""

        proposals = state.setdefault("fact_proposals", {})
        fact_operations = [
            operation
            for operation in state.get("operations", {}).values()
            if operation.get("kind") == "fact"
            and self._fact_proposal_id_of(operation)
        ]
        fact_operations.sort(
            key=lambda operation: (
                self._fact_proposal_id_of(operation),
                int(operation.get("candidate_version", 0)),
                str(operation.get("operation_id") or ""),
            )
        )
        for operation in fact_operations:
            proposal_id = self._fact_proposal_id_of(operation)
            candidate_id = str(
                operation.get("candidate_id")
                or operation.get("payload", {}).get("candidate_id")
                or ""
            )
            if candidate_id:
                lineage = state.setdefault("fact_lineages", {}).setdefault(
                    candidate_id,
                    {
                        "candidate_id": candidate_id,
                        "task_id": operation.get("task_id"),
                        "proposal_id": proposal_id,
                        "versions": {},
                        "revision_requests": int(
                            operation.get("repair_request", 0)
                        ),
                        "rejected_bundle_digests": [],
                        "concession_required": False,
                        "closed": operation.get("state")
                        in FINAL_OPERATION_STATES,
                        "repair_chain_id": operation.get(
                            "repair_chain_id", proposal_id
                        ),
                    },
                )
                version = int(
                    operation.get("candidate_version")
                    or operation.get("payload", {}).get("candidate_version")
                    or 0
                )
                if version > 0:
                    lineage.setdefault("versions", {}).setdefault(
                        str(version), operation.get("operation_id")
                    )
            current = proposals.setdefault(
                proposal_id,
                {
                    "proposal_id": proposal_id,
                    "candidate_id": candidate_id,
                    "task_id": operation.get("task_id"),
                    "current_operation_id": operation.get("operation_id"),
                    "state": "pending",
                    "canonical_id": None,
                    "terminal_reason": None,
                    "terminal_operation_id": None,
                    "store_rejection_applied": False,
                    "repair_chain_id": operation.get("repair_chain_id", proposal_id),
                    "repair_request": int(operation.get("repair_request", 0)),
                },
            )
            current_operation = state.get("operations", {}).get(
                current.get("current_operation_id"), {}
            )
            if int(operation.get("candidate_version", 0)) >= int(
                current_operation.get("candidate_version", 0)
            ):
                current["current_operation_id"] = operation.get("operation_id")
                current["candidate_id"] = candidate_id
                current["task_id"] = operation.get("task_id")
            canonical_id = state.get("proposal_mappings", {}).get(proposal_id)
            selected_operation = state.get("operations", {}).get(
                current.get("current_operation_id"), {}
            )
            if (
                not canonical_id
                and selected_operation.get("state")
                == OperationState.COMMITTED.value
                and selected_operation.get("canonical_id")
            ):
                canonical_id = str(selected_operation["canonical_id"])
                state.setdefault("proposal_mappings", {})[
                    proposal_id
                ] = canonical_id
            if canonical_id:
                current["state"] = "resolved"
                current["canonical_id"] = canonical_id

    def _is_current_fact_proposal_operation_locked(
        self, state: MutableMapping[str, Any], operation: Mapping[str, Any]
    ) -> bool:
        proposal_id = self._fact_proposal_id_of(operation)
        if not proposal_id:
            return False
        self._ensure_fact_proposals_locked(state)
        proposal = state.get("fact_proposals", {}).get(proposal_id, {})
        return proposal.get("current_operation_id") == operation.get("operation_id")

    @staticmethod
    def _fence_fact_operation_calls_locked(
        state: MutableMapping[str, Any], operation: Mapping[str, Any]
    ) -> None:
        for pointer in ("synthesizer_call_id", "verifier_call_id"):
            call_id = operation.get(pointer)
            call = state.get("calls", {}).get(call_id)
            if call and call.get("status") not in {
                CallState.COMMITTED.value,
                CallState.SUPERSEDED.value,
                CallState.CANCELLED.value,
            }:
                _apply_call_event(
                    state,
                    call,
                    call_machine.Fence(CallState.SUPERSEDED),
                )

    def _reject_fact_proposal_cascade_locked(
        self,
        state: MutableMapping[str, Any],
        temporary_id: str,
        *,
        reason: str,
        predecessor_operation_id: str | None,
    ) -> list[str]:
        """Reject one terminal proposal and its unpublished transitive closure."""

        self._ensure_fact_proposals_locked(state)
        proposals = state.setdefault("fact_proposals", {})
        if temporary_id not in proposals:
            matching = [
                operation
                for operation in state.get("operations", {}).values()
                if operation.get("kind") == "fact"
                and self._fact_proposal_id_of(operation) == temporary_id
            ]
            if not matching:
                proposals[temporary_id] = {
                    "proposal_id": temporary_id,
                    "candidate_id": "",
                    "task_id": None,
                    "current_operation_id": predecessor_operation_id,
                    "state": "pending",
                    "canonical_id": None,
                    "terminal_reason": None,
                    "terminal_operation_id": None,
                    "store_rejection_applied": False,
                    "repair_chain_id": temporary_id,
                    "repair_request": 0,
                }

        dependents: dict[str, set[str]] = {}
        for proposal_id, proposal in proposals.items():
            operation = state.get("operations", {}).get(
                proposal.get("current_operation_id")
            )
            if not isinstance(operation, Mapping):
                continue
            if operation.get("kind") != "fact" or operation.get("state") == (
                OperationState.COMMITTED.value
            ):
                continue
            for gate_id in self._operation_fact_gate_ids(state, operation):
                dependents.setdefault(gate_id, set()).add(str(proposal_id))

        queue = [temporary_id]
        visited: set[str] = set()
        rejected_operation_ids: list[str] = []
        changed = False
        while queue:
            proposal_id = queue.pop(0)
            if proposal_id in visited:
                continue
            visited.add(proposal_id)
            proposal = proposals.get(proposal_id)
            if proposal is None:
                continue
            if proposal.get("state") == "resolved" or state.get(
                "proposal_mappings", {}
            ).get(proposal_id):
                self._add_attention(
                    state,
                    f"fact-proposal:{proposal_id}:terminal-conflict",
                    "resolved_fact_proposal_rejection_conflict",
                    {"proposal_id": proposal_id, "reason": reason},
                )
                continue
            operation = state.get("operations", {}).get(
                proposal.get("current_operation_id")
            )
            is_root = proposal_id == temporary_id
            if isinstance(operation, MutableMapping) and operation.get("state") != (
                OperationState.COMMITTED.value
            ):
                self._fence_fact_operation_calls_locked(state, operation)
                if not is_root and (
                    operation.get("state") != OperationState.ABANDONED.value
                    and (
                        operation.get("state") != OperationState.REJECTED.value
                        or not operation.get("rejected_predecessor")
                    )
                ):
                    triggering_gate = next(
                        (
                            gate
                            for gate in self._operation_fact_gate_ids(state, operation)
                            if gate in visited
                        ),
                        temporary_id,
                    )
                    self._return_dependent_candidate_locked(
                        state,
                        operation,
                        temporary_id=triggering_gate,
                        reason=reason,
                        predecessor_operation_id=predecessor_operation_id,
                    )
                    changed = True
                elif operation.get("state") not in {
                    OperationState.REJECTED.value,
                    OperationState.ABANDONED.value,
                }:
                    operation["state"] = OperationState.REJECTED.value
                    operation["error"] = reason
                    changed = True
                rejected_operation_ids.append(str(operation.get("operation_id")))
                candidate_id = str(operation.get("candidate_id") or "")
                lineage = state.get("fact_lineages", {}).get(candidate_id)
                if isinstance(lineage, MutableMapping):
                    lineage["closed"] = True
            if proposal.get("state") != "rejected" or not proposal.get(
                "cascade_applied"
            ):
                changed = True
            proposal["state"] = "rejected"
            proposal["canonical_id"] = None
            proposal.setdefault("terminal_reason", None)
            if not proposal.get("terminal_reason"):
                proposal["terminal_reason"] = reason
            proposal.setdefault("terminal_operation_id", None)
            if not proposal.get("terminal_operation_id"):
                proposal["terminal_operation_id"] = (
                    predecessor_operation_id
                    or (
                        operation.get("operation_id")
                        if isinstance(operation, Mapping)
                        else None
                    )
                )
            proposal["store_rejection_operation_id"] = (
                "fact-proposal-terminal:"
                + stable_digest({"proposal_id": proposal_id})[:24]
            )
            proposal.setdefault("store_rejection_applied", False)
            proposal["cascade_applied"] = True
            for dependent_id in sorted(dependents.get(proposal_id, ())):
                if dependent_id not in visited:
                    queue.append(dependent_id)

        if changed:
            append_event(
                state,
                "fact_proposal_rejection_cascade",
                {
                    "root_proposal_id": temporary_id,
                    "reason": reason,
                    "predecessor_operation_id": predecessor_operation_id,
                    "rejected_proposal_ids": sorted(visited),
                    "rejected_operation_ids": sorted(set(rejected_operation_ids)),
                },
            )
        return sorted(set(rejected_operation_ids))

    def _return_dependent_candidate_locked(
        self,
        state: MutableMapping[str, Any],
        operation: MutableMapping[str, Any],
        *,
        temporary_id: str,
        reason: str,
        predecessor_operation_id: str | None,
    ) -> None:
        operation["state"] = OperationState.REJECTED.value
        operation["error"] = f"temporary predecessor {temporary_id} rejected: {reason}"
        operation["rejected_predecessor"] = {
            "temporary_id": temporary_id,
            "predecessor_operation_id": predecessor_operation_id,
            "reason": reason,
        }
        task = state["tasks"][operation["task_id"]]
        rejection = {
            "temporary_id": temporary_id,
            "dependent_operation_id": operation["operation_id"],
            "predecessor_operation_id": predecessor_operation_id,
            "reason": reason,
        }
        supplement = task.get("dependency_repair_required")
        if not isinstance(supplement, Mapping) or supplement.get("kind") != "predecessor_rejected":
            supplement = {
                "kind": "predecessor_rejected",
                "rejections": [],
            }
        else:
            supplement = copy.deepcopy(dict(supplement))
        if rejection not in supplement["rejections"]:
            supplement["rejections"].append(rejection)
        supplement.update({
            "kind": "predecessor_rejected",
            "temporary_id": temporary_id,
            "dependent_operation_id": operation["operation_id"],
            "predecessor_operation_id": predecessor_operation_id,
            "reason": reason,
        })
        task["dependency_repair_required"] = copy.deepcopy(supplement)
        current = TaskState(task["state"])
        existing_semantic = preserved_attempt_supplement(
            task.get("pending_attempt_supplement")
        )
        has_priority_fact_repair = bool(
            isinstance(existing_semantic, Mapping)
            and existing_semantic.get("kind")
            in {
                "verifier_revision",
                "fact_concession",
                "identical_rejected_bundle",
            }
        )
        if has_priority_fact_repair and isinstance(
            task.get("pending_attempt_supplement"), Mapping
        ):
            priority_supplement = copy.deepcopy(
                dict(task["pending_attempt_supplement"])
            )
            priority_supplement["dependency_rejections"] = copy.deepcopy(
                supplement.get("rejections", [])
            )
            task["pending_attempt_supplement"] = priority_supplement
        if current in {TaskState.POSTPROCESSING, TaskState.ATTEMPT_ENDED}:
            self._transition_task(state, task, TaskState.REVISION_PENDING)
            if not has_priority_fact_repair:
                task["pending_attempt_supplement"] = copy.deepcopy(supplement)
        elif current is TaskState.REVISION_PENDING and not has_priority_fact_repair:
            task["pending_attempt_supplement"] = copy.deepcopy(supplement)
        append_event(
            state,
            "dependent_fact_returned",
            {
                "operation_id": operation["operation_id"],
                "temporary_id": temporary_id,
                "predecessor_operation_id": predecessor_operation_id,
            },
        )

    def _terminalize_unpublishable_final_fact_gates(self, task_id: str) -> None:
        """Reject absent Fact targets once the task's final receipt is fixed."""

        while True:
            unresolved_roots: list[str] = []
            with self._mutate() as state:
                self._ensure_fact_proposals_locked(state)
                proposals = state.get("fact_proposals", {})
                current_fact_operations: list[Mapping[str, Any]] = []
                for operation in state.get("operations", {}).values():
                    if (
                        operation.get("task_id") != task_id
                        or operation.get("kind") != "fact"
                        or operation.get("state")
                        not in {
                            OperationState.WAITING_PREDECESSORS.value,
                            OperationState.RECEIVED.value,
                            OperationState.SYNTHESIZING.value,
                            OperationState.VERIFYING.value,
                            OperationState.NEEDS_ATTENTION.value,
                        }
                        or not self._is_current_fact_proposal_operation_locked(
                            state, operation
                        )
                    ):
                        continue
                    current_fact_operations.append(operation)
                    for gate_id in self._operation_fact_gate_ids(state, operation):
                        target = proposals.get(gate_id)
                        if target is None:
                            unresolved_roots.append(gate_id)
                            continue
                        if target.get("state") == "rejected":
                            unresolved_roots.append(gate_id)
                            continue
                        target_operation = state.get("operations", {}).get(
                            target.get("current_operation_id")
                        )
                        if target.get("state") == "resolved":
                            continue
                        if not isinstance(target_operation, Mapping) or target_operation.get(
                            "state"
                        ) in {
                            OperationState.REJECTED.value,
                            OperationState.ABANDONED.value,
                        }:
                            unresolved_roots.append(gate_id)
                # A declared target is not sufficient when all remaining
                # targets form a WAITING cycle.  Compute the least fixed point
                # of proposals that still have a path to publication; a closed
                # SCC with no ready/verifying member is terminally impossible.
                publishable = {
                    str(proposal_id)
                    for proposal_id, proposal in proposals.items()
                    if proposal.get("state") == "resolved"
                    or state.get("proposal_mappings", {}).get(str(proposal_id))
                }
                for operation in current_fact_operations:
                    if operation.get("state") != OperationState.WAITING_PREDECESSORS.value:
                        proposal_id = self._fact_proposal_id_of(operation)
                        if proposal_id:
                            publishable.add(proposal_id)
                made_progress = True
                while made_progress:
                    made_progress = False
                    for operation in current_fact_operations:
                        proposal_id = self._fact_proposal_id_of(operation)
                        if not proposal_id or proposal_id in publishable:
                            continue
                        gates = self._operation_fact_gate_ids(state, operation)
                        if all(gate_id in publishable for gate_id in gates):
                            publishable.add(proposal_id)
                            made_progress = True
                for operation in current_fact_operations:
                    if operation.get("state") != OperationState.WAITING_PREDECESSORS.value:
                        continue
                    for gate_id in self._operation_fact_gate_ids(state, operation):
                        target = proposals.get(gate_id)
                        if (
                            isinstance(target, Mapping)
                            and target.get("state") == "pending"
                            and gate_id not in publishable
                        ):
                            unresolved_roots.append(gate_id)
                unresolved_roots = sorted(set(unresolved_roots))
            if not unresolved_roots:
                return
            for gate_id in unresolved_roots:
                self.reject_temporary_predecessor(
                    gate_id,
                    reason=(
                        f"task {task_id} finalized without publishing the temporary "
                        "Fact gate"
                    ),
                    predecessor_operation_id=f"task-final:{task_id}",
                )
            # Cascades are transitive; one more scan handles a legacy snapshot
            # whose proposal-control record was reconstructed during this pass.

    def reject_temporary_predecessor(
        self,
        temporary_id: str,
        *,
        reason: str,
        predecessor_operation_id: str | None = None,
    ) -> tuple[str, ...]:
        """Terminalize a Fact proposal and reject its unpublished closure."""

        temporary_id = str(temporary_id).strip()
        reason = str(reason).strip()
        if not temporary_id or not reason:
            raise SchedulerError("temporary predecessor rejection requires an ID and reason")
        returned: list[str]
        with self._mutate() as state:
            returned = self._reject_fact_proposal_cascade_locked(
                state,
                temporary_id,
                reason=reason,
                predecessor_operation_id=predecessor_operation_id,
            )
        self._reconcile_fact_proposal_rejection_tails()
        return tuple(sorted(returned))

    def _reconcile_fact_proposal_rejection_tails(self) -> None:
        """Replay canonical-store terminal markers for rejected Fact proposals."""

        if not hasattr(self.store, "reject_temporary_reference"):
            return
        with self._mutate() as state:
            self._ensure_fact_proposals_locked(state)
        pending = [
            copy.deepcopy(dict(proposal))
            for proposal in self._state.get("fact_proposals", {}).values()
            if proposal.get("state") == "rejected"
            and not proposal.get("store_rejection_applied")
        ]
        pending.sort(key=lambda proposal: str(proposal.get("proposal_id") or ""))
        for proposal in pending:
            proposal_id = str(proposal.get("proposal_id") or "")
            if not proposal_id:
                continue
            operation_id = str(
                proposal.get("store_rejection_operation_id")
                or "fact-proposal-terminal:"
                + stable_digest({"proposal_id": proposal_id})[:24]
            )
            reason = str(
                proposal.get("terminal_reason") or "Fact proposal was not published"
            )
            try:
                self.store.reject_temporary_reference(
                    proposal_id,
                    "fact",
                    reason=reason,
                    operation_id=operation_id,
                    actor="scheduler",
                )
            except Exception as exc:
                with self._mutate() as state:
                    current = state.setdefault("fact_proposals", {}).get(proposal_id)
                    if current and current.get("state") == "rejected":
                        current["store_rejection_error"] = str(exc)
                        current["store_rejection_attempted_at"] = utc_now()
                continue
            with self._mutate() as state:
                current = state.setdefault("fact_proposals", {}).get(proposal_id)
                if current and current.get("state") == "rejected":
                    current["store_rejection_applied"] = True
                    current["store_rejection_applied_at"] = utc_now()
                    current.pop("store_rejection_error", None)
                    append_event(
                        state,
                        "fact_proposal_store_rejection_applied",
                        {
                            "proposal_id": proposal_id,
                            "operation_id": operation_id,
                        },
                    )

    # --------------------------------------------------- synthesis/verification

    @staticmethod
    def _synthesis_memory_type(operation_kind: str) -> str:
        try:
            return {
                "fact": "fact",
                "route_add": "route",
                "obligation_add": "obligation",
            }[operation_kind]
        except KeyError as exc:
            raise SchedulerError(
                f"operation kind {operation_kind!r} has no synthesis scope"
            ) from exc

    def _synthesis_scope_snapshot(self, operation_kind: str) -> list[dict[str, Any]]:
        """Return the cheap same-type canonical view used to fence a review.

        The snapshot contains identity, revisions, status, and abstract only;
        it does not force the synthesizer to open full records.  Any same-type
        canonical delta is returned to a fresh reconfirmation call because the
        deterministic scheduler cannot decide mathematical relevance itself.
        """

        memory_type = self._synthesis_memory_type(operation_kind)
        raw_records: Iterable[Any]
        if hasattr(self.store, "list_records"):
            raw_records = self.store.list_records(types=[memory_type], include_inactive=True)
        elif isinstance(getattr(self.store, "records", None), Mapping):
            raw_records = getattr(self.store, "records").values()
        else:
            raw_records = ()
        snapshot: list[dict[str, Any]] = []
        for raw in raw_records:
            record = _as_dict(raw)
            actual_type = str(record.get("type", record.get("memory_type", "")))
            if actual_type.startswith("MemoryType."):
                actual_type = actual_type.rsplit(".", 1)[-1].lower()
            if actual_type != memory_type:
                continue
            snapshot.append(
                {
                    "id": str(record.get("id", record.get("memory_id", ""))),
                    "revision": int(record.get("revision", 1)),
                    "metadata_version": int(record.get("metadata_version", 1)),
                    "status": str(record.get("status", "active")),
                    "abstract": str(record.get("abstract", "")),
                }
            )
        return sorted(snapshot, key=lambda item: item["id"])

    @staticmethod
    def _synthesis_scope_delta(
        previous: Sequence[Mapping[str, Any]], current: Sequence[Mapping[str, Any]]
    ) -> dict[str, list[dict[str, Any]]]:
        before = {str(item["id"]): dict(item) for item in previous}
        after = {str(item["id"]): dict(item) for item in current}
        return {
            "added": [after[item] for item in sorted(after.keys() - before.keys())],
            "removed": [before[item] for item in sorted(before.keys() - after.keys())],
            "changed": [
                {"before": before[item], "after": after[item]}
                for item in sorted(before.keys() & after.keys())
                if before[item] != after[item]
            ],
        }

    def synthesizer_input(self, operation_id: str) -> dict[str, Any]:
        operation = self._state["operations"].get(operation_id)
        if not operation:
            raise SchedulerError(f"unknown operation {operation_id}")
        if operation["state"] != OperationState.SYNTHESIZING.value:
            raise WorkflowError(f"operation {operation_id} is not awaiting synthesis")
        scope = self._synthesis_scope_snapshot(operation["kind"])
        value = {
            "operation_id": operation_id,
            "operation_digest": operation["input_digest"],
            "proposal": copy.deepcopy(operation["payload"]),
            "event_cursor": self.event_cursor,
            "review_scope_digest": stable_digest(scope),
            "review_scope": scope,
        }
        value.update(
            self._advisor_problem_binding_locked(
                self._state, task_id=str(operation.get("task_id") or "")
            )
        )
        reconfirmation = operation.get("synthesizer_reconfirmation")
        if reconfirmation:
            value["prior_review"] = copy.deepcopy(reconfirmation["prior_review"])
            value["delta_since_prior_review"] = copy.deepcopy(
                reconfirmation["delta"]
            )
        return value

    def prepare_synthesizer_call(self, operation_id: str) -> str:
        operation = self._state["operations"].get(operation_id)
        if operation and operation.get("synthesizer_call_id"):
            return str(operation["synthesizer_call_id"])
        exact_input = self.synthesizer_input(operation_id)
        return self._prepare_owned_call(
            "synthesizer",
            exact_input,
            owner_collection="operations",
            owner_id=operation_id,
            pointer_field="synthesizer_call_id",
            continuation={"operation_id": operation_id},
            owner_updates={
                "synthesizer_scope": copy.deepcopy(exact_input["review_scope"]),
                "synthesizer_scope_digest": exact_input["review_scope_digest"],
            },
        )

    def commit_synthesizer_call(self, call_id: str) -> None:
        call = self._state["calls"].get(call_id)
        if not call or call["kind"] != "synthesizer":
            raise SchedulerError("unknown synthesizer call")
        if call["status"] == CallState.COMMITTED.value:
            return
        if call["status"] != CallState.COMPLETED.value:
            raise WorkflowError("synthesizer call has no completed result")
        operation_id = str(call["continuation"]["operation_id"])
        self.apply_synthesizer_result(operation_id, call["result"])
        self.mark_call_committed(call_id)

    def apply_synthesizer_result(
        self, operation_id: str, result: Mapping[str, Any]
    ) -> bool:
        report = copy.deepcopy(dict(result))
        resolution = str(report.get("resolution") or "")
        if resolution not in {"new", "duplicate", "update"}:
            raise SchedulerError("synthesizer resolution must be new, duplicate, or update")
        operation = self._state["operations"].get(operation_id)
        if not operation:
            raise SchedulerError(f"unknown operation {operation_id}")
        if operation["state"] != OperationState.SYNTHESIZING.value:
            if operation.get("synthesizer_result_digest") == stable_digest(report):
                return True
            raise WorkflowError("operation is not awaiting synthesizer result")
        if report.get("operation_digest") and report["operation_digest"] != operation["input_digest"]:
            raise SchedulerError("synthesizer report has the wrong operation digest")
        previous_scope = operation.get("synthesizer_scope", [])
        current_scope = self._synthesis_scope_snapshot(operation["kind"])
        previous_digest = operation.get("synthesizer_scope_digest")
        current_digest = stable_digest(current_scope)
        if previous_digest and previous_digest != current_digest:
            with self._mutate() as state:
                current = state["operations"][operation_id]
                current["synthesizer_reconfirmation"] = {
                    "prior_review": report,
                    "prior_scope_digest": previous_digest,
                    "current_scope_digest": current_digest,
                    "delta": self._synthesis_scope_delta(previous_scope, current_scope),
                }
                current["synthesizer_call_id"] = None
                append_event(
                    state,
                    "synthesizer_reconfirmation_required",
                    {
                        "operation_id": operation_id,
                        "prior_scope_digest": previous_digest,
                        "current_scope_digest": current_digest,
                    },
                )
            return False
        relied_on = report.get("relied_on", [])
        if not isinstance(relied_on, list):
            raise SchedulerError("synthesizer relied_on must be a list")
        for raw in relied_on:
            if not isinstance(raw, Mapping) or not raw.get("id"):
                raise SchedulerError("each synthesizer relied_on entry requires an ID")
            memory_id = str(raw["id"])
            try:
                record = self.store.get(memory_id) if hasattr(self.store, "get") else None
            except NotFoundError as exc:
                raise SchedulerError(
                    f"synthesizer relied on missing memory {memory_id}"
                ) from exc
            if record is None:
                raise SchedulerError(f"synthesizer relied on missing memory {raw['id']}")
            current_record = _as_dict(record)
            expected_revision = raw.get("revision")
            if expected_revision is not None and int(expected_revision) != int(
                current_record.get("revision", 1)
            ):
                raise SchedulerError(
                    f"synthesizer relied on stale revision of {raw['id']}"
                )
        kind = operation["kind"]
        rejected_digest: str | None = None
        prior_rejection: Mapping[str, Any] | None = None
        will_verify = kind == "fact" and (
            resolution == "new"
            or (
                resolution == "duplicate"
                and bool(operation["payload"].get("root_resolution"))
            )
        )
        if will_verify:
            semantic, _predecessor_records, _prior_report = self._verification_material(
                operation
            )
            candidate_digest = stable_digest(semantic)
            prior_rejection = self._rejected_bundle_entry(self._state, candidate_digest)
            if prior_rejection is not None:
                rejected_digest = candidate_digest
        action: tuple[str, dict[str, Any] | None] | None = None
        rejected_bundle_repair_scheduled: bool | None = None
        with self._mutate() as state:
            current = state["operations"][operation_id]
            current["synthesizer_result"] = report
            current["synthesizer_result_digest"] = stable_digest(report)
            current.pop("synthesizer_reconfirmation", None)
            if resolution == "new":
                if kind == "fact":
                    if rejected_digest is not None:
                        rejected_bundle_repair_scheduled = (
                            self._return_identical_bundle_locked(
                                state, current, rejected_digest, prior_rejection
                            )
                        )
                    else:
                        current["state"] = OperationState.VERIFYING.value
                elif current.get("group_id"):
                    current["synthesizer_new_confirmed"] = True
                    current["state"] = OperationState.WAITING_PREDECESSORS.value
                else:
                    current["state"] = OperationState.RECEIVED.value
                    action = (kind, None)
            elif resolution == "duplicate":
                canonical_id = str(report.get("canonical_id") or report.get("existing_id") or "")
                if not canonical_id:
                    raise SchedulerError("duplicate result requires canonical_id")
                root_resolution = current["payload"].get("root_resolution")
                if kind == "fact" and root_resolution:
                    # Approved exception: a duplicate mathematical statement
                    # carrying a newly verified root resolution may become a
                    # new immutable fact after exact-version verification.
                    current["duplicate_of"] = canonical_id
                    current["duplicate_root_exception"] = True
                    if rejected_digest is not None:
                        rejected_bundle_repair_scheduled = (
                            self._return_identical_bundle_locked(
                                state, current, rejected_digest, prior_rejection
                            )
                        )
                    else:
                        current["state"] = OperationState.VERIFYING.value
                else:
                    current["canonical_id"] = canonical_id
                    current["state"] = OperationState.COMMITTED.value
                    state["proposal_mappings"][
                        str(current["payload"].get("proposal_id") or operation_id)
                    ] = canonical_id
            else:
                if kind not in {"route_add", "obligation_add"}:
                    raise SchedulerError("synthesizer update is only valid for route/obligation")
                patch = report.get("patch")
                if not isinstance(patch, Mapping):
                    raise SchedulerError("synthesizer update requires a machine-readable patch")
                update_kind = "route_update" if kind == "route_add" else "obligation_update"
                current["state"] = OperationState.RECEIVED.value
                action = (update_kind, copy.deepcopy(dict(patch)))
            append_event(
                state,
                "synthesizer_resolution_received",
                {"operation_id": operation_id, "resolution": resolution},
            )
        if action:
            self._commit_memory_operation(
                operation_id, operation_kind=action[0], payload=action[1]
            )
        elif resolution == "duplicate" and not (
            kind == "fact" and operation["payload"].get("root_resolution")
        ):
            self._record_duplicate_resolution(operation_id)
        if rejected_digest is not None:
            proposal_id = operation["payload"].get("proposal_id")
            if proposal_id:
                self.reject_temporary_predecessor(
                    str(proposal_id),
                    reason="identical rejected verification bundle was resubmitted",
                    predecessor_operation_id=operation_id,
                )
        if rejected_digest is not None and not rejected_bundle_repair_scheduled:
            self._on_operation_terminal(operation_id)
        group_id = self._state["operations"][operation_id].get("group_id")
        if group_id:
            self._try_commit_operation_group(str(group_id))
        return True

    def _record_duplicate_resolution(self, operation_id: str) -> None:
        operation = self._state["operations"][operation_id]
        report = operation.get("synthesizer_result") or {}
        canonical_id = operation.get("canonical_id")
        proposal_id = str(operation["payload"].get("proposal_id") or operation_id)
        if hasattr(self.store, "record_duplicate_resolution"):
            try:
                result = self.store.record_duplicate_resolution(
                    operation_id,
                    operation["kind"],
                    proposal_id,
                    canonical_id,
                    _operation_body(operation["payload"]),
                    actor="scheduler",
                )
                if _status_from_result(result) != "committed":
                    raise SchedulerError(
                        _error_from_result(result) or "duplicate resolution was rejected"
                    )
            except Exception as exc:
                with self._mutate() as state:
                    current = state["operations"][operation_id]
                    current["state"] = OperationState.NEEDS_ATTENTION.value
                    current["error"] = str(exc)
                    self._add_attention(
                        state,
                        f"operation:{operation_id}",
                        "duplicate_mapping_failed",
                        {"error": str(exc), "canonical_id": canonical_id},
                        scope="task",
                        owner_id=str(current["task_id"]),
                    )
                return
        with self._mutate() as state:
            current = state["operations"][operation_id]
            current["state"] = OperationState.COMMITTED.value
            current["canonical_id"] = canonical_id
            state["proposal_mappings"][proposal_id] = canonical_id
            if current.get("kind") == "fact":
                self._cancel_stale_fact_repair_intents_locked(
                    state,
                    candidate_id=str(current.get("candidate_id") or ""),
                    winner_operation_id=operation_id,
                )
            append_event(
                state,
                "duplicate_resolution_committed",
                {"operation_id": operation_id, "canonical_id": canonical_id},
            )
        if canonical_id and str(canonical_id).startswith("F-"):
            self.resolve_temporary_predecessor(proposal_id, str(canonical_id))
        self._on_operation_terminal(operation_id)
        self._recheck_reference_blocked_tasks()

    @staticmethod
    def _substitute_values(
        operation_kind: str,
        value: Mapping[str, Any],
        mapping: Mapping[str, str],
    ) -> dict[str, Any]:
        memory_type = {
            "route_add": "route",
            "memo": "memo",
            "claim_add": "claim",
            "obligation_add": "obligation",
        }.get(operation_kind)
        if memory_type is None:
            raise SchedulerError("legacy groups support only non-fact add operations")
        try:
            return substitute_nonfact_typed_ids(memory_type, value, mapping)
        except ReferenceValidationError as exc:
            raise SchedulerError(str(exc)) from exc

    def _try_commit_operation_group(self, group_id: str) -> None:
        group = self._state.get("operation_groups", {}).get(group_id)
        if not group or group["state"] in {"committed", "rejected", "needs_attention"}:
            return
        members = [self._state["operations"][item] for item in group["operation_ids"]]
        if any(
            item["kind"] in {"route_add", "obligation_add"}
            and item["state"] not in FINAL_OPERATION_STATES
            and not item.get("synthesizer_new_confirmed")
            for item in members
        ):
            return
        remaining = [item for item in members if item["state"] not in FINAL_OPERATION_STATES]
        if not remaining:
            with self._mutate() as state:
                state["operation_groups"][group_id]["state"] = "committed"
            return
        entries: list[dict[str, Any]] = []
        mapping = copy.deepcopy(self._state["proposal_mappings"])
        for item in remaining:
            proposal_id = str(item["payload"].get("proposal_id") or "")
            if not proposal_id:
                return
            entries.append(
                {
                    "operation_id": item["operation_id"],
                    "operation_type": item["kind"],
                    "proposal_id": proposal_id,
                    "payload": self._substitute_values(
                        item["kind"], _operation_body(item["payload"]), mapping
                    ),
                }
            )
        try:
            results = self.store.apply_operation_group(group_id, entries, actor="scheduler")
        except Exception as exc:
            with self._mutate() as state:
                current_group = state["operation_groups"][group_id]
                current_group["state"] = "needs_attention"
                current_group["error"] = str(exc)
                for operation_id in current_group["operation_ids"]:
                    operation = state["operations"][operation_id]
                    if operation["state"] not in FINAL_OPERATION_STATES:
                        operation["state"] = OperationState.NEEDS_ATTENTION.value
                task_ids = {
                    str(state["operations"][operation_id]["task_id"])
                    for operation_id in current_group["operation_ids"]
                }
                scope = "task" if len(task_ids) == 1 else "project"
                owner_id = next(iter(task_ids)) if len(task_ids) == 1 else None
                self._add_attention(
                    state,
                    f"operation-group:{group_id}",
                    "operation_group_commit_failed",
                    {"error": str(exc)},
                    scope=scope,
                    owner_id=owner_id,
                )
            return
        terminal_tasks: set[str] = set()
        with self._mutate() as state:
            current_group = state["operation_groups"][group_id]
            any_rejected = False
            for entry, result in zip(entries, results):
                operation = state["operations"][entry["operation_id"]]
                status = _status_from_result(result)
                if status == "committed":
                    operation["state"] = OperationState.COMMITTED.value
                    operation["canonical_id"] = _canonical_id_from_result(result)
                    state["proposal_mappings"][entry["proposal_id"]] = operation[
                        "canonical_id"
                    ]
                else:
                    any_rejected = True
                    operation["state"] = OperationState.REJECTED.value
                    operation["error"] = _error_from_result(result) or "group rejected"
                terminal_tasks.add(operation["task_id"])
            current_group["state"] = "rejected" if any_rejected else "committed"
            append_event(
                state,
                "operation_group_terminal",
                {"group_id": group_id, "state": current_group["state"]},
            )
        for task_id in terminal_tasks:
            self._maybe_close_task(task_id)
        # Group publication can resolve or reject temporary targets whose
        # already-published sources belong to another postprocessing task.
        self._recheck_reference_blocked_tasks()

    @staticmethod
    def _immutable_predecessor_core(
        predecessor_id: str, record: Mapping[str, Any]
    ) -> dict[str, Any]:
        """Extract exactly the immutable mathematical core of a fact."""

        value = copy.deepcopy(dict(record))
        predecessor_ids = value.get("predecessor_fact_ids", [])
        return {
            "id": str(predecessor_id),
            "statement": value.get("statement"),
            "proof": value.get("proof"),
            "predecessor_fact_ids": copy.deepcopy(list(predecessor_ids or [])),
            "originating_task_id": value.get("originating_task_id"),
            "foundation_policy_version": value.get("foundation_policy_version"),
            "introduced_notation": copy.deepcopy(value.get("introduced_notation", [])),
            "external_references": copy.deepcopy(value.get("external_references", [])),
            "root_resolution": copy.deepcopy(value.get("root_resolution")),
        }

    def _prior_verification_report(
        self, operation: Mapping[str, Any]
    ) -> dict[str, Any] | None:
        inherited = operation.get("prior_verification_report")
        if isinstance(inherited, Mapping):
            return copy.deepcopy(dict(inherited))
        lineage = self._state.get("fact_lineages", {}).get(operation.get("candidate_id"), {})
        current_version = int(operation.get("candidate_version", 0))
        versions = lineage.get("versions", {}) if isinstance(lineage, Mapping) else {}
        for raw_version in sorted(
            (int(item) for item in versions if int(item) < current_version), reverse=True
        ):
            prior_id = versions.get(str(raw_version))
            prior = self._state.get("operations", {}).get(prior_id, {})
            report = prior.get("verifier_report")
            if isinstance(report, Mapping):
                return copy.deepcopy(dict(report))
        return None

    def _verification_material(
        self, operation: Mapping[str, Any]
    ) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any] | None]:
        """Build semantic digest material and complete cited predecessor records."""

        payload = operation["payload"]
        predecessor_ids = [
            str(item)
            for item in payload.get("predecessor_fact_ids", [])
        ]
        if any(not item.startswith("F-") for item in predecessor_ids):
            raise SchedulerError("temporary predecessors must resolve before verification")
        predecessor_hashes: dict[str, str] = {}
        predecessor_records: list[dict[str, Any]] = []
        for predecessor_id in predecessor_ids:
            if not hasattr(self.store, "get"):
                raise SchedulerError("Store must provide complete verifier predecessor records")
            try:
                record = self.store.get(predecessor_id)
            except Exception as exc:
                raise SchedulerError(
                    f"cannot materialize predecessor {predecessor_id}: {exc}"
                ) from exc
            if record is None:
                raise SchedulerError(f"cannot materialize predecessor {predecessor_id}")
            record_dict = _as_dict(record)
            actual = str(record_dict.get("type", record_dict.get("memory_type", "fact")))
            if actual.startswith("MemoryType."):
                actual = actual.rsplit(".", 1)[-1].lower()
            status = record_dict.get("status", "active")
            if actual != "fact" or record_dict.get("active", True) is False or status in {
                "inactive",
                "revoked",
            }:
                raise SchedulerError(f"predecessor {predecessor_id} is not an active fact")
            core = self._immutable_predecessor_core(predecessor_id, record_dict)
            predecessor_hashes[predecessor_id] = stable_digest(core)
            predecessor_records.append(record_dict)
        semantic = {
            "statement": payload.get("statement"),
            "proof": payload.get("proof"),
            "predecessor_ids": predecessor_ids,
            "predecessor_core_hashes": predecessor_hashes,
            "introduced_notation": copy.deepcopy(payload.get("introduced_notation", [])),
            "external_references": copy.deepcopy(payload.get("external_references", [])),
            "foundation_policy": copy.deepcopy(self._state.get("foundation_policy", {})),
            "foundation_policy_version": self._state.get("foundation_policy_version", 1),
            "root_resolution": copy.deepcopy(payload.get("root_resolution")),
            "root_obligation_id": self._state["root"].get("obligation_id"),
            "root_obligation_hash": stable_digest(
                {
                    "id": self._state["root"].get("obligation_id"),
                    "problem": self._state["root"].get("problem"),
                }
            ),
        }
        return semantic, predecessor_records, self._prior_verification_report(operation)

    @staticmethod
    def _rejected_bundle_entry(
        state: Mapping[str, Any], digest: str
    ) -> Mapping[str, Any] | None:
        entry = state.get("rejected_verification_bundles", {}).get(digest)
        if isinstance(entry, Mapping):
            return entry
        for operation in state.get("operations", {}).values():
            if (
                operation.get("verification_bundle_digest") == digest
                and operation.get("error") == "verifier_incorrect"
            ):
                return {
                    "operation_id": operation.get("operation_id"),
                    "candidate_id": operation.get("candidate_id"),
                    "verification_report": copy.deepcopy(operation.get("verifier_report")),
                }
        return None

    @staticmethod
    def _latest_relevant_fact_operation_id(
        state: Mapping[str, Any], candidate_id: str
    ) -> str | None:
        """Return the newest non-abandoned operation in one fact lineage."""

        lineage = state.get("fact_lineages", {}).get(candidate_id)
        if not isinstance(lineage, Mapping):
            return None
        versions = lineage.get("versions", {})
        if not isinstance(versions, Mapping):
            return None
        for raw_version in sorted((int(item) for item in versions), reverse=True):
            operation_id = str(versions[str(raw_version)])
            operation = state.get("operations", {}).get(operation_id)
            if isinstance(operation, Mapping) and operation.get("state") != (
                OperationState.ABANDONED.value
            ):
                return operation_id
        return None

    @staticmethod
    def _latest_relevant_fact_repair_operation_id(
        state: Mapping[str, Any], repair_chain_id: str
    ) -> str | None:
        """Return the newest non-abandoned Fact submitted in one repair chain."""

        if not repair_chain_id:
            return None
        for operation_id, operation in reversed(
            tuple(state.get("operations", {}).items())
        ):
            if operation.get("kind") != "fact" or operation.get("state") == (
                OperationState.ABANDONED.value
            ):
                continue
            proposal_id = Scheduler._fact_proposal_id_of(operation)
            operation_chain_id = str(
                operation.get("repair_chain_id") or proposal_id or ""
            )
            if operation_chain_id == repair_chain_id:
                return str(operation_id)
        return None

    @staticmethod
    def _fact_repair_intent(
        supplement: Any,
    ) -> tuple[str, str, str, str | None] | None:
        """Return ``(kind, candidate, repair chain, cause)`` for one repair intent."""

        semantic = preserved_attempt_supplement(supplement)
        if not isinstance(semantic, Mapping):
            return None
        kind = str(semantic.get("kind") or "")
        if kind not in {
            "verifier_revision",
            "fact_concession",
            "identical_rejected_bundle",
        }:
            return None
        candidate_id = str(semantic.get("candidate_id") or "")
        if not candidate_id:
            return None
        repair_chain_id = str(
            semantic.get("repair_chain_id")
            or semantic.get("proposal_id")
            or candidate_id
        )
        operation_id = str(semantic.get("operation_id") or "") or None
        report = semantic.get("verification_report")
        if operation_id is None and isinstance(report, Mapping):
            operation_id = str(report.get("operation_id") or "") or None
        return kind, candidate_id, repair_chain_id, operation_id

    def _cancel_stale_fact_repair_intents_locked(
        self,
        state: MutableMapping[str, Any],
        *,
        candidate_id: str | None = None,
        winner_operation_id: str | None = None,
    ) -> tuple[str, ...]:
        """Cancel queued repair work superseded by a newer or closed lineage."""

        committed_winners_by_chain: dict[str, str] = {}
        for operation_id, operation in state.get("operations", {}).items():
            if operation.get("kind") != "fact" or operation.get("state") != (
                OperationState.COMMITTED.value
            ):
                continue
            proposal_id = self._fact_proposal_id_of(operation)
            repair_chain_id = str(
                operation.get("repair_chain_id") or proposal_id or ""
            )
            if repair_chain_id:
                committed_winners_by_chain[repair_chain_id] = str(operation_id)

        cancelled: list[str] = []
        for task_id, task in state.get("tasks", {}).items():
            semantic_supplement = preserved_attempt_supplement(
                task.get("pending_attempt_supplement")
            )
            intent = self._fact_repair_intent(task.get("pending_attempt_supplement"))
            if intent is None:
                continue
            (
                kind,
                intent_candidate_id,
                intent_repair_chain_id,
                cause_operation_id,
            ) = intent
            lineage = state.get("fact_lineages", {}).get(intent_candidate_id)
            if (
                isinstance(lineage, Mapping)
                and isinstance(semantic_supplement, Mapping)
                and not semantic_supplement.get("repair_chain_id")
                and not semantic_supplement.get("proposal_id")
            ):
                intent_repair_chain_id = str(
                    lineage.get("repair_chain_id") or intent_repair_chain_id
                )
            chain_winner_operation_id = committed_winners_by_chain.get(
                intent_repair_chain_id
            )
            same_chain_winner = chain_winner_operation_id is not None
            latest_operation_id = self._latest_relevant_fact_repair_operation_id(
                state, intent_repair_chain_id
            )
            superseded = bool(
                cause_operation_id
                and latest_operation_id
                and cause_operation_id != latest_operation_id
            )
            if not same_chain_winner:
                # Rejecting one proposal closes only that proposal's lineage.
                # The requested correction is a fresh proposal/candidate and
                # must remain queued until a winner in the same repair chain
                # has actually committed.
                if (
                    isinstance(semantic_supplement, Mapping)
                    and semantic_supplement.get("fresh_proposal_required")
                    and not superseded
                ):
                    continue
                if (
                    not superseded
                    and candidate_id is not None
                    and intent_candidate_id != candidate_id
                ):
                    continue
            if not same_chain_winner and not isinstance(lineage, Mapping):
                continue
            lineage_closed = bool(
                isinstance(lineage, Mapping) and lineage.get("closed")
            )
            if not same_chain_winner and not lineage_closed and not superseded:
                continue
            task.pop("pending_attempt_supplement", None)
            current = TaskState(task["state"])
            if current in {TaskState.REVISION_PENDING, TaskState.RETRY_PENDING}:
                task["slot_reserved"] = False
                self._transition_task(state, task, TaskState.POSTPROCESSING)
            append_event(
                state,
                "stale_fact_repair_intent_cancelled",
                {
                    "task_id": task_id,
                    "kind": kind,
                    "candidate_id": intent_candidate_id,
                    "repair_chain_id": intent_repair_chain_id,
                    "cause_operation_id": cause_operation_id,
                    "latest_relevant_operation_id": latest_operation_id,
                    "winner_operation_id": (
                        chain_winner_operation_id or winner_operation_id
                    ),
                    "same_repair_chain_winner": same_chain_winner,
                    "lineage_closed": lineage_closed,
                },
            )
            cancelled.append(str(task_id))
        return tuple(sorted(cancelled))

    def _return_identical_bundle_locked(
        self,
        state: MutableMapping[str, Any],
        operation: MutableMapping[str, Any],
        digest: str,
        prior_rejection: Mapping[str, Any] | None,
    ) -> bool:
        """Durably return an unchanged rejected proof without another verifier call."""

        operation["state"] = OperationState.REJECTED.value
        operation["verification_bundle_digest"] = digest
        operation["error"] = "identical_rejected_verification_bundle"
        candidate_id = str(operation.get("candidate_id") or "")
        lineage = state.get("fact_lineages", {}).get(candidate_id, {})
        repair_chain_id = str(
            operation.get("repair_chain_id")
            or self._fact_proposal_id_of(operation)
            or ""
        )
        latest_operation_id = self._latest_relevant_fact_repair_operation_id(
            state, repair_chain_id
        )
        repair_scheduled = bool(
            not lineage.get("closed")
            and latest_operation_id == operation.get("operation_id")
        )
        operation["repair_scheduled"] = repair_scheduled
        if not repair_scheduled:
            operation["superseded_by_operation_id"] = latest_operation_id
            append_event(
                state,
                "identical_rejected_bundle_returned",
                {
                    "operation_id": operation.get("operation_id"),
                    "candidate_id": candidate_id,
                    "bundle_digest": digest,
                    "repair_scheduled": False,
                    "latest_relevant_operation_id": latest_operation_id,
                    "lineage_closed": bool(lineage.get("closed")),
                },
            )
            return False
        task = state["tasks"][operation["task_id"]]
        current = TaskState(task["state"])
        if current is TaskState.RUNNING:
            task["slot_reserved"] = False
            if task.get("attempts") and task["attempts"][-1].get("state") == "running":
                attempt = task["attempts"][-1]
                attempt["state"] = "ended_for_identical_bundle_correction"
                attempt["ended_at"] = utc_now()
                worker_call = state["calls"].get(attempt.get("call_id"))
                if worker_call:
                    _apply_call_event(
                        state,
                        worker_call,
                        call_machine.Fence(CallState.SUPERSEDED),
                    )
            self._transition_task(state, task, TaskState.ATTEMPT_ENDED)
            current = TaskState.ATTEMPT_ENDED
        if current in {
            TaskState.ATTEMPT_ENDED,
            TaskState.POSTPROCESSING,
            TaskState.RETRY_PENDING,
            TaskState.NEEDS_ATTENTION,
        }:
            self._transition_task(state, task, TaskState.REVISION_PENDING)
        elif current is not TaskState.REVISION_PENDING:
            raise WorkflowError(
                f"task {task['task_id']} cannot receive an identical-bundle correction "
                f"from {current.value}"
            )
        requests = int(operation.get("repair_request", 0))
        proposal_id = self._fact_proposal_id_of(operation)
        prior_report = (
            prior_rejection.get("verification_report")
            if isinstance(prior_rejection, Mapping)
            else None
        )
        if requests < 2:
            lineage["revision_requests"] = requests + 1
            task["pending_attempt_supplement"] = {
                "kind": "identical_rejected_bundle",
                "candidate_id": operation.get("candidate_id"),
                "proposal_id": proposal_id,
                "operation_id": operation.get("operation_id"),
                "repair_chain_id": operation.get("repair_chain_id") or proposal_id,
                "revision_request": requests + 1,
                "bundle_digest": digest,
                "verification_report": copy.deepcopy(prior_report),
                "reason": "the mathematical verification bundle is unchanged from a rejection",
                "prior_rejection": copy.deepcopy(dict(prior_rejection or {})),
                "fresh_proposal_required": True,
                "fresh_candidate_required": True,
                "candidate_version": 1,
            }
        else:
            lineage["concession_required"] = True
            task["pending_attempt_supplement"] = {
                "kind": "fact_concession",
                "candidate_id": operation.get("candidate_id"),
                "proposal_id": proposal_id,
                "operation_id": operation.get("operation_id"),
                "repair_chain_id": operation.get("repair_chain_id") or proposal_id,
                "verification_report": copy.deepcopy(prior_report),
                "forbid_new_candidate": True,
                "require_failure_memo": True,
            }
        append_event(
            state,
            "identical_rejected_bundle_returned",
            {
                "operation_id": operation.get("operation_id"),
                "candidate_id": operation.get("candidate_id"),
                "bundle_digest": digest,
                "repair_scheduled": True,
            },
        )
        return True

    @staticmethod
    def _source_attempt_is_terminal_locked(
        state: Mapping[str, Any], operation: Mapping[str, Any] | None
    ) -> bool:
        if not isinstance(operation, Mapping):
            return False
        task = state.get("tasks", {}).get(operation.get("task_id"))
        if not isinstance(task, Mapping):
            return False
        try:
            source_attempt = int(operation.get("attempt"))
        except (TypeError, ValueError):
            return False
        matches: list[Mapping[str, Any]] = []
        for attempt in task.get("attempts", []):
            try:
                attempt_number = int(attempt.get("attempt"))
            except (AttributeError, TypeError, ValueError):
                continue
            if attempt_number == source_attempt:
                matches.append(attempt)
        return bool(
            len(matches) == 1
            and matches[0].get("state") in _TERMINAL_WORKER_ATTEMPT_STATES
        )

    def source_attempt_terminal(self, operation_id: str) -> bool:
        """Return whether an operation's exact source worker attempt has ended."""

        with self._lock:
            operation = self._state.get("operations", {}).get(operation_id)
            return bool(
                isinstance(operation, Mapping)
                and self._source_attempt_is_terminal_locked(self._state, operation)
            )

    def verifier_ready(self, operation_id: str) -> bool:
        """Return whether a pending Fact may start verification."""

        with self._lock:
            operation = self._state.get("operations", {}).get(operation_id)
            return bool(
                isinstance(operation, Mapping)
                and operation.get("kind") == "fact"
                and operation.get("state") == OperationState.VERIFYING.value
                and self._source_attempt_is_terminal_locked(self._state, operation)
            )

    def verification_bundle(self, operation_id: str) -> dict[str, Any]:
        operation = self._state["operations"].get(operation_id)
        if not operation:
            raise SchedulerError(f"unknown operation {operation_id}")
        if operation["state"] != OperationState.VERIFYING.value:
            if operation.get("error") == "identical_rejected_verification_bundle":
                raise SchedulerError(
                    "identical rejected verification bundle will not be rechecked"
                )
            raise WorkflowError(f"operation {operation_id} is not awaiting verification")
        if not self.verifier_ready(operation_id):
            raise WorkflowError(
                f"operation {operation_id} cannot be verified before its worker attempt ends"
            )
        semantic, predecessor_records, prior_report = self._verification_material(operation)
        digest = stable_digest(semantic)
        rejected = self._rejected_bundle_entry(self._state, digest)
        if rejected is not None:
            repair_scheduled = False
            with self._mutate() as state:
                current = state["operations"][operation_id]
                repair_scheduled = self._return_identical_bundle_locked(
                    state, current, digest, rejected
                )
            proposal_id = operation.get("payload", {}).get("proposal_id")
            if proposal_id:
                self.reject_temporary_predecessor(
                    str(proposal_id),
                    reason="identical rejected verification bundle was resubmitted",
                    predecessor_operation_id=operation_id,
                )
            if not repair_scheduled:
                self._on_operation_terminal(operation_id)
            raise SchedulerError("identical rejected verification bundle will not be rechecked")
        with self._mutate() as state:
            current = state["operations"][operation_id]
            verifier_attempt_id = current.get("verifier_attempt_id")
            if not verifier_attempt_id:
                verifier_attempt_id = self._allocate_id(state, "VERIFY")
                current["verifier_attempt_id"] = verifier_attempt_id
            envelope = {
                "candidate_id": current["candidate_id"],
                "candidate_version": current["candidate_version"],
                "operation_id": operation_id,
                "verifier_attempt_id": verifier_attempt_id,
                **copy.deepcopy(semantic),
                "bundle_digest": digest,
                "predecessor_records": copy.deepcopy(predecessor_records),
                "prior_verification_report": copy.deepcopy(prior_report),
            }
            envelope.update(
                self._advisor_problem_binding_locked(
                    state, task_id=str(current.get("task_id") or "")
                )
            )
            current["verification_bundle"] = copy.deepcopy(semantic)
            current["verification_envelope"] = copy.deepcopy(envelope)
            current["verification_bundle_digest"] = digest
            append_event(
                state,
                "verification_bundle_materialized",
                {
                    "operation_id": operation_id,
                    "digest": digest,
                    "verifier_attempt_id": verifier_attempt_id,
                    "predecessor_ids": list(semantic["predecessor_ids"]),
                },
            )
        return copy.deepcopy(envelope)

    def prepare_verifier_call(self, operation_id: str) -> str:
        operation = self._state["operations"].get(operation_id)
        if (
            operation
            and operation.get("state") == OperationState.VERIFYING.value
            and not self.verifier_ready(operation_id)
        ):
            raise WorkflowError(
                f"operation {operation_id} cannot be verified before its worker attempt ends"
            )
        if operation and operation.get("verifier_call_id"):
            return str(operation["verifier_call_id"])
        bundle = self.verification_bundle(operation_id)
        return self._prepare_owned_call(
            "verifier",
            bundle,
            owner_collection="operations",
            owner_id=operation_id,
            pointer_field="verifier_call_id",
            continuation={"operation_id": operation_id},
        )

    def commit_verifier_call(self, call_id: str) -> str | None:
        call = self._state["calls"].get(call_id)
        if not call or call["kind"] != "verifier":
            raise SchedulerError("unknown verifier call")
        if call["status"] == CallState.COMMITTED.value:
            operation_id = str(call["continuation"]["operation_id"])
            return self._state["operations"][operation_id].get("canonical_id")
        if call["status"] != CallState.COMPLETED.value:
            raise WorkflowError("verifier call has no completed result")
        operation_id = str(call["continuation"]["operation_id"])
        if not self.source_attempt_terminal(operation_id):
            raise WorkflowError(
                f"operation {operation_id} cannot be verified before its worker attempt ends"
            )
        canonical_id = self.apply_verifier_report(operation_id, call["result"])
        self.mark_call_committed(call_id)
        return canonical_id

    def apply_verifier_report(
        self, operation_id: str, report: Mapping[str, Any]
    ) -> str | None:
        result = copy.deepcopy(dict(report))
        operation = self._state["operations"].get(operation_id)
        if not operation:
            raise SchedulerError(f"unknown operation {operation_id}")
        if operation["state"] != OperationState.VERIFYING.value:
            if operation.get("verifier_report_digest") == stable_digest(result):
                return operation.get("canonical_id")
            raise WorkflowError("operation is not awaiting verifier report")
        if not self.verifier_ready(operation_id):
            raise WorkflowError(
                f"operation {operation_id} cannot be verified before its worker attempt ends"
            )
        bundle_digest = operation.get("verification_bundle_digest")
        if not bundle_digest:
            raise SchedulerError("verification bundle has not been materialized")
        verdict = str(result.get("verdict") or "")
        if verdict not in {"correct", "incorrect"}:
            raise SchedulerError("verifier verdict must be correct or incorrect")
        envelope = operation.get("verification_envelope")
        if not isinstance(envelope, Mapping):
            raise SchedulerError("verification envelope has not been materialized")
        identity_fields = {
            "candidate_id",
            "candidate_version",
            "operation_id",
            "bundle_digest",
            "verifier_attempt_id",
        }
        confirmation_fields = {
            "predecessor_ids",
            "introduced_notation",
            "external_references",
            "root_resolution",
        }
        missing = (identity_fields | confirmation_fields | {"errors"}) - set(result)
        if missing:
            raise SchedulerError(
                "verifier report is missing exact-envelope fields: "
                + ", ".join(sorted(missing))
            )
        expected_values = {
            "candidate_id": operation["candidate_id"],
            "candidate_version": operation["candidate_version"],
            "operation_id": operation_id,
            "bundle_digest": bundle_digest,
            "verifier_attempt_id": envelope.get("verifier_attempt_id"),
            "predecessor_ids": envelope.get("predecessor_ids", []),
            "introduced_notation": envelope.get("introduced_notation", []),
            "external_references": envelope.get("external_references", []),
            "root_resolution": envelope.get("root_resolution"),
        }
        mismatched = [
            field
            for field, expected in expected_values.items()
            if result.get(field) != expected
        ]
        if mismatched:
            raise SchedulerError(
                "verifier report does not match the exact envelope: "
                + ", ".join(sorted(mismatched))
            )
        errors = result.get("errors")
        if not isinstance(errors, list):
            raise SchedulerError("verifier errors must be a list")
        if verdict == "correct" and errors:
            raise SchedulerError("a correct verifier report must have no errors")
        if verdict == "incorrect" and not errors:
            raise SchedulerError("an incorrect verifier report requires detailed errors")
        if verdict == "incorrect":
            self._handle_incorrect_fact(operation_id, result)
            return None
        # A semantic correct verdict is persisted before the idempotent store
        # operation, so full-stop recovery can safely repeat publication.
        with self._mutate() as state:
            current = state["operations"][operation_id]
            current["verifier_report"] = result
            current["verifier_report_digest"] = stable_digest(result)
            current["verified_correct"] = True
            append_event(state, "fact_verified_correct", {"operation_id": operation_id})
        canonical_id = self._publish_verified_fact(operation_id)
        return canonical_id

    def _publish_verified_fact(self, operation_id: str) -> str | None:
        with self._lock:
            operation = self._state["operations"][operation_id]
            if not self._source_attempt_is_terminal_locked(self._state, operation):
                raise WorkflowError(
                    f"operation {operation_id} cannot be verified before its worker attempt ends"
                )
            operation = copy.deepcopy(operation)
        payload = _operation_body(operation["payload"])
        payload.setdefault("originating_task_id", operation["task_id"])
        payload["foundation_policy_version"] = self._state.get("foundation_policy_version", 1)
        try:
            result = self.store.apply_operation(
                operation_id,
                "fact",
                payload,
                proposal_id=operation["payload"].get("proposal_id"),
                actor="scheduler",
            )
            status = _status_from_result(result)
            canonical_id = _canonical_id_from_result(result)
            if status not in {"committed", "rejected"}:
                raise SchedulerError(
                    _error_from_result(result)
                    or f"fact publication returned unknown status {status!r}"
                )
            if status == "committed" and not canonical_id:
                raise SchedulerError("fact publication returned no canonical ID")
        except Exception as exc:
            with self._mutate() as state:
                current = state["operations"][operation_id]
                current["state"] = OperationState.NEEDS_ATTENTION.value
                current["error"] = str(exc)
                self._add_attention(
                    state,
                    f"operation:{operation_id}",
                    "verified_fact_publication_failed",
                    {"error": str(exc)},
                    scope="task",
                    owner_id=str(current["task_id"]),
                )
            return None
        if status == "rejected":
            reason = _error_from_result(result) or "fact publication rejected"
            proposal_id = str(operation["payload"].get("proposal_id") or operation_id)
            with self._mutate() as state:
                current = state["operations"][operation_id]
                # Accept the exact completed verifier result before the
                # terminal proposal cascade fences any still-live Fact calls.
                verifier_call = state.get("calls", {}).get(
                    current.get("verifier_call_id")
                )
                if (
                    isinstance(verifier_call, MutableMapping)
                    and verifier_call.get("status") == CallState.COMPLETED.value
                    and verifier_call.get("result_digest")
                    == current.get("verifier_report_digest")
                ):
                    _apply_call_event(
                        state,
                        verifier_call,
                        call_machine.CommitResult(),
                    )
                if self._operation_may_terminalize_fact_proposal(state, current):
                    self._reject_fact_proposal_cascade_locked(
                        state,
                        proposal_id,
                        reason=reason,
                        predecessor_operation_id=operation_id,
                    )
                else:
                    current["state"] = OperationState.REJECTED.value
                    current["error"] = reason
                self._resolve_attention(state, f"operation:{operation_id}")
                append_event(
                    state,
                    "verified_fact_publication_rejected",
                    {"operation_id": operation_id, "error": reason},
                )
            self._reconcile_fact_proposal_rejection_tails()
            self._on_operation_terminal(operation_id)
            return None
        with self._mutate() as state:
            current = state["operations"][operation_id]
            current["state"] = OperationState.COMMITTED.value
            current["canonical_id"] = canonical_id
            current["verifier_report_committed"] = True
            current["error"] = None
            self._resolve_attention(state, f"operation:{operation_id}")
            candidate_id = current["candidate_id"]
            state["fact_lineages"][candidate_id]["closed"] = True
            self._cancel_stale_fact_repair_intents_locked(
                state,
                candidate_id=candidate_id,
                winner_operation_id=operation_id,
            )
            state["proposal_mappings"][
                str(current["payload"].get("proposal_id") or operation_id)
            ] = canonical_id
            proposal_id = str(current["payload"].get("proposal_id") or operation_id)
            proposal = state.setdefault("fact_proposals", {}).get(proposal_id)
            if proposal is not None:
                proposal["state"] = "resolved"
                proposal["canonical_id"] = canonical_id
                proposal["current_operation_id"] = operation_id
            append_event(
                state,
                "verified_fact_published",
                {"operation_id": operation_id, "fact_id": canonical_id},
            )
            root_resolution = current["payload"].get("root_resolution")
            if root_resolution:
                outcome = (
                    root_resolution.get("outcome")
                    if isinstance(root_resolution, Mapping)
                    else str(root_resolution)
                )
                self._record_root_resolution_in_state(
                    state, canonical_id, str(outcome), operation_id=operation_id
                )
        proposal_id = str(operation["payload"].get("proposal_id") or operation_id)
        self.resolve_temporary_predecessor(proposal_id, canonical_id)
        self._on_operation_terminal(operation_id)
        self._maybe_complete_project()
        return canonical_id

    def _handle_incorrect_fact(
        self, operation_id: str, report: Mapping[str, Any]
    ) -> None:
        task_id: str
        proposal_id: str | None = None
        repair_scheduled = False
        with self._mutate() as state:
            operation = state["operations"][operation_id]
            proposal_id = str(operation.get("payload", {}).get("proposal_id") or "") or None
            digest = operation["verification_bundle_digest"]
            lineage = state["fact_lineages"][operation["candidate_id"]]
            repair_chain_id = str(
                operation.get("repair_chain_id") or proposal_id or ""
            )
            latest_operation_id = self._latest_relevant_fact_repair_operation_id(
                state, repair_chain_id
            )
            repair_scheduled = bool(
                not lineage.get("closed") and latest_operation_id == operation_id
            )
            if repair_scheduled and digest in lineage["rejected_bundle_digests"]:
                raise SchedulerError("identical rejected verification bundle will not be rechecked")
            if repair_scheduled:
                lineage["rejected_bundle_digests"].append(digest)
            rejection_audit = {
                "bundle_digest": digest,
                "candidate_id": operation["candidate_id"],
                "candidate_version": operation["candidate_version"],
                "operation_id": operation_id,
                "task_id": operation["task_id"],
                "verification_report": copy.deepcopy(dict(report)),
                "repair_scheduled": repair_scheduled,
                "latest_relevant_operation_id": latest_operation_id,
            }
            rejected_bundles = state.setdefault("rejected_verification_bundles", {})
            existing_audit = rejected_bundles.get(digest)
            if existing_audit is None:
                rejected_bundles[digest] = rejection_audit
            else:
                existing_audit.setdefault("additional_verifier_results", []).append(
                    rejection_audit
                )
            operation["state"] = OperationState.REJECTED.value
            operation["verifier_report"] = copy.deepcopy(dict(report))
            operation["verifier_report_digest"] = stable_digest(report)
            operation["error"] = "verifier_incorrect"
            operation["repair_scheduled"] = repair_scheduled
            # Persist the exact completed verifier call in the same control
            # transaction as its accepted verdict.  The terminal proposal
            # cascade below deliberately fences every still-live call owned by
            # rejected Facts; without this commit, it would supersede the very
            # call whose result has just been applied and the runtime could not
            # finish its idempotent commit step.
            verifier_call = state.get("calls", {}).get(
                operation.get("verifier_call_id")
            )
            if (
                isinstance(verifier_call, MutableMapping)
                and verifier_call.get("status") == CallState.COMPLETED.value
                and verifier_call.get("result_digest") == stable_digest(report)
            ):
                _apply_call_event(
                    state,
                    verifier_call,
                    call_machine.CommitResult(),
                )
            if not repair_scheduled:
                operation["superseded_by_operation_id"] = latest_operation_id
            task_id = operation["task_id"]
            task = state["tasks"][task_id]
            if repair_scheduled:
                if task["state"] == TaskState.RUNNING.value:
                    task["slot_reserved"] = False
                    task["attempts"][-1]["state"] = "ended_for_verifier_revision"
                    worker_call = state["calls"].get(task["attempts"][-1].get("call_id"))
                    if worker_call:
                        _apply_call_event(
                            state,
                            worker_call,
                            call_machine.Fence(CallState.SUPERSEDED),
                        )
                    self._transition_task(state, task, TaskState.ATTEMPT_ENDED)
                elif task["state"] == TaskState.POSTPROCESSING.value:
                    pass
                elif task["state"] == TaskState.CLOSED.value:
                    raise SchedulerError("closed task cannot enter fact revision")
                requests = int(operation.get("repair_request", 0))
                if requests < 2:
                    lineage["revision_requests"] = requests + 1
                    self._transition_task(state, task, TaskState.REVISION_PENDING)
                    task["pending_attempt_supplement"] = {
                        "kind": "verifier_revision",
                        "candidate_id": operation["candidate_id"],
                        "proposal_id": proposal_id,
                        "operation_id": operation_id,
                        "repair_chain_id": operation.get("repair_chain_id")
                        or proposal_id,
                        "revision_request": requests + 1,
                        "verification_report": copy.deepcopy(dict(report)),
                        "fresh_proposal_required": True,
                        "fresh_candidate_required": True,
                        "candidate_version": 1,
                    }
                else:
                    lineage["concession_required"] = True
                    self._transition_task(state, task, TaskState.REVISION_PENDING)
                    task["pending_attempt_supplement"] = {
                        "kind": "fact_concession",
                        "candidate_id": operation["candidate_id"],
                        "proposal_id": proposal_id,
                        "operation_id": operation_id,
                        "repair_chain_id": operation.get("repair_chain_id")
                        or proposal_id,
                        "verification_report": copy.deepcopy(dict(report)),
                        "forbid_new_candidate": True,
                        "require_failure_memo": True,
                    }
            append_event(
                state,
                "fact_verified_incorrect",
                {
                    "operation_id": operation_id,
                    "candidate_id": operation["candidate_id"],
                    "revision_requests": int(operation.get("repair_request", 0)),
                    "concession_required": lineage["concession_required"],
                    "repair_scheduled": repair_scheduled,
                    "latest_relevant_operation_id": latest_operation_id,
                },
            )
        if proposal_id:
            self.reject_temporary_predecessor(
                proposal_id,
                reason="verifier rejected the predecessor proof",
                predecessor_operation_id=operation_id,
            )
        if not repair_scheduled:
            self._on_operation_terminal(operation_id)

    def challenge_bundle(self, challenge_id: str) -> dict[str, Any]:
        """Materialize the exact immutable fact core for one challenge review."""

        challenge = self._state["challenges"].get(challenge_id)
        if not challenge:
            raise SchedulerError(f"unknown challenge {challenge_id}")
        if challenge["state"] != OperationState.VERIFYING.value:
            raise WorkflowError(f"challenge {challenge_id} is not awaiting verification")
        fact_id = str(challenge["payload"].get("fact_id") or "")
        if not fact_id or not hasattr(self.store, "get"):
            raise SchedulerError("challenge requires a canonical fact")
        try:
            fact = _as_dict(self.store.get(fact_id))
        except Exception as exc:
            raise SchedulerError(f"cannot materialize challenged fact {fact_id}") from exc
        if str(fact.get("type", fact.get("memory_type", "fact"))) not in {
            "fact",
            "MemoryType.FACT",
        }:
            raise SchedulerError(f"challenge target {fact_id} is not a fact")
        core_fields = (
            "id",
            "statement",
            "proof",
            "predecessor_fact_ids",
            "originating_task_id",
            "foundation_policy_version",
            "introduced_notation",
            "external_references",
            "root_resolution",
        )
        fact_core = {key: copy.deepcopy(fact.get(key)) for key in core_fields}
        value = {
            "challenge_id": challenge_id,
            "challenge_digest": challenge["input_digest"],
            "challenge": copy.deepcopy(challenge["payload"]),
            "fact_core": fact_core,
            "fact_core_digest": stable_digest(fact_core),
            "foundation_policy": copy.deepcopy(self._state.get("foundation_policy", {})),
        }
        value.update(
            self._advisor_problem_binding_locked(
                self._state, task_id=str(challenge.get("task_id") or "")
            )
        )
        digest = stable_digest(value)
        with self._mutate() as state:
            current = state["challenges"][challenge_id]
            current["verification_bundle"] = copy.deepcopy(value)
            current["verification_bundle_digest"] = digest
            append_event(
                state,
                "fact_challenge_bundle_materialized",
                {"challenge_id": challenge_id, "bundle_digest": digest},
            )
        return dict(value, bundle_digest=digest)

    def prepare_challenge_verifier_call(self, challenge_id: str) -> str:
        challenge = self._state["challenges"].get(challenge_id)
        if challenge and challenge.get("verifier_call_id"):
            return str(challenge["verifier_call_id"])
        return self._prepare_owned_call(
            "challenge-verifier",
            self.challenge_bundle(challenge_id),
            owner_collection="challenges",
            owner_id=challenge_id,
            pointer_field="verifier_call_id",
            continuation={"challenge_id": challenge_id},
        )

    def commit_challenge_verifier_call(self, call_id: str) -> None:
        call = self._state["calls"].get(call_id)
        if not call or call["kind"] != "challenge-verifier":
            raise SchedulerError("unknown fact-challenge verifier call")
        if call["status"] == CallState.COMMITTED.value:
            challenge_id = str(call.get("continuation", {}).get("challenge_id") or "")
            challenge = self._state["challenges"].get(challenge_id)
            if challenge and challenge.get("resolution") is not None:
                self._apply_fact_challenge_resolution_tail(challenge_id)
            return
        if call["status"] != CallState.COMPLETED.value:
            raise WorkflowError("fact-challenge verifier call has no completed result")
        challenge_id = str(call["continuation"]["challenge_id"])
        challenge = self._state["challenges"][challenge_id]
        report = copy.deepcopy(dict(call["result"]))
        if report.get("challenge_id") != challenge_id:
            raise SchedulerError("fact-challenge report has the wrong challenge ID")
        if report.get("bundle_digest") != challenge.get("verification_bundle_digest"):
            raise SchedulerError("fact-challenge report does not match the exact bundle")
        resolution = str(report.get("resolution") or "")
        self.resolve_fact_challenge(challenge_id, resolution, report)
        self.mark_call_committed(call_id)

    def resolve_fact_challenge(
        self,
        challenge_id: str,
        resolution: str,
        report: Mapping[str, Any],
    ) -> None:
        if resolution not in {"confirmed_invalid", "challenge_rejected", "inconclusive"}:
            raise SchedulerError("invalid fact-challenge resolution")
        exact_report = copy.deepcopy(dict(report))
        resolution_digest = stable_digest(
            {"resolution": resolution, "report": exact_report}
        )
        with self._mutate() as state:
            challenge = state["challenges"].get(challenge_id)
            if not challenge:
                raise SchedulerError(f"unknown challenge {challenge_id}")
            if challenge.get("resolution") is not None:
                prior_digest = challenge.get("resolution_digest") or stable_digest(
                    {
                        "resolution": challenge.get("resolution"),
                        "report": challenge.get("report", {}),
                    }
                )
                if prior_digest != resolution_digest:
                    raise IdempotencyConflict(
                        "challenge received a conflicting resolution result"
                    )
                challenge["resolution_digest"] = prior_digest
                challenge.setdefault(
                    "resolution_tail",
                    self._challenge_resolution_tail_record(
                        challenge_id, str(challenge.get("resolution"))
                    ),
                )
            else:
                fact_id = challenge["payload"].get("fact_id")
                challenge["resolution"] = resolution
                challenge["report"] = exact_report
                challenge["resolution_digest"] = resolution_digest
                challenge["resolution_tail"] = self._challenge_resolution_tail_record(
                    challenge_id, resolution
                )
                if resolution == "inconclusive":
                    challenge["state"] = OperationState.NEEDS_ATTENTION.value
                    self._add_attention(
                        state,
                        f"challenge:{challenge_id}",
                        "fact_challenge_inconclusive",
                        {"fact_id": fact_id},
                    )
                else:
                    challenge["state"] = OperationState.COMMITTED.value
                append_event(
                    state,
                    "fact_challenge_resolved",
                    {"challenge_id": challenge_id, "resolution": resolution},
                )
        self._apply_fact_challenge_resolution_tail(challenge_id)

    @staticmethod
    def _challenge_resolution_tail_record(
        challenge_id: str, resolution: str
    ) -> dict[str, Any]:
        return {
            "status": "pending",
            "kind": (
                "fact_revocation"
                if resolution == "confirmed_invalid"
                else "challenge_closure"
            ),
            "operation_id": f"challenge-revocation:{challenge_id}",
        }

    def _apply_fact_challenge_resolution_tail(self, challenge_id: str) -> None:
        """Finish the deterministic tail of one persisted challenge verdict."""

        challenge = self._state["challenges"].get(challenge_id)
        if not challenge or challenge.get("resolution") is None:
            raise SchedulerError(f"challenge {challenge_id} has no resolution")
        tail = challenge.get("resolution_tail") or self._challenge_resolution_tail_record(
            challenge_id, str(challenge["resolution"])
        )
        if tail.get("status") == "applied":
            return
        resolution = str(challenge["resolution"])
        fact_id = str(challenge.get("payload", {}).get("fact_id") or "")
        if resolution == "confirmed_invalid" and fact_id:
            self.revoke_fact(
                fact_id,
                reason="confirmed fact challenge",
                evidence=challenge.get("report", {}),
                operation_id=str(tail["operation_id"]),
            )
        task_id = challenge.get("task_id")
        if task_id:
            self._maybe_close_task(str(task_id))
        with self._mutate() as state:
            current = state["challenges"].get(challenge_id)
            if not current or current.get("resolution") != resolution:
                raise IdempotencyConflict(
                    "challenge changed while applying its resolution tail"
                )
            current_tail = current.setdefault(
                "resolution_tail",
                self._challenge_resolution_tail_record(challenge_id, resolution),
            )
            if current_tail.get("status") == "applied":
                return
            if current_tail.get("operation_id") != tail.get("operation_id"):
                raise IdempotencyConflict(
                    "challenge resolution tail has a conflicting operation ID"
                )
            current_tail["status"] = "applied"
            current_tail["applied_at"] = utc_now()
            append_event(
                state,
                "fact_challenge_resolution_tail_applied",
                {"challenge_id": challenge_id, "resolution": resolution},
            )

    # ------------------------------------------------------------- task close

    def _on_operation_terminal(self, operation_id: str) -> None:
        operation = self._state["operations"].get(operation_id)
        if operation:
            self._maybe_close_task(operation["task_id"])

    def _task_temporary_reference_blocks(
        self, state: Mapping[str, Any], task: Mapping[str, Any]
    ) -> list[dict[str, Any]]:
        if not hasattr(self.store, "pending_references_for"):
            return []
        blocked: list[dict[str, Any]] = []
        source_kinds = {
            "fact",
            "route_add",
            "route_update",
            "memo",
            "claim_add",
            "obligation_add",
            "obligation_update",
        }
        for operation_id in task.get("operation_ids", []):
            operation = state["operations"].get(operation_id)
            if not operation or operation.get("kind") not in source_kinds:
                continue
            canonical_id = operation.get("canonical_id")
            if not canonical_id or operation.get("state") != OperationState.COMMITTED.value:
                continue
            for reference in self.store.pending_references_for(str(canonical_id)):
                if reference.get("cause_operation_id") != operation_id:
                    continue
                if reference.get("state") in {"pending", "rejected"}:
                    blocked.append(copy.deepcopy(dict(reference)))
        for staging_id in task.get("computation_ids", []):
            computation = state.get("computations", {}).get(staging_id)
            if (
                not computation
                or computation.get("state") != OperationState.COMMITTED.value
                or not computation.get("canonical_id")
            ):
                continue
            for reference in self.store.pending_references_for(
                str(computation["canonical_id"])
            ):
                if reference.get("cause_operation_id") not in {
                    str(staging_id),
                    f"computation:{staging_id}",
                }:
                    continue
                if reference.get("state") in {"pending", "rejected"}:
                    blocked.append(copy.deepcopy(dict(reference)))
        return blocked

    def _stage_task_soft_reference_finalization_locked(
        self,
        state: MutableMapping[str, Any],
        task: MutableMapping[str, Any],
    ) -> None:
        """Persist nonblocking abandon tails for unresolved soft references."""

        try:
            blocks = self._task_temporary_reference_blocks(state, task)
        except Exception as exc:
            task["soft_reference_discovery_pending"] = {
                "error": str(exc),
                "last_attempt_at": utc_now(),
            }
            return
        task.pop("soft_reference_discovery_pending", None)
        tail = task.setdefault("soft_reference_finalization", {})
        for block in blocks:
            temporary_id = str(block.get("temporary_id") or "")
            if not temporary_id:
                continue
            entry = tail.setdefault(
                temporary_id,
                {
                    "temporary_id": temporary_id,
                    "status": "pending",
                    "operation_id": (
                        "task-soft-reference-finalize:"
                        + stable_digest(
                            {
                                "task_id": task.get("task_id"),
                                "temporary_id": temporary_id,
                            }
                        )[:24]
                    ),
                    "reason": (
                        f"source task {task.get('task_id')} finalized before the "
                        "temporary target published"
                    ),
                    "source_reference_ids": [],
                },
            )
            reference_id = block.get("reference_id")
            if reference_id and reference_id not in entry["source_reference_ids"]:
                entry["source_reference_ids"].append(reference_id)
                entry["source_reference_ids"].sort()
        task.pop("temporary_reference_blocks", None)
        task.pop("temporary_reference_correction_attention", None)
        self._resolve_attention(
            state, f"task:{task.get('task_id')}:temporary-references"
        )

    def _reconcile_task_soft_reference_finalization(self, task_id: str) -> None:
        """Apply persisted soft-reference tails without delaying task closure."""

        if not hasattr(self.store, "abandon_temporary_reference"):
            return
        task = self._state.get("tasks", {}).get(task_id, {})
        entries = [
            copy.deepcopy(dict(entry))
            for entry in task.get("soft_reference_finalization", {}).values()
            if entry.get("status") != "applied"
        ]
        entries.sort(key=lambda entry: str(entry.get("temporary_id") or ""))
        for entry in entries:
            temporary_id = str(entry.get("temporary_id") or "")
            if not temporary_id:
                continue
            already_terminal = False
            if hasattr(self.store, "temporary_reference_status"):
                try:
                    status = self.store.temporary_reference_status(temporary_id)
                except Exception:
                    status = None
                already_terminal = bool(
                    status is None
                    or status.get("state") in {"resolved", "abandoned"}
                )
            error: str | None = None
            if not already_terminal:
                try:
                    self.store.abandon_temporary_reference(
                        temporary_id,
                        reason=str(entry.get("reason") or "source task finalized"),
                        operation_id=str(entry.get("operation_id")),
                        actor="scheduler",
                    )
                except Exception as exc:
                    if hasattr(self.store, "temporary_reference_status"):
                        try:
                            status = self.store.temporary_reference_status(temporary_id)
                        except Exception:
                            status = None
                        if status is not None and status.get("state") in {
                            "resolved",
                            "abandoned",
                        }:
                            already_terminal = True
                        else:
                            error = str(exc)
                    else:
                        error = str(exc)
            with self._mutate() as state:
                current_task = state.get("tasks", {}).get(task_id)
                current = (
                    current_task.get("soft_reference_finalization", {}).get(
                        temporary_id
                    )
                    if current_task
                    else None
                )
                if not isinstance(current, MutableMapping):
                    continue
                if error is None:
                    current["status"] = "applied"
                    current["applied_at"] = utc_now()
                    current.pop("error", None)
                    append_event(
                        state,
                        "task_soft_reference_terminalized",
                        {
                            "task_id": task_id,
                            "temporary_id": temporary_id,
                            "operation_id": current.get("operation_id"),
                        },
                    )
                else:
                    current["status"] = "pending"
                    current["error"] = error
                    current["last_attempt_at"] = utc_now()

    def _temporary_reference_correction_supplement(
        self,
        state: Mapping[str, Any],
        task: Mapping[str, Any],
        rejected_blocks: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any]:
        """Build one self-contained, bounded correction assignment."""

        grouped: dict[str, dict[str, Any]] = {}
        for block in rejected_blocks:
            temporary_id = str(block.get("temporary_id") or "")
            if not temporary_id:
                continue
            item = grouped.setdefault(
                temporary_id,
                {
                    "temporary_id": temporary_id,
                    "expected_type": block.get("expected_type"),
                    "source_operation_ids": [],
                    "source_reference_ids": [],
                },
            )
            cause_id = block.get("cause_operation_id")
            reference_id = block.get("reference_id")
            if cause_id and cause_id not in item["source_operation_ids"]:
                item["source_operation_ids"].append(cause_id)
            if reference_id and reference_id not in item["source_reference_ids"]:
                item["source_reference_ids"].append(reference_id)
        for item in grouped.values():
            item["source_operation_ids"].sort()
            item["source_reference_ids"].sort()
            if hasattr(self.store, "temporary_reference_status"):
                status = self.store.temporary_reference_status(item["temporary_id"])
                if status is not None:
                    item["target_operation_id"] = status.get("target_operation_id")
                    item["rejection_reason"] = status.get("reason")

        # Include every rejected non-fact target owned by this task.  A direct
        # target can itself have failed because it cited another rejected
        # temporary target, so the worker needs the whole local repair chain.
        target_kinds = {
            "route_add",
            "route_update",
            "memo",
            "claim_add",
            "obligation_add",
            "obligation_update",
        }
        targets: list[dict[str, Any]] = []
        for operation_id in task.get("operation_ids", []):
            operation = state.get("operations", {}).get(operation_id)
            if (
                not operation
                or operation.get("kind") not in target_kinds
                or operation.get("state") != OperationState.REJECTED.value
            ):
                continue
            proposal_id = str(operation.get("payload", {}).get("proposal_id") or "")
            if not proposal_id:
                continue
            status = (
                self.store.temporary_reference_status(proposal_id)
                if hasattr(self.store, "temporary_reference_status")
                else None
            )
            if status is not None and status.get("state") != "rejected":
                continue
            targets.append(
                {
                    "rejected_operation_id": operation_id,
                    "kind": operation.get("kind"),
                    "proposal_id": proposal_id,
                    "error": operation.get("error"),
                    "original_operation": copy.deepcopy(operation.get("payload", {})),
                }
            )
        targets.sort(key=lambda item: (str(item["proposal_id"]), str(item["kind"])))
        return {
            "kind": "temporary_reference_correction",
            "reason": (
                "One or more typed temporary targets were terminally rejected. "
                "Pending or not-yet-declared targets are not part of this correction."
            ),
            "requirements": [
                "Submit each correction with a new operation_id and the same proposal_id.",
                "Correct every rejected target in the local chain needed by the blocked links.",
                "Do not silently replace or abandon a typed relationship.",
            ],
            "rejected_references": [grouped[key] for key in sorted(grouped)],
            "rejected_targets": targets,
        }

    def _recheck_reference_blocked_tasks(self) -> None:
        """Cancel legacy soft-reference corrections and re-run closure.

        Soft references are never a mathematical correction condition.  This
        also upgrades durable snapshots created by the former blocking model.
        """

        correction_candidates = [
            task_id
            for task_id, task in self._state["tasks"].items()
            if task.get("state")
            in {TaskState.REVISION_PENDING.value, TaskState.RETRY_PENDING.value}
            and (
                preserved_attempt_supplement(
                    task.get("pending_attempt_supplement")
                )
                or {}
            ).get("kind")
            == "temporary_reference_correction"
        ]
        if correction_candidates:
            with self._mutate() as state:
                for task_id in correction_candidates:
                    task = state["tasks"].get(task_id)
                    if not task or task.get("state") not in {
                        TaskState.REVISION_PENDING.value,
                        TaskState.RETRY_PENDING.value,
                    }:
                        continue
                    supplement = preserved_attempt_supplement(
                        task.get("pending_attempt_supplement")
                    )
                    if not isinstance(supplement, Mapping) or supplement.get(
                        "kind"
                    ) != "temporary_reference_correction":
                        continue
                    task.pop("pending_attempt_supplement", None)
                    task["slot_reserved"] = False
                    self._transition_task(state, task, TaskState.POSTPROCESSING)
                    append_event(
                        state,
                        "temporary_reference_correction_cancelled",
                        {
                            "task_id": task_id,
                            "reason": (
                                "soft references do not require worker correction"
                            ),
                        },
                    )
        task_ids = [
            task_id
            for task_id, task in self._state["tasks"].items()
            if task.get("state")
            in {TaskState.POSTPROCESSING.value, TaskState.NEEDS_ATTENTION.value}
        ]
        for task_id in task_ids:
            self._maybe_close_task(task_id)

    def abandon_temporary_reference(
        self,
        temporary_id: str,
        *,
        reason: str,
        operation_id: str,
        actor: str = "scheduler",
    ) -> tuple[str, ...]:
        """Explicitly abandon unresolved non-fact links and resume task closure."""

        if not hasattr(self.store, "abandon_temporary_reference"):
            raise SchedulerError("canonical store cannot abandon temporary references")
        affected = tuple(
            self.store.abandon_temporary_reference(
                temporary_id,
                reason=reason,
                operation_id=operation_id,
                actor=actor,
            )
        )
        with self._mutate() as state:
            append_event(
                state,
                "temporary_reference_abandoned",
                {
                    "temporary_id": temporary_id,
                    "operation_id": operation_id,
                    "actor": actor,
                    "affected_source_ids": list(affected),
                },
            )
        self._recheck_reference_blocked_tasks()
        return affected

    def resolve_temporary_reference(
        self,
        temporary_id: str,
        canonical_id: str,
        *,
        resolution: str,
        operation_id: str,
        actor: str = "scheduler",
    ) -> tuple[str, ...]:
        """Explicitly correct a typed temporary target and resume closure."""

        if not hasattr(self.store, "resolve_temporary_reference"):
            raise SchedulerError("canonical store cannot resolve temporary references")
        affected = tuple(
            self.store.resolve_temporary_reference(
                temporary_id,
                canonical_id,
                resolution=resolution,
                operation_id=operation_id,
                actor=actor,
            )
        )
        with self._mutate() as state:
            append_event(
                state,
                "temporary_reference_resolved_explicitly",
                {
                    "temporary_id": temporary_id,
                    "canonical_id": canonical_id,
                    "resolution": resolution,
                    "operation_id": operation_id,
                    "actor": actor,
                    "affected_source_ids": list(affected),
                },
            )
        self._recheck_reference_blocked_tasks()
        return affected

    def _task_downstream_states(
        self, state: Mapping[str, Any], task: Mapping[str, Any]
    ) -> list[str]:
        values: list[str] = []
        for operation_id in task.get("operation_ids", []):
            operation = state["operations"].get(operation_id)
            if operation:
                values.append(str(operation["state"]))
        for staging_id in task.get("computation_ids", []):
            computation = state["computations"].get(staging_id)
            if computation:
                values.append(str(computation["state"]))
        for challenge_id in task.get("challenge_ids", []):
            challenge = state["challenges"].get(challenge_id)
            if challenge:
                values.append(str(challenge["state"]))
        return values

    def _maybe_close_task(self, task_id: str) -> None:
        continue_close = False
        apply_close_tail = False
        with self._mutate() as state:
            task = self._require_task(state, task_id)
            current = TaskState(task["state"])
            if current == TaskState.CLOSED:
                self._stage_task_soft_reference_finalization_locked(state, task)
                apply_close_tail = True
            elif task.get("close_intent") is not None:
                if current not in {
                    TaskState.POSTPROCESSING,
                    TaskState.NEEDS_ATTENTION,
                }:
                    raise WorkflowError(
                        f"task {task_id} has a close intent in {current.value}"
                    )
                continue_close = True
            elif current not in {TaskState.POSTPROCESSING, TaskState.NEEDS_ATTENTION}:
                return
            else:
                states = self._task_downstream_states(state, task)
                if any(value == OperationState.NEEDS_ATTENTION.value for value in states):
                    if current == TaskState.POSTPROCESSING:
                        self._transition_task(state, task, TaskState.NEEDS_ATTENTION)
                    return
                if any(value not in FINAL_OPERATION_STATES for value in states):
                    return
                # Soft references never cause correction or prevent closure.
                # Persist their terminalization tail now and apply it outside
                # the scheduler-state transaction.
                self._stage_task_soft_reference_finalization_locked(state, task)
                dependency_repair = task.get("dependency_repair_required")
                if dependency_repair:
                    if current in {TaskState.POSTPROCESSING, TaskState.NEEDS_ATTENTION}:
                        self._transition_task(state, task, TaskState.REVISION_PENDING)
                        task["pending_attempt_supplement"] = copy.deepcopy(dependency_repair)
                    return
                forced = task.get("forced_outcome")
                if forced == TaskOutcome.INTERRUPTED.value:
                    outcome = TaskOutcome.INTERRUPTED.value
                    summary = task.get("mechanical_summary") or {
                        "kind": "mechanical_interruption",
                        "reason": "unrecovered abnormal termination",
                    }
                else:
                    evidence = task.get("completion_evidence_ids", [])
                    owned_operation_ids = set(task.get("operation_ids", []))
                    owned_computation_ids = set(task.get("computation_ids", []))

                    def evidence_state(evidence_id: str) -> str | None:
                        if evidence_id in owned_operation_ids:
                            operation = state["operations"].get(evidence_id)
                            return (
                                str(operation.get("state"))
                                if operation is not None
                                else None
                            )
                        if evidence_id in owned_computation_ids:
                            computation = state["computations"].get(evidence_id)
                            return (
                                str(computation.get("state"))
                                if computation is not None
                                else None
                            )
                        return None

                    evidence_states = {
                        evidence_id: evidence_state(evidence_id)
                        for evidence_id in evidence
                    }
                    rejected_evidence: list[str] = []
                    reviewed_evidence = set(
                        task.get("closure_reviewed_evidence_ids", [])
                    )
                    for evidence_id, value in evidence_states.items():
                        if value not in {
                            OperationState.REJECTED.value,
                            OperationState.ABANDONED.value,
                        } or evidence_id in reviewed_evidence:
                            continue
                        operation = (
                            state["operations"].get(evidence_id, {})
                            if evidence_id in owned_operation_ids
                            else {}
                        )
                        if operation.get("kind") == "fact":
                            candidate_id = str(operation.get("candidate_id") or "")
                            lineage = state.get("fact_lineages", {}).get(
                                candidate_id, {}
                            )
                            latest_operation_id = (
                                self._latest_relevant_fact_operation_id(
                                    state, candidate_id
                                )
                            )
                            if (
                                lineage.get("closed")
                                or latest_operation_id != evidence_id
                            ):
                                continue
                        rejected_evidence.append(evidence_id)
                    # The one-shot main-sort is a selective ingestion barrier,
                    # not an ordinary mathematical assignment.  A rejected
                    # provisional promotion remains terminal in its promotion
                    # ledger and must not resume the sorter for a correction
                    # attempt; Franta planning proceeds using only publications
                    # that actually committed.
                    if task.get("agent_system") == "franta-sort":
                        rejected_evidence = []
                    if rejected_evidence:
                        # Non-fact completion evidence gets a correction attempt;
                        # fact rejection already installed its exact repair state.
                        if current == TaskState.POSTPROCESSING:
                            self._transition_task(state, task, TaskState.REVISION_PENDING)
                            task["pending_attempt_supplement"] = {
                                "kind": "completion_evidence_correction",
                                "rejected_operation_ids": rejected_evidence,
                            }
                        return
                    closing_attempt = task["attempts"][-1] if task.get("attempts") else {}
                    if closing_attempt.get("kind") in {
                        "fact_concession",
                        "concession_memo_correction",
                    }:
                        concession_memos = [
                            operation
                            for operation in state["operations"].values()
                            if operation.get("task_id") == task_id
                            and operation.get("kind") == "memo"
                            and operation.get("state") == OperationState.COMMITTED.value
                            and int(operation.get("attempt", -1))
                            == int(closing_attempt.get("attempt", -2))
                        ]
                        if not concession_memos:
                            self._transition_task(state, task, TaskState.REVISION_PENDING)
                            task["pending_attempt_supplement"] = {
                                "kind": "concession_memo_correction",
                                "reason": "required failure memo did not commit",
                                "forbid_new_candidate": True,
                                "require_failure_memo": True,
                            }
                            return
                        for lineage in state["fact_lineages"].values():
                            if lineage["task_id"] == task_id and lineage.get("concession_required"):
                                lineage["closed"] = True
                    outcome = task.get("proposed_outcome")
                    if outcome not in {
                        TaskOutcome.FINISHED.value,
                        TaskOutcome.PROGRESS.value,
                        TaskOutcome.FAILED.value,
                    }:
                        if current == TaskState.POSTPROCESSING:
                            self._transition_task(state, task, TaskState.NEEDS_ATTENTION)
                        self._add_attention(
                            state,
                            f"task:{task_id}:invalid-outcome",
                            "invalid_worker_outcome",
                            {"proposed_outcome": outcome},
                            scope="task",
                            owner_id=task_id,
                        )
                        return
                    summary = task.get("closing_summary") or {}
                if current == TaskState.NEEDS_ATTENTION:
                    self._transition_task(state, task, TaskState.POSTPROCESSING)
                task["final_status"] = outcome
                task["final_summary"] = copy.deepcopy(summary)
                if isinstance(summary, Mapping):
                    summary_text = str(
                        summary.get("summary")
                        or summary.get("progress")
                        or summary.get("reason")
                        or canonical_json(summary)
                    )
                else:
                    summary_text = str(summary)
                canonical_computations = [
                    state["computations"][staging_id].get("canonical_id")
                    for staging_id in task["computation_ids"]
                    if state["computations"].get(staging_id, {}).get("canonical_id")
                ]
                canonical_record = self._canonical_task_record(
                    {
                        "id": task_id,
                        "assign_record": copy.deepcopy(task["assign_record"]),
                        "final_status": outcome,
                        "final_summary": summary_text,
                        "artifact_references": copy.deepcopy(
                            task.get("artifact_references", [])
                        ),
                        "computation_ids": canonical_computations,
                    }
                )
                task["close_intent"] = {
                    "record": canonical_record,
                    "record_digest": stable_digest(canonical_record),
                    "publication_operation_id": f"task-close:{task_id}",
                    "created_at": utc_now(),
                }
                task["after_close_applied"] = False
                append_event(
                    state,
                    "task_close_intent_persisted",
                    {
                        "task_id": task_id,
                        "record_digest": task["close_intent"]["record_digest"],
                    },
                )
                continue_close = True
        if continue_close or apply_close_tail:
            self._reconcile_task_soft_reference_finalization(task_id)
        if continue_close:
            self._continue_task_close(task_id)
        elif apply_close_tail and self._task_publication_committed(task_id):
            self._apply_task_close_tail(task_id)

    def closure_review_input(self, task_id: str) -> dict[str, Any]:
        task = self._state["tasks"].get(task_id)
        if not task:
            raise SchedulerError(f"unknown task {task_id}")
        if not task.get("closure_review_required"):
            raise WorkflowError("task has no explicit closure ambiguity")
        operations = {
            operation_id: copy.deepcopy(self._state["operations"].get(operation_id))
            for operation_id in task.get("operation_ids", [])
        }
        computations = {
            staging_id: copy.deepcopy(self._state["computations"].get(staging_id))
            for staging_id in task.get("computation_ids", [])
        }
        challenges = {
            challenge_id: copy.deepcopy(self._state["challenges"].get(challenge_id))
            for challenge_id in task.get("challenge_ids", [])
        }
        value = {
            "task_id": task_id,
            "task_objective": task["task_card"].get("objective"),
            "closure_ambiguity": copy.deepcopy(task.get("closure_review_reason")),
            "proposed_outcome": task.get("proposed_outcome"),
            "closing_summary": copy.deepcopy(task.get("closing_summary")),
            "attempts": copy.deepcopy(task.get("attempts", [])),
            "operations": operations,
            "computations": computations,
            "challenges": challenges,
        }
        value.update(
            self._advisor_problem_binding_locked(self._state, task_id=task_id)
        )
        return value

    def prepare_main_closure_review_call(self, task_id: str) -> str:
        task = self._state["tasks"].get(task_id)
        if task and task.get("closure_review_call_id"):
            return str(task["closure_review_call_id"])
        return self._prepare_owned_call(
            "main-closure-review",
            self.closure_review_input(task_id),
            owner_collection="tasks",
            owner_id=task_id,
            pointer_field="closure_review_call_id",
            continuation={"task_id": task_id},
        )

    def commit_main_closure_review_call(self, call_id: str) -> None:
        call = self._state["calls"].get(call_id)
        if not call or call["kind"] != "main-closure-review":
            raise SchedulerError("unknown main closure-review call")
        if call["status"] == CallState.COMMITTED.value:
            return
        if call["status"] != CallState.COMPLETED.value:
            raise WorkflowError("main closure-review call has no completed result")
        task_id = str(call["continuation"]["task_id"])
        result = copy.deepcopy(dict(call["result"]))
        outcome = str(result.get("outcome") or "")
        summary = result.get("summary")
        if not isinstance(summary, Mapping):
            raise SchedulerError("closure review requires a structured summary")
        self.apply_main_closure_review(task_id, outcome, summary)
        self.mark_call_committed(call_id)

    def apply_main_closure_review(
        self, task_id: str, outcome: str, summary: Mapping[str, Any]
    ) -> None:
        if outcome not in {
            TaskOutcome.FINISHED.value,
            TaskOutcome.PROGRESS.value,
            TaskOutcome.FAILED.value,
        }:
            raise SchedulerError("closure review must choose finished/progress/failed")
        exact_summary = copy.deepcopy(dict(summary))
        result_digest = stable_digest(
            {"outcome": outcome, "summary": exact_summary}
        )
        with self._mutate() as state:
            task = self._require_task(state, task_id)
            prior_digest = task.get("closure_review_result_digest")
            if prior_digest is not None:
                if prior_digest != result_digest:
                    raise IdempotencyConflict(
                        "closure review was replayed with a different result"
                    )
                # The semantic decision is already durable.  The close tail is
                # deliberately retried below because publication may have been
                # interrupted after this transaction.
                pass
            else:
                if not task.get("closure_review_required"):
                    raise WorkflowError("task does not require main-agent closure review")
                reason = copy.deepcopy(task.get("closure_review_reason"))
                if isinstance(reason, Mapping) and reason.get("operation_id"):
                    reviewed = task.setdefault("closure_reviewed_evidence_ids", [])
                    if reason["operation_id"] not in reviewed:
                        reviewed.append(reason["operation_id"])
                task["closure_review_required"] = False
                task.pop("closure_review_reason", None)
                task["proposed_outcome"] = outcome
                task["closing_summary"] = exact_summary
                task["closure_review_result_digest"] = result_digest
                if task["state"] == TaskState.NEEDS_ATTENTION.value:
                    self._transition_task(state, task, TaskState.POSTPROCESSING)
                self._resolve_attention(state, f"task:{task_id}:closure")
                append_event(
                    state,
                    "closure_review_result_applied",
                    {"task_id": task_id, "result_digest": result_digest},
                )
        self._maybe_close_task(task_id)

    def _publish_task_record(self, task_id: str, record: Mapping[str, Any]) -> bool:
        return self._publish_task_record_with_key(
            task_id,
            record,
            operation_id=f"task-close:{task_id}",
        )

    @staticmethod
    def _canonical_task_record(record: Mapping[str, Any]) -> dict[str, Any]:
        """Return the canonical task-memory representation.

        Scheduler state keeps ``relative_path`` because the task archive
        broker resolves it against its private archive root.  A durable close
        intent freezes the canonical store schema's ``path`` field.  Accepting
        either spelling here keeps replay idempotent across that boundary.
        """

        canonical = copy.deepcopy(dict(record))
        references: list[dict[str, Any]] = []
        for raw in canonical.get("artifact_references", []):
            reference = copy.deepcopy(dict(raw))
            references.append(
                {
                    "kind": reference.get("kind", "artifact"),
                    "path": reference.get("path", reference.get("relative_path")),
                    "sha256": reference.get("sha256"),
                }
            )
        canonical["artifact_references"] = references
        return canonical

    def _publish_task_record_with_key(
        self,
        task_id: str,
        record: Mapping[str, Any],
        *,
        operation_id: str,
    ) -> bool:
        canonical_record = self._canonical_task_record(record)
        try:
            if hasattr(self.store, "add_task"):
                result = self.store.add_task(
                    operation_id,
                    canonical_record,
                    actor="scheduler",
                )
                if _status_from_result(result) != "committed":
                    raise SchedulerError(
                        _error_from_result(result) or "task publication rejected"
                    )
            elif hasattr(self.store, "apply_operation"):
                result = self.store.apply_operation(
                    operation_id,
                    "task",
                    canonical_record,
                    actor="scheduler",
                )
                if _status_from_result(result) != "committed":
                    raise SchedulerError(
                        _error_from_result(result) or "task publication rejected"
                    )
            else:
                raise SchedulerError("store does not expose task publication")
        except Exception as exc:
            # The close intent, rather than a closed task, is already durable.
            # Publication remains idempotently repairable without reopening
            # any mathematical decision.
            with self._mutate() as state:
                self._add_attention(
                    state,
                    f"task-publication:{task_id}",
                    "task_memory_publication_failed",
                    {"error": str(exc)},
                    scope="task",
                    owner_id=task_id,
                )
            return False
        self._resolve_task_publication_attention(task_id)
        return True

    def _resolve_task_publication_attention(self, task_id: str) -> None:
        attention_id = f"task-publication:{task_id}"
        if not any(
            item.get("attention_id") == attention_id
            and item.get("resolved_at") is None
            for item in self._state.get("needs_attention", [])
        ):
            return
        with self._mutate() as state:
            self._resolve_attention(state, attention_id)

    @staticmethod
    def _task_summary_text(summary: Any) -> str:
        if isinstance(summary, Mapping):
            return str(
                summary.get("summary")
                or summary.get("progress")
                or summary.get("reason")
                or canonical_json(summary)
            )
        return str(summary)

    def _closed_task_publication_record(
        self, task_id: str, task: Mapping[str, Any]
    ) -> dict[str, Any]:
        canonical_computations = [
            self._state["computations"][staging_id].get("canonical_id")
            for staging_id in task.get("computation_ids", [])
            if self._state["computations"].get(staging_id, {}).get("canonical_id")
        ]
        return {
            "id": task_id,
            "assign_record": copy.deepcopy(task["assign_record"]),
            "final_status": task["final_status"],
            "final_summary": self._task_summary_text(task.get("final_summary")),
            "artifact_references": copy.deepcopy(task.get("artifact_references", [])),
            "computation_ids": canonical_computations,
        }

    def _task_publication_status(self, operation_id: str) -> str | None:
        if not hasattr(self.store, "operation_status"):
            return None
        result = self.store.operation_status(operation_id)
        return None if result is None else _status_from_result(result)

    def _committed_task_publication_operation(self, task_id: str) -> str | None:
        for operation_id in (
            f"task-close:{task_id}",
            f"task-close-repair:{task_id}",
        ):
            if self._task_publication_status(operation_id) == "committed":
                return operation_id
        return None

    def _task_publication_committed(self, task_id: str) -> bool:
        return self._committed_task_publication_operation(task_id) is not None

    def _continue_task_close(self, task_id: str) -> None:
        """Publish a frozen close intent, then durably close and run its tail."""

        task = self._state["tasks"].get(task_id)
        if not task:
            raise SchedulerError(f"unknown task {task_id}")
        if task.get("state") == TaskState.CLOSED.value:
            self._apply_task_close_tail(task_id)
            return
        intent = task.get("close_intent")
        if not isinstance(intent, Mapping):
            raise WorkflowError(f"task {task_id} has no durable close intent")
        record = intent.get("record")
        if not isinstance(record, Mapping):
            raise SchedulerError(f"task {task_id} close intent has no canonical record")
        record_digest = str(intent.get("record_digest") or "")
        if stable_digest(record) != record_digest:
            raise SchedulerError(f"task {task_id} close intent digest mismatch")

        committed_operation = self._committed_task_publication_operation(task_id)
        operation_id = str(
            committed_operation
            or intent.get("publication_operation_id")
            or f"task-close:{task_id}"
        )
        if (
            committed_operation is None
            and operation_id == f"task-close:{task_id}"
            and self._task_publication_status(operation_id) == "rejected"
        ):
            # Releases before the canonical boundary fix may have durably
            # rejected this key.  A distinct deterministic key is required by
            # store idempotency for the corrected record.
            operation_id = f"task-close-repair:{task_id}"
        if intent.get("publication_operation_id") != operation_id:
            with self._mutate() as state:
                current = self._require_task(state, task_id).get("close_intent")
                if not isinstance(current, MutableMapping):
                    raise SchedulerError(f"task {task_id} lost its close intent")
                if current.get("record_digest") != record_digest:
                    raise IdempotencyConflict(
                        f"task {task_id} close intent changed during publication"
                    )
                current["publication_operation_id"] = operation_id

        if not self._publish_task_record_with_key(
            task_id,
            record,
            operation_id=operation_id,
        ):
            return
        self._finalize_published_task_close(
            task_id,
            record_digest=record_digest,
            operation_id=operation_id,
        )
        self._apply_task_close_tail(task_id)

    def _finalize_published_task_close(
        self,
        task_id: str,
        *,
        record_digest: str,
        operation_id: str,
    ) -> None:
        """Commit control closure only after the exact task record exists."""

        with self._mutate() as state:
            task = self._require_task(state, task_id)
            if task.get("state") == TaskState.CLOSED.value:
                return
            intent = task.get("close_intent")
            if not isinstance(intent, MutableMapping):
                raise SchedulerError(f"task {task_id} lost its close intent")
            if intent.get("record_digest") != record_digest:
                raise IdempotencyConflict(
                    f"task {task_id} close intent changed after publication"
                )
            if stable_digest(intent.get("record")) != record_digest:
                raise SchedulerError(f"task {task_id} close intent digest mismatch")
            current = TaskState(task["state"])
            if current not in {
                TaskState.POSTPROCESSING,
                TaskState.NEEDS_ATTENTION,
            }:
                raise WorkflowError(
                    f"task {task_id} cannot finalize close from {current.value}"
                )
            legacy_close_was_announced = bool(
                intent.get("recovered_from_legacy_close")
            ) and any(
                event.get("type") == "task_closed"
                and event.get("payload", {}).get("task_id") == task_id
                for event in state.get("events", [])
            )
            intent["publication_operation_id"] = operation_id
            intent["published_at"] = utc_now()
            self._transition_task(state, task, TaskState.CLOSED)
            task["slot_reserved"] = False
            self._release_lineage_task_locked(state, task)
            if not legacy_close_was_announced:
                append_event(
                    state,
                    "task_closed",
                    {"task_id": task_id, "final_status": task["final_status"]},
                )

    def _install_legacy_close_intent(self, task_id: str) -> None:
        """Normalize a closed pre-two-phase snapshot for deterministic replay."""

        task = self._state["tasks"].get(task_id)
        if not task or task.get("close_intent") is not None:
            return
        record = self._canonical_task_record(
            self._closed_task_publication_record(task_id, task)
        )
        operation_id = (
            self._committed_task_publication_operation(task_id)
            or f"task-close-repair:{task_id}"
        )
        with self._mutate() as state:
            current = self._require_task(state, task_id)
            if current.get("close_intent") is not None:
                return
            current["close_intent"] = {
                "record": record,
                "record_digest": stable_digest(record),
                "publication_operation_id": operation_id,
                "created_at": utc_now(),
                "recovered_from_legacy_close": True,
            }
            current.setdefault("after_close_applied", False)
            append_event(
                state,
                "legacy_task_close_intent_recovered",
                {"task_id": task_id},
            )

    def _migrate_legacy_closed_task_for_publication(self, task_id: str) -> None:
        """Move an unpublished legacy close behind the canonical boundary.

        Pre-two-phase snapshots may say ``closed`` even though their task-memory
        publication failed.  That historical transition cannot be treated as a
        valid close: preserve the frozen record installed above, audit the
        one-time schema migration, and let the ordinary publish/finalize path
        close the task only after canonical publication succeeds.
        """

        with self._mutate() as state:
            task = self._require_task(state, task_id)
            if task.get("state") != TaskState.CLOSED.value:
                return
            intent = task.get("close_intent")
            if not isinstance(intent, Mapping) or not intent.get(
                "recovered_from_legacy_close"
            ):
                raise WorkflowError(
                    f"task {task_id} is not an unpublished legacy close"
                )
            task["state"] = TaskState.POSTPROCESSING.value
            task["slot_reserved"] = False
            append_event(
                state,
                "legacy_task_close_migrated_for_publication",
                {
                    "task_id": task_id,
                    "from": TaskState.CLOSED.value,
                    "to": TaskState.POSTPROCESSING.value,
                    "record_digest": intent.get("record_digest"),
                },
            )

    def _repair_closed_task_publications(self) -> None:
        """Resume every durable phase of task closure after a full stop.

        The repair key is intentionally distinct from the original close key:
        older releases durably rejected the latter after sending the internal
        ``relative_path`` shape to the canonical store.  Reusing that key with
        the corrected boundary representation would violate store
        idempotency.
        """

        for task_id in list(self._state["tasks"]):
            task = self._state["tasks"][task_id]
            if task.get("state") != TaskState.CLOSED.value:
                if task.get("close_intent") is not None:
                    self._continue_task_close(task_id)
                continue
            self._install_legacy_close_intent(task_id)
            task = self._state["tasks"][task_id]
            if not self._task_publication_committed(task_id):
                self._migrate_legacy_closed_task_for_publication(task_id)
                self._continue_task_close(task_id)
                continue
            self._resolve_task_publication_attention(task_id)
            self._apply_task_close_tail(task_id)

    def _mark_task_close_tail_applied(self, task_id: str) -> None:
        with self._mutate() as state:
            task = self._require_task(state, task_id)
            if task.get("after_close_applied"):
                return
            if task.get("state") != TaskState.CLOSED.value:
                raise WorkflowError("post-close effects require a closed task")
            task["after_close_applied"] = True
            task["after_close_applied_at"] = utc_now()
            append_event(state, "task_close_tail_applied", {"task_id": task_id})

    def _apply_task_close_tail(self, task_id: str) -> None:
        task = self._state["tasks"].get(task_id)
        if not task or task.get("state") != TaskState.CLOSED.value:
            return
        if task.get("after_close_applied"):
            return
        sprint_id = task.get("sprint_id")
        if sprint_id:
            self.advance_sprint(sprint_id)
        self._maybe_complete_project()
        self._mark_task_close_tail_applied(task_id)

    def _after_task_closed(self, task_id: str) -> None:
        self._apply_task_close_tail(task_id)

    # ----------------------------------------------------------- root outcome

    def _record_root_resolution_in_state(
        self,
        state: MutableMapping[str, Any],
        fact_id: str,
        outcome: str,
        *,
        operation_id: str,
    ) -> None:
        if outcome not in {"proved", "disproved"}:
            raise SchedulerError("root resolution outcome must be proved or disproved")
        root = state["root"]
        if root.get("solution_fact_id") is None:
            root["solution_fact_id"] = fact_id
            root["outcome"] = outcome
            root["obligation_status"] = "resolved"
            root["terminal_main_decision_done"] = False
            root["resolution_event_id"] = self._event_cursor_of(state) + 1
            self._cancel_unlaunched_controls_for_resolution(state)
            self._transition_gate(state, GateState.RESOLUTION_PENDING)
            append_event(
                state,
                "root_resolution_first",
                {"fact_id": fact_id, "outcome": outcome, "operation_id": operation_id},
            )
            return
        if root["outcome"] == outcome:
            if fact_id != root["solution_fact_id"] and fact_id not in {
                item["fact_id"] for item in root["alternates"]
            }:
                root["alternates"].append(
                    {"fact_id": fact_id, "outcome": outcome, "operation_id": operation_id}
                )
                append_event(
                    state,
                    "root_resolution_alternate",
                    {"fact_id": fact_id, "outcome": outcome},
                )
            return
        conflict = {
            "first_fact_id": root["solution_fact_id"],
            "first_outcome": root["outcome"],
            "conflicting_fact_id": fact_id,
            "conflicting_outcome": outcome,
            "operation_id": operation_id,
        }
        root["conflicts"].append(conflict)
        root["obligation_status"] = "resolution_conflict"
        self._add_attention(state, f"root-conflict:{fact_id}", "conflicting_root_resolutions", conflict)
        append_event(state, "root_resolution_conflict", conflict)

    def _cancel_unlaunched_controls_for_resolution(
        self, state: MutableMapping[str, Any]
    ) -> None:
        trim_transition = trim_control.supersede_for_root_resolution(state["trim"])
        state["trim"] = copy.deepcopy(dict(trim_transition.trim))
        exploration_state.supersede_for_root_resolution(
            state,
            cancel_call=lambda call: _apply_call_event(
                state, call, call_machine.Cancel()
            ),
        )

    def record_terminal_main_decision(
        self,
        assign_report: Mapping[str, Any] | None = None,
        *,
        batch_id: str | None = None,
    ) -> str | None:
        if self.gate != GateState.RESOLUTION_PENDING:
            raise WorkflowError("terminal main decision requires resolution_pending")
        if self._state["root"].get("terminal_main_decision_done"):
            if assign_report is not None:
                raise WorkflowError("terminal main decision already completed")
            return self._state.get("proof_writer_task_id")
        task_id: str | None = None
        if assign_report is not None:
            report = copy.deepcopy(dict(assign_report))
            mode = str(report.get("mode", report.get("work_mode", "")))
            if mode != "proof-writer":
                raise SchedulerError("terminal call may assign only proof-writer")
            if self._state.get("proof_writer_task_id"):
                raise WorkflowError("at most one proof-writer assignment is allowed")
            ids = self.submit_batch(
                batch_id or self._next_external_id("BATCH-PROOF"),
                [report],
                terminal_proof_writer=True,
            )
            task_id = ids[0]
        with self._mutate() as state:
            state["root"]["terminal_main_decision_done"] = True
            append_event(
                state,
                "terminal_main_decision",
                {"proof_writer_task_id": task_id},
            )
        self._maybe_complete_project()
        return task_id

    def record_main_checkpoint(
        self,
        *,
        call_id: str,
        observed_event_cursor: int,
        waiting_for: Sequence[str] = (),
    ) -> None:
        """Persist the wake-up boundary consumed by one main-agent call."""

        with self._mutate() as state:
            call = state["calls"].get(call_id)
            if not call or call.get("kind") != "main":
                raise SchedulerError("main checkpoint requires a persisted main call")
            if call.get("status") != CallState.COMMITTED.value:
                raise WorkflowError("main checkpoint requires a committed main result")
            if int(observed_event_cursor) > self._event_cursor_of(state):
                raise SchedulerError("main checkpoint cannot observe a future event")
            checkpoint = state["main_checkpoint"]
            checkpoint.update(
                {
                    "event_cursor": int(observed_event_cursor),
                    "call_id": call_id,
                    "waiting_for": sorted(set(str(item) for item in waiting_for)),
                }
            )
            append_event(
                state,
                "main_checkpoint_committed",
                {
                    "call_id": call_id,
                    "observed_event_cursor": int(observed_event_cursor),
                },
            )
            # The checkpoint event is administrative and must not wake the main
            # agent by itself.
            checkpoint["event_cursor"] = self._event_cursor_of(state)

    def _next_external_id(self, prefix: str) -> str:
        with self._mutate() as state:
            return self._allocate_id(state, prefix)

    def _maybe_complete_project(self) -> None:
        with self._mutate() as state:
            if GateState(state["gate"]) != GateState.RESOLUTION_PENDING:
                return
            root = state["root"]
            if not root.get("terminal_main_decision_done") or root.get("conflicts"):
                return
            if any(task["state"] != TaskState.CLOSED.value for task in state["tasks"].values()):
                return
            self._transition_gate(state, GateState.COMPLETED)
            append_event(
                state,
                "project_completed",
                {"fact_id": root["solution_fact_id"], "outcome": root["outcome"]},
            )

    def _stop_proof_writer_for_revocation_locked(
        self, state: MutableMapping[str, Any], affected: set[str]
    ) -> str | None:
        task_id = state.get("proof_writer_task_id")
        if not task_id:
            return None
        task = state["tasks"].get(task_id)
        if not task or task["state"] == TaskState.CLOSED.value:
            return None
        assigned_root = task.get("task_card", {}).get("root_solution_fact_id")
        if assigned_root and assigned_root not in affected:
            return None
        current = TaskState(task["state"])
        task["stop_requested"] = {
            "reason": "assigned root-resolution fact was revoked",
            "affected_fact_ids": sorted(affected),
            "requested_at": utc_now(),
        }
        if current == TaskState.RUNNING:
            self._transition_task(state, task, TaskState.STOPPING)
            append_event(
                state,
                "proof_writer_stop_requested",
                {"task_id": task_id, "affected_fact_ids": sorted(affected)},
            )
            return None
        if current in {TaskState.ATTEMPT_ENDED, TaskState.POSTPROCESSING}:
            # The process has ended; retain every staged artifact and let the
            # ordinary downstream pipeline reach terminal dispositions.
            append_event(
                state,
                "proof_writer_draining_after_revocation",
                {"task_id": task_id, "affected_fact_ids": sorted(affected)},
            )
            return None
        if current != TaskState.STOPPING:
            self._transition_task(state, task, TaskState.STOPPING)
        task["forced_outcome"] = TaskOutcome.INTERRUPTED.value
        task["mechanical_summary"] = {
            "kind": "scheduler_stop",
            "reason": "proof-writer cancelled before launch because its root fact was revoked",
            "last_progress_id": task.get("last_progress_id"),
        }
        self._transition_task(state, task, TaskState.ATTEMPT_ENDED)
        self._transition_task(state, task, TaskState.POSTPROCESSING)
        append_event(
            state,
            "proof_writer_cancelled_before_launch",
            {"task_id": task_id, "affected_fact_ids": sorted(affected)},
        )
        return task_id

    def revoke_fact(
        self,
        fact_id: str,
        *,
        reason: str,
        evidence: Mapping[str, Any] | None = None,
        operation_id: str | None = None,
    ) -> Any:
        exact_evidence = copy.deepcopy(dict(evidence or {}))
        application_digest = stable_digest(
            {"fact_id": fact_id, "reason": reason, "evidence": exact_evidence}
        )
        result = None
        if hasattr(self.store, "revoke_fact"):
            arguments: dict[str, Any] = {
                "reason": reason,
                "actor": "scheduler",
                "evidence": exact_evidence,
            }
            if operation_id is not None:
                arguments["operation_id"] = operation_id
            result = self.store.revoke_fact(fact_id, **arguments)
        if operation_id is not None:
            prior = self._state.get("revocation_applications", {}).get(operation_id)
            if prior is not None:
                if prior.get("input_digest") != application_digest:
                    raise IdempotencyConflict(
                        "revocation operation was replayed with different input"
                    )
                return result
        affected: set[str] = {fact_id}
        if result is not None:
            result_dict = _as_dict(result)
            affected.update(str(x) for x in result_dict.get("revoked_fact_ids", []))
            affected.update(str(x) for x in result_dict.get("descendant_ids", []))
        canonical_root = None
        if hasattr(self.store, "root_resolution_state"):
            canonical_root = self.store.root_resolution_state()
        cancelled_proof_writer: str | None = None
        with self._mutate() as state:
            applications = state.setdefault("revocation_applications", {})
            if operation_id is not None and operation_id in applications:
                if applications[operation_id].get("input_digest") != application_digest:
                    raise IdempotencyConflict(
                        "revocation operation was replayed with different input"
                    )
                return result
            root = state["root"]
            prior_primary = root.get("solution_fact_id")
            prior_outcome = root.get("outcome")
            prior_conflicts = list(root.get("conflicts", []))
            root["alternates"] = [
                item
                for item in root.get("alternates", [])
                if item.get("fact_id") not in affected
            ]
            root["conflicts"] = [
                item
                for item in prior_conflicts
                if item.get("conflicting_fact_id") not in affected
                and item.get("first_fact_id") not in affected
            ]
            for item in prior_conflicts:
                if item not in root["conflicts"]:
                    self._resolve_attention(
                        state,
                        f"root-conflict:{item.get('conflicting_fact_id')}",
                    )

            if prior_primary in affected:
                cancelled_proof_writer = self._stop_proof_writer_for_revocation_locked(
                    state, affected
                )
                surviving_same_outcome = next(
                    (
                        item
                        for item in root["alternates"]
                        if item.get("outcome") == prior_outcome
                    ),
                    None,
                )
                replacement_id = (
                    surviving_same_outcome.get("fact_id")
                    if surviving_same_outcome
                    else None
                )
                replacement_outcome = prior_outcome
                canonical_status = None
                if isinstance(canonical_root, Mapping):
                    replacement_id = canonical_root.get("root_solution_fact_id")
                    replacement_outcome = canonical_root.get("root_resolution_outcome")
                    canonical_status = canonical_root.get("status")
                if replacement_id:
                    promoted = next(
                        (
                            item
                            for item in root["alternates"]
                            if item.get("fact_id") == replacement_id
                        ),
                        {"fact_id": replacement_id, "outcome": replacement_outcome},
                    )
                    root["alternates"] = [
                        item
                        for item in root["alternates"]
                        if item.get("fact_id") != replacement_id
                    ]
                    root["solution_fact_id"] = replacement_id
                    root["outcome"] = replacement_outcome
                    root["obligation_status"] = (
                        "resolution_conflict"
                        if canonical_status == "needs_attention" or root["conflicts"]
                        else "resolved"
                    )
                    append_event(
                        state,
                        "root_resolution_alternate_promoted",
                        {
                            "revoked_fact_id": prior_primary,
                            "promoted_fact_id": promoted["fact_id"],
                            "outcome": replacement_outcome,
                        },
                    )
                else:
                    root["solution_fact_id"] = None
                    root["outcome"] = None
                    if canonical_status == "needs_attention":
                        root["obligation_status"] = "resolution_conflict"
                        self._add_attention(
                            state,
                            "root:resolution-review",
                            "root_resolution_requires_review_after_revocation",
                            {"revoked_fact_id": prior_primary},
                        )
                    else:
                        root["obligation_status"] = "active"
                        root["terminal_main_decision_done"] = False
                        root["alternates"] = []
                        root["conflicts"] = []
                        state["proof_writer_task_id"] = None
                        if GateState(state["gate"]) in {
                            GateState.COMPLETED,
                            GateState.RESOLUTION_PENDING,
                        }:
                            self._transition_gate(state, GateState.OPEN)
            elif root["conflicts"]:
                root["obligation_status"] = "resolution_conflict"
            elif root.get("solution_fact_id"):
                root["obligation_status"] = "resolved"
            append_event(
                state,
                "fact_revocation_applied",
                {"fact_id": fact_id, "affected_ids": sorted(affected), "reason": reason},
            )
            if operation_id is not None:
                applications[operation_id] = {
                    "operation_id": operation_id,
                    "input_digest": application_digest,
                    "fact_id": fact_id,
                    "affected_ids": sorted(affected),
                    "status": "applied",
                    "applied_at": utc_now(),
                }
        if cancelled_proof_writer:
            self._maybe_close_task(cancelled_proof_writer)
        self._maybe_complete_project()
        return result

    def bind_canonical_root_obligation(self) -> None:
        """Bind an already-published bootstrap obligation in canonical storage.

        The outer bootstrap coordinator publishes the root obligation through
        the normal obligation path, then calls this method.  Scheduler
        bootstrap itself never bypasses canonical validation.
        """
        obligation_id = self._state["root"].get("obligation_id")
        if not obligation_id:
            raise SchedulerError("scheduler has no root obligation ID")
        if not hasattr(self.store, "set_root_obligation"):
            raise SchedulerError("Store does not expose set_root_obligation")
        self.store.set_root_obligation(obligation_id, actor="scheduler")
        with self._mutate() as state:
            state["root"]["canonical_binding_committed"] = True
            append_event(
                state,
                "canonical_root_obligation_bound",
                {"obligation_id": obligation_id},
            )

    # --------------------------------------------------------- trim/guidance

    def submit_stuck_report(self, batch_id: str, report: Mapping[str, Any]) -> None:
        digest = stable_digest(report)
        with self._mutate() as state:
            try:
                transition = trim_control.receive_stuck_report(
                    state["trim"],
                    gate=GateState(state["gate"]),
                    batch_id=batch_id,
                    report=report,
                    report_digest=digest,
                    base_event_id=self._event_cursor_of(state),
                )
            except trim_control.TrimControlError as exc:
                _raise_trim_control_error(exc)
            state["trim"] = copy.deepcopy(dict(transition.trim))
            if not transition.changed:
                return
            assert transition.gate_target is not None
            self._transition_gate(state, transition.gate_target)
            _append_trim_effects(state, transition.effects)

    def start_trim_review_round(self) -> str:
        """Bind a top-level review/trim round to its three-round session."""

        with self._mutate() as state:
            rounds_per_session = int(
                state["limits"]["trimmer_rounds_per_session"]
            )
            review = state["trim"].get("active_review") or {}
            new_session_id = None
            if not review.get("session_id") and trim_control.needs_new_trimmer_session(
                state["trim"], rounds_per_session=rounds_per_session
            ):
                new_session_id = self._allocate_id(state, "TRIM-SESSION")
            try:
                transition = trim_control.start_review_round(
                    state["trim"],
                    gate=GateState(state["gate"]),
                    rounds_per_session=rounds_per_session,
                    new_session_id=new_session_id,
                )
            except trim_control.TrimControlError as exc:
                _raise_trim_control_error(exc)
            state["trim"] = copy.deepcopy(dict(transition.trim))
            _append_trim_effects(state, transition.effects)
            return str(transition.value)

    def apply_trim_review_decision(self, decision: str, reason: str) -> str | None:
        try:
            trim_control.validate_review_decision(decision)
        except trim_control.TrimControlError as exc:
            _raise_trim_control_error(exc)
        if not (self._state["trim"].get("active_review") or {}).get("session_id"):
            self.start_trim_review_round()
        with self._mutate() as state:
            try:
                transition = trim_control.apply_review_decision(
                    state["trim"],
                    gate=GateState(state["gate"]),
                    decision=decision,
                    reason=reason,
                    # The established cutoff includes the gate transition.
                    cutoff_event_id=self._event_cursor_of(state) + 1,
                )
            except trim_control.TrimControlError as exc:
                _raise_trim_control_error(exc)
            state["trim"] = copy.deepcopy(dict(transition.trim))
            assert transition.gate_target is not None
            self._transition_gate(state, transition.gate_target)
            _append_trim_effects(state, transition.effects)
            return None if transition.value is None else str(transition.value)

    def set_trim_phase(self, phase: str) -> None:
        with self._mutate() as state:
            try:
                transition = trim_control.set_trim_phase(
                    state["trim"], gate=GateState(state["gate"]), phase=phase
                )
            except trim_control.TrimControlError as exc:
                _raise_trim_control_error(exc)
            state["trim"] = copy.deepcopy(dict(transition.trim))
            _append_trim_effects(state, transition.effects)

    def commit_trim(
        self,
        proposal: Mapping[str, Any],
        *,
        expected_portfolio_revision: int,
        confirmed_through_event_id: int,
        continuation_call_id: str | None = None,
    ) -> int:
        exact = copy.deepcopy(dict(proposal))
        with self._mutate() as state:
            relevant_cursor = self._event_cursor_of(state)
            try:
                trim_plan = trim_control.plan_portfolio_commit(
                    state["trim"],
                    exact,
                    gate=GateState(state["gate"]),
                    expected_portfolio_revision=expected_portfolio_revision,
                    confirmed_through_event_id=confirmed_through_event_id,
                    commit_event_cursor=relevant_cursor,
                )
            except trim_control.TrimControlError as exc:
                _raise_trim_control_error(exc)
            active = trim_plan.active_trim
            continuation_call = None
            if continuation_call_id is not None:
                continuation_call = state["calls"].get(continuation_call_id)
                if not continuation_call or continuation_call.get("kind") != "trimmer":
                    raise SchedulerError("trim continuation has the wrong call ID")
                if continuation_call.get("status") != CallState.COMPLETED.value:
                    raise WorkflowError("trim continuation call has no completed result")
                call_session = str(
                    continuation_call.get("continuation", {}).get("session_id") or ""
                )
                if call_session and call_session != str(active.get("session_id") or ""):
                    raise IdempotencyConflict(
                        "trim continuation call belongs to a different trim session"
                    )
            # The semantic caller supplies all relevant deltas; unrelated later
            # events do not block.  Record both cursors for audit.
            state["trim"] = copy.deepcopy(dict(trim_plan.trim))
            revision = trim_plan.revision
            try:
                sprint_integration_event = exploration_state.record_trim_integration(
                    state,
                    portfolio_revision=revision,
                    trim_session_id=active.get("session_id"),
                    trimmer_call_id=continuation_call_id,
                )
            except exploration_state.ExplorationConflictError as exc:
                raise IdempotencyConflict(str(exc)) from exc
            if sprint_integration_event is not None:
                append_event(
                    state,
                    "sprint_trim_continuation_committed",
                    sprint_integration_event,
                )
            _append_trim_effects(state, (trim_plan.effect,))
            if continuation_call is not None:
                _apply_call_event(
                    state,
                    continuation_call,
                    call_machine.CommitResult(),
                )
            self._transition_gate(state, GateState.OPEN)
            return revision

    def request_human_guidance(
        self,
        request_id: str,
        report_ref: str,
        question: str,
    ) -> None:
        with self._mutate() as state:
            exploration_state.request_human_guidance(
                state,
                request_id=request_id,
                report_ref=report_ref,
                question=question,
                event_cursor=self._event_cursor_of,
                transition_gate=self._transition_gate,
                append_event=append_event,
                stable_digest=stable_digest,
            )

    def resolve_human_guidance(
        self,
        request_id: str,
        *,
        response: str | None = None,
        cancelled: bool = False,
    ) -> None:
        if not cancelled and response is None:
            raise SchedulerError("guidance resolution requires response or cancelled=True")
        with self._mutate() as state:
            try:
                exploration_state.resolve_human_guidance(
                    state,
                    request_id=request_id,
                    response=response,
                    cancelled=cancelled,
                    event_cursor=self._event_cursor_of,
                    transition_gate=self._transition_gate,
                    append_event=append_event,
                )
            except exploration_state.ExplorationStateError as exc:
                raise SchedulerError(str(exc)) from exc

    # -------------------------------------------------------- discovery sprint

    @staticmethod
    def _sprint_slot_configuration_error(state: Mapping[str, Any]) -> str | None:
        return exploration_state.sprint_slot_configuration_error(state)

    def _waiting_sprint_launch_problem(
        self,
        state: Mapping[str, Any],
        sprint: Mapping[str, Any],
    ) -> tuple[str, str] | None:
        slot_error = self._sprint_slot_configuration_error(state)
        if slot_error is not None:
            return "invalid_four_slot_configuration", slot_error
        try:
            self._validate_sprint_target(sprint["plan"]["target_obligation"])
        except (KeyError, SchedulerError) as exc:
            return "stale_target", str(exc)
        return None

    def _mark_sprint_launch_needs_attention_locked(
        self,
        state: MutableMapping[str, Any],
        sprint_id: str,
        *,
        reason: str,
        error: str,
        batch_id: str | None = None,
    ) -> None:
        """Fence an unlaunched sprint whose durable launch preconditions changed."""
        try:
            exploration_state.mark_sprint_launch_needs_attention(
                state,
                sprint_id,
                reason=reason,
                error=error,
                batch_id=batch_id,
                now=utc_now,
                validate_reports=self._validate_sprint_reports_against_plan,
                stable_digest=stable_digest,
                transition_task=self._transition_task,
                add_attention=self._add_attention,
                append_event=append_event,
            )
        except exploration_state.ExplorationStateError as exc:
            raise SchedulerError(str(exc)) from exc

    def _canonical_sprint_snapshot(self, canonical_id: str | None) -> dict[str, Any] | None:
        try:
            return exploration_snapshots.canonical_sprint_snapshot(
                canonical_id,
                record_lookup=self.store.get if hasattr(self.store, "get") else None,
            )
        except exploration_snapshots.ExplorationSnapshotError as exc:
            raise SchedulerError(str(exc)) from exc

    @staticmethod
    def _sprint_operation_change_kind(operation: Mapping[str, Any]) -> str | None:
        return exploration_snapshots.sprint_operation_change_kind(operation)

    def _sprint_lane_result_locked(
        self,
        state: Mapping[str, Any],
        task_id: str,
    ) -> dict[str, Any]:
        try:
            return exploration_snapshots.sprint_lane_result(
                state,
                task_id,
                canonical_snapshot=self._canonical_sprint_snapshot,
                operation_body=_operation_body,
            )
        except exploration_snapshots.ExplorationSnapshotError as exc:
            raise SchedulerError(str(exc)) from exc

    def _freeze_sprint_input_locked(
        self,
        state: Mapping[str, Any],
        sprint: Mapping[str, Any],
        *,
        cancellation: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        return exploration_snapshots.freeze_sprint_input(
            state,
            sprint,
            lane_result=self._sprint_lane_result_locked,
            cancellation=cancellation,
        )

    def _attach_sprint_batch_locked(
        self,
        state: MutableMapping[str, Any],
        sprint_id: str,
        batch_id: str,
    ) -> list[str]:
        """Attach a complete four-task batch before any lane can be resumed."""
        try:
            return exploration_state.attach_sprint_batch(
                state,
                sprint_id,
                batch_id,
                validate_reports=self._validate_sprint_reports_against_plan,
                stable_digest=stable_digest,
                append_event=append_event,
            )
        except exploration_state.ExplorationStateError as exc:
            raise SchedulerError(str(exc)) from exc

    def _reconcile_sprint_launches_locked(
        self, state: MutableMapping[str, Any]
    ) -> None:
        """Repair the historical save boundary between a sprint batch and its owner."""

        for batch_id, batch in list(state.get("batches", {}).items()):
            sprint_id = batch.get("sprint_id")
            if not sprint_id:
                continue
            sprint = state.get("sprints", {}).get(sprint_id)
            if not sprint or sprint.get("status") not in {"waiting_for_slots", "running"}:
                continue
            if sprint.get("status") == "waiting_for_slots":
                problem = self._waiting_sprint_launch_problem(state, sprint)
                if problem is not None:
                    reason, error = problem
                    self._mark_sprint_launch_needs_attention_locked(
                        state,
                        str(sprint_id),
                        reason=reason,
                        error=error,
                        batch_id=str(batch_id),
                    )
                    continue
            self._attach_sprint_batch_locked(state, str(sprint_id), str(batch_id))

    def persist_sprint_plan(self, sprint_id: str, plan: Mapping[str, Any]) -> str:
        with self._mutate() as state:
            try:
                return exploration_state.persist_sprint_plan(
                    state,
                    sprint_id,
                    plan,
                    stable_digest=stable_digest,
                    validate_target=self._validate_sprint_target,
                    validate_lanes=self._validate_sprint_lane_blueprints,
                    append_event=append_event,
                )
            except exploration_state.ExplorationConflictError as exc:
                raise IdempotencyConflict(str(exc)) from exc
            except exploration_state.ExplorationCapacityError as exc:
                raise CapacityError(str(exc)) from exc
            except exploration_state.ExplorationStateError as exc:
                raise SchedulerError(str(exc)) from exc

    def preflight_sprint_launch(self, sprint_id: str) -> bool:
        """Validate a waiting sprint before task-writing serializes its lanes.

        ``launch_sprint`` repeats the same checks at the commit boundary.  This
        earlier sprint-only check prevents stale or unlaunchable plans from
        producing assignment artifacts that can never be consumed.
        """

        sprint = self._state["sprints"].get(sprint_id)
        if not sprint:
            raise SchedulerError(f"unknown sprint {sprint_id}")
        if sprint.get("status") == "needs_attention":
            return False
        if sprint.get("status") != "waiting_for_slots":
            raise WorkflowError(
                f"sprint cannot preflight from {sprint.get('status')}"
            )
        problem = self._waiting_sprint_launch_problem(self._state, sprint)
        if problem is not None:
            reason, error = problem
            with self._mutate() as state:
                self._mark_sprint_launch_needs_attention_locked(
                    state,
                    sprint_id,
                    reason=reason,
                    error=error,
                )
            return False
        return self.free_non_verifier_slots() == int(
            self._state["limits"]["max_non_verifier_workers"]
        )

    def launch_sprint(
        self,
        sprint_id: str,
        assign_reports: Sequence[Mapping[str, Any]],
        *,
        batch_id: str,
    ) -> list[str]:
        sprint = self._state["sprints"].get(sprint_id)
        if not sprint:
            raise SchedulerError(f"unknown sprint {sprint_id}")
        if sprint["status"] not in {"waiting_for_slots", "running"}:
            raise WorkflowError(f"sprint cannot launch from {sprint['status']}")
        if sprint["status"] == "waiting_for_slots":
            problem = self._waiting_sprint_launch_problem(self._state, sprint)
            if problem is not None:
                reason, error = problem
                with self._mutate() as state:
                    self._mark_sprint_launch_needs_attention_locked(
                        state,
                        sprint_id,
                        reason=reason,
                        error=error,
                    )
                if reason == "invalid_four_slot_configuration":
                    raise CapacityError(error)
                raise SchedulerError(error)
        if sprint["status"] == "waiting_for_slots" and self.free_non_verifier_slots() != int(
            self._state["limits"]["max_non_verifier_workers"]
        ):
            raise CapacityError("sprint waits until all four worker slots are free")
        sealed_reports = []
        for report in assign_reports:
            item = copy.deepcopy(dict(report))
            item["access_policy"] = {
                "canonical_memory": False,
                "internal_search": False,
                "sealed_workspace": True,
            }
            sealed_reports.append(item)
        # submit_batch creates the task cards, reserves all four slots, binds
        # their lane identities, and attaches the batch to the sprint in one
        # control-state transaction.
        try:
            return self.submit_batch(batch_id, sealed_reports, sprint_id=sprint_id)
        except SchedulerError:
            # Canonical memory has its own transaction boundary. If the target
            # changes between the preflight above and the atomic batch save,
            # persist the blocked launch instead of leaving an endless wait.
            current = self._state["sprints"].get(sprint_id)
            if current and current.get("status") == "waiting_for_slots":
                problem = self._waiting_sprint_launch_problem(self._state, current)
                if problem is not None:
                    reason, error = problem
                    with self._mutate() as state:
                        self._mark_sprint_launch_needs_attention_locked(
                            state,
                            sprint_id,
                            reason=reason,
                            error=error,
                        )
            raise

    def advance_sprint(self, sprint_id: str) -> str:
        with self._mutate() as state:
            try:
                return exploration_state.advance_sprint(
                    state,
                    sprint_id,
                    freeze_input=self._freeze_sprint_input_locked,
                    stable_digest=stable_digest,
                    append_event=append_event,
                )
            except exploration_state.ExplorationStateError as exc:
                raise SchedulerError(str(exc)) from exc

    def sprint_summary_input(self, sprint_id: str) -> dict[str, Any]:
        value = exploration_state.sprint_summary_input(self._state, sprint_id)
        value.update(self._advisor_problem_binding_locked(self._state))
        return value

    def prepare_sprint_summarizer_call(self, sprint_id: str) -> str:
        sprint = self._state["sprints"].get(sprint_id)
        if sprint and sprint.get("summarizer_call_id"):
            return str(sprint["summarizer_call_id"])
        return self._prepare_owned_call(
            "summarizer",
            self.sprint_summary_input(sprint_id),
            owner_collection="sprints",
            owner_id=sprint_id,
            pointer_field="summarizer_call_id",
            continuation={"sprint_id": sprint_id},
        )

    def commit_sprint_summarizer_call(self, call_id: str) -> None:
        call = self._state["calls"].get(call_id)
        if not call or call["kind"] != "summarizer":
            raise SchedulerError("unknown summarizer call")
        if call["status"] == CallState.COMMITTED.value:
            return
        if call["status"] != CallState.COMPLETED.value:
            raise WorkflowError("summarizer call has no completed result")
        sprint_id = str(call["continuation"]["sprint_id"])
        self.accept_sprint_summary(sprint_id, call["result"])
        self.mark_call_committed(call_id)

    def accept_sprint_summary(self, sprint_id: str, report: Mapping[str, Any]) -> None:
        with self._mutate() as state:
            try:
                exploration_state.accept_sprint_summary(
                    state,
                    sprint_id,
                    report,
                    stable_digest=stable_digest,
                    append_event=append_event,
                )
            except exploration_state.ExplorationConflictError as exc:
                raise IdempotencyConflict(str(exc)) from exc
            except exploration_state.ExplorationStateError as exc:
                raise SchedulerError(str(exc)) from exc

    def cancel_sprint(
        self,
        sprint_id: str,
        *,
        authorized_by: str,
        reason: str,
    ) -> str:
        """Cancel a safely idle sprint and continue trimming with its artifacts."""

        actor = str(authorized_by).strip()
        explanation = str(reason).strip()
        if not actor or not explanation:
            raise SchedulerError("sprint cancellation requires an actor and reason")

        with self._mutate() as state:
            try:
                plan = exploration_state.plan_sprint_cancellation(
                    state,
                    sprint_id,
                    authorized_by=actor,
                    reason=explanation,
                )
                for task_id in plan.task_ids_to_close:
                    task = state["tasks"][task_id]
                    if task["state"] == TaskState.LAUNCHING.value:
                        self._transition_task(state, task, TaskState.NEEDS_ATTENTION)
                    task["slot_reserved"] = False
                    task["launch_intent"] = False
                    task["sprint_launch_fenced"] = True
                    task["forced_outcome"] = TaskOutcome.INTERRUPTED.value
                    task["mechanical_summary"] = {
                        "kind": "sprint_cancellation",
                        "reason": plan.reason,
                        "authorized_by": plan.actor,
                    }
                if not plan.replay and plan.summarizer_call_id is not None:
                    call = state["calls"][plan.summarizer_call_id]
                    if plan.cancel_summarizer_call:
                        _apply_call_event(
                            state,
                            call,
                            call_machine.Cancel(
                                advance_epoch=True,
                                authorized_by=plan.actor,
                                reason=plan.reason,
                            ),
                        )
                    self._resolve_attention(
                        state, f"call:{plan.summarizer_call_id}"
                    )
                sprint = state["sprints"][sprint_id]
                frozen_input = None
                cancellation = None
                if not plan.replay:
                    cancellation = copy.deepcopy(dict(plan.cancellation or {}))
                    cancellation["cancelled_at"] = utc_now()
                if not plan.replay and sprint.get("frozen_synthesis_input") is None:
                    frozen_input = self._freeze_sprint_input_locked(
                        state,
                        sprint,
                        cancellation=cancellation,
                    )
                status, event_payload = exploration_state.commit_sprint_cancellation(
                    state,
                    plan,
                    frozen_input=frozen_input,
                    cancellation=cancellation,
                    stable_digest=stable_digest,
                )
                if event_payload is not None:
                    self._resolve_attention(state, f"sprint:{sprint_id}")
                    append_event(
                        state,
                        "sprint_cancelled",
                        event_payload,
                    )
            except exploration_state.ExplorationConflictError as exc:
                raise IdempotencyConflict(str(exc)) from exc
            except exploration_state.ExplorationStateError as exc:
                raise SchedulerError(str(exc)) from exc

        for task_id in plan.task_ids_to_close:
            self._maybe_close_task(task_id)
        return status

    def complete_sprint_continuation(self, sprint_id: str) -> None:
        with self._mutate() as state:
            exploration_state.complete_sprint_continuation(
                state,
                sprint_id,
                now=utc_now,
                append_event=append_event,
            )

    # -------------------------------------------------------- attention/replay

    @staticmethod
    def _add_attention(
        state: MutableMapping[str, Any],
        attention_id: str,
        reason: str,
        details: Mapping[str, Any],
        *,
        scope: str = "project",
        owner_id: str | None = None,
    ) -> None:
        if scope not in _ATTENTION_SCOPES:
            raise SchedulerError(f"unknown attention scope {scope!r}")
        if scope != "project" and not owner_id:
            raise SchedulerError(f"{scope} attention requires an owner ID")
        for item in state["needs_attention"]:
            if item["attention_id"] == attention_id and item.get("resolved_at") is None:
                return
        state["needs_attention"].append(
            {
                "attention_id": attention_id,
                "reason": reason,
                "details": copy.deepcopy(dict(details)),
                "scope": scope,
                "owner_id": owner_id,
                "created_at": utc_now(),
                "resolved_at": None,
            }
        )

    def has_blocking_attention(self) -> bool:
        """Return whether unresolved non-task attention must pause the runtime."""

        return any(
            item.get("resolved_at") is None and _attention_scope(item) != "task"
            for item in self._state.get("needs_attention", [])
        )

    def attention_is_task_local(self, attention_id: str) -> bool:
        return any(
            item.get("attention_id") == attention_id
            and item.get("resolved_at") is None
            and _attention_scope(item) == "task"
            for item in self._state.get("needs_attention", [])
        )

    @staticmethod
    def _resolve_attention(state: MutableMapping[str, Any], attention_id: str) -> None:
        for item in state["needs_attention"]:
            if item["attention_id"] == attention_id and item.get("resolved_at") is None:
                item["resolved_at"] = utc_now()

    def abandon_operation(self, operation_id: str, *, authorized_by: str, reason: str) -> None:
        task_id: str
        fact_proposal_id: str | None = None
        with self._mutate() as state:
            operation = state["operations"].get(operation_id)
            if not operation:
                raise SchedulerError(f"unknown operation {operation_id}")
            if operation["state"] in FINAL_OPERATION_STATES:
                return
            operation["state"] = OperationState.ABANDONED.value
            operation["abandonment"] = {"authorized_by": authorized_by, "reason": reason}
            if operation.get("kind") == "fact":
                fact_proposal_id = self._fact_proposal_id_of(operation) or None
            task_id = operation["task_id"]
            if operation_id in state["tasks"][task_id].get("completion_evidence_ids", []):
                task = state["tasks"][task_id]
                task["closure_review_required"] = True
                task["closure_review_reason"] = {
                    "kind": "completion_evidence_abandoned",
                    "operation_id": operation_id,
                    "authorized_by": authorized_by,
                    "reason": reason,
                }
                if task["state"] == TaskState.POSTPROCESSING.value:
                    self._transition_task(state, task, TaskState.NEEDS_ATTENTION)
                self._add_attention(
                    state,
                    f"task:{task_id}:closure",
                    "completion_evidence_abandoned",
                    {"operation_id": operation_id},
                    scope="task",
                    owner_id=task_id,
                )
            self._resolve_attention(state, f"operation:{operation_id}")
            append_event(
                state,
                "operation_abandoned",
                {"operation_id": operation_id, "authorized_by": authorized_by, "reason": reason},
            )
        if fact_proposal_id:
            self.reject_temporary_predecessor(
                fact_proposal_id,
                reason=f"Fact proposal abandoned by {authorized_by}: {reason}",
                predecessor_operation_id=operation_id,
            )
        self._maybe_close_task(task_id)

    def retry_attention_call(self, call_id: str) -> None:
        with self._mutate() as state:
            call = state["calls"].get(call_id)
            if not call or call["status"] != CallState.NEEDS_ATTENTION.value:
                raise WorkflowError("call is not in needs_attention")
            continuation = call.get("continuation")
            if not isinstance(continuation, Mapping):
                raise WorkflowError("attention call has no durable continuation owner")
            restored_operation_id: str | None = None
            restored_owner_id: str | None = None
            kind = str(call["kind"])
            if kind in {"synthesizer", "verifier"}:
                operation_id = continuation.get("operation_id")
                operation = state["operations"].get(operation_id)
                if not operation:
                    raise WorkflowError(
                        f"{kind} retry has no matching continuation operation"
                    )
                pointer_field = (
                    "synthesizer_call_id" if kind == "synthesizer" else "verifier_call_id"
                )
                if operation.get(pointer_field) != call_id:
                    raise WorkflowError(
                        f"{kind} retry does not match the operation's active call"
                    )
                if operation.get("state") != OperationState.NEEDS_ATTENTION.value:
                    raise WorkflowError(
                        f"{kind} retry operation is not in needs_attention"
                    )
                if kind == "verifier":
                    task = state["tasks"].get(operation.get("task_id"))
                    if not task or task.get("state") != TaskState.NEEDS_ATTENTION.value:
                        raise WorkflowError(
                            "verifier retry task is not paused in needs_attention"
                        )
                    operation["state"] = OperationState.VERIFYING.value
                else:
                    operation["state"] = OperationState.SYNTHESIZING.value
                restored_operation_id = str(operation_id)
                restored_owner_id = str(operation_id)
            elif kind == "summarizer":
                restored_owner_id = exploration_state.restore_summarizer_retry(
                    state,
                    call_id=call_id,
                    continuation=continuation,
                )
            elif kind == "challenge-verifier":
                challenge_id = str(continuation.get("challenge_id") or "")
                challenge = state["challenges"].get(challenge_id)
                if not challenge or challenge.get("verifier_call_id") != call_id:
                    raise WorkflowError(
                        "challenge-verifier retry does not match the challenge's active call"
                    )
                if challenge.get("state") != OperationState.NEEDS_ATTENTION.value:
                    raise WorkflowError(
                        "challenge-verifier retry challenge is not in needs_attention"
                    )
                challenge["state"] = OperationState.VERIFYING.value
                restored_owner_id = challenge_id
            _apply_call_event(state, call, call_machine.AuthorizeRetry())
            self._resolve_attention(state, f"call:{call_id}")
            state["halt_requested"] = _halt_required(state)
            append_event(
                state,
                "call_retry_authorized",
                {
                    "call_id": call_id,
                    "restored_operation_id": restored_operation_id,
                    "restored_owner_id": restored_owner_id,
                },
            )

    def recover(
        self,
        *,
        live_call_ids: Iterable[str] = (),
        live_task_ids: Iterable[str] = (),
    ) -> RecoveryPlan:
        with self._lock:
            revision, latest = self._repository.load()
            if latest is None:
                raise SchedulerError("cannot recover missing scheduler state")
            # Older builds saved the four task cards before attaching them to
            # their sprint.  Repair that durable boundary before recovery can
            # return any task launch intent.
            self._reconcile_sprint_launches_locked(latest)
            recovered, plan = reconcile_after_full_stop(
                latest,
                live_call_ids=live_call_ids,
                live_task_ids=live_task_ids,
            )
            recovered.setdefault("fact_proposals", {})
            self._ensure_fact_proposals_locked(recovered)
            for operation_id, operation in list(
                recovered.get("operations", {}).items()
            ):
                proposal_id = self._fact_proposal_id_of(operation)
                if (
                    operation.get("kind") == "fact"
                    and operation.get("state") == OperationState.REJECTED.value
                    and proposal_id
                    and self._operation_may_terminalize_fact_proposal(
                        recovered, operation
                    )
                ):
                    self._reject_fact_proposal_cascade_locked(
                        recovered,
                        proposal_id,
                        reason=str(operation.get("error") or "Fact proposal rejected"),
                        predecessor_operation_id=str(operation_id),
                    )
            self._cancel_stale_fact_repair_intents_locked(recovered)
            recovered["halt_requested"] = _halt_required(recovered)
            plan = RecoveryPlan(
                retry_call_ids=plan.retry_call_ids,
                relaunch_task_ids=tuple(
                    task_id
                    for task_id in plan.relaunch_task_ids
                    if recovered.get("tasks", {}).get(task_id, {}).get("state")
                    in {
                        TaskState.LAUNCHING.value,
                        TaskState.RETRY_PENDING.value,
                        TaskState.REVISION_PENDING.value,
                    }
                ),
                commit_call_ids=plan.commit_call_ids,
                needs_attention_ids=plan.needs_attention_ids,
            )
            new_revision = self._repository.save(revision, recovered)
            self._state = recovered
            self._revision = new_revision
            return plan

    def reconcile_pending_ingestion(self) -> dict[str, list[str]]:
        """Replay idempotent post-receipt work after a crash.

        Calls still requiring an agent are returned by
        :meth:`pending_recovery_work`; this method performs only deterministic
        routing and already-authorized canonical publications.
        """
        self._reconcile_continuation_call_owners()
        for staging_id, computation in list(self._state["computations"].items()):
            if computation["state"] == OperationState.RECEIVED.value:
                self._commit_computation(staging_id)
        for operation_id, operation in list(self._state["operations"].items()):
            state_value = operation["state"]
            if state_value == OperationState.RECEIVED.value:
                synthesis = operation.get("synthesizer_result") or {}
                if (
                    operation.get("kind") in {"route_add", "obligation_add"}
                    and synthesis.get("resolution") == "update"
                    and isinstance(synthesis.get("patch"), Mapping)
                ):
                    update_kind = (
                        "route_update"
                        if operation["kind"] == "route_add"
                        else "obligation_update"
                    )
                    self._commit_memory_operation(
                        operation_id,
                        operation_kind=update_kind,
                        payload=synthesis["patch"],
                    )
                else:
                    self._route_operation(operation_id)
            elif (
                state_value
                in {
                    OperationState.VERIFYING.value,
                    OperationState.NEEDS_ATTENTION.value,
                }
                and operation.get("verified_correct")
                and self._source_attempt_is_terminal_locked(
                    self._state, operation
                )
            ):
                self._publish_verified_fact(operation_id)
            elif state_value == OperationState.WAITING_PREDECESSORS.value:
                gate_ids = set(self._temporary_fact_predecessor_ids(operation))
                gate_ids.update(operation.get("body_temporary_fact_ids", []))
                for temporary_id in sorted(str(item) for item in gate_ids):
                    canonical_id = self._state["proposal_mappings"].get(str(temporary_id))
                    if canonical_id:
                        self.resolve_temporary_predecessor(str(temporary_id), canonical_id)
                    elif self._state.get("fact_proposals", {}).get(
                        str(temporary_id), {}
                    ).get("state") == "rejected":
                        self.reject_temporary_predecessor(
                            str(temporary_id),
                            reason=str(
                                self._state["fact_proposals"][str(temporary_id)].get(
                                    "terminal_reason"
                                )
                                or "Fact publication gate rejected"
                            ),
                            predecessor_operation_id=self._state[
                                "fact_proposals"
                            ][str(temporary_id)].get("terminal_operation_id"),
                        )
            elif (
                state_value == OperationState.REJECTED.value
                and operation.get("kind") == "fact"
                and self._operation_may_terminalize_fact_proposal(
                    self._state, operation
                )
            ):
                self.reject_temporary_predecessor(
                    str(operation["payload"]["proposal_id"]),
                    reason=str(operation.get("error") or "fact proposal rejected"),
                    predecessor_operation_id=operation_id,
                )
        self._reconcile_fact_proposal_rejection_tails()
        for operation_id in list(self._state["operations"]):
            self._reconcile_committed_update_resolution(operation_id)
        for challenge_id, challenge in list(self._state["challenges"].items()):
            tail = challenge.get("resolution_tail") or {}
            if challenge.get("resolution") is not None and tail.get("status") != "applied":
                self._apply_fact_challenge_resolution_tail(challenge_id)
        for sprint_id, sprint in list(self._state["sprints"].items()):
            integration = sprint.get("trim_integration") or {}
            if (
                sprint.get("status") == "trimmer_continuation"
                and integration.get("status") == "trim_committed"
            ):
                self.complete_sprint_continuation(sprint_id)
        for task_id, task in list(self._state["tasks"].items()):
            final_progress = self._state.get("progress", {}).get(
                task.get("last_progress_id"), {}
            )
            if final_progress.get("is_final"):
                self._terminalize_unpublishable_final_fact_gates(task_id)
        self._recheck_reference_blocked_tasks()
        for task_id, task in list(self._state["tasks"].items()):
            if task["state"] in {
                TaskState.POSTPROCESSING.value,
                TaskState.NEEDS_ATTENTION.value,
                TaskState.CLOSED.value,
            }:
                self._maybe_close_task(task_id)
            self._reconcile_task_soft_reference_finalization(task_id)
        self._repair_closed_task_publications()
        return self.pending_recovery_work()

    def _reconcile_continuation_call_owners(self) -> None:
        """Attach calls persisted by the pre-atomic continuation protocol."""

        specs = {
            "synthesizer": ("operations", "operation_id", "synthesizer_call_id"),
            "verifier": ("operations", "operation_id", "verifier_call_id"),
            "challenge-verifier": ("challenges", "challenge_id", "verifier_call_id"),
            "main-closure-review": ("tasks", "task_id", "closure_review_call_id"),
            "summarizer": ("sprints", "sprint_id", "summarizer_call_id"),
        }
        live_states = {
            CallState.PREPARED.value,
            CallState.RUNNING.value,
            CallState.RETRY_PENDING.value,
            CallState.COMPLETED.value,
            CallState.NEEDS_ATTENTION.value,
        }
        for call in list(self._state["calls"].values()):
            kind = str(call.get("kind") or "")
            spec = specs.get(kind)
            if spec is None or call.get("status") not in live_states:
                continue
            collection, continuation_key, pointer_field = spec
            continuation = call.get("continuation")
            if not isinstance(continuation, Mapping):
                continue
            owner_id = str(continuation.get(continuation_key) or "")
            owner = self._state.get(collection, {}).get(owner_id)
            if not owner or owner.get(pointer_field):
                continue
            owner_updates = None
            if kind == "synthesizer":
                owner_updates = {
                    "synthesizer_scope": copy.deepcopy(
                        call.get("input", {}).get("review_scope", [])
                    ),
                    "synthesizer_scope_digest": call.get("input", {}).get(
                        "review_scope_digest"
                    ),
                }
            self._prepare_owned_call(
                kind,
                call.get("input", {}),
                owner_collection=collection,
                owner_id=owner_id,
                pointer_field=pointer_field,
                continuation=continuation,
                owner_updates=owner_updates,
            )

    def pending_recovery_work(self) -> dict[str, list[str]]:
        """Return exact durable work that a terminal resume entry point relaunches."""
        calls = self._state["calls"]
        tasks = self._state["tasks"]
        return {
            "calls": sorted(
                call_id
                for call_id, call in calls.items()
                if call["status"] in {
                    CallState.PREPARED.value,
                    CallState.RETRY_PENDING.value,
                    CallState.COMPLETED.value,
                }
                and (
                    call.get("kind") != "verifier"
                    or self._source_attempt_is_terminal_locked(
                        self._state,
                        self._state.get("operations", {}).get(
                            str(
                                call.get("continuation", {}).get("operation_id")
                                or ""
                            )
                        ),
                    )
                )
            ),
            "tasks": sorted(
                task_id
                for task_id, task in tasks.items()
                if task["state"] in {
                    TaskState.LAUNCHING.value,
                    TaskState.RETRY_PENDING.value,
                    TaskState.REVISION_PENDING.value,
                }
            ),
            "operations": sorted(
                operation_id
                for operation_id, operation in self._state["operations"].items()
                if operation["state"] not in FINAL_OPERATION_STATES
                and operation["state"] != OperationState.NEEDS_ATTENTION.value
            ),
        }


__all__ = [
    "CapacityError",
    "IdempotencyConflict",
    "Scheduler",
    "SchedulerError",
    "StaleLeaseError",
    "TransportFailure",
    "RUNTIME_EVENT_LOOP_CONTRACT",
]
