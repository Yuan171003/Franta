from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path

from franta.config import load_manifest
from franta.runtime import AgentCall, FrantaRuntime
from franta.scheduler import IdempotencyConflict, Scheduler
from franta.skill_runtime import SkillContext, SkillRuntime
from franta.testing import FakeControlStore, SimulatedCrash
from franta.workflows import OperationState


MEMORY_GROUPS = ("fact", "route", "memo", "claim", "obligation", "computation")


def _portfolio() -> dict[str, list[str]]:
    return {kind: [] for kind in MEMORY_GROUPS}


def _assignment() -> dict:
    return {
        "report_id": "AR-VERIFY-CAS",
        "objective": "Prove the test statement.",
        "if_resume": None,
        "mode": "associate",
        "main_route_ids": [],
        "main_obligation_ids": [],
        "perspective": None,
        "portfolio": _portfolio(),
        "reason": "Exercise optional verifier CAS provenance.",
    }


def _fact_operation() -> dict:
    return {
        "operation_id": "OP-VERIFY-CAS-FACT",
        "kind": "fact",
        "proposal_id": "TMP-VERIFY-CAS-FACT",
        "candidate_id": "FC-VERIFY-CAS",
        "candidate_version": 1,
        "statement": "Every test object has property P.",
        "proof": "This follows from the defining test-object axiom.",
        "predecessor_fact_ids": [],
        "abstract": "Test objects have property P.",
        "keywords": ["test object"],
        "introduced_notation": [],
        "external_references": [],
        "related_route_ids": [],
    }


def _exact_report(bundle: dict) -> dict:
    return {
        "verdict": "correct",
        "candidate_id": bundle["candidate_id"],
        "candidate_version": bundle["candidate_version"],
        "operation_id": bundle["operation_id"],
        "bundle_digest": bundle["bundle_digest"],
        "verifier_attempt_id": bundle["verifier_attempt_id"],
        "predecessor_ids": bundle["predecessor_ids"],
        "introduced_notation": bundle["introduced_notation"],
        "external_references": bundle["external_references"],
        "root_resolution": bundle["root_resolution"],
        "errors": [],
    }


def _computation() -> dict:
    return {
        "operation_id": "CAS-VERIFY-1",
        "staging_id": "CAS-VERIFY-1",
        "software": {"name": "test-cas", "version": "1.0"},
        "exact_input": "factor(6)",
        "output": "2 * 3",
        "exit_status": 0,
        "description": "Factor a test integer.",
        "assumptions": "Integer arithmetic.",
        "environment_versions": {},
        "random_seed": None,
        "error_output": "",
        "interpretation": "The check supports the submitted arithmetic step.",
        "related_memory_ids": {},
        "fact_candidate_operation_ids": ["OP-VERIFY-CAS-FACT"],
    }


def _prepared_scheduler(store: FakeControlStore) -> tuple[Scheduler, str, int, str]:
    scheduler = Scheduler(store)
    scheduler.bootstrap(root_problem="Prove the test property.")
    scheduler.commit_initial_trim({"category_ids": []})
    task_id = scheduler.submit_batch("B-VERIFY-CAS", [_assignment()])[0]
    attempt = scheduler.start_task_attempt(task_id)
    scheduler.ingest_progress(
        {
            "progress_id": "PRG-VERIFY-CAS",
            "task_id": task_id,
            "attempt": attempt,
            "sequence": 1,
            "is_final": True,
            "outcome_status": "finished",
            "operations": [_fact_operation()],
            "completion_evidence_ids": ["OP-VERIFY-CAS-FACT"],
            "attempt_summary": {
                "work_mode": "associate",
                "task": "Prove the test statement.",
                "proposed_outcome": "finished",
                "cumulative_important_progress": "Submitted a proof.",
                "completion_evidence_operation_ids": ["OP-VERIFY-CAS-FACT"],
                "most_promising_next_steps": "Verify it.",
            },
        }
    )
    operation_id = "OP-VERIFY-CAS-FACT"
    scheduler.apply_synthesizer_result(
        operation_id,
        {
            "resolution": "new",
            "operation_digest": scheduler.state["operations"][operation_id][
                "input_digest"
            ],
            "relied_on": [],
        },
    )
    call_id = scheduler.prepare_verifier_call(operation_id)
    epoch, _ = scheduler.mark_call_running(call_id)
    scheduler.accept_call_result(
        call_id,
        epoch,
        _exact_report(scheduler.state["calls"][call_id]["input"]),
    )
    return scheduler, task_id, attempt, call_id


