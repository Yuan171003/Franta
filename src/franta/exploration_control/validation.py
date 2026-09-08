"""Mechanical discovery-sprint validation for Block 10."""

from __future__ import annotations

import copy
from typing import Any, Callable, Mapping, Sequence

from ..contracts.workflows import validate_sprint_modes


SPRINT_PORTFOLIO_TYPES = (
    "fact",
    "route",
    "memo",
    "claim",
    "obligation",
    "computation",
)
SPRINT_LANE_LABELS = "ABCD"


class ExplorationValidationError(ValueError):
    """A discovery-sprint artifact violates its mechanical contract."""


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return copy.deepcopy(dict(value))
    if hasattr(value, "to_dict"):
        return copy.deepcopy(dict(value.to_dict()))
    if hasattr(value, "__dict__"):
        return copy.deepcopy(vars(value))
    raise TypeError(f"expected mapping-like result, got {type(value).__name__}")


def sprint_portfolio(
    item: Mapping[str, Any], *, lane: str
) -> dict[str, list[str]]:
    assignment = item.get("assignment_portfolio")
    legacy = item.get("portfolio")
    if assignment is not None and legacy is not None and assignment != legacy:
        raise ExplorationValidationError(
            f"discovery-sprint lane {lane} supplies two different portfolios"
        )
    raw = assignment if assignment is not None else legacy
    if not isinstance(raw, Mapping) or set(raw) != set(SPRINT_PORTFOLIO_TYPES):
        raise ExplorationValidationError(
            f"discovery-sprint lane {lane} requires exactly the six portfolio lists"
        )
    portfolio: dict[str, list[str]] = {}
    for memory_type in SPRINT_PORTFOLIO_TYPES:
        ids = raw[memory_type]
        if not isinstance(ids, list) or not all(
            isinstance(memory_id, str) for memory_id in ids
        ):
            raise ExplorationValidationError(
                f"discovery-sprint lane {lane} portfolio {memory_type} must be an ID list"
            )
        portfolio[memory_type] = list(ids)
    return portfolio


def sprint_perspective(item: Mapping[str, Any], *, lane: str) -> Any:
    selected = item.get("selected_new_perspective")
    legacy = item.get("perspective")
    if selected is not None and legacy is not None and selected != legacy:
        raise ExplorationValidationError(
            f"discovery-sprint lane {lane} supplies two different perspectives"
        )
    return selected if selected is not None else legacy


def validate_sprint_target(
    target: Mapping[str, Any],
    *,
    record_lookup: Callable[[str], Any],
) -> str:
    target_id = target.get("id")
    revision = target.get("revision")
    statement = target.get("statement")
    if (
        not isinstance(target_id, str)
        or not target_id.strip()
        or not isinstance(revision, int)
        or isinstance(revision, bool)
        or revision < 1
        or not isinstance(statement, str)
        or not statement.strip()
    ):
        raise ExplorationValidationError(
            "sprint plan requires exact target obligation id/revision/statement"
        )
    try:
        record = _as_dict(record_lookup(target_id))
    except Exception as exc:
        raise ExplorationValidationError(
            f"invalid sprint target obligation {target_id}: {exc}"
        ) from exc
    actual_type = str(record.get("type", record.get("memory_type", "")))
    if actual_type.startswith("MemoryType."):
        actual_type = actual_type.rsplit(".", 1)[-1].lower()
    if actual_type != "obligation":
        raise ExplorationValidationError(
            f"sprint target {target_id} is not an obligation"
        )
    if not bool(record.get("active", True)) or record.get("status") == "removed":
        raise ExplorationValidationError(
            f"sprint target obligation is inactive: {target_id}"
        )
    if int(record.get("revision", -1)) != revision:
        raise ExplorationValidationError(
            f"stale sprint target revision for {target_id}: "
            f"expected {record.get('revision')}, got {revision}"
        )
    if record.get("statement") != statement:
        raise ExplorationValidationError(
            f"sprint target statement does not match canonical obligation {target_id}"
        )
    return target_id


