from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from typing import Any

from advisor_system.contracts import (
    AdvisorCycleContext,
    HumanFeedback,
    SelectionReport,
)
from franta.access import policy_for
from franta.config import load_manifest
from franta.explorer_adapter import (
    build_main_sort_explorer_snapshot_for_frozen_turn,
)
from franta.materialize import explorer_snapshot_digest
from franta.runtime import AgentCall, FrantaRuntime, RuntimeErrorBase
from franta.scheduler import IdempotencyConflict, TransportFailure
from franta.skill_runtime import SkillContext, SkillRuntime
from franta.workflows import WorkflowError


ROOT = "Prove the immutable original ROOT statement."
CUSTOM_SUBPROBLEM = "Construct the exact human-requested obstruction example."


def _write_manifest(root: Path, *, advisor: bool) -> Path:
    advisor_table = "\n[advisor]\n" if advisor else ""
    manifest = root / "bootstrap.toml"
    manifest.write_text(
        (
            "[project]\n"
            'name = "advisor-runtime-integration"\n'
            'directory = "project"\n'
            f'root_problem = "{ROOT}"\n'
            'foundation_policy = "Use only the declared test axioms."\n'
            "\n[explorer]\n"
            "max_workers = 1\n"
            "attempts_per_worker = 3\n"
            "attempt_seconds = 30\n"
            "explorer_admission_seconds = 1\n"
            "franta_admission_seconds = 30\n"
            f"{advisor_table}"
            "\n[initial]\n"
        ),
        encoding="utf-8",
    )
    return manifest


def _enter_franta_drain(
    runtime: FrantaRuntime, *, marker: str, stop_in_run: bool = False
) -> None:
    """Use only published scheduler transitions to create an empty Franta turn."""

    runtime.scheduler.activate_alternation()
    phase = runtime.scheduler.state["phase_control"]
    explorer_deadline = datetime.fromisoformat(
        phase["explorer"]["admission_deadline"]
    )
    runtime.scheduler.tick_alternation(now=explorer_deadline)
    repository = runtime.explorer_repository
    assert repository is not None
    turn_id = f"XTURN-{int(phase['cycle']):08d}"
    frozen = repository.freeze_turn(turn_id)
    sort_run_id = f"SORT-{marker}"
    snapshot = build_main_sort_explorer_snapshot_for_frozen_turn(
        repository,
        sort_run_id=sort_run_id,
        frozen_turn=frozen,
    )
    task_id = runtime.scheduler.prepare_main_sort_task(
        sort_run_id=sort_run_id,
        turn_id=turn_id,
        source_high_water_seq=frozen.high_water_seq,
        source_set_digest=frozen.source_set_digest,
        snapshot_format_version=1,
        snapshot_digest=explorer_snapshot_digest(snapshot),
    )
    sort_call_id = runtime.scheduler.begin_main_sort_task_attempt(
        task_id,
        sort_run_id=sort_run_id,
        session_key=f"main:{marker}",
        now=explorer_deadline,
    )
    runtime.scheduler.open_franta_run(
        sort_call_id=sort_call_id,
        planning_call_id=f"CALL-PLANNING-{marker}",
        session_key=f"main:{marker}",
        now=explorer_deadline,
    )
    if stop_in_run:
        assert runtime.scheduler.alternation_phase == "franta_run"
        return
    franta_deadline = datetime.fromisoformat(
        runtime.scheduler.state["phase_control"]["franta"]["admission_deadline"]
    )
    runtime.scheduler.tick_alternation(now=franta_deadline)
    assert runtime.scheduler.alternation_phase == "franta_drain"


