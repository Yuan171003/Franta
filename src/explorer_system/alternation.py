"""Portable durable transitions for alternating Explorer and host-agent turns.

This module deliberately owns no scheduler, transport, storage, or filesystem
objects.  Every reducer deep-copies its input and returns the replacement state
plus scheduler effects.  A caller can therefore apply the replacement and
append the effects in one existing control-state CAS transaction.

The two configured clocks are *admission* clocks:

* Explorer admits new lineages for two hours.  Lineages admitted before the
  deadline may subsequently consume their complete attempt allowance.
* The collaborator admits research work for eight hours after the handoff
  barrier opens.
  Work already admitted is drained by the surrounding runtime.

Root proof/disproof reports from Explorer are persisted here only as
unverified candidates. They never mutate the collaborator's authoritative
root-resolution
state.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Callable, Mapping


PHASE_CONTROL_KEY = "phase_control"
PHASE_SCHEMA_VERSION = 1
EXPLORER_ADMISSION_DURATION = timedelta(hours=2)
HOST_ADMISSION_DURATION = timedelta(hours=8)
EXPLORER_ADMISSION_SECONDS = int(EXPLORER_ADMISSION_DURATION.total_seconds())
HOST_ADMISSION_SECONDS = int(HOST_ADMISSION_DURATION.total_seconds())

Clock = Callable[[], datetime]


class Phase(str, Enum):
    EXPLORER_ADMISSION = "explorer_admission"
    EXPLORER_DRAIN = "explorer_drain"
    HOST_SORT = "host_sort"
    HOST_RUN = "host_run"
    HOST_DRAIN = "host_drain"


class PhaseControlError(RuntimeError):
    """A requested phase transition is invalid."""


class PhaseConflictError(PhaseControlError):
    """An idempotent transition was replayed with different input."""


@dataclass(frozen=True)
class PhaseTransition:
    """A pure replacement state and its scheduler-owned audit effects."""

    state: dict[str, Any]
    events: tuple[dict[str, Any], ...] = ()
    changed: bool = True


def _instant(now: datetime | Clock) -> datetime:
    value = now() if callable(now) else now
    if not isinstance(value, datetime):
        raise TypeError("clock must return a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("phase-control clock must be timezone-aware")
    return value.astimezone(timezone.utc)


def _stamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _parse(value: Any, field: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise PhaseControlError(f"{field} is missing")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise PhaseControlError(f"{field} is not an ISO timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise PhaseControlError(f"{field} must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PhaseControlError(f"{field} must be nonempty text")
    return value.strip()


def _positive_seconds(value: Any, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise PhaseControlError(f"{field} must be a positive integer")
    return value


def _settings(state: Mapping[str, Any]) -> tuple[int, int]:
    settings = state.get("settings")
    if not isinstance(settings, Mapping):
        raise PhaseControlError("phase-control settings are missing")
    return (
        _positive_seconds(
            settings.get("explorer_admission_seconds"),
            "settings.explorer_admission_seconds",
        ),
        _positive_seconds(
            settings.get("host_admission_seconds"),
            "settings.host_admission_seconds",
        ),
    )


def _event(event_type: str, at: datetime, **payload: Any) -> dict[str, Any]:
    return {
        "type": event_type,
        "time": _stamp(at),
        "payload": copy.deepcopy(payload),
    }


def _replacement(
    original: Mapping[str, Any],
    value: dict[str, Any],
    *events: dict[str, Any],
) -> PhaseTransition:
    return PhaseTransition(
        state=value,
        events=tuple(copy.deepcopy(events)),
        changed=value != dict(original),
    )


def _enter(value: dict[str, Any], phase: Phase, at: datetime) -> None:
    value["phase"] = phase.value
    value["phase_epoch"] = int(value.get("phase_epoch", 0)) + 1
    value["entered_at"] = _stamp(at)


def initialize_phase_state(
    *,
    now: datetime | Clock,
    explorer_admission_seconds: int = EXPLORER_ADMISSION_SECONDS,
    host_admission_seconds: int = HOST_ADMISSION_SECONDS,
) -> dict[str, Any]:
    """Create the first enabled phase state, beginning with Explorer."""

    at = _instant(now)
    explorer_seconds = _positive_seconds(
        explorer_admission_seconds, "explorer_admission_seconds"
    )
    host_seconds = _positive_seconds(host_admission_seconds, "host_admission_seconds")
    return {
        "schema_version": PHASE_SCHEMA_VERSION,
        "enabled": True,
        "settings": {
            "explorer_admission_seconds": explorer_seconds,
            "host_admission_seconds": host_seconds,
        },
        "phase": Phase.EXPLORER_ADMISSION.value,
        "phase_epoch": 1,
        "cycle": 1,
        "entered_at": _stamp(at),
        "explorer": {
            "admission_started_at": _stamp(at),
            "admission_deadline": _stamp(at + timedelta(seconds=explorer_seconds)),
            "admission_closed_at": None,
            "drain_reason": None,
            "root_candidate": None,
        },
        "sort": None,
        "host": None,
        "history": [],
    }


def install_phase_control(
    scheduler_state: Mapping[str, Any],
    *,
    enabled: bool,
    now: datetime | Clock,
    explorer_admission_seconds: int = EXPLORER_ADMISSION_SECONDS,
    host_admission_seconds: int = HOST_ADMISSION_SECONDS,
) -> PhaseTransition:
    """Install the optional state without changing legacy disabled projects."""

    original = copy.deepcopy(dict(scheduler_state))
    if not enabled:
        return PhaseTransition(state=original, changed=False)
    existing = original.get(PHASE_CONTROL_KEY)
    if existing is not None:
        if not isinstance(existing, Mapping) or not existing.get("enabled"):
            raise PhaseConflictError("phase_control already has incompatible state")
        configured = (
            _positive_seconds(
                explorer_admission_seconds, "explorer_admission_seconds"
            ),
            _positive_seconds(
                host_admission_seconds,
                "host_admission_seconds",
            ),
        )
        if _settings(existing) != configured:
            raise PhaseConflictError(
                "phase_control settings differ from the persisted configuration"
            )
        return PhaseTransition(state=original, changed=False)
    at = _instant(now)
    original[PHASE_CONTROL_KEY] = initialize_phase_state(
        now=at,
        explorer_admission_seconds=explorer_admission_seconds,
        host_admission_seconds=host_admission_seconds,
    )
    return PhaseTransition(
        state=original,
        events=(_event("phase_control_enabled", at, phase="explorer_admission"),),
        changed=True,
    )


def phase_enabled(scheduler_state: Mapping[str, Any]) -> bool:
    value = scheduler_state.get(PHASE_CONTROL_KEY)
    return isinstance(value, Mapping) and value.get("enabled") is True


def explorer_admission_open(
    state: Mapping[str, Any], *, now: datetime | Clock
) -> bool:
    """Return whether a new Explorer lineage may be admitted right now."""

    if state.get("phase") != Phase.EXPLORER_ADMISSION.value:
        return False
    explorer = state.get("explorer")
    if not isinstance(explorer, Mapping):
        raise PhaseControlError("Explorer phase metadata is missing")
    return _instant(now) < _parse(
        explorer.get("admission_deadline"), "explorer.admission_deadline"
    )


def request_explorer_drain(
    state: Mapping[str, Any],
    *,
    reason: str,
    now: datetime | Clock,
) -> PhaseTransition:
    """Close lineage admission without stopping already admitted lineages."""

    at = _instant(now)
    exact_reason = _text(reason, "reason")
    value = copy.deepcopy(dict(state))
    current = value.get("phase")
    explorer = value.get("explorer")
    if not isinstance(explorer, dict):
        raise PhaseControlError("Explorer phase metadata is missing")
    if current == Phase.EXPLORER_DRAIN.value:
        if explorer.get("drain_reason") == exact_reason:
            return PhaseTransition(state=value, changed=False)
        raise PhaseConflictError("Explorer drain already has a different reason")
    if current != Phase.EXPLORER_ADMISSION.value:
        raise PhaseControlError(f"cannot drain Explorer from phase {current!r}")
    explorer["admission_closed_at"] = _stamp(at)
    explorer["drain_reason"] = exact_reason
    _enter(value, Phase.EXPLORER_DRAIN, at)
    return _replacement(
        state,
        value,
        _event("explorer_admission_closed", at, reason=exact_reason),
    )


def accept_root_candidate(
    state: Mapping[str, Any],
    *,
    candidate_id: str,
    scratch_id: str,
    lineage_id: str,
    attempt_number: int,
    candidate_outcome: str,
    now: datetime | Clock,
) -> PhaseTransition:
    """Persist an unverified root candidate and close Explorer immediately."""

    at = _instant(now)
    if candidate_outcome not in {"proved", "disproved"}:
        raise PhaseControlError("candidate_outcome must be proved or disproved")
    if not isinstance(attempt_number, int) or isinstance(attempt_number, bool) or attempt_number < 1:
        raise PhaseControlError("attempt_number must be a positive integer")
    candidate = {
        "candidate_id": _text(candidate_id, "candidate_id"),
        "scratch_id": _text(scratch_id, "scratch_id"),
        "lineage_id": _text(lineage_id, "lineage_id"),
        "attempt_number": attempt_number,
        "candidate_outcome": candidate_outcome,
        "status": "unverified_candidate",
        "recorded_at": _stamp(at),
    }
    value = copy.deepcopy(dict(state))
    if value.get("phase") not in {
        Phase.EXPLORER_ADMISSION.value,
        Phase.EXPLORER_DRAIN.value,
    }:
        raise PhaseControlError("root candidates are accepted only during Explorer")
    explorer = value.get("explorer")
    if not isinstance(explorer, dict):
        raise PhaseControlError("Explorer phase metadata is missing")
    existing = explorer.get("root_candidate")
    if existing is not None:
        comparable = copy.deepcopy(candidate)
        comparable["recorded_at"] = existing.get("recorded_at")
        if existing == comparable:
            return PhaseTransition(state=value, changed=False)
        raise PhaseConflictError("Explorer turn already has another root candidate")
    explorer["root_candidate"] = candidate
    explorer["admission_closed_at"] = explorer.get("admission_closed_at") or _stamp(at)
    explorer["drain_reason"] = "root_candidate"
    if value.get("phase") == Phase.EXPLORER_ADMISSION.value:
        _enter(value, Phase.EXPLORER_DRAIN, at)
    return _replacement(
        state,
        value,
        _event(
            "explorer_root_candidate_recorded",
            at,
            candidate_id=candidate["candidate_id"],
            scratch_id=candidate["scratch_id"],
            lineage_id=candidate["lineage_id"],
            attempt_number=attempt_number,
            candidate_outcome=candidate_outcome,
            status="unverified_candidate",
        ),
    )


def begin_host_sort(
    state: Mapping[str, Any],
    *,
    explorer_drained: bool,
    sort_id: str,
    sort_call_id: str,
    main_session_key: str,
    now: datetime | Clock,
) -> PhaseTransition:
    """Enter the sort barrier after every admitted Explorer lineage stopped."""

    at = _instant(now)
    value = copy.deepcopy(dict(state))
    expected = {
        "sort_id": _text(sort_id, "sort_id"),
        "status": "running",
        "sort_call_id": _text(sort_call_id, "sort_call_id"),
        "planning_call_id": None,
        "main_session_key": _text(main_session_key, "main_session_key"),
        "started_at": _stamp(at),
        "completed_at": None,
    }
    if value.get("phase") == Phase.HOST_SORT.value:
        existing = value.get("sort")
        comparable = copy.deepcopy(expected)
        if isinstance(existing, Mapping):
            comparable["started_at"] = existing.get("started_at")
        if existing == comparable:
            return PhaseTransition(state=value, changed=False)
        raise PhaseConflictError("host sort was replayed with different input")
    if value.get("phase") != Phase.EXPLORER_DRAIN.value:
        raise PhaseControlError("host sort requires Explorer drain")
    if explorer_drained is not True:
        raise PhaseControlError("host sort requires all Explorer lineages to drain")
    value["sort"] = expected
    _enter(value, Phase.HOST_SORT, at)
    return _replacement(
        state,
        value,
        _event(
            "host_sort_started",
            at,
            sort_id=expected["sort_id"],
            sort_call_id=expected["sort_call_id"],
            main_session_key=expected["main_session_key"],
        ),
    )


def complete_sort_barrier(
    state: Mapping[str, Any],
    *,
    sort_call_id: str,
    planning_call_id: str,
    main_session_key: str,
    now: datetime | Clock,
) -> PhaseTransition:
    """Open host admission and bind planning to the sorter's exact session."""

    at = _instant(now)
    value = copy.deepcopy(dict(state))
    sort = value.get("sort")
    if not isinstance(sort, dict):
        raise PhaseControlError("sort barrier metadata is missing")
    exact_sort_call = _text(sort_call_id, "sort_call_id")
    exact_planning_call = _text(planning_call_id, "planning_call_id")
    exact_session = _text(main_session_key, "main_session_key")
    if sort.get("sort_call_id") != exact_sort_call:
        raise PhaseConflictError("sort_call_id does not match the active sort")
    if sort.get("main_session_key") != exact_session:
        raise PhaseConflictError("planning must resume the sort main session")
    if value.get("phase") == Phase.HOST_RUN.value:
        if sort.get("planning_call_id") == exact_planning_call:
            return PhaseTransition(state=value, changed=False)
        raise PhaseConflictError("sort barrier already opened for another planning call")
    if value.get("phase") != Phase.HOST_SORT.value or sort.get("status") != "running":
        raise PhaseControlError("sort barrier is not active")
    sort["status"] = "complete"
    sort["planning_call_id"] = exact_planning_call
    sort["completed_at"] = _stamp(at)
    _, host_seconds = _settings(value)
    value["host"] = {
        "admission_started_at": _stamp(at),
        "admission_deadline": _stamp(at + timedelta(seconds=host_seconds)),
        "admission_closed_at": None,
        "drain_reason": None,
    }
    _enter(value, Phase.HOST_RUN, at)
    return _replacement(
        state,
        value,
        _event(
            "host_sort_barrier_opened",
            at,
            sort_call_id=exact_sort_call,
            planning_call_id=exact_planning_call,
            main_session_key=exact_session,
        ),
    )


