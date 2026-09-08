from __future__ import annotations

import hashlib
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from franta.models import (  # noqa: E402
    AccessDeniedError,
    AccessPolicy,
    ConflictError,
    IdempotencyConflict,
    MemoryType,
)
from franta.render import render_record  # noqa: E402
from franta.search import SearchEngine  # noqa: E402
from franta.store import MemoryStore  # noqa: E402
from franta.access import AuditedMemoryAPI, policy_for  # noqa: E402


class StoreTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.store = MemoryStore(root / "memory.sqlite3", root / "projection")
        self.task_id = self.store.allocate_id(MemoryType.TASK)

    def tearDown(self) -> None:
        self.store.close()
        self.temp.cleanup()

    def fact_payload(
        self,
        statement: str,
        proof: str,
        *,
        predecessors: list[str] | None = None,
        abstract: str | None = None,
        root_resolution: dict[str, str] | None = None,
        routes: list[str] | None = None,
    ) -> dict[str, object]:
        return {
            "statement": statement,
            "proof": proof,
            "predecessor_fact_ids": predecessors or [],
            "originating_task_id": self.task_id,
            "foundation_policy_version": 1,
            "introduced_notation": [],
            "external_references": [],
            "root_resolution": root_resolution,
            "abstract": abstract or statement,
            "keywords": ["algebraic", "geometry"],
            "related_route_ids": routes or [],
        }

    @staticmethod
    def route_payload(**changes: object) -> dict[str, object]:
        payload: dict[str, object] = {
            "abstract": "Degeneration route for the target cycle",
            "strategy_description": "Degenerate the target to a normal-crossings model.",
            "value_assessment": {
                "confidence": "plausible after semistable reduction",
                "success_gain": "would resolve the main obstruction",
                "failure_gain": "would isolate a monodromy obstruction",
                "relevance": "addresses the central target",
                "novelty": "uses a new limiting mixed structure",
            },
            "progress": [],
            "related_obligation_ids": [],
            "next_steps": ["Construct the limiting family."],
            "obstacles": ["Control specialization."],
            "active_fact_ids": [],
            "relevant_memo_ids": [],
            "relevant_claim_ids": [],
        }
        payload.update(changes)
        return payload

    @staticmethod
    def obligation_payload(**changes: object) -> dict[str, object]:
        payload: dict[str, object] = {
            "abstract": "Vanishing of the limiting obstruction",
            "statement": "For every admissible degeneration, the limiting obstruction vanishes.",
            "importance": "This removes the central obstruction.",
            "predecessor_fact_ids": [],
            "partial_progress": [],
            "related_route_ids": [],
            "relations": [],
        }
        payload.update(changes)
        return payload

    def test_wal_ids_seven_types_and_deterministic_projections(self) -> None:
        self.assertEqual(self.store.journal_mode, "wal")
        root = self.store.add_obligation("op-root", self.obligation_payload())
        self.assertEqual(root.status, "committed")
        self.store.set_root_obligation(root.canonical_id)
        route = self.store.add_route("op-route", self.route_payload())
        memo = self.store.add_memo(
            "op-memo",
            {
                "abstract": "Semistable reduction may expose the obstruction",
                "genre": "high-level",
                "content": "Compare the weight filtration before and after specialization.",
                "related_route_ids": [route.canonical_id],
            },
        )
        claim = self.store.add_claim(
            "op-claim",
            {
                "abstract": "The toy model has trivial obstruction",
                "content": "A direct calculation proves the toy-model assertion.",
                "related_route_ids": [route.canonical_id],
            },
        )
        fact = self.store.add_fact(
            "op-fact",
            self.fact_payload("The base case holds.", "This follows directly from the definitions."),
        )
        computation = self.store.publish_computation(
            "op-computation",
            {
                "task_id": self.task_id,
                "description": "Compute the obstruction in the two-dimensional toy model.",
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
                    "fact": [fact.canonical_id],
                    "route": [route.canonical_id],
                    "memo": [memo.canonical_id],
                    "claim": [claim.canonical_id],
                    "obligation": [root.canonical_id],
                },
                "fact_candidate_operation_ids": ["candidate-op-1"],
            },
        )
        task = self.store.publish_task(
            "op-task-close",
            {
                "id": self.task_id,
                "assign_record": {"task_id": self.task_id, "objective": "Study the toy model."},
                "final_status": "progress",
                "final_summary": "The toy computation suggests vanishing.",
                "artifact_references": [
                    {
                        "kind": "progress",
                        "path": "archive/progress-1.json",
                        "sha256": hashlib.sha256(b"progress").hexdigest(),
                    }
                ],
                "computation_ids": [computation.canonical_id],
            },
        )
        records = self.store.list_records()
        self.assertEqual({record.memory_type for record in records}, set(MemoryType))
        self.assertTrue(fact.canonical_id.startswith("F-"))
        self.assertTrue(claim.canonical_id.startswith("CL-"))
        self.assertTrue(computation.canonical_id.startswith("C-"))
        self.assertEqual(task.canonical_id, self.task_id)
        route_record = self.store.get(route.canonical_id)
        self.assertEqual(route_record["relevant_memo_ids"], [memo.canonical_id])
        self.assertEqual(route_record["relevant_claim_ids"], [claim.canonical_id])
        self.assertEqual(
            self.store.get(memo.canonical_id)["related_route_ids"], [route.canonical_id]
        )
        projection = self.store.projection_path(fact.canonical_id)
        self.assertIsNotNone(projection)
        assert projection is not None
        self.assertEqual(projection.read_text(encoding="utf-8"), render_record(self.store.get(fact.canonical_id)))
        before = projection.read_bytes()
        self.store.rebuild_projections()
        self.assertEqual(before, projection.read_bytes())

    def test_idempotency_patch_revision_and_immutable_fact_core(self) -> None:
        route = self.store.add_route("route-add", self.route_payload(), proposal_id="P-route")
        replay = self.store.add_route("route-add", self.route_payload(), proposal_id="P-route")
        self.assertTrue(replay.replayed)
        self.assertEqual(route.canonical_id, replay.canonical_id)
        changed = self.route_payload(abstract="Different")
        with self.assertRaises(IdempotencyConflict):
            self.store.add_route("route-add", changed, proposal_id="P-route")
        update = self.store.update_route(
            "route-update",
            {
                "target_id": route.canonical_id,
                "expected_base_revision": 1,
                "set": {"abstract": "Degeneration and monodromy route"},
                "append": {"progress": ["Constructed a candidate family."]},
                "add_ids": {},
                "remove_ids": {},
                "explanation": "The family sharpens the same mechanism.",
                "supporting_memory_ids": [],
            },
        )
        self.assertEqual(update.status, "committed")
        self.assertEqual(self.store.get(route.canonical_id).revision, 2)
        stale = self.store.update_route(
            "route-stale",
            {
                "target_id": route.canonical_id,
                "expected_base_revision": 1,
                "set": {"abstract": "Stale overwrite"},
                "append": {},
                "add_ids": {},
                "remove_ids": {},
                "explanation": "Stale test.",
                "supporting_memory_ids": [],
            },
        )
        self.assertEqual(stale.status, "rejected")
        fact = self.store.add_fact(
            "fact-add",
            self.fact_payload("A rigid statement.", "The definitions give the assertion."),
        )
        before = self.store.get(fact.canonical_id)
        metadata = self.store.update_fact_metadata(
            "fact-meta",
            fact.canonical_id,
            expected_metadata_version=1,
            abstract="Rigid statement with a refined searchable abstract",
            keywords=["rigidity"],
        )
        self.assertEqual(metadata.status, "committed")
        after = self.store.get(fact.canonical_id)
        self.assertEqual(before["proof"], after["proof"])
        self.assertEqual(before["statement"], after["statement"])
        self.assertEqual(after.metadata_version, 2)

    def test_atomic_nonfact_cycle_and_proposal_mappings(self) -> None:
        operations = [
            {
                "operation_id": "cycle-route-op",
                "operation_type": "route_add",
                "proposal_id": "TEMP-ROUTE",
                "payload": self.route_payload(relevant_memo_ids=["TEMP-MEMO"]),
            },
            {
                "operation_id": "cycle-memo-op",
                "operation_type": "memo",
                "proposal_id": "TEMP-MEMO",
                "payload": {
                    "abstract": "Memo attached to the cyclically proposed route",
                    "genre": "normal",
                    "content": "This is an immature specialization idea.",
                    "related_route_ids": ["TEMP-ROUTE"],
                },
            },
        ]
        results = self.store.apply_operation_group("group-cycle", operations)
        self.assertEqual([item.status for item in results], ["committed", "committed"])
        route_id = self.store.proposal_mapping("TEMP-ROUTE")["canonical_id"]
        memo_id = self.store.proposal_mapping("TEMP-MEMO")["canonical_id"]
        self.assertEqual(self.store.get(route_id)["relevant_memo_ids"], [memo_id])
        self.assertEqual(self.store.get(memo_id)["related_route_ids"], [route_id])
        replay = self.store.apply_operation_group("group-cycle", operations)
        self.assertTrue(all(item.replayed for item in replay))

    def test_search_is_abstract_first_filtered_and_audited(self) -> None:
        first = self.store.add_memo(
            "memo-one",
            {
                "abstract": "Fourier transform controls the spectral obstruction",
                "genre": "high-level",
                "content": "Secret full details are not part of a search result.",
                "related_route_ids": [],
            },
        )
        second = self.store.add_memo(
            "memo-two",
            {
                "abstract": "A combinatorial boundary calculation",
                "genre": "normal",
                "content": "Unrelated content.",
                "related_route_ids": [],
            },
        )
        engine = SearchEngine(self.store)
        policy = AccessPolicy.sealed([first.canonical_id], label="sealed-test")
        results = engine.search(
            "Fourier spectral",
            types=["memo"],
            actor="worker-T",
            access_policy=policy,
        )
        self.assertEqual([item.memory_id for item in results], [first.canonical_id])
        self.assertFalse(hasattr(results[0], "content"))
        fetched = engine.fetch(first.canonical_id, actor="worker-T", access_policy=policy)
        self.assertIn("Secret full details", fetched["content"])
        with self.assertRaises(AccessDeniedError):
            engine.fetch(second.canonical_id, actor="worker-T", access_policy=policy)
        audit = self.store.list_read_audit()
        self.assertEqual([entry["action"] for entry in audit], ["search", "fetch", "fetch"])
        self.assertFalse(audit[-1]["allowed"])

    def test_transitive_revocation_overlays_and_root_reopening(self) -> None:
        root = self.store.add_obligation("root-obligation", self.obligation_payload())
        self.store.set_root_obligation(root.canonical_id)
        base = self.store.add_fact(
            "base-fact",
            self.fact_payload("The base lemma holds.", "This is immediate."),
        )
        child = self.store.add_fact(
            "child-fact",
            self.fact_payload(
                "The root problem is solved.",
                f"Apply {base.canonical_id} to the defining family.",
                predecessors=[base.canonical_id],
                root_resolution={"target": "ROOT", "outcome": "proved"},
            ),
        )
        route = self.store.add_route(
            "support-route",
            self.route_payload(active_fact_ids=[child.canonical_id]),
        )
        obligation = self.store.add_obligation(
            "dependent-obligation",
            self.obligation_payload(predecessor_fact_ids=[child.canonical_id]),
        )
        self.assertEqual(self.store.root_resolution_state()["status"], "resolved")
        self.assertEqual(self.store.get(root.canonical_id).status, "resolved")
        revoked = self.store.revoke_fact(
            base.canonical_id,
            reason="The base proof omitted a boundary case.",
            actor="verifier",
            evidence={"report": "challenge-1"},
            operation_id="revoke-base",
        )
        self.assertEqual(revoked.revoked_fact_ids, (base.canonical_id, child.canonical_id))
        self.assertEqual(self.store.get(base.canonical_id).status, "revoked")
        self.assertEqual(self.store.get(child.canonical_id).status, "revoked")
        self.assertEqual(self.store.get(obligation.canonical_id).status, "unsupported")
        self.assertEqual(self.store.get(route.canonical_id)["active_fact_ids"], [])
        self.assertTrue(revoked.root_resolution_cleared)
        self.assertEqual(self.store.root_resolution_state()["status"], "open")
        self.assertEqual(self.store.get(root.canonical_id).status, "active")
        history = self.store.status_history(child.canonical_id)
        self.assertEqual(history[-1]["dependency_path"], [base.canonical_id, child.canonical_id])
        replay = self.store.revoke_fact(
            base.canonical_id,
            reason="The base proof omitted a boundary case.",
            actor="verifier",
            evidence={"report": "challenge-1"},
            operation_id="revoke-base",
        )
        self.assertTrue(replay.replayed)

    def test_root_first_wins_same_outcome_and_opposite_freezes(self) -> None:
        root = self.store.add_obligation("root", self.obligation_payload())
        self.store.set_root_obligation(root.canonical_id)
        first = self.store.add_fact(
            "root-first",
            self.fact_payload(
                "ROOT holds by argument A.",
                "Argument A proves the assertion.",
                root_resolution={"target": "ROOT", "outcome": "proved"},
            ),
        )
        second = self.store.add_fact(
            "root-second",
            self.fact_payload(
                "ROOT holds by argument B.",
                "Argument B proves the assertion.",
                root_resolution={"target": "ROOT", "outcome": "proved"},
            ),
        )
        state = self.store.root_resolution_state()
        self.assertEqual(state["root_solution_fact_id"], first.canonical_id)
        self.assertEqual(state["status"], "resolved")
        opposite = self.store.add_fact(
            "root-opposite",
            self.fact_payload(
                "ROOT is false by counterexample C.",
                "Counterexample C violates the conclusion.",
                root_resolution={"target": "ROOT", "outcome": "disproved"},
            ),
        )
        self.assertEqual(opposite.status, "committed")
        state = self.store.root_resolution_state()
        self.assertEqual(state["root_solution_fact_id"], first.canonical_id)
        self.assertEqual(state["status"], "needs_attention")
        self.assertEqual(self.store.get(root.canonical_id).status, "resolution_conflict")
        self.assertEqual(len(state["resolutions"]), 3)
        self.assertFalse(state["resolutions"][1]["is_primary"])

    def test_revoking_primary_root_proof_promotes_active_same_outcome_alternate(self) -> None:
        root = self.store.add_obligation("root-promote", self.obligation_payload())
        self.store.set_root_obligation(root.canonical_id)
        first = self.store.add_fact(
            "root-proof-a",
            self.fact_payload(
                "ROOT holds.",
                "Independent argument A proves ROOT.",
                root_resolution={"target": "ROOT", "outcome": "proved"},
            ),
        )
        second = self.store.add_fact(
            "root-proof-b",
            self.fact_payload(
                "ROOT holds by another proof.",
                "Independent argument B proves ROOT.",
                root_resolution={"target": "ROOT", "outcome": "proved"},
            ),
        )
        revoked = self.store.revoke_fact(
            first.canonical_id,
            reason="Argument A has a fatal gap.",
            actor="scheduler",
            operation_id="revoke-root-proof-a",
        )
        state = self.store.root_resolution_state()
        self.assertFalse(revoked.root_resolution_cleared)
        self.assertEqual(state["root_solution_fact_id"], second.canonical_id)
        self.assertEqual(state["root_resolution_outcome"], "proved")
        self.assertEqual(state["status"], "resolved")
        resolutions = {item["fact_id"]: item for item in state["resolutions"]}
        self.assertTrue(resolutions[second.canonical_id]["is_primary"])
        self.assertEqual(self.store.get(root.canonical_id).status, "resolved")

    def test_removals_keep_history_and_control_state_cas(self) -> None:
        claim = self.store.add_claim(
            "claim",
            {
                "abstract": "A disposable unverified claim",
                "content": "Proof sketch.",
                "related_route_ids": [],
            },
        )
        removed = self.store.remove_claim(
            "claim-remove",
            {"target_id": claim.canonical_id, "reason": "Superseded"},
        )
        self.assertEqual(removed.status, "committed")
        self.assertEqual(self.store.get(claim.canonical_id).status, "withdrawn")
        self.assertTrue(self.store.status_history(claim.canonical_id))
        revision = self.store.compare_and_swap_control_state(
            "scheduler", None, {"gate": "open", "cursor": 1}
        )
        self.assertEqual(revision, 1)
        self.assertEqual(
            self.store.load_control_state("scheduler"),
            (1, {"cursor": 1, "gate": "open"}),
        )
        revision = self.store.compare_and_swap_control_state(
            "scheduler", 1, {"gate": "trimming", "cursor": 2}
        )
        self.assertEqual(revision, 2)
        with self.assertRaises(ConflictError):
            self.store.compare_and_swap_control_state(
                "scheduler", 1, {"gate": "open"}
            )
        self.assertTrue(self.store.append_control_event("event-1", {"kind": "wake"}))
        self.assertFalse(self.store.append_control_event("event-1", {"kind": "wake"}))
        with self.assertRaises(IdempotencyConflict):
            self.store.append_control_event("event-1", {"kind": "other"})

    def test_access_backend_adapter_and_category_id(self) -> None:
        memo = self.store.add_memo(
            "adapter-memo",
            {
                "abstract": "Derived-category Fourier transform idea",
                "genre": "normal",
                "content": "Try the transform on the obstruction complex.",
                "related_route_ids": [],
            },
        )
        api = AuditedMemoryAPI(
            self.store.as_memory_backend(),
            policy_for("worker", mode="research"),
            caller_id="adapter-test",
        )
        summaries = api.search("Fourier", ["memo"])
        self.assertEqual([item["id"] for item in summaries], [memo.canonical_id])
        full = api.fetch(memo.canonical_id)
        self.assertIn("obstruction complex", full["content"])
        category_id = self.store.allocate_id("category")
        self.assertTrue(category_id.startswith("CAT-"))


if __name__ == "__main__":
    unittest.main()
