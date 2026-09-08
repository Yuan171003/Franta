from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from franta.access import policy_for
from franta.materialize import WorkspaceMaterializer
from franta.scheduler import Scheduler, SchedulerError
from franta.testing import FakeControlStore, SimulatedCrash
from franta.workflows import OperationState, TaskState


def _research_report(index: int) -> dict:
    return {
        "report_id": f"AR-APPROVED-{index}",
        "objective": f"Investigate approved regression {index}",
        "if_resume": None,
        "mode": "research",
        "main_route_ids": [f"R-approved-{index}"],
        "main_obligation_ids": [],
        "perspective": None,
        "portfolio": {
            "fact": [],
            "route": [],
            "memo": [],
            "claim": [],
            "obligation": [],
            "computation": [],
        },
        "reason": "focused regression",
    }


def _progress(
    task_id: str,
    attempt: int,
    *,
    sequence: int = 1,
    is_final: bool = True,
    operations: list[dict] | None = None,
    computations: list[dict] | None = None,
    evidence: list[str] | None = None,
) -> dict:
    evidence = list(evidence or [])
    return {
        "progress_id": f"P-{task_id}-{attempt}-{sequence}",
        "task_id": task_id,
        "attempt": attempt,
        "sequence": sequence,
        "is_final": is_final,
        "outcome_status": "progress",
        "progress_since_previous": "Focused regression progress.",
        "operations": list(operations or []),
        "computations": list(computations or []),
        "fact_challenges": [],
        "completion_evidence_ids": evidence,
        "attempt_summary": {
            "work_mode": "research",
            "task": "Exercise the approved regression.",
            "proposed_outcome": "progress",
            "cumulative_important_progress": "The regression path completed.",
            "completion_evidence_operation_ids": list(evidence),
            "most_promising_next_steps": "None for this focused test.",
        },
    }


def _cas(staging_id: str) -> dict:
    return {
        "staging_id": staging_id,
        "description": "Compute an exact integer sum.",
        "assumptions": "Integer arithmetic.",
        "exact_input": "2 + 2",
        "software": {"name": "SageMath", "version": "test"},
        "environment_versions": {},
        "random_seed": None,
        "output": "4",
        "exit_status": 0,
        "error_output": "",
        "interpretation": "The exact result is four.",
        "related_memory_ids": {},
        "fact_candidate_operation_ids": [],
    }


def _memo(operation_id: str) -> dict:
    return {
        "operation_id": operation_id,
        "kind": "memo",
        "proposal_id": f"TMP-{operation_id}",
        "abstract": "A focused regression memo",
        "genre": "normal",
        "content": "The focused regression reached this durable checkpoint.",
        "related_route_ids": [],
    }


class _RejectingComputationStore(FakeControlStore):
    def apply_operation(
        self,
        operation_id,
        operation_type,
        payload,
        *,
        proposal_id=None,
        actor="scheduler",
    ):
        if operation_type == "computation":
            return {
                "status": "rejected",
                "canonical_id": None,
                "error": "deliberate computation rejection",
            }
        return super().apply_operation(
            operation_id,
            operation_type,
            payload,
            proposal_id=proposal_id,
            actor=actor,
        )


