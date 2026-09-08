from __future__ import annotations

import unittest
import tempfile
from pathlib import Path

from franta.scheduler import (
    CapacityError,
    IdempotencyConflict,
    Scheduler,
    SchedulerError,
    StaleLeaseError,
)
from franta.testing import FakeControlStore, FakeTransport, TransportError
from franta.workflows import GateState, OperationState, TaskState, WorkflowError
from franta.store import MemoryStore


def research_report(index: int = 0) -> dict:
    return {
        "report_id": f"AR-{index}",
        "objective": f"Investigate objective {index}",
        "if_resume": None,
        "mode": "research",
        "main_route_ids": [f"R-route-{index}"],
        "main_obligation_ids": [],
        "perspective": None,
        "portfolio": {"fact": [], "route": [], "memo": [], "claim": [], "obligation": [], "computation": []},
        "reason": "test",
    }


def final_progress(task_id: str, attempt: int, sequence: int = 1, **extra: object) -> dict:
    payload = {
        "progress_id": f"P-{task_id}-{attempt}-{sequence}",
        "task_id": task_id,
        "attempt": attempt,
        "sequence": sequence,
        "is_final": True,
        "outcome_status": "progress",
        "attempt_summary": {"summary": "Useful progress", "proposed_outcome": "progress"},
        "operations": [],
        "completion_evidence_ids": [],
    }
    payload.update(extra)
    summary = dict(payload["attempt_summary"])
    if "completion_evidence_operation_ids" not in summary:
        summary["completion_evidence_operation_ids"] = list(
            payload["completion_evidence_ids"]
        )
    payload["attempt_summary"] = summary
    return payload


class SchedulerTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.store = FakeControlStore()
        for index in range(100):
            self.store.records[f"R-route-{index}"] = {
                "id": f"R-route-{index}",
                "type": "route",
                "active": True,
                "status": "active",
            }
        self.store.records["O-target"] = {
            "id": "O-target",
            "type": "obligation",
            "revision": 1,
            "statement": "Prove P",
            "active": True,
            "status": "active",
        }
        self.scheduler = Scheduler(self.store)
        self.scheduler.bootstrap(root_problem="Prove ROOT")
        self.scheduler.commit_initial_trim({"category_ids": []})

    def close_task(self, task_id: str) -> None:
        attempt = self.scheduler.start_task_attempt(task_id)
        self.scheduler.ingest_progress(final_progress(task_id, attempt))
        self.assertEqual(self.scheduler.state["tasks"][task_id]["state"], TaskState.CLOSED.value)

    def test_identical_artifact_registration_is_read_only_but_conflicts_reject(
        self,
    ) -> None:
        task_id = self.scheduler.submit_batch(
            "B-artifact-registration", [research_report(1)]
        )[0]
        reference = {
            "relative_path": f"{task_id}/CALL-1/outbox/record-progress/RP-1.json",
            "sha256": "a" * 64,
            "kind": "outbox",
        }
        self.scheduler.register_task_artifacts(task_id, [reference])
        revision_after_registration = self.scheduler.revision

        self.scheduler.register_task_artifacts(task_id, [reference])
        self.assertEqual(self.scheduler.revision, revision_after_registration)

        with self.assertRaises(IdempotencyConflict):
            self.scheduler.register_task_artifacts(
                task_id,
                [{**reference, "sha256": "b" * 64}],
            )
        self.assertEqual(self.scheduler.revision, revision_after_registration)

        self.close_task(task_id)
        closed_revision = self.scheduler.revision
        with self.assertRaises(WorkflowError):
            self.scheduler.register_task_artifacts(task_id, [reference])
        self.assertEqual(self.scheduler.revision, closed_revision)

    def test_final_completion_evidence_is_duplicate_free_and_matches_as_a_set(self) -> None:
        task_id = self.scheduler.submit_batch("B-evidence", [research_report(1)])[0]
        attempt = self.scheduler.start_task_attempt(task_id)

        invalid_pairs = (
            (None, []),
            ([], None),
            (["OP-A", "OP-A"], ["OP-A"]),
            (["OP-A"], ["OP-A", "OP-A"]),
            (["OP-A"], ["OP-B"]),
            ([1], [1]),
        )
        for evidence, summary_evidence in invalid_pairs:
            with self.subTest(evidence=evidence, summary_evidence=summary_evidence):
                with self.assertRaises(SchedulerError):
                    self.scheduler.ingest_progress(
                        final_progress(
                            task_id,
                            attempt,
                            completion_evidence_ids=evidence,
                            attempt_summary={
                                "summary": "Invalid evidence declaration",
                                "proposed_outcome": "progress",
                                "completion_evidence_operation_ids": summary_evidence,
                            },
                        )
                    )
                self.assertNotIn(
                    f"P-{task_id}-{attempt}-1", self.scheduler.state["progress"]
                )

        operations = [
            {
                "operation_id": operation_id,
                "kind": "memo",
                "genre": "normal",
                "content": f"Evidence {operation_id}",
                "related_route_ids": [],
            }
            for operation_id in ("OP-A", "OP-B")
        ]
        self.scheduler.ingest_progress(
            final_progress(
                task_id,
                attempt,
                operations=operations,
                completion_evidence_ids=["OP-A", "OP-B"],
                attempt_summary={
                    "summary": "Both operations support completion",
                    "proposed_outcome": "progress",
                    "completion_evidence_operation_ids": ["OP-B", "OP-A"],
                },
            )
        )
        self.assertEqual(
            self.scheduler.state["tasks"][task_id]["completion_evidence_ids"],
            ["OP-A", "OP-B"],
        )

    def test_worker_computations_require_cas_authentication_and_exact_identity(self) -> None:
        computation = {
            "staging_id": "CAS-SAME",
            "description": "Compute a toy value.",
            "assumptions": "None.",
            "exact_input": "1 + 1",
            "software": {"name": "SageMath", "version": "1"},
            "environment_versions": {},
            "random_seed": None,
            "output": "2",
            "exit_status": 0,
            "error_output": "",
            "interpretation": "The toy value is two.",
            "related_memory_ids": {},
            "fact_candidate_operation_ids": [],
        }
        task_one = self.scheduler.submit_batch("B-CAS-1", [research_report(10)])[0]
        attempt_one = self.scheduler.start_task_attempt(task_one)
        forged = final_progress(
            task_one,
            attempt_one,
            is_final=False,
            computations=[computation],
        )
        with self.assertRaises(SchedulerError):
            self.scheduler.ingest_progress(forged)
        self.assertNotIn("CAS-SAME", self.scheduler.state["computations"])

        self.scheduler.ingest_progress(forged, authenticated_computations=True)
        # The full progress receipt, not the nested CAS payload, owns the
        # progress idempotency digest.
        self.scheduler.ingest_progress(forged, authenticated_computations=True)
        exact_reuse = final_progress(
            task_one,
            attempt_one,
            sequence=2,
            is_final=False,
            computations=[computation],
        )
        self.scheduler.ingest_progress(
            exact_reuse,
            authenticated_computations=True,
        )

        changed = dict(computation)
        changed["output"] = "3"
        with self.assertRaises(IdempotencyConflict):
            self.scheduler.ingest_progress(
                final_progress(
                    task_one,
                    attempt_one,
                    sequence=3,
                    is_final=False,
                    computations=[changed],
                ),
                authenticated_computations=True,
            )

        task_two = self.scheduler.submit_batch("B-CAS-2", [research_report(11)])[0]
        attempt_two = self.scheduler.start_task_attempt(task_two)
        with self.assertRaises(IdempotencyConflict):
            self.scheduler.ingest_progress(
                final_progress(
                    task_two,
                    attempt_two,
                    is_final=False,
                    computations=[computation],
                ),
                authenticated_computations=True,
            )

    def test_atomic_capacity_and_eight_assignments_do_not_trigger_trim(self) -> None:
        first = self.scheduler.submit_batch("B-1", [research_report(i) for i in range(4)])
        with self.assertRaises(CapacityError):
            self.scheduler.submit_batch("B-too-many", [research_report(99)])
        self.assertNotIn("B-too-many", self.scheduler.state["batches"])
        for task_id in first:
            self.close_task(task_id)
        second = self.scheduler.submit_batch("B-2", [research_report(i) for i in range(4, 8)])
        self.assertEqual(len(second), 4)
        state = self.scheduler.state
        self.assertEqual(state["gate"], GateState.OPEN.value)
        self.assertIsNone(state["trim"]["active_review"])
        self.assertEqual(state["trim"]["assignment_reports"], [])

    def test_transport_retry_and_full_stop_fences_stale_epoch(self) -> None:
        transport = FakeTransport().enqueue("main", TransportError("offline"), {"decision": "wait"})
        scheduler = Scheduler(self.store, transport=transport)
        call_id = scheduler.prepare_call("main", {"portfolio": 1})
        result = scheduler.invoke_call(call_id)
        self.assertEqual(result["decision"], "wait")
        self.assertEqual([item.lease_epoch for item in transport.invocations], [1, 2])

        call2 = scheduler.prepare_call("trimmer", {"round": 1})
        old_epoch, _ = scheduler.mark_call_running(call2)
        plan = scheduler.recover()
        self.assertIn(call2, plan.retry_call_ids)
        self.assertEqual(scheduler.gate, GateState.OPEN)
        with self.assertRaises(StaleLeaseError):
            scheduler.accept_call_result(call2, old_epoch, {"decision": "no_trim"})

    def test_invalid_control_result_consumes_persisted_retry_budget(self) -> None:
        call_id = self.scheduler.prepare_call("main", {"portfolio": 1})
        epoch, _ = self.scheduler.mark_call_running(call_id)
        self.scheduler.accept_call_result(call_id, epoch, {"decision": "malformed"})
        self.assertTrue(self.scheduler.reject_call_result(call_id, "invalid decision"))
        call = self.scheduler.state["calls"][call_id]
        self.assertEqual(call["status"], "retry_pending")
        self.assertEqual(call["retry_count"], 1)
        self.assertIsNone(call["result"])
        self.assertEqual(call["invalid_results"][0]["result"]["decision"], "malformed")

    def test_stuck_report_replay_is_idempotent_after_gate_closes(self) -> None:
        report = {"summary": "The same obstacle persists."}
        self.scheduler.submit_stuck_report("B-STUCK-IDEMPOTENT", report)
        revision = self.scheduler.revision
        self.assertEqual(self.scheduler.gate, GateState.REVIEWING_TRIM)
        self.scheduler.submit_stuck_report("B-STUCK-IDEMPOTENT", report)
        self.assertGreater(self.scheduler.revision, revision)
        self.assertEqual(
            self.scheduler.state["trim"]["active_review"]["report"], report
        )

    def test_stale_synthesizer_review_is_rebased_for_reconfirmation(self) -> None:
        task_id = self.scheduler.submit_batch("B-synth-rebase", [research_report(1)])[0]
        attempt = self.scheduler.start_task_attempt(task_id)
        self.scheduler.ingest_progress(
            final_progress(
                task_id,
                attempt,
                operations=[
                    {
                        "operation_id": "route-proposal",
                        "kind": "route_add",
                        "proposal_id": "TMP-ROUTE",
                        "abstract": "A proposed route",
                    }
                ],
            )
        )
        call_id = self.scheduler.prepare_synthesizer_call("route-proposal")
        old_input = self.scheduler.state["calls"][call_id]["input"]
        self.store.records["R-concurrent"] = {
            "id": "R-concurrent",
            "type": "route",
            "revision": 1,
            "active": True,
            "status": "active",
            "abstract": "A concurrently published route",
        }
        epoch, _ = self.scheduler.mark_call_running(call_id)
        self.scheduler.accept_call_result(
            call_id,
            epoch,
            {
                "resolution": "new",
                "operation_digest": old_input["operation_digest"],
                "relied_on": [],
            },
        )
        self.scheduler.commit_synthesizer_call(call_id)
        operation = self.scheduler.state["operations"]["route-proposal"]
        self.assertEqual(operation["state"], OperationState.SYNTHESIZING.value)
        next_call = self.scheduler.prepare_synthesizer_call("route-proposal")
        self.assertNotEqual(next_call, call_id)
        next_input = self.scheduler.state["calls"][next_call]["input"]
        self.assertEqual(
            next_input["delta_since_prior_review"]["added"][0]["id"],
            "R-concurrent",
        )

    def test_invalid_operation_does_not_block_valid_sibling(self) -> None:
        task_id = self.scheduler.submit_batch("B-op-isolation", [research_report(1)])[0]
        attempt = self.scheduler.start_task_attempt(task_id)
        statuses = self.scheduler.ingest_progress(
            {
                "progress_id": "P-op-isolation",
                "task_id": task_id,
                "attempt": attempt,
                "sequence": 1,
                "is_final": False,
                "operations": [
                    {"operation_id": "bad-op", "kind": "not-an-operation"},
                    {
                        "operation_id": "good-memo",
                        "kind": "memo",
                        "proposal_id": "TMP-MEMO",
                        "abstract": "A useful obstruction",
                        "genre": "normal",
                        "content": "The attempted reduction loses an invariant.",
                        "related_route_ids": [],
                    },
                ],
            }
        )
        self.assertEqual(statuses["bad-op"], OperationState.REJECTED.value)
        self.assertEqual(statuses["good-memo"], OperationState.COMMITTED.value)

    def _submit_fact_attempt(
        self,
        task_id: str,
        attempt: int,
        version: int,
        operation_id: str,
        *,
        root: bool = False,
    ) -> dict:
        operation = {
            "operation_id": operation_id,
            "kind": "fact",
            "proposal_id": f"TMP-{operation_id}",
            "candidate_id": f"FC-{operation_id}",
            "candidate_version": 1,
            "statement": "Every test object has property P.",
            "proof": f"Version {version}: direct verification.",
            "predecessor_fact_ids": [],
            "abstract": "Test objects have property P by direct verification.",
            "keywords": ["test", "property P"],
            "introduced_notation": [],
            "external_references": [],
            "related_route_ids": [],
        }
        if root:
            operation["root_resolution"] = {"target": "ROOT", "outcome": "proved"}
        self.scheduler.ingest_progress(
            final_progress(
                task_id,
                attempt,
                operations=[operation],
                completion_evidence_ids=[operation_id],
                outcome_status="finished",
                attempt_summary={"summary": "Candidate proof", "proposed_outcome": "finished"},
            )
        )
        self.scheduler.apply_synthesizer_result(
            operation_id,
            {
                "resolution": "new",
                "operation_digest": self.scheduler.state["operations"][operation_id]["input_digest"],
            },
        )
        return self.scheduler.verification_bundle(operation_id)

    @staticmethod
    def _exact_verifier_report(
        bundle: dict, verdict: str, *, errors: list[object] | None = None
    ) -> dict:
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

    def test_incorrect_verifier_report_must_echo_root_resolution_exactly(self) -> None:
        task_id = self.scheduler.submit_batch("B-exact-incorrect", [research_report(99)])[0]
        attempt = self.scheduler.start_task_attempt(task_id)
        bundle = self._submit_fact_attempt(
            task_id,
            attempt,
            1,
            "fact-exact-incorrect",
            root=True,
        )
        report = self._exact_verifier_report(
            bundle,
            "incorrect",
            errors=[{"location": "proof", "message": "The proof has a gap."}],
        )
        report["root_resolution"] = None
        with self.assertRaisesRegex(
            SchedulerError, "does not match the exact envelope: root_resolution"
        ):
            self.scheduler.apply_verifier_report("fact-exact-incorrect", report)

        operation = self.scheduler.state["operations"]["fact-exact-incorrect"]
        self.assertEqual(operation["state"], OperationState.VERIFYING.value)

    def test_exact_two_request_fact_repair_then_mandatory_concession_memo(self) -> None:
        task_id = self.scheduler.submit_batch("B-repair", [research_report(1)])[0]
        for version in (1, 2, 3):
            attempt = self.scheduler.start_task_attempt(task_id)
            operation_id = f"fact-v{version}"
            bundle = self._submit_fact_attempt(task_id, attempt, version, operation_id)
            self.scheduler.apply_verifier_report(
                operation_id,
                self._exact_verifier_report(bundle, "incorrect", errors=["gap"]),
            )
        state = self.scheduler.state
        self.assertEqual(
            {
                state["operations"][f"fact-v{version}"]["candidate_id"]
                for version in (1, 2, 3)
            },
            {"FC-fact-v1", "FC-fact-v2", "FC-fact-v3"},
        )
        self.assertTrue(
            all(
                state["operations"][f"fact-v{version}"]["candidate_version"] == 1
                for version in (1, 2, 3)
            )
        )
        latest_lineage = state["fact_lineages"]["FC-fact-v3"]
        self.assertEqual(latest_lineage["revision_requests"], 2)
        self.assertTrue(latest_lineage["concession_required"])

        attempt = self.scheduler.start_task_attempt(task_id)
        with self.assertRaises(SchedulerError):
            self.scheduler.ingest_progress(final_progress(task_id, attempt))

        self.scheduler.ingest_progress(
            final_progress(
                task_id,
                attempt,
                progress_id="P-concession",
                operations=[
                    {
                        "operation_id": "failure-memo",
                        "kind": "memo",
                        "proposal_id": "TMP-FAILURE",
                        "abstract": "The direct property-P proof fails at the boundary case.",
                        "genre": "normal",
                        "content": "The third proof version assumes the missing boundary case.",
                        "related_route_ids": [],
                    },
                    {
                        "operation_id": "surviving-claim",
                        "kind": "claim_add",
                        "proposal_id": "TMP-CLAIM",
                        "abstract": "Interior test objects retain property P.",
                        "content": "The submitted calculation proves only the interior case.",
                        "related_route_ids": [],
                    },
                ],
                outcome_status="progress",
                attempt_summary={"summary": "Conceded with a useful interior claim", "proposed_outcome": "progress"},
            )
        )
        self.assertEqual(self.scheduler.state["tasks"][task_id]["state"], TaskState.CLOSED.value)

    def test_temporary_fact_predecessor_uses_exact_token_normalization(self) -> None:
        task_id = self.scheduler.submit_batch("B-normalize", [research_report(30)])[0]
        attempt = self.scheduler.start_task_attempt(task_id)
        self.scheduler.ingest_progress(
            final_progress(
                task_id,
                attempt,
                operations=[
                    {
                        "operation_id": "lemma-v1",
                        "kind": "fact",
                        "proposal_id": "TMP-LEMMA",
                        "candidate_id": "FC-lemma",
                        "candidate_version": 1,
                        "statement": "The temporary lemma holds.",
                        "proof": "A direct argument.",
                        "predecessor_fact_ids": [],
                        "abstract": "A temporary lemma owned by this task.",
                        "keywords": [],
                        "introduced_notation": [],
                        "external_references": [],
                        "related_route_ids": [],
                    },
                    {
                        "operation_id": "dependent-v1",
                        "kind": "fact",
                        "proposal_id": "TMP-DEPENDENT",
                        "candidate_id": "FC-dependent",
                        "candidate_version": 1,
                        "statement": "The dependent assertion holds.",
                        "proof": "Apply TMP-LEMMA. TMP-LEMMA-extra is unrelated.",
                        "predecessor_fact_ids": ["TMP-LEMMA"],
                        "abstract": "The abstract retains TMP-LEMMA as historical input text.",
                        "keywords": [],
                        "introduced_notation": [],
                        "external_references": [],
                        "related_route_ids": [],
                    }
                ],
            )
        )
        self.assertEqual(
            self.scheduler.state["operations"]["dependent-v1"]["state"],
            OperationState.WAITING_PREDECESSORS.value,
        )
        self.scheduler.resolve_temporary_predecessor("TMP-LEMMA", "F-canonical")
        state = self.scheduler.state
        normalized_id = "dependent-v1:normalized:2"
        normalized = state["operations"][normalized_id]
        self.assertEqual(normalized["state"], OperationState.SYNTHESIZING.value)
        self.assertEqual(
            normalized["payload"]["predecessor_fact_ids"], ["F-canonical"]
        )
        self.assertEqual(
            normalized["payload"]["proof"],
            "Apply TMP-LEMMA. TMP-LEMMA-extra is unrelated.",
        )
        self.assertIn("TMP-LEMMA", normalized["payload"]["abstract"])
        self.assertEqual(
            state["operations"]["dependent-v1"]["state"],
            OperationState.ABANDONED.value,
        )

    def test_mapped_temporary_fact_id_remains_scoped_to_its_source_task(self) -> None:
        predecessor_task_id = self.scheduler.submit_batch(
            "B-known-predecessor", [research_report(33)]
        )[0]
        predecessor_attempt = self.scheduler.start_task_attempt(predecessor_task_id)
        predecessor_progress = final_progress(
            predecessor_task_id,
            predecessor_attempt,
            operations=[
                {
                    "operation_id": "known-predecessor-v1",
                    "kind": "fact",
                    "proposal_id": "TMP-ALREADY-KNOWN",
                    "candidate_id": "FC-already-known",
                    "candidate_version": 1,
                    "statement": "The known predecessor holds.",
                    "proof": "A direct proof.",
                    "predecessor_fact_ids": [],
                    "abstract": "A predecessor published by this task.",
                    "keywords": [],
                    "introduced_notation": [],
                    "external_references": [],
                    "related_route_ids": [],
                }
            ],
        )
        self.scheduler.ingest_progress(predecessor_progress)
        predecessor_digest = self.scheduler.state["operations"][
            "known-predecessor-v1"
        ]["input_digest"]
        self.scheduler.apply_synthesizer_result(
            "known-predecessor-v1",
            {"resolution": "new", "operation_digest": predecessor_digest},
        )
        predecessor_bundle = self.scheduler.verification_bundle("known-predecessor-v1")
        self.scheduler.apply_verifier_report(
            "known-predecessor-v1",
            self._exact_verifier_report(predecessor_bundle, "correct"),
        )
        known_fact_id = self.scheduler.state["operations"]["known-predecessor-v1"][
            "canonical_id"
        ]
        dependent_report = research_report(34)
        dependent_report["portfolio"]["fact"] = [known_fact_id]
        task_id = self.scheduler.submit_batch(
            "B-known-dependent", [dependent_report]
        )[0]
        attempt = self.scheduler.start_task_attempt(task_id)
        self.scheduler.ingest_progress(
            final_progress(
                task_id,
                attempt,
                operations=[
                    {
                        "operation_id": "known-dependent-v1",
                        "kind": "fact",
                        "proposal_id": "TMP-KNOWN-DEPENDENT",
                        "candidate_id": "FC-known-dependent",
                        "candidate_version": 1,
                        "statement": "The known dependent assertion holds.",
                        "proof": "Apply TMP-ALREADY-KNOWN.",
                        "predecessor_fact_ids": ["TMP-ALREADY-KNOWN"],
                        "abstract": "A candidate whose predecessor was already published.",
                        "keywords": [],
                        "introduced_notation": [],
                        "external_references": [],
                        "related_route_ids": [],
                    }
                ],
            )
        )
        state = self.scheduler.state
        dependent = state["operations"]["known-dependent-v1"]
        self.assertEqual(
            dependent["state"],
            OperationState.REJECTED.value,
        )
        self.assertIn(
            "temporary ID TMP-ALREADY-KNOWN belongs to another task",
            str(dependent["error"]),
        )
        self.assertNotIn("known-dependent-v1:normalized:2", state["operations"])

    def test_rejected_fact_predecessor_returns_every_waiting_dependent(self) -> None:
        dependent_task = self.scheduler.submit_batch(
            "B-dependent", [research_report(31)]
        )[0]
        dependent_attempt = self.scheduler.start_task_attempt(dependent_task)
        self.scheduler.ingest_progress(
            final_progress(
                dependent_task,
                dependent_attempt,
                operations=[
                    {
                        "operation_id": "bad-predecessor",
                        "kind": "fact",
                        "proposal_id": "TMP-BAD-PREDECESSOR",
                        "candidate_id": "FC-bad-predecessor",
                        "candidate_version": 1,
                        "statement": "The proposed predecessor holds.",
                        "proof": "A purported direct argument.",
                        "predecessor_fact_ids": [],
                        "abstract": "A proposed predecessor.",
                        "keywords": [],
                        "introduced_notation": [],
                        "external_references": [],
                        "related_route_ids": [],
                    },
                    {
                        "operation_id": "dependent-fact",
                        "kind": "fact",
                        "proposal_id": "TMP-CHILD",
                        "candidate_id": "FC-child",
                        "candidate_version": 1,
                        "statement": "The child assertion holds.",
                        "proof": "Use TMP-BAD-PREDECESSOR.",
                        "predecessor_fact_ids": ["TMP-BAD-PREDECESSOR"],
                        "abstract": "A dependent candidate.",
                        "keywords": [],
                        "introduced_notation": [],
                        "external_references": [],
                        "related_route_ids": [],
                    }
                ],
            )
        )
        digest = self.scheduler.state["operations"]["bad-predecessor"]["input_digest"]
        self.scheduler.apply_synthesizer_result(
            "bad-predecessor", {"resolution": "new", "operation_digest": digest}
        )
        bundle = self.scheduler.verification_bundle("bad-predecessor")
        self.scheduler.apply_verifier_report(
            "bad-predecessor",
            self._exact_verifier_report(
                bundle,
                "incorrect",
                errors=["The direct argument omits a case."],
            ),
        )
        state = self.scheduler.state
        self.assertEqual(
            state["operations"]["dependent-fact"]["state"],
            OperationState.REJECTED.value,
        )
        self.assertEqual(
            state["tasks"][dependent_task]["state"], TaskState.REVISION_PENDING.value
        )
        self.assertEqual(
            state["tasks"][dependent_task]["dependency_repair_required"]["kind"],
            "predecessor_rejected",
        )

    def test_root_resolution_stops_research_and_completes_after_terminal_call(self) -> None:
        task_id = self.scheduler.submit_batch("B-root", [research_report(1)])[0]
        attempt = self.scheduler.start_task_attempt(task_id)
        bundle = self._submit_fact_attempt(task_id, attempt, 1, "root-fact", root=True)
        fact_id = self.scheduler.apply_verifier_report(
            "root-fact", self._exact_verifier_report(bundle, "correct")
        )
        self.assertIsNotNone(fact_id)
        self.assertEqual(self.scheduler.gate, GateState.RESOLUTION_PENDING)
        self.assertEqual(self.scheduler.state["root"]["obligation_status"], "resolved")
        with self.assertRaises(Exception):
            self.scheduler.submit_batch("B-forbidden", [research_report(9)])
        self.scheduler.record_terminal_main_decision()
        self.assertEqual(self.scheduler.gate, GateState.COMPLETED)

    def test_alternate_root_proof_survives_primary_revocation(self) -> None:
        task_ids = self.scheduler.submit_batch(
            "B-two-root-proofs", [research_report(1), research_report(2)]
        )
        fact_ids: list[str] = []
        for index, task_id in enumerate(task_ids, 1):
            attempt = self.scheduler.start_task_attempt(task_id)
            operation_id = f"root-proof-{index}"
            candidate_id = f"FC-root-{index}"
            operation = {
                "operation_id": operation_id,
                "kind": "fact",
                "proposal_id": f"TMP-{operation_id}",
                "candidate_id": candidate_id,
                "candidate_version": 1,
                "statement": "ROOT holds.",
                "proof": f"Independent proof {index}.",
                "predecessor_fact_ids": [],
                "abstract": f"Independent root proof {index}",
                "keywords": ["ROOT"],
                "introduced_notation": [],
                "external_references": [],
                "related_route_ids": [],
                "root_resolution": {"target": "ROOT", "outcome": "proved"},
            }
            self.scheduler.ingest_progress(
                final_progress(
                    task_id,
                    attempt,
                    operations=[operation],
                    completion_evidence_ids=[operation_id],
                    outcome_status="finished",
                    attempt_summary={
                        "summary": f"Root proof {index}",
                        "proposed_outcome": "finished",
                    },
                )
            )
            self.scheduler.apply_synthesizer_result(
                operation_id,
                {
                    "resolution": "new",
                    "operation_digest": self.scheduler.state["operations"][operation_id][
                        "input_digest"
                    ],
                    "relied_on": [],
                },
            )
            bundle = self.scheduler.verification_bundle(operation_id)
            fact_id = self.scheduler.apply_verifier_report(
                operation_id,
                self._exact_verifier_report(bundle, "correct"),
            )
            self.assertIsNotNone(fact_id)
            fact_ids.append(str(fact_id))
        self.assertEqual(self.scheduler.state["root"]["solution_fact_id"], fact_ids[0])
        self.scheduler.revoke_fact(fact_ids[0], reason="first proof has a fatal gap")
        root = self.scheduler.state["root"]
        self.assertEqual(root["solution_fact_id"], fact_ids[1])
        self.assertEqual(root["outcome"], "proved")
        self.assertEqual(root["obligation_status"], "resolved")
        self.assertEqual(self.scheduler.gate, GateState.RESOLUTION_PENDING)

    def test_unlaunched_proof_writer_is_cancelled_when_root_fact_is_revoked(self) -> None:
        task_id = self.scheduler.submit_batch("B-root-cancel", [research_report(1)])[0]
        attempt = self.scheduler.start_task_attempt(task_id)
        bundle = self._submit_fact_attempt(
            task_id, attempt, 1, "root-to-revoke", root=True
        )
        fact_id = self.scheduler.apply_verifier_report(
            "root-to-revoke", self._exact_verifier_report(bundle, "correct")
        )
        proof_report = {
            "report_id": "AR-proof-writer",
            "objective": "Write the final proof.",
            "if_resume": None,
            "mode": "proof-writer",
            "main_route_ids": [],
            "main_obligation_ids": [],
            "perspective": None,
            "portfolio": {
                "fact": [], "route": [], "memo": [], "claim": [],
                "obligation": [], "computation": [],
            },
            "reason": "ROOT is resolved.",
        }
        proof_task = self.scheduler.record_terminal_main_decision(
            proof_report, batch_id="B-proof-writer"
        )
        self.assertIsNotNone(proof_task)
        self.scheduler.revoke_fact(str(fact_id), reason="root proof invalidated")
        cancelled = self.scheduler.state["tasks"][str(proof_task)]
        self.assertEqual(cancelled["state"], TaskState.CLOSED.value)
        self.assertEqual(cancelled["final_status"], "interrupted")
        self.assertEqual(self.scheduler.gate, GateState.OPEN)

    def test_fact_challenge_uses_exact_bundle_and_scheduler_resolution(self) -> None:
        fact_id = "F-challenged"
        self.store.records[fact_id] = {
            "id": fact_id,
            "type": "fact",
            "active": True,
            "status": "active",
            "statement": "P holds.",
            "proof": "Proof.",
            "predecessor_fact_ids": [],
            "external_references": [],
            "introduced_notation": [],
        }
        task_id = self.scheduler.submit_batch("B-challenge", [research_report(1)])[0]
        attempt = self.scheduler.start_task_attempt(task_id)
        self.scheduler.ingest_progress(
            final_progress(
                task_id,
                attempt,
                fact_challenges=[
                    {
                        "challenge_id": "CH-1",
                        "fact_id": fact_id,
                        "alleged_failure": "The proof omits a boundary case.",
                    }
                ],
            )
        )
        call_id = self.scheduler.prepare_challenge_verifier_call("CH-1")
        epoch, _ = self.scheduler.mark_call_running(call_id)
        bundle_digest = self.scheduler.state["calls"][call_id]["input"]["bundle_digest"]
        self.scheduler.accept_call_result(
            call_id,
            epoch,
            {
                "challenge_id": "CH-1",
                "bundle_digest": bundle_digest,
                "resolution": "challenge_rejected",
                "justification": "The boundary case is covered.",
            },
        )
        self.scheduler.commit_challenge_verifier_call(call_id)
        self.assertEqual(
            self.scheduler.state["challenges"]["CH-1"]["resolution"],
            "challenge_rejected",
        )
        self.assertEqual(self.scheduler.state["tasks"][task_id]["state"], "closed")

    def test_main_closure_review_is_only_created_for_explicit_ambiguity(self) -> None:
        task_id = self.scheduler.submit_batch("B-closure-review", [research_report(1)])[0]
        attempt = self.scheduler.start_task_attempt(task_id)
        self.scheduler.ingest_progress(
            final_progress(
                task_id,
                attempt,
                operations=[
                    {
                        "operation_id": "ambiguous-route",
                        "kind": "route_add",
                        "proposal_id": "TMP-AMBIGUOUS-ROUTE",
                        "abstract": "An unresolved route proposal",
                    }
                ],
                completion_evidence_ids=["ambiguous-route"],
            )
        )
        with self.assertRaises(Exception):
            self.scheduler.prepare_main_closure_review_call(task_id)
        self.scheduler.abandon_operation(
            "ambiguous-route", authorized_by="operator", reason="semantic review required"
        )
        call_id = self.scheduler.prepare_main_closure_review_call(task_id)
        epoch, _ = self.scheduler.mark_call_running(call_id)
        self.scheduler.accept_call_result(
            call_id,
            epoch,
            {
                "outcome": "progress",
                "summary": {"summary": "Useful work remains despite the abandoned route."},
            },
        )
        self.scheduler.commit_main_closure_review_call(call_id)
        self.assertEqual(self.scheduler.state["tasks"][task_id]["state"], "closed")
        self.assertEqual(self.scheduler.state["tasks"][task_id]["final_status"], "progress")

    def test_sprint_uses_atomic_four_sealed_lanes_and_barrier(self) -> None:
        self.scheduler.submit_stuck_report("B-stuck", {"summary": "Repeated mechanism"})
        self.scheduler.apply_trim_review_decision("trim", "stuck")
        plan = {
            "target_obligation": {"id": "O-target", "revision": 1, "statement": "Prove P"},
            "lanes": [
                {
                    "mode": "brainstorm",
                    "objective": "Find a clean-room proof.",
                    "main_obligation_ids": ["O-target"],
                    "assignment_portfolio": {"fact": [], "route": [], "memo": [], "claim": [], "obligation": [], "computation": []},
                    "reason": "Supply only the target and omit project mechanisms.",
                },
                {
                    "mode": "multi-discipline",
                    "objective": "Translate the target.",
                    "main_obligation_ids": ["O-target"],
                    "selected_new_perspective": "categorical",
                    "assignment_portfolio": {"fact": [], "route": [], "memo": [], "claim": [], "obligation": [], "computation": []},
                    "reason": "Supply only the target and one new perspective.",
                },
                {
                    "mode": "computation",
                    "objective": "Test boundary cases.",
                    "main_obligation_ids": ["O-target"],
                    "computation_portfolio": ["Enumerate the smallest cases."],
                    "assignment_portfolio": {"fact": [], "route": [], "memo": [], "claim": [], "obligation": [], "computation": []},
                    "reason": "Supply the target and one explicit experiment.",
                },
                {
                    "mode": "associate",
                    "objective": "Seek a remote bridge.",
                    "main_obligation_ids": ["O-target"],
                    "assignment_portfolio": {"fact": [], "route": ["R-route-90", "R-route-91"], "memo": [], "claim": [], "obligation": [], "computation": []},
                    "reason": "Supply exactly two distant routes and omit the current mechanism.",
                },
            ],
        }
        self.scheduler.persist_sprint_plan("S-1", plan)
        reports = []
        for index, lane in enumerate(plan["lanes"]):
            reports.append(
                {
                    "report_id": f"AR-SPRINT-{index}",
                    "objective": lane["objective"],
                    "if_resume": None,
                    "mode": lane["mode"],
                    "main_route_ids": [],
                    "main_obligation_ids": ["O-target"],
                    "perspective": lane.get("selected_new_perspective"),
                    "computation_portfolio": lane.get("computation_portfolio"),
                    "portfolio": lane["assignment_portfolio"],
                    "reason": lane["reason"],
                }
            )
        task_ids = self.scheduler.launch_sprint("S-1", reports, batch_id="B-sprint")
        self.assertEqual(len(task_ids), 4)
        for index, task_id in enumerate(task_ids):
            self.assertEqual(
                self.scheduler.state["tasks"][task_id]["sprint_lane"],
                "ABCD"[index],
            )
            policy = self.scheduler.state["tasks"][task_id]["task_card"]["access_policy"]
            self.assertFalse(policy["canonical_memory"])
            self.assertFalse(policy["internal_search"])
            self.assertTrue(policy["sealed_workspace"])
        for task_id in task_ids:
            self.close_task(task_id)
        self.assertEqual(self.scheduler.state["sprints"]["S-1"]["status"], "awaiting_summary")