def validate_sprint_lane_blueprints(
    target_id: str,
    lanes: Sequence[Mapping[str, Any]],
    *,
    validate_memory_id: Callable[[str, str, bool], None],
) -> None:
    # Preserve the shared-contract WorkflowError for a mode-order failure.
    validate_sprint_modes(
        str(lane.get("mode", lane.get("work_mode", ""))) for lane in lanes
    )
    for index, lane in enumerate(lanes):
        label = SPRINT_LANE_LABELS[index]
        lane_label = lane.get("lane")
        if lane_label is not None and lane_label != label:
            raise ExplorationValidationError(
                f"discovery-sprint lane {label} has the wrong label"
            )
        if not str(lane.get("objective", lane.get("task_objective", ""))).strip():
            raise ExplorationValidationError(
                f"discovery-sprint lane {label} needs an objective"
            )
        obligations = lane.get("main_obligation_ids") or []
        if obligations != [target_id]:
            raise ExplorationValidationError(
                f"discovery-sprint lane {label} must target only {target_id}"
            )
        if (lane.get("main_route_ids") or []) != []:
            raise ExplorationValidationError(
                f"discovery-sprint lane {label} may not have a main route"
            )
        if lane.get("if_resume"):
            raise ExplorationValidationError(
                f"discovery-sprint lane {label} must start a fresh sealed session"
            )
        policy = lane.get("access_policy")
        if isinstance(policy, Mapping) and (
            policy.get("canonical_memory", False)
            or policy.get("internal_search", False)
            or policy.get("sealed_workspace") is False
        ):
            raise ExplorationValidationError(
                f"discovery-sprint lane {label} requests an unsealed access policy"
            )
        portfolio = sprint_portfolio(lane, lane=label)
        material_ids = [
            memory_id
            for memory_type in SPRINT_PORTFOLIO_TYPES
            for memory_id in portfolio[memory_type]
        ]
        if target_id in material_ids:
            raise ExplorationValidationError(
                f"discovery-sprint lane {label} must carry its target as the main "
                "obligation, not portfolio material"
            )
        for memory_type, ids in portfolio.items():
            for memory_id in ids:
                validate_memory_id(memory_id, memory_type, memory_type == "fact")
        perspective = sprint_perspective(lane, lane=label)
        if label == "B":
            if not isinstance(perspective, str) or not perspective.strip():
                raise ExplorationValidationError(
                    "discovery-sprint lane B needs one selected new perspective"
                )
        elif perspective is not None:
            raise ExplorationValidationError(
                "selected_new_perspective is allowed only for discovery-sprint "
                f"lane B, not {label}"
            )
        if label in {"A", "B"} and any(
            portfolio[memory_type]
            for memory_type in SPRINT_PORTFOLIO_TYPES
            if memory_type != "fact"
        ):
            raise ExplorationValidationError(
                f"discovery-sprint lane {label} may receive only active facts besides its target"
            )
        if label == "C":
            computation_portfolio = lane.get("computation_portfolio")
            if not isinstance(computation_portfolio, list) or not computation_portfolio:
                raise ExplorationValidationError(
                    "discovery-sprint lane C needs an explicit computation portfolio"
                )
            if not all(
                isinstance(item, str) and item.strip()
                for item in computation_portfolio
            ):
                raise ExplorationValidationError(
                    "discovery-sprint lane C computation portfolio must contain "
                    "nonempty instructions"
                )
        if label == "D":
            if not 2 <= len(material_ids) <= 4:
                raise ExplorationValidationError(
                    "discovery-sprint lane D needs two to four distant material IDs"
                )
            if len(set(material_ids)) != len(material_ids):
                raise ExplorationValidationError(
                    "discovery-sprint lane D material IDs must be distinct"
                )
        reason = lane.get("reason", lane.get("material_and_omissions_reason"))
        if not isinstance(reason, str) or not reason.strip():
            raise ExplorationValidationError(
                f"discovery-sprint lane {label} must explain supplied material and omissions"
            )


def validate_sprint_reports_against_plan(
    plan: Mapping[str, Any],
    reports: Sequence[Mapping[str, Any]],
) -> None:
    lanes = list(plan.get("lanes", []))
    if len(lanes) != 4 or len(reports) != 4:
        raise ExplorationValidationError(
            "discovery sprint requires exactly four lane reports"
        )
    target_id = str(plan["target_obligation"]["id"])
    for index, (blueprint, report) in enumerate(zip(lanes, reports, strict=True)):
        label = SPRINT_LANE_LABELS[index]
        expected_mode = str(blueprint.get("mode", blueprint.get("work_mode", "")))
        actual_mode = str(report.get("mode", report.get("work_mode", "")))
        if actual_mode != expected_mode:
            raise ExplorationValidationError(
                f"discovery-sprint lane {label} report changed its worker mode"
            )
        report_lane = report.get("sprint_lane")
        if report_lane is not None and report_lane != label:
            raise ExplorationValidationError(
                f"discovery-sprint lane {label} report changed its lane identity"
            )
        if report.get("if_resume"):
            raise ExplorationValidationError(
                f"discovery-sprint lane {label} report must start fresh"
            )
        expected_objective = blueprint.get(
            "objective", blueprint.get("task_objective")
        )
        actual_objective = report.get("objective", report.get("task_objective"))
        if actual_objective != expected_objective:
            raise ExplorationValidationError(
                f"discovery-sprint lane {label} report changed its objective"
            )
        if (report.get("main_obligation_ids") or []) != [target_id]:
            raise ExplorationValidationError(
                f"discovery-sprint lane {label} report changed its target"
            )
        if (report.get("main_route_ids") or []) != []:
            raise ExplorationValidationError(
                f"discovery-sprint lane {label} report added a main route"
            )
        if sprint_portfolio(report, lane=label) != sprint_portfolio(
            blueprint, lane=label
        ):
            raise ExplorationValidationError(
                f"discovery-sprint lane {label} report changed its supplied material"
            )
        expected_perspective = sprint_perspective(blueprint, lane=label)
        actual_perspective = sprint_perspective(report, lane=label)
        if actual_perspective != expected_perspective:
            raise ExplorationValidationError(
                f"discovery-sprint lane {label} report changed its perspective"
            )
        if label == "C" and report.get("computation_portfolio") != blueprint.get(
            "computation_portfolio"
        ):
            raise ExplorationValidationError(
                "discovery-sprint lane C report changed its computation portfolio"
            )
