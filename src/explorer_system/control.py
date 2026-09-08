"""Portable state transitions for dynamically admitted Explorer lineages."""

from __future__ import annotations

import copy
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable, Mapping


EXPLORER_SCHEMA_VERSION = 1
DEFAULT_MAX_SLOTS = 4
ATTEMPT_LIMIT = 3
ATTEMPT_TIME_LIMIT = timedelta(hours=4)
ATTEMPT_SECONDS = int(ATTEMPT_TIME_LIMIT.total_seconds())
TERMINAL_LINEAGE_STATES = frozenset({"closed", "stopped"})

Clock = Callable[[], datetime]


class ExplorerStateError(RuntimeError):
    """An Explorer lineage or attempt transition is invalid."""


class ExplorerConflictError(ExplorerStateError):
    """An idempotent Explorer operation was replayed differently."""


class ExplorerCapacityError(ExplorerStateError):
    """No dynamic Explorer slot is available."""


@dataclass(frozen=True)
class ExplorerTransition:
    state: dict[str, Any]
    cancel_call_ids: tuple[str, ...] = ()
    events: tuple[dict[str, Any], ...] = ()
    changed: bool = True


def _instant(now: datetime | Clock) -> datetime:
    value = now() if callable(now) else now
    if not isinstance(value, datetime):
        raise TypeError("clock must return a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Explorer clock must be timezone-aware")
    return value.astimezone(timezone.utc)


