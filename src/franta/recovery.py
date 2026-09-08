"""Persistence helpers and deterministic full-process recovery planning."""

from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, MutableMapping, Protocol

from .execution_gateway import call_state as call_machine
from .contracts.workflows import (
    GateState,
    RetryPolicy,
    SchedulerLimits,
    TaskOutcome,
    TaskState,
)


class ControlStateConflict(RuntimeError):
    """Another scheduler committed a different control-state revision."""


class ControlStateStore(Protocol):
    """Minimal scheduler-private API supplied by :mod:`franta.store`."""

    def load_control_state(self, key: str) -> Any: ...

    def compare_and_swap_control_state(
        self, key: str, expected_revision: int | None, payload: Mapping[str, Any]
    ) -> int: ...


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def stable_digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def preserved_attempt_supplement(value: Any) -> dict[str, Any] | None:
    """Return the semantic supplement carried through infrastructure retries.

    Infrastructure failure is orthogonal to verifier feedback, concession
    requirements, and dependency repair.  Retry wrappers therefore retain the
    original immutable supplement instead of replacing it.
    """

    if not isinstance(value, Mapping):
        return None
    current = copy.deepcopy(dict(value))
    while current.get("kind") == "infrastructure_retry":
        nested = current.get("preserved_supplement")
        if not isinstance(nested, Mapping):
            return None
        current = copy.deepcopy(dict(nested))
    return current


