"""Pure logical-call, lease, and retry state transitions.

This module owns the execution semantics of one persisted logical call.  It
does not load or save scheduler state, allocate durable IDs, or decide what a
retry exhaustion means for a task, operation, sprint, or assignment gate.
Callers apply the returned snapshot and effects inside their own Durable
Kernel compare-and-swap transaction.

The reducer deliberately accepts and returns JSON-shaped dictionaries.  That
keeps the durable scheduler record unchanged while making every lease/retry
transition independently testable.

Persisted lifecycle map::

    prepared/retry_pending --Launch--> running
    running --ReceiveResult(current lease)--> completed --CommitResult--> committed
    running --RecordTransportFailure--> retry_pending | needs_attention
    completed --RejectInvalidResult--> retry_pending | needs_attention
    active --Fence/Cancel--> superseded | needs_attention | cancelled
    needs_attention --AuthorizeRetry--> retry_pending

Failure and invalid-result retries fence the old epoch and advance it.  A full
stop does the same without consuming retry budget; worker calls are
superseded because their owning Task creates a new numbered attempt.  Output
from a non-current or fenced epoch raises :class:`CallLeaseRejected` and never
enters the returned state.  Task, operation, sprint, and gate consequences are
intentionally not part of this reducer.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any, Literal, Mapping

from ..contracts.workflows import CallState


RecoveryDirective = Literal["retry", "commit", "attention"]


class CallTransitionError(RuntimeError):
    """The event is not legal from the persisted call state."""


class CallLeaseRejected(CallTransitionError):
    """An event belongs to an old or fenced lease epoch."""


class CallResultConflict(CallTransitionError):
    """A completed call was replayed with a different result."""


@dataclass(frozen=True)
class CallEffect:
    """A durable scheduler event requested by a pure call transition."""

    event_type: str
    payload: Mapping[str, Any]


@dataclass(frozen=True)
class CallTransition:
    """The complete outcome of reducing one call event."""

    call: Mapping[str, Any]
    effects: tuple[CallEffect, ...] = ()
    changed: bool = True
    retry_allowed: bool | None = None
    recovery_directive: RecoveryDirective | None = None


@dataclass(frozen=True)
class Launch:
    pass


@dataclass(frozen=True)
class ReceiveResult:
    lease_epoch: int
    result: Mapping[str, Any]
    result_digest: str


@dataclass(frozen=True)
class CommitResult:
    pass


@dataclass(frozen=True)
class RecordTransportFailure:
    lease_epoch: int
    reason: str
    occurred_at: str


@dataclass(frozen=True)
class RejectInvalidResult:
    reason: str
    occurred_at: str


@dataclass(frozen=True)
class Fence:
    """Fence the current epoch and place the call in ``target_status``.

    ``advance_epoch`` is true when a future launch may reuse the same logical
    call.  Worker attempts are instead superseded and historically retain the
    numeric epoch, so their callers pass false.
    """

    target_status: CallState
    advance_epoch: bool = False


@dataclass(frozen=True)
class Cancel:
    advance_epoch: bool = False
    authorized_by: str | None = None
    reason: str | None = None


@dataclass(frozen=True)
class AuthorizeRetry:
    pass


@dataclass(frozen=True)
class CommitWorkerResult:
    result: Mapping[str, Any]
    result_digest: str


@dataclass(frozen=True)
class ReconcileAfterStop:
    live: bool = False


CallEvent = (
    Launch
    | ReceiveResult
    | CommitResult
    | RecordTransportFailure
    | RejectInvalidResult
    | Fence
    | Cancel
    | AuthorizeRetry
    | CommitWorkerResult
    | ReconcileAfterStop
)


def prepare_call_record(
    *,
    call_id: str,
    kind: str,
    payload: Mapping[str, Any],
    input_digest: str,
    retry_limit: int,
    event_cursor: int,
    continuation: Mapping[str, Any] | None = None,
    status: CallState = CallState.PREPARED,
    lease_epoch: int = 1,
    attempt: int = 0,
    retry_count: int = 0,
) -> dict[str, Any]:
    """Create the established JSON record for a logical call."""

    return {
        "call_id": str(call_id),
        "kind": str(kind),
        "input": copy.deepcopy(dict(payload)),
        "input_digest": str(input_digest),
        "status": status.value,
        "lease_epoch": int(lease_epoch),
        "fenced_epochs": [],
        "attempt": int(attempt),
        "retry_count": int(retry_count),
        "retry_limit": int(retry_limit),
        "event_cursor": int(event_cursor),
        "continuation": copy.deepcopy(dict(continuation or {})),
        "result": None,
        "result_digest": None,
    }


def _effect(call: Mapping[str, Any], event_type: str, **payload: Any) -> CallEffect:
    return CallEffect(
        event_type,
        {"call_id": str(call["call_id"]), **copy.deepcopy(payload)},
    )


def _fenced(call: dict[str, Any], epoch: int) -> None:
    call["fenced_epochs"] = sorted(
        set(int(value) for value in call.get("fenced_epochs", [])) | {int(epoch)}
    )


def reduce_call(call: Mapping[str, Any], event: CallEvent) -> CallTransition:
    """Apply one execution event without persistence or business side effects."""

    current = copy.deepcopy(dict(call))
    status = str(current.get("status"))

    if isinstance(event, Launch):
        if status not in {
            CallState.PREPARED.value,
            CallState.RETRY_PENDING.value,
        }:
            raise CallTransitionError(
                f"call {current['call_id']} cannot launch from {status}"
            )
        current["status"] = CallState.RUNNING.value
        current["attempt"] = int(current.get("attempt", 0)) + 1
        return CallTransition(
            current,
            (
                _effect(
                    current,
                    "call_running",
                    lease_epoch=int(current["lease_epoch"]),
                    attempt=int(current["attempt"]),
                ),
            ),
        )

    if isinstance(event, ReceiveResult):
        epoch = int(event.lease_epoch)
        if epoch != int(current["lease_epoch"]) or epoch in {
            int(value) for value in current.get("fenced_epochs", [])
        }:
            raise CallLeaseRejected(
                f"stale lease {epoch} for {current['call_id']}"
            )
        if status in {CallState.COMPLETED.value, CallState.COMMITTED.value}:
            if current.get("result_digest") != str(event.result_digest):
                raise CallResultConflict(
                    f"call {current['call_id']} returned two different results"
                )
            return CallTransition(current, changed=False)
        if status != CallState.RUNNING.value:
            raise CallTransitionError(
                f"call {current['call_id']} cannot complete from {status}"
            )
        current["status"] = CallState.COMPLETED.value
        current["result"] = copy.deepcopy(dict(event.result))
        current["result_digest"] = str(event.result_digest)
        return CallTransition(
            current,
            (_effect(current, "call_result_received"),),
        )

    if isinstance(event, CommitResult):
        if status == CallState.COMMITTED.value:
            return CallTransition(current, changed=False)
        if status != CallState.COMPLETED.value:
            raise CallTransitionError(
                f"call {current['call_id']} has no completed result"
            )
        current["status"] = CallState.COMMITTED.value
        return CallTransition(
            current,
            (_effect(current, "call_result_committed"),),
        )

    if isinstance(event, RecordTransportFailure):
        epoch = int(event.lease_epoch)
        if int(current["lease_epoch"]) != epoch:
            raise CallLeaseRejected(f"stale failure for {current['call_id']}")
        current["retry_count"] = int(current.get("retry_count", 0)) + 1
        current.setdefault("failures", []).append(
            {
                "time": str(event.occurred_at),
                "reason": str(event.reason),
                "lease_epoch": epoch,
            }
        )
        # Preserve the historical append semantics for transport failures.
        current.setdefault("fenced_epochs", []).append(epoch)
        current["lease_epoch"] = epoch + 1
        can_retry = int(current["retry_count"]) <= int(current["retry_limit"])
        current["status"] = (
            CallState.RETRY_PENDING.value
            if can_retry
            else CallState.NEEDS_ATTENTION.value
        )
        effects = [
            _effect(
                current,
                "call_transport_failure",
                retry=can_retry,
                reason=str(event.reason),
            )
        ]
        if not can_retry:
            effects.insert(
                0,
                _effect(
                    current,
                    "transport_retry_exhausted",
                    kind=str(current["kind"]),
                    reason=str(event.reason),
                ),
            )
        return CallTransition(
            current,
            tuple(effects),
            retry_allowed=can_retry,
        )

    if isinstance(event, RejectInvalidResult):
        if status != CallState.COMPLETED.value:
            raise CallTransitionError(
                f"call {current['call_id']} has no completed result to reject"
            )
        old_epoch = int(current["lease_epoch"])
        current.setdefault("invalid_results", []).append(
            {
                "time": str(event.occurred_at),
                "reason": str(event.reason),
                "lease_epoch": old_epoch,
                "result": copy.deepcopy(current.get("result")),
                "result_digest": current.get("result_digest"),
            }
        )
        current["retry_count"] = int(current.get("retry_count", 0)) + 1
        _fenced(current, old_epoch)
        current["lease_epoch"] = old_epoch + 1
        current["result"] = None
        current["result_digest"] = None
        can_retry = int(current["retry_count"]) <= int(current["retry_limit"])
        current["status"] = (
            CallState.RETRY_PENDING.value
            if can_retry
            else CallState.NEEDS_ATTENTION.value
        )
        effects = [
            _effect(
                current,
                "call_invalid_output",
                retry=can_retry,
                reason=str(event.reason),
            )
        ]
        if not can_retry:
            effects.insert(
                0,
                _effect(
                    current,
                    "invalid_output_retry_exhausted",
                    kind=str(current["kind"]),
                    reason=str(event.reason),
                ),
            )
        return CallTransition(
            current,
            tuple(effects),
            retry_allowed=can_retry,
        )

    if isinstance(event, Fence):
        old_epoch = int(current.get("lease_epoch", 0))
        _fenced(current, old_epoch)
        if event.advance_epoch:
            current["lease_epoch"] = old_epoch + 1
        current["status"] = event.target_status.value
        return CallTransition(current)

    if isinstance(event, Cancel):
        old_epoch = int(current.get("lease_epoch", 0))
        if event.advance_epoch:
            _fenced(current, old_epoch)
            current["lease_epoch"] = old_epoch + 1
        current["status"] = CallState.CANCELLED.value
        if event.authorized_by is not None or event.reason is not None:
            current["cancellation"] = {
                "authorized_by": str(event.authorized_by or ""),
                "reason": str(event.reason or ""),
            }
        return CallTransition(current)

    if isinstance(event, AuthorizeRetry):
        if status != CallState.NEEDS_ATTENTION.value:
            raise CallTransitionError("call is not in needs_attention")
        current["status"] = CallState.RETRY_PENDING.value
        # Preserve the established operator-authorized one-attempt budget.
        current["retry_limit"] = int(current["retry_count"])
        return CallTransition(current)

    if isinstance(event, CommitWorkerResult):
        current["status"] = CallState.COMMITTED.value
        current["result"] = copy.deepcopy(dict(event.result))
        current["result_digest"] = str(event.result_digest)
        return CallTransition(current)

    if isinstance(event, ReconcileAfterStop):
        if status == CallState.COMPLETED.value:
            return CallTransition(
                current,
                changed=False,
                recovery_directive="commit",
            )
        if status not in {CallState.RUNNING.value, CallState.PREPARED.value}:
            directive: RecoveryDirective | None = None
            if status == CallState.RETRY_PENDING.value:
                directive = "retry"
            elif status == CallState.NEEDS_ATTENTION.value:
                directive = "attention"
            return CallTransition(
                current,
                changed=False,
                recovery_directive=directive,
            )
        if status == CallState.RUNNING.value and event.live:
            return CallTransition(current, changed=False)

        old_epoch = int(current.get("lease_epoch", 0))
        _fenced(current, old_epoch)
        current["lease_epoch"] = old_epoch + 1
        directive = None
        if current.get("kind") == "worker":
            current["status"] = CallState.SUPERSEDED.value
        elif int(current.get("retry_count", 0)) >= int(
            current.get("retry_limit", 0)
        ):
            current["status"] = CallState.NEEDS_ATTENTION.value
            directive = "attention"
        else:
            current["status"] = CallState.RETRY_PENDING.value
            directive = "retry"
        return CallTransition(
            current,
            (
                _effect(
                    current,
                    "call_lease_fenced",
                    old_epoch=old_epoch,
                    new_epoch=old_epoch + 1,
                ),
            ),
            recovery_directive=directive,
        )

    raise TypeError(f"unsupported call event {type(event).__name__}")


__all__ = [
    "AuthorizeRetry",
    "CallEffect",
    "CallEvent",
    "CallLeaseRejected",
    "CallResultConflict",
    "CallTransition",
    "CallTransitionError",
    "Cancel",
    "CommitResult",
    "CommitWorkerResult",
    "Fence",
    "Launch",
    "ReceiveResult",
    "ReconcileAfterStop",
    "RecordTransportFailure",
    "RecoveryDirective",
    "RejectInvalidResult",
    "prepare_call_record",
    "reduce_call",
]