def _request_host_drain(
    state: Mapping[str, Any], *, reason: str, at: datetime
) -> PhaseTransition:
    value = copy.deepcopy(dict(state))
    host = value.get("host")
    if not isinstance(host, dict):
        raise PhaseControlError("host phase metadata is missing")
    if value.get("phase") == Phase.HOST_DRAIN.value:
        if host.get("drain_reason") == reason:
            return PhaseTransition(state=value, changed=False)
        raise PhaseConflictError("host drain already has a different reason")
    if value.get("phase") != Phase.HOST_RUN.value:
        raise PhaseControlError("host drain requires host run")
    host["admission_closed_at"] = _stamp(at)
    host["drain_reason"] = reason
    _enter(value, Phase.HOST_DRAIN, at)
    return _replacement(
        state,
        value,
        _event("host_admission_closed", at, reason=reason),
    )


def tick(state: Mapping[str, Any], *, now: datetime | Clock) -> PhaseTransition:
    """Apply only deadline-driven transitions; all barriers stay explicit."""

    at = _instant(now)
    phase = state.get("phase")
    if phase == Phase.EXPLORER_ADMISSION.value:
        explorer = state.get("explorer")
        if not isinstance(explorer, Mapping):
            raise PhaseControlError("Explorer phase metadata is missing")
        if at >= _parse(
            explorer.get("admission_deadline"), "explorer.admission_deadline"
        ):
            return request_explorer_drain(state, reason="deadline", now=at)
    elif phase == Phase.HOST_RUN.value:
        host = state.get("host")
        if not isinstance(host, Mapping):
            raise PhaseControlError("host phase metadata is missing")
        if at >= _parse(host.get("admission_deadline"), "host.admission_deadline"):
            return _request_host_drain(state, reason="deadline", at=at)
    return PhaseTransition(state=copy.deepcopy(dict(state)), changed=False)


