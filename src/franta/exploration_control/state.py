"""Deterministic Block-10 state transitions.

The caller supplies gate/event/digest ports so transitions remain inside the
Scheduler's existing durable CAS transaction.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any, Callable, Mapping, MutableMapping, Sequence

from ..contracts.workflows import (
    CallState,
    GateState,
    TaskState,
    WorkflowError,
    validate_sprint_modes,
)
from .validation import SPRINT_LANE_LABELS


class ExplorationStateError(RuntimeError):
    """A Block-10 state reference or payload is invalid."""


class ExplorationConflictError(ExplorationStateError):
    """An idempotent Block-10 operation was replayed differently."""


class ExplorationCapacityError(ExplorationStateError):
    """A discovery sprint cannot satisfy its atomic four-slot contract."""


def sprint_slot_configuration_error(state: Mapping[str, Any]) -> str | None:
    configured = int(state["limits"]["max_non_verifier_workers"])
    if configured == 4:
        return None
    return (
        "discovery sprint requires exactly four configured non-verifier "
        f"worker slots; found {configured}"
    )


def persist_sprint_plan(
    state: MutableMapping[str, Any],
    sprint_id: str,
    plan: Mapping[str, Any],
    *,
    stable_digest: Callable[[Any], str],
    validate_target: Callable[[Mapping[str, Any]], str],
    validate_lanes: Callable[[str, list[Mapping[str, Any]]], None],
    append_event: Callable[[MutableMapping[str, Any], str, Mapping[str, Any]], int],
) -> str:
    exact = copy.deepcopy(dict(plan))
    digest = stable_digest(exact)
    if GateState(state["gate"]) != GateState.TRIMMING:
        raise WorkflowError("discovery sprint requires trimming gate")
    existing = state["sprints"].get(sprint_id)
    if existing:
        if existing["plan_digest"] != digest:
            raise ExplorationConflictError(
                "sprint ID replayed with a different plan"
            )
        return str(existing["status"])
    if state.get("active_sprint_id"):
        raise WorkflowError("another discovery sprint is already active")
    no_sprint = (
        exact.get("decision") == "no_sprint"
        or exact.get("resolution") == "no_sprint"
        or bool(exact.get("no_sprint"))
    )
    if no_sprint:
        if not str(exact.get("reason", "")).strip():
            raise ExplorationStateError("no_sprint requires a concise reason")
        state["sprints"][sprint_id] = {
            "sprint_id": sprint_id,
            "plan": exact,
            "plan_digest": digest,
            "status": "no_sprint",
        }
        append_event(state, "sprint_declined", {"sprint_id": sprint_id})
        return "no_sprint"
    if exact.get("decision") not in (None, "plan"):
        raise ExplorationStateError(
            "discovery-sprint decision must be no_sprint or plan"
        )
    slot_error = sprint_slot_configuration_error(state)
    if slot_error is not None:
        raise ExplorationCapacityError(slot_error)
    target = exact.get("target_obligation") or {}
    if not isinstance(target, Mapping):
        raise ExplorationStateError(
            "sprint plan requires exact target obligation id/revision/statement"
        )
    target_id = validate_target(target)
    lanes = list(exact.get("lanes", []))
    if len(lanes) != 4:
        raise ExplorationStateError("sprint plan requires four lane blueprints")
    if not all(isinstance(lane, Mapping) for lane in lanes):
        raise ExplorationStateError("every discovery-sprint lane must be an object")
    validate_lanes(target_id, lanes)
    state["sprints"][sprint_id] = {
        "sprint_id": sprint_id,
        "plan": exact,
        "plan_digest": digest,
        "status": "waiting_for_slots",
        "task_ids": [],
        "batch_id": None,
        "frozen_synthesis_input": None,
        "summary": None,
        "skip_synthesis_and_trim": False,
    }
    state["active_sprint_id"] = sprint_id
    append_event(state, "sprint_plan_persisted", {"sprint_id": sprint_id})
    return "waiting_for_slots"


def advance_sprint(
    state: MutableMapping[str, Any],
    sprint_id: str,
    *,
    freeze_input: Callable[[Mapping[str, Any], Mapping[str, Any]], dict[str, Any]],
    stable_digest: Callable[[Any], str],
    append_event: Callable[[MutableMapping[str, Any], str, Mapping[str, Any]], int],
) -> str:
    sprint = state["sprints"].get(sprint_id)
    if not sprint:
        raise ExplorationStateError(f"unknown sprint {sprint_id}")
    if sprint["status"] not in {"running", "draining_after_root_resolution"}:
        return str(sprint["status"])
    task_ids = sprint.get("task_ids", [])
    if not task_ids or any(
        state["tasks"][task_id]["state"] != TaskState.CLOSED.value
        for task_id in task_ids
    ):
        return str(sprint["status"])
    if sprint.get("skip_synthesis_and_trim") or GateState(state["gate"]) in {
        GateState.RESOLUTION_PENDING,
        GateState.COMPLETED,
    }:
        sprint["status"] = "closed_without_summary_after_root_resolution"
        state["active_sprint_id"] = None
        append_event(state, "sprint_drained_without_summary", {"sprint_id": sprint_id})
        return str(sprint["status"])
    frozen = freeze_input(state, sprint)
    sprint["frozen_synthesis_input"] = frozen
    sprint["frozen_synthesis_digest"] = stable_digest(frozen)
    sprint["status"] = "awaiting_summary"
    append_event(state, "sprint_barrier_opened", {"sprint_id": sprint_id})
    return str(sprint["status"])


def mark_summarizer_needs_attention(
    state: MutableMapping[str, Any], call: Mapping[str, Any]
) -> None:
    continuation = call.get("continuation", {})
    sprint_id = continuation.get("sprint_id")
    if call.get("kind") == "summarizer" and sprint_id in state["sprints"]:
        state["sprints"][sprint_id]["status"] = "needs_attention"


def record_trim_integration(
    state: MutableMapping[str, Any],
    *,
    portfolio_revision: int,
    trim_session_id: Any,
    trimmer_call_id: str | None,
) -> Mapping[str, Any] | None:
    """Record Block 9's committed trim as the Block-10 continuation tail."""

    sprint_id = state.get("active_sprint_id")
    if not sprint_id:
        return None
    sprint = state["sprints"].get(str(sprint_id))
    if not sprint or sprint.get("status") != "trimmer_continuation":
        return None
    if sprint.get("trim_integration") is not None:
        raise ExplorationConflictError(
            "sprint already has a different trim integration"
        )
    sprint["trim_integration"] = {
        "status": "trim_committed",
        "portfolio_revision": portfolio_revision,
        "trim_session_id": trim_session_id,
        "trimmer_call_id": trimmer_call_id,
    }
    return {
        "sprint_id": sprint_id,
        "portfolio_revision": portfolio_revision,
        "trimmer_call_id": trimmer_call_id,
    }