class VerifierCasSchedulerTests(unittest.TestCase):
    def test_review_computation_is_optional_task_linked_and_idempotent(self) -> None:
        store = FakeControlStore()
        scheduler, task_id, attempt, call_id = _prepared_scheduler(store)
        self.assertEqual(scheduler.ingest_review_computations(call_id, []), {})
        result = scheduler.ingest_review_computations(call_id, [_computation()])
        self.assertEqual(result["CAS-VERIFY-1"], OperationState.COMMITTED.value)
        record = scheduler.state["computations"]["CAS-VERIFY-1"]
        self.assertEqual(record["task_id"], task_id)
        self.assertEqual(record["source_attempt"], attempt)
        self.assertEqual(record["source_call_id"], call_id)
        self.assertIn("CAS-VERIFY-1", scheduler.state["tasks"][task_id]["computation_ids"])
        self.assertIn("CAS-VERIFY-1", scheduler.state["calls"][call_id]["computation_ids"])

        before_events = len(scheduler.state["events"])
        scheduler.ingest_review_computations(call_id, [_computation()])
        self.assertEqual(len(scheduler.state["events"]), before_events)
        changed = copy.deepcopy(_computation())
        changed["output"] = "different"
        with self.assertRaises(IdempotencyConflict):
            scheduler.ingest_review_computations(call_id, [changed])

    def test_received_review_computation_is_replayed_after_scheduler_crash(self) -> None:
        class CrashOnceStore(FakeControlStore):
            crash_computation = False

            def apply_operation(self, operation_id, operation_type, payload, **kwargs):
                if operation_type == "computation" and self.crash_computation:
                    self.crash_computation = False
                    raise SimulatedCrash("after verifier computation receipt")
                return super().apply_operation(
                    operation_id, operation_type, payload, **kwargs
                )

        store = CrashOnceStore()
        scheduler, _, _, call_id = _prepared_scheduler(store)
        store.crash_computation = True
        with self.assertRaises(SimulatedCrash):
            scheduler.ingest_review_computations(call_id, [_computation()])
        resumed = Scheduler(store)
        self.assertEqual(
            resumed.state["computations"]["CAS-VERIFY-1"]["state"],
            OperationState.RECEIVED.value,
        )
        resumed.reconcile_pending_ingestion()
        self.assertEqual(
            resumed.state["computations"]["CAS-VERIFY-1"]["state"],
            OperationState.COMMITTED.value,
        )

    def test_challenge_verifier_computation_keeps_originating_attempt(self) -> None:
        store = FakeControlStore()
        scheduler = Scheduler(store)
        scheduler.bootstrap(root_problem="Prove the test property.")
        scheduler.commit_initial_trim({"category_ids": []})
        store.records["F-CHALLENGED"] = {
            "id": "F-CHALLENGED",
            "type": "fact",
            "active": True,
            "status": "active",
            "statement": "P holds.",
            "proof": "Proof.",
            "predecessor_fact_ids": [],
            "external_references": [],
            "introduced_notation": [],
        }
        task_id = scheduler.submit_batch("B-CHALLENGE-CAS", [_assignment()])[0]
        attempt = scheduler.start_task_attempt(task_id)
        scheduler.ingest_progress(
            {
                "progress_id": "PRG-CHALLENGE-CAS",
                "task_id": task_id,
                "attempt": attempt,
                "sequence": 1,
                "is_final": True,
                "outcome_status": "finished",
                "operations": [],
                "completion_evidence_ids": [],
                "fact_challenges": [
                    {
                        "challenge_id": "CH-CAS",
                        "fact_id": "F-CHALLENGED",
                        "alleged_failure": "Check a boundary case.",
                    }
                ],
                "attempt_summary": {
                    "work_mode": "associate",
                    "task": "Check the fact.",
                    "proposed_outcome": "finished",
                    "cumulative_important_progress": "Raised a precise challenge.",
                    "completion_evidence_operation_ids": [],
                    "most_promising_next_steps": "Verify the challenge.",
                },
            }
        )
        call_id = scheduler.prepare_challenge_verifier_call("CH-CAS")
        epoch, _ = scheduler.mark_call_running(call_id)
        bundle = scheduler.state["calls"][call_id]["input"]
        scheduler.accept_call_result(
            call_id,
            epoch,
            {
                "challenge_id": "CH-CAS",
                "bundle_digest": bundle["bundle_digest"],
                "resolution": "challenge_rejected",
                "justification": "The boundary is covered.",
            },
        )
        scheduler.ingest_review_computations(call_id, [_computation()])
        record = scheduler.state["computations"]["CAS-VERIFY-1"]
        self.assertEqual(record["source_kind"], "challenge-verifier")
        self.assertEqual(record["source_id"], "CH-CAS")
        self.assertEqual(record["source_attempt"], attempt)
        self.assertEqual(record["task_id"], task_id)


