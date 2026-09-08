"""Pure runtime selection helpers for persisted discovery sprints."""

from __future__ import annotations

import copy
from typing import Any, Mapping

from ..contracts.workflows import TaskState


class ExplorationRuntimeError(RuntimeError):
    pass


def waiting_sprint_predecessor_task_ids(
    state: Mapping[str, Any], sprint_id: str
) -> list[str]:
    plan_event_id = next(
        (
            int(event["event_id"])
            for event in state["events"]
            if event.get("type") == "sprint_plan_persisted"
            and event.get("payload", {}).get("sprint_id") == sprint_id
        ),
        None,
    )
    if plan_event_id is None:
        raise ExplorationRuntimeError(
            f"sprint {sprint_id} has no persisted plan event"
        )
    accepted_before_plan = {
        str(event.get("payload", {}).get("task_id"))
        for event in state["events"]
        if int(event["event_id"]) < plan_event_id
        and event.get("type") == "task_launch_intent_committed"
        and event.get("payload", {}).get("task_id")
    }
    return [
        task_id
        for task_id, task in state["tasks"].items()
        if task_id in accepted_before_plan
        and task.get("sprint_id") != sprint_id
        and task["state"] != TaskState.CLOSED.value
    ]


def waiting_sprint_drain_launches(
    state: Mapping[str, Any],
    sprint_id: str,
    *,
    free_slots: int,
) -> list[str]:
    launchable_states = {
        TaskState.LAUNCHING.value,
        TaskState.RETRY_PENDING.value,
        TaskState.REVISION_PENDING.value,
    }
    candidates = [
        task_id
        for task_id in waiting_sprint_predecessor_task_ids(state, sprint_id)
        if state["tasks"][task_id]["state"] in launchable_states
    ]
    reserved = [
        task_id for task_id in candidates if state["tasks"][task_id].get("slot_reserved")
    ]
    unreserved = [task_id for task_id in candidates if task_id not in reserved]
    return reserved + unreserved[:free_slots]


def sprint_task_writing_payloads(
    sprint_id: str,
    sprint: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Serialize the already-final sprint blueprints for task-writing.

    This function deliberately performs no mathematical planning and does not
    invoke the skill.  It only preserves the existing deterministic adapter
    between the persisted Block-10 plan and Block-4 assignment reports.
    """

    payloads: list[dict[str, Any]] = []
    batch_id = f"BATCH-{sprint_id}"
    for index, blueprint in enumerate(sprint["plan"]["lanes"]):
        item = copy.deepcopy(dict(blueprint))
        operation_id = f"{sprint_id}-lane-{index + 1}"
        portfolio = copy.deepcopy(
            item.get("assignment_portfolio", item.get("portfolio", {})) or {}
        )
        for memory_type in (
            "fact",
            "route",
            "memo",
            "claim",
            "obligation",
            "computation",
        ):
            portfolio.setdefault(memory_type, [])
        payloads.append(
            {
                "operation_id": operation_id,
                "batch_finalized": True,
                "batch_id": batch_id,
                "objective": item.get("objective")
                or item.get("task_objective")
                or "Explore the sprint target.",
                "work_mode": item.get("mode"),
                "sprint_lane": "ABCD"[index],
                "if_resume": None,
                "main_route_ids": item.get("main_route_ids", []),
                "main_obligation_ids": item.get("main_obligation_ids")
                or [sprint["plan"]["target_obligation"]["id"]],
                "selected_new_perspective": item.get(
                    "selected_new_perspective", item.get("perspective")
                ),
                "computation_portfolio": copy.deepcopy(
                    item.get("computation_portfolio")
                ),
                "assignment_portfolio": portfolio,
                "reason": item.get("reason")
                or item.get("material_and_omissions_reason")
                or "Finalized discovery-sprint lane.",
                "root_solution_fact_id": None,
            }
        )
    return payloads