def _stamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _parse(value: Any, field: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise ExplorerStateError(f"{field} is missing")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ExplorerStateError(f"{field} is not an ISO timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ExplorerStateError(f"{field} must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ExplorerStateError(f"{field} must be nonempty text")
    return value.strip()


def _positive_integer(value: Any, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ExplorerStateError(f"{field} must be a positive integer")
    return value


def _settings(state: Mapping[str, Any]) -> tuple[int, int]:
    settings = state.get("settings")
    if not isinstance(settings, Mapping):
        raise ExplorerStateError("Explorer attempt settings are missing")
    return (
        _positive_integer(settings.get("attempt_limit"), "settings.attempt_limit"),
        _positive_integer(
            settings.get("attempt_seconds"), "settings.attempt_seconds"
        ),
    )


def _event(event_type: str, at: datetime, **payload: Any) -> dict[str, Any]:
    return {
        "type": event_type,
        "time": _stamp(at),
        "payload": copy.deepcopy(payload),
    }


def _transition(
    original: Mapping[str, Any],
    value: dict[str, Any],
    *,
    cancel_call_ids: Iterable[str] = (),
    events: Iterable[dict[str, Any]] = (),
) -> ExplorerTransition:
    return ExplorerTransition(
        state=value,
        cancel_call_ids=tuple(sorted(set(cancel_call_ids))),
        events=tuple(copy.deepcopy(tuple(events))),
        changed=value != dict(original),
    )


def initialize_explorer_state(
    *,
    attempt_limit: int = ATTEMPT_LIMIT,
    attempt_seconds: int = ATTEMPT_SECONDS,
) -> dict[str, Any]:
    exact_limit = _positive_integer(attempt_limit, "attempt_limit")
    exact_seconds = _positive_integer(attempt_seconds, "attempt_seconds")
    return {
        "schema_version": EXPLORER_SCHEMA_VERSION,
        "settings": {
            "attempt_limit": exact_limit,
            "attempt_seconds": exact_seconds,
        },
        "lineages": {},
        "admission_order": [],
        "root_candidate": None,
    }


def _lineages(state: Mapping[str, Any]) -> Mapping[str, Any]:
    value = state.get("lineages")
    if not isinstance(value, Mapping):
        raise ExplorerStateError("Explorer lineage table is missing")
    return value


def active_lineage_ids(state: Mapping[str, Any]) -> tuple[str, ...]:
    return tuple(
        lineage_id
        for lineage_id in state.get("admission_order", [])
        if _lineages(state).get(lineage_id, {}).get("status")
        not in TERMINAL_LINEAGE_STATES
    )


def available_slots(state: Mapping[str, Any], *, max_slots: int = DEFAULT_MAX_SLOTS) -> int:
    if not isinstance(max_slots, int) or isinstance(max_slots, bool) or max_slots <= 0:
        raise ExplorerStateError("max_slots must be a positive integer")
    return max(0, max_slots - len(active_lineage_ids(state)))


def admit_lineage(
    state: Mapping[str, Any],
    *,
    lineage_id: str,
    session_key: str,
    phase_cycle: int,
    phase_epoch: int,
    admission_allowed: bool,
    now: datetime | Clock,
    max_slots: int = DEFAULT_MAX_SLOTS,
) -> ExplorerTransition:
    """Admit one lineage; directions deliberately impose no serial gate."""

    at = _instant(now)
    exact_id = _text(lineage_id, "lineage_id")
    exact_session = _text(session_key, "session_key")
    if not isinstance(phase_cycle, int) or phase_cycle < 1:
        raise ExplorerStateError("phase_cycle must be a positive integer")
    if not isinstance(phase_epoch, int) or phase_epoch < 1:
        raise ExplorerStateError("phase_epoch must be a positive integer")
    value = copy.deepcopy(dict(state))
    lineages = value.setdefault("lineages", {})
    existing = lineages.get(exact_id)
    if existing is not None:
        if (
            existing.get("session_key") == exact_session
            and existing.get("phase_cycle") == phase_cycle
            and existing.get("phase_epoch") == phase_epoch
        ):
            return ExplorerTransition(state=value, changed=False)
        raise ExplorerConflictError("lineage_id was replayed with different input")
    if admission_allowed is not True:
        raise ExplorerStateError("Explorer lineage admission is closed")
    if available_slots(value, max_slots=max_slots) <= 0:
        raise ExplorerCapacityError("no Explorer slot is free")
    attempt_limit, attempt_seconds = _settings(value)
    lineages[exact_id] = {
        "lineage_id": exact_id,
        "session_key": exact_session,
        "phase_cycle": phase_cycle,
        "phase_epoch": phase_epoch,
        "status": "ready",
        "slot_reserved": True,
        "admitted_at": _stamp(at),
        "attempt_limit": attempt_limit,
        "attempt_seconds": attempt_seconds,
        "attempts_started": 0,
        "attempts": [],
        "current_call_id": None,
        "stop_after_current": False,
        "stop_reason": None,
        "root_candidate_id": None,
    }
    value.setdefault("admission_order", []).append(exact_id)
    return _transition(
        state,
        value,
        events=(
            _event(
                "explorer_lineage_admitted",
                at,
                lineage_id=exact_id,
                phase_cycle=phase_cycle,
                phase_epoch=phase_epoch,
            ),
        ),
    )


def _call_owner(state: Mapping[str, Any], call_id: str) -> str | None:
    for lineage_id, lineage in _lineages(state).items():
        for attempt in lineage.get("attempts", []):
            if attempt.get("call_id") == call_id:
                return str(lineage_id)
    return None


def start_attempt(
    state: Mapping[str, Any],
    *,
    lineage_id: str,
    call_id: str,
    now: datetime | Clock,
    attempt_limit: int = ATTEMPT_LIMIT,
    attempt_seconds: int = ATTEMPT_SECONDS,
) -> ExplorerTransition:
    at = _instant(now)
    exact_lineage = _text(lineage_id, "lineage_id")
    exact_call = _text(call_id, "call_id")
    exact_limit = _positive_integer(attempt_limit, "attempt_limit")
    exact_seconds = _positive_integer(attempt_seconds, "attempt_seconds")
    value = copy.deepcopy(dict(state))
    lineage = value.get("lineages", {}).get(exact_lineage)
    if not isinstance(lineage, dict):
        raise ExplorerStateError(f"unknown Explorer lineage {exact_lineage}")
    persisted = (
        _positive_integer(lineage.get("attempt_limit"), "lineage.attempt_limit"),
        _positive_integer(lineage.get("attempt_seconds"), "lineage.attempt_seconds"),
    )
    if persisted != (exact_limit, exact_seconds):
        raise ExplorerConflictError(
            "attempt settings differ from the admitted lineage configuration"
        )
    owner = _call_owner(value, exact_call)
    if owner is not None:
        if owner == exact_lineage:
            return ExplorerTransition(state=value, changed=False)
        raise ExplorerConflictError(f"call_id already belongs to lineage {owner}")
    if lineage.get("status") not in {"ready", "continuation_pending"}:
        raise ExplorerStateError(
            f"lineage {exact_lineage} cannot start from {lineage.get('status')!r}"
        )
    started = int(lineage.get("attempts_started", 0))
    limit = int(lineage["attempt_limit"])
    if started >= limit:
        raise ExplorerStateError("Explorer lineage exhausted its three attempts")
    number = started + 1
    lineage["attempts_started"] = number
    lineage["status"] = "running"
    lineage["current_call_id"] = exact_call
    lineage.setdefault("attempts", []).append(
        {
            "attempt_number": number,
            "call_id": exact_call,
            "status": "running",
            "started_at": _stamp(at),
            "deadline": _stamp(at + timedelta(seconds=exact_seconds)),
            "ended_at": None,
            "outcome": None,
            "stop_reason": None,
        }
    )
    return _transition(
        state,
        value,
        events=(
            _event(
                "explorer_attempt_started",
                at,
                lineage_id=exact_lineage,
                attempt_number=number,
                call_id=exact_call,
            ),
        ),
    )


def request_expired_attempt_stops(
    state: Mapping[str, Any], *, now: datetime | Clock
) -> ExplorerTransition:
    """Fence every call that reached its configured attempt deadline."""

    at = _instant(now)
    value = copy.deepcopy(dict(state))
    cancellations: list[str] = []
    events: list[dict[str, Any]] = []
    for lineage_id in value.get("admission_order", []):
        lineage = value.get("lineages", {}).get(lineage_id, {})
        if lineage.get("status") != "running" or not lineage.get("attempts"):
            continue
        attempt = lineage["attempts"][-1]
        if attempt.get("status") != "running" or at < _parse(
            attempt.get("deadline"), "attempt.deadline"
        ):
            continue
        attempt["status"] = "stop_requested"
        attempt["stop_reason"] = "attempt_deadline"
        lineage["status"] = "stop_requested"
        call_id = str(attempt["call_id"])
        cancellations.append(call_id)
        events.append(
            _event(
                "explorer_attempt_stop_requested",
                at,
                lineage_id=lineage_id,
                attempt_number=attempt["attempt_number"],
                call_id=call_id,
                reason="attempt_deadline",
            )
        )
    return _transition(
        state,
        value,
        cancel_call_ids=cancellations,
        events=events,
    )


def acknowledge_attempt_end(
    state: Mapping[str, Any],
    *,
    lineage_id: str,
    call_id: str,
    outcome: str,
    now: datetime | Clock,
) -> ExplorerTransition:
    """End one attempt and schedule at most the lineage's remaining attempts."""

    if outcome not in {"finished", "progress", "failed", "timed_out", "interrupted", "stopped"}:
        raise ExplorerStateError("unknown Explorer attempt outcome")
    at = _instant(now)
    exact_lineage = _text(lineage_id, "lineage_id")
    exact_call = _text(call_id, "call_id")
    value = copy.deepcopy(dict(state))
    lineage = value.get("lineages", {}).get(exact_lineage)
    if not isinstance(lineage, dict):
        raise ExplorerStateError(f"unknown Explorer lineage {exact_lineage}")
    matching = [
        attempt
        for attempt in lineage.get("attempts", [])
        if attempt.get("call_id") == exact_call
    ]
    if len(matching) != 1:
        raise ExplorerStateError("attempt call does not belong to the lineage")
    attempt = matching[0]
    if attempt.get("status") in {"ended", "stopped"}:
        if attempt.get("outcome") == outcome:
            return ExplorerTransition(state=value, changed=False)
        raise ExplorerConflictError("attempt end was replayed with another outcome")
    if attempt is not lineage.get("attempts", [])[-1] or attempt.get("status") not in {
        "running",
        "stop_requested",
    }:
        raise ExplorerStateError("only the current running attempt may end")
    forced_stop = bool(lineage.get("stop_after_current"))
    attempt["status"] = "stopped" if forced_stop or outcome == "stopped" else "ended"
    attempt["ended_at"] = _stamp(at)
    attempt["outcome"] = outcome
    lineage["current_call_id"] = None
    if forced_stop or outcome == "stopped":
        lineage["status"] = "stopped"
        lineage["slot_reserved"] = False
    elif int(lineage["attempts_started"]) < int(lineage["attempt_limit"]):
        lineage["status"] = "continuation_pending"
    else:
        lineage["status"] = "closed"
        lineage["slot_reserved"] = False
    return _transition(
        state,
        value,
        events=(
            _event(
                "explorer_attempt_ended",
                at,
                lineage_id=exact_lineage,
                attempt_number=attempt["attempt_number"],
                call_id=exact_call,
                outcome=outcome,
                lineage_status=lineage["status"],
            ),
        ),
    )


def request_stop_all(
    state: Mapping[str, Any],
    *,
    reason: str,
    now: datetime | Clock,
) -> ExplorerTransition:
    """Stop all current and pending lineages without scheduling continuation."""

    at = _instant(now)
    exact_reason = _text(reason, "reason")
    value = copy.deepcopy(dict(state))
    cancellations: list[str] = []
    events: list[dict[str, Any]] = []
    for lineage_id in value.get("admission_order", []):
        lineage = value.get("lineages", {}).get(lineage_id, {})
        if lineage.get("status") in TERMINAL_LINEAGE_STATES:
            continue
        lineage["stop_after_current"] = True
        lineage["stop_reason"] = exact_reason
        current_call = lineage.get("current_call_id")
        if current_call:
            attempt = lineage.get("attempts", [])[-1]
            attempt["status"] = "stop_requested"
            attempt["stop_reason"] = exact_reason
            lineage["status"] = "stop_requested"
            cancellations.append(str(current_call))
        else:
            lineage["status"] = "stopped"
            lineage["slot_reserved"] = False
        events.append(
            _event(
                "explorer_lineage_stop_requested",
                at,
                lineage_id=lineage_id,
                call_id=current_call,
                reason=exact_reason,
            )
        )
    return _transition(
        state,
        value,
        cancel_call_ids=cancellations,
        events=events,
    )


def claim_root_candidate(
    state: Mapping[str, Any],
    *,
    candidate_id: str,
    scratch_id: str,
    lineage_id: str,
    call_id: str,
    candidate_outcome: str,
    now: datetime | Clock,
) -> ExplorerTransition:
    """Record one candidate and immediately request every Explorer call stop."""

    if candidate_outcome not in {"proved", "disproved"}:
        raise ExplorerStateError("candidate_outcome must be proved or disproved")
    at = _instant(now)
    exact_candidate = _text(candidate_id, "candidate_id")
    exact_scratch = _text(scratch_id, "scratch_id")
    exact_lineage = _text(lineage_id, "lineage_id")
    exact_call = _text(call_id, "call_id")
    value = copy.deepcopy(dict(state))
    lineage = value.get("lineages", {}).get(exact_lineage)
    if not isinstance(lineage, dict) or lineage.get("current_call_id") != exact_call:
        raise ExplorerStateError("root candidate must come from a running lineage call")
    attempt = lineage.get("attempts", [])[-1]
    candidate = {
        "candidate_id": exact_candidate,
        "scratch_id": exact_scratch,
        "lineage_id": exact_lineage,
        "attempt_number": int(attempt["attempt_number"]),
        "call_id": exact_call,
        "candidate_outcome": candidate_outcome,
        "status": "unverified_candidate",
        "recorded_at": _stamp(at),
    }
    existing = value.get("root_candidate")
    if existing is not None:
        comparable = copy.deepcopy(candidate)
        comparable["recorded_at"] = existing.get("recorded_at")
        if existing == comparable:
            return ExplorerTransition(state=value, changed=False)
        raise ExplorerConflictError("Explorer turn already has another root candidate")
    value["root_candidate"] = candidate
    lineage["root_candidate_id"] = exact_candidate
    stopped = request_stop_all(value, reason="root_candidate", now=at)
    events = (
        _event(
            "explorer_root_candidate_claimed",
            at,
            candidate_id=exact_candidate,
            scratch_id=exact_scratch,
            lineage_id=exact_lineage,
            attempt_number=attempt["attempt_number"],
            candidate_outcome=candidate_outcome,
            status="unverified_candidate",
        ),
        *stopped.events,
    )
    return _transition(
        state,
        stopped.state,
        cancel_call_ids=stopped.cancel_call_ids,
        events=events,
    )


def reconcile_after_restart(
    state: Mapping[str, Any],
    *,
    live_call_ids: Iterable[str],
    now: datetime | Clock,
) -> ExplorerTransition:
    """Fence lost calls while preserving the three-attempt lineage budget."""

    at = _instant(now)
    live = frozenset(str(call_id) for call_id in live_call_ids)
    value = copy.deepcopy(dict(state))
    events: list[dict[str, Any]] = []
    for lineage_id in value.get("admission_order", []):
        lineage = value.get("lineages", {}).get(lineage_id, {})
        call_id = lineage.get("current_call_id")
        if not call_id or call_id in live:
            continue
        attempt = lineage.get("attempts", [])[-1]
        attempt["ended_at"] = _stamp(at)
        lineage["current_call_id"] = None
        if lineage.get("stop_after_current"):
            attempt["status"] = "stopped"
            attempt["outcome"] = "stopped"
            lineage["status"] = "stopped"
            lineage["slot_reserved"] = False
        else:
            attempt["status"] = "ended"
            attempt["outcome"] = "interrupted"
            if int(lineage["attempts_started"]) < int(lineage["attempt_limit"]):
                lineage["status"] = "continuation_pending"
            else:
                lineage["status"] = "closed"
                lineage["slot_reserved"] = False
        events.append(
            _event(
                "lost_explorer_call_reconciled",
                at,
                lineage_id=lineage_id,
                call_id=call_id,
                lineage_status=lineage["status"],
            )
        )
    return _transition(state, value, events=events)


def drained(state: Mapping[str, Any]) -> bool:
    return all(
        lineage.get("status") in TERMINAL_LINEAGE_STATES
        for lineage in _lineages(state).values()
    )


__all__ = [
    "ATTEMPT_LIMIT",
    "ATTEMPT_SECONDS",
    "ATTEMPT_TIME_LIMIT",
    "DEFAULT_MAX_SLOTS",
    "EXPLORER_SCHEMA_VERSION",
    "ExplorerCapacityError",
    "ExplorerConflictError",
    "ExplorerStateError",
    "ExplorerTransition",
    "TERMINAL_LINEAGE_STATES",
    "acknowledge_attempt_end",
    "active_lineage_ids",
    "admit_lineage",
    "available_slots",
    "claim_root_candidate",
    "drained",
    "initialize_explorer_state",
    "reconcile_after_restart",
    "request_expired_attempt_stops",
    "request_stop_all",
    "start_attempt",
]
