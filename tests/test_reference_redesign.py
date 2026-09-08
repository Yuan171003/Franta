from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from typing import Any, Mapping

from franta.models import MemoryType
from franta.scheduler import Scheduler
from franta.store import MemoryStore
from franta.testing import FakeControlStore
from franta.workflows import OperationState, TaskState


def _route_payload(**changes: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "abstract": "A route used by the reference-redesign regression tests.",
        "strategy_description": "Develop the reduction directly.",
        "value_assessment": {
            "confidence": "plausible",
            "success_gain": "would settle the local step",
            "failure_gain": "would expose the obstruction",
            "relevance": "central",
            "novelty": "a distinct reduction",
        },
        "progress": [],
        "related_obligation_ids": [],
        "next_steps": [],
        "obstacles": [],
        "active_fact_ids": [],
        "relevant_memo_ids": [],
        "relevant_claim_ids": [],
    }
    payload.update(changes)
    return payload


def _memo_payload(**changes: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "abstract": "A memo with structured soft references.",
        "genre": "normal",
        "content": "The mathematical content is independent of those links.",
        "related_route_ids": [],
    }
    payload.update(changes)
    return payload


def _obligation_payload(**changes: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "abstract": "An obligation with a structured relation.",
        "statement": "Establish the remaining local assertion.",
        "importance": "It closes the local step.",
        "predecessor_fact_ids": [],
        "partial_progress": [],
        "related_route_ids": [],
        "relations": [],
    }
    payload.update(changes)
    return payload


def _computation_payload(
    task_id: str, related_memory_ids: Mapping[str, list[str]]
) -> dict[str, object]:
    return {
        "task_id": task_id,
        "description": "Evaluate an exact reference-redesign toy case.",
        "assumptions": "None.",
        "exact_input": "print(0)",
        "software": {"name": "Python", "version": "3.14"},
        "environment_versions": {},
        "random_seed": None,
        "output": "0",
        "exit_status": 0,
        "error_output": "",
        "interpretation": "The exact toy output is zero.",
        "related_memory_ids": dict(related_memory_ids),
        "fact_candidate_operation_ids": [],
    }


def _portfolio() -> dict[str, list[str]]:
    return {
        kind: []
        for kind in (
            "fact",
            "route",
            "memo",
            "claim",
            "obligation",
            "computation",
        )
    }


def _research_report(index: int, route_id: str) -> dict[str, Any]:
    return {
        "report_id": f"AR-REFERENCE-REDESIGN-{index}",
        "objective": "Exercise the redesigned reference contract.",
        "if_resume": None,
        "mode": "research",
        "main_route_ids": [route_id],
        "main_obligation_ids": [],
        "perspective": None,
        "portfolio": _portfolio(),
        "reason": "Reference semantics need an end-to-end regression.",
    }


def _progress(
    task_id: str,
    attempt: int,
    operations: list[dict[str, Any]],
    *,
    sequence: int = 1,
    final: bool = False,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "progress_id": f"P-REFERENCE-{task_id}-{sequence}",
        "task_id": task_id,
        "attempt": attempt,
        "sequence": sequence,
        "is_final": final,
        "operations": operations,
    }
    if final:
        payload.update(
            {
                "outcome_status": "progress",
                "completion_evidence_ids": [],
                "attempt_summary": {
                    "summary": "The source memory was published.",
                    "proposed_outcome": "progress",
                    "completion_evidence_operation_ids": [],
                },
            }
        )
    return payload


def _fact_operation(
    operation_id: str,
    proposal_id: str,
    *,
    predecessors: list[str] | None = None,
    statement: str | None = None,
    proof: str | None = None,
) -> dict[str, Any]:
    return {
        "operation_id": operation_id,
        "kind": "fact",
        "proposal_id": proposal_id,
        "candidate_id": f"FC-{operation_id}",
        "candidate_version": 1,
        "statement": statement or f"The assertion of {operation_id} holds.",
        "proof": proof or "A complete direct argument.",
        "predecessor_fact_ids": list(predecessors or []),
        "abstract": f"The assertion of {operation_id}.",
        "keywords": ["reference regression"],
        "introduced_notation": [],
        "external_references": [],
        "related_route_ids": [],
    }