def complete_host_drain(
    state: Mapping[str, Any],
    *,
    host_drained: bool,
    now: datetime | Clock,
) -> PhaseTransition:
    """Start the next Explorer admission window after the host tail drains."""

    at = _instant(now)
    value = copy.deepcopy(dict(state))
    if value.get("phase") != Phase.HOST_DRAIN.value:
        raise PhaseControlError("next Explorer turn requires host drain")
    if host_drained is not True:
        raise PhaseControlError("next Explorer turn requires the host tail to drain")
    value.setdefault("history", []).append(
        {
            "cycle": int(value.get("cycle", 0)),
            "explorer": copy.deepcopy(value.get("explorer")),
            "sort": copy.deepcopy(value.get("sort")),
            "host": copy.deepcopy(value.get("host")),
        }
    )
    explorer_seconds, _ = _settings(value)
    value["cycle"] = int(value.get("cycle", 0)) + 1
    value["explorer"] = {
        "admission_started_at": _stamp(at),
        "admission_deadline": _stamp(at + timedelta(seconds=explorer_seconds)),
        "admission_closed_at": None,
        "drain_reason": None,
        "root_candidate": None,
    }
    value["sort"] = None
    value["host"] = None
    _enter(value, Phase.EXPLORER_ADMISSION, at)
    return _replacement(
        state,
        value,
        _event(
            "explorer_turn_started",
            at,
            cycle=value["cycle"],
            phase_epoch=value["phase_epoch"],
        ),
    )


__all__ = [
    "HOST_ADMISSION_DURATION",
    "HOST_ADMISSION_SECONDS",
    "EXPLORER_ADMISSION_DURATION",
    "EXPLORER_ADMISSION_SECONDS",
    "PHASE_CONTROL_KEY",
    "PHASE_SCHEMA_VERSION",
    "Phase",
    "PhaseConflictError",
    "PhaseControlError",
    "PhaseTransition",
    "accept_root_candidate",
    "begin_host_sort",
    "complete_host_drain",
    "complete_sort_barrier",
    "explorer_admission_open",
    "initialize_phase_state",
    "install_phase_control",
    "phase_enabled",
    "request_explorer_drain",
    "tick",
]
