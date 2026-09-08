"""Deterministic test doubles for scheduler crash and transport tests."""

from __future__ import annotations

import copy
import threading
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Any, Callable, Deque, Mapping


class TransportError(RuntimeError):
    """A retryable transport/process failure that produced no semantic result."""


class SimulatedCrash(BaseException):
    """Abrupt full-process stop; intentionally not caught as a transport error."""


@dataclass(frozen=True)
class TransportInvocation:
    kind: str
    payload: dict[str, Any]
    call_id: str
    lease_epoch: int
    attempt: int


class FakeTransport:
    """A scripted synchronous transport with deterministic phase hooks.

    Script entries may be result objects, exceptions, or callables accepting a
    :class:`TransportInvocation`.  Hooks use phases ``before`` and ``after``;
    they are useful for crash injection at exact scheduler boundaries.
    """

    def __init__(self) -> None:
        self._scripts: dict[str, Deque[Any]] = defaultdict(deque)
        self._hooks: dict[str, list[Callable[[TransportInvocation, Any], None]]] = defaultdict(
            list
        )
        self.invocations: list[TransportInvocation] = []

    def enqueue(self, kind: str, *steps: Any) -> "FakeTransport":
        self._scripts[kind].extend(steps)
        return self

    def add_hook(
        self, phase: str, hook: Callable[[TransportInvocation, Any], None]
    ) -> "FakeTransport":
        if phase not in {"before", "after"}:
            raise ValueError("phase must be 'before' or 'after'")
        self._hooks[phase].append(hook)
        return self

    def call(
        self,
        kind: str,
        payload: Mapping[str, Any],
        *,
        call_id: str,
        lease_epoch: int,
        attempt: int,
    ) -> Any:
        invocation = TransportInvocation(
            kind=kind,
            payload=copy.deepcopy(dict(payload)),
            call_id=call_id,
            lease_epoch=int(lease_epoch),
            attempt=int(attempt),
        )
        self.invocations.append(invocation)
        for hook in self._hooks["before"]:
            hook(invocation, None)
        if not self._scripts[kind]:
            raise TransportError(f"no scripted response for {kind}")
        step = self._scripts[kind].popleft()
        if isinstance(step, BaseException):
            raise step
        result = step(invocation) if callable(step) else copy.deepcopy(step)
        for hook in self._hooks["after"]:
            hook(invocation, result)
        return result


class FakeControlStore:
    """Thread-safe in-memory implementation of the scheduler-private CAS API."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._states: dict[str, tuple[int, dict[str, Any]]] = {}
        self.records: dict[str, dict[str, Any]] = {}
        self.operation_results: dict[str, dict[str, Any]] = {}
        self._id_counters: dict[str, int] = defaultdict(int)

    def load_control_state(self, key: str) -> tuple[int, dict[str, Any]] | None:
        with self._lock:
            item = self._states.get(key)
            return copy.deepcopy(item) if item is not None else None

    def compare_and_swap_control_state(
        self, key: str, expected_revision: int | None, payload: Mapping[str, Any]
    ) -> int:
        with self._lock:
            current = self._states.get(key)
            current_revision = current[0] if current is not None else None
            if current_revision != expected_revision:
                raise RuntimeError(
                    f"control revision conflict: expected {expected_revision}, got {current_revision}"
                )
            new_revision = 1 if current_revision is None else current_revision + 1
            self._states[key] = (new_revision, copy.deepcopy(dict(payload)))
            return new_revision

    def allocate_id(self, memory_type: str) -> str:
        prefix = {
            "fact": "F",
            "route": "R",
            "memo": "M",
            "claim": "CL",
            "obligation": "O",
            "task": "T",
            "computation": "C",
            "category": "CAT",
        }.get(str(memory_type), str(memory_type).upper())
        self._id_counters[prefix] += 1
        return f"{prefix}-{self._id_counters[prefix]:06d}"

    def apply_operation(
        self,
        operation_id: str,
        operation_type: str,
        payload: Mapping[str, Any],
        *,
        proposal_id: str | None = None,
        actor: str = "scheduler",
    ) -> dict[str, Any]:
        del actor
        existing = self.operation_results.get(operation_id)
        normalized = {
            "operation_type": str(operation_type),
            "payload": copy.deepcopy(dict(payload)),
            "proposal_id": proposal_id,
        }
        if existing is not None:
            if existing["input"] != normalized:
                raise ValueError("operation id replayed with different input")
            return copy.deepcopy(existing["result"])
        record_id = payload.get("id")
        if not record_id and operation_type.endswith("_add"):
            record_id = self.allocate_id(operation_type.removesuffix("_add"))
        if operation_type == "fact":
            record_id = record_id or self.allocate_id("fact")
        if record_id:
            self.records[str(record_id)] = copy.deepcopy(dict(payload, id=record_id))
        result = {"status": "committed", "canonical_id": record_id}
        self.operation_results[operation_id] = {"input": normalized, "result": result}
        return copy.deepcopy(result)

    def operation_status(self, operation_id: str) -> dict[str, Any] | None:
        item = self.operation_results.get(operation_id)
        return copy.deepcopy(item["result"]) if item else None

    def get(self, record_id: str) -> dict[str, Any] | None:
        value = self.records.get(record_id)
        return copy.deepcopy(value) if value else None