def _verifier_report(
    bundle: Mapping[str, Any],
    verdict: str,
    *,
    errors: list[object] | None = None,
) -> dict[str, Any]:
    return {
        "verdict": verdict,
        "candidate_id": bundle["candidate_id"],
        "candidate_version": bundle["candidate_version"],
        "operation_id": bundle["operation_id"],
        "bundle_digest": bundle["bundle_digest"],
        "verifier_attempt_id": bundle["verifier_attempt_id"],
        "predecessor_ids": bundle["predecessor_ids"],
        "introduced_notation": bundle["introduced_notation"],
        "external_references": bundle["external_references"],
        "root_resolution": bundle["root_resolution"],
        "errors": list(errors or []),
    }


class SoftReferenceProjectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temporary.name) / "memory.sqlite3"
        self.store = MemoryStore(self.db_path, projection_dir=False)

    def tearDown(self) -> None:
        self.store.close()
        self.temporary.cleanup()

    def test_simple_list_slots_show_raw_pending_marker_terminal_and_canonical_success(
        self,
    ) -> None:
        result = self.store.add_memo(
            "soft-list-source",
            _memo_payload(
                related_route_ids=[
                    "TMP-REJECTED-ROUTE",
                    "TMP-ABANDONED-ROUTE",
                    "TMP-PUBLISHED-ROUTE",
                ]
            ),
        )
        self.assertEqual(result.status, "committed")
        source_id = result.canonical_id
        assert source_id is not None
        self.assertEqual(
            self.store.get(source_id)["related_route_ids"],
            [
                "TMP-REJECTED-ROUTE",
                "TMP-ABANDONED-ROUTE",
                "TMP-PUBLISHED-ROUTE",
            ],
        )

        self.store.reject_temporary_reference(
            "TMP-REJECTED-ROUTE",
            MemoryType.ROUTE,
            reason="The target proposal failed validation.",
            operation_id="reject-soft-route",
        )
        self.store.abandon_temporary_reference(
            "TMP-ABANDONED-ROUTE",
            reason="The target proposal was withdrawn.",
            operation_id="abandon-soft-route",
        )
        published = self.store.add_route(
            "publish-soft-route",
            _route_payload(),
            proposal_id="TMP-PUBLISHED-ROUTE",
        )
        published_id = published.canonical_id
        assert published_id is not None
        self.assertEqual(
            self.store.get(source_id)["related_route_ids"],
            [
                "TMP-REJECTED-ROUTE(unpublished)",
                "TMP-ABANDONED-ROUTE(unpublished)",
                published_id,
            ],
        )

    def test_nested_relation_premise_and_conclusion_keep_terminal_markers_in_place(
        self,
    ) -> None:
        source = self.store.add_obligation(
            "soft-relation-terminal-source",
            _obligation_payload(
                relations=[
                    {
                        "relation_type": "suffices_for",
                        "premise_memory_ids": ["TMP-REJECTED-PREMISE"],
                        "conclusion": "TMP-ABANDONED-CONCLUSION",
                        "explanation": "The premise would imply the conclusion.",
                        "supporting_fact_ids": [],
                    }
                ]
            ),
        )
        source_id = source.canonical_id
        assert source_id is not None
        relation = self.store.get(source_id)["relations"][0]
        self.assertEqual(relation["premise_memory_ids"], ["TMP-REJECTED-PREMISE"])
        self.assertEqual(relation["conclusion"], "TMP-ABANDONED-CONCLUSION")

        self.store.reject_temporary_reference(
            "TMP-REJECTED-PREMISE",
            MemoryType.OBLIGATION,
            reason="The premise proposal failed validation.",
            operation_id="reject-relation-premise",
        )
        self.store.abandon_temporary_reference(
            "TMP-ABANDONED-CONCLUSION",
            reason="The conclusion proposal was withdrawn.",
            operation_id="abandon-relation-conclusion",
        )
        relation = self.store.get(source_id)["relations"][0]
        self.assertEqual(
            relation["premise_memory_ids"],
            ["TMP-REJECTED-PREMISE(unpublished)"],
        )
        self.assertEqual(
            relation["conclusion"], "TMP-ABANDONED-CONCLUSION(unpublished)"
        )

    def test_nested_relation_premise_and_conclusion_resolve_in_place(self) -> None:
        source = self.store.add_obligation(
            "soft-relation-success-source",
            _obligation_payload(
                relations=[
                    {
                        "relation_type": "reduces_to",
                        "premise_memory_ids": ["TMP-PUBLISHED-PREMISE"],
                        "conclusion": "TMP-PUBLISHED-CONCLUSION",
                        "explanation": "The first obligation reduces to the second.",
                        "supporting_fact_ids": [],
                    }
                ]
            ),
        )
        source_id = source.canonical_id
        assert source_id is not None

        premise = self.store.add_obligation(
            "publish-relation-premise",
            _obligation_payload(statement="Establish the premise."),
            proposal_id="TMP-PUBLISHED-PREMISE",
        )
        premise_id = premise.canonical_id
        assert premise_id is not None
        relation = self.store.get(source_id)["relations"][0]
        self.assertEqual(relation["premise_memory_ids"], [premise_id])
        self.assertEqual(relation["conclusion"], "TMP-PUBLISHED-CONCLUSION")

        conclusion = self.store.add_obligation(
            "publish-relation-conclusion",
            _obligation_payload(statement="Establish the conclusion."),
            proposal_id="TMP-PUBLISHED-CONCLUSION",
        )
        conclusion_id = conclusion.canonical_id
        assert conclusion_id is not None
        relation = self.store.get(source_id)["relations"][0]
        self.assertEqual(relation["premise_memory_ids"], [premise_id])
        self.assertEqual(relation["conclusion"], conclusion_id)

    def test_computation_nested_related_ids_are_soft_views_with_idempotent_versions(
        self,
    ) -> None:
        task_id = self.store.allocate_id(MemoryType.TASK)
        source = self.store.publish_computation(
            "soft-computation-source",
            _computation_payload(
                task_id,
                {
                    "route": [
                        "TMP-COMPUTATION-REJECTED-ROUTE",
                        "TMP-COMPUTATION-ABANDONED-ROUTE",
                        "TMP-COMPUTATION-PUBLISHED-ROUTE",
                    ]
                },
            ),
        )
        self.assertEqual(source.status, "committed")
        source_id = source.canonical_id
        assert source_id is not None
        record = self.store.get(source_id)
        self.assertEqual(record.metadata_version, 1)
        self.assertEqual(
            record["related_memory_ids"]["route"],
            [
                "TMP-COMPUTATION-REJECTED-ROUTE",
                "TMP-COMPUTATION-ABANDONED-ROUTE",
                "TMP-COMPUTATION-PUBLISHED-ROUTE",
            ],
        )
        with closing(sqlite3.connect(self.db_path)) as connection:
            core_json = connection.execute(
                "SELECT core_json FROM memories WHERE memory_id=?", (source_id,)
            ).fetchone()[0]
        self.assertNotIn("TMP-", core_json)

        reject_arguments = {
            "temporary_id": "TMP-COMPUTATION-REJECTED-ROUTE",
            "expected_type": MemoryType.ROUTE,
            "reason": "The computation's related route failed validation.",
            "operation_id": "reject-computation-route",
        }
        self.store.reject_temporary_reference(**reject_arguments)
        record = self.store.get(source_id)
        self.assertEqual(record.metadata_version, 2)
        self.assertEqual(
            record["related_memory_ids"]["route"],
            [
                "TMP-COMPUTATION-REJECTED-ROUTE(unpublished)",
                "TMP-COMPUTATION-ABANDONED-ROUTE",
                "TMP-COMPUTATION-PUBLISHED-ROUTE",
            ],
        )
        self.store.reject_temporary_reference(**reject_arguments)
        self.assertEqual(self.store.get(source_id).metadata_version, 2)

        abandon_arguments = {
            "temporary_id": "TMP-COMPUTATION-ABANDONED-ROUTE",
            "reason": "The computation's related route was withdrawn.",
            "operation_id": "abandon-computation-route",
        }
        self.store.abandon_temporary_reference(**abandon_arguments)
        record = self.store.get(source_id)
        self.assertEqual(record.metadata_version, 3)
        self.assertEqual(
            record["related_memory_ids"]["route"],
            [
                "TMP-COMPUTATION-REJECTED-ROUTE(unpublished)",
                "TMP-COMPUTATION-ABANDONED-ROUTE(unpublished)",
                "TMP-COMPUTATION-PUBLISHED-ROUTE",
            ],
        )
        self.store.abandon_temporary_reference(**abandon_arguments)
        self.assertEqual(self.store.get(source_id).metadata_version, 3)

        published = self.store.add_route(
            "publish-computation-route",
            _route_payload(),
            proposal_id="TMP-COMPUTATION-PUBLISHED-ROUTE",
        )
        published_id = published.canonical_id
        assert published_id is not None
        record = self.store.get(source_id)
        self.assertEqual(record.metadata_version, 4)
        self.assertEqual(
            record["related_memory_ids"]["route"],
            [
                "TMP-COMPUTATION-REJECTED-ROUTE(unpublished)",
                "TMP-COMPUTATION-ABANDONED-ROUTE(unpublished)",
                published_id,
            ],
        )
        replay = self.store.add_route(
            "publish-computation-route",
            _route_payload(),
            proposal_id="TMP-COMPUTATION-PUBLISHED-ROUTE",
        )
        self.assertTrue(replay.replayed)
        self.assertEqual(self.store.get(source_id).metadata_version, 4)
        with closing(sqlite3.connect(self.db_path)) as connection:
            core_json = connection.execute(
                "SELECT core_json FROM memories WHERE memory_id=?", (source_id,)
            ).fetchone()[0]
        self.assertNotIn("TMP-", core_json)


class SchedulerFactReferenceRedesignTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = FakeControlStore()
        self.store.records["R-REFERENCE"] = {
            "id": "R-REFERENCE",
            "type": "route",
            "active": True,
            "status": "active",
        }
        self.scheduler = Scheduler(self.store)
        self.scheduler.bootstrap(root_problem="Prove ROOT.")
        self.scheduler.commit_initial_trim({"category_ids": []})

    def _start_task(self, index: int) -> tuple[str, int]:
        task_id = self.scheduler.submit_batch(
            f"B-REFERENCE-{index}", [_research_report(index, "R-REFERENCE")]
        )[0]
        return task_id, self.scheduler.start_task_attempt(task_id)

    def _finish_fact(
        self,
        operation_id: str,
        *,
        verdict: str = "correct",
    ) -> str | None:
        operation = self.scheduler.state["operations"][operation_id]
        self.assertEqual(
            operation["state"], OperationState.SYNTHESIZING.value, operation
        )
        self.scheduler.apply_synthesizer_result(
            operation_id,
            {
                "resolution": "new",
                "operation_digest": operation["input_digest"],
            },
        )
        bundle = self.scheduler.verification_bundle(operation_id)
        errors: list[object] = [] if verdict == "correct" else ["A fatal proof gap."]
        canonical_id = self.scheduler.apply_verifier_report(
            operation_id,
            _verifier_report(bundle, verdict, errors=errors),
        )
        if canonical_id is not None:
            # FakeControlStore intentionally persists the exact operation body
            # and therefore does not synthesize the canonical record envelope.
            self.store.records[canonical_id].update(
                {"type": "fact", "active": True, "status": "active"}
            )
        return canonical_id

    def test_only_predecessor_list_normalizes_body_verbatim_and_uncited_canonical_publishes(
        self,
    ) -> None:
        task_id, attempt = self._start_task(1)
        self.scheduler.ingest_progress(
            _progress(
                task_id,
                attempt,
                [_fact_operation("fact-a", "TMP-FACT-A")],
            )
        )

        statement = "The historical label TMP-FACT-A remains in this statement."
        proof = "The complete argument keeps TMP-FACT-A verbatim in its prose."
        self.scheduler.ingest_progress(
            _progress(
                task_id,
                attempt,
                [
                    _fact_operation(
                        "fact-b",
                        "TMP-FACT-B",
                        predecessors=["TMP-FACT-A"],
                        statement=statement,
                        proof=proof,
                    )
                ],
                sequence=2,
            )
        )

        uncited_proof = "A complete argument with no identifier token in its text."
        self.scheduler.ingest_progress(
            _progress(
                task_id,
                attempt,
                [
                    _fact_operation(
                        "fact-c",
                        "TMP-FACT-C",
                        predecessors=["TMP-FACT-B"],
                        proof=uncited_proof,
                    )
                ],
                sequence=3,
            )
        )
        self.scheduler.ingest_progress(
            _progress(task_id, attempt, [], sequence=4, final=True)
        )

        fact_a_id = self._finish_fact("fact-a")
        assert fact_a_id is not None
        original = self.scheduler.state["operations"]["fact-b"]
        self.assertEqual(original["state"], OperationState.ABANDONED.value)
        normalized_id = original["normalized_to"]
        normalized = self.scheduler.state["operations"][normalized_id]
        self.assertEqual(normalized["payload"]["predecessor_fact_ids"], [fact_a_id])
        self.assertEqual(normalized["payload"]["statement"], statement)
        self.assertEqual(normalized["payload"]["proof"], proof)
        fact_b_id = self._finish_fact(normalized_id)
        assert fact_b_id is not None

        fact_c = self.scheduler.state["operations"]["fact-c"]
        fact_c_id = self._finish_fact(str(fact_c["normalized_to"]))
        assert fact_c_id is not None
        published = self.store.get(fact_c_id)
        assert published is not None
        self.assertEqual(published["predecessor_fact_ids"], [fact_b_id])
        self.assertEqual(published["proof"], uncited_proof)

    def test_terminal_fact_rejection_recurses_through_chain_and_body_gate(self) -> None:
        task_id, attempt = self._start_task(2)
        self.scheduler.ingest_progress(
            _progress(
                task_id,
                attempt,
                [
                    _fact_operation("chain-a", "TMP-CHAIN-A"),
                    _fact_operation(
                        "chain-b",
                        "TMP-CHAIN-B",
                        predecessors=["TMP-CHAIN-A"],
                    ),
                    _fact_operation(
                        "chain-c",
                        "TMP-CHAIN-C",
                        predecessors=["TMP-CHAIN-B"],
                    ),
                    _fact_operation(
                        "body-gated",
                        "TMP-BODY-GATED",
                        proof="Use TMP-CHAIN-A in the mathematical prose.",
                    ),
                ],
            )
        )
        state = self.scheduler.state
        self.assertEqual(state["operations"]["chain-a"]["state"], "synthesizing")
        for operation_id in ("chain-b", "chain-c", "body-gated"):
            self.assertEqual(
                state["operations"][operation_id]["state"],
                OperationState.WAITING_PREDECESSORS.value,
            )
        self.assertEqual(
            state["operations"]["body-gated"]["body_temporary_fact_ids"],
            ["TMP-CHAIN-A"],
        )
        self.assertEqual(
            state["operations"]["body-gated"]["payload"]["predecessor_fact_ids"],
            [],
        )

        self.scheduler.ingest_progress(
            _progress(task_id, attempt, [], sequence=2, final=True)
        )
        self._finish_fact("chain-a", verdict="incorrect")
        state = self.scheduler.state
        for operation_id in ("chain-a", "chain-b", "chain-c", "body-gated"):
            self.assertEqual(
                state["operations"][operation_id]["state"],
                OperationState.REJECTED.value,
            )
        self.assertIn("TMP-CHAIN-A", state["operations"]["chain-b"]["error"])
        self.assertIn("TMP-CHAIN-B", state["operations"]["chain-c"]["error"])
        self.assertIn("TMP-CHAIN-A", state["operations"]["body-gated"]["error"])

    def test_final_receipt_terminalizes_never_declared_strong_and_body_gates(
        self,
    ) -> None:
        task_id, attempt = self._start_task(4)
        self.scheduler.ingest_progress(
            _progress(
                task_id,
                attempt,
                [
                    _fact_operation(
                        "unknown-strong-gate",
                        "TMP-UNKNOWN-STRONG-SOURCE",
                        predecessors=["TMP-NEVER-DECLARED-FACT"],
                    )
                ],
            )
        )
        self.assertEqual(
            self.scheduler.state["operations"]["unknown-strong-gate"]["state"],
            OperationState.WAITING_PREDECESSORS.value,
        )
        self.scheduler.ingest_progress(
            _progress(
                task_id,
                attempt,
                [
                    _fact_operation(
                        "unknown-body-gate",
                        "TMP-UNKNOWN-BODY-SOURCE",
                        proof=(
                            "Use TMP-NEVER-DECLARED-FACT as an exact temporary "
                            "Fact citation."
                        ),
                    ),
                ],
                sequence=2,
                final=True,
            )
        )
        state = self.scheduler.state
        for operation_id in ("unknown-strong-gate", "unknown-body-gate"):
            operation = state["operations"][operation_id]
            self.assertEqual(
                operation["state"], OperationState.REJECTED.value, operation
            )
            self.assertIn("TMP-NEVER-DECLARED-FACT", operation["error"])
        self.assertEqual(
            state["operations"]["unknown-body-gate"]["body_temporary_fact_ids"],
            ["TMP-NEVER-DECLARED-FACT"],
        )
        self.assertNotEqual(
            state["tasks"][task_id]["state"], TaskState.POSTPROCESSING.value
        )

    def test_final_receipt_terminalizes_closed_temporary_fact_cycle(self) -> None:
        task_id, attempt = self._start_task(5)
        self.scheduler.ingest_progress(
            _progress(
                task_id,
                attempt,
                [
                    _fact_operation(
                        "cycle-a",
                        "TMP-CYCLE-A",
                        predecessors=["TMP-CYCLE-B"],
                    ),
                    _fact_operation(
                        "cycle-b",
                        "TMP-CYCLE-B",
                        predecessors=["TMP-CYCLE-A"],
                    ),
                ],
                final=True,
            )
        )
        state = self.scheduler.state
        for operation_id in ("cycle-a", "cycle-b"):
            self.assertEqual(
                state["operations"][operation_id]["state"],
                OperationState.REJECTED.value,
            )
        self.assertNotEqual(
            state["tasks"][task_id]["state"], TaskState.POSTPROCESSING.value
        )

    def test_fact_predecessor_merge_collapses_duplicate_canonical_id(self) -> None:
        self.store.records["F-SHARED"] = {
            "id": "F-SHARED",
            "type": "fact",
            "active": True,
            "status": "active",
            "statement": "The shared predecessor holds.",
            "proof": "A complete proof of the shared predecessor.",
            "predecessor_fact_ids": [],
            "introduced_notation": [],
            "external_references": [],
            "root_resolution": None,
        }
        task_id, attempt = self._start_task(6)
        statement = "The dependent assertion survives predecessor merging."
        proof = "A complete proof whose prose is immutable during merging."
        self.scheduler.ingest_progress(
            _progress(
                task_id,
                attempt,
                [
                    _fact_operation(
                        "merged-dependent",
                        "TMP-MERGED-DEPENDENT",
                        predecessors=["TMP-MERGE-A", "F-SHARED", "TMP-MERGE-B"],
                        statement=statement,
                        proof=proof,
                    )
                ],
            )
        )
        self.scheduler.resolve_temporary_predecessor("TMP-MERGE-A", "F-SHARED")
        first = self.scheduler.state["operations"]["merged-dependent"]
        first_normalized_id = first["normalized_to"]
        first_normalized = self.scheduler.state["operations"][first_normalized_id]
        self.assertEqual(
            first_normalized["payload"]["predecessor_fact_ids"],
            ["F-SHARED", "TMP-MERGE-B"],
        )

        self.scheduler.resolve_temporary_predecessor("TMP-MERGE-B", "F-SHARED")
        first_normalized = self.scheduler.state["operations"][first_normalized_id]
        second_normalized_id = first_normalized["normalized_to"]
        second_normalized = self.scheduler.state["operations"][second_normalized_id]
        self.assertEqual(
            second_normalized["payload"]["predecessor_fact_ids"], ["F-SHARED"]
        )
        self.assertEqual(second_normalized["payload"]["statement"], statement)
        self.assertEqual(second_normalized["payload"]["proof"], proof)
        self.scheduler.ingest_progress(
            _progress(task_id, attempt, [], sequence=2, final=True)
        )
        canonical_id = self._finish_fact(second_normalized_id)
        assert canonical_id is not None
        self.assertEqual(
            self.store.get(canonical_id)["predecessor_fact_ids"], ["F-SHARED"]
        )

    def test_worker_legacy_predecessor_ids_field_is_rejected(self) -> None:
        task_id, attempt = self._start_task(3)
        operation = _fact_operation("legacy-fact", "TMP-LEGACY-FACT")
        operation.pop("predecessor_fact_ids")
        operation["predecessor_ids"] = []
        statuses = self.scheduler.ingest_progress(
            _progress(task_id, attempt, [operation])
        )
        self.assertEqual(statuses["legacy-fact"], OperationState.REJECTED.value)
        rejected = self.scheduler.state["operations"]["legacy-fact"]
        self.assertIn("must use predecessor_fact_ids", rejected["error"])
        self.assertNotIn("legacy-fact", self.store.operation_results)