def infrastructure_retry_supplement(
    reason: str,
    prior: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Wrap a retry reason without discarding the attempt's semantic input."""

    result: dict[str, Any] = {
        "kind": "infrastructure_retry",
        "reason": str(reason),
    }
    semantic = preserved_attempt_supplement(prior)
    if semantic is not None:
        result["preserved_supplement"] = semantic
    return result


def _unpack_loaded(value: Any) -> tuple[int, dict[str, Any]] | None:
    if value is None:
        return None
    if isinstance(value, tuple) and len(value) == 2:
        revision, payload = value
        return int(revision), copy.deepcopy(dict(payload))
    if isinstance(value, Mapping):
        if "revision" in value and "payload" in value:
            return int(value["revision"]), copy.deepcopy(dict(value["payload"]))
        # Some stores expose the payload itself and put its CAS revision inside
        # a private envelope.  Accepting this shape is lossless.
        if "_control_revision" in value:
            payload = dict(value)
            revision = int(payload.pop("_control_revision"))
            return revision, copy.deepcopy(payload)
    revision = getattr(value, "revision", None)
    payload = getattr(value, "payload", None)
    if revision is not None and payload is not None:
        return int(revision), copy.deepcopy(dict(payload))
    raise TypeError("load_control_state must return None, (revision, payload), or an envelope")


class ControlRepository:
    """Exact JSON snapshot persistence with compare-and-swap revisions."""

    def __init__(self, store: ControlStateStore, key: str = "scheduler") -> None:
        if not hasattr(store, "load_control_state") or not hasattr(
            store, "compare_and_swap_control_state"
        ):
            raise TypeError(
                "Store must provide load_control_state and compare_and_swap_control_state"
            )
        self.store = store
        self.key = key

    def load(self) -> tuple[int | None, dict[str, Any] | None]:
        loaded = _unpack_loaded(self.store.load_control_state(self.key))
        if loaded is None:
            return None, None
        return loaded

    def save(self, expected_revision: int | None, payload: Mapping[str, Any]) -> int:
        # JSON round-tripping here prevents dataclasses, enums, or object
        # identities from accidentally becoming authoritative state.
        exact_payload = json.loads(canonical_json(payload))
        try:
            return int(
                self.store.compare_and_swap_control_state(
                    self.key, expected_revision, exact_payload
                )
            )
        except Exception as exc:
            name = type(exc).__name__.lower()
            if "conflict" in name or "revision" in name or "compare" in name:
                raise ControlStateConflict(str(exc)) from exc
            raise


def default_scheduler_state(
    *,
    retry_policy: RetryPolicy | None = None,
    limits: SchedulerLimits | None = None,
) -> dict[str, Any]:
    retries = retry_policy or RetryPolicy()
    scheduler_limits = limits or SchedulerLimits()
    return {
        "schema_version": 1,
        "bootstrapped": False,
        "halt_requested": False,
        # Runtime opens this gate deterministically once bootstrap is stable;
        # no category portfolio or trimmer call is required.
        "gate": GateState.TRIMMING.value,
        "root": {
            "problem": None,
            "obligation_id": None,
            "obligation_status": "active",
            "solution_fact_id": None,
            "outcome": None,
            "alternates": [],
            "conflicts": [],
        },
        "retry_policy": asdict(retries),
        "limits": asdict(scheduler_limits),
        "ids": {},
        "events": [],
        "calls": {},
        "tasks": {},
        "batches": {},
        "progress": {},
        "operations": {},
        "operation_groups": {},
        "proposal_mappings": {},
        "fact_proposals": {},
        "fact_lineages": {},
        "rejected_verification_bundles": {},
        "worker_session_lineages": {},
        "computations": {},
        "challenges": {},
        "revocation_applications": {},
        "trim": {
            "assignment_reports": [],
            "round": 0,
            "session_id": None,
            "session_rounds": 0,
            "active_review": None,
            "active_trim": None,
            "portfolio": None,
            "portfolio_revision": 0,
        },
        "guidance": {"active": None, "history": []},
        "sprints": {},
        "active_sprint_id": None,
        "proof_writer_task_id": None,
        "main_checkpoint": {"event_cursor": 0},
        "needs_attention": [],
    }


def append_event(
    state: MutableMapping[str, Any], event_type: str, payload: Mapping[str, Any] | None = None
) -> int:
    events = state.setdefault("events", [])
    event_id = (int(events[-1]["event_id"]) + 1) if events else 1
    events.append(
        {
            "event_id": event_id,
            "type": event_type,
            "time": utc_now(),
            "payload": copy.deepcopy(dict(payload or {})),
        }
    )
    return event_id


@dataclass(frozen=True)
class RecoveryPlan:
    retry_call_ids: tuple[str, ...]
    relaunch_task_ids: tuple[str, ...]
    commit_call_ids: tuple[str, ...]
    needs_attention_ids: tuple[str, ...]


def reconcile_after_full_stop(
    original: Mapping[str, Any],
    *,
    live_call_ids: Iterable[str] = (),
    live_task_ids: Iterable[str] = (),
) -> tuple[dict[str, Any], RecoveryPlan]:
    """Fence lost leases and return a deterministic restart plan.

    No gate state is opened or otherwise reconstructed here.  In particular,
    reviewing/trimming/guidance/resolution/completed survive a full stop.
    """

    state = copy.deepcopy(dict(original))
    live_calls = set(live_call_ids)
    live_tasks = set(live_task_ids)
    retry_calls: list[str] = []
    relaunch_tasks: list[str] = []
    commit_calls: list[str] = []
    attention: list[str] = []

    for call_id, call in state.get("calls", {}).items():
        transition = call_machine.reduce_call(
            call,
            call_machine.ReconcileAfterStop(live=call_id in live_calls),
        )
        if transition.changed:
            call.clear()
            call.update(copy.deepcopy(dict(transition.call)))
        if transition.recovery_directive == "commit":
            commit_calls.append(call_id)
        elif transition.recovery_directive == "retry":
            retry_calls.append(call_id)
        elif transition.recovery_directive == "attention":
            attention.append(call_id)
        for effect in transition.effects:
            append_event(state, effect.event_type, effect.payload)

    worker_limit = int(state.get("retry_policy", {}).get("worker", 3))
    for task_id, task in state.get("tasks", {}).items():
        status = task.get("state")
        if task.get("agent_system") == "franta-sort":
            # The one-shot sort call owns its own launch-bound retry and
            # frozen Explorer validation.  Its logical call was fenced above;
            # leave the task/attempt running so FrantaRuntime can resume that
            # same call through the dedicated sort barrier.  Treating it as an
            # ordinary lost worker would create a new task attempt and bypass
            # the sorter recovery contract.
            continue
        if status == TaskState.STOPPING.value:
            if task_id in live_tasks:
                continue
            attempts = task.setdefault("attempts", [])
            if attempts and attempts[-1].get("state") == "running":
                attempts[-1]["state"] = "stopped"
                attempts[-1]["ended_reason"] = "lost_after_scheduler_stop_request"
            task["state"] = TaskState.POSTPROCESSING.value
            task["slot_reserved"] = False
            task["forced_outcome"] = TaskOutcome.INTERRUPTED.value
            task["mechanical_summary"] = {
                "kind": "scheduler_stop",
                "reason": "stopped assignment did not survive full-process restart",
                "last_progress_id": task.get("last_progress_id"),
            }
            append_event(state, "stopped_worker_reconciled", {"task_id": task_id})
            continue
        if status not in {TaskState.LAUNCHING.value, TaskState.RUNNING.value}:
            if status == TaskState.RETRY_PENDING.value:
                relaunch_tasks.append(task_id)
            continue
        if task_id in live_tasks:
            continue
        attempts = task.setdefault("attempts", [])
        if attempts and attempts[-1].get("state") == "running":
            attempts[-1]["state"] = "interrupted"
            attempts[-1]["ended_reason"] = "process_lost_during_full_stop"
        retry_count = int(task.get("interruption_retry_count", 0))
        if retry_count >= worker_limit:
            # Recovery must take the same postprocessing path as a live retry
            # exhaustion.  Pending synthesizer/verifier/reference work drains
            # before the canonical task record is published.
            task["state"] = TaskState.POSTPROCESSING.value
            task["slot_reserved"] = False
            task["forced_outcome"] = TaskOutcome.INTERRUPTED.value
            task["mechanical_summary"] = {
                "kind": "mechanical_interruption",
                "reason": "worker retry limit exhausted during recovery",
                "last_progress_id": task.get("last_progress_id"),
                "retry_count": retry_count,
            }
        else:
            task["state"] = TaskState.RETRY_PENDING.value
            task["interruption_retry_count"] = retry_count + 1
            prior_supplement = attempts[-1].get("supplement") if attempts else None
            task["pending_attempt_supplement"] = infrastructure_retry_supplement(
                "process_lost_during_full_stop",
                prior_supplement,
            )
            relaunch_tasks.append(task_id)
        append_event(state, "lost_worker_reconciled", {"task_id": task_id})

    # Task records are authoritative for which member of a worker session
    # lineage is still active.  Reconcile the derived pointer after recovery,
    # including retry-exhausted tasks that were closed above.
    lineages = state.setdefault("worker_session_lineages", {})
    tasks_by_lineage: dict[str, list[tuple[str, Mapping[str, Any]]]] = {}
    for task_id, task in state.get("tasks", {}).items():
        lineage_id = task.get("session_lineage_id")
        if lineage_id:
            tasks_by_lineage.setdefault(str(lineage_id), []).append((str(task_id), task))
    for lineage_id, members in tasks_by_lineage.items():
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
        entry["task_ids"] = sorted(task_id for task_id, _task in members)
        entry["explicit_resume_launches"] = sum(
            1
            for _task_id, task in members
            if (task.get("task_card") or {}).get("if_resume")
            or (task.get("assign_record") or {}).get("if_resume")
        )
        active = [
            task_id
            for task_id, task in members
            if task.get("state") != TaskState.CLOSED.value
        ]
        entry["active_task_id"] = active[0] if len(active) == 1 else None

    plan = RecoveryPlan(
        retry_call_ids=tuple(sorted(set(retry_calls))),
        relaunch_task_ids=tuple(sorted(set(relaunch_tasks))),
        commit_call_ids=tuple(sorted(set(commit_calls))),
        needs_attention_ids=tuple(sorted(set(attention))),
    )
    return state, plan