def restore_summarizer_retry(
    state: MutableMapping[str, Any],
    *,
    call_id: str,
    continuation: Mapping[str, Any],
) -> str:
    sprint_id = str(continuation.get("sprint_id") or "")
    sprint = state["sprints"].get(sprint_id)
    if not sprint or sprint.get("summarizer_call_id") != call_id:
        raise WorkflowError(
            "summarizer retry does not match the sprint's active call"
        )
    if sprint.get("status") != "needs_attention":
        raise WorkflowError("summarizer retry sprint is not in needs_attention")
    sprint["status"] = "awaiting_summary"
    return sprint_id


def supersede_for_root_resolution(
    state: MutableMapping[str, Any],
    *,
    cancel_call: Callable[[MutableMapping[str, Any]], None],
) -> None:
    guidance = state["guidance"].get("active")
    if guidance:
        guidance["status"] = "cancelled_by_root_resolution"
        state["guidance"]["history"].append(guidance)
        state["guidance"]["active"] = None
    sprint_id = state.get("active_sprint_id")
    if sprint_id:
        sprint = state["sprints"][sprint_id]
        if sprint["status"] in {"planned", "waiting_for_slots"}:
            sprint["status"] = "cancelled_by_root_resolution"
            state["active_sprint_id"] = None
        else:
            sprint["status"] = "draining_after_root_resolution"
            sprint["skip_synthesis_and_trim"] = True
    for call in state["calls"].values():
        if call["kind"] in {"trimmer", "summarizer"} and call["status"] in {
            CallState.PREPARED.value,
            CallState.RETRY_PENDING.value,
        }:
            cancel_call(call)