class SchedulerSoftReferenceCompletionTests(unittest.TestCase):
    def test_source_task_closes_without_waiting_for_its_soft_reference(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            store = MemoryStore(Path(raw) / "memory.sqlite3", projection_dir=False)
            try:
                scheduler = Scheduler(store)
                root_id = scheduler.bootstrap(root_problem="Prove ROOT.")
                store.add_obligation(
                    "bootstrap-reference-root",
                    _obligation_payload(
                        id=root_id,
                        abstract="The root problem.",
                        statement="ROOT holds.",
                        importance="This is the target.",
                    ),
                )
                scheduler.bind_canonical_root_obligation()
                route = store.add_route(
                    "portfolio-reference-route",
                    _route_payload(related_obligation_ids=[root_id]),
                )
                route_id = route.canonical_id
                assert route_id is not None
                scheduler.commit_initial_trim({"category_ids": []})

                task_id = scheduler.submit_batch(
                    "B-SOFT-SOURCE", [_research_report(10, route_id)]
                )[0]
                attempt = scheduler.start_task_attempt(task_id)
                final_receipt = _progress(
                    task_id,
                    attempt,
                    [
                        {
                            "operation_id": "pending-source-memo",
                            "kind": "memo",
                            "proposal_id": "TMP-SOURCE-MEMO",
                            **_memo_payload(
                                related_route_ids=[
                                    "TMP-FUTURE-SOFT-ROUTE",
                                    "TMP-FUTURE-SOFT-COMPUTATION-ROUTE",
                                ]
                            ),
                        }
                    ],
                    final=True,
                )
                final_receipt["computations"] = [
                    {
                        "staging_id": "pending-source-computation",
                        **_computation_payload(
                            task_id,
                            {
                                "route": [
                                    "TMP-FUTURE-SOFT-COMPUTATION-ROUTE"
                                ]
                            },
                        ),
                    }
                ]
                statuses = scheduler.ingest_progress(
                    final_receipt,
                    authenticated_computations=True,
                )
                self.assertEqual(
                    statuses["pending-source-memo"], OperationState.COMMITTED.value
                )
                self.assertEqual(
                    scheduler.state["tasks"][task_id]["state"],
                    TaskState.CLOSED.value,
                )
                source_id = scheduler.state["operations"]["pending-source-memo"][
                    "canonical_id"
                ]
                self.assertEqual(
                    store.get(source_id)["related_route_ids"],
                    [
                        "TMP-FUTURE-SOFT-ROUTE(unpublished)",
                        "TMP-FUTURE-SOFT-COMPUTATION-ROUTE(unpublished)",
                    ],
                )
                self.assertEqual(
                    store.temporary_reference_status("TMP-FUTURE-SOFT-ROUTE")[
                        "state"
                    ],
                    "abandoned",
                )
                computation_id = scheduler.state["computations"][
                    "pending-source-computation"
                ]["canonical_id"]
                self.assertEqual(
                    store.get(computation_id)["related_memory_ids"]["route"],
                    ["TMP-FUTURE-SOFT-COMPUTATION-ROUTE(unpublished)"],
                )
                self.assertEqual(
                    store.temporary_reference_status(
                        "TMP-FUTURE-SOFT-COMPUTATION-ROUTE"
                    )["state"],
                    "abandoned",
                )
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
