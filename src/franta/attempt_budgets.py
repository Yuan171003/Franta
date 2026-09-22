"""Durable phase-wide logical worker-attempt accounting.

Helpers mutate only the scheduler's private transaction copy. The public status
projection is read-only and never activates a pending operator edit.
"""

from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, MutableMapping


EXPLORER_ATTEMPT_LIMIT = 20
FRANTA_ATTEMPT_LIMIT = 30
EDIT_DELAY_SECONDS = 120


def _instant(value: datetime | str) -> datetime:
    at = datetime.fromisoformat(value) if isinstance(value, str) else value
    if not isinstance(at, datetime) or at.tzinfo is None or at.utcoffset() is None:
        raise ValueError("attempt budget timestamps must be timezone-aware")
    return at.astimezone(timezone.utc)


def _limit(value: Any) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError("attempt limits must be positive integers")
    return value


def enabled(state: Mapping[str, Any]) -> bool:
    return isinstance((state.get("phase_control") or {}).get("attempt_budget"), Mapping)


def is_budget_drain(state: Mapping[str, Any], stage: str = "franta") -> bool:
    phase = state.get("phase_control") or {}
    return bool(
        enabled(state)
        and phase.get("phase") == f"{stage}_drain"
        and (phase.get(stage) or {}).get("drain_reason") == "attempt_budget"
    )


def install(
    state: MutableMapping[str, Any], *, explorer_limit: int, franta_limit: int
) -> None:
    """Upgrade an active legacy turn once, preserving subsequent operator edits."""

    limits = {"explorer": _limit(explorer_limit), "franta": _limit(franta_limit)}
    phase = state["phase_control"]
    if "attempt_budget" not in phase:
        phase["attempt_budget"] = {
            "schema_version": 1,
            "limits": limits,
            "pending": None,
            "latest_command_id": None,
            "latest_submitted_at": None,
        }
        # A legacy elapsed clock is no longer an admission condition. Preserve
        # already-started handoff/Advisor barriers and all root-candidate fences.
        stage = "explorer" if str(phase.get("phase", "")).startswith("explorer") else "franta"
        metadata = phase.get(stage) or {}
        if phase.get("phase") == f"{stage}_drain" and metadata.get("drain_reason") == "deadline":
            metadata["drain_reason"] = "attempt_budget"
        if phase.get("phase") in {"franta_run", "franta_drain"}:
            run_epoch = int(phase.get("phase_epoch", 0)) - (phase["phase"] == "franta_drain")
            for task in state.get("tasks", {}).values():
                if task.get("agent_system") == "franta" and int(task.get("origin_phase_epoch", -1)) == run_epoch:
                    task["attempt_budget_cycle"] = int(phase["cycle"])
    refresh_counts(state)


def queue_limits(
    state: MutableMapping[str, Any], *, explorer_limit: int, franta_limit: int,
    command_id: str, submitted_at: datetime | str,
) -> bool:
    if not enabled(state):
        raise ValueError("attempt budgets are not enabled for this project")
    explorer_limit, franta_limit = _limit(explorer_limit), _limit(franta_limit)
    if not isinstance(command_id, str) or not command_id.strip():
        raise ValueError("attempt budget command_id must be nonempty")
    at = _instant(submitted_at)
    budget = state["phase_control"]["attempt_budget"]
    previous = budget.get("latest_submitted_at")
    if previous is not None:
        ordering = (at, command_id)
        prior_ordering = (_instant(previous), str(budget.get("latest_command_id") or ""))
        if ordering <= prior_ordering:
            return False
    budget["latest_submitted_at"] = at.isoformat()
    budget["latest_command_id"] = command_id
    budget["pending"] = {
        "explorer_limit": explorer_limit,
        "franta_limit": franta_limit,
        "command_id": command_id,
        "submitted_at": at.isoformat(),
        "effective_at": (at + timedelta(seconds=EDIT_DELAY_SECONDS)).isoformat(),
    }
    return True


