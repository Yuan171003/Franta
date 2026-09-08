from __future__ import annotations

import hashlib
import json
import stat
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from franta.contracts.canonical import MemoryType  # noqa: E402
from franta.read_access.main_memory_snapshot import (  # noqa: E402
    MainMemorySnapshotError,
    build_main_memory_snapshot,
    recover_main_memory_snapshot,
    validate_main_memory_snapshot,
)
from franta.read_access import main_memory_snapshot as snapshot_module  # noqa: E402
from franta.render import render_record  # noqa: E402
from franta.store import MemoryStore  # noqa: E402


class MainMemorySnapshotTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.database = self.root / "memory.sqlite3"
        self.store = MemoryStore(self.database, projection_dir=False)

    def tearDown(self) -> None:
        self.store.close()
        # Snapshot directories are intentionally 0555.  Restore directory
        # owner permissions so TemporaryDirectory can remove nested files on
        # every supported platform.
        for path in sorted(
            self.root.rglob("*"), key=lambda item: len(item.parts), reverse=True
        ):
            if path.is_dir() and not path.is_symlink():
                try:
                    path.chmod(0o700)
                except OSError:
                    pass
        self.temporary.cleanup()

    @staticmethod
    def _route_payload(**changes: object) -> dict[str, object]:
        payload: dict[str, object] = {
            "abstract": "Degenerate the target and inspect the limiting obstruction",
            "strategy_description": "Construct a semistable limiting family.",
            "value_assessment": {
                "confidence": "plausible",
                "success_gain": "settles the obstruction",
                "failure_gain": "isolates a monodromy failure",
                "relevance": "central",
                "novelty": "uses a limiting structure",
            },
            "progress": [],
            "related_obligation_ids": [],
            "next_steps": ["Construct the family."],
            "obstacles": ["Control specialization."],
            "active_fact_ids": [],
            "relevant_memo_ids": [],
            "relevant_claim_ids": [],
        }
        payload.update(changes)
        return payload

    @staticmethod
    def _obligation_payload(statement: str) -> dict[str, object]:
        return {
            "abstract": statement,
            "statement": statement,
            "importance": "It controls the central obstruction.",
            "predecessor_fact_ids": [],
            "partial_progress": [],
            "related_route_ids": [],
            "relations": [],
        }

    @staticmethod
    def _fact_payload(task_id: str, statement: str) -> dict[str, object]:
        return {
            "statement": statement,
            "proof": "This follows directly from the stated definitions.",
            "predecessor_fact_ids": [],
            "originating_task_id": task_id,
            "foundation_policy_version": 1,
            "introduced_notation": [],
            "external_references": [],
            "root_resolution": None,
            "abstract": statement,
            "keywords": ["snapshot"],
            "related_route_ids": [],
        }

    def _populate_store(self) -> dict[str, str]:
        task_id = self.store.allocate_id(MemoryType.TASK)
        root = self.store.add_obligation(
            "snapshot-root-obligation",
            self._obligation_payload("The limiting obstruction vanishes."),
        ).canonical_id
        assert root is not None
        self.store.set_root_obligation(root)
        removed_obligation = self.store.add_obligation(
            "snapshot-removed-obligation",
            self._obligation_payload("A discarded auxiliary condition holds."),
        ).canonical_id
        assert removed_obligation is not None
        route = self.store.add_route(
            "snapshot-route",
            self._route_payload(related_obligation_ids=[root]),
        ).canonical_id
        assert route is not None
        memo = self.store.add_memo(
            "snapshot-memo",
            {
                "abstract": "The weight filtration may detect the obstruction",
                "genre": "high-level",
                "content": "Compare weights before and after specialization.",
                "related_route_ids": [route],
            },
        ).canonical_id
        assert memo is not None
        claim = self.store.add_claim(
            "snapshot-claim",
            {
                "abstract": "A toy model has trivial obstruction",
                "content": "A direct toy calculation gives zero.",
                "related_route_ids": [route],
            },
        ).canonical_id
        assert claim is not None
        active_fact = self.store.add_fact(
            "snapshot-active-fact",
            self._fact_payload(task_id, "The base case holds."),
        ).canonical_id
        revoked_fact = self.store.add_fact(
            "snapshot-revoked-fact",
            self._fact_payload(task_id, "The obsolete base case holds."),
        ).canonical_id
        assert active_fact is not None and revoked_fact is not None
        computation = self.store.publish_computation(
            "snapshot-computation",
            {
                "task_id": task_id,
                "description": "Compute the toy obstruction.",
                "assumptions": "Characteristic zero.",
                "exact_input": "print(0)",
                "software": {"name": "Python", "version": "3.11"},
                "environment_versions": {},
                "random_seed": None,
                "output": "0",
                "exit_status": 0,
                "error_output": "",
                "interpretation": "The sampled obstruction vanishes.",
                "related_memory_ids": {
                    "fact": [active_fact],
                    "route": [route],
                    "memo": [memo],
                    "claim": [claim],
                    "obligation": [root],
                },
                "fact_candidate_operation_ids": [],
            },
        ).canonical_id
        assert computation is not None
        task = self.store.publish_task(
            "snapshot-task",
            {
                "id": task_id,
                "assign_record": {
                    "task_id": task_id,
                    "objective": "Study the toy obstruction.",
                },
                "final_status": "progress",
                "final_summary": "The toy computation suggests vanishing.",
                "artifact_references": [
                    {
                        "kind": "progress",
                        "path": "task-archive/private-progress.json",
                        "sha256": hashlib.sha256(b"private-progress").hexdigest(),
                    }
                ],
                "computation_ids": [computation],
            },
        ).canonical_id
        assert task == task_id

        self.store.remove_claim(
            "snapshot-withdraw-claim",
            {"target_id": claim, "reason": "Superseded toy calculation."},
        )
        self.store.remove_obligation(
            "snapshot-remove-obligation",
            {
                "target_id": removed_obligation,
                "reason": "No longer relevant.",
                "resolving_fact_ids": [],
                "refuting_fact_ids": [],
            },
        )
        self.store.revoke_fact(
            revoked_fact,
            reason="The proof used a false simplification.",
            actor="test",
            evidence={"kind": "unit-test"},
            operation_id="snapshot-revoke-fact",
        )
        return {
            "task": task_id,
            "root": root,
            "removed_obligation": removed_obligation,
            "route": route,
            "memo": memo,
            "claim": claim,
            "active_fact": active_fact,
            "revoked_fact": revoked_fact,
            "computation": computation,
        }

    def test_complete_snapshot_has_catalog_status_partitions_and_read_only_tree(self) -> None:
        ids = self._populate_store()
        snapshot = build_main_memory_snapshot(
            self.store,
            self.root / "workspace" / "input" / "main_memory_snapshot",
            source_event_cursor=37,
            snapshot_id="MMS-" + "a" * 32,
            created_at="2026-08-31T00:00:00.000000Z",
        )

        records = self.store.list_records(include_inactive=True)
        self.assertEqual(snapshot.record_count, len(records))
        self.assertEqual(snapshot.source_event_cursor, 37)
        manifest = json.loads(snapshot.manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(manifest["record_count"], len(records))
        self.assertEqual(set(manifest["type_counts"]), {kind.value for kind in MemoryType})
        self.assertTrue(all(manifest["type_counts"][kind.value] >= 1 for kind in MemoryType))

        catalog = [
            json.loads(line)
            for line in snapshot.catalog_path.read_text(encoding="utf-8").splitlines()
        ]
        by_id = {entry["id"]: entry for entry in catalog}
        self.assertEqual(by_id[ids["active_fact"]]["status"], "active")
        self.assertEqual(by_id[ids["revoked_fact"]]["status"], "revoked")
        self.assertEqual(by_id[ids["claim"]]["status"], "withdrawn")
        self.assertEqual(by_id[ids["removed_obligation"]]["status"], "removed")
        self.assertIn(
            "/facts/revoked/", "/" + by_id[ids["revoked_fact"]]["path"]
        )
        self.assertIn("only active facts", (snapshot.root / "README.md").read_text())

        for record in records:
            entry = by_id[record.memory_id]
            projected = snapshot.root / entry["path"]
            self.assertEqual(projected.read_text(encoding="utf-8"), render_record(record))
        for path in [snapshot.root, *snapshot.root.rglob("*")]:
            self.assertFalse(path.is_symlink())
            if path.is_dir():
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o555)
            else:
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o444)

        recovered = recover_main_memory_snapshot(
            snapshot.root,
            expected_snapshot_id=snapshot.snapshot_id,
            expected_snapshot_digest=snapshot.snapshot_digest,
        )
        self.assertEqual(recovered, snapshot)

    def test_validation_fails_closed_for_wrong_identity_tamper_and_symlink(self) -> None:
        self._populate_store()
        first = build_main_memory_snapshot(self.store, self.root / "first")
        with self.assertRaisesRegex(MainMemorySnapshotError, "persisted call input"):
            recover_main_memory_snapshot(
                first.root,
                expected_snapshot_id="MMS-" + "f" * 32,
                expected_snapshot_digest=first.snapshot_digest,
            )

        record = next((first.root / "records").rglob("*.md"))
        record.chmod(0o644)
        record.write_text(record.read_text(encoding="utf-8") + "tampered\n", encoding="utf-8")
        with self.assertRaises(MainMemorySnapshotError):
            validate_main_memory_snapshot(first.root)

        second = build_main_memory_snapshot(self.store, self.root / "second")
        second.root.chmod(0o755)
        (second.root / "untrusted-link").symlink_to("catalog.jsonl")
        second.root.chmod(0o555)
        with self.assertRaisesRegex(MainMemorySnapshotError, "symlink"):
            validate_main_memory_snapshot(second.root)

    def test_capture_is_one_store_state_even_when_another_connection_writes(self) -> None:
        ids = self._populate_store()
        second_store = MemoryStore(self.database, projection_dir=False)
        capture_ready = threading.Event()
        allow_capture_return = threading.Event()
        writer_started = threading.Event()
        writer_done = threading.Event()
        build_result: list[object] = []
        thread_errors: list[BaseException] = []
        original_list_records = self.store.list_records

        def paused_list_records(*args: object, **kwargs: object) -> object:
            result = original_list_records(*args, **kwargs)
            capture_ready.set()
            if not allow_capture_return.wait(timeout=5):
                raise RuntimeError("test timed out while pausing snapshot capture")
            return result

        def build() -> None:
            try:
                build_result.append(
                    build_main_memory_snapshot(self.store, self.root / "consistent")
                )
            except BaseException as exc:  # pragma: no cover - asserted below.
                thread_errors.append(exc)

        def update() -> None:
            try:
                writer_started.set()
                second_store.update_route(
                    "concurrent-route-update",
                    {
                        "target_id": ids["route"],
                        "expected_base_revision": 1,
                        "set": {"abstract": "A concurrently updated route"},
                        "append": {},
                        "add_ids": {},
                        "remove_ids": {},
                        "explanation": "Exercise the snapshot read transaction.",
                        "supporting_memory_ids": [],
                    },
                )
            except BaseException as exc:  # pragma: no cover - asserted below.
                thread_errors.append(exc)
            finally:
                writer_done.set()

        try:
            with patch.object(
                self.store, "list_records", side_effect=paused_list_records
            ):
                build_thread = threading.Thread(target=build)
                build_thread.start()
                self.assertTrue(capture_ready.wait(timeout=5))
                writer_thread = threading.Thread(target=update)
                writer_thread.start()
                self.assertTrue(writer_started.wait(timeout=5))
                # BEGIN IMMEDIATE held by the capture blocks this second store
                # from committing until the complete old record set is copied.
                self.assertFalse(writer_done.wait(timeout=0.1))
                allow_capture_return.set()
                build_thread.join(timeout=10)
                writer_thread.join(timeout=10)
                self.assertFalse(build_thread.is_alive())
                self.assertFalse(writer_thread.is_alive())
        finally:
            allow_capture_return.set()
            second_store.close()

        self.assertEqual(thread_errors, [])
        self.assertEqual(len(build_result), 1)
        snapshot = build_result[0]
        assert hasattr(snapshot, "catalog_path")
        catalog = {
            item["id"]: item
            for item in (
                json.loads(line)
                for line in snapshot.catalog_path.read_text(encoding="utf-8").splitlines()
            )
        }
        self.assertEqual(catalog[ids["route"]]["revision"], 1)
        self.assertEqual(self.store.get(ids["route"]).revision, 2)

    def test_existing_destination_is_never_overwritten(self) -> None:
        destination = self.root / "already-there"
        destination.mkdir()
        sentinel = destination / "sentinel"
        sentinel.write_text("keep", encoding="utf-8")
        with self.assertRaisesRegex(MainMemorySnapshotError, "already exists"):
            build_main_memory_snapshot(self.store, destination)
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep")

    def test_safe_non_uuid_canonical_ids_are_valid_snapshot_paths(self) -> None:
        relative = snapshot_module._record_relative_path(
            {"id": "F-ONE", "type": "fact", "status": "active"}
        )
        self.assertEqual(relative, Path("records/facts/active/F-ONE.md"))
        with self.assertRaises(MainMemorySnapshotError):
            snapshot_module._record_relative_path(
                {"id": "F-../escape", "type": "fact", "status": "active"}
            )


if __name__ == "__main__":
    unittest.main()
