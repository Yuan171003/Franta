from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path

from franta.categories import CategoryStore
from franta.contracts.agent_access import policy_for
from franta.materialize import MemorySnapshot as LegacyMemorySnapshot
from franta.read_access.materialization import (
    MaterializationError,
    MemorySnapshot as MaterializationMemorySnapshot,
    WorkspaceMaterializer,
)
from franta.read_access.snapshots import (
    MemorySnapshot,
    SnapshotValidationError,
    assignment_snapshots_from_task_card,
    snapshot_from_record,
    validate_assignment_portfolio,
    validate_category_portfolio_snapshot,
    validate_event_window,
    validate_memory_snapshot,
    validate_record_reference,
)


def _members(**updates: list[str]) -> dict[str, list[str]]:
    result = {
        key: []
        for key in ("fact", "route", "memo", "claim", "obligation")
    }
    result.update(updates)
    return result


class ReadAccessSnapshotTests(unittest.TestCase):
    def test_memory_snapshot_move_preserves_legacy_object_identity(self) -> None:
        self.assertIs(LegacyMemorySnapshot, MemorySnapshot)
        self.assertIs(MaterializationMemorySnapshot, MemorySnapshot)

    def test_exact_memory_snapshot_binds_id_type_revision_and_status(self) -> None:
        snapshot = MemorySnapshot(
            "F-1",
            "fact",
            "An abstract",
            "A copied proof",
            revision=3,
            status="active",
        )
        self.assertIs(
            validate_memory_snapshot(
                snapshot,
                expected_id="F-1",
                expected_type="fact",
                expected_revision=3,
                expected_status="active",
            ),
            snapshot,
        )
        with self.assertRaisesRegex(SnapshotValidationError, "does not match type"):
            validate_memory_snapshot(
                MemorySnapshot("R-1", "fact", "", "", revision=1, status="active")
            )
        with self.assertRaisesRegex(SnapshotValidationError, "revision mismatch"):
            validate_memory_snapshot(snapshot, expected_revision=2)
        with self.assertRaisesRegex(SnapshotValidationError, "status mismatch"):
            validate_memory_snapshot(snapshot, expected_status="revoked")

    def test_record_snapshot_exact_mode_and_id_only_mode_are_explicit(self) -> None:
        record = {
            "id": "F-1",
            "type": "fact",
            "revision": 4,
            "status": "active",
            "active": True,
            "abstract": "Current record",
        }
        snapshot = snapshot_from_record(
            record,
            content="Current proof",
            requested_id="F-1",
            exact=True,
        )
        self.assertEqual((snapshot.revision, snapshot.status), (4, "active"))
        validate_record_reference(
            record,
            memory_id="F-1",
            expected_type="fact",
            active_fact=True,
            exact_id=True,
            expected_revision=4,
            expected_status="active",
        )
        with self.assertRaisesRegex(SnapshotValidationError, "does not match type"):
            validate_record_reference(
                {**record, "id": "R-1", "type": "fact"},
                memory_id="R-1",
                expected_type="fact",
                active_fact=True,
                exact_id=True,
            )

        # The currently persisted assignment format contains IDs, not pinned
        # revisions.  A newer current revision therefore remains valid here.
        newer = {**record, "revision": 5}
        validate_record_reference(
            newer,
            memory_id="F-1",
            expected_type="fact",
            active_fact=True,
        )
        with self.assertRaisesRegex(SnapshotValidationError, "revision mismatch"):
            validate_record_reference(
                newer,
                memory_id="F-1",
                expected_type="fact",
                active_fact=True,
                expected_revision=4,
            )
        legacy = snapshot_from_record(
            {**newer, "id": "F-different"},
            content="Latest proof",
            requested_id="F-1",
        )
        self.assertEqual((legacy.memory_id, legacy.revision), ("F-1", 5))

    def test_assignment_portfolio_retains_current_type_and_active_fact_rules(self) -> None:
        records = {
            "F-1": {
                "id": "F-1",
                "type": "fact",
                "revision": 1,
                "status": "active",
                "active": True,
            },
            "R-1": {
                "id": "R-1",
                "type": "route",
                "revision": 2,
                "status": "active",
                "active": True,
            },
        }
        validate_assignment_portfolio(
            {"fact": ["F-1"], "route": ["R-1"]},
            record_lookup=records.__getitem__,
        )
        wrong_type = {**records, "R-1": {**records["R-1"], "type": "memo"}}
        with self.assertRaisesRegex(SnapshotValidationError, "expected route"):
            validate_assignment_portfolio(
                {"route": ["R-1"]},
                record_lookup=wrong_type.__getitem__,
            )
        inactive = {**records, "F-1": {**records["F-1"], "status": "revoked", "active": False}}
        with self.assertRaisesRegex(SnapshotValidationError, "inactive fact"):
            validate_assignment_portfolio(
                {"fact": ["F-1"]},
                record_lookup=inactive.__getitem__,
            )

        with self.assertRaisesRegex(
            SnapshotValidationError,
            r"^Store must provide get\(\) for portfolio validation$",
        ):
            validate_assignment_portfolio(
                {"fact": ["F-1"]},
                record_lookup=None,
            )

    def test_task_card_snapshot_resolution_keeps_order_dedup_and_proof_extra(self) -> None:
        looked_up: list[str] = []

        def load(memory_id: str) -> MemorySnapshot:
            looked_up.append(memory_id)
            memory_type = "fact" if memory_id.startswith("F-") else "route"
            return MemorySnapshot(memory_id, memory_type, memory_id, memory_id)

        result = assignment_snapshots_from_task_card(
            {
                "portfolio": {
                    "fact": ["F-1", "F-1"],
                    "route": ["R-1"],
                },
                "root_solution_fact_id": "F-root",
            },
            "proof-writer",
            snapshot_lookup=load,
        )
        self.assertEqual(looked_up, ["F-1", "R-1", "F-root"])
        self.assertEqual(
            [snapshot.memory_id for snapshot in result],
            ["F-1", "R-1", "F-root"],
        )

    def test_category_snapshot_uses_pinned_historical_revision_and_exact_union(self) -> None:
        historical = {
            "id": "CAT-1",
            "revision": 1,
            "status": "active",
            "members": _members(fact=["F-1"], route=["R-1"]),
        }
        current = {
            **historical,
            "revision": 2,
            "members": _members(fact=["F-1"], route=["R-2"]),
        }
        snapshot = {
            "snapshot_id": "CP-test",
            "revision": 7,
            "categories": [
                {
                    "category_id": "CAT-1",
                    "category_revision": 1,
                    "status": "active",
                    "current_revision": 2,
                }
            ],
            "flattened_members": _members(fact=["F-1"], route=["R-1"]),
            "base_event_id": 10,
            "confirmed_through_event_id": 12,
        }
        validate_category_portfolio_snapshot(
            snapshot,
            current_categories={"CAT-1": current},
            category_history={"CAT-1": [historical]},
            expected_portfolio_revision=7,
            expected_base_event_id=10,
            expected_confirmed_through_event_id=12,
        )

        malformed = copy.deepcopy(snapshot)
        malformed["flattened_members"]["route"] = ["R-2"]
        with self.assertRaisesRegex(SnapshotValidationError, "exactly equal"):
            validate_category_portfolio_snapshot(
                malformed,
                current_categories={"CAT-1": current},
                category_history={"CAT-1": [historical]},
            )
        stale_overlay = copy.deepcopy(snapshot)
        stale_overlay["categories"][0]["current_revision"] = 1
        with self.assertRaisesRegex(SnapshotValidationError, "overlay is stale"):
            validate_category_portfolio_snapshot(
                stale_overlay,
                current_categories={"CAT-1": current},
                category_history={"CAT-1": [historical]},
            )
        stale = copy.deepcopy(snapshot)
        stale["categories"][0]["category_revision"] = 3
        with self.assertRaisesRegex(SnapshotValidationError, "missing category revision"):
            validate_category_portfolio_snapshot(
                stale,
                current_categories={"CAT-1": current},
                category_history={"CAT-1": [historical]},
            )

    def test_event_window_validation_does_not_invent_an_authoritative_cursor(self) -> None:
        self.assertEqual(validate_event_window(4, 9), (4, 9))
        with self.assertRaisesRegex(SnapshotValidationError, "cannot precede"):
            validate_event_window(9, 4)
        with self.assertRaisesRegex(SnapshotValidationError, "base event mismatch"):
            validate_event_window(4, 9, expected_base_event_id=3)

    def test_category_composition_preserves_missing_category_overlay(self) -> None:
        snapshot = {
            "snapshot_id": "CP-missing",
            "revision": 1,
            "categories": [
                {"category_id": "CAT-missing", "category_revision": 1}
            ],
            "flattened_members": _members(fact=["F-1"]),
            "base_event_id": 0,
            "confirmed_through_event_id": 0,
        }
        categories = object.__new__(CategoryStore)
        categories._load = lambda: (
            1,
            {"categories": {}, "category_history": {}},
        )
        categories._member_entries = lambda members: {
            memory_type: [
                {
                    "id": memory_id,
                    "type": memory_type,
                    "status": "missing",
                    "active": False,
                }
                for memory_id in ids
            ]
            for memory_type, ids in members.items()
        }

        composed = categories._compose_portfolio(snapshot)
        self.assertEqual(composed["categories"][0]["status"], "missing")
        self.assertIsNone(composed["categories"][0]["current_revision"])

    def test_materializer_duplicate_rejection_is_unchanged(self) -> None:
        snapshot = MemorySnapshot("F-1", "fact", "a", "b")
        with tempfile.TemporaryDirectory() as raw:
            with self.assertRaisesRegex(MaterializationError, "duplicate portfolio ID"):
                WorkspaceMaterializer(Path(raw) / "workspaces").create(
                    "duplicate",
                    root_problem="Prove the theorem.",
                    policy=policy_for("worker", mode="research"),
                    portfolio=[snapshot, snapshot],
                )


if __name__ == "__main__":
    unittest.main()