def attach_sprint_batch(
    state: MutableMapping[str, Any],
    sprint_id: str,
    batch_id: str,
    *,
    validate_reports: Callable[
        [Mapping[str, Any], Sequence[Mapping[str, Any]]], None
    ],
    stable_digest: Callable[[Any], str],
    append_event: Callable[[MutableMapping[str, Any], str, Mapping[str, Any]], int],
) -> list[str]:
    sprint = state["sprints"].get(sprint_id)
    batch = state["batches"].get(batch_id)
    if not sprint or not batch or batch.get("sprint_id") != sprint_id:
        raise ExplorationStateError(
            "discovery-sprint launch has no matching persisted batch"
        )
    task_ids = list(batch.get("task_ids", []))
    reports = list(batch.get("reports", []))
    if len(task_ids) != 4 or len(reports) != 4:
        raise ExplorationStateError(
            "discovery-sprint launch requires one complete four-task batch"
        )
    validate_sprint_modes(
        str(report.get("mode", report.get("work_mode", "")))
        for report in reports
    )
    validate_reports(sprint["plan"], reports)
    for index, task_id in enumerate(task_ids):
        task = state["tasks"].get(task_id)
        if (
            not task
            or task.get("batch_id") != batch_id
            or task.get("sprint_id") != sprint_id
        ):
            raise ExplorationStateError(
                f"discovery-sprint batch has an invalid lane task: {task_id}"
            )
        lane = SPRINT_LANE_LABELS[index]
        existing_lane = task.get("sprint_lane")
        if existing_lane is not None and existing_lane != lane:
            raise ExplorationStateError(
                f"discovery-sprint task {task_id} has conflicting lane identity"
            )
        task["sprint_lane"] = lane
        card = task.get("task_card")
        if not isinstance(card, MutableMapping):
            raise ExplorationStateError(
                f"discovery-sprint task {task_id} has no task card"
            )
        card["sprint_lane"] = lane
        task["task_card_digest"] = stable_digest(card)
    already_attached = (
        sprint.get("status") == "running"
        and sprint.get("batch_id") == batch_id
        and list(sprint.get("task_ids", [])) == task_ids
    )
    if sprint.get("status") not in {"waiting_for_slots", "running"}:
        raise WorkflowError(f"sprint cannot attach from {sprint.get('status')}")
    sprint["status"] = "running"
    sprint["batch_id"] = batch_id
    sprint["task_ids"] = task_ids
    if not already_attached:
        append_event(
            state,
            "sprint_lanes_launched",
            {
                "sprint_id": sprint_id,
                "task_ids": task_ids,
                "lanes": {
                    SPRINT_LANE_LABELS[index]: task_id
                    for index, task_id in enumerate(task_ids)
                },
            },
        )
    return task_ids


