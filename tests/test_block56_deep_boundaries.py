from __future__ import annotations

import copy
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest import mock

from franta.contracts.workflows import CallState
from franta.execution_gateway.call_state import (
    CommitResult,
    Launch,
    ReceiveResult,
    ReconcileAfterStop,
    RecordTransportFailure,
    reduce_call,
)
from franta.read_access.snapshots import (
    MemorySnapshot,
    SnapshotValidationError,
    snapshot_from_record,
    validate_assignment_portfolio,
    validate_category_portfolio_snapshot,
    validate_event_window,
    validate_memory_snapshot,
)
from franta.recovery import stable_digest
from franta.scheduler import Scheduler
from franta.testing import FakeControlStore


_MEMBER_TYPES = ("fact", "route", "memo", "claim", "obligation")
_PORTFOLIO_TYPES = (*_MEMBER_TYPES, "computation")


def _empty_members() -> dict[str, list[str]]:
    return {memory_type: [] for memory_type in _MEMBER_TYPES}


class ReadSnapshotContractTests(unittest.TestCase):
    def test_record_snapshot_is_exact_and_validation_does_not_mutate_it(self) -> None:
        record = {
            "id": "F-EXACT",
            "type": "fact",
            "abstract": "A copied abstract.",
            "revision": 3,
            "status": "active",
            "active": True,
        }
        snapshot = snapshot_from_record(
            record,
            content="The complete copied record.",
            requested_id="F-EXACT",
        )
        before = copy.deepcopy(snapshot)

        returned = validate_memory_snapshot(
            snapshot,
            expected_id="F-EXACT",
            expected_type="fact",
            expected_revision=3,
            expected_status="active",
        )

        self.assertIs(returned, snapshot)
        self.assertEqual(snapshot, before)
        self.assertEqual(
            snapshot.summary(),
            {
                "id": "F-EXACT",
                "memory_type": "fact",
                "abstract": "A copied abstract.",
                "revision": 3,
                "status": "active",
            },
        )

        with self.assertRaisesRegex(SnapshotValidationError, "does not match type"):
            validate_memory_snapshot(
                MemorySnapshot("R-MISTYPED", "fact", "", "copied")
            )
        with self.assertRaisesRegex(SnapshotValidationError, "positive integer"):
            validate_memory_snapshot(
                MemorySnapshot("F-BAD-REV", "fact", "", "copied", revision=True)
            )

    def test_assignment_validation_preserves_the_id_only_rules(self) -> None:
        prefixes = {
            "fact": "F",
            "route": "R",
            "memo": "M",
            "claim": "CL",
            "obligation": "O",
            "computation": "C",
        }
        records = {
            f"{prefix}-ONE": {
                "id": f"{prefix}-ONE",
                "type": memory_type,
                "revision": 7,
                "status": "active",
                "active": True,
            }
            for memory_type, prefix in prefixes.items()
        }
        portfolio = {
            memory_type: [f"{prefixes[memory_type]}-ONE"]
            for memory_type in _PORTFOLIO_TYPES
        }

        self.assertIsNone(
            validate_assignment_portfolio(portfolio, record_lookup=records.get)
        )

        # The established assignment wire format binds IDs, not revisions.
        # A changed non-Fact revision therefore remains accepted here.
        records["R-ONE"]["revision"] = 99
        self.assertIsNone(
            validate_assignment_portfolio(portfolio, record_lookup=records.get)
        )

        inactive = copy.deepcopy(records)
        inactive["F-ONE"].update({"active": False, "status": "revoked"})
        with self.assertRaisesRegex(SnapshotValidationError, "inactive fact"):
            validate_assignment_portfolio(portfolio, record_lookup=inactive.get)

        mistyped = copy.deepcopy(records)
        mistyped["R-ONE"]["type"] = "memo"
        with self.assertRaisesRegex(SnapshotValidationError, "expected route"):
            validate_assignment_portfolio(portfolio, record_lookup=mistyped.get)

    def test_category_snapshot_uses_the_selected_historical_revision(self) -> None:
        revision_one_members = _empty_members()
        revision_one_members["fact"] = ["F-OLD"]
        revision_two_members = _empty_members()
        revision_two_members["fact"] = ["F-NEW"]
        current = {
            "CAT-ONE": {
                "id": "CAT-ONE",
                "revision": 2,
                "status": "active",
                "members": revision_two_members,
            }
        }
        history = {
            "CAT-ONE": [
                {
                    "id": "CAT-ONE",
                    "revision": 1,
                    "status": "active",
                    "members": revision_one_members,
                }
            ]
        }
        flattened = _empty_members()
        flattened["fact"] = ["F-OLD"]
        snapshot = {
            "snapshot_id": "CP-ONE",
            "revision": 4,
            "categories": [
                {"category_id": "CAT-ONE", "category_revision": 1}
            ],
            "flattened_members": flattened,
            "base_event_id": 10,
            "confirmed_through_event_id": 12,
        }

        self.assertIsNone(
            validate_category_portfolio_snapshot(
                snapshot,
                current_categories=current,
                category_history=history,
                expected_portfolio_revision=4,
                expected_base_event_id=10,
                expected_confirmed_through_event_id=12,
            )
        )
        broken = copy.deepcopy(snapshot)
        broken["flattened_members"]["fact"] = ["F-NEW"]
        with self.assertRaisesRegex(SnapshotValidationError, "selected category union"):
            validate_category_portfolio_snapshot(
                broken,
                current_categories=current,
                category_history=history,
            )
        with self.assertRaisesRegex(SnapshotValidationError, "cannot precede"):
            validate_event_window(13, 12)


class SchedulerCallAdapterParityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.scheduler = Scheduler(FakeControlStore())

    def test_launch_receive_replay_and_commit_match_the_pure_machine(self) -> None:
        call_id = self.scheduler.prepare_call(
            "main",
            {"portfolio": {"revision": 4}},
            call_id="CALL-PARITY",
            retry_limit=2,
            continuation={"phase": "planning"},
        )
        pure = copy.deepcopy(self.scheduler.state["calls"][call_id])

        launched = reduce_call(pure, Launch())
        lease_epoch, attempt = self.scheduler.mark_call_running(call_id)
        self.assertEqual((lease_epoch, attempt), (1, 1))
        self.assertEqual(self.scheduler.state["calls"][call_id], launched.call)

        result = {"decision": "wait", "reason": "pending work"}
        digest = stable_digest(result)
        received = reduce_call(
            launched.call,
            ReceiveResult(lease_epoch=lease_epoch, result=result, result_digest=digest),
        )
        self.scheduler.accept_call_result(call_id, lease_epoch, result)
        self.assertEqual(self.scheduler.state["calls"][call_id], received.call)

        replayed = reduce_call(
            received.call,
            ReceiveResult(lease_epoch=lease_epoch, result=result, result_digest=digest),
        )
        self.assertFalse(replayed.changed)
        self.scheduler.accept_call_result(call_id, lease_epoch, result)
        self.assertEqual(self.scheduler.state["calls"][call_id], replayed.call)

        committed = reduce_call(replayed.call, CommitResult())
        self.scheduler.mark_call_committed(call_id)
        self.assertEqual(self.scheduler.state["calls"][call_id], committed.call)

    def test_failure_and_full_stop_recovery_match_the_pure_machine(self) -> None:
        call_id = self.scheduler.prepare_call(
            "main", {"input": "exact"}, call_id="CALL-FAIL", retry_limit=1
        )
        pure = reduce_call(self.scheduler.state["calls"][call_id], Launch()).call
        lease_epoch, _ = self.scheduler.mark_call_running(call_id)
        fixed_time = "2030-01-02T03:04:05+00:00"
        failed = reduce_call(
            pure,
            RecordTransportFailure(
                lease_epoch=lease_epoch,
                reason="offline",
                occurred_at=fixed_time,
            ),
        )
        with mock.patch("franta.scheduler.utc_now", return_value=fixed_time):
            self.assertTrue(
                self.scheduler.mark_call_failed(call_id, lease_epoch, "offline")
            )
        self.assertEqual(self.scheduler.state["calls"][call_id], failed.call)

        relaunched = reduce_call(failed.call, Launch())
        second_epoch, second_attempt = self.scheduler.mark_call_running(call_id)
        self.assertEqual((second_epoch, second_attempt), (2, 2))
        self.assertEqual(self.scheduler.state["calls"][call_id], relaunched.call)
        exhausted = reduce_call(
            relaunched.call,
            RecordTransportFailure(
                lease_epoch=second_epoch,
                reason="still offline",
                occurred_at=fixed_time,
            ),
        )
        with mock.patch("franta.scheduler.utc_now", return_value=fixed_time):
            self.assertFalse(
                self.scheduler.mark_call_failed(
                    call_id, second_epoch, "still offline"
                )
            )
        self.assertEqual(self.scheduler.state["calls"][call_id], exhausted.call)
        self.assertEqual(exhausted.call["status"], CallState.NEEDS_ATTENTION.value)
        self.assertTrue(
            any(
                item["attention_id"] == f"call:{call_id}"
                for item in self.scheduler.state["needs_attention"]
            )
        )

        recovery_id = self.scheduler.prepare_call(
            "trimmer", {"round": 1}, call_id="CALL-RECOVERY", retry_limit=2
        )
        original = self.scheduler.state["calls"][recovery_id]
        expected = reduce_call(original, ReconcileAfterStop(live=False))
        gate_before = self.scheduler.gate
        plan = self.scheduler.recover()
        self.assertEqual(self.scheduler.state["calls"][recovery_id], expected.call)
        self.assertIn(recovery_id, plan.retry_call_ids)
        self.assertEqual(self.scheduler.gate, gate_before)


class BlockBoundaryDependencyTests(unittest.TestCase):
    def test_pure_block_modules_do_not_import_state_owning_workflows(self) -> None:
        source_root = Path(__file__).resolve().parents[1] / "src"
        environment = dict(os.environ)
        environment["PYTHONPATH"] = str(source_root)
        probes = (
            "import sys; import franta.read_access.snapshots; "
            "blocked={'franta.scheduler','franta.runtime','franta.categories',"
            "'franta.execution_gateway'}; present=sorted(blocked & set(sys.modules)); "
            "raise SystemExit('back edge: '+repr(present) if present else 0)",
            "import sys; import franta.execution_gateway.call_state; "
            "blocked={'franta.scheduler','franta.runtime','franta.categories'}; "
            "present=sorted(blocked & set(sys.modules)); "
            "raise SystemExit('back edge: '+repr(present) if present else 0)",
        )
        for probe in probes:
            with self.subTest(probe=probe):
                result = subprocess.run(
                    [sys.executable, "-c", probe],
                    capture_output=True,
                    check=False,
                    env=environment,
                    text=True,
                )
                self.assertEqual(result.returncode, 0, result.stderr or result.stdout)


if __name__ == "__main__":
    unittest.main()