class RealStoreSchedulerIntegrationTest(unittest.TestCase):
    def _reference_scheduler(
        self, raw: str, suffix: str
    ) -> tuple[MemoryStore, Scheduler, str, str]:
        store = MemoryStore(Path(raw) / "memory.sqlite3", projection_dir=False)
        scheduler = Scheduler(store)
        root_id = scheduler.bootstrap(root_problem="Prove ROOT")
        store.add_obligation(
            f"bootstrap-root-{suffix}",
            {
                "id": root_id,
                "abstract": "The root problem",
                "statement": "ROOT holds.",
                "importance": "This is the target.",
                "predecessor_fact_ids": [],
                "partial_progress": [],
                "related_route_ids": [],
                "relations": [],
            },
        )
        scheduler.bind_canonical_root_obligation()
        route = store.add_route(
            f"bootstrap-route-{suffix}",
            {
                "abstract": "Portfolio route",
                "strategy_description": "Study ROOT directly.",
                "value_assessment": {
                    "confidence": "plausible",
                    "success_gain": "resolves ROOT",
                    "failure_gain": "locates the obstruction",
                    "relevance": "central",
                    "novelty": "baseline",
                },
                "progress": [],
                "related_obligation_ids": [root_id],
                "next_steps": [],
                "obstacles": [],
                "active_fact_ids": [],
                "relevant_memo_ids": [],
                "relevant_claim_ids": [],
            },
        )
        route_id = route.canonical_id
        assert route_id is not None
        scheduler.commit_initial_trim({"category_ids": []})
        return store, scheduler, root_id, route_id

    def test_temporary_fact_is_soft_only_in_synthesized_route_update_patch(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            store, scheduler, _root_id, route_id = self._reference_scheduler(
                raw, "synthesized-update-temporary-fact"
            )
            try:
                fact_proposal_id = "P-FACT-RAMPAZZO-MOTIVE-0001"
                route_operation_id = "route-update-with-temporary-fact"
                report = research_report(88)
                report["main_route_ids"] = [route_id]
                task_id = scheduler.submit_batch(
                    "B-synthesized-update-temporary-fact", [report]
                )[0]
                attempt = scheduler.start_task_attempt(task_id)
                scheduler.ingest_progress(
                    final_progress(
                        task_id,
                        attempt,
                        is_final=False,
                        operations=[
                            {
                                "operation_id": "pending-fact-proposal",
                                "kind": "fact",
                                "proposal_id": fact_proposal_id,
                                "candidate_id": "FC-RAMPAZZO-MOTIVE-0001",
                                "candidate_version": 1,
                                "statement": "The proposed motive comparison holds.",
                                "proof": "A complete direct argument is proposed.",
                                "predecessor_fact_ids": [],
                                "abstract": "A Fact proposal awaiting synthesis.",
                                "keywords": ["motive"],
                                "introduced_notation": [],
                                "external_references": [],
                                "related_route_ids": [],
                            },
                            {
                                "operation_id": route_operation_id,
                                "kind": "route_add",
                                "proposal_id": "P-ROUTE-RAMPAZZO-MOTIVE-0001",
                                "abstract": "Refine the portfolio route using the proposed Fact.",
                                "strategy_description": "Study ROOT directly.",
                                "value_assessment": {
                                    "confidence": "plausible",
                                    "success_gain": "would sharpen the reduction",
                                    "failure_gain": "would locate the obstruction",
                                    "relevance": "central",
                                    "novelty": "refinement",
                                },
                                "progress": ["The proposed Fact sharpens one step."],
                                "related_obligation_ids": [],
                                "next_steps": [],
                                "obstacles": [],
                                "active_fact_ids": [fact_proposal_id],
                                "relevant_memo_ids": [],
                                "relevant_claim_ids": [],
                            },
                        ],
                    )
                )
                self.assertIsNone(
                    scheduler.state["operations"]["pending-fact-proposal"].get(
                        "canonical_id"
                    )
                )

                patch = {
                    "target_id": route_id,
                    "expected_base_revision": 1,
                    "set": {},
                    "append": {
                        "progress": ["The proposed Fact sharpens one step."]
                    },
                    "add_ids": {"active_fact_ids": [fact_proposal_id]},
                    "remove_ids": {},
                    "explanation": "Merge only the new route progress.",
                    "supporting_memory_ids": [],
                }
                operation_digest = scheduler.state["operations"][route_operation_id][
                    "input_digest"
                ]
                invalid_report = {
                    "resolution": "update",
                    "operation_digest": operation_digest,
                    "explanation": "The existing route can absorb the progress.",
                    "canonical_id": route_id,
                    "relied_on": [
                        {"id": route_id, "revision": 1},
                        {"id": fact_proposal_id, "revision": None},
                    ],
                    "patch": patch,
                }
                scheduler_revision = scheduler.revision
                route_before = store.get(route_id).to_dict()
                self.assertIsNone(store.temporary_reference_status(fact_proposal_id))

                with self.assertRaisesRegex(
                    SchedulerError,
                    f"synthesizer relied on missing memory {fact_proposal_id}",
                ):
                    scheduler.apply_synthesizer_result(
                        route_operation_id, invalid_report
                    )

                self.assertEqual(scheduler.revision, scheduler_revision)
                self.assertEqual(store.get(route_id).to_dict(), route_before)
                self.assertIsNone(store.temporary_reference_status(fact_proposal_id))
                self.assertIsNone(store.operation_status(route_operation_id))
                invalid_operation = scheduler.state["operations"][route_operation_id]
                self.assertEqual(
                    invalid_operation["state"], OperationState.SYNTHESIZING.value
                )
                self.assertIsNone(invalid_operation.get("synthesizer_result"))

                valid_report = dict(invalid_report)
                valid_report["relied_on"] = [{"id": route_id, "revision": 1}]
                self.assertTrue(
                    scheduler.apply_synthesizer_result(
                        route_operation_id, valid_report
                    )
                )

                updated_route = store.get(route_id)
                self.assertEqual(updated_route.revision, 2)
                self.assertEqual(
                    updated_route["active_fact_ids"], [fact_proposal_id]
                )
                self.assertEqual(
                    updated_route["pending_references"],
                    [
                        {
                            "field_path": ["active_fact_ids", 0],
                            "expected_type": "fact",
                            "temporary_id": fact_proposal_id,
                            "state": "pending",
                        }
                    ],
                )
                self.assertEqual(
                    store.temporary_reference_status(fact_proposal_id)["state"],
                    "pending",
                )
                committed = scheduler.state["operations"][route_operation_id]
                self.assertEqual(committed["state"], OperationState.COMMITTED.value)
                self.assertEqual(committed["canonical_id"], route_id)
            finally:
                store.close()

    def test_late_target_reservation_survives_restart_and_rejects_collisions(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            store, scheduler, _root_id, portfolio_route_id = self._reference_scheduler(
                raw, "late-binding"
            )
            try:
                report = research_report(80)
                report["main_route_ids"] = [portfolio_route_id]
                task_id = scheduler.submit_batch("B-late-binding", [report])[0]
                attempt = scheduler.start_task_attempt(task_id)
                scheduler.ingest_progress(
                    final_progress(
                        task_id,
                        attempt,
                        is_final=False,
                        operations=[
                            {
                                "operation_id": "late-source-memo",
                                "kind": "memo",
                                "proposal_id": "TMP-LATE-SOURCE-MEMO",
                                "abstract": "Memo published before its route",
                                "genre": "normal",
                                "content": "The route will be supplied in later progress.",
                                "related_route_ids": ["TMP-LATE-TARGET-ROUTE"],
                            }
                        ],
                    )
                )
                source_id = scheduler.state["operations"]["late-source-memo"][
                    "canonical_id"
                ]
                self.assertIsNotNone(source_id)
                self.assertEqual(
                    store.temporary_reference_status("TMP-LATE-TARGET-ROUTE")[
                        "state"
                    ],
                    "pending",
                )

                scheduler = Scheduler(store)
                scheduler.ingest_progress(
                    final_progress(
                        task_id,
                        attempt,
                        sequence=2,
                        is_final=False,
                        operations=[
                            {
                                "operation_id": "late-wrong-type",
                                "kind": "memo",
                                "proposal_id": "TMP-LATE-TARGET-ROUTE",
                                "abstract": "Wrongly typed declaration",
                                "genre": "normal",
                                "content": "This must not steal a route reservation.",
                                "related_route_ids": [],
                            }
                        ],
                    )
                )
                wrong_type = scheduler.state["operations"]["late-wrong-type"]
                self.assertEqual(wrong_type["state"], OperationState.REJECTED.value)
                self.assertIn("already typed as route", wrong_type["error"])

                other_report = research_report(81)
                other_report["main_route_ids"] = [portfolio_route_id]
                other_task = scheduler.submit_batch(
                    "B-late-binding-collision", [other_report]
                )[0]
                other_attempt = scheduler.start_task_attempt(other_task)
                scheduler.ingest_progress(
                    final_progress(
                        other_task,
                        other_attempt,
                        operations=[
                            {
                                "operation_id": "late-other-task-target",
                                "kind": "route_add",
                                "proposal_id": "TMP-LATE-TARGET-ROUTE",
                                "abstract": "Cross-task collision",
                                "strategy_description": "This declaration is unauthorized.",
                                "value_assessment": {
                                    "confidence": "uncertain",
                                    "success_gain": "none",
                                    "failure_gain": "tests ownership",
                                    "relevance": "related",
                                    "novelty": "none",
                                },
                                "progress": [],
                                "related_obligation_ids": [],
                                "next_steps": [],
                                "obstacles": [],
                                "active_fact_ids": [],
                                "relevant_memo_ids": [],
                                "relevant_claim_ids": [],
                            }
                        ],
                    )
                )
                other = scheduler.state["operations"]["late-other-task-target"]
                self.assertEqual(other["state"], OperationState.REJECTED.value)
                self.assertIn("belongs to another task", other["error"])
                self.assertEqual(
                    store.temporary_reference_status("TMP-LATE-TARGET-ROUTE")[
                        "state"
                    ],
                    "pending",
                )

                scheduler.ingest_progress(
                    final_progress(
                        task_id,
                        attempt,
                        sequence=3,
                        operations=[
                            {
                                "operation_id": "late-target-route",
                                "kind": "route_add",
                                "proposal_id": "TMP-LATE-TARGET-ROUTE",
                                "abstract": "The genuinely late route",
                                "strategy_description": "Attach the previously published memo.",
                                "value_assessment": {
                                    "confidence": "plausible",
                                    "success_gain": "activates the route",
                                    "failure_gain": "tests late binding",
                                    "relevance": "central",
                                    "novelty": "late publication",
                                },
                                "progress": [],
                                "related_obligation_ids": [],
                                "next_steps": [],
                                "obstacles": [],
                                "active_fact_ids": [],
                                "relevant_memo_ids": [],
                                "relevant_claim_ids": [],
                            }
                        ],
                        completion_evidence_ids=[
                            "late-source-memo",
                            "late-target-route",
                        ],
                    )
                )
                scheduler.apply_synthesizer_result(
                    "late-target-route",
                    {
                        "resolution": "new",
                        "operation_digest": scheduler.state["operations"][
                            "late-target-route"
                        ]["input_digest"],
                    },
                )
                target_id = scheduler.state["operations"]["late-target-route"][
                    "canonical_id"
                ]
                self.assertEqual(store.get(source_id)["related_route_ids"], [target_id])
                self.assertEqual(store.get(target_id)["relevant_memo_ids"], [source_id])
                self.assertEqual(
                    scheduler.state["tasks"][task_id]["state"], TaskState.CLOSED.value
                )
            finally:
                store.close()

    def test_late_target_deduplication_resolves_source_and_reciprocal_index(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            store, scheduler, _root_id, existing_route_id = self._reference_scheduler(
                raw, "late-binding-deduplication"
            )
            try:
                report = research_report(84)
                report["main_route_ids"] = [existing_route_id]
                task_id = scheduler.submit_batch(
                    "B-late-binding-deduplication", [report]
                )[0]
                attempt = scheduler.start_task_attempt(task_id)
                scheduler.ingest_progress(
                    final_progress(
                        task_id,
                        attempt,
                        is_final=False,
                        operations=[
                            {
                                "operation_id": "late-dedup-source-memo",
                                "kind": "memo",
                                "proposal_id": "TMP-LATE-DEDUP-SOURCE-MEMO",
                                "abstract": "Memo awaiting a deduplicated route",
                                "genre": "normal",
                                "content": "The later proposal matches an existing route.",
                                "related_route_ids": ["TMP-LATE-DEDUP-ROUTE"],
                            }
                        ],
                    )
                )
                source_id = scheduler.state["operations"][
                    "late-dedup-source-memo"
                ]["canonical_id"]
                self.assertEqual(
                    store.temporary_reference_status("TMP-LATE-DEDUP-ROUTE")[
                        "state"
                    ],
                    "pending",
                )

                scheduler = Scheduler(store)
                scheduler.ingest_progress(
                    final_progress(
                        task_id,
                        attempt,
                        sequence=2,
                        operations=[
                            {
                                "operation_id": "late-dedup-target-route",
                                "kind": "route_add",
                                "proposal_id": "TMP-LATE-DEDUP-ROUTE",
                                "abstract": "Duplicate of the portfolio route",
                                "strategy_description": "Study ROOT directly.",
                                "value_assessment": {
                                    "confidence": "plausible",
                                    "success_gain": "resolves ROOT",
                                    "failure_gain": "locates the obstruction",
                                    "relevance": "central",
                                    "novelty": "duplicate",
                                },
                                "progress": [],
                                "related_obligation_ids": [],
                                "next_steps": [],
                                "obstacles": [],
                                "active_fact_ids": [],
                                "relevant_memo_ids": [],
                                "relevant_claim_ids": [],
                            }
                        ],
                        completion_evidence_ids=[
                            "late-dedup-source-memo",
                            "late-dedup-target-route",
                        ],
                    )
                )
                scheduler.apply_synthesizer_result(
                    "late-dedup-target-route",
                    {
                        "resolution": "duplicate",
                        "canonical_id": existing_route_id,
                        "operation_digest": scheduler.state["operations"][
                            "late-dedup-target-route"
                        ]["input_digest"],
                    },
                )

                self.assertEqual(
                    store.get(source_id)["related_route_ids"], [existing_route_id]
                )
                self.assertEqual(
                    store.get(existing_route_id)["relevant_memo_ids"], [source_id]
                )
                self.assertEqual(
                    store.temporary_reference_status("TMP-LATE-DEDUP-ROUTE")[
                        "canonical_id"
                    ],
                    existing_route_id,
                )
                self.assertEqual(
                    scheduler.state["tasks"][task_id]["state"],
                    TaskState.CLOSED.value,
                )
            finally:
                store.close()

    def test_rejected_reference_closes_source_with_unpublished_marker(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            store, scheduler, _root_id, portfolio_route_id = self._reference_scheduler(
                raw, "reference-correction"
            )
            try:
                report = research_report(82)
                report["main_route_ids"] = [portfolio_route_id]
                task_id = scheduler.submit_batch("B-reference-correction", [report])[0]
                attempt = scheduler.start_task_attempt(task_id)
                scheduler.ingest_progress(
                    final_progress(
                        task_id,
                        attempt,
                        is_final=False,
                        operations=[
                            {
                                "operation_id": "correction-source-route",
                                "kind": "route_add",
                                "proposal_id": "TMP-CORRECTION-SOURCE-ROUTE",
                                "abstract": "Route awaiting a later memo",
                                "strategy_description": "Use the later memo as context.",
                                "value_assessment": {
                                    "confidence": "plausible",
                                    "success_gain": "advances ROOT",
                                    "failure_gain": "tests correction",
                                    "relevance": "central",
                                    "novelty": "late memo",
                                },
                                "progress": [],
                                "related_obligation_ids": [],
                                "next_steps": [],
                                "obstacles": [],
                                "active_fact_ids": [],
                                "relevant_memo_ids": ["TMP-REJECTED-LATE-MEMO"],
                                "relevant_claim_ids": [],
                            }
                        ],
                    )
                )
                scheduler.apply_synthesizer_result(
                    "correction-source-route",
                    {
                        "resolution": "new",
                        "operation_digest": scheduler.state["operations"][
                            "correction-source-route"
                        ]["input_digest"],
                    },
                )
                source_id = scheduler.state["operations"]["correction-source-route"][
                    "canonical_id"
                ]
                self.assertEqual(
                    store.get(source_id)["relevant_memo_ids"],
                    ["TMP-REJECTED-LATE-MEMO"],
                )
                scheduler.ingest_progress(
                    final_progress(
                        task_id,
                        attempt,
                        sequence=2,
                        operations=[
                            {
                                "operation_id": "rejected-late-memo-v1",
                                "kind": "memo",
                                "proposal_id": "TMP-REJECTED-LATE-MEMO",
                                "abstract": "Invalid late memo",
                                "genre": "unsupported",
                                "content": "This target must be corrected.",
                                "related_route_ids": [],
                            }
                        ],
                        completion_evidence_ids=["correction-source-route"],
                    )
                )
                task = scheduler.state["tasks"][task_id]
                self.assertEqual(task["state"], TaskState.CLOSED.value)
                self.assertNotIn("pending_attempt_supplement", task)
                self.assertNotIn("temporary_reference_correction_attention", task)
                self.assertEqual(
                    store.get(source_id)["relevant_memo_ids"],
                    ["TMP-REJECTED-LATE-MEMO(unpublished)"],
                )
                self.assertEqual(
                    store.temporary_reference_status("TMP-REJECTED-LATE-MEMO")[
                        "state"
                    ],
                    "abandoned",
                )
                self.assertNotIn(task_id, scheduler.pending_recovery_work()["tasks"])
                self.assertFalse(
                    any(
                        item.get("resolved_at") is None
                        and item.get("attention_id")
                        == f"task:{task_id}:temporary-references"
                        for item in scheduler.state["needs_attention"]
                    )
                )
            finally:
                store.close()

    def test_rejected_update_reference_closes_without_correction(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            store, scheduler, _root_id, route_id = self._reference_scheduler(
                raw, "cancel-reference-correction"
            )
            try:
                report = research_report(83)
                report["main_route_ids"] = [route_id]
                task_id = scheduler.submit_batch(
                    "B-cancel-reference-correction", [report]
                )[0]
                attempt = scheduler.start_task_attempt(task_id)
                scheduler.ingest_progress(
                    final_progress(
                        task_id,
                        attempt,
                        is_final=False,
                        operations=[
                            {
                                "operation_id": "route-update-awaiting-memo",
                                "kind": "route_update",
                                "target_id": route_id,
                                "expected_base_revision": 1,
                                "set": {},
                                "append": {},
                                "add_ids": {
                                    "relevant_memo_ids": ["TMP-RESOLVED-BEFORE-RETRY"]
                                },
                                "remove_ids": {},
                                "explanation": "Attach the later memo atomically.",
                                "supporting_memory_ids": [],
                            }
                        ],
                    )
                )
                self.assertEqual(
                    store.get(route_id)["relevant_memo_ids"],
                    ["TMP-RESOLVED-BEFORE-RETRY"],
                )
                scheduler.ingest_progress(
                    final_progress(
                        task_id,
                        attempt,
                        sequence=2,
                        operations=[
                            {
                                "operation_id": "invalid-memo-before-retry",
                                "kind": "memo",
                                "proposal_id": "TMP-RESOLVED-BEFORE-RETRY",
                                "abstract": "Invalid memo target",
                                "genre": "unsupported",
                                "content": "An operator will select the canonical target.",
                                "related_route_ids": [],
                            }
                        ],
                        completion_evidence_ids=["route-update-awaiting-memo"],
                    )
                )
                task = scheduler.state["tasks"][task_id]
                self.assertEqual(task["state"], TaskState.CLOSED.value)
                self.assertEqual(len(task["attempts"]), 1)
                self.assertNotIn("pending_attempt_supplement", task)
                self.assertNotIn("temporary_reference_correction_attention", task)
                self.assertNotIn(task_id, scheduler.pending_recovery_work()["tasks"])
                self.assertEqual(
                    store.get(route_id)["relevant_memo_ids"],
                    ["TMP-RESOLVED-BEFORE-RETRY(unpublished)"],
                )
                self.assertEqual(
                    store.temporary_reference_status("TMP-RESOLVED-BEFORE-RETRY")[
                        "state"
                    ],
                    "abandoned",
                )
                self.assertFalse(
                    any(
                        item.get("resolved_at") is None
                        and item.get("attention_id")
                        == f"task:{task_id}:temporary-references"
                        for item in scheduler.state["needs_attention"]
                    )
                )
            finally:
                store.close()

    def test_bootstrap_task_computation_and_control_state_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            store = MemoryStore(Path(raw) / "memory.sqlite3", projection_dir=False)
            try:
                scheduler = Scheduler(store)
                root_id = scheduler.bootstrap(root_problem="Prove ROOT")
                root = store.add_obligation(
                    "bootstrap-root",
                    {
                        "id": root_id,
                        "abstract": "The self-contained root problem",
                        "statement": "ROOT holds.",
                        "importance": "This is the project target.",
                        "predecessor_fact_ids": [],
                        "partial_progress": [],
                        "related_route_ids": [],
                        "relations": [],
                    },
                )
                self.assertEqual(root.status, "committed")
                scheduler.bind_canonical_root_obligation()
                route = store.add_route(
                    "bootstrap-route",
                    {
                        "abstract": "Direct route to ROOT",
                        "strategy_description": "Unfold the definitions and prove ROOT directly.",
                        "value_assessment": {
                            "confidence": "plausible",
                            "success_gain": "resolves ROOT",
                            "failure_gain": "isolates the obstruction",
                            "relevance": "central",
                            "novelty": "baseline direct mechanism",
                        },
                        "progress": [],
                        "related_obligation_ids": [root_id],
                        "next_steps": ["Check the first case."],
                        "obstacles": ["Control the boundary."],
                        "active_fact_ids": [],
                        "relevant_memo_ids": [],
                        "relevant_claim_ids": [],
                    },
                )
                scheduler.commit_initial_trim({"category_ids": []})
                report = research_report(1)
                report["main_route_ids"] = [route.canonical_id]
                task_id = scheduler.submit_batch("real-batch", [report])[0]
                attempt = scheduler.start_task_attempt(task_id)
                scheduler.ingest_progress(
                    final_progress(
                        task_id,
                        attempt,
                        computations=[
                            {
                                "staging_id": "real-computation",
                                "description": "Evaluate the boundary toy case.",
                                "assumptions": "Characteristic zero.",
                                "exact_input": "print(0)",
                                "software": {"name": "Python", "version": "3"},
                                "environment_versions": {},
                                "random_seed": None,
                                "output": "0",
                                "exit_status": 0,
                                "error_output": "",
                                "interpretation": "The toy obstruction vanishes.",
                                "related_memory_ids": {
                                    "fact": [],
                                    "route": [route.canonical_id],
                                    "memo": [],
                                    "claim": [],
                                    "obligation": [root_id],
                                },
                                "fact_candidate_operation_ids": [],
                            }
                        ],
                    ),
                    authenticated_computations=True,
                )
                self.assertEqual(store.get(task_id)["final_status"], "progress")
                resumed = Scheduler(store)
                self.assertEqual(resumed.state["tasks"][task_id]["state"], "closed")
                self.assertTrue(resumed.state["root"]["canonical_binding_committed"])
            finally:
                store.close()

    def test_cross_progress_nonfact_resolution_and_explicit_abandonment(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            store = MemoryStore(Path(raw) / "memory.sqlite3", projection_dir=False)
            try:
                scheduler = Scheduler(store)
                root_id = scheduler.bootstrap(root_problem="Prove ROOT")
                store.add_obligation(
                    "bootstrap-root-cross-progress",
                    {
                        "id": root_id,
                        "abstract": "The root problem",
                        "statement": "ROOT holds.",
                        "importance": "This is the target.",
                        "predecessor_fact_ids": [],
                        "partial_progress": [],
                        "related_route_ids": [],
                        "relations": [],
                    },
                )
                scheduler.bind_canonical_root_obligation()
                portfolio_route = store.add_route(
                    "portfolio-route-cross-progress",
                    {
                        "abstract": "Portfolio route",
                        "strategy_description": "Study ROOT directly.",
                        "value_assessment": {
                            "confidence": "plausible",
                            "success_gain": "resolves ROOT",
                            "failure_gain": "locates the obstruction",
                            "relevance": "central",
                            "novelty": "baseline",
                        },
                        "progress": [],
                        "related_obligation_ids": [root_id],
                        "next_steps": [],
                        "obstacles": [],
                        "active_fact_ids": [],
                        "relevant_memo_ids": [],
                        "relevant_claim_ids": [],
                    },
                )
                scheduler.commit_initial_trim({"category_ids": []})

                source_report = research_report(40)
                source_report["main_route_ids"] = [portfolio_route.canonical_id]
                source_task = scheduler.submit_batch("B-source-cross", [source_report])[0]
                source_attempt = scheduler.start_task_attempt(source_task)
                scheduler.ingest_progress(
                    final_progress(
                        source_task,
                        source_attempt,
                        is_final=False,
                        operations=[
                            {
                                "operation_id": "future-route-op",
                                "kind": "route_add",
                                "proposal_id": "TMP-FUTURE-ROUTE",
                                "abstract": "The future route",
                                "strategy_description": "Use the invariant from the memo.",
                                "value_assessment": {
                                    "confidence": "plausible",
                                    "success_gain": "advances ROOT",
                                    "failure_gain": "tests the invariant",
                                    "relevance": "central",
                                    "novelty": "new route",
                                },
                                "progress": [],
                                "related_obligation_ids": [],
                                "next_steps": [],
                                "obstacles": [],
                                "active_fact_ids": [],
                                "relevant_memo_ids": [],
                                "relevant_claim_ids": [],
                            }
                        ],
                    )
                )
                scheduler.ingest_progress(
                    final_progress(
                        source_task,
                        source_attempt,
                        sequence=2,
                        operations=[
                            {
                                "operation_id": "source-memo-op",
                                "kind": "memo",
                                "proposal_id": "TMP-SOURCE-MEMO",
                                "abstract": "Memo awaiting a future route",
                                "genre": "normal",
                                "content": "The invariant should be attached to the future route.",
                                "related_route_ids": ["TMP-FUTURE-ROUTE"],
                            }
                        ],
                    )
                )
                self.assertEqual(
                    scheduler.state["tasks"][source_task]["state"],
                    TaskState.POSTPROCESSING.value,
                )
                scheduler = Scheduler(store)
                scheduler.reconcile_pending_ingestion()
                self.assertEqual(
                    scheduler.state["tasks"][source_task]["state"],
                    TaskState.POSTPROCESSING.value,
                )

                scheduler.apply_synthesizer_result(
                    "future-route-op",
                    {
                        "resolution": "new",
                        "operation_digest": scheduler.state["operations"][
                            "future-route-op"
                        ]["input_digest"],
                    },
                )
                state = scheduler.state
                self.assertEqual(state["tasks"][source_task]["state"], TaskState.CLOSED.value)
                memo_id = state["operations"]["source-memo-op"]["canonical_id"]
                route_id = state["operations"]["future-route-op"]["canonical_id"]
                self.assertEqual(store.get(memo_id)["related_route_ids"], [route_id])

                abandoned_report = research_report(42)
                abandoned_report["main_route_ids"] = [portfolio_route.canonical_id]
                abandoned_task = scheduler.submit_batch(
                    "B-abandon-cross", [abandoned_report]
                )[0]
                abandoned_attempt = scheduler.start_task_attempt(abandoned_task)
                scheduler.ingest_progress(
                    final_progress(
                        abandoned_task,
                        abandoned_attempt,
                        is_final=False,
                        operations=[
                            {
                                "operation_id": "abandoned-route-op",
                                "kind": "route_add",
                                "proposal_id": "TMP-NEVER-ROUTE",
                                "abstract": "A route proposal later abandoned",
                                "strategy_description": "Try a disposable auxiliary route.",
                                "value_assessment": {
                                    "confidence": "uncertain",
                                    "success_gain": "could advance ROOT",
                                    "failure_gain": "tests the auxiliary idea",
                                    "relevance": "related",
                                    "novelty": "temporary",
                                },
                                "progress": [],
                                "related_obligation_ids": [],
                                "next_steps": [],
                                "obstacles": [],
                                "active_fact_ids": [],
                                "relevant_memo_ids": [],
                                "relevant_claim_ids": [],
                            }
                        ],
                    )
                )
                scheduler.ingest_progress(
                    final_progress(
                        abandoned_task,
                        abandoned_attempt,
                        sequence=2,
                        operations=[
                            {
                                "operation_id": "abandoned-memo-op",
                                "kind": "memo",
                                "proposal_id": "TMP-ABANDONED-MEMO",
                                "abstract": "Memo with an abandoned future route",
                                "genre": "normal",
                                "content": "This link may be abandoned explicitly.",
                                "related_route_ids": ["TMP-NEVER-ROUTE"],
                            }
                        ],
                    )
                )
                self.assertEqual(
                    scheduler.state["tasks"][abandoned_task]["state"],
                    TaskState.POSTPROCESSING.value,
                )
                self.assertEqual(
                    store.temporary_reference_status("TMP-NEVER-ROUTE")["state"],
                    "pending",
                )
                self.assertIn(
                    "TMP-NEVER-ROUTE",
                    {
                        item["temporary_id"]
                        for item in store.list_temporary_references(state="pending")
                    },
                )
                scheduler.abandon_operation(
                    "abandoned-route-op",
                    authorized_by="operator",
                    reason="The route proposal was withdrawn.",
                )
                pending_task = scheduler.state["tasks"][abandoned_task]
                self.assertEqual(pending_task["state"], TaskState.CLOSED.value)
                self.assertNotIn("pending_attempt_supplement", pending_task)
                self.assertNotIn("temporary_reference_blocks", pending_task)
                self.assertEqual(
                    store.temporary_reference_status("TMP-NEVER-ROUTE")["state"],
                    "abandoned",
                )
                abandoned_memo_id = scheduler.state["operations"][
                    "abandoned-memo-op"
                ]["canonical_id"]
                self.assertEqual(
                    store.get(abandoned_memo_id)["related_route_ids"],
                    ["TMP-NEVER-ROUTE(unpublished)"],
                )

            finally:
                store.close()

    def test_synthesized_route_update_resolves_waiting_task_and_replays(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            store = MemoryStore(Path(raw) / "memory.sqlite3", projection_dir=False)
            try:
                scheduler = Scheduler(store)
                root_id = scheduler.bootstrap(root_problem="Prove ROOT")
                store.add_obligation(
                    "bootstrap-root-synth-update",
                    {
                        "id": root_id,
                        "abstract": "The root problem",
                        "statement": "ROOT holds.",
                        "importance": "This is the target.",
                        "predecessor_fact_ids": [],
                        "partial_progress": [],
                        "related_route_ids": [],
                        "relations": [],
                    },
                )
                scheduler.bind_canonical_root_obligation()
                existing = store.add_route(
                    "existing-route-synth-update",
                    {
                        "abstract": "Existing route",
                        "strategy_description": "Use the established reduction.",
                        "value_assessment": {
                            "confidence": "plausible",
                            "success_gain": "advances ROOT",
                            "failure_gain": "locates the obstruction",
                            "relevance": "central",
                            "novelty": "baseline",
                        },
                        "progress": [],
                        "related_obligation_ids": [root_id],
                        "next_steps": [],
                        "obstacles": [],
                        "active_fact_ids": [],
                        "relevant_memo_ids": [],
                        "relevant_claim_ids": [],
                    },
                )
                route_id = existing.canonical_id
                assert route_id
                scheduler.commit_initial_trim({"category_ids": []})

                source_report = research_report(70)
                source_report["main_route_ids"] = [route_id]
                source_task = scheduler.submit_batch(
                    "B-source-synth-update", [source_report]
                )[0]
                source_attempt = scheduler.start_task_attempt(source_task)
                scheduler.ingest_progress(
                    final_progress(
                        source_task,
                        source_attempt,
                        is_final=False,
                        operations=[
                            {
                                "operation_id": "memo-awaits-synth-update",
                                "kind": "memo",
                                "proposal_id": "TMP-MEMO-SYNTH-UPDATE",
                                "abstract": "Memo awaiting the proposed route",
                                "genre": "normal",
                                "content": "Attach this memo when the route proposal resolves.",
                                "related_route_ids": ["TMP-ROUTE-SYNTH-UPDATE"],
                            }
                        ],
                    )
                )
                memo_id = scheduler.state["operations"][
                    "memo-awaits-synth-update"
                ]["canonical_id"]
                self.assertEqual(
                    store.temporary_reference_status("TMP-ROUTE-SYNTH-UPDATE")[
                        "state"
                    ],
                    "pending",
                )
                scheduler = Scheduler(store)
                scheduler.ingest_progress(
                    final_progress(
                        source_task,
                        source_attempt,
                        sequence=2,
                        operations=[
                            {
                                "operation_id": "route-proposal-synth-update",
                                "kind": "route_add",
                                "proposal_id": "TMP-ROUTE-SYNTH-UPDATE",
                                "abstract": "A refinement of the existing route",
                                "strategy_description": "Use the established reduction.",
                                "value_assessment": {
                                    "confidence": "stronger",
                                    "success_gain": "advances ROOT",
                                    "failure_gain": "locates the obstruction",
                                    "relevance": "central",
                                    "novelty": "refinement",
                                },
                                "progress": ["A sharper boundary estimate is available."],
                                "related_obligation_ids": [root_id],
                                "next_steps": [],
                                "obstacles": [],
                                "active_fact_ids": [],
                                "relevant_memo_ids": [],
                                "relevant_claim_ids": [],
                            }
                        ],
                    )
                )
                self.assertEqual(
                    scheduler.state["tasks"][source_task]["state"],
                    TaskState.POSTPROCESSING.value,
                )

                scheduler.apply_synthesizer_result(
                    "route-proposal-synth-update",
                    {
                        "resolution": "update",
                        "operation_digest": scheduler.state["operations"][
                            "route-proposal-synth-update"
                        ]["input_digest"],
                        "patch": {
                            "target_id": route_id,
                            "expected_base_revision": 1,
                            "set": {},
                            "append": {
                                "progress": ["A sharper boundary estimate is available."]
                            },
                            "add_ids": {},
                            "remove_ids": {},
                            "explanation": "Absorb the proposal's genuine progress.",
                            "supporting_memory_ids": [],
                        },
                    },
                )
                self.assertEqual(store.get(memo_id)["related_route_ids"], [route_id])
                self.assertEqual(store.get(route_id)["relevant_memo_ids"], [memo_id])
                self.assertEqual(store.get(route_id).revision, 2)
                self.assertEqual(
                    scheduler.state["tasks"][source_task]["state"],
                    TaskState.CLOSED.value,
                )
                self.assertTrue(
                    scheduler.state["operations"]["route-proposal-synth-update"][
                        "update_reference_resolution_applied"
                    ]
                )

                resumed = Scheduler(store)
                resumed.reconcile_pending_ingestion()
                self.assertEqual(store.get(route_id).revision, 2)
                self.assertEqual(store.get(memo_id).metadata_version, 2)
            finally:
                store.close()

    def test_recovery_repairs_legacy_committed_synthesized_update_tail(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            store = MemoryStore(Path(raw) / "memory.sqlite3", projection_dir=False)
            try:
                scheduler = Scheduler(store)
                root_id = scheduler.bootstrap(root_problem="Prove ROOT")
                store.add_obligation(
                    "bootstrap-root-legacy-update",
                    {
                        "id": root_id,
                        "abstract": "The root problem",
                        "statement": "ROOT holds.",
                        "importance": "This is the target.",
                        "predecessor_fact_ids": [],
                        "partial_progress": [],
                        "related_route_ids": [],
                        "relations": [],
                    },
                )
                scheduler.bind_canonical_root_obligation()
                existing = store.add_route(
                    "existing-route-legacy-update",
                    {
                        "abstract": "Existing legacy route",
                        "strategy_description": "Use the legacy reduction.",
                        "value_assessment": {
                            "confidence": "plausible",
                            "success_gain": "advances ROOT",
                            "failure_gain": "locates the obstruction",
                            "relevance": "central",
                            "novelty": "baseline",
                        },
                        "progress": [],
                        "related_obligation_ids": [root_id],
                        "next_steps": [],
                        "obstacles": [],
                        "active_fact_ids": [],
                        "relevant_memo_ids": [],
                        "relevant_claim_ids": [],
                    },
                )
                route_id = existing.canonical_id
                assert route_id
                scheduler.commit_initial_trim({"category_ids": []})

                source_report = research_report(72)
                source_report["main_route_ids"] = [route_id]
                source_task = scheduler.submit_batch(
                    "B-source-legacy-update", [source_report]
                )[0]
                source_attempt = scheduler.start_task_attempt(source_task)
                scheduler.ingest_progress(
                    final_progress(
                        source_task,
                        source_attempt,
                        operations=[
                            {
                                "operation_id": "route-proposal-legacy-update",
                                "kind": "route_add",
                                "proposal_id": "TMP-ROUTE-LEGACY-UPDATE",
                                "abstract": "Legacy route proposal",
                                "strategy_description": "Use the legacy reduction.",
                                "value_assessment": {
                                    "confidence": "plausible",
                                    "success_gain": "advances ROOT",
                                    "failure_gain": "locates the obstruction",
                                    "relevance": "central",
                                    "novelty": "refinement",
                                },
                                "progress": [],
                                "related_obligation_ids": [root_id],
                                "next_steps": [],
                                "obstacles": [],
                                "active_fact_ids": [],
                                "relevant_memo_ids": [],
                                "relevant_claim_ids": [],
                            },
                            {
                                "operation_id": "memo-awaits-legacy-update",
                                "kind": "memo",
                                "proposal_id": "TMP-MEMO-LEGACY-UPDATE",
                                "abstract": "Memo awaiting a legacy update",
                                "genre": "normal",
                                "content": "The old runtime left this link pending.",
                                "related_route_ids": ["TMP-ROUTE-LEGACY-UPDATE"],
                            }
                        ],
                    )
                )
                patch = {
                    "target_id": route_id,
                    "expected_base_revision": 1,
                    "set": {},
                    "append": {"progress": ["Legacy progress already committed."]},
                    "add_ids": {},
                    "remove_ids": {},
                    "explanation": "Simulate the pre-fix committed update.",
                    "supporting_memory_ids": [],
                }
                store.update_route(
                    "route-proposal-legacy-update", patch, actor="legacy-runtime"
                )
                with scheduler._mutate() as state:
                    operation = state["operations"]["route-proposal-legacy-update"]
                    operation["synthesizer_result"] = {
                        "resolution": "update",
                        "operation_digest": operation["input_digest"],
                        "patch": patch,
                    }
                    operation["synthesizer_result_digest"] = "legacy-result"
                    operation["state"] = OperationState.COMMITTED.value
                    operation["canonical_id"] = route_id
                    state["proposal_mappings"]["TMP-ROUTE-LEGACY-UPDATE"] = route_id

                resumed = Scheduler(store)
                resumed.reconcile_pending_ingestion()
                self.assertEqual(
                    store.temporary_reference_status("TMP-ROUTE-LEGACY-UPDATE")[
                        "state"
                    ],
                    "resolved",
                )
                mapping = store.proposal_mapping("TMP-ROUTE-LEGACY-UPDATE")
                self.assertEqual(mapping["canonical_id"], route_id)
                self.assertEqual(mapping["resolution"], "updated")
                memo_id = resumed.state["operations"][
                    "memo-awaits-legacy-update"
                ]["canonical_id"]
                self.assertEqual(store.get(memo_id)["related_route_ids"], [route_id])
                self.assertEqual(store.get(route_id).revision, 2)
                self.assertTrue(
                    resumed.state["operations"]["route-proposal-legacy-update"][
                        "update_reference_resolution_applied"
                    ]
                )
                resumed.reconcile_pending_ingestion()
                self.assertEqual(store.get(route_id).revision, 2)
                self.assertEqual(store.get(memo_id).metadata_version, 2)
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