def mark_sprint_launch_needs_attention(
    state: MutableMapping[str, Any],
    sprint_id: str,
    *,
    reason: str,
    error: str,
    batch_id: str | None,
    now: Callable[[], str],
    validate_reports: Callable[
        [Mapping[str, Any], Sequence[Mapping[str, Any]]], None
    ],
    stable_digest: Callable[[Any], str],
    transition_task: Callable[
        [MutableMapping[str, Any], MutableMapping[str, Any], TaskState], None
    ],
    add_attention: Callable[
        [MutableMapping[str, Any], str, str, Mapping[str, Any]], None
    ],
    append_event: Callable[[MutableMapping[str, Any], str, Mapping[str, Any]], int],
) -> None:
    sprint = state["sprints"].get(sprint_id)
    if not sprint:
        raise ExplorationStateError(f"unknown sprint {sprint_id}")
    attention = {"reason": reason, "error": error, "detected_at": now()}
    prior = sprint.get("launch_attention")
    if sprint.get("status") == "needs_attention" and isinstance(prior, Mapping):
        return
    if sprint.get("status") != "waiting_for_slots":
        raise WorkflowError(
            f"sprint launch cannot be paused from {sprint.get('status')}"
        )

    if batch_id is not None:
        batch = state.get("batches", {}).get(batch_id)
        if not batch or batch.get("sprint_id") != sprint_id:
            raise ExplorationStateError(
                "discovery-sprint launch attention has no matching batch"
            )
        task_ids = list(batch.get("task_ids", []))
        reports = list(batch.get("reports", []))
        if len(task_ids) != 4 or len(reports) != 4:
            raise ExplorationStateError(
                "discovery-sprint launch attention requires a complete four-task batch"
            )
        validate_sprint_modes(
            str(report.get("mode", report.get("work_mode", "")))
            for report in reports
        )
        validate_reports(sprint["plan"], reports)
        for index, task_id in enumerate(task_ids):
            task = state["tasks"].get(task_id)
            if (
                not task
                or task.get("batch_id") != batch_id
                or task.get("sprint_id") != sprint_id
                or task.get("attempts")
                or task.get("state")
                not in {
                    TaskState.LAUNCHING.value,
                    TaskState.NEEDS_ATTENTION.value,
                }
            ):
                raise WorkflowError(
                    "a discovery-sprint lane already started and cannot be fenced "
                    "as an unlaunched stale-target batch"
                )
            lane = SPRINT_LANE_LABELS[index]
            task["sprint_lane"] = lane
            card = task.get("task_card")
            if not isinstance(card, MutableMapping):
                raise ExplorationStateError(
                    f"discovery-sprint task {task_id} has no task card"
                )
            card["sprint_lane"] = lane
            task["task_card_digest"] = stable_digest(card)
            if task["state"] == TaskState.LAUNCHING.value:
                transition_task(state, task, TaskState.NEEDS_ATTENTION)
            task["slot_reserved"] = False
            task["launch_intent"] = False
            task["sprint_launch_fenced"] = True
        sprint["batch_id"] = batch_id
        sprint["task_ids"] = task_ids

    sprint["status"] = "needs_attention"
    sprint["launch_attention"] = attention
    add_attention(
        state,
        f"sprint:{sprint_id}",
        "discovery_sprint_launch_blocked",
        {"reason": reason, "error": error},
    )
    append_event(
        state,
        "sprint_launch_needs_attention",
        {"sprint_id": sprint_id, "reason": reason, "error": error},
    )


@dataclass(frozen=True)
class SprintCancellationPlan:
    sprint_id: str
    actor: str
    reason: str
    prior_status: str
    result_status: str
    replay: bool
    task_ids_to_close: tuple[str, ...]
    summarizer_call_id: str | None
    cancel_summarizer_call: bool
    cancellation: Mapping[str, Any] | None


def plan_sprint_cancellation(
    state: Mapping[str, Any],
    sprint_id: str,
    *,
    authorized_by: str,
    reason: str,
) -> SprintCancellationPlan:
    actor = str(authorized_by).strip()
    explanation = str(reason).strip()
    if not actor or not explanation:
        raise ExplorationStateError(
            "sprint cancellation requires an actor and reason"
        )
    sprint = state["sprints"].get(sprint_id)
    if not sprint:
        raise ExplorationStateError(f"unknown sprint {sprint_id}")
    prior = sprint.get("cancellation")
    if isinstance(prior, Mapping):
        if (
            prior.get("authorized_by") != actor
            or prior.get("reason") != explanation
        ):
            raise ExplorationConflictError(
                "sprint cancellation was replayed with different authorization"
            )
        return SprintCancellationPlan(
            sprint_id=sprint_id,
            actor=actor,
            reason=explanation,
            prior_status=str(prior.get("prior_status") or ""),
            result_status=str(
                prior.get("result_status") or "trimmer_continuation"
            ),
            replay=True,
            task_ids_to_close=(),
            summarizer_call_id=(
                str(sprint["summarizer_call_id"])
                if sprint.get("summarizer_call_id")
                else None
            ),
            cancel_summarizer_call=False,
            cancellation=copy.deepcopy(dict(prior)),
        )

    prior_status = str(sprint.get("status") or "")
    if prior_status not in {"waiting_for_slots", "needs_attention"}:
        raise WorkflowError(f"sprint cannot be cancelled safely from {prior_status}")

    tasks_to_close: list[str] = []
    for raw_task_id in sprint.get("task_ids", []):
        task_id = str(raw_task_id)
        task = state["tasks"].get(task_id)
        if not task:
            raise ExplorationStateError(
                f"sprint cancellation references unknown task {task_id}"
            )
        if task.get("state") == TaskState.CLOSED.value:
            continue
        safe_unlaunched = (
            task.get("state")
            in {TaskState.LAUNCHING.value, TaskState.NEEDS_ATTENTION.value}
            and not task.get("attempts")
            and not task.get("operation_ids")
            and not task.get("computation_ids")
            and not task.get("challenge_ids")
        )
        if not safe_unlaunched:
            raise WorkflowError(
                "sprint cancellation is allowed only when no lane process is running"
            )
        tasks_to_close.append(task_id)

    summarizer_call_id = sprint.get("summarizer_call_id")
    cancel_summarizer_call = False
    if summarizer_call_id:
        call = state["calls"].get(str(summarizer_call_id))
        if (
            not call
            or call.get("kind") != "summarizer"
            or call.get("continuation", {}).get("sprint_id") != sprint_id
        ):
            raise ExplorationStateError(
                "sprint cancellation has an invalid summarizer call"
            )
        if call.get("status") in {
            CallState.COMPLETED.value,
            CallState.COMMITTED.value,
        }:
            raise WorkflowError(
                "a completed sprint summary cannot be discarded by cancellation"
            )
        cancel_summarizer_call = call.get("status") != CallState.CANCELLED.value

    cancellation = {
        "authorized_by": actor,
        "reason": explanation,
        "prior_status": prior_status,
        "result_status": "trimmer_continuation",
        "available_task_ids": list(sprint.get("task_ids", [])),
    }
    return SprintCancellationPlan(
        sprint_id=sprint_id,
        actor=actor,
        reason=explanation,
        prior_status=prior_status,
        result_status="trimmer_continuation",
        replay=False,
        task_ids_to_close=tuple(tasks_to_close),
        summarizer_call_id=(
            str(summarizer_call_id) if summarizer_call_id else None
        ),
        cancel_summarizer_call=cancel_summarizer_call,
        cancellation=copy.deepcopy(cancellation),
    )


