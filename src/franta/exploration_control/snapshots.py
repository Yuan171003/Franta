"""Frozen discovery-sprint synthesis snapshots."""

from __future__ import annotations

import copy
from typing import Any, Callable, Mapping

from ..contracts.workflows import OperationState


class ExplorationSnapshotError(RuntimeError):
    """A sprint-owned terminal artifact cannot be frozen consistently."""


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return copy.deepcopy(dict(value))
    if hasattr(value, "to_dict"):
        return copy.deepcopy(dict(value.to_dict()))
    if hasattr(value, "__dict__"):
        return copy.deepcopy(vars(value))
    raise TypeError(f"expected mapping-like result, got {type(value).__name__}")


def canonical_sprint_snapshot(
    canonical_id: str | None,
    *,
    record_lookup: Callable[[str], Any] | None,
) -> dict[str, Any] | None:
    if not canonical_id or record_lookup is None:
        return None
    try:
        record = record_lookup(str(canonical_id))
    except Exception as exc:
        raise ExplorationSnapshotError(
            f"cannot freeze sprint canonical record {canonical_id}: {exc}"
        ) from exc
    if record is None:
        raise ExplorationSnapshotError(
            f"cannot freeze missing sprint canonical record {canonical_id}"
        )
    return _as_dict(record)


def sprint_operation_change_kind(operation: Mapping[str, Any]) -> str | None:
    if operation.get("state") != OperationState.COMMITTED.value or not operation.get(
        "canonical_id"
    ):
        return None
    synthesis = operation.get("synthesizer_result") or {}
    resolution = synthesis.get("resolution")
    if resolution == "duplicate" and not operation.get("duplicate_root_exception"):
        return None
    kind = str(operation.get("kind") or "")
    if resolution == "update" or kind.endswith("_update"):
        return "updated"
    if kind.endswith("_remove"):
        return "removed"
    return "published"


def sprint_lane_result(
    state: Mapping[str, Any],
    task_id: str,
    *,
    canonical_snapshot: Callable[[str | None], dict[str, Any] | None],
    operation_body: Callable[[Mapping[str, Any]], Mapping[str, Any]],
) -> dict[str, Any]:
    task = state["tasks"].get(task_id)
    if not task:
        raise ExplorationSnapshotError(f"sprint references unknown task {task_id}")

    terminal_operations: list[dict[str, Any]] = []
    canonical_changes: list[dict[str, Any]] = []
    for operation_id in task.get("operation_ids", []):
        operation = state["operations"].get(operation_id)
        if not operation:
            raise ExplorationSnapshotError(
                f"sprint task {task_id} references unknown operation {operation_id}"
            )
        synthesis = operation.get("synthesizer_result") or {}
        kind = str(operation.get("kind") or "")
        accepted_update_patch = None
        if operation.get("state") == OperationState.COMMITTED.value:
            if synthesis.get("resolution") == "update":
                accepted_update_patch = copy.deepcopy(synthesis.get("patch"))
            elif kind in {"route_update", "obligation_update"}:
                accepted_update_patch = copy.deepcopy(
                    dict(operation_body(operation.get("payload") or {}))
                )
        terminal_operations.append(
            {
                "operation_id": operation_id,
                "kind": kind,
                "state": operation.get("state"),
                "canonical_id": operation.get("canonical_id"),
                "resolution": synthesis.get("resolution"),
                "accepted_update_patch": accepted_update_patch,
                "error": operation.get("error"),
            }
        )
        change_kind = sprint_operation_change_kind(operation)
        if change_kind is not None:
            canonical_id = str(operation["canonical_id"])
            canonical_changes.append(
                {
                    "operation_id": operation_id,
                    "kind": operation.get("kind"),
                    "state": operation.get("state"),
                    "change_kind": change_kind,
                    "canonical_id": canonical_id,
                    "canonical_snapshot": canonical_snapshot(canonical_id),
                }
            )

    computations: list[dict[str, Any]] = []
    for staging_id in task.get("computation_ids", []):
        computation = state["computations"].get(staging_id)
        if not computation:
            raise ExplorationSnapshotError(
                f"sprint task {task_id} references unknown computation {staging_id}"
            )
        canonical_id = computation.get("canonical_id")
        item = {
            "staging_id": staging_id,
            "state": computation.get("state"),
            "payload": copy.deepcopy(computation.get("payload")),
            "canonical_id": canonical_id,
            "error": computation.get("error"),
        }
        if computation.get("state") == OperationState.COMMITTED.value and canonical_id:
            item["canonical_snapshot"] = canonical_snapshot(str(canonical_id))
        computations.append(item)

    challenges: list[dict[str, Any]] = []
    for challenge_id in task.get("challenge_ids", []):
        challenge = state["challenges"].get(challenge_id)
        if not challenge:
            raise ExplorationSnapshotError(
                f"sprint task {task_id} references unknown challenge {challenge_id}"
            )
        tail = copy.deepcopy(challenge.get("resolution_tail"))
        revocation = None
        if isinstance(tail, Mapping) and tail.get("operation_id"):
            revocation = copy.deepcopy(
                state.get("revocation_applications", {}).get(tail["operation_id"])
            )
        fact_id = str(challenge.get("payload", {}).get("fact_id") or "")
        challenges.append(
            {
                "challenge_id": challenge_id,
                "fact_id": fact_id,
                "state": challenge.get("state"),
                "challenge": copy.deepcopy(challenge.get("payload")),
                "resolution": challenge.get("resolution"),
                "report": copy.deepcopy(challenge.get("report")),
                "resolution_tail": tail,
                "revocation_result": revocation,
                "challenged_fact_snapshot": canonical_snapshot(fact_id)
                if (
                    fact_id
                    and challenge.get("access_authorized", True)
                    and challenge.get("state") != OperationState.REJECTED.value
                )
                else None,
            }
        )

    return {
        "task_id": task_id,
        "lane": task.get("sprint_lane"),
        "mode": task.get("task_card", {}).get("mode"),
        "task_state": task.get("state"),
        "final_status": task.get("final_status") or task.get("forced_outcome"),
        "final_summary": copy.deepcopy(
            task.get("final_summary") or task.get("mechanical_summary")
        ),
        "terminal_operations": terminal_operations,
        "canonical_changes": canonical_changes,
        "computations": computations,
        "challenges": challenges,
    }


def freeze_sprint_input(
    state: Mapping[str, Any],
    sprint: Mapping[str, Any],
    *,
    lane_result: Callable[[Mapping[str, Any], str], dict[str, Any]],
    cancellation: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    frozen = {
        "plan": copy.deepcopy(sprint["plan"]),
        "lane_results": [
            lane_result(state, str(task_id)) for task_id in sprint.get("task_ids", [])
        ],
    }
    if cancellation is not None:
        frozen["cancellation"] = copy.deepcopy(dict(cancellation))
    return frozen