class VerifierCasRuntimeTests(unittest.TestCase):
    def test_runtime_consumes_optional_verifier_cas_before_committing_report(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = root / "bootstrap.toml"
            manifest.write_text(
                "\n".join(
                    (
                        "[project]",
                        'name = "verifier-cas-runtime"',
                        'directory = "project"',
                        'root_problem = "Prove the test property."',
                        'foundation_policy = "Use the test-object axiom."',
                        "",
                        "[context_budgets]",
                        "main = 1000",
                        "",
                        "[initial]",
                    )
                )
                + "\n",
                encoding="utf-8",
            )
            def executor(call: AgentCall) -> dict:
                self.assertEqual(call.kind, "verifier")
                SkillRuntime(SkillContext.load(call.workspace.path)).invoke(
                    "CAS",
                    {
                        "operation_id": "CAS-RUNTIME-VERIFY",
                        "software": "test-cas",
                        "software_version": "1.0",
                        "exact_input": "factor(6)",
                        "exact_output": "2 * 3",
                        "exit_status": 0,
                        "description": "Factor a test integer.",
                        "assumptions": "Integer arithmetic.",
                        "interpretation": "The arithmetic check is consistent.",
                        "related_ids": {},
                        "fact_candidate_operation_ids": ["OP-VERIFY-CAS-FACT"],
                    },
                )
                SkillRuntime(SkillContext.load(call.workspace.path)).invoke(
                    "CAS",
                    {
                        "operation_id": "CAS-RUNTIME-FAILED",
                        "software": "test-cas",
                        "software_version": "1.0",
                        "exact_input": "fail()",
                        "exact_output": "partial output",
                        "error_output": "deliberate failure",
                        "exit_status": 1,
                        "description": "Retain one failed verifier computation.",
                        "assumptions": "None.",
                        "interpretation": "The failed run proves nothing.",
                        "related_ids": {},
                        "fact_candidate_operation_ids": ["OP-VERIFY-CAS-FACT"],
                    },
                )
                return _exact_report(dict(call.payload))

            runtime = FrantaRuntime.initialize(
                load_manifest(manifest), executor=executor
            )
            try:
                runtime.start_services()
                runtime.scheduler.commit_initial_trim({"category_ids": []})
                task_id = runtime.scheduler.submit_batch(
                    "B-VERIFY-CAS", [_assignment()]
                )[0]
                attempt = runtime.scheduler.start_task_attempt(task_id)
                runtime.scheduler.ingest_progress(
                    {
                        "progress_id": "PRG-VERIFY-CAS",
                        "task_id": task_id,
                        "attempt": attempt,
                        "sequence": 1,
                        "is_final": True,
                        "outcome_status": "finished",
                        "operations": [_fact_operation()],
                        "completion_evidence_ids": ["OP-VERIFY-CAS-FACT"],
                        "attempt_summary": {
                            "work_mode": "associate",
                            "task": "Prove the test statement.",
                            "proposed_outcome": "finished",
                            "cumulative_important_progress": "Submitted a proof.",
                            "completion_evidence_operation_ids": [
                                "OP-VERIFY-CAS-FACT"
                            ],
                            "most_promising_next_steps": "Verify it.",
                        },
                    }
                )
                operation_id = "OP-VERIFY-CAS-FACT"
                runtime.scheduler.apply_synthesizer_result(
                    operation_id,
                    {
                        "resolution": "new",
                        "operation_digest": runtime.scheduler.state["operations"][
                            operation_id
                        ]["input_digest"],
                        "relied_on": [],
                    },
                )
                call_id = runtime.scheduler.prepare_verifier_call(operation_id)
                runtime._run_memory_review_call(call_id)
                state = runtime.scheduler.state
                computation = state["computations"]["CAS-RUNTIME-VERIFY"]
                self.assertEqual(computation["state"], OperationState.COMMITTED.value)
                self.assertEqual(computation["task_id"], task_id)
                self.assertEqual(computation["source_call_id"], call_id)
                self.assertIsNotNone(computation["canonical_id"])
                canonical = runtime.store.get(str(computation["canonical_id"])).to_dict()
                self.assertEqual(canonical["type"], "computation")
                self.assertEqual(canonical["task_id"], task_id)
                self.assertIn("CAS-RUNTIME-VERIFY", state["calls"][call_id]["computation_ids"])
                self.assertNotIn("CAS-RUNTIME-FAILED", state["computations"])
                archived_paths = {
                    item["relative_path"]
                    for item in state["tasks"][task_id]["artifact_references"]
                }
                self.assertTrue(
                    any("CAS-RUNTIME-FAILED.json" in path for path in archived_paths)
                )
            finally:
                runtime.close()


if __name__ == "__main__":
    unittest.main()