def commit_sprint_cancellation(
    state: MutableMapping[str, Any],
    plan: SprintCancellationPlan,
    *,
    frozen_input: Mapping[str, Any] | None,
    cancellation: Mapping[str, Any] | None,
    stable_digest: Callable[[Any], str],
) -> tuple[str, Mapping[str, Any] | None]:
    if plan.replay:
        return plan.result_status, None
    sprint = state["sprints"][plan.sprint_id]
    if sprint.get("frozen_synthesis_input") is None:
        if frozen_input is None:
            raise ExplorationStateError(
                "sprint cancellation requires its frozen synthesis input"
            )
        frozen = copy.deepcopy(dict(frozen_input))
        sprint["frozen_synthesis_input"] = frozen
        sprint["frozen_synthesis_digest"] = stable_digest(frozen)
    exact_cancellation = copy.deepcopy(dict(cancellation or {}))
    sprint["cancellation"] = exact_cancellation
    sprint["summary_skipped_by_cancellation"] = True
    sprint["status"] = "trimmer_continuation"
    return plan.result_status, {
        "sprint_id": plan.sprint_id,
        "authorized_by": plan.actor,
        "reason": plan.reason,
        "prior_status": plan.prior_status,
        "frozen_synthesis_digest": sprint.get("frozen_synthesis_digest"),
    }


def request_human_guidance(
    state: MutableMapping[str, Any],
    *,
    request_id: str,
    report_ref: str,
    question: str,
    event_cursor: Callable[[Mapping[str, Any]], int],
    transition_gate: Callable[[MutableMapping[str, Any], GateState], None],
    append_event: Callable[[MutableMapping[str, Any], str, Mapping[str, Any]], int],
    stable_digest: Callable[[Any], str],
) -> None:
    payload = {
        "request_id": request_id,
        "report_ref": report_ref,
        "question": question,
    }
    if GateState(state["gate"]) != GateState.TRIMMING:
        raise WorkflowError("human guidance may only be requested during trimming")
    active = state["guidance"].get("active")
    if active:
        if active["request_id"] == request_id and stable_digest(
            active["input"]
        ) == stable_digest(payload):
            return
        raise WorkflowError("another human-guidance request is already active")
    state["guidance"]["active"] = {
        "request_id": request_id,
        "input": payload,
        "status": "waiting",
        "snapshot_event_id": event_cursor(state),
    }
    transition_gate(state, GateState.WAITING_FOR_HUMAN)
    append_event(state, "human_guidance_requested", {"request_id": request_id})