class _AdvisorExecutor:
    """Produce valid Advisor outputs through the same staged skill boundary."""

    def __init__(self) -> None:
        self.calls: list[AgentCall] = []

    @staticmethod
    def _report(call: AgentCall) -> dict[str, Any]:
        context = AdvisorCycleContext.from_dict(call.payload)
        index = context.advisor_index
        report = {
            "selection_report_id": f"ADVISOR-REPORT-{index}",
            "feedback_request_id": f"ADVISOR-FEEDBACK-REQUEST-{index}",
            "advisor_index": index,
            "source_cycle": context.source_cycle,
            "target_cycle": context.target_cycle,
            "original_problem_digest": context.original_problem_digest,
            "memory_snapshot_id": context.memory_snapshot.snapshot_id,
            "memory_snapshot_digest": context.memory_snapshot.snapshot_digest,
            "previous_assignments_digest": context.previous_assignments_digest,
            "obligations": [
                {
                    "obligation_id": f"ADV-{index}-{rank}",
                    "rank": rank,
                    "title": f"Decisive direction {rank}",
                    "statement": (
                        f"Prove the cycle {index} decisive reduction number {rank}."
                    ),
                    "importance": (
                        f"Reduction {rank} isolates a central obstruction."
                    ),
                    "landscape_change": (
                        f"Resolving reduction {rank} removes a major branch."
                    ),
                    "relationship_to_root": (
                        f"Reduction {rank} supplies a missing ROOT implication."
                    ),
                    "novelty": (
                        f"Reduction {rank} is distinct from earlier assignments."
                    ),
                    "previous_assignment_ids": [],
                    "repeat_justification": None,
                    "breakthrough_evidence_ids": [],
                }
                for rank in range(1, 6)
            ],
            "human_question": "Choose one or two obligations for the next cycle.",
            "report_path": f"artifacts/advisor-{index}-selection.md",
        }
        staged = SkillRuntime(SkillContext.load(call.workspace.path)).invoke(
            "selection-report", report
        )
        return {
            "stage": "proposal",
            "call_ended": True,
            "selection_report_id": staged["selection_report_id"],
            "selection_report_digest": staged["selection_report_digest"],
            "feedback_request_id": staged["feedback_request_id"],
            "report_path": staged["report_path"],
        }

    @staticmethod
    def _finalization(call: AgentCall) -> dict[str, Any]:
        report = SelectionReport.from_dict(call.payload["selection_report"])
        feedback = HumanFeedback.from_dict(call.payload["human_feedback"])
        by_id = {item.obligation_id: item for item in report.obligations}
        selected = []
        for choice_index, choice in enumerate(feedback.choices, start=1):
            if choice.kind == "listed":
                obligation = by_id[str(choice.obligation_id)]
                selected.append(
                    {
                        "source": "listed",
                        "obligation_id": obligation.obligation_id,
                        "statement": obligation.statement,
                        "human_choice_index": choice_index,
                    }
                )
            else:
                selected.append(
                    {
                        "source": "human_override",
                        "obligation_id": None,
                        "statement": choice.statement,
                        "human_choice_index": choice_index,
                    }
                )
        return {
            "stage": "finalize",
            "call_ended": True,
            "selection_report_id": report.selection_report_id,
            "selection_report_digest": report.digest,
            "feedback_id": feedback.feedback_id,
            "feedback_digest": feedback.digest,
            "selected_subproblems": selected,
        }

    def __call__(self, call: AgentCall) -> dict[str, Any]:
        self.calls.append(call)
        if call.kind == "advisor-proposal":
            return self._report(call)
        if call.kind == "advisor-finalize":
            return self._finalization(call)
        raise AssertionError(f"unexpected deterministic call {call.kind}")


class _RetryingAdvisorExecutor(_AdvisorExecutor):
    """Return one semantically invalid proposal before a valid retry."""

    def __init__(self) -> None:
        super().__init__()
        self.workspace_inodes: list[int] = []
        self.marker_visible: list[bool] = []

    def __call__(self, call: AgentCall) -> dict[str, Any]:
        if call.kind != "advisor-proposal":
            return super().__call__(call)
        self.calls.append(call)
        marker = call.workspace.artifacts_path / "invalid-first-generation.txt"
        self.workspace_inodes.append(call.workspace.path.stat().st_ino)
        self.marker_visible.append(marker.exists())
        if len(self.workspace_inodes) == 1:
            marker.write_text("must be retired\n", encoding="utf-8")
            return {
                "stage": "proposal",
                "call_ended": True,
                "selection_report_id": "MISSING-REPORT",
                "selection_report_digest": "0" * 64,
                "feedback_request_id": "MISSING-FEEDBACK-REQUEST",
                "report_path": "artifacts/missing-report.md",
            }
        return self._report(call)


