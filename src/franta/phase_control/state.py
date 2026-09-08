"""Franta wire adapter for Explorer's host-neutral alternation reducer.

Existing project snapshots retain their original ``franta_*`` phase/event
spelling and ``settings.franta_admission_seconds``/``franta`` keys.  This module
is the translation boundary: the portable reducer sees a neutral host wire,
while Scheduler continues to recover and persist the legacy Franta wire.
"""

from __future__ import annotations

import copy
from datetime import datetime
from enum import Enum
from typing import Any, Callable, Mapping

from explorer_system import alternation as _portable


PHASE_CONTROL_KEY = _portable.PHASE_CONTROL_KEY
PHASE_SCHEMA_VERSION = _portable.PHASE_SCHEMA_VERSION
EXPLORER_ADMISSION_DURATION = _portable.EXPLORER_ADMISSION_DURATION
EXPLORER_ADMISSION_SECONDS = _portable.EXPLORER_ADMISSION_SECONDS
FRANTA_ADMISSION_DURATION = _portable.HOST_ADMISSION_DURATION
FRANTA_ADMISSION_SECONDS = _portable.HOST_ADMISSION_SECONDS
HOST_ADMISSION_DURATION = FRANTA_ADMISSION_DURATION
HOST_ADMISSION_SECONDS = FRANTA_ADMISSION_SECONDS

Clock = Callable[[], datetime]
PhaseControlError = _portable.PhaseControlError
PhaseConflictError = _portable.PhaseConflictError
PhaseTransition = _portable.PhaseTransition


class Phase(str, Enum):
    EXPLORER_ADMISSION = "explorer_admission"
    EXPLORER_DRAIN = "explorer_drain"
    FRANTA_SORT = "franta_sort"
    FRANTA_RUN = "franta_run"
    FRANTA_DRAIN = "franta_drain"
    HOST_SORT = FRANTA_SORT
    HOST_RUN = FRANTA_RUN
    HOST_DRAIN = FRANTA_DRAIN


_TO_PORTABLE_PHASE = {
    Phase.FRANTA_SORT.value: _portable.Phase.HOST_SORT.value,
    Phase.FRANTA_RUN.value: _portable.Phase.HOST_RUN.value,
    Phase.FRANTA_DRAIN.value: _portable.Phase.HOST_DRAIN.value,
}
_TO_FRANTA_PHASE = {value: key for key, value in _TO_PORTABLE_PHASE.items()}
_TO_FRANTA_EVENT = {
    "host_sort_started": "franta_sort_started",
    "host_sort_barrier_opened": "franta_sort_barrier_opened",
    "host_admission_closed": "franta_admission_closed",
}


def _adapt_history(items: Any, *, to_portable: bool) -> Any:
    if not isinstance(items, list):
        return copy.deepcopy(items)
    source_key, target_key = (
        ("franta", "host") if to_portable else ("host", "franta")
    )
    result: list[Any] = []
    for item in items:
        if not isinstance(item, Mapping):
            result.append(copy.deepcopy(item))
            continue
        entry = copy.deepcopy(dict(item))
        if source_key in entry:
            entry[target_key] = entry.pop(source_key)
        result.append(entry)
    return result


def _control_to_portable(state: Mapping[str, Any]) -> dict[str, Any]:
    value = copy.deepcopy(dict(state))
    value["phase"] = _TO_PORTABLE_PHASE.get(value.get("phase"), value.get("phase"))
    settings = value.get("settings")
    if isinstance(settings, Mapping):
        neutral = copy.deepcopy(dict(settings))
        if "franta_admission_seconds" in neutral:
            neutral["host_admission_seconds"] = neutral.pop(
                "franta_admission_seconds"
            )
        value["settings"] = neutral
    if "franta" in value:
        value["host"] = value.pop("franta")
    value["history"] = _adapt_history(value.get("history"), to_portable=True)
    return value


def _control_to_franta(state: Mapping[str, Any]) -> dict[str, Any]:
    value = copy.deepcopy(dict(state))
    value["phase"] = _TO_FRANTA_PHASE.get(value.get("phase"), value.get("phase"))
    settings = value.get("settings")
    if isinstance(settings, Mapping):
        legacy = copy.deepcopy(dict(settings))
        if "host_admission_seconds" in legacy:
            legacy["franta_admission_seconds"] = legacy.pop(
                "host_admission_seconds"
            )
        value["settings"] = legacy
    if "host" in value:
        value["franta"] = value.pop("host")
    value["history"] = _adapt_history(value.get("history"), to_portable=False)
    return value


def _transition_to_franta(transition: _portable.PhaseTransition) -> PhaseTransition:
    events: list[dict[str, Any]] = []
    for event in transition.events:
        adapted = copy.deepcopy(event)
        adapted["type"] = _TO_FRANTA_EVENT.get(adapted.get("type"), adapted.get("type"))
        payload = adapted.get("payload")
        if isinstance(payload, dict) and payload.get("phase") in _TO_FRANTA_PHASE:
            payload["phase"] = _TO_FRANTA_PHASE[payload["phase"]]
        events.append(adapted)
    return PhaseTransition(
        state=_control_to_franta(transition.state),
        events=tuple(events),
        changed=transition.changed,
    )