def resolve_human_guidance(
    state: MutableMapping[str, Any],
    *,
    request_id: str,
    response: str | None,
    cancelled: bool,
    event_cursor: Callable[[Mapping[str, Any]], int],
    transition_gate: Callable[[MutableMapping[str, Any], GateState], None],
    append_event: Callable[[MutableMapping[str, Any], str, Mapping[str, Any]], int],
) -> None:
    if GateState(state["gate"]) != GateState.WAITING_FOR_HUMAN:
        raise WorkflowError("scheduler is not waiting for human guidance")
    active = state["guidance"].get("active")
    if not active or active["request_id"] != request_id:
        raise ExplorationStateError("unknown active guidance request")
    active["status"] = "cancelled" if cancelled else "answered"
    active["response"] = response
    active["resolved_event_id"] = event_cursor(state) + 1
    state["guidance"]["history"].append(active)
    state["guidance"]["active"] = None
    transition_gate(state, GateState.TRIMMING)
    append_event(
        state,
        "human_guidance_resolved",
        {"request_id": request_id, "cancelled": cancelled},
    )


def sprint_summary_input(
    state: Mapping[str, Any], sprint_id: str
) -> dict[str, Any]:
    sprint = state["sprints"].get(sprint_id)
    if not sprint or sprint["status"] != "awaiting_summary":
        raise WorkflowError("sprint is not awaiting a summary")
    return copy.deepcopy(sprint["frozen_synthesis_input"])


def accept_sprint_summary(
    state: MutableMapping[str, Any],
    sprint_id: str,
    report: Mapping[str, Any],
    *,
    stable_digest: Callable[[Any], str],
    append_event: Callable[[MutableMapping[str, Any], str, Mapping[str, Any]], int],
) -> None:
    exact = copy.deepcopy(dict(report))
    digest = stable_digest(exact)
    required = {
        "mechanism_fingerprints",
        "differences",
        "shared_bottlenecks",
        "bridges",
        "negative_results",
        "follow_up_tasks",
    }
    missing = sorted(required - set(exact))
    if missing:
        raise ExplorationStateError(
            f"invalid sprint synthesis report; missing {missing}"
        )
    sprint = state["sprints"].get(sprint_id)
    if not sprint:
        raise ExplorationStateError(f"unknown sprint {sprint_id}")
    prior_digest = sprint.get("summary_digest")
    if prior_digest is not None:
        if prior_digest != digest:
            raise ExplorationConflictError(
                "sprint summary was replayed with a different result"
            )
        if sprint.get("summary") != exact:
            raise ExplorationConflictError(
                "sprint summary digest does not match its durable result"
            )
        if sprint["status"] not in {"trimmer_continuation", "integrated"}:
            raise WorkflowError(
                "durable sprint summary has an inconsistent continuation state"
            )
        return
    if sprint["status"] != "awaiting_summary":
        raise WorkflowError("sprint is not awaiting a summary")
    sprint["summary"] = exact
    sprint["summary_digest"] = digest
    sprint["status"] = "trimmer_continuation"
    append_event(
        state,
        "sprint_summary_accepted",
        {"sprint_id": sprint_id, "summary_digest": digest},
    )


def complete_sprint_continuation(
    state: MutableMapping[str, Any],
    sprint_id: str,
    *,
    now: Callable[[], str],
    append_event: Callable[[MutableMapping[str, Any], str, Mapping[str, Any]], int],
) -> None:
    sprint = state["sprints"].get(sprint_id)
    if sprint and sprint.get("status") == "integrated":
        return
    if not sprint or sprint.get("status") != "trimmer_continuation":
        raise WorkflowError("sprint has no active trimmer continuation")
    integration = sprint.get("trim_integration")
    if not isinstance(integration, MutableMapping) or integration.get("status") != (
        "trim_committed"
    ):
        raise WorkflowError("sprint trimmer continuation has not committed its trim")
    sprint["status"] = "integrated"
    integration["status"] = "integrated"
    integration["integrated_at"] = now()
    if state.get("active_sprint_id") == sprint_id:
        state["active_sprint_id"] = None
    append_event(state, "sprint_integrated", {"sprint_id": sprint_id})
