from __future__ import annotations

import hashlib
import sys
import tempfile
import unittest
from pathlib import Path


SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from franta.contracts.agent_access import MemoryRecord, policy_for  # noqa: E402
from franta.explorer.contracts import ExplorerReadScope, ExplorerWriteContext  # noqa: E402
from franta.explorer.repository import ExplorerRepository  # noqa: E402
from franta.read_access.audit import AuditLog  # noqa: E402
from franta.read_access.explorer import AuditedExplorerAPI  # noqa: E402
from franta.read_access.memory import AccessError, InMemoryBackend  # noqa: E402


def _receipt(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


class ExplorerSearchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.repository = ExplorerRepository(
            Path(self.temporary.name) / "explorer.sqlite3"
        )
        self.context = ExplorerWriteContext(
            turn_id="ETURN-search",
            worker_session_id="EWORK-search",
            attempt_no=1,
            call_id="CALL-search",
        )
        scratch_receipt = _receipt("search-scratch")
        self.scratch = self.repository.prepare_scratch(
            self.context,
            {
                "skill": "record-scratch",
                "operation_id": "OP-search-scratch",
                "record_id": "ES-search-scratch",
                "record_kind": "idea",
                "abstract": "Spectral filtration approach",
                "content": "Full provisional details of the filtration.",
                "related_memory_ids": ["F-1"],
                "cas_operation_ids": [],
            },
            scratch_receipt,
        )
        self.repository.trust_receipt(scratch_receipt)
        summary_receipt = _receipt("search-summary")
        self.summary = self.repository.prepare_summary(
            self.context,
            {
                "skill": "record-summary",
                "operation_id": "OP-search-summary",
                "record_id": "ESUM-search-summary",
                "abstract": "Spectral attempt summary",
                "content": "Complete attempt-final synthesis.",
                "directions_tried": ["Spectral filtration", "Dual obstruction"],
                "main_progress": "Located the only possible spectral differential.",
                "main_obstacles": "The differential may survive.",
                "source_scratch_ids": [self.scratch.record_id],
            },
            summary_receipt,
        )
        self.repository.trust_receipt(summary_receipt)
        self.backend = InMemoryBackend(
            [
                MemoryRecord(
                    "F-1",
                    "fact",
                    "Spectral degeneration lemma",
                    "Canonical proof details.",
                ),
                MemoryRecord(
                    "R-1",
                    "route",
                    "Unrelated deformation route",
                    "Canonical route details.",
                ),
                MemoryRecord(
                    "F-inactive",
                    "fact",
                    "Spectral historical failure",
                    "Superseded proof.",
                    active=False,
                ),
            ]
        )
        self.audit = AuditLog()
        self.api = AuditedExplorerAPI(
            self.backend,
            self.repository,
            policy_for("explorer-worker", mode="explore"),
            ExplorerReadScope(frozenset({self.context.turn_id})),
            audit=self.audit,
            caller_id="CALL-search",
        )

    def tearDown(self) -> None:
        self.repository.close()
        self.temporary.cleanup()

    def test_joint_abstract_search_labels_spaces_and_summary_fields(self) -> None:
        results = self.api.search(
            "spectral",
            ["fact", "scratch", "summary"],
            limit=10,
        )
        by_id = {result["id"]: result for result in results}
        self.assertEqual(
            set(by_id), {"F-1", self.scratch.record_id, self.summary.record_id}
        )
        self.assertEqual(by_id["F-1"]["record_space"], "canonical")
        self.assertEqual(by_id[self.scratch.record_id]["status"], "provisional")
        summary = by_id[self.summary.record_id]
        self.assertEqual(summary["record_type"], "summary")
        self.assertEqual(
            summary["directions_tried"], ["Spectral filtration", "Dual obstruction"]
        )
        self.assertEqual(
            summary["main_progress"],
            "Located the only possible spectral differential.",
        )
        self.assertNotIn("content", summary)
        self.assertEqual(self.audit.events[-1]["action"], "explorer_search")

    def test_fetch_dispatches_by_namespace_and_returns_full_records(self) -> None:
        canonical = self.api.fetch("F-1")
        scratch = self.api.fetch(self.scratch.record_id)
        summary = self.api.fetch(self.summary.record_id)

        self.assertEqual(canonical["record_space"], "canonical")
        self.assertEqual(canonical["content"], "Canonical proof details.")
        self.assertEqual(scratch["record_space"], "explorer")
        self.assertEqual(
            scratch["content"], "Full provisional details of the filtration."
        )
        self.assertEqual(summary["source_scratch_ids"], [self.scratch.record_id])
        self.assertEqual(self.audit.events[-1]["action"], "explorer_fetch")

    def test_fetch_derives_discoverable_xcas_identity_from_cited_operation(self) -> None:
        context = ExplorerWriteContext(
            turn_id=self.context.turn_id,
            worker_session_id=self.context.worker_session_id,
            attempt_no=2,
            call_id="CALL-search-cas",
        )
        cas_receipt = _receipt("search-cas")
        evidence = self.repository.prepare_cas_evidence(
            context,
            {
                "skill": "CAS",
                "operation_id": "CAS-search-operation",
                "software": "python",
                "software_version": "3.14",
                "exact_input": "print(6 * 7)",
                "exact_output": "42\n",
                "exit_status": 0,
                "description": "Evaluate a small exact example.",
                "assumptions": "Integer arithmetic.",
                "interpretation": "The example evaluates to forty-two.",
                "related_ids": {},
                "execution_succeeded": True,
            },
            cas_receipt,
        )
        self.repository.trust_receipt(cas_receipt)
        scratch_receipt = _receipt("search-cas-scratch")
        scratch = self.repository.prepare_scratch(
            context,
            {
                "skill": "record-scratch",
                "operation_id": "OP-search-cas-scratch",
                "record_id": "ES-search-cas-scratch",
                "record_kind": "computation",
                "abstract": "Exact value in the model example",
                "content": "The trusted computation returned 42.",
                "related_memory_ids": [],
                "cas_operation_ids": ["CAS-search-operation"],
            },
            scratch_receipt,
        )
        self.repository.trust_receipt(scratch_receipt)

        fetched = self.api.fetch(scratch.record_id)
        self.assertEqual(
            fetched["cas_evidence"],
            [
                {
                    "evidence_id": evidence.evidence_id,
                    "operation_id": "CAS-search-operation",
                    "execution_succeeded": True,
                    "turn_id": context.turn_id,
                    "worker_session_id": context.worker_session_id,
                    "attempt_no": 2,
                    "output_artifact_sha256": None,
                }
            ],
        )

    def test_high_water_and_session_scope_are_enforced_before_ranking(self) -> None:
        high_water = self.repository.visible_high_water()
        late_context = ExplorerWriteContext(
            turn_id=self.context.turn_id,
            worker_session_id="EWORK-late",
            attempt_no=1,
            call_id="CALL-late",
        )
        receipt = _receipt("late-search")
        late = self.repository.prepare_scratch(
            late_context,
            {
                "skill": "record-scratch",
                "operation_id": "OP-late-search",
                "record_id": "ES-late-search",
                "record_kind": "idea",
                "abstract": "Spectral late idea",
                "content": "Late content.",
                "related_memory_ids": [],
                "cas_operation_ids": [],
            },
            receipt,
        )
        self.repository.trust_receipt(receipt)
        scoped = AuditedExplorerAPI(
            self.backend,
            self.repository,
            policy_for("explorer-worker", mode="explore"),
            ExplorerReadScope(
                frozenset({self.context.turn_id}),
                frozenset({self.context.worker_session_id, late_context.worker_session_id}),
                max_seq=high_water,
                label="frozen-sort",
            ),
        )
        result_ids = {
            item["id"]
            for item in scoped.search("spectral", ["scratch", "summary"], limit=10)
        }
        self.assertNotIn(late.record_id, result_ids)
        with self.assertRaises(AccessError):
            scoped.fetch(late.record_id)

        other_session = AuditedExplorerAPI(
            self.backend,
            self.repository,
            policy_for("explorer-worker", mode="explore"),
            ExplorerReadScope(
                frozenset({self.context.turn_id}),
                frozenset({late_context.worker_session_id}),
            ),
        )
        with self.assertRaises(AccessError):
            other_session.fetch(self.scratch.record_id)

    def test_canonical_status_filter_and_policy_boundary(self) -> None:
        result_ids = {
            item["id"]
            for item in self.api.search("spectral", ["fact"], limit=10)
        }
        self.assertNotIn("F-inactive", result_ids)
        with self.assertRaises(AccessError):
            self.api.fetch("F-inactive")
        with self.assertRaises(AccessError):
            AuditedExplorerAPI(
                self.backend,
                self.repository,
                policy_for("main"),
                ExplorerReadScope(frozenset({self.context.turn_id})),
            )


if __name__ == "__main__":
    unittest.main()