class _ExhaustThenRecoverAdvisorExecutor(_AdvisorExecutor):
    """Stage a report on every rejected generation until operator retry."""

    def __init__(self) -> None:
        super().__init__()
        self.allow_success = False
        self.workspace_inodes: list[int] = []

    def __call__(self, call: AgentCall) -> dict[str, Any]:
        if call.kind != "advisor-proposal":
            return super().__call__(call)
        self.calls.append(call)
        self.workspace_inodes.append(call.workspace.path.stat().st_ino)
        response = self._report(call)
        if not self.allow_success:
            response["selection_report_digest"] = "0" * 64
        return response


def _ordinary_assignment(marker: str) -> dict[str, Any]:
    return {
        "report_id": f"AR-{marker}",
        "objective": f"Investigate the assigned problem for {marker}.",
        "if_resume": None,
        "mode": "associate",
        "main_route_ids": [],
        "main_obligation_ids": [],
        "perspective": None,
        "portfolio": {
            kind: []
            for kind in (
                "fact",
                "route",
                "memo",
                "claim",
                "obligation",
                "computation",
            )
        },
        "reason": "Exercise downstream Advisor problem binding.",
    }


def _attempt_summary(*, proposed_outcome: str, evidence: list[str]) -> dict[str, Any]:
    return {
        "work_mode": "associate",
        "task": "Investigate the assigned problem.",
        "proposed_outcome": proposed_outcome,
        "cumulative_important_progress": "Recorded deterministic test progress.",
        "completion_evidence_operation_ids": list(evidence),
        "most_promising_next_steps": "Review the staged mathematical object.",
    }


def _fact_operation(operation_id: str) -> dict[str, Any]:
    return {
        "operation_id": operation_id,
        "kind": "fact",
        "proposal_id": f"TMP-{operation_id}",
        "candidate_id": f"FC-{operation_id}",
        "candidate_version": 1,
        "statement": "Every deterministic test object has property P.",
        "proof": "This follows directly from the declared test axiom.",
        "predecessor_fact_ids": [],
        "abstract": "The test axiom gives property P.",
        "keywords": ["test object", "property P"],
        "introduced_notation": [],
        "external_references": [],
        "related_route_ids": [],
    }


def _exact_verifier_report(bundle: dict[str, Any]) -> dict[str, Any]:
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


