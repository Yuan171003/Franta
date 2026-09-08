from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from franta.config import load_manifest
from franta.recovery import stable_digest
from franta.runtime import AgentCall, FrantaRuntime, ProjectNeedsAttention
from franta.scheduler import Scheduler, SchedulerError
from franta.skill_runtime import SkillContext, SkillRuntime
from franta.testing import FakeControlStore
from franta.workflows import CallState, OperationState, RetryPolicy, TaskState, WorkflowError


MEMORY_GROUPS = ("fact", "route", "memo", "claim", "obligation", "computation")


def _empty_portfolio() -> dict[str, list[str]]:
    return {kind: [] for kind in MEMORY_GROUPS}


def _manifest(root: Path, *, name: str = "requirement-six-faults") -> Path:
    path = root / "bootstrap.toml"
    path.write_text(
        "\n".join(
            [
                "[project]",
                f'name = "{name}"',
                'directory = "project"',
                'root_problem = "Prove that every test object has property P."',
                'foundation_policy = "Use only the declared test-object axioms."',
                "",
                "[context_budgets]",
                "main = 1000",
                "",
                "[initial]",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return path


def _associate_report(index: int, *, if_resume: str | None = None) -> dict[str, Any]:
    return {
        "report_id": f"AR-{index}",
        "objective": f"Investigate the test obstruction, branch {index}.",
        "if_resume": if_resume,
        "mode": "associate",
        "main_route_ids": [],
        "main_obligation_ids": [],
        "perspective": None,
        "portfolio": _empty_portfolio(),
        "reason": "Exercise durable worker-session behavior.",
    }


def _progress(
    task_id: str,
    attempt: int,
    *,
    progress_id: str,
    sequence: int = 1,
    final: bool = True,
    operations: list[dict[str, Any]] | None = None,
    outcome: str = "progress",
    completion_evidence_ids: list[str] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "progress_id": progress_id,
        "task_id": task_id,
        "attempt": attempt,
        "sequence": sequence,
        "is_final": final,
        "operations": list(operations or []),
        "completion_evidence_ids": list(completion_evidence_ids or []),
    }
    if final:
        payload.update(
            {
                "outcome_status": outcome,
                "attempt_summary": {
                    "work_mode": "associate",
                    "task": "Investigate the test obstruction.",
                    "proposed_outcome": outcome,
                    "cumulative_important_progress": "The durable test result.",
                    "completion_evidence_operation_ids": list(
                        completion_evidence_ids or []
                    ),
                    "most_promising_next_steps": "Continue from the durable state.",
                },
            }
        )
    return payload


def _close_task(scheduler: Scheduler, task_id: str, *, marker: str) -> None:
    attempt = scheduler.start_task_attempt(task_id)
    scheduler.ingest_progress(
        _progress(
            task_id,
            attempt,
            progress_id=f"PRG-{marker}",
            outcome="failed",
        )
    )
    if scheduler.state["tasks"][task_id]["state"] != TaskState.CLOSED.value:
        raise AssertionError(f"test fixture failed to close {task_id}")


def _contains_nested(value: Any, expected: Any) -> bool:
    """Return whether an immutable supplement retained a prior semantic value."""

    if value == expected:
        return True
    if isinstance(value, dict):
        return any(_contains_nested(item, expected) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_contains_nested(item, expected) for item in value)
    return False


def _exact_verifier_report(
    bundle: dict[str, Any],
    verdict: str,
    *,
    errors: list[Any] | None = None,
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


class ExplicitWorkerResumeTests(unittest.TestCase):
    @staticmethod
    def _write_worker_thread_audit(
        runtime: FrantaRuntime, call_id: str, thread_id: str
    ) -> None:
        path = runtime.transport.state_dir / "calls" / f"{call_id}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        rows = [
            {
                "type": "transport.call_started",
                "call_id": call_id,
                "role": "worker",
                "session_key": None,
                "lease_epoch": 1,
                "launch_attempt": 1,
            },
            {
                "type": "transport.codex_event",
                "event": {"type": "thread.started", "thread_id": thread_id},
            },
            {
                "type": "transport.call_ended",
                "returncode": 0,
                "thread_id": thread_id,
            },
        ]
        path.write_text(
            "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
            encoding="utf-8",
        )

    def test_restart_reconstructs_explicit_resume_from_linked_call_audit(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            runtime = FrantaRuntime.initialize(load_manifest(_manifest(root)))
            try:
                runtime.scheduler.commit_initial_trim({"category_ids": []})
                source_id = runtime.scheduler.submit_batch(
                    "B-RESUME-SOURCE", [_associate_report(80)]
                )[0]
                _close_task(runtime.scheduler, source_id, marker="RESUME-SOURCE")
                source_call_id = runtime.scheduler.state["tasks"][source_id][
                    "attempts"
                ][-1]["call_id"]
                self._write_worker_thread_audit(
                    runtime, source_call_id, "thread-resume-source"
                )
                successor_id = runtime.scheduler.submit_batch(
                    "B-RESUME-SUCCESSOR",
                    [_associate_report(81, if_resume=source_id)],
                )[0]
                runtime._prepare_worker_workspace(successor_id)
                project_dir = runtime.layout.root
            finally:
                runtime.close()

            restarted = FrantaRuntime.open(project_dir)
            try:
                task = restarted.scheduler.state["tasks"][successor_id]
                key, resume = restarted._worker_session(task, 1)
                self.assertTrue(resume)
                self.assertEqual(
                    restarted.transport.ledger.resolve(key),
                    "thread-resume-source",
                )
            finally:
                restarted.close()

    def test_explicit_resume_without_audited_thread_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            runtime = FrantaRuntime.initialize(load_manifest(_manifest(root)))
            try:
                runtime.scheduler.commit_initial_trim({"category_ids": []})
                source_id = runtime.scheduler.submit_batch(
                    "B-MISSING-THREAD-SOURCE", [_associate_report(82)]
                )[0]
                _close_task(runtime.scheduler, source_id, marker="MISSING-THREAD")
                successor_id = runtime.scheduler.submit_batch(
                    "B-MISSING-THREAD-SUCCESSOR",
                    [_associate_report(83, if_resume=source_id)],
                )[0]
                runtime._prepare_worker_workspace(successor_id)
                task = runtime.scheduler.state["tasks"][successor_id]
                with self.assertRaises(ProjectNeedsAttention):
                    runtime._worker_session(task, 1)
                state = runtime.scheduler.state
                self.assertEqual(
                    state["tasks"][successor_id]["state"],
                    TaskState.NEEDS_ATTENTION.value,
                )
                call_id = state["tasks"][successor_id]["attempts"][-1][
                    "call_id"
                ]
                self.assertEqual(
                    state["calls"][call_id]["status"],
                    CallState.NEEDS_ATTENTION.value,
                )
                attention = next(
                    item
                    for item in state["needs_attention"]
                    if item["reason"] == "worker_resume_thread_unavailable"
                    and item.get("resolved_at") is None
                )
                self.assertEqual(attention["scope"], "task")
                self.assertEqual(attention["owner_id"], successor_id)
                self.assertFalse(runtime.scheduler.has_blocking_attention())
            finally:
                runtime.close()


class RuntimeControlCallRecoveryTests(unittest.TestCase):
    """Executable specifications for results lost at each durable call phase."""

    @staticmethod
    def _write_main_thread_audit(
        runtime: FrantaRuntime, call_id: str, thread_id: str
    ) -> None:
        path = runtime.transport.state_dir / "calls" / f"{call_id}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        rows = [
            {
                "type": "transport.call_started",
                "call_id": call_id,
                "role": "main",
                "session_key": "main:project",
                "lease_epoch": 1,
                "launch_attempt": 1,
            },
            {
                "type": "transport.codex_event",
                "event": {"type": "thread.started", "thread_id": thread_id},
            },
            {
                "type": "transport.call_ended",
                "returncode": 0,
                "thread_id": thread_id,
            },
        ]
        path.write_text(
            "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
            encoding="utf-8",
        )

    @staticmethod
    def _prepare_main_call(runtime: FrantaRuntime, batch_id: str) -> str:
        context = runtime._main_context(batch_id)
        context["terminal_resolution_call"] = False
        return runtime.scheduler.prepare_call(
            "main",
            context,
            continuation={
                "reserved_batch_id": batch_id,
                "session_key": "main:project",
            },
        )

    def test_main_session_recovers_missing_ledger_binding_from_call_audit(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            runtime = FrantaRuntime.initialize(load_manifest(_manifest(Path(raw))))
            try:
                runtime.scheduler.open_assignment_without_trim()
                call_id = self._prepare_main_call(runtime, "BATCH-MAIN-AUDIT")
                self._write_main_thread_audit(runtime, call_id, "thread-main-audit")

                self.assertIsNone(runtime.transport.ledger.resolve("main:project"))
                self.assertTrue(runtime._main_session_resume(call_id))
                self.assertEqual(
                    runtime.transport.ledger.resolve("main:project"),
                    "thread-main-audit",
                )
            finally:
                runtime.close()

    def test_main_session_recovery_rejects_conflicting_thread_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            runtime = FrantaRuntime.initialize(load_manifest(_manifest(Path(raw))))
            try:
                runtime.scheduler.open_assignment_without_trim()
                first = self._prepare_main_call(runtime, "BATCH-MAIN-CONFLICT-1")
                epoch, _ = runtime.scheduler.mark_call_running(first)
                runtime.scheduler.accept_call_result(
                    first,
                    epoch,
                    {
                        "decision": "wait_for_results",
                        "batch_id": None,
                        "assignment_report_ids": [],
                        "wait_for_task_ids": [],
                        "decline_proof_writer": False,
                    },
                )
                runtime.scheduler.mark_call_committed(first)
                second = self._prepare_main_call(runtime, "BATCH-MAIN-CONFLICT-2")
                self._write_main_thread_audit(runtime, first, "thread-main-one")
                self._write_main_thread_audit(runtime, second, "thread-main-two")

                with self.assertRaises(ProjectNeedsAttention):
                    runtime._main_session_resume(second)
                self.assertIsNone(runtime.transport.ledger.resolve("main:project"))
            finally:
                runtime.close()

    def _exercise_main_recovery(self, phase: str) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            manifest = load_manifest(_manifest(root, name=f"recover-main-{phase}"))
            runtime = FrantaRuntime.initialize(manifest)
            try:
                runtime.scheduler.commit_initial_trim({"category_ids": []})
                pending_task_id = runtime.scheduler.submit_batch(
                    f"BATCH-PENDING-{phase.upper()}",
                    [_associate_report(900 + len(phase))],
                )[0]
                batch_id = f"BATCH-RECOVER-MAIN-{phase.upper()}"
                exact_input = runtime._main_context(batch_id)
                exact_input["terminal_resolution_call"] = False
                call_id = runtime.scheduler.prepare_call(
                    "main",
                    exact_input,
                    continuation={
                        "reserved_batch_id": batch_id,
                        "session_key": "main:project",
                    },
                )
                result = {
                    "decision": "wait_for_results",
                    "batch_id": None,
                    "assignment_report_ids": [],
                    "wait_for_task_ids": [pending_task_id],
                    "decline_proof_writer": False,
                }
                if phase in {"running", "completed"}:
                    epoch, _ = runtime.scheduler.mark_call_running(call_id)
                    if phase == "completed":
                        runtime.scheduler.accept_call_result(call_id, epoch, result)
                project_dir = runtime.layout.root
            finally:
                runtime.close()

            observed: list[AgentCall] = []

            def executor(call: AgentCall) -> dict[str, Any]:
                observed.append(call)
                if call.kind == "main":
                    self.assertEqual(call.call_id, call_id)
                    self.assertEqual(call.payload, exact_input)
                    return result
                if call.kind == "trimmer":
                    return {"decision": "no_trim", "reason": "Recovery already checked."}
                raise AssertionError(f"unexpected recovered call {call.kind}")

            resumed = FrantaRuntime.open(project_dir, executor=executor)
            try:
                resumed.run(resume=True, max_cycles=1)
                state = resumed.scheduler.state
                self.assertEqual(
                    state["calls"][call_id]["status"], CallState.COMMITTED.value
                )
                self.assertEqual(
                    state["main_checkpoint"]["call_id"], call_id
                )
                self.assertEqual(
                    state["main_checkpoint"]["waiting_for"], [pending_task_id]
                )
                recovered_invocations = [item for item in observed if item.call_id == call_id]
                self.assertEqual(len(recovered_invocations), 0 if phase == "completed" else 1)
            finally:
                resumed.close()

    def _exercise_trimmer_recovery(self, phase: str) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            manifest = load_manifest(_manifest(root, name=f"recover-trimmer-{phase}"))
            runtime = FrantaRuntime.initialize(manifest)
            try:
                runtime.scheduler.commit_initial_trim({"category_ids": []})
                batch_id = f"BATCH-STUCK-{phase.upper()}"
                runtime.scheduler.submit_stuck_report(
                    batch_id, {"summary": f"Prepare a {phase} trimmer review."}
                )
                session_id = runtime.scheduler.start_trim_review_round()
                exact_input = runtime._trimmer_context(phase="review")
                call_id = runtime.scheduler.prepare_call(
                    "trimmer",
                    exact_input,
                    continuation={"phase": "review", "session_id": session_id},
                )
                result = {
                    "decision": "no_trim",
                    "reason": f"The {phase} recovered review keeps the portfolio.",
                }
                if phase in {"running", "completed"}:
                    epoch, _ = runtime.scheduler.mark_call_running(call_id)
                    if phase == "completed":
                        runtime.scheduler.accept_call_result(call_id, epoch, result)
                project_dir = runtime.layout.root
            finally:
                runtime.close()

            observed: list[AgentCall] = []

            def executor(call: AgentCall) -> dict[str, Any]:
                observed.append(call)
                if call.kind == "trimmer":
                    self.assertEqual(call.call_id, call_id)
                    self.assertEqual(call.payload, exact_input)
                    return result
                if call.kind == "main":
                    return {
                        "decision": "stuck",
                        "batch_id": None,
                        "assignment_report_ids": [],
                        "wait_for_task_ids": [],
                        "report": {"summary": "No assignment is needed in this probe."},
                        "decline_proof_writer": False,
                    }
                raise AssertionError(f"unexpected recovered call {call.kind}")

            resumed = FrantaRuntime.open(project_dir, executor=executor)
            try:
                resumed.run(resume=True, max_cycles=1)
                state = resumed.scheduler.state
                self.assertEqual(
                    state["calls"][call_id]["status"], CallState.COMMITTED.value
                )
                self.assertTrue(
                    any(
                        event["type"] == "trim_review_decided"
                        and event["payload"]["decision"] == "no_trim"
                        for event in state["events"]
                    )
                )
                recovered_invocations = [item for item in observed if item.call_id == call_id]
                self.assertEqual(len(recovered_invocations), 0 if phase == "completed" else 1)
            finally:
                resumed.close()

    def test_expected_recovery_gap_lost_prepared_main_is_relaunched(self) -> None:
        self._exercise_main_recovery("prepared")

    def test_expected_recovery_gap_lost_running_main_is_fenced_and_relaunched(self) -> None:
        self._exercise_main_recovery("running")

    def test_expected_recovery_gap_completed_main_result_is_committed_without_relaunch(self) -> None:
        self._exercise_main_recovery("completed")

    def test_expected_recovery_gap_lost_prepared_trimmer_is_relaunched(self) -> None:
        self._exercise_trimmer_recovery("prepared")

    def test_expected_recovery_gap_lost_running_trimmer_is_fenced_and_relaunched(self) -> None:
        self._exercise_trimmer_recovery("running")

    def test_expected_recovery_gap_completed_trimmer_result_is_committed_without_relaunch(
        self,
    ) -> None:
        self._exercise_trimmer_recovery("completed")


class WorkerFaultAndLineageTests(unittest.TestCase):
    def _scheduler(self) -> tuple[FakeControlStore, Scheduler]:
        store = FakeControlStore()
        scheduler = Scheduler(store)
        scheduler.bootstrap(root_problem="Prove the test property.")
        scheduler.commit_initial_trim({"category_ids": []})
        return store, scheduler

    def test_retry_exhaustion_drains_pending_operation_before_task_publication(self) -> None:
        store, scheduler = self._scheduler()
        task_id = scheduler.submit_batch("B-DRAIN", [_associate_report(1)])[0]
        attempt = scheduler.start_task_attempt(task_id)
        route_operation = {
            "operation_id": "OP-PENDING-ROUTE",
            "kind": "route_add",
            "proposal_id": "TMP-PENDING-ROUTE",
            "abstract": "A route retained across repeated worker disconnections.",
            "strategy_description": "Reduce the test property to its boundary case.",
            "value_assessment": {
                "confidence": "plausible",
                "success_gain": "would settle the target",
                "failure_gain": "would isolate the obstruction",
                "relevance": "central",
                "novelty": "a distinct boundary reduction",
            },
            "progress": [],
            "related_obligation_ids": [],
            "next_steps": ["Analyze the boundary case."],
            "obstacles": ["No boundary estimate is known."],
            "active_fact_ids": [],
            "relevant_memo_ids": [],
            "relevant_claim_ids": [],
        }
        scheduler.ingest_progress(
            _progress(
                task_id,
                attempt,
                progress_id="PRG-DURABLE-INTERMEDIATE",
                final=False,
                operations=[route_operation],
            )
        )
        self.assertEqual(
            scheduler.state["operations"]["OP-PENDING-ROUTE"]["state"],
            OperationState.SYNTHESIZING.value,
        )

        scheduler.record_worker_interruption(task_id, "disconnect-1")
        for number in (2, 3, 4):
            scheduler.start_task_attempt(task_id)
            scheduler.record_worker_interruption(task_id, f"disconnect-{number}")

        pending = scheduler.state["tasks"][task_id]
        self.assertEqual(pending["state"], TaskState.POSTPROCESSING.value)
        self.assertEqual(pending["final_status"], None)
        self.assertNotIn(task_id, store.records)

        digest = scheduler.state["operations"]["OP-PENDING-ROUTE"]["input_digest"]
        scheduler.apply_synthesizer_result(
            "OP-PENDING-ROUTE",
            {"resolution": "new", "operation_digest": digest, "relied_on": []},
        )

        closed = scheduler.state["tasks"][task_id]
        self.assertEqual(closed["state"], TaskState.CLOSED.value)
        self.assertEqual(closed["final_status"], "interrupted")
        self.assertEqual(
            scheduler.state["operations"]["OP-PENDING-ROUTE"]["state"],
            OperationState.COMMITTED.value,
        )
        self.assertEqual(store.records[task_id]["final_status"], "interrupted")

    def test_expected_full_stop_gap_exhausted_worker_drains_before_canonical_task_publish(
        self,
    ) -> None:
        """Recovery must not bypass postprocessing when its final retry is lost."""

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            initial = FrantaRuntime.initialize(
                load_manifest(_manifest(root, name="full-stop-worker-exhaustion"))
            )
            try:
                initial.scheduler.commit_initial_trim({"category_ids": []})
                task_id = initial.scheduler.submit_batch(
                    "B-FULL-STOP-DRAIN", [_associate_report(3)]
                )[0]
                first_attempt = initial.scheduler.start_task_attempt(task_id)
                route_operation = {
                    "operation_id": "OP-FULL-STOP-ROUTE",
                    "kind": "route_add",
                    "proposal_id": "TMP-FULL-STOP-ROUTE",
                    "abstract": "A pending route retained when the final retry process is lost.",
                    "strategy_description": "Reduce the target to a stable boundary estimate.",
                    "value_assessment": {
                        "confidence": "plausible",
                        "success_gain": "would settle the target",
                        "failure_gain": "would locate the obstruction",
                        "relevance": "central",
                        "novelty": "a boundary estimate not tried before",
                    },
                    "progress": [],
                    "related_obligation_ids": [],
                    "next_steps": ["Prove the boundary estimate."],
                    "obstacles": ["The endpoint is uncontrolled."],
                    "active_fact_ids": [],
                    "relevant_memo_ids": [],
                    "relevant_claim_ids": [],
                }
                initial.scheduler.ingest_progress(
                    _progress(
                        task_id,
                        first_attempt,
                        progress_id="PRG-BEFORE-FULL-STOP",
                        final=False,
                        operations=[route_operation],
                    )
                )
                initial.scheduler.record_worker_interruption(task_id, "disconnect-1")
                for number in (2, 3):
                    initial.scheduler.start_task_attempt(task_id)
                    initial.scheduler.record_worker_interruption(
                        task_id, f"disconnect-{number}"
                    )
                # The fourth running attempt is the final eligible retry.  A
                # full-process stop loses it before a normal attempt summary.
                initial.scheduler.start_task_attempt(task_id)
                project_dir = initial.layout.root
            finally:
                initial.close()

            def executor(call: AgentCall) -> dict[str, Any]:
                if call.kind == "synthesizer":
                    return {
                        "resolution": "new",
                        "operation_digest": call.payload["operation_digest"],
                        "explanation": "The route is distinct.",
                        "canonical_id": None,
                        "relied_on": [],
                        "patch": None,
                    }
                if call.kind == "main":
                    return {
                        "decision": "stuck",
                        "batch_id": None,
                        "assignment_report_ids": [],
                        "wait_for_task_ids": [],
                        "report": {
                            "summary": "The interrupted task has drained its pending route."
                        },
                        "decline_proof_writer": False,
                    }
                if call.kind == "trimmer":
                    return {"decision": "no_trim", "reason": "No trim is needed."}
                raise AssertionError(f"unexpected recovery call {call.kind}")

            resumed = FrantaRuntime.open(project_dir, executor=executor)
            try:
                resumed.run(resume=True, max_cycles=1)
                state = resumed.scheduler.state
                self.assertEqual(
                    state["operations"]["OP-FULL-STOP-ROUTE"]["state"],
                    OperationState.COMMITTED.value,
                )
                self.assertEqual(
                    state["tasks"][task_id]["state"], TaskState.CLOSED.value
                )
                self.assertEqual(state["tasks"][task_id]["final_status"], "interrupted")
                canonical_task = resumed.store.get(task_id).to_dict()
                self.assertEqual(canonical_task["final_status"], "interrupted")
                terminal_event_ids = {
                    event["type"]: event["event_id"]
                    for event in state["events"]
                    if event["type"] in {"synthesizer_resolution_received", "task_closed"}
                }
                self.assertLess(
                    terminal_event_ids["synthesizer_resolution_received"],
                    terminal_event_ids["task_closed"],
                )
            finally:
                resumed.close()

    def test_expected_disconnect_gap_final_record_preserves_operations_not_normal_outcome(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            calls: list[str] = []

            def executor(call: AgentCall) -> dict[str, Any]:
                calls.append(call.kind)
                if call.kind == "worker":
                    card = json.loads(
                        call.workspace.task_card_path.read_text(encoding="utf-8")
                    )
                    SkillRuntime(SkillContext.load(call.workspace.path)).invoke(
                        "record-progress",
                        {
                            "operation_id": "FINAL-STAGED-BEFORE-DISCONNECT",
                            "progress_id": "PRG-STAGED-BEFORE-DISCONNECT",
                            "sequence": 1,
                            "is_final": True,
                            "outcome_status": "progress",
                            "progress_since_previous": "Found a reusable obstruction.",
                            "operations": [
                                {
                                    "operation_id": "OP-STAGED-MEMO",
                                    "kind": "memo",
                                    "proposal_id": "TMP-STAGED-MEMO",
                                    "abstract": "A durable obstruction found before disconnect.",
                                    "genre": "normal",
                                    "content": "The boundary reduction loses the test invariant.",
                                    "related_route_ids": [],
                                }
                            ],
                            "computation_operation_ids": [],
                            "fact_challenges": [],
                            "completion_evidence_ids": [],
                            "attempt_summary": {
                                "work_mode": card["mode"],
                                "task": card["objective"],
                                "proposed_outcome": "progress",
                                "cumulative_important_progress": "A reusable obstruction.",
                                "completion_evidence_operation_ids": [],
                                "most_promising_next_steps": "Repair the boundary reduction.",
                            },
                        },
                    )
                    raise ConnectionError("connection lost after the final skill receipt")
                if call.kind == "main":
                    return {
                        "decision": "stuck",
                        "batch_id": None,
                        "assignment_report_ids": [],
                        "wait_for_task_ids": [],
                        "report": {"summary": "Wait for the interrupted worker retry."},
                        "decline_proof_writer": False,
                    }
                raise AssertionError(f"unexpected call {call.kind}")

            runtime = FrantaRuntime.initialize(
                load_manifest(_manifest(root, name="staged-final-disconnect")),
                executor=executor,
            )
            try:
                runtime.scheduler.commit_initial_trim({"category_ids": []})
                task_id = runtime.scheduler.submit_batch(
                    "B-STAGED-DISCONNECT", [_associate_report(2)]
                )[0]
                runtime.run(max_cycles=1)
                state = runtime.scheduler.state
                self.assertIn("OP-STAGED-MEMO", state["operations"])
                self.assertEqual(
                    state["operations"]["OP-STAGED-MEMO"]["state"],
                    OperationState.COMMITTED.value,
                )
                task = state["tasks"][task_id]
                self.assertEqual(task["state"], TaskState.RETRY_PENDING.value)
                self.assertIsNone(task["proposed_outcome"])
                self.assertEqual(task["attempts"][0]["state"], "interrupted")
                self.assertIn("worker", calls)
            finally:
                runtime.close()

    def test_expected_resume_gap_if_resume_requires_a_closed_source_task(self) -> None:
        _, scheduler = self._scheduler()
        source_id = scheduler.submit_batch("B-SOURCE-OPEN", [_associate_report(10)])[0]
        with self.assertRaisesRegex(SchedulerError, "closed"):
            scheduler.submit_batch(
                "B-ILLEGAL-RESUME",
                [_associate_report(11, if_resume=source_id)],
            )

    def test_expected_resume_gap_six_launch_cap_is_lineage_wide_under_branching(
        self,
    ) -> None:
        _, scheduler = self._scheduler()
        source_id = scheduler.submit_batch("B-LINEAGE-ROOT", [_associate_report(20)])[0]
        _close_task(scheduler, source_id, marker="LINEAGE-ROOT")

        for branch in range(1, 7):
            task_id = scheduler.submit_batch(
                f"B-LINEAGE-BRANCH-{branch}",
                [_associate_report(20 + branch, if_resume=source_id)],
            )[0]
            _close_task(scheduler, task_id, marker=f"LINEAGE-BRANCH-{branch}")

        with self.assertRaisesRegex(SchedulerError, "fresh_start_required"):
            scheduler.submit_batch(
                "B-LINEAGE-BRANCH-7",
                [_associate_report(27, if_resume=source_id)],
            )

    def test_resume_has_one_active_successor_and_allows_compatible_mode_change(self) -> None:
        _, scheduler = self._scheduler()
        source_id = scheduler.submit_batch("B-ACCESS-ROOT", [_associate_report(30)])[0]
        _close_task(scheduler, source_id, marker="ACCESS-ROOT")

        changed_mode = _associate_report(31, if_resume=source_id)
        changed_mode["mode"] = "reformulate"
        successor = scheduler.submit_batch("B-ACCESS-SUCCESSOR", [changed_mode])[0]
        with self.assertRaisesRegex(SchedulerError, "active successor"):
            scheduler.submit_batch(
                "B-ACCESS-SIBLING",
                [_associate_report(32, if_resume=source_id)],
            )
        _close_task(scheduler, successor, marker="ACCESS-SUCCESSOR")

        lineage_id = scheduler.state["tasks"][source_id]["session_lineage_id"]
        lineage = scheduler.state["worker_session_lineages"][lineage_id]
        self.assertEqual(lineage["explicit_resume_launches"], 1)
        self.assertIsNone(lineage["active_task_id"])

    def test_resume_rejects_project_wide_history_into_isolated_mode(self) -> None:
        store, scheduler = self._scheduler()
        store.records["O-ISOLATED"] = {
            "id": "O-ISOLATED",
            "type": "obligation",
            "active": True,
            "status": "active",
        }
        source_id = scheduler.submit_batch("B-WIDE-ROOT", [_associate_report(33)])[0]
        _close_task(scheduler, source_id, marker="WIDE-ROOT")
        isolated = _associate_report(34, if_resume=source_id)
        isolated.update(
            {
                "mode": "brainstorm",
                "main_obligation_ids": ["O-ISOLATED"],
                "portfolio": _empty_portfolio(),
            }
        )
        with self.assertRaisesRegex(SchedulerError, "prior memory access"):
            scheduler.submit_batch("B-NARROW-RESUME", [isolated])

    def test_second_fact_version_repairs_first_verifier_rejection(self) -> None:
        store, scheduler = self._scheduler()
        task_id = scheduler.submit_batch("B-REPAIR-SUCCESS", [_associate_report(40)])[0]

        first_attempt = scheduler.start_task_attempt(task_id)
        first = self._fact_candidate(version=1, operation_id="OP-FACT-V1")
        scheduler.ingest_progress(
            _progress(
                task_id,
                first_attempt,
                progress_id="PRG-FACT-V1",
                operations=[first],
                outcome="finished",
                completion_evidence_ids=["OP-FACT-V1"],
            )
        )
        first_digest = scheduler.state["operations"]["OP-FACT-V1"]["input_digest"]
        scheduler.apply_synthesizer_result(
            "OP-FACT-V1",
            {"resolution": "new", "operation_digest": first_digest, "relied_on": []},
        )
        first_bundle = scheduler.verification_bundle("OP-FACT-V1")
        scheduler.apply_verifier_report(
            "OP-FACT-V1",
            _exact_verifier_report(
                first_bundle,
                "incorrect",
                errors=["The boundary case is not justified."],
            ),
        )
        self.assertEqual(
            scheduler.state["tasks"][task_id]["state"],
            TaskState.REVISION_PENDING.value,
        )

        second_attempt = scheduler.start_task_attempt(task_id)
        second = self._fact_candidate(version=2, operation_id="OP-FACT-V2")
        scheduler.ingest_progress(
            _progress(
                task_id,
                second_attempt,
                progress_id="PRG-FACT-V2",
                operations=[second],
                outcome="finished",
                completion_evidence_ids=["OP-FACT-V2"],
            )
        )
        second_digest = scheduler.state["operations"]["OP-FACT-V2"]["input_digest"]
        scheduler.apply_synthesizer_result(
            "OP-FACT-V2",
            {"resolution": "new", "operation_digest": second_digest, "relied_on": []},
        )
        second_bundle = scheduler.verification_bundle("OP-FACT-V2")
        fact_id = scheduler.apply_verifier_report(
            "OP-FACT-V2",
            _exact_verifier_report(second_bundle, "correct"),
        )

        self.assertIsNotNone(fact_id)
        first_lineage = scheduler.state["fact_lineages"][first["candidate_id"]]
        second_lineage = scheduler.state["fact_lineages"][second["candidate_id"]]
        self.assertEqual(first_lineage["revision_requests"], 1)
        self.assertEqual(second_lineage["revision_requests"], 1)
        self.assertEqual(set(first_lineage["versions"]), {"1"})
        self.assertEqual(set(second_lineage["versions"]), {"1"})
        self.assertEqual(
            second_lineage["repair_chain_id"], first["proposal_id"]
        )
        self.assertFalse(second_lineage["concession_required"])
        task = scheduler.state["tasks"][task_id]
        self.assertEqual(task["state"], TaskState.CLOSED.value)
        self.assertEqual(task["final_status"], "finished")
        self.assertEqual(store.records[str(fact_id)]["proof"], second["proof"])

    def test_stale_fact_rejection_does_not_launch_after_newer_version_publishes(
        self,
    ) -> None:
        store, scheduler = self._scheduler()
        task_id = scheduler.submit_batch("B-STALE-REPAIR", [_associate_report(41)])[0]
        self._submit_rejected_fact_version(
            scheduler,
            task_id,
            version=1,
            operation_id="OP-STALE-FACT-V1",
        )

        second_attempt = scheduler.start_task_attempt(task_id)
        second = self._fact_candidate(version=2, operation_id="OP-STALE-FACT-V2")
        third = self._fact_candidate(version=3, operation_id="OP-STALE-FACT-V3")
        scheduler.ingest_progress(
            _progress(
                task_id,
                second_attempt,
                progress_id="PRG-STALE-FACT-V2",
                sequence=1,
                final=False,
                operations=[second],
            )
        )
        scheduler.ingest_progress(
            _progress(
                task_id,
                second_attempt,
                progress_id="PRG-STALE-FACT-V3",
                sequence=2,
                final=False,
                operations=[third],
            )
        )
        scheduler.ingest_progress(
            _progress(
                task_id,
                second_attempt,
                progress_id="PRG-STALE-FACT-FINAL",
                sequence=3,
                outcome="finished",
                completion_evidence_ids=["OP-STALE-FACT-V3"],
            )
        )
        for operation_id in ("OP-STALE-FACT-V2", "OP-STALE-FACT-V3"):
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

        second_bundle = scheduler.verification_bundle("OP-STALE-FACT-V2")
        second_report = _exact_verifier_report(
            second_bundle,
            "incorrect",
            errors=["Version 2 still omits a required hypothesis."],
        )
        rejected_temporary_ids: list[str] = []
        reject_temporary_predecessor = scheduler.reject_temporary_predecessor
        scheduler.reject_temporary_predecessor = (
            lambda temporary_id, **_kwargs: rejected_temporary_ids.append(temporary_id)
            or ()
        )
        try:
            scheduler.apply_verifier_report("OP-STALE-FACT-V2", second_report)
        finally:
            scheduler.reject_temporary_predecessor = reject_temporary_predecessor

        state = scheduler.state
        lineage = state["fact_lineages"][second["candidate_id"]]
        self.assertEqual(lineage["revision_requests"], 1)
        self.assertEqual(lineage["rejected_bundle_digests"], [])
        self.assertFalse(
            state["operations"]["OP-STALE-FACT-V2"]["repair_scheduled"]
        )
        self.assertFalse(
            state["rejected_verification_bundles"][second_bundle["bundle_digest"]][
                "repair_scheduled"
            ]
        )
        self.assertEqual(
            state["tasks"][task_id]["state"], TaskState.POSTPROCESSING.value
        )
        self.assertNotIn(
            "pending_attempt_supplement", state["tasks"][task_id]
        )
        self.assertEqual(rejected_temporary_ids, [second["proposal_id"]])

        # Pre-fix durable records have no repair_scheduled marker.  Recovery
        # must still recognize this registered version as stale.
        with scheduler._mutate() as mutable:
            mutable["operations"]["OP-STALE-FACT-V2"].pop(
                "repair_scheduled", None
            )
        recovery_rejections: list[str] = []
        scheduler.reject_temporary_predecessor = (
            lambda temporary_id, **_kwargs: recovery_rejections.append(temporary_id)
            or ()
        )
        try:
            scheduler.reconcile_pending_ingestion()
        finally:
            scheduler.reject_temporary_predecessor = reject_temporary_predecessor
        self.assertEqual(
            recovery_rejections,
            [
                scheduler.state["operations"]["OP-STALE-FACT-V1"]["payload"][
                    "proposal_id"
                ],
                second["proposal_id"],
            ],
        )

        # Simulate the exact stale intent persisted by the pre-fix scheduler.
        # Publication of the newer verified version must clean it atomically.
        with scheduler._mutate() as mutable:
            task = mutable["tasks"][task_id]
            scheduler._transition_task(mutable, task, TaskState.REVISION_PENDING)
            task["pending_attempt_supplement"] = {
                "kind": "verifier_revision",
                "candidate_id": second["candidate_id"],
                "revision_request": 2,
                "verification_report": second_report,
            }

        third_bundle = scheduler.verification_bundle("OP-STALE-FACT-V3")
        fact_id = scheduler.apply_verifier_report(
            "OP-STALE-FACT-V3",
            _exact_verifier_report(third_bundle, "correct"),
        )

        self.assertIsNotNone(fact_id)
        final = scheduler.state
        self.assertTrue(final["fact_lineages"][third["candidate_id"]]["closed"])
        self.assertEqual(
            final["fact_lineages"][third["candidate_id"]]["revision_requests"], 1
        )
        self.assertEqual(final["tasks"][task_id]["state"], TaskState.CLOSED.value)
        self.assertEqual(len(final["tasks"][task_id]["attempts"]), 2)
        self.assertNotIn(task_id, scheduler.pending_recovery_work()["tasks"])
        self.assertEqual(
            sum(
                call["kind"] == "worker"
                and call.get("continuation", {}).get("task_id") == task_id
                for call in final["calls"].values()
            ),
            2,
        )
        self.assertTrue(
            any(
                event["type"] == "stale_fact_repair_intent_cancelled"
                for event in final["events"]
            )
        )
        self.assertEqual(store.records[str(fact_id)]["proof"], third["proof"])

    def test_newer_duplicate_cancels_stale_fact_repair_intent(self) -> None:
        store, scheduler = self._scheduler()
        store.records["F-EXISTING-DUPLICATE"] = {
            "id": "F-EXISTING-DUPLICATE",
            "type": "fact",
            "active": True,
            "status": "active",
        }
        task_id = scheduler.submit_batch(
            "B-STALE-REPAIR-DUPLICATE", [_associate_report(43)]
        )[0]
        self._submit_rejected_fact_version(
            scheduler,
            task_id,
            version=1,
            operation_id="OP-STALE-DUPLICATE-V1",
        )

        second_attempt = scheduler.start_task_attempt(task_id)
        second = self._fact_candidate(
            version=2, operation_id="OP-STALE-DUPLICATE-V2"
        )
        third = self._fact_candidate(
            version=3, operation_id="OP-STALE-DUPLICATE-V3"
        )
        scheduler.ingest_progress(
            _progress(
                task_id,
                second_attempt,
                progress_id="PRG-STALE-DUPLICATE-V2",
                sequence=1,
                final=False,
                operations=[second],
            )
        )
        scheduler.ingest_progress(
            _progress(
                task_id,
                second_attempt,
                progress_id="PRG-STALE-DUPLICATE-V3",
                sequence=2,
                operations=[third],
                outcome="finished",
                completion_evidence_ids=["OP-STALE-DUPLICATE-V3"],
            )
        )
        scheduler.apply_synthesizer_result(
            "OP-STALE-DUPLICATE-V2",
            {
                "resolution": "new",
                "operation_digest": scheduler.state["operations"][
                    "OP-STALE-DUPLICATE-V2"
                ]["input_digest"],
                "relied_on": [],
            },
        )
        second_bundle = scheduler.verification_bundle("OP-STALE-DUPLICATE-V2")
        second_report = _exact_verifier_report(
            second_bundle,
            "incorrect",
            errors=["Version 2 remains incomplete."],
        )
        scheduler.apply_verifier_report("OP-STALE-DUPLICATE-V2", second_report)

        with scheduler._mutate() as state:
            task = state["tasks"][task_id]
            scheduler._transition_task(state, task, TaskState.REVISION_PENDING)
            task["pending_attempt_supplement"] = {
                "kind": "verifier_revision",
                "candidate_id": second["candidate_id"],
                "revision_request": 2,
                "verification_report": second_report,
            }

        scheduler.apply_synthesizer_result(
            "OP-STALE-DUPLICATE-V3",
            {
                "resolution": "duplicate",
                "canonical_id": "F-EXISTING-DUPLICATE",
                "operation_digest": scheduler.state["operations"][
                    "OP-STALE-DUPLICATE-V3"
                ]["input_digest"],
                "relied_on": [],
            },
        )

        final = scheduler.state
        self.assertEqual(final["tasks"][task_id]["state"], TaskState.CLOSED.value)
        self.assertEqual(len(final["tasks"][task_id]["attempts"]), 2)
        self.assertNotIn("pending_attempt_supplement", final["tasks"][task_id])
        self.assertNotIn(task_id, scheduler.pending_recovery_work()["tasks"])
        self.assertEqual(
            final["operations"]["OP-STALE-DUPLICATE-V3"]["canonical_id"],
            "F-EXISTING-DUPLICATE",
        )

    def test_stale_identical_bundle_does_not_supersede_newer_fact_version(
        self,
    ) -> None:
        _, scheduler = self._scheduler()
        task_id = scheduler.submit_batch("B-STALE-IDENTICAL", [_associate_report(42)])[0]
        self._submit_rejected_fact_version(
            scheduler,
            task_id,
            version=1,
            operation_id="OP-STALE-IDENTICAL-V1",
        )

        second_attempt = scheduler.start_task_attempt(task_id)
        second = self._fact_candidate(
            version=2, operation_id="OP-STALE-IDENTICAL-V2"
        )
        second["proof"] = scheduler.state["operations"]["OP-STALE-IDENTICAL-V1"][
            "payload"
        ]["proof"]
        third = self._fact_candidate(
            version=3, operation_id="OP-STALE-IDENTICAL-V3"
        )
        scheduler.ingest_progress(
            _progress(
                task_id,
                second_attempt,
                progress_id="PRG-STALE-IDENTICAL-V2",
                sequence=1,
                final=False,
                operations=[second],
            )
        )
        scheduler.ingest_progress(
            _progress(
                task_id,
                second_attempt,
                progress_id="PRG-STALE-IDENTICAL-V3",
                sequence=2,
                final=False,
                operations=[third],
            )
        )
        scheduler.ingest_progress(
            _progress(
                task_id,
                second_attempt,
                progress_id="PRG-STALE-IDENTICAL-FINAL",
                sequence=3,
                outcome="finished",
                completion_evidence_ids=["OP-STALE-IDENTICAL-V3"],
            )
        )

        scheduler.apply_synthesizer_result(
            "OP-STALE-IDENTICAL-V2",
            {
                "resolution": "new",
                "operation_digest": scheduler.state["operations"][
                    "OP-STALE-IDENTICAL-V2"
                ]["input_digest"],
                "relied_on": [],
            },
        )
        stale = scheduler.state
        self.assertEqual(
            stale["operations"]["OP-STALE-IDENTICAL-V2"]["state"],
            OperationState.REJECTED.value,
        )
        self.assertFalse(
            stale["operations"]["OP-STALE-IDENTICAL-V2"]["repair_scheduled"]
        )
        self.assertEqual(
            stale["tasks"][task_id]["state"], TaskState.POSTPROCESSING.value
        )
        self.assertNotIn(
            "pending_attempt_supplement", stale["tasks"][task_id]
        )
        self.assertEqual(
            stale["fact_lineages"][second["candidate_id"]]["revision_requests"], 1
        )

        scheduler.apply_synthesizer_result(
            "OP-STALE-IDENTICAL-V3",
            {
                "resolution": "new",
                "operation_digest": scheduler.state["operations"][
                    "OP-STALE-IDENTICAL-V3"
                ]["input_digest"],
                "relied_on": [],
            },
        )
        third_bundle = scheduler.verification_bundle("OP-STALE-IDENTICAL-V3")
        scheduler.apply_verifier_report(
            "OP-STALE-IDENTICAL-V3",
            _exact_verifier_report(third_bundle, "correct"),
        )
        final = scheduler.state
        self.assertEqual(final["tasks"][task_id]["state"], TaskState.CLOSED.value)
        self.assertEqual(len(final["tasks"][task_id]["attempts"]), 2)
        self.assertEqual(
            final["fact_lineages"][third["candidate_id"]]["revision_requests"], 1
        )

    def test_identical_bundle_race_routes_current_and_stale_operations(self) -> None:
        store, stale_scheduler = self._scheduler()
        store.records["F-RACE-DUPLICATE"] = {
            "id": "F-RACE-DUPLICATE",
            "type": "fact",
            "active": True,
            "status": "active",
        }
        stale_task = stale_scheduler.submit_batch(
            "B-IDENTICAL-RACE-STALE", [_associate_report(44)]
        )[0]
        self._submit_rejected_fact_version(
            stale_scheduler,
            stale_task,
            version=1,
            operation_id="OP-IDENTICAL-RACE-STALE-V1",
        )
        stale_attempt = stale_scheduler.start_task_attempt(stale_task)
        stale_v2 = self._fact_candidate(
            version=2, operation_id="OP-IDENTICAL-RACE-STALE-V2"
        )
        stale_v3 = self._fact_candidate(
            version=3, operation_id="OP-IDENTICAL-RACE-STALE-V3"
        )
        stale_scheduler.ingest_progress(
            _progress(
                stale_task,
                stale_attempt,
                progress_id="PRG-IDENTICAL-RACE-STALE-V2",
                sequence=1,
                final=False,
                operations=[stale_v2],
            )
        )
        stale_scheduler.apply_synthesizer_result(
            "OP-IDENTICAL-RACE-STALE-V2",
            {
                "resolution": "new",
                "operation_digest": stale_scheduler.state["operations"][
                    "OP-IDENTICAL-RACE-STALE-V2"
                ]["input_digest"],
                "relied_on": [],
            },
        )
        stale_scheduler.ingest_progress(
            _progress(
                stale_task,
                stale_attempt,
                progress_id="PRG-IDENTICAL-RACE-STALE-V3",
                sequence=2,
                operations=[stale_v3],
                outcome="finished",
                completion_evidence_ids=["OP-IDENTICAL-RACE-STALE-V3"],
            )
        )
        stale_scheduler.apply_synthesizer_result(
            "OP-IDENTICAL-RACE-STALE-V3",
            {
                "resolution": "duplicate",
                "canonical_id": "F-RACE-DUPLICATE",
                "operation_digest": stale_scheduler.state["operations"][
                    "OP-IDENTICAL-RACE-STALE-V3"
                ]["input_digest"],
                "relied_on": [],
            },
        )
        stale_operation = stale_scheduler.state["operations"][
            "OP-IDENTICAL-RACE-STALE-V2"
        ]
        stale_semantic = stale_scheduler._verification_material(stale_operation)[0]
        stale_digest = stable_digest(stale_semantic)
        with stale_scheduler._mutate() as state:
            state["rejected_verification_bundles"][stale_digest] = {
                "operation_id": "OP-OTHER-REJECTION",
                "candidate_id": "FC-OTHER",
                "verification_report": {"verdict": "incorrect"},
            }
        with self.assertRaisesRegex(SchedulerError, "identical rejected"):
            stale_scheduler.verification_bundle("OP-IDENTICAL-RACE-STALE-V2")
        stale_state = stale_scheduler.state
        self.assertFalse(
            stale_state["operations"]["OP-IDENTICAL-RACE-STALE-V2"][
                "repair_scheduled"
            ]
        )
        self.assertEqual(
            stale_state["tasks"][stale_task]["state"], TaskState.CLOSED.value
        )

        _, current_scheduler = self._scheduler()
        current_task = current_scheduler.submit_batch(
            "B-IDENTICAL-RACE-CURRENT", [_associate_report(45)]
        )[0]
        current_attempt = current_scheduler.start_task_attempt(current_task)
        current_operation_id = "OP-IDENTICAL-RACE-CURRENT"
        current_scheduler.ingest_progress(
            _progress(
                current_task,
                current_attempt,
                progress_id="PRG-IDENTICAL-RACE-CURRENT",
                operations=[
                    self._fact_candidate(
                        version=1, operation_id=current_operation_id
                    )
                ],
                outcome="finished",
                completion_evidence_ids=[current_operation_id],
            )
        )
        current_scheduler.apply_synthesizer_result(
            current_operation_id,
            {
                "resolution": "new",
                "operation_digest": current_scheduler.state["operations"][
                    current_operation_id
                ]["input_digest"],
                "relied_on": [],
            },
        )
        current_operation = current_scheduler.state["operations"][
            current_operation_id
        ]
        current_semantic = current_scheduler._verification_material(
            current_operation
        )[0]
        current_digest = stable_digest(current_semantic)
        with current_scheduler._mutate() as state:
            state["rejected_verification_bundles"][current_digest] = {
                "operation_id": "OP-OTHER-CURRENT-REJECTION",
                "candidate_id": "FC-OTHER-CURRENT",
                "verification_report": {"verdict": "incorrect"},
            }
        rejected_temporary_ids: list[str] = []
        reject_temporary_predecessor = current_scheduler.reject_temporary_predecessor
        current_scheduler.reject_temporary_predecessor = (
            lambda temporary_id, **_kwargs: rejected_temporary_ids.append(temporary_id)
            or ()
        )
        try:
            with self.assertRaisesRegex(SchedulerError, "identical rejected"):
                current_scheduler.verification_bundle(current_operation_id)
        finally:
            current_scheduler.reject_temporary_predecessor = (
                reject_temporary_predecessor
            )
        current_state = current_scheduler.state
        self.assertTrue(
            current_state["operations"][current_operation_id]["repair_scheduled"]
        )
        self.assertEqual(
            rejected_temporary_ids,
            [current_scheduler.state["operations"][current_operation_id]["payload"]["proposal_id"]],
        )
        self.assertEqual(
            current_state["tasks"][current_task]["state"],
            TaskState.REVISION_PENDING.value,
        )

    def test_authorized_verifier_retry_restores_exact_continuation(self) -> None:
        store = FakeControlStore()
        scheduler = Scheduler(store, retry_policy=RetryPolicy(verifier=1))
        scheduler.bootstrap(root_problem="Prove the test property.")
        scheduler.commit_initial_trim({"category_ids": []})
        task_id = scheduler.submit_batch(
            "B-AUTHORIZED-VERIFIER-RETRY", [_associate_report(401)]
        )[0]
        attempt = scheduler.start_task_attempt(task_id)
        operation_id = "OP-AUTHORIZED-VERIFIER-RETRY"
        candidate = self._fact_candidate(version=1, operation_id=operation_id)
        scheduler.ingest_progress(
            _progress(
                task_id,
                attempt,
                progress_id="PRG-AUTHORIZED-VERIFIER-RETRY",
                operations=[candidate],
                outcome="finished",
                completion_evidence_ids=[operation_id],
            )
        )
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
        exact_input = scheduler.state["calls"][call_id]["input"]

        for expected_retry in (True, False):
            epoch, _ = scheduler.mark_call_running(call_id)
            scheduler.accept_call_result(call_id, epoch, {"verdict": "malformed"})
            self.assertEqual(
                scheduler.reject_call_result(call_id, "invalid verifier report"),
                expected_retry,
            )

        exhausted = scheduler.state
        self.assertEqual(
            exhausted["calls"][call_id]["status"], CallState.NEEDS_ATTENTION.value
        )
        self.assertEqual(
            exhausted["operations"][operation_id]["state"],
            OperationState.NEEDS_ATTENTION.value,
        )
        self.assertEqual(
            exhausted["tasks"][task_id]["state"], TaskState.NEEDS_ATTENTION.value
        )
        attention = next(
            item
            for item in exhausted["needs_attention"]
            if item["attention_id"] == f"call:{call_id}"
            and item.get("resolved_at") is None
        )
        self.assertEqual(attention["scope"], "task")
        self.assertEqual(attention["owner_id"], task_id)
        self.assertFalse(scheduler.has_blocking_attention())

        scheduler.retry_attention_call(call_id)
        authorized = scheduler.state
        self.assertEqual(
            authorized["calls"][call_id]["status"], CallState.RETRY_PENDING.value
        )
        self.assertEqual(
            authorized["operations"][operation_id]["state"],
            OperationState.VERIFYING.value,
        )
        self.assertEqual(
            authorized["tasks"][task_id]["state"], TaskState.NEEDS_ATTENTION.value
        )
        self.assertEqual(authorized["calls"][call_id]["input"], exact_input)

        recovery = scheduler.recover()
        self.assertIn(call_id, recovery.retry_call_ids)
        epoch, _ = scheduler.mark_call_running(call_id)
        self.assertEqual(scheduler.state["calls"][call_id]["input"], exact_input)
        scheduler.accept_call_result(
            call_id, epoch, _exact_verifier_report(exact_input, "correct")
        )
        fact_id = scheduler.commit_verifier_call(call_id)
        self.assertIsNotNone(fact_id)
        final = scheduler.state
        self.assertEqual(final["calls"][call_id]["status"], CallState.COMMITTED.value)
        self.assertEqual(
            final["operations"][operation_id]["state"], OperationState.COMMITTED.value
        )
        self.assertEqual(final["tasks"][task_id]["state"], TaskState.CLOSED.value)

    def test_exhausted_verifier_does_not_block_healthy_sibling_verifier(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            manifest_path = _manifest(root, name="task-local-verifier-exhaustion")
            manifest_path.write_text(
                manifest_path.read_text(encoding="utf-8")
                + "\n[retries]\nverifier_transport = 1\n",
                encoding="utf-8",
            )
            bad_operation_id = "OP-A-BAD-VERIFIER"
            healthy_operation_id = "OP-B-HEALTHY-VERIFIER"
            verifier_invocations: list[str] = []

            def executor(call: AgentCall) -> dict[str, Any]:
                self.assertEqual(call.kind, "verifier")
                operation_id = str(call.payload["operation_id"])
                verifier_invocations.append(operation_id)
                if operation_id == bad_operation_id:
                    # This passes the shallow verdict check but fails the exact
                    # verifier-envelope validation until its retry is exhausted.
                    return {"verdict": "correct"}
                return _exact_verifier_report(dict(call.payload), "correct")

            runtime = FrantaRuntime.initialize(
                load_manifest(manifest_path), executor=executor
            )
            try:
                runtime.start_services()
                runtime.scheduler.commit_initial_trim({"category_ids": []})
                bad_task_id, healthy_task_id = runtime.scheduler.submit_batch(
                    "B-TASK-LOCAL-VERIFIER-PAIR",
                    [_associate_report(404), _associate_report(405)],
                )
                for task_id, operation_id in (
                    (bad_task_id, bad_operation_id),
                    (healthy_task_id, healthy_operation_id),
                ):
                    attempt = runtime.scheduler.start_task_attempt(task_id)
                    runtime.scheduler.ingest_progress(
                        _progress(
                            task_id,
                            attempt,
                            progress_id=f"PRG-{operation_id}",
                            operations=[
                                self._fact_candidate(
                                    version=2, operation_id=operation_id
                                )
                            ],
                            outcome="finished",
                            completion_evidence_ids=[operation_id],
                        )
                    )
                    runtime.scheduler.apply_synthesizer_result(
                        operation_id,
                        {
                            "resolution": "new",
                            "operation_digest": runtime.scheduler.state[
                                "operations"
                            ][operation_id]["input_digest"],
                            "relied_on": [],
                        },
                    )

                self.assertTrue(runtime._advance_operations())
                state = runtime.scheduler.state
                bad_operation = state["operations"][bad_operation_id]
                bad_call_id = str(bad_operation["verifier_call_id"])
                self.assertEqual(
                    bad_operation["state"], OperationState.NEEDS_ATTENTION.value
                )
                self.assertEqual(
                    state["tasks"][bad_task_id]["state"],
                    TaskState.NEEDS_ATTENTION.value,
                )
                self.assertEqual(
                    state["calls"][bad_call_id]["status"],
                    CallState.NEEDS_ATTENTION.value,
                )
                attention = next(
                    item
                    for item in state["needs_attention"]
                    if item["attention_id"] == f"call:{bad_call_id}"
                    and item.get("resolved_at") is None
                )
                self.assertEqual(attention["scope"], "task")
                self.assertEqual(attention["owner_id"], bad_task_id)
                self.assertFalse(runtime.scheduler.has_blocking_attention())

                self.assertEqual(
                    state["operations"][healthy_operation_id]["state"],
                    OperationState.COMMITTED.value,
                )
                self.assertEqual(
                    state["tasks"][healthy_task_id]["state"],
                    TaskState.CLOSED.value,
                )
                self.assertEqual(
                    verifier_invocations,
                    [bad_operation_id, bad_operation_id, healthy_operation_id],
                )

                # Re-entering the operation pump must leave the contained call
                # alone instead of attempting an invalid relaunch.
                self.assertFalse(runtime._advance_operations())
                self.assertEqual(
                    verifier_invocations,
                    [bad_operation_id, bad_operation_id, healthy_operation_id],
                )
            finally:
                runtime.close()

    def test_exhausted_synthesizer_missing_relied_on_is_task_local_and_not_relaunched(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            manifest_path = _manifest(root, name="task-local-synthesizer-exhaustion")
            manifest_path.write_text(
                manifest_path.read_text(encoding="utf-8")
                + "\n[retries]\nsynthesizer_transport = 1\n",
                encoding="utf-8",
            )
            operation_id = "OP-BAD-SYNTHESIZER-RELIED-ON"
            missing_id = "P-FACT-RAMPAZZO-MOTIVE-0001"
            invocations: list[str] = []

            def executor(call: AgentCall) -> dict[str, Any]:
                self.assertEqual(call.kind, "synthesizer")
                invocations.append(call.call_id)
                return {
                    "resolution": "new",
                    "operation_digest": call.payload["operation_digest"],
                    "explanation": "The proposed fact is distinct.",
                    "canonical_id": None,
                    "relied_on": [{"id": missing_id, "revision": None}],
                    "patch": None,
                }

            runtime = FrantaRuntime.initialize(
                load_manifest(manifest_path), executor=executor
            )
            try:
                runtime.start_services()
                runtime.scheduler.commit_initial_trim({"category_ids": []})
                task_id = runtime.scheduler.submit_batch(
                    "B-TASK-LOCAL-SYNTHESIZER", [_associate_report(406)]
                )[0]
                attempt = runtime.scheduler.start_task_attempt(task_id)
                runtime.scheduler.ingest_progress(
                    _progress(
                        task_id,
                        attempt,
                        progress_id="PRG-BAD-SYNTHESIZER-RELIED-ON",
                        operations=[
                            self._fact_candidate(
                                version=2, operation_id=operation_id
                            )
                        ],
                        outcome="finished",
                        completion_evidence_ids=[operation_id],
                    )
                )

                self.assertTrue(runtime._advance_operations())
                state = runtime.scheduler.state
                operation = state["operations"][operation_id]
                call_id = str(operation["synthesizer_call_id"])
                self.assertEqual(
                    operation["state"], OperationState.NEEDS_ATTENTION.value
                )
                self.assertEqual(
                    state["tasks"][task_id]["state"], TaskState.NEEDS_ATTENTION.value
                )
                self.assertEqual(
                    state["calls"][call_id]["status"], CallState.NEEDS_ATTENTION.value
                )
                self.assertEqual(
                    [
                        item["reason"]
                        for item in state["calls"][call_id]["invalid_results"]
                    ],
                    [
                        f"synthesizer relied on missing memory {missing_id}",
                        f"synthesizer relied on missing memory {missing_id}",
                    ],
                )
                attention = next(
                    item
                    for item in state["needs_attention"]
                    if item["attention_id"] == f"call:{call_id}"
                    and item.get("resolved_at") is None
                )
                self.assertEqual(attention["reason"], "invalid_output_retry_exhausted")
                self.assertEqual(attention["scope"], "task")
                self.assertEqual(attention["owner_id"], task_id)
                self.assertFalse(state["halt_requested"])
                self.assertFalse(runtime.scheduler.has_blocking_attention())
                self.assertEqual(invocations, [call_id, call_id])

                self.assertFalse(runtime._advance_operations())
                self.assertEqual(invocations, [call_id, call_id])
            finally:
                runtime.close()

    def test_exhausted_closure_review_is_task_local_and_not_relaunched(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            manifest_path = _manifest(root, name="task-local-closure-review")
            manifest_path.write_text(
                manifest_path.read_text(encoding="utf-8")
                + "\n[retries]\nmain_transport = 1\n",
                encoding="utf-8",
            )
            invocations: list[str] = []

            def executor(call: AgentCall) -> dict[str, Any]:
                self.assertEqual(call.kind, "main-closure-review")
                invocations.append(call.call_id)
                # The outcome passes shallow validation, while the scalar
                # summary fails the scheduler's exact closure-review contract.
                return {"outcome": "progress", "summary": "not structured"}

            runtime = FrantaRuntime.initialize(
                load_manifest(manifest_path), executor=executor
            )
            try:
                runtime.start_services()
                runtime.scheduler.commit_initial_trim({"category_ids": []})
                task_id = runtime.scheduler.submit_batch(
                    "B-TASK-LOCAL-CLOSURE", [_associate_report(406)]
                )[0]
                attempt = runtime.scheduler.start_task_attempt(task_id)
                operation_id = "OP-AMBIGUOUS-CLOSURE-EVIDENCE"
                runtime.scheduler.ingest_progress(
                    _progress(
                        task_id,
                        attempt,
                        progress_id="PRG-AMBIGUOUS-CLOSURE-EVIDENCE",
                        operations=[
                            {
                                "operation_id": operation_id,
                                "kind": "route_add",
                                "proposal_id": "TMP-AMBIGUOUS-CLOSURE-EVIDENCE",
                                "abstract": "An abandoned completion route needs review.",
                            }
                        ],
                        outcome="finished",
                        completion_evidence_ids=[operation_id],
                    )
                )
                runtime.scheduler.abandon_operation(
                    operation_id,
                    authorized_by="operator",
                    reason="Exercise the explicit closure-review boundary.",
                )

                self.assertTrue(runtime._advance_operations())
                state = runtime.scheduler.state
                task = state["tasks"][task_id]
                call_id = str(task["closure_review_call_id"])
                self.assertEqual(
                    task["state"], TaskState.NEEDS_ATTENTION.value
                )
                self.assertEqual(
                    state["calls"][call_id]["status"],
                    CallState.NEEDS_ATTENTION.value,
                )
                attention = next(
                    item
                    for item in state["needs_attention"]
                    if item["attention_id"] == f"call:{call_id}"
                    and item.get("resolved_at") is None
                )
                self.assertEqual(attention["reason"], "invalid_output_retry_exhausted")
                self.assertEqual(attention["scope"], "task")
                self.assertEqual(attention["owner_id"], task_id)
                self.assertFalse(state["halt_requested"])
                self.assertFalse(runtime.scheduler.has_blocking_attention())
                self.assertEqual(invocations, [call_id, call_id])

                # The durable task-local attention is quiescent on later
                # cycles; it must not be launched through an invalid state.
                self.assertFalse(runtime._advance_operations())
                self.assertEqual(invocations, [call_id, call_id])
            finally:
                runtime.close()

    def test_legacy_closure_halt_is_rederived_without_masking_main_attention(
        self,
    ) -> None:
        store = FakeControlStore()
        scheduler = Scheduler(store, retry_policy=RetryPolicy(main=1))
        scheduler.bootstrap(root_problem="Prove the test property.")
        scheduler.commit_initial_trim({"category_ids": []})
        task_id = scheduler.submit_batch(
            "B-LEGACY-CLOSURE-HALT", [_associate_report(407)]
        )[0]
        attempt = scheduler.start_task_attempt(task_id)
        operation_id = "OP-LEGACY-CLOSURE-HALT"
        scheduler.ingest_progress(
            _progress(
                task_id,
                attempt,
                progress_id="PRG-LEGACY-CLOSURE-HALT",
                operations=[
                    {
                        "operation_id": operation_id,
                        "kind": "route_add",
                        "proposal_id": "TMP-LEGACY-CLOSURE-HALT",
                        "abstract": "An abandoned completion route needs review.",
                    }
                ],
                outcome="finished",
                completion_evidence_ids=[operation_id],
            )
        )
        scheduler.abandon_operation(
            operation_id,
            authorized_by="operator",
            reason="Construct a closure-review compatibility fixture.",
        )
        closure_call_id = scheduler.prepare_main_closure_review_call(task_id)

        def exhaust_closure_review() -> None:
            epoch, _ = scheduler.mark_call_running(closure_call_id)
            scheduler.accept_call_result(
                closure_call_id,
                epoch,
                {"outcome": "progress", "summary": "not structured"},
            )
            with self.assertRaisesRegex(SchedulerError, "structured summary"):
                scheduler.commit_main_closure_review_call(closure_call_id)
            scheduler.reject_call_result(
                closure_call_id, "closure review requires a structured summary"
            )

        exhaust_closure_review()
        exhaust_closure_review()
        self.assertEqual(
            scheduler.state["calls"][closure_call_id]["status"],
            CallState.NEEDS_ATTENTION.value,
        )

        # Older builds persisted this global bit for a task-owned closure
        # failure. Reload must derive it from live project control failures.
        with scheduler._mutate() as state:
            state["halt_requested"] = True
        reloaded = Scheduler(store)
        self.assertFalse(reloaded.state["halt_requested"])

        # Operator authorization must also clear a stale persisted bit when
        # no main/trimmer attention remains.
        with reloaded._mutate() as state:
            state["halt_requested"] = True
        reloaded.retry_attention_call(closure_call_id)
        self.assertFalse(reloaded.state["halt_requested"])

        # Recreate closure attention, then add a real project-owned main
        # failure. Authorizing only the closure call must preserve the halt.
        scheduler = reloaded
        exhaust_closure_review()
        main_call_id = scheduler.prepare_call("main", {"probe": "project halt"})
        for expected_retry in (True, False):
            epoch, _ = scheduler.mark_call_running(main_call_id)
            self.assertEqual(
                scheduler.mark_call_failed(
                    main_call_id, epoch, "main control transport unavailable"
                ),
                expected_retry,
            )
        self.assertTrue(scheduler.state["halt_requested"])
        scheduler.retry_attention_call(closure_call_id)
        self.assertTrue(scheduler.state["halt_requested"])
        self.assertEqual(
            scheduler.state["calls"][main_call_id]["status"],
            CallState.NEEDS_ATTENTION.value,
        )

    def test_authorized_verifier_retry_rejects_abandoned_operation(self) -> None:
        store = FakeControlStore()
        scheduler = Scheduler(store, retry_policy=RetryPolicy(verifier=1))
        scheduler.bootstrap(root_problem="Prove the test property.")
        scheduler.commit_initial_trim({"category_ids": []})
        task_id = scheduler.submit_batch(
            "B-STALE-VERIFIER-RETRY", [_associate_report(402)]
        )[0]
        attempt = scheduler.start_task_attempt(task_id)
        operation_id = "OP-STALE-VERIFIER-RETRY"
        scheduler.ingest_progress(
            _progress(
                task_id,
                attempt,
                progress_id="PRG-STALE-VERIFIER-RETRY",
                operations=[self._fact_candidate(version=1, operation_id=operation_id)],
                outcome="finished",
                completion_evidence_ids=[operation_id],
            )
        )
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
        for _ in range(2):
            epoch, _ = scheduler.mark_call_running(call_id)
            scheduler.accept_call_result(call_id, epoch, {"verdict": "malformed"})
            scheduler.reject_call_result(call_id, "invalid verifier report")
        scheduler.abandon_operation(
            operation_id,
            authorized_by="operator",
            reason="The operator withdraws this nonessential proposal.",
        )
        with self.assertRaisesRegex(
            WorkflowError, "call is not in needs_attention"
        ):
            scheduler.retry_attention_call(call_id)

    def test_authorized_verifier_retry_can_return_incorrect(self) -> None:
        store = FakeControlStore()
        scheduler = Scheduler(store, retry_policy=RetryPolicy(verifier=1))
        scheduler.bootstrap(root_problem="Prove the test property.")
        scheduler.commit_initial_trim({"category_ids": []})
        task_id = scheduler.submit_batch(
            "B-AUTHORIZED-INCORRECT-RETRY", [_associate_report(403)]
        )[0]
        attempt = scheduler.start_task_attempt(task_id)
        operation_id = "OP-AUTHORIZED-INCORRECT-RETRY"
        scheduler.ingest_progress(
            _progress(
                task_id,
                attempt,
                progress_id="PRG-AUTHORIZED-INCORRECT-RETRY",
                operations=[self._fact_candidate(version=1, operation_id=operation_id)],
                outcome="finished",
                completion_evidence_ids=[operation_id],
            )
        )
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
        exact_input = scheduler.state["calls"][call_id]["input"]
        for _ in range(2):
            epoch, _ = scheduler.mark_call_running(call_id)
            scheduler.accept_call_result(call_id, epoch, {"verdict": "malformed"})
            scheduler.reject_call_result(call_id, "invalid verifier report")

        scheduler.retry_attention_call(call_id)
        epoch, _ = scheduler.mark_call_running(call_id)
        report = _exact_verifier_report(
            exact_input,
            "incorrect",
            errors=[
                {
                    "location": "proof",
                    "message": "The boundary case is not justified.",
                }
            ],
        )
        scheduler.accept_call_result(call_id, epoch, report)
        self.assertIsNone(scheduler.commit_verifier_call(call_id))

        final = scheduler.state
        self.assertEqual(
            final["operations"][operation_id]["state"],
            OperationState.REJECTED.value,
        )
        self.assertEqual(
            final["tasks"][task_id]["state"], TaskState.REVISION_PENDING.value
        )
        self.assertEqual(
            final["tasks"][task_id]["pending_attempt_supplement"]["kind"],
            "verifier_revision",
        )
        self.assertEqual(
            final["calls"][call_id]["status"], CallState.COMMITTED.value
        )

    def test_verifier_input_contains_complete_predecessors_and_prior_report(self) -> None:
        store, scheduler = self._scheduler()
        store.records["F-PREDECESSOR-FULL"] = {
            "id": "F-PREDECESSOR-FULL",
            "type": "fact",
            "status": "active",
            "active": True,
            "statement": "The boundary clause applies.",
            "proof": "Proof of the complete predecessor.",
            "predecessor_fact_ids": [],
            "originating_task_id": "T-OLD",
            "foundation_policy_version": 1,
            "introduced_notation": [],
            "external_references": [],
            "root_resolution": None,
            "abstract": "A predecessor with its complete proof.",
        }
        task_id = scheduler.submit_batch("B-VERIFIER-INPUT", [_associate_report(41)])[0]
        first_attempt = scheduler.start_task_attempt(task_id)
        first = self._fact_candidate(version=1, operation_id="OP-INPUT-V1")
        first["predecessor_fact_ids"] = ["F-PREDECESSOR-FULL"]
        first["proof"] = "Use F-PREDECESSOR-FULL, but omit the final case."
        scheduler.ingest_progress(
            _progress(
                task_id,
                first_attempt,
                progress_id="PRG-INPUT-V1",
                operations=[first],
                outcome="finished",
                completion_evidence_ids=["OP-INPUT-V1"],
            )
        )
        scheduler.apply_synthesizer_result(
            "OP-INPUT-V1",
            {
                "resolution": "new",
                "operation_digest": scheduler.state["operations"]["OP-INPUT-V1"][
                    "input_digest"
                ],
                "relied_on": [],
            },
        )
        first_call = scheduler.prepare_verifier_call("OP-INPUT-V1")
        first_input = scheduler.state["calls"][first_call]["input"]
        self.assertEqual(
            first_input["predecessor_records"][0]["proof"],
            "Proof of the complete predecessor.",
        )
        self.assertIsNone(first_input["prior_verification_report"])
        first_report = _exact_verifier_report(
            first_input,
            "incorrect",
            errors=[{"location": "proof", "message": "The final case is absent."}],
        )
        scheduler.apply_verifier_report("OP-INPUT-V1", first_report)

        second_attempt = scheduler.start_task_attempt(task_id)
        second = self._fact_candidate(version=2, operation_id="OP-INPUT-V2")
        second["predecessor_fact_ids"] = ["F-PREDECESSOR-FULL"]
        second["proof"] = "Use F-PREDECESSOR-FULL and handle the final case separately."
        scheduler.ingest_progress(
            _progress(
                task_id,
                second_attempt,
                progress_id="PRG-INPUT-V2",
                operations=[second],
                outcome="finished",
                completion_evidence_ids=["OP-INPUT-V2"],
            )
        )
        scheduler.apply_synthesizer_result(
            "OP-INPUT-V2",
            {
                "resolution": "new",
                "operation_digest": scheduler.state["operations"]["OP-INPUT-V2"][
                    "input_digest"
                ],
                "relied_on": [],
            },
        )
        second_call = scheduler.prepare_verifier_call("OP-INPUT-V2")
        second_input = scheduler.state["calls"][second_call]["input"]
        self.assertEqual(second_input["prior_verification_report"], first_report)

    def test_incorrect_verdict_after_disconnect_launches_revision_before_main(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            initial = FrantaRuntime.initialize(
                load_manifest(_manifest(root, name="disconnect-verifier-revision"))
            )
            try:
                scheduler = initial.scheduler
                scheduler.commit_initial_trim({"category_ids": []})
                task_id = scheduler.submit_batch(
                    "B-DISCONNECT-VERIFIER-REVISION", [_associate_report(49)]
                )[0]
                attempt = scheduler.start_task_attempt(task_id)
                operation_id = "OP-DISCONNECT-VERIFIER-REVISION"
                scheduler.ingest_progress(
                    _progress(
                        task_id,
                        attempt,
                        progress_id="PRG-DISCONNECT-VERIFIER-REVISION",
                        final=False,
                        operations=[
                            self._fact_candidate(
                                version=1, operation_id=operation_id
                            )
                        ],
                    )
                )
                first_worker_call = scheduler.state["tasks"][task_id]["attempts"][
                    0
                ]["call_id"]
                scheduler.record_worker_interruption(
                    task_id, "worker disconnected after staging the fact"
                )
                interrupted = scheduler.state["tasks"][task_id]
                self.assertEqual(interrupted["state"], TaskState.RETRY_PENDING.value)
                self.assertEqual(interrupted["interruption_retry_count"], 1)
                self.assertEqual(interrupted["attempts"][0]["state"], "interrupted")
                self.assertEqual(
                    scheduler.state["calls"][first_worker_call]["status"],
                    CallState.SUPERSEDED.value,
                )

                operation_digest = scheduler.state["operations"][operation_id][
                    "input_digest"
                ]
                scheduler.apply_synthesizer_result(
                    operation_id,
                    {
                        "resolution": "new",
                        "operation_digest": operation_digest,
                        "relied_on": [],
                    },
                )
                verifier_call = scheduler.prepare_verifier_call(operation_id)
                exact_input = scheduler.state["calls"][verifier_call]["input"]
                report = _exact_verifier_report(
                    exact_input,
                    "incorrect",
                    errors=[
                        {
                            "location": "proof",
                            "message": "The boundary case is not justified.",
                        }
                    ],
                )
                lease_epoch, _ = scheduler.mark_call_running(verifier_call)
                scheduler.accept_call_result(verifier_call, lease_epoch, report)
                self.assertIsNone(scheduler.commit_verifier_call(verifier_call))

                revised = scheduler.state["tasks"][task_id]
                expected_supplement = revised["pending_attempt_supplement"]
                self.assertEqual(revised["state"], TaskState.REVISION_PENDING.value)
                self.assertEqual(revised["interruption_retry_count"], 1)
                self.assertEqual(revised["attempts"][0]["state"], "interrupted")
                self.assertEqual(
                    revised["attempts"][0]["ended_reason"],
                    "worker disconnected after staging the fact",
                )
                self.assertEqual(
                    revised["pending_attempt_supplement"], expected_supplement
                )
                self.assertEqual(
                    scheduler.state["calls"][verifier_call]["status"],
                    CallState.COMMITTED.value,
                )
                incorrect_events = sum(
                    event["type"] == "fact_verified_incorrect"
                    for event in scheduler.state["events"]
                )
                self.assertIsNone(scheduler.commit_verifier_call(verifier_call))
                self.assertEqual(
                    sum(
                        event["type"] == "fact_verified_incorrect"
                        for event in scheduler.state["events"]
                    ),
                    incorrect_events,
                )
                project_dir = initial.layout.root
            finally:
                initial.close()

            observed_calls: list[AgentCall] = []

            def executor(call: AgentCall) -> dict[str, Any]:
                observed_calls.append(call)
                if call.kind != "worker":
                    raise AssertionError(
                        f"revision worker must launch before a new {call.kind} call"
                    )
                self.assertEqual(call.payload["attempt"], 2)
                self.assertEqual(call.payload["supplement"], expected_supplement)
                raise ConnectionError("stop after observing the revision launch")

            resumed = FrantaRuntime.open(project_dir, executor=executor)
            try:
                before_run = resumed.scheduler.state["tasks"][task_id]
                self.assertEqual(
                    before_run["state"], TaskState.REVISION_PENDING.value
                )
                self.assertEqual(before_run["interruption_retry_count"], 1)
                self.assertEqual(len(before_run["attempts"]), 1)
                pending = resumed.scheduler.pending_recovery_work()
                self.assertIn(task_id, pending["tasks"])
                self.assertNotIn(verifier_call, pending["calls"])

                resumed.run(resume=True, max_cycles=1)
                self.assertEqual(
                    [call.kind for call in observed_calls], ["worker"]
                )
                launched = resumed.scheduler.state["tasks"][task_id]
                self.assertEqual(len(launched["attempts"]), 2)
                self.assertEqual(
                    launched["attempts"][1]["supplement"], expected_supplement
                )
                self.assertEqual(
                    sum(
                        call["kind"] == "verifier"
                        for call in resumed.scheduler.state["calls"].values()
                    ),
                    1,
                )
            finally:
                resumed.close()

    def test_expected_revision_gap_verifier_feedback_survives_worker_disconnect(self) -> None:
        _, scheduler = self._scheduler()
        task_id = scheduler.submit_batch("B-REVISION-RETRY", [_associate_report(50)])[0]
        report = self._submit_rejected_fact_version(
            scheduler, task_id, version=1, operation_id="OP-REVISION-V1"
        )
        original = scheduler.state["tasks"][task_id]["pending_attempt_supplement"]
        self.assertEqual(original["kind"], "verifier_revision")

        scheduler.start_task_attempt(task_id)
        scheduler.record_worker_interruption(
            task_id, "worker disconnected while repairing the verifier-reported gap"
        )
        scheduler.start_task_attempt(task_id)
        retry_supplement = scheduler.state["tasks"][task_id]["attempts"][-1][
            "supplement"
        ]

        self.assertTrue(_contains_nested(retry_supplement, "verifier_revision"))
        self.assertTrue(_contains_nested(retry_supplement, report))
        self.assertTrue(
            _contains_nested(
                retry_supplement,
                scheduler.state["operations"]["OP-REVISION-V1"]["candidate_id"],
            )
        )

    def test_expected_concession_gap_survives_worker_disconnect(self) -> None:
        _, scheduler = self._scheduler()
        task_id = scheduler.submit_batch("B-CONCESSION-RETRY", [_associate_report(60)])[0]
        latest_report: dict[str, Any] | None = None
        for version in (1, 2, 3):
            latest_report = self._submit_rejected_fact_version(
                scheduler,
                task_id,
                version=version,
                operation_id=f"OP-CONCESSION-V{version}",
            )
        original = scheduler.state["tasks"][task_id]["pending_attempt_supplement"]
        self.assertEqual(original["kind"], "fact_concession")
        self.assertTrue(original["forbid_new_candidate"])
        self.assertTrue(original["require_failure_memo"])

        scheduler.start_task_attempt(task_id)
        scheduler.record_worker_interruption(
            task_id, "worker disconnected before writing the mandatory concession memo"
        )
        scheduler.start_task_attempt(task_id)
        retry_supplement = scheduler.state["tasks"][task_id]["attempts"][-1][
            "supplement"
        ]

        self.assertTrue(_contains_nested(retry_supplement, "fact_concession"))
        self.assertTrue(_contains_nested(retry_supplement, latest_report))
        self.assertTrue(_contains_nested(retry_supplement, True))

    def test_expected_identical_bundle_gap_returns_durable_correction(self) -> None:
        _, scheduler = self._scheduler()
        task_id = scheduler.submit_batch("B-IDENTICAL-BUNDLE", [_associate_report(70)])[0]
        self._submit_rejected_fact_version(
            scheduler, task_id, version=1, operation_id="OP-IDENTICAL-V1"
        )
        rejected_proof = scheduler.state["operations"]["OP-IDENTICAL-V1"]["payload"][
            "proof"
        ]

        attempt = scheduler.start_task_attempt(task_id)
        resubmission = self._fact_candidate(version=2, operation_id="OP-IDENTICAL-V2")
        # Only the bookkeeping version changes.  None of the mathematical
        # fields Design.md recognizes as a corrected bundle changes.
        resubmission["proof"] = rejected_proof
        scheduler.ingest_progress(
            _progress(
                task_id,
                attempt,
                progress_id="PRG-IDENTICAL-V2",
                operations=[resubmission],
                outcome="finished",
                completion_evidence_ids=["OP-IDENTICAL-V2"],
            )
        )
        operation_digest = scheduler.state["operations"]["OP-IDENTICAL-V2"][
            "input_digest"
        ]
        scheduler.apply_synthesizer_result(
            "OP-IDENTICAL-V2",
            {
                "resolution": "new",
                "operation_digest": operation_digest,
                "relied_on": [],
            },
        )
        try:
            scheduler.verification_bundle("OP-IDENTICAL-V2")
        except SchedulerError as exc:
            self.assertIn("identical", str(exc))

        state = scheduler.state
        self.assertIn(
            state["operations"]["OP-IDENTICAL-V2"]["state"],
            {OperationState.REJECTED.value, OperationState.ABANDONED.value},
        )
        self.assertEqual(
            state["tasks"][task_id]["state"], TaskState.REVISION_PENDING.value
        )
        self.assertIn(
            "identical",
            json.dumps(
                state["tasks"][task_id].get("pending_attempt_supplement", {}),
                sort_keys=True,
            ).lower(),
        )
        self.assertEqual(
            state["fact_lineages"][resubmission["candidate_id"]]["revision_requests"],
            2,
        )

    def test_identical_bundle_after_disconnect_preserves_retry_history_on_restart(
        self,
    ) -> None:
        store, scheduler = self._scheduler()
        task_id = scheduler.submit_batch(
            "B-IDENTICAL-BUNDLE-DISCONNECT", [_associate_report(71)]
        )[0]
        first_attempt = scheduler.start_task_attempt(task_id)
        first_operation_id = "OP-IDENTICAL-DISCONNECT-V1"
        first_candidate = self._fact_candidate(
            version=1, operation_id=first_operation_id
        )
        scheduler.ingest_progress(
            _progress(
                task_id,
                first_attempt,
                progress_id="PRG-IDENTICAL-DISCONNECT-V1",
                operations=[first_candidate],
                outcome="finished",
                completion_evidence_ids=[first_operation_id],
            )
        )
        scheduler.apply_synthesizer_result(
            first_operation_id,
            {
                "resolution": "new",
                "operation_digest": scheduler.state["operations"][first_operation_id][
                    "input_digest"
                ],
                "relied_on": [],
            },
        )
        first_verifier_call = scheduler.prepare_verifier_call(first_operation_id)
        first_input = scheduler.state["calls"][first_verifier_call]["input"]
        first_report = _exact_verifier_report(
            first_input,
            "incorrect",
            errors=[
                {
                    "location": "proof",
                    "message": "The boundary case is not justified.",
                }
            ],
        )
        lease_epoch, _ = scheduler.mark_call_running(first_verifier_call)
        scheduler.accept_call_result(first_verifier_call, lease_epoch, first_report)
        self.assertIsNone(scheduler.commit_verifier_call(first_verifier_call))

        second_attempt = scheduler.start_task_attempt(task_id)
        second_operation_id = "OP-IDENTICAL-DISCONNECT-V2"
        second_candidate = self._fact_candidate(
            version=2, operation_id=second_operation_id
        )
        second_candidate["proof"] = first_candidate["proof"]
        scheduler.ingest_progress(
            _progress(
                task_id,
                second_attempt,
                progress_id="PRG-IDENTICAL-DISCONNECT-V2",
                final=False,
                operations=[second_candidate],
            )
        )
        second_worker_call = scheduler.state["tasks"][task_id]["attempts"][1][
            "call_id"
        ]
        scheduler.record_worker_interruption(
            task_id, "worker disconnected after staging the unchanged revision"
        )
        interrupted = scheduler.state["tasks"][task_id]
        self.assertEqual(interrupted["state"], TaskState.RETRY_PENDING.value)
        self.assertEqual(interrupted["interruption_retry_count"], 1)
        self.assertEqual(interrupted["attempts"][1]["state"], "interrupted")

        second_result = {
            "resolution": "new",
            "operation_digest": scheduler.state["operations"][second_operation_id][
                "input_digest"
            ],
            "relied_on": [],
        }
        self.assertTrue(
            scheduler.apply_synthesizer_result(second_operation_id, second_result)
        )
        state = scheduler.state
        second_operation = state["operations"][second_operation_id]
        correction = state["tasks"][task_id]["pending_attempt_supplement"]
        self.assertEqual(second_operation["state"], OperationState.REJECTED.value)
        self.assertEqual(
            second_operation["error"], "identical_rejected_verification_bundle"
        )
        self.assertEqual(
            state["tasks"][task_id]["state"], TaskState.REVISION_PENDING.value
        )
        self.assertEqual(state["tasks"][task_id]["interruption_retry_count"], 1)
        self.assertEqual(
            state["tasks"][task_id]["attempts"][1]["state"], "interrupted"
        )
        self.assertEqual(
            state["tasks"][task_id]["attempts"][1]["ended_reason"],
            "worker disconnected after staging the unchanged revision",
        )
        self.assertEqual(correction["kind"], "identical_rejected_bundle")
        self.assertEqual(correction["candidate_id"], second_candidate["candidate_id"])
        self.assertEqual(correction["operation_id"], second_operation_id)
        self.assertEqual(
            correction["prior_rejection"]["verification_report"], first_report
        )
        self.assertEqual(
            state["fact_lineages"][second_candidate["candidate_id"]][
                "revision_requests"
            ],
            2,
        )
        self.assertEqual(
            state["calls"][second_worker_call]["status"],
            CallState.SUPERSEDED.value,
        )
        self.assertEqual(
            sum(call["kind"] == "verifier" for call in state["calls"].values()), 1
        )

        identical_events = sum(
            event["type"] == "identical_rejected_bundle_returned"
            for event in state["events"]
        )
        call_count = len(state["calls"])
        self.assertTrue(
            scheduler.apply_synthesizer_result(second_operation_id, second_result)
        )
        self.assertEqual(len(scheduler.state["calls"]), call_count)
        self.assertEqual(
            sum(
                event["type"] == "identical_rejected_bundle_returned"
                for event in scheduler.state["events"]
            ),
            identical_events,
        )
        with self.assertRaisesRegex(SchedulerError, "identical"):
            scheduler.prepare_verifier_call(second_operation_id)

        resumed = Scheduler(store)
        recovery = resumed.recover()
        self.assertNotIn(first_verifier_call, recovery.retry_call_ids)
        self.assertIn(task_id, resumed.pending_recovery_work()["tasks"])
        recovered = resumed.state["tasks"][task_id]
        self.assertEqual(recovered["state"], TaskState.REVISION_PENDING.value)
        self.assertEqual(recovered["interruption_retry_count"], 1)
        self.assertEqual(recovered["pending_attempt_supplement"], correction)
        self.assertEqual(
            [item["state"] for item in recovered["attempts"]],
            ["ended", "interrupted"],
        )

        third_attempt = resumed.start_task_attempt(task_id)
        self.assertEqual(third_attempt, 3)
        relaunched = resumed.state["tasks"][task_id]
        self.assertEqual(
            relaunched["attempts"][2]["kind"], "identical_rejected_bundle"
        )
        self.assertEqual(relaunched["attempts"][2]["supplement"], correction)
        self.assertEqual(relaunched["interruption_retry_count"], 1)
        self.assertEqual(
            sum(
                call["kind"] == "verifier"
                for call in resumed.state["calls"].values()
            ),
            1,
        )

    def test_expected_verifier_confirmation_gap_blocks_underconfirmed_publication(
        self,
    ) -> None:
        """A bundle digest alone is not a complete successful verifier report."""

        malformed_reports = {
            "missing confirmations": lambda report: {
                "verdict": "correct",
                "bundle_digest": report["bundle_digest"],
            },
            "wrong candidate identity": lambda report: {
                **report,
                "candidate_id": "FC-WRONG",
            },
            "wrong candidate version": lambda report: {
                **report,
                "candidate_version": 2,
            },
            "wrong dependency confirmation": lambda report: {
                **report,
                "predecessor_ids": [],
            },
            "missing root confirmation": lambda report: {
                **report,
                "root_resolution": None,
            },
        }
        for label, mutate in malformed_reports.items():
            with self.subTest(label=label):
                store, scheduler = self._scheduler()
                store.records["F-PREDECESSOR"] = {
                    "id": "F-PREDECESSOR",
                    "type": "fact",
                    "status": "active",
                    "active": True,
                    "statement": "The boundary clause applies to test objects.",
                    "proof": "This is a declared predecessor for the fault probe.",
                    "predecessor_fact_ids": [],
                    "external_references": [],
                    "introduced_notation": [],
                }
                task_id = scheduler.submit_batch(
                    f"B-VERIFIER-CONFIRM-{label}", [_associate_report(80)]
                )[0]
                attempt = scheduler.start_task_attempt(task_id)
                operation_id = "OP-ROOT-CONFIRM"
                candidate = self._fact_candidate(version=1, operation_id=operation_id)
                candidate["candidate_id"] = "FC-ROOT-CONFIRM"
                candidate["predecessor_fact_ids"] = ["F-PREDECESSOR"]
                candidate["proof"] = "Apply F-PREDECESSOR to prove every case."
                candidate["root_resolution"] = {
                    "target": "ROOT",
                    "outcome": "proved",
                }
                scheduler.ingest_progress(
                    _progress(
                        task_id,
                        attempt,
                        progress_id="PRG-ROOT-CONFIRM",
                        operations=[candidate],
                        outcome="finished",
                        completion_evidence_ids=[operation_id],
                    )
                )
                operation_digest = scheduler.state["operations"][operation_id][
                    "input_digest"
                ]
                scheduler.apply_synthesizer_result(
                    operation_id,
                    {
                        "resolution": "new",
                        "operation_digest": operation_digest,
                        "relied_on": [],
                    },
                )
                bundle = scheduler.verification_bundle(operation_id)
                valid_report = {
                    "verdict": "correct",
                    "candidate_id": "FC-ROOT-CONFIRM",
                    "candidate_version": 1,
                    "operation_id": operation_id,
                    "bundle_digest": bundle["bundle_digest"],
                    "verifier_attempt_id": bundle["verifier_attempt_id"],
                    "predecessor_ids": ["F-PREDECESSOR"],
                    "introduced_notation": [],
                    "external_references": [],
                    "root_resolution": {"target": "ROOT", "outcome": "proved"},
                    "errors": [],
                }
                with self.assertRaises(SchedulerError):
                    scheduler.apply_verifier_report(
                        operation_id, mutate(valid_report)
                    )
                state = scheduler.state
                self.assertEqual(
                    state["operations"][operation_id]["state"],
                    OperationState.VERIFYING.value,
                )
                self.assertIsNone(state["operations"][operation_id]["canonical_id"])
                self.assertEqual(state["root"]["obligation_status"], "active")

    def _submit_rejected_fact_version(
        self,
        scheduler: Scheduler,
        task_id: str,
        *,
        version: int,
        operation_id: str,
    ) -> dict[str, Any]:
        attempt = scheduler.start_task_attempt(task_id)
        candidate = self._fact_candidate(version=version, operation_id=operation_id)
        scheduler.ingest_progress(
            _progress(
                task_id,
                attempt,
                progress_id=f"PRG-{operation_id}",
                operations=[candidate],
                outcome="finished",
                completion_evidence_ids=[operation_id],
            )
        )
        operation_digest = scheduler.state["operations"][operation_id]["input_digest"]
        scheduler.apply_synthesizer_result(
            operation_id,
            {
                "resolution": "new",
                "operation_digest": operation_digest,
                "relied_on": [],
            },
        )
        bundle = scheduler.verification_bundle(operation_id)
        report = {
            **_exact_verifier_report(bundle, "incorrect"),
            "errors": [
                {
                    "location": "proof",
                    "message": "The boundary case is not justified.",
                }
            ],
        }
        scheduler.apply_verifier_report(operation_id, report)
        return report

    @staticmethod
    def _fact_candidate(*, version: int, operation_id: str) -> dict[str, Any]:
        return {
            "operation_id": operation_id,
            "kind": "fact",
            "proposal_id": f"TMP-REPAIRED-FACT-{operation_id}",
            "candidate_id": f"FC-REPAIR-{operation_id}",
            "candidate_version": 1,
            "statement": "Every test object has property P.",
            "proof": (
                "The defining axiom proves the generic case but omits the boundary."
                if version == 1
                else f"Revision {version}: the defining axiom proves the generic case; "
                "its boundary clause separately proves the remaining case."
            ),
            "predecessor_fact_ids": [],
            "abstract": "Test objects have property P, including the boundary case.",
            "keywords": ["test object", "property P", "boundary"],
            "introduced_notation": [],
            "external_references": [],
            "related_route_ids": [],
        }


if __name__ == "__main__":
    unittest.main()