def apply_pending(state: MutableMapping[str, Any], *, now: datetime) -> dict[str, Any] | None:
    if not enabled(state):
        return None
    budget = state["phase_control"]["attempt_budget"]
    pending = budget.get("pending")
    if not isinstance(pending, Mapping) or _instant(now) < _instant(pending["effective_at"]):
        return None
    budget["limits"] = {"explorer": pending["explorer_limit"], "franta": pending["franta_limit"]}
    budget["pending"] = None
    return copy.deepcopy(dict(pending))


def _is_infrastructure_retry(attempt: Mapping[str, Any]) -> bool:
    return (attempt.get("supplement") or {}).get("kind") == "infrastructure_retry"


def refresh_counts(state: MutableMapping[str, Any]) -> dict[str, dict[str, int]]:
    """Rebuild exact counters from durable attempts, attaching stable charges.

    Explorer transport retries already share one planned attempt. Franta wraps
    infrastructure retries in a new physical task attempt; those inherit the
    preceding logical charge even when a semantic supplement is preserved.
    """

    counts = {stage: {"used": 0, "completed": 0, "running": 0} for stage in ("explorer", "franta")}
    if not enabled(state):
        return counts
    phase = state["phase_control"]
    cycle = int(phase.get("cycle", 0))
    for lineage in (state.get("explorer_control") or {}).get("lineages", {}).values():
        if int(lineage.get("phase_cycle", cycle)) != cycle:
            continue
        for attempt in lineage.get("attempts", []):
            counts["explorer"]["used"] += 1
            if attempt.get("status") in {"ended", "stopped"}:
                counts["explorer"]["completed"] += 1
            elif attempt.get("status") in {"running", "stop_requested"}:
                counts["explorer"]["running"] += 1
    for task_id, task in state.get("tasks", {}).items():
        if task.get("agent_system") == "franta-sort" or task.get("non_slot_task"):
            continue
        if task.get("attempt_budget_cycle") != cycle:
            continue
        groups: dict[str, list[Mapping[str, Any]]] = {}
        previous_charge: str | None = None
        for attempt in task.get("attempts", []):
            charge = attempt.get("attempt_budget_charge")
            if not charge:
                charge = previous_charge if _is_infrastructure_retry(attempt) else None
                charge = charge or f"{task_id}:{int(attempt.get('attempt', 0))}"
                attempt["attempt_budget_charge"] = charge
            groups.setdefault(str(charge), []).append(attempt)
            previous_charge = str(charge)
        for charge, attempts in groups.items():
            last = attempts[-1]
            counts["franta"]["used"] += 1
            retry_pending = (
                charge == previous_charge
                and task.get("state") == "retry_pending"
                and (task.get("pending_attempt_supplement") or {}).get("kind") == "infrastructure_retry"
            )
            if last.get("state") == "running":
                counts["franta"]["running"] += 1
            elif not retry_pending:
                counts["franta"]["completed"] += 1
    for stage, values in counts.items():
        metadata = phase.get(stage)
        if isinstance(metadata, dict):
            metadata["attempts_used"] = values["used"]
    return counts


def attempt_budget_status(state: Mapping[str, Any]) -> dict[str, Any]:
    """Project persisted limits and logical counts without changing live state."""

    snapshot = copy.deepcopy(dict(state))
    counts = refresh_counts(snapshot)
    budget = (snapshot.get("phase_control") or {}).get("attempt_budget") or {}
    limits = budget.get("limits") or {"explorer": EXPLORER_ATTEMPT_LIMIT, "franta": FRANTA_ATTEMPT_LIMIT}
    return {
        "enabled": enabled(state),
        **{stage: {"limit": int(limits[stage]), **counts[stage]} for stage in counts},
        "pending": copy.deepcopy(budget.get("pending")),
        "latest_command_id": budget.get("latest_command_id"),
        "latest_submitted_at": budget.get("latest_submitted_at"),
    }