class AdvisorRuntimeIntegrationTests(unittest.TestCase):
    def test_advisor_topology_rejects_a_snapshotless_main_call(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            runtime = FrantaRuntime.initialize(
                load_manifest(_write_manifest(Path(raw), advisor=True))
            )
            try:
                context = {"event_cursor": 0, "portfolio_revision": 0}
                call_id = runtime.scheduler.prepare_call(
                    "main",
                    context,
                    continuation={
                        "reserved_batch_id": "BATCH-SNAPSHOTLESS-ADVISOR",
                        "session_key": "main:project",
                    },
                )
                with self.assertRaisesRegex(
                    RuntimeErrorBase,
                    "Main call lacks its exact complete-memory snapshot identity",
                ):
                    runtime._make_workspace(
                        call_id=call_id,
                        policy=policy_for("main"),
                        context=context,
                    )
            finally:
                runtime.close()

    def test_disabled_advisor_preserves_the_existing_drain_path(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            runtime = FrantaRuntime.initialize(
                load_manifest(_write_manifest(Path(raw), advisor=False))
            )
            try:
                runtime.scheduler.commit_initial_trim({"category_ids": []})
                self.assertIsNone(runtime.advisor_program)
                self.assertNotIn("advisor", runtime.status())
                _enter_franta_drain(runtime, marker="LEGACY")

                runtime.run(max_cycles=1)

                self.assertEqual(runtime.scheduler.alternation_phase, "explorer_admission")
                self.assertEqual(runtime.scheduler.state["phase_control"]["cycle"], 2)
                self.assertEqual(runtime.scheduler.effective_problem()["problem_text"], ROOT)
                self.assertNotIn("advisor_control", runtime.scheduler.state)
            finally:
                runtime.close()

    def test_hard_pause_feedback_resume_and_next_problem_visibility(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            executor = _AdvisorExecutor()
            runtime = FrantaRuntime.initialize(
                load_manifest(_write_manifest(Path(raw), advisor=True)),
                executor=executor,
            )
            try:
                runtime.scheduler.commit_initial_trim({"category_ids": []})
                original = runtime.scheduler.effective_problem()
                self.assertTrue(original["original"])
                self.assertEqual(original["problem_text"], ROOT)
                _enter_franta_drain(runtime, marker="CYCLE-1")
                with self.assertRaisesRegex(
                    WorkflowError,
                    "Advisor-enabled Franta drain must commit its problem assignment",
                ):
                    runtime.scheduler.complete_franta_drain()
                self.assertEqual(runtime.scheduler.alternation_phase, "franta_drain")

                waiting = runtime.run(max_cycles=1)

                self.assertEqual(waiting["advisor"]["status"], "waiting_for_human")
                self.assertEqual(runtime.scheduler.alternation_phase, "franta_drain")
                self.assertEqual(runtime.scheduler.state["phase_control"]["cycle"], 1)
                self.assertEqual([call.kind for call in executor.calls], ["advisor-proposal"])
                proposal = executor.calls[0]
                self.assertFalse(proposal.resume)
                self.assertEqual(proposal.session_key, "advisor:project")
                self.assertEqual(proposal.payload["previous_assignments"], [])
                self.assertIn("host_memory_snapshot", proposal.payload)
                report = waiting["advisor"]["selection_report"]
                self.assertEqual([item["rank"] for item in report["obligations"]], [1, 2, 3, 4, 5])
                archived_report = runtime.layout.root / waiting["advisor"]["selection_report_path"]
                self.assertTrue(archived_report.is_file())
                self.assertIn(
                    "# Advisor selection report",
                    archived_report.read_text(encoding="utf-8"),
                )

                request_id = waiting["advisor"]["feedback_request_id"]
                feedback_payload = {
                    "feedback_id": "HUMAN-FEEDBACK-1",
                    "choices": [
                        {"kind": "listed", "obligation_id": "ADV-1-3"},
                        {"kind": "custom", "statement": CUSTOM_SUBPROBLEM},
                    ],
                    "instructions": "Preserve both choices and their order exactly.",
                }
                runtime.submit_advisor_feedback(request_id, feedback_payload)
                runtime.submit_advisor_feedback(request_id, feedback_payload)
                conflicting = {
                    **feedback_payload,
                    "choices": [
                        {"kind": "listed", "obligation_id": "ADV-1-4"}
                    ],
                }
                with self.assertRaises((IdempotencyConflict, RuntimeErrorBase)):
                    runtime.submit_advisor_feedback(request_id, conflicting)
                self.assertEqual(
                    runtime.status()["advisor"]["status"], "ready_to_finalize"
                )
                self.assertEqual(len(executor.calls), 1)

                completed = runtime.run(max_cycles=1)

                self.assertEqual(runtime.scheduler.alternation_phase, "explorer_admission")
                self.assertEqual(runtime.scheduler.state["phase_control"]["cycle"], 2)
                self.assertEqual([call.kind for call in executor.calls], ["advisor-proposal", "advisor-finalize"])
                final_call = executor.calls[1]
                self.assertTrue(final_call.resume)
                self.assertEqual(final_call.session_key, proposal.session_key)
                self.assertEqual(
                    runtime.scheduler.advisor_state["session_id"],
                    "test-session:advisor:project",
                )
                self.assertEqual(completed["advisor"]["status"], "idle")
                self.assertEqual(completed["advisor"]["completed_rounds"], 1)

                problem = runtime.scheduler.effective_problem()
                self.assertFalse(problem["original"])
                self.assertEqual(problem["target_cycle"], 2)
                self.assertEqual(
                    [item["statement"] for item in problem["subproblems"]],
                    [
                        "Prove the cycle 1 decisive reduction number 3.",
                        CUSTOM_SUBPROBLEM,
                    ],
                )
                self.assertEqual(runtime.scheduler.state["root"]["problem"], ROOT)
                self.assertIn(f"Original Problem:\n{ROOT}", problem["problem_text"])
                self.assertIn(
                    "Subproblem 1:\nProve the cycle 1 decisive reduction number 3.",
                    problem["problem_text"],
                )
                self.assertIn(
                    f"Subproblem 2:\n{CUSTOM_SUBPROBLEM}",
                    problem["problem_text"],
                )
                history = runtime.scheduler.advisor_state["history"]
                problem_file = runtime.layout.root / history[0]["assignment_file"]
                self.assertEqual(problem_file.read_text(encoding="utf-8"), problem["problem_text"])

                phase = runtime.scheduler.state["phase_control"]
                admitted_at = datetime.fromisoformat(phase["entered_at"])
                lineage_id = runtime.scheduler.admit_explorer_lineage(now=admitted_at)
                planned = runtime.scheduler.start_explorer_attempt(
                    lineage_id,
                    source_high_water_seq=0,
                    now=admitted_at,
                )
                explorer_input = runtime.scheduler.state["calls"][planned["call_id"]]["input"]
                self.assertEqual(explorer_input["root_problem"], problem["problem_text"])
                self.assertEqual(
                    explorer_input["problem_assignment"]["assignment_id"],
                    problem["assignment_id"],
                )
                main_context = runtime._main_context("BATCH-ADVISOR-VISIBILITY")
                self.assertEqual(main_context["root_problem"], problem["problem_text"])
                self.assertEqual(
                    main_context["problem_assignment"]["assignment_id"],
                    problem["assignment_id"],
                )
            finally:
                runtime.close()

    def test_next_advisor_round_resumes_session_and_sees_assignment_history(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            executor = _AdvisorExecutor()
            runtime = FrantaRuntime.initialize(
                load_manifest(_write_manifest(Path(raw), advisor=True)),
                executor=executor,
            )
            try:
                runtime.scheduler.commit_initial_trim({"category_ids": []})
                stale_evidence_id = runtime.store.add_memo(
                    "advisor-stale-evidence",
                    {
                        "abstract": "Evidence already known before the first assignment.",
                        "genre": "high-level",
                        "content": "This evidence predates the assignment.",
                        "related_route_ids": [],
                    },
                ).canonical_id
                assert stale_evidence_id is not None
                _enter_franta_drain(runtime, marker="FIRST")
                first_wait = runtime.run(max_cycles=1)
                runtime.submit_advisor_feedback(
                    first_wait["advisor"]["feedback_request_id"],
                    {
                        "choices": [
                            {"kind": "listed", "obligation_id": "ADV-1-1"}
                        ]
                    },
                )
                runtime.run(max_cycles=1)

                fresh_evidence_id = runtime.store.add_memo(
                    "advisor-fresh-evidence",
                    {
                        "abstract": "A breakthrough found after the first assignment.",
                        "genre": "high-level",
                        "content": "This evidence is new in the second Advisor snapshot.",
                        "related_route_ids": [],
                    },
                ).canonical_id
                assert fresh_evidence_id is not None

                _enter_franta_drain(runtime, marker="SECOND")
                second_wait = runtime.run(max_cycles=1)

                proposal_calls = [
                    call for call in executor.calls if call.kind == "advisor-proposal"
                ]
                self.assertEqual(len(proposal_calls), 2)
                self.assertTrue(proposal_calls[1].resume)
                self.assertEqual(
                    proposal_calls[1].session_key, proposal_calls[0].session_key
                )
                previous = proposal_calls[1].payload["previous_assignments"]
                self.assertEqual(len(previous), 1)
                self.assertEqual(previous[0]["target_cycle"], 2)
                self.assertEqual(
                    proposal_calls[1].payload["problem_assignment"]["assignment_id"],
                    previous[0]["assignment_id"],
                )
                freshness = proposal_calls[1].payload[
                    "breakthrough_evidence_freshness"
                ]
                self.assertEqual(
                    freshness["memory_snapshot_id"],
                    proposal_calls[1].payload["memory_snapshot"]["snapshot_id"],
                )
                self.assertNotIn(stale_evidence_id, freshness["records"])
                self.assertEqual(
                    freshness["records"][fresh_evidence_id],
                    {"revision": 1, "newer_than_advisor_index": 1},
                )
                self.assertEqual(
                    second_wait["advisor"]["status"], "waiting_for_human"
                )
                self.assertEqual(second_wait["advisor"]["advisor_index"], 2)
                self.assertEqual(runtime.scheduler.alternation_phase, "franta_drain")
            finally:
                runtime.close()

    def test_invalid_proposal_retry_uses_a_fresh_workspace_generation(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            executor = _RetryingAdvisorExecutor()
            runtime = FrantaRuntime.initialize(
                load_manifest(_write_manifest(Path(raw), advisor=True)),
                executor=executor,
            )
            try:
                runtime.scheduler.commit_initial_trim({"category_ids": []})
                _enter_franta_drain(runtime, marker="RETRY")

                waiting = runtime.run(max_cycles=1)

                self.assertEqual(waiting["advisor"]["status"], "waiting_for_human")
                self.assertEqual(len(executor.calls), 2)
                self.assertEqual(
                    [call.call_id for call in executor.calls],
                    [executor.calls[0].call_id, executor.calls[0].call_id],
                )
                self.assertNotEqual(
                    executor.workspace_inodes[0], executor.workspace_inodes[1]
                )
                self.assertEqual(executor.marker_visible, [False, False])
                persisted = runtime.scheduler.state["calls"][executor.calls[0].call_id]
                self.assertEqual(persisted["status"], "committed")
                self.assertEqual(persisted["attempt"], 2)
                retired = list(
                    (runtime.layout.private / "invalid-control-workspaces").glob(
                        f"{executor.calls[0].call_id}-attempt-*"
                    )
                )
                self.assertEqual(len(retired), 1)
                self.assertEqual(
                    (retired[0] / "artifacts" / "invalid-first-generation.txt").read_text(
                        encoding="utf-8"
                    ),
                    "must be retired\n",
                )
            finally:
                runtime.close()

    def test_restart_retires_report_staged_by_fenced_proposal_generation(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            project_dir: Path | None = None
            call_id = ""
            stale_generation = ""
            runtime = FrantaRuntime.initialize(
                load_manifest(_write_manifest(Path(raw), advisor=True)),
                executor=_AdvisorExecutor(),
            )
            try:
                runtime.scheduler.commit_initial_trim({"category_ids": []})
                _enter_franta_drain(runtime, marker="CRASHED-PROPOSAL")
                call_id = runtime._prepare_advisor_proposal(runtime._advisor_context())
                policy = policy_for("advisor", mode="proposal")
                workspace = runtime._advisor_workspace(call_id, stage="proposal")
                stale_generation = runtime._workspace_generation(workspace)
                runtime.scheduler.mark_call_running(call_id)
                call = runtime._call_spec(
                    call_id,
                    workspace=workspace,
                    policy=policy,
                    mode="proposal",
                    session_key=str(runtime.config["advisor"]["session_key"]),
                    resume=False,
                )

                # Model a hard process death after the tool returned but before
                # _run_prepared_call could accept/reject the agent's response.
                _AdvisorExecutor._report(call)
                self.assertEqual(
                    runtime.scheduler.state["calls"][call_id]["status"], "running"
                )
                self.assertTrue(
                    (
                        workspace.artifacts_path / "advisor-1-selection.md"
                    ).is_file()
                )
                project_dir = runtime.layout.root
            finally:
                runtime.close()

            assert project_dir is not None
            recovered = FrantaRuntime.open(project_dir, executor=_AdvisorExecutor())
            try:
                recovered.recover()
                self.assertEqual(
                    recovered.scheduler.state["calls"][call_id]["status"],
                    "retry_pending",
                )
                rematerialized = recovered._advisor_workspace(
                    call_id, stage="proposal"
                )
                fresh_generation = recovered._workspace_generation(rematerialized)
                self.assertNotEqual(stale_generation, fresh_generation)
            finally:
                recovered.close()

            executor = _AdvisorExecutor()
            resumed = FrantaRuntime.open(project_dir, executor=executor)
            try:
                waiting = resumed.run(resume=True, max_cycles=1)

                self.assertEqual(waiting["advisor"]["status"], "waiting_for_human")
                self.assertEqual(len(executor.calls), 1)
                self.assertEqual(executor.calls[0].call_id, call_id)
                self.assertEqual(
                    fresh_generation,
                    resumed._workspace_generation(executor.calls[0].workspace),
                )
                persisted = resumed.scheduler.state["calls"][call_id]
                self.assertEqual(persisted["status"], "committed")
                self.assertEqual(persisted["attempt"], 2)
                retired = list(
                    (
                        resumed.layout.private / "invalid-control-workspaces"
                    ).glob(f"{call_id}-attempt-*-generation-*")
                )
                self.assertEqual(len(retired), 1)
                self.assertTrue(
                    (
                        retired[0] / "artifacts" / "advisor-1-selection.md"
                    ).is_file()
                )
            finally:
                resumed.close()

    def test_authorized_retry_after_exhaustion_rematerializes_without_artifact_collision(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            executor = _ExhaustThenRecoverAdvisorExecutor()
            runtime = FrantaRuntime.initialize(
                load_manifest(_write_manifest(Path(raw), advisor=True)),
                executor=executor,
            )
            try:
                runtime.scheduler.commit_initial_trim({"category_ids": []})
                _enter_franta_drain(runtime, marker="EXHAUSTED-RETRY")

                with self.assertRaisesRegex(
                    TransportFailure, "retry limit exhausted"
                ):
                    runtime.run(max_cycles=1)

                call_id = executor.calls[0].call_id
                exhausted = runtime.scheduler.state["calls"][call_id]
                failed_generations = int(exhausted["retry_limit"]) + 1
                self.assertEqual(len(executor.calls), failed_generations)
                self.assertEqual(exhausted["status"], "needs_attention")
                self.assertFalse((runtime.layout.workspaces / call_id).exists())
                retired = sorted(
                    (runtime.layout.private / "invalid-control-workspaces").glob(
                        f"{call_id}-attempt-*"
                    )
                )
                self.assertEqual(len(retired), failed_generations)
                for generation in retired:
                    self.assertTrue(
                        (
                            generation
                            / "artifacts"
                            / "advisor-1-selection.md"
                        ).is_file()
                    )

                runtime.scheduler.retry_attention_call(call_id)
                executor.allow_success = True
                waiting = runtime.run(max_cycles=1)

                self.assertEqual(waiting["advisor"]["status"], "waiting_for_human")
                self.assertEqual(len(executor.calls), failed_generations + 1)
                self.assertEqual(
                    len(set(executor.workspace_inodes)), failed_generations + 1
                )
                recovered = runtime.scheduler.state["calls"][call_id]
                self.assertEqual(recovered["status"], "committed")
                self.assertTrue(
                    (
                        runtime.layout.workspaces
                        / call_id
                        / "artifacts"
                        / "advisor-1-selection.md"
                    ).is_file()
                )
            finally:
                runtime.close()

    def test_generated_problem_is_frozen_into_every_downstream_call_input(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            executor = _AdvisorExecutor()
            runtime = FrantaRuntime.initialize(
                load_manifest(_write_manifest(Path(raw), advisor=True)),
                executor=executor,
            )
            try:
                runtime.scheduler.commit_initial_trim({"category_ids": []})
                _enter_franta_drain(runtime, marker="BINDING-FIRST")
                waiting = runtime.run(max_cycles=1)
                runtime.submit_advisor_feedback(
                    waiting["advisor"]["feedback_request_id"],
                    {
                        "choices": [
                            {"kind": "listed", "obligation_id": "ADV-1-2"}
                        ]
                    },
                )
                runtime.run(max_cycles=1)
                problem = runtime.scheduler.effective_problem()

                _enter_franta_drain(
                    runtime,
                    marker="BINDING-SECOND",
                    stop_in_run=True,
                )

                fact_task = runtime.scheduler.submit_batch(
                    "BATCH-ADVISOR-FACT",
                    [_ordinary_assignment("ADVISOR-FACT")],
                )[0]
                fact_attempt = runtime.scheduler.start_task_attempt(fact_task)
                fact_operation_id = "OP-ADVISOR-FACT"
                runtime.scheduler.ingest_progress(
                    {
                        "progress_id": "PRG-ADVISOR-FACT",
                        "task_id": fact_task,
                        "attempt": fact_attempt,
                        "sequence": 1,
                        "is_final": True,
                        "outcome_status": "finished",
                        "operations": [_fact_operation(fact_operation_id)],
                        "completion_evidence_ids": [fact_operation_id],
                        "attempt_summary": _attempt_summary(
                            proposed_outcome="finished",
                            evidence=[fact_operation_id],
                        ),
                    }
                )
                synthesizer_call = runtime.scheduler.prepare_synthesizer_call(
                    fact_operation_id
                )
                runtime.scheduler.apply_synthesizer_result(
                    fact_operation_id,
                    {
                        "resolution": "new",
                        "operation_digest": runtime.scheduler.state["operations"][
                            fact_operation_id
                        ]["input_digest"],
                        "relied_on": [],
                    },
                )
                verifier_call = runtime.scheduler.prepare_verifier_call(
                    fact_operation_id
                )
                verifier_input = runtime.scheduler.state["calls"][verifier_call][
                    "input"
                ]
                canonical_fact_id = runtime.scheduler.apply_verifier_report(
                    fact_operation_id,
                    _exact_verifier_report(verifier_input),
                )
                self.assertIsNotNone(canonical_fact_id)

                challenge_task = runtime.scheduler.submit_batch(
                    "BATCH-ADVISOR-CHALLENGE",
                    [_ordinary_assignment("ADVISOR-CHALLENGE")],
                )[0]
                challenge_attempt = runtime.scheduler.start_task_attempt(
                    challenge_task
                )
                runtime.scheduler.ingest_progress(
                    {
                        "progress_id": "PRG-ADVISOR-CHALLENGE",
                        "task_id": challenge_task,
                        "attempt": challenge_attempt,
                        "sequence": 1,
                        "is_final": True,
                        "outcome_status": "finished",
                        "operations": [],
                        "completion_evidence_ids": [],
                        "fact_challenges": [
                            {
                                "challenge_id": "CH-ADVISOR-BINDING",
                                "fact_id": canonical_fact_id,
                                "alleged_failure": "Check the decisive boundary case.",
                            }
                        ],
                        "attempt_summary": _attempt_summary(
                            proposed_outcome="finished",
                            evidence=[],
                        ),
                    }
                )
                challenge_call = runtime.scheduler.prepare_challenge_verifier_call(
                    "CH-ADVISOR-BINDING"
                )

                closure_task = runtime.scheduler.submit_batch(
                    "BATCH-ADVISOR-CLOSURE",
                    [_ordinary_assignment("ADVISOR-CLOSURE")],
                )[0]
                closure_attempt = runtime.scheduler.start_task_attempt(closure_task)
                route_operation_id = "OP-ADVISOR-ABANDONED-ROUTE"
                runtime.scheduler.ingest_progress(
                    {
                        "progress_id": "PRG-ADVISOR-CLOSURE",
                        "task_id": closure_task,
                        "attempt": closure_attempt,
                        "sequence": 1,
                        "is_final": True,
                        "outcome_status": "progress",
                        "operations": [
                            {
                                "operation_id": route_operation_id,
                                "kind": "route_add",
                                "proposal_id": "TMP-ADVISOR-ABANDONED-ROUTE",
                                "abstract": "A route requiring explicit closure review.",
                            }
                        ],
                        "completion_evidence_ids": [route_operation_id],
                        "attempt_summary": _attempt_summary(
                            proposed_outcome="progress",
                            evidence=[route_operation_id],
                        ),
                    }
                )
                runtime.scheduler.abandon_operation(
                    route_operation_id,
                    authorized_by="operator",
                    reason="Create the deterministic closure-review fixture.",
                )
                closure_call = runtime.scheduler.prepare_main_closure_review_call(
                    closure_task
                )

                sprint_id = "SPRINT-ADVISOR-SUMMARY"
                with runtime.scheduler._mutate() as state:
                    state["sprints"][sprint_id] = {
                        "sprint_id": sprint_id,
                        "status": "awaiting_summary",
                        "frozen_synthesis_input": {
                            "plan": {"target": "the assigned subproblem"},
                            "lane_results": [],
                        },
                        "frozen_synthesis_digest": "fixture",
                        "summary": None,
                    }
                summarizer_call = runtime.scheduler.prepare_sprint_summarizer_call(
                    sprint_id
                )

                for call_id in (
                    synthesizer_call,
                    verifier_call,
                    challenge_call,
                    closure_call,
                    summarizer_call,
                ):
                    with self.subTest(call_id=call_id):
                        call_input = runtime.scheduler.state["calls"][call_id]["input"]
                        self.assertEqual(
                            call_input["root_problem"], problem["problem_text"]
                        )
                        self.assertEqual(
                            call_input["problem_assignment"]["assignment_id"],
                            problem["assignment_id"],
                        )
            finally:
                runtime.close()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