def initialize_phase_state(
    *,
    now: datetime | Clock,
    explorer_admission_seconds: int = EXPLORER_ADMISSION_SECONDS,
    franta_admission_seconds: int = FRANTA_ADMISSION_SECONDS,
    host_admission_seconds: int | None = None,
) -> dict[str, Any]:
    seconds = franta_admission_seconds if host_admission_seconds is None else host_admission_seconds
    return _control_to_franta(
        _portable.initialize_phase_state(
            now=now,
            explorer_admission_seconds=explorer_admission_seconds,
            host_admission_seconds=seconds,
        )
    )


def install_phase_control(
    scheduler_state: Mapping[str, Any],
    *,
    enabled: bool,
    now: datetime | Clock,
    explorer_admission_seconds: int = EXPLORER_ADMISSION_SECONDS,
    franta_admission_seconds: int = FRANTA_ADMISSION_SECONDS,
    host_admission_seconds: int | None = None,
) -> PhaseTransition:
    if not enabled:
        return PhaseTransition(state=copy.deepcopy(dict(scheduler_state)), changed=False)
    seconds = franta_admission_seconds if host_admission_seconds is None else host_admission_seconds
    neutral_state = copy.deepcopy(dict(scheduler_state))
    existing = neutral_state.get(PHASE_CONTROL_KEY)
    if isinstance(existing, Mapping):
        neutral_state[PHASE_CONTROL_KEY] = _control_to_portable(existing)
    transition = _portable.install_phase_control(
        neutral_state,
        enabled=True,
        now=now,
        explorer_admission_seconds=explorer_admission_seconds,
        host_admission_seconds=seconds,
    )
    state = copy.deepcopy(transition.state)
    control = state.get(PHASE_CONTROL_KEY)
    if isinstance(control, Mapping):
        state[PHASE_CONTROL_KEY] = _control_to_franta(control)
    events: list[dict[str, Any]] = []
    for event in transition.events:
        adapted = copy.deepcopy(event)
        adapted["type"] = _TO_FRANTA_EVENT.get(adapted.get("type"), adapted.get("type"))
        events.append(adapted)
    return PhaseTransition(state=state, events=tuple(events), changed=transition.changed)


def phase_enabled(scheduler_state: Mapping[str, Any]) -> bool:
    return _portable.phase_enabled(scheduler_state)


def explorer_admission_open(state: Mapping[str, Any], *, now: datetime | Clock) -> bool:
    return _portable.explorer_admission_open(_control_to_portable(state), now=now)


def request_explorer_drain(
    state: Mapping[str, Any], *, reason: str, now: datetime | Clock
) -> PhaseTransition:
    return _transition_to_franta(
        _portable.request_explorer_drain(_control_to_portable(state), reason=reason, now=now)
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
    return _transition_to_franta(
        _portable.accept_root_candidate(
            _control_to_portable(state),
            candidate_id=candidate_id,
            scratch_id=scratch_id,
            lineage_id=lineage_id,
            attempt_number=attempt_number,
            candidate_outcome=candidate_outcome,
            now=now,
        )
    )


def begin_franta_sort(
    state: Mapping[str, Any],
    *,
    explorer_drained: bool,
    sort_id: str,
    sort_call_id: str,
    main_session_key: str,
    now: datetime | Clock,
) -> PhaseTransition:
    return _transition_to_franta(
        _portable.begin_host_sort(
            _control_to_portable(state),
            explorer_drained=explorer_drained,
            sort_id=sort_id,
            sort_call_id=sort_call_id,
            main_session_key=main_session_key,
            now=now,
        )
    )


begin_host_sort = begin_franta_sort


def complete_sort_barrier(
    state: Mapping[str, Any],
    *,
    sort_call_id: str,
    planning_call_id: str,
    main_session_key: str,
    now: datetime | Clock,
) -> PhaseTransition:
    return _transition_to_franta(
        _portable.complete_sort_barrier(
            _control_to_portable(state),
            sort_call_id=sort_call_id,
            planning_call_id=planning_call_id,
            main_session_key=main_session_key,
            now=now,
        )
    )


def tick(state: Mapping[str, Any], *, now: datetime | Clock) -> PhaseTransition:
    return _transition_to_franta(_portable.tick(_control_to_portable(state), now=now))


def complete_franta_drain(
    state: Mapping[str, Any], *, franta_drained: bool, now: datetime | Clock
) -> PhaseTransition:
    return _transition_to_franta(
        _portable.complete_host_drain(
            _control_to_portable(state), host_drained=franta_drained, now=now
        )
    )


def complete_host_drain(
    state: Mapping[str, Any], *, host_drained: bool, now: datetime | Clock
) -> PhaseTransition:
    return complete_franta_drain(state, franta_drained=host_drained, now=now)


__all__ = [
    "FRANTA_ADMISSION_DURATION",
    "FRANTA_ADMISSION_SECONDS",
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
    "begin_franta_sort",
    "begin_host_sort",
    "complete_franta_drain",
    "complete_host_drain",
    "complete_sort_barrier",
    "explorer_admission_open",
    "initialize_phase_state",
    "install_phase_control",
    "phase_enabled",
    "request_explorer_drain",
    "tick",
]