class ApprovedFixRegressionTests(unittest.TestCase):
    def _scheduler(self, store: FakeControlStore | None = None) -> Scheduler:
        store = store or FakeControlStore()
        for index in range(20):
            store.records[f"R-approved-{index}"] = {
                "id": f"R-approved-{index}",
                "type": "route",
                "active": True,
                "status": "active",
            }
        store.records["O-approved-premise"] = {
            "id": "O-approved-premise",
            "type": "obligation",
            "revision": 1,
            "statement": "An approved premise",
            "active": True,
            "status": "active",
        }
        scheduler = Scheduler(store)
        scheduler.bootstrap(root_problem="Prove ROOT")
        scheduler.commit_initial_trim({"category_ids": []})
        return scheduler

    @staticmethod
    def _submit(scheduler: Scheduler, batch: str, index: int) -> tuple[str, int]:
        task_id = scheduler.submit_batch(batch, [_research_report(index)])[0]
        return task_id, scheduler.start_task_attempt(task_id)

    def test_root_conclusion_is_not_reserved_as_a_cross_task_temporary_id(self) -> None:
        scheduler = self._scheduler()
        first_task, first_attempt = self._submit(scheduler, "B-ROOT-1", 1)
        second_task, second_attempt = self._submit(scheduler, "B-ROOT-2", 2)

        def obligation(operation_id: str) -> dict:
            return {
                "operation_id": operation_id,
                "kind": "obligation_add",
                "proposal_id": f"TMP-{operation_id}",
                "abstract": "An obligation relation concluding the root problem",
                "statement": "Prove the indicated intermediate statement.",
                "importance": "It directly advances the root problem.",
                "predecessor_fact_ids": [],
                "partial_progress": [],
                "related_route_ids": [],
                "relations": [
                    {
                        "relation_type": "suffices_for",
                        "premise_memory_ids": ["O-approved-premise"],
                        "conclusion": "ROOT",
                        "explanation": "The premise implies the root conclusion.",
                        "supporting_fact_ids": [],
                    }
                ],
            }

        scheduler.ingest_progress(
            _progress(
                first_task,
                first_attempt,
                is_final=False,
                operations=[obligation("OP-ROOT-1")],
            )
        )
        scheduler.ingest_progress(
            _progress(
                second_task,
                second_attempt,
                is_final=False,
                operations=[obligation("OP-ROOT-2")],
            )
        )

        for operation_id in ("OP-ROOT-1", "OP-ROOT-2"):
            operation = scheduler.state["operations"][operation_id]
            self.assertEqual(operation["state"], OperationState.SYNTHESIZING.value)
            self.assertIsNone(operation["error"])

    def test_materialized_record_progress_skill_contains_complete_premise_rule(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            workspace = WorkspaceMaterializer(Path(raw) / "workspaces").create(
                "approved-premise-rule",
                root_problem="Prove ROOT",
                policy=policy_for("worker", mode="research"),
                task_card={"task_id": "T-APPROVED", "attempt": 1},
                skills=("record-progress",),
            )
            text = (
                workspace.path / ".agents/skills/record-progress/SKILL.md"
            ).read_text(encoding="utf-8")
        compact = " ".join(text.split())

        for required in (
            "`premise_memory_ids` must be a nonempty, exhaustive list",
            "actual Fact or Obligation premises on the left-hand side",
            "not an implicit premise",
            "use its canonical ID if it already exists",
            "own temporary `proposal_id`",
        ):
            self.assertIn(required, compact)

    def test_cas_only_final_completion_evidence_closes(self) -> None:
        scheduler = self._scheduler()
        task_id, attempt = self._submit(scheduler, "B-CAS-FINAL", 3)

        scheduler.ingest_progress(
            _progress(
                task_id,
                attempt,
                computations=[_cas("CAS-FINAL-ONLY")],
                evidence=["CAS-FINAL-ONLY"],
            ),
            authenticated_computations=True,
        )

        state = scheduler.state
        self.assertEqual(
            state["computations"]["CAS-FINAL-ONLY"]["state"],
            OperationState.COMMITTED.value,
        )
        self.assertEqual(state["tasks"][task_id]["state"], TaskState.CLOSED.value)

    def test_prior_progress_cas_can_be_final_completion_evidence(self) -> None:
        scheduler = self._scheduler()
        task_id, attempt = self._submit(scheduler, "B-CAS-PRIOR", 4)
        scheduler.ingest_progress(
            _progress(
                task_id,
                attempt,
                is_final=False,
                computations=[_cas("CAS-PRIOR-PROGRESS")],
            ),
            authenticated_computations=True,
        )

        scheduler.ingest_progress(
            _progress(
                task_id,
                attempt,
                sequence=2,
                evidence=["CAS-PRIOR-PROGRESS"],
            )
        )

        self.assertEqual(
            scheduler.state["tasks"][task_id]["state"], TaskState.CLOSED.value
        )

    def test_same_task_operation_and_computation_evidence_collision_is_atomic(self) -> None:
        scheduler = self._scheduler()
        task_id, attempt = self._submit(scheduler, "B-CAS-AMBIGUOUS", 5)
        revision = scheduler.revision

        with self.assertRaisesRegex(
            SchedulerError, "ambiguous completion evidence IDs"
        ):
            scheduler.ingest_progress(
                _progress(
                    task_id,
                    attempt,
                    operations=[_memo("SHARED-EVIDENCE-ID")],
                    computations=[_cas("SHARED-EVIDENCE-ID")],
                    evidence=["SHARED-EVIDENCE-ID"],
                ),
                authenticated_computations=True,
            )

        state = scheduler.state
        self.assertEqual(scheduler.revision, revision)
        self.assertNotIn(f"P-{task_id}-{attempt}-1", state["progress"])
        self.assertNotIn("SHARED-EVIDENCE-ID", state["operations"])
        self.assertNotIn("SHARED-EVIDENCE-ID", state["computations"])
        self.assertEqual(state["tasks"][task_id]["state"], TaskState.RUNNING.value)
        self.assertEqual(state["tasks"][task_id]["attempts"][-1]["last_sequence"], 0)

    def test_cross_task_id_collision_uses_current_task_namespace(self) -> None:
        scheduler = self._scheduler()
        first_task, first_attempt = self._submit(scheduler, "B-CROSS-OP", 6)
        scheduler.ingest_progress(
            _progress(
                first_task,
                first_attempt,
                operations=[
                    {
                        "operation_id": "CROSS-TASK-EVIDENCE",
                        "kind": "not-a-memory-operation",
                    }
                ],
            )
        )
        self.assertEqual(
            scheduler.state["operations"]["CROSS-TASK-EVIDENCE"]["state"],
            OperationState.REJECTED.value,
        )

        second_task, second_attempt = self._submit(scheduler, "B-CROSS-CAS", 7)
        scheduler.ingest_progress(
            _progress(
                second_task,
                second_attempt,
                computations=[_cas("CROSS-TASK-EVIDENCE")],
                evidence=["CROSS-TASK-EVIDENCE"],
            ),
            authenticated_computations=True,
        )

        state = scheduler.state
        self.assertEqual(
            state["computations"]["CROSS-TASK-EVIDENCE"]["task_id"], second_task
        )
        self.assertEqual(
            state["computations"]["CROSS-TASK-EVIDENCE"]["state"],
            OperationState.COMMITTED.value,
        )
        self.assertEqual(
            state["tasks"][second_task]["state"], TaskState.CLOSED.value
        )

    def test_rejected_cas_completion_evidence_uses_existing_correction_path(self) -> None:
        scheduler = self._scheduler(_RejectingComputationStore())
        task_id, attempt = self._submit(scheduler, "B-CAS-REJECTED", 8)

        scheduler.ingest_progress(
            _progress(
                task_id,
                attempt,
                computations=[_cas("CAS-REJECTED-EVIDENCE")],
                evidence=["CAS-REJECTED-EVIDENCE"],
            ),
            authenticated_computations=True,
        )

        task = scheduler.state["tasks"][task_id]
        self.assertEqual(task["state"], TaskState.REVISION_PENDING.value)
        self.assertEqual(
            task["pending_attempt_supplement"],
            {
                "kind": "completion_evidence_correction",
                "rejected_operation_ids": ["CAS-REJECTED-EVIDENCE"],
            },
        )

    def test_received_cas_recovery_commits_and_closes_task(self) -> None:
        store = FakeControlStore()
        scheduler = self._scheduler(store)
        task_id, attempt = self._submit(scheduler, "B-CAS-RECOVERY", 9)

        with mock.patch.object(
            scheduler,
            "_commit_computation",
            side_effect=SimulatedCrash("crash after durable final receipt"),
        ):
            with self.assertRaises(SimulatedCrash):
                scheduler.ingest_progress(
                    _progress(
                        task_id,
                        attempt,
                        computations=[_cas("CAS-RECEIVED-RECOVERY")],
                        evidence=["CAS-RECEIVED-RECOVERY"],
                    ),
                    authenticated_computations=True,
                )

        crashed = Scheduler(store).state
        self.assertEqual(
            crashed["computations"]["CAS-RECEIVED-RECOVERY"]["state"],
            OperationState.RECEIVED.value,
        )
        self.assertEqual(
            crashed["tasks"][task_id]["state"], TaskState.POSTPROCESSING.value
        )

        resumed = Scheduler(store)
        resumed.reconcile_pending_ingestion()
        recovered = resumed.state
        self.assertEqual(
            recovered["computations"]["CAS-RECEIVED-RECOVERY"]["state"],
            OperationState.COMMITTED.value,
        )
        self.assertEqual(
            recovered["tasks"][task_id]["state"], TaskState.CLOSED.value
        )

    def test_rejected_operation_remains_allowed_as_completion_evidence(self) -> None:
        scheduler = self._scheduler()
        task_id, attempt = self._submit(scheduler, "B-REJECTED-OP-EVIDENCE", 10)

        scheduler.ingest_progress(
            _progress(
                task_id,
                attempt,
                operations=[
                    {
                        "operation_id": "OP-REJECTED-EVIDENCE",
                        "kind": "not-a-memory-operation",
                    }
                ],
                evidence=["OP-REJECTED-EVIDENCE"],
            )
        )

        task = scheduler.state["tasks"][task_id]
        self.assertEqual(task["state"], TaskState.REVISION_PENDING.value)
        self.assertEqual(
            task["pending_attempt_supplement"],
            {
                "kind": "completion_evidence_correction",
                "rejected_operation_ids": ["OP-REJECTED-EVIDENCE"],
            },
        )


if __name__ == "__main__":
    unittest.main()
