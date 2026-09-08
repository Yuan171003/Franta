from __future__ import annotations

import copy
import unittest

from franta.contracts.workflows import CallState
from franta.execution_gateway.call_state import (
    AuthorizeRetry,
    CallLeaseRejected,
    CallResultConflict,
    CallTransitionError,
    Cancel,
    CommitResult,
    CommitWorkerResult,
    Fence,
    Launch,
    ReceiveResult,
    ReconcileAfterStop,
    RecordTransportFailure,
    RejectInvalidResult,
    prepare_call_record,
    reduce_call,
)


def _call(
    *,
    status: CallState = CallState.PREPARED,
    kind: str = "main",
    retry_limit: int = 3,
) -> dict[str, object]:
    return prepare_call_record(
        call_id="CALL-1",
        kind=kind,
        payload={"nested": {"value": 1}},
        input_digest="input-digest",
        retry_limit=retry_limit,
        event_cursor=17,
        continuation={"operation_id": "OP-1"},
        status=status,
    )


class ExecutionCallStateTests(unittest.TestCase):
    def test_prepared_record_preserves_the_durable_json_shape(self) -> None:
        payload = {"nested": {"value": 1}}
        continuation = {"operation_id": "OP-1"}
        call = prepare_call_record(
            call_id="CALL-1",
            kind="main",
            payload=payload,
            input_digest="input-digest",
            retry_limit=3,
            event_cursor=17,
            continuation=continuation,
        )
        payload["nested"]["value"] = 2
        continuation["operation_id"] = "OP-2"
        self.assertEqual(
            call,
            {
                "call_id": "CALL-1",
                "kind": "main",
                "input": {"nested": {"value": 1}},
                "input_digest": "input-digest",
                "status": "prepared",
                "lease_epoch": 1,
                "fenced_epochs": [],
                "attempt": 0,
                "retry_count": 0,
                "retry_limit": 3,
                "event_cursor": 17,
                "continuation": {"operation_id": "OP-1"},
                "result": None,
                "result_digest": None,
            },
        )

    def test_launch_is_pure_and_increments_only_attempt(self) -> None:
        original = _call()
        frozen = copy.deepcopy(original)
        transition = reduce_call(original, Launch())
        self.assertEqual(original, frozen)
        self.assertEqual(transition.call["status"], "running")
        self.assertEqual(transition.call["attempt"], 1)
        self.assertEqual(transition.call["lease_epoch"], 1)
        self.assertEqual(
            [(item.event_type, dict(item.payload)) for item in transition.effects],
            [
                (
                    "call_running",
                    {"call_id": "CALL-1", "lease_epoch": 1, "attempt": 1},
                )
            ],
        )
        with self.assertRaisesRegex(CallTransitionError, "cannot launch from running"):
            reduce_call(transition.call, Launch())

    def test_result_replay_and_commit_are_digest_idempotent(self) -> None:
        running = reduce_call(_call(), Launch()).call
        completed = reduce_call(
            running,
            ReceiveResult(1, {"decision": "wait"}, "result-digest"),
        )
        self.assertEqual(completed.call["status"], "completed")
        self.assertEqual(completed.call["result"], {"decision": "wait"})
        replay = reduce_call(
            completed.call,
            ReceiveResult(1, {"decision": "wait"}, "result-digest"),
        )
        self.assertFalse(replay.changed)
        self.assertEqual(replay.effects, ())
        with self.assertRaisesRegex(CallResultConflict, "two different results"):
            reduce_call(
                completed.call,
                ReceiveResult(1, {"decision": "stuck"}, "different-digest"),
            )
        committed = reduce_call(completed.call, CommitResult())
        self.assertEqual(committed.call["status"], "committed")
        self.assertEqual(
            [item.event_type for item in committed.effects],
            ["call_result_committed"],
        )
        self.assertFalse(reduce_call(committed.call, CommitResult()).changed)

    def test_stale_or_fenced_output_never_changes_the_call(self) -> None:
        running = reduce_call(_call(), Launch()).call
        fenced = reduce_call(
            running,
            Fence(CallState.RETRY_PENDING, advance_epoch=True),
        ).call
        self.assertEqual(fenced["lease_epoch"], 2)
        self.assertEqual(fenced["fenced_epochs"], [1])
        frozen = copy.deepcopy(fenced)
        with self.assertRaisesRegex(CallLeaseRejected, "stale lease 1"):
            reduce_call(
                fenced,
                ReceiveResult(1, {"late": True}, "late-digest"),
            )
        self.assertEqual(fenced, frozen)
        self.assertIsNone(fenced["result"])

    def test_transport_retry_budget_matches_existing_n_plus_one_attempts(self) -> None:
        current = _call(retry_limit=3)
        observed: list[tuple[int, int, int, bool, str]] = []
        for index in range(4):
            launch = reduce_call(current, Launch())
            current = launch.call
            failure = reduce_call(
                current,
                RecordTransportFailure(
                    lease_epoch=int(current["lease_epoch"]),
                    reason=f"offline-{index}",
                    occurred_at=f"time-{index}",
                ),
            )
            current = failure.call
            observed.append(
                (
                    int(current["attempt"]),
                    int(current["retry_count"]),
                    int(current["lease_epoch"]),
                    bool(failure.retry_allowed),
                    str(current["status"]),
                )
            )
        self.assertEqual(
            observed,
            [
                (1, 1, 2, True, "retry_pending"),
                (2, 2, 3, True, "retry_pending"),
                (3, 3, 4, True, "retry_pending"),
                (4, 4, 5, False, "needs_attention"),
            ],
        )
        self.assertEqual(current["fenced_epochs"], [1, 2, 3, 4])
        self.assertEqual(
            [entry["time"] for entry in current["failures"]],
            ["time-0", "time-1", "time-2", "time-3"],
        )

    def test_invalid_result_is_audited_then_cleared_under_same_budget(self) -> None:
        current = reduce_call(_call(retry_limit=1), Launch()).call
        current = reduce_call(
            current,
            ReceiveResult(1, {"verdict": "malformed"}, "bad-one"),
        ).call
        rejected = reduce_call(
            current,
            RejectInvalidResult("invalid verifier report", "time-one"),
        )
        self.assertTrue(rejected.retry_allowed)
        self.assertEqual(rejected.call["status"], "retry_pending")
        self.assertIsNone(rejected.call["result"])
        self.assertEqual(
            rejected.call["invalid_results"],
            [
                {
                    "time": "time-one",
                    "reason": "invalid verifier report",
                    "lease_epoch": 1,
                    "result": {"verdict": "malformed"},
                    "result_digest": "bad-one",
                }
            ],
        )
        current = reduce_call(rejected.call, Launch()).call
        current = reduce_call(
            current,
            ReceiveResult(2, {"verdict": "still malformed"}, "bad-two"),
        ).call
        exhausted = reduce_call(
            current,
            RejectInvalidResult("invalid again", "time-two"),
        )
        self.assertFalse(exhausted.retry_allowed)
        self.assertEqual(exhausted.call["status"], "needs_attention")
        self.assertEqual(exhausted.call["retry_count"], 2)
        self.assertEqual(exhausted.call["lease_epoch"], 3)

    def test_fence_cancel_authorize_and_worker_commit_preserve_legacy_fields(self) -> None:
        running = reduce_call(_call(kind="worker"), Launch()).call
        superseded = reduce_call(
            running,
            Fence(CallState.SUPERSEDED),
        ).call
        self.assertEqual(superseded["status"], "superseded")
        self.assertEqual(superseded["lease_epoch"], 1)
        self.assertEqual(superseded["fenced_epochs"], [1])

        completed_worker = reduce_call(
            running,
            CommitWorkerResult(
                {"final_progress_id": "PRG-1"},
                "worker-result-digest",
            ),
        ).call
        self.assertEqual(completed_worker["status"], "committed")
        self.assertEqual(
            completed_worker["result"], {"final_progress_id": "PRG-1"}
        )

        attention = copy.deepcopy(running)
        attention["status"] = "needs_attention"
        attention["retry_count"] = 4
        authorized = reduce_call(attention, AuthorizeRetry()).call
        self.assertEqual(authorized["status"], "retry_pending")
        self.assertEqual(authorized["retry_limit"], 4)

        cancelled = reduce_call(
            running,
            Cancel(
                advance_epoch=True,
                authorized_by="operator",
                reason="stop",
            ),
        ).call
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertEqual(cancelled["lease_epoch"], 2)
        self.assertEqual(cancelled["fenced_epochs"], [1])
        self.assertEqual(
            cancelled["cancellation"],
            {"authorized_by": "operator", "reason": "stop"},
        )

    def test_recovery_transition_table_is_deterministic(self) -> None:
        cases = [
            (CallState.COMPLETED, False, "completed", 1, "commit"),
            (CallState.RETRY_PENDING, False, "retry_pending", 1, "retry"),
            (CallState.NEEDS_ATTENTION, False, "needs_attention", 1, "attention"),
            (CallState.COMMITTED, False, "committed", 1, None),
            (CallState.CANCELLED, False, "cancelled", 1, None),
            (CallState.RUNNING, True, "running", 1, None),
            (CallState.RUNNING, False, "retry_pending", 2, "retry"),
            (CallState.PREPARED, False, "retry_pending", 2, "retry"),
        ]
        for initial, live, expected_status, expected_epoch, directive in cases:
            with self.subTest(initial=initial, live=live):
                transition = reduce_call(
                    _call(status=initial),
                    ReconcileAfterStop(live=live),
                )
                self.assertEqual(transition.call["status"], expected_status)
                self.assertEqual(transition.call["lease_epoch"], expected_epoch)
                self.assertEqual(transition.recovery_directive, directive)

        exhausted = _call(status=CallState.RUNNING, retry_limit=3)
        exhausted["retry_count"] = 3
        transition = reduce_call(exhausted, ReconcileAfterStop())
        self.assertEqual(transition.call["status"], "needs_attention")
        self.assertEqual(transition.recovery_directive, "attention")
        self.assertEqual(transition.call["retry_count"], 3)

        worker = reduce_call(
            _call(status=CallState.RUNNING, kind="worker"),
            ReconcileAfterStop(),
        )
        self.assertEqual(worker.call["status"], "superseded")
        self.assertIsNone(worker.recovery_directive)
        self.assertEqual(worker.call["fenced_epochs"], [1])
        self.assertEqual(worker.call["lease_epoch"], 2)


if __name__ == "__main__":
    unittest.main()
