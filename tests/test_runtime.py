from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from franta.config import load_manifest
from franta.access import AccessPolicy, policy_for
from franta.advisor_adapter import advisor_agent_call_spec
from franta.runtime import (
    AgentCall,
    FrantaRuntime,
    WORKER_CALL_TIMEOUT_SECONDS,
)
from franta.read_access.main_memory_snapshot import (
    MAIN_MEMORY_SNAPSHOT_RELATIVE_PATH,
    validate_main_memory_snapshot,
)
from franta.skill_runtime import SkillContext, SkillRuntime
from franta.transport import CodexResult


def _manifest(root: Path, *, name: str = "runtime-test") -> Path:
    path = root / "bootstrap.toml"
    path.write_text(
        "\n".join(
            [
                "[project]",
                f'name = "{name}"',
                'directory = "project"',
                'root_problem = "Prove that every test object has property P."',
                'foundation_policy = "Use only the declared definition of a test object."',
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


def _portfolio() -> dict[str, list[str]]:
    return {
        kind: []
        for kind in ("fact", "route", "memo", "claim", "obligation", "computation")
    }


class RuntimeIntegrationTests(unittest.TestCase):
    def test_worker_call_timeout_is_four_hours_despite_legacy_one_hour_default(
        self,
    ) -> None:
        class RecordingTransport:
            def __init__(self) -> None:
                self.requests: list[object] = []

            def invoke(self, request: object) -> CodexResult:
                self.requests.append(request)
                return CodexResult(
                    call_id=request.call_id,
                    thread_id="worker-timeout-thread",
                    resumed=False,
                    returncode=0,
                    final_message=json.dumps(
                        {"attempt_ended": True, "final_progress_id": None}
                    ),
                )

        with tempfile.TemporaryDirectory() as directory:
            manifest_path = _manifest(Path(directory), name="worker-four-hour-timeout")
            manifest_path.write_text(
                manifest_path.read_text(encoding="utf-8")
                + "\n[timeouts]\nagent_call_seconds = 3600\n",
                encoding="utf-8",
            )
            transport = RecordingTransport()
            runtime = FrantaRuntime.initialize(
                load_manifest(manifest_path),
                transport=transport,
            )
            try:
                runtime.scheduler.commit_initial_trim({"category_ids": []})
                root_id = str(runtime.scheduler.state["root"]["obligation_id"])
                task_id = runtime.scheduler.submit_batch(
                    "B-WORKER-TIMEOUT",
                    [
                        {
                            "operation_id": "AR-WORKER-TIMEOUT",
                            "objective": "Exercise the worker watchdog.",
                            "work_mode": "brainstorm",
                            "if_resume": None,
                            "main_route_ids": [],
                            "main_obligation_ids": [root_id],
                            "selected_new_perspective": None,
                            "assignment_portfolio": _portfolio(),
                            "reason": "Verify the role-specific timeout.",
                            "root_solution_fact_id": None,
                        }
                    ],
                )[0]
                call_id, _attempt, workspace = runtime._prepare_worker_workspace(task_id)
                policy = policy_for("worker", mode="brainstorm")
                call = runtime._call_spec(
                    call_id,
                    workspace=workspace,
                    policy=policy,
                    mode="brainstorm",
                )
                binding = SimpleNamespace(token="test-token")
                with (
                    mock.patch.object(runtime.broker, "issue", return_value=binding),
                    mock.patch.object(runtime.broker, "revoke"),
                ):
                    runtime._invoke_agent(call)

                self.assertEqual(WORKER_CALL_TIMEOUT_SECONDS, 14_400)
                self.assertEqual(len(transport.requests), 1)
                self.assertEqual(transport.requests[0].timeout_seconds, 14_400)
            finally:
                runtime.close()

    def test_existing_project_rejects_model_configuration_changes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manifest_path = _manifest(Path(directory), name="immutable-models")
            manifest_path.write_text(
                manifest_path.read_text(encoding="utf-8")
                + "\n[models.default]\n"
                + 'model = "gpt-6-astra"\n'
                + 'reasoning_effort = "ultra"\n',
                encoding="utf-8",
            )
            runtime = FrantaRuntime.initialize(load_manifest(manifest_path))
            runtime.close()

            manifest_path.write_text(
                manifest_path.read_text(encoding="utf-8").replace(
                    'reasoning_effort = "ultra"',
                    'reasoning_effort = "high"',
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                RuntimeError, "different bootstrap manifest"
            ):
                FrantaRuntime.initialize(load_manifest(manifest_path))

    def test_reopened_sol_project_launches_astra_without_rewriting_config(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manifest_path = _manifest(Path(directory), name="legacy-sol-models")
            manifest_path.write_text(
                manifest_path.read_text(encoding="utf-8")
                + '\n[models.default]\nmodel = "gpt-5.6-sol"\n'
                + 'reasoning_effort = "max"\n'
                + '\n[models.synthesizer]\nmodel = "gpt-5.6-sol"\n'
                + 'reasoning_effort = "xhigh"\n',
                encoding="utf-8",
            )
            runtime = FrantaRuntime.initialize(load_manifest(manifest_path))
            project_dir = runtime.layout.root
            config_path = runtime.layout.private / "runtime-config.json"
            original_config = config_path.read_bytes()
            runtime.close()

            runtime = FrantaRuntime.open(project_dir)
            try:
                for kind, effort, policy in (
                    ("verifier", "max", policy_for("verifier")),
                    ("synthesizer", "xhigh", policy_for("synthesizer", review_memory_type="fact")),
                ):
                    with self.subTest(role=kind):
                        payload = {"label": kind}
                        call_id = runtime.scheduler.prepare_call(kind, payload)
                        workspace = runtime._make_workspace(
                            call_id=call_id, policy=policy, context=payload
                        )
                        spec = runtime._call_spec(
                            call_id, workspace=workspace, policy=policy
                        )
                        self.assertEqual(spec.model_config.model, "gpt-6-astra")
                        self.assertEqual(spec.model_config.reasoning_effort, effort)
                self.assertEqual(config_path.read_bytes(), original_config)
            finally:
                runtime.close()

            for stage in ("proposal", "finalize"):
                with self.subTest(advisor_stage=stage):
                    spec = advisor_agent_call_spec(
                        stage=stage,
                        advisor_index=1,
                        settings={"model": "gpt-5.6-sol", "reasoning_effort": "ultra"},
                    )
                    self.assertEqual(spec.model_config.model, "gpt-6-astra")
                    self.assertEqual(spec.model_config.reasoning_effort, "ultra")

    def test_runtime_uses_operator_configured_models(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manifest_path = _manifest(Path(directory), name="configured-models")
            manifest_path.write_text(
                manifest_path.read_text(encoding="utf-8")
                + "\n[models.default]\n"
                + 'model = "operator-default"\n'
                + 'reasoning_effort = "high"\n'
                + "\n[models.synthesizer]\n"
                + 'model = "operator-synthesizer"\n'
                + 'reasoning_effort = "xhigh"\n',
                encoding="utf-8",
            )
            runtime = FrantaRuntime.initialize(load_manifest(manifest_path))
            try:
                def call_spec(
                    label: str,
                    kind: str,
                    policy: AccessPolicy,
                    *,
                    mode: str | None = None,
                ) -> AgentCall:
                    payload = {"input": label}
                    continuation = None
                    if kind == "main":
                        batch_id = "BATCH-MODEL-CONFIG"
                        payload = runtime._main_context(batch_id)
                        continuation = {
                            "reserved_batch_id": batch_id,
                            "session_key": "main:project",
                        }
                    call_id = runtime.scheduler.prepare_call(
                        kind, payload, continuation=continuation
                    )
                    workspace = runtime._make_workspace(
                        call_id=call_id, policy=policy, context=payload
                    )
                    return runtime._call_spec(
                        call_id,
                        workspace=workspace,
                        policy=policy,
                        mode=mode,
                    )

                unchanged_default_roles = (
                    ("main", "main", policy_for("main")),
                    ("trimmer", "trimmer", policy_for("trimmer")),
                    ("verifier", "verifier", policy_for("verifier")),
                    (
                        "challenge-verifier",
                        "challenge-verifier",
                        policy_for("verifier"),
                    ),
                    (
                        "main-closure-review",
                        "main-closure-review",
                        policy_for("main"),
                    ),
                )
                for label, kind, policy in unchanged_default_roles:
                    with self.subTest(role=label):
                        spec = call_spec(label, kind, policy)
                        self.assertEqual(
                            spec.model_config.model, "operator-default"
                        )
                        self.assertEqual(
                            spec.model_config.reasoning_effort, "high"
                        )

                synth_spec = call_spec(
                    "synthesizer",
                    "synthesizer",
                    policy_for("synthesizer", review_memory_type="fact"),
                )
                self.assertEqual(
                    synth_spec.model_config.model, "operator-synthesizer"
                )
                self.assertEqual(
                    synth_spec.model_config.reasoning_effort, "xhigh"
                )

                max_worker_routes = (
                    (
                        "ordinary-worker",
                        "worker",
                        policy_for("worker", mode="research"),
                        "research",
                    ),
                    (
                        "proof-writer",
                        "worker",
                        policy_for("proof-writer", mode="proof-writer"),
                        "proof-writer",
                    ),
                    (
                        "sprint-lane-A",
                        "worker",
                        policy_for("worker", mode="brainstorm", sprint_lane="A"),
                        "brainstorm",
                    ),
                    (
                        "sprint-lane-B",
                        "worker",
                        policy_for(
                            "worker", mode="multi-discipline", sprint_lane="B"
                        ),
                        "multi-discipline",
                    ),
                    (
                        "sprint-lane-C",
                        "worker",
                        policy_for("worker", mode="computation", sprint_lane="C"),
                        "computation",
                    ),
                    (
                        "sprint-lane-D",
                        "worker",
                        policy_for("worker", mode="associate", sprint_lane="D"),
                        "associate",
                    ),
                    (
                        "sprint-summarizer",
                        "summarizer",
                        policy_for("summarizer"),
                        None,
                    ),
                )
                for label, kind, policy, mode in max_worker_routes:
                    with self.subTest(role=label):
                        spec = call_spec(label, kind, policy, mode=mode)
                        self.assertEqual(spec.model_config.model, "gpt-6-astra")
                        self.assertEqual(
                            spec.model_config.reasoning_effort, "max"
                        )
            finally:
                runtime.close()

    def test_repeated_worker_outbox_poll_does_not_rearchive_ingested_progress(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime = FrantaRuntime.initialize(
                load_manifest(_manifest(Path(directory), name="outbox-poll-replay")),
                executor=lambda _call: {},
            )
            try:
                runtime.scheduler.commit_initial_trim({"category_ids": []})
                root_id = str(runtime.scheduler.state["root"]["obligation_id"])
                task_id = runtime.scheduler.submit_batch(
                    "B-OUTBOX-POLL",
                    [
                        {
                            "operation_id": "AR-OUTBOX-POLL",
                            "objective": "Record one durable intermediate result.",
                            "work_mode": "brainstorm",
                            "if_resume": None,
                            "main_route_ids": [],
                            "main_obligation_ids": [root_id],
                            "selected_new_perspective": None,
                            "assignment_portfolio": _portfolio(),
                            "reason": "Exercise repeated outbox polling.",
                            "root_solution_fact_id": None,
                        }
                    ],
                )[0]
                call_id, attempt, workspace = runtime._prepare_worker_workspace(task_id)
                SkillRuntime(SkillContext.load(workspace.path)).invoke(
                    "record-progress",
                    {
                        "operation_id": "RP-OUTBOX-POLL",
                        "progress_id": "PRG-OUTBOX-POLL",
                        "sequence": 1,
                        "is_final": False,
                        "outcome_status": "progress",
                        "progress_since_previous": "Recorded one intermediate result.",
                        "operations": [],
                        "computation_operation_ids": [],
                        "fact_challenges": [],
                        "completion_evidence_ids": [],
                    },
                )
                call_state = runtime.scheduler.state["calls"][call_id]

                def ingest() -> bool:
                    return runtime._ingest_worker_outbox(
                        task_id,
                        call_id,
                        workspace,
                        expected_lease_epoch=int(call_state["lease_epoch"]),
                        expected_launch_attempt=int(call_state["attempt"]),
                    )

                with mock.patch.object(
                    runtime,
                    "_archive_workspace_artifacts",
                    wraps=runtime._archive_workspace_artifacts,
                ) as archive:
                    self.assertTrue(ingest())
                    revision_after_first_poll = runtime.scheduler.revision
                    self.assertFalse(ingest())

                self.assertEqual(archive.call_count, 1)
                self.assertEqual(runtime.scheduler.revision, revision_after_first_poll)
                self.assertIn("PRG-OUTBOX-POLL", runtime.scheduler.state["progress"])
                self.assertEqual(
                    runtime.scheduler.state["tasks"][task_id]["current_attempt"],
                    attempt,
                )
            finally:
                runtime.close()

    def test_scripted_runtime_enforces_main_and_worker_terminal_skills(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            main_calls = 0
            main_invocations: list[AgentCall] = []
            main_snapshot_paths: list[Path] = []

            def executor(call: AgentCall) -> dict:
                nonlocal main_calls
                if call.kind == "trimmer":
                    raise AssertionError("new Franta runs must not invoke trimmer")
                if call.kind == "main":
                    main_calls += 1
                    main_invocations.append(call)
                    descriptor = call.payload["memory_snapshot"]
                    snapshot_path = (
                        call.workspace.path / MAIN_MEMORY_SNAPSHOT_RELATIVE_PATH
                    )
                    validate_main_memory_snapshot(
                        snapshot_path,
                        expected_snapshot_id=descriptor["snapshot_id"],
                        expected_snapshot_digest=descriptor["snapshot_digest"],
                    )
                    main_snapshot_paths.append(snapshot_path)
                    self.assertFalse(
                        (call.workspace.path / ".agents/skills/internal-search").exists()
                    )
                    if main_calls == 1:
                        runtime.transport.ledger.record(
                            session_key=str(call.session_key),
                            thread_id="THREAD-MAIN-PROJECT",
                            call_id=call.call_id,
                            role="main",
                        )
                    root_id = call.payload["root"]["obligation_id"]
                    batch_id = call.payload["reserved_batch_id"]
                    operation_id = (
                        "AR-FIRST" if main_calls == 1 else f"AR-FOLLOWUP-{main_calls}"
                    )
                    artifact = SkillRuntime(SkillContext.load(call.workspace.path)).invoke(
                        "task-writing",
                        {
                            "operation_id": operation_id,
                            "batch_finalized": True,
                            "batch_id": batch_id,
                            "objective": "Independently attack the root obligation.",
                            "work_mode": "brainstorm",
                            "if_resume": None,
                            "main_route_ids": [],
                            "main_obligation_ids": [root_id],
                            "selected_new_perspective": None,
                            "assignment_portfolio": _portfolio(),
                            "reason": "A clean first attempt avoids premature route bias.",
                            "root_solution_fact_id": None,
                        },
                    )["artifact"]
                    return {
                        "decision": "assignments",
                        "batch_id": batch_id,
                        "assignment_report_ids": [artifact["operation_id"]],
                        "wait_for_task_ids": [],
                        "decline_proof_writer": False,
                    }
                if call.kind == "worker":
                    card = json.loads(call.workspace.task_card_path.read_text(encoding="utf-8"))
                    progress_id = f"PRG-{card['task_id']}-{card['attempt']}"
                    SkillRuntime(SkillContext.load(call.workspace.path)).invoke(
                        "record-progress",
                        {
                            "operation_id": f"FINAL-{card['task_id']}-{card['attempt']}",
                            "progress_id": progress_id,
                            "sequence": 1,
                            "is_final": True,
                            "outcome_status": "failed",
                            "progress_since_previous": "No rigorous advance survived checking.",
                            "operations": [],
                            "computation_operation_ids": [],
                            "fact_challenges": [],
                            "completion_evidence_ids": [],
                            "attempt_summary": {
                                "work_mode": card["mode"],
                                "task": card["objective"],
                                "proposed_outcome": "failed",
                                "cumulative_important_progress": "No significant progress.",
                                "completion_evidence_operation_ids": [],
                                "most_promising_next_steps": "Try a structurally different mode.",
                            },
                        },
                    )
                    return {"attempt_ended": True, "final_progress_id": progress_id}
                raise AssertionError(f"unexpected call {call.kind}")

            runtime = FrantaRuntime.initialize(
                load_manifest(_manifest(Path(directory), name="skills-runtime")),
                executor=executor,
            )
            try:
                status = runtime.run(max_cycles=4)
                self.assertNotIn("trimming", status["gate"])
                state = runtime.scheduler.state
                task = next(
                    item
                    for item in state["tasks"].values()
                    if item["final_status"] == "failed"
                )
                self.assertEqual(task["final_status"], "failed")
                self.assertEqual(
                    task["attempts"][0]["final_progress_id"],
                    f"PRG-{task['task_id']}-1",
                )
                main_workspace = next(
                    path
                    for path in runtime.layout.workspaces.iterdir()
                    if (path / "outbox/task-writing/AR-FIRST.json").is_file()
                )
                self.assertTrue((main_workspace / "outbox/skill_activity.jsonl").is_file())
                worker_workspace = runtime.layout.workspaces / task["attempts"][0]["call_id"]
                self.assertTrue(
                    any((worker_workspace / "outbox/record-progress").glob("*.json"))
                )
                self.assertGreaterEqual(len(main_invocations), 2)
                self.assertEqual(
                    {item.session_key for item in main_invocations},
                    {"main:project"},
                )
                self.assertFalse(main_invocations[0].resume)
                self.assertTrue(main_invocations[1].resume)
                self.assertEqual(
                    len(
                        {
                            item.payload["memory_snapshot"]["snapshot_id"]
                            for item in main_invocations
                        }
                    ),
                    len(main_invocations),
                )
                for item, path in zip(main_invocations, main_snapshot_paths):
                    validate_main_memory_snapshot(
                        path,
                        expected_snapshot_id=item.payload["memory_snapshot"][
                            "snapshot_id"
                        ],
                        expected_snapshot_digest=item.payload["memory_snapshot"][
                            "snapshot_digest"
                        ],
                    )
            finally:
                runtime.close()

    def test_sprint_serializes_every_lane_through_task_writing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime = FrantaRuntime.initialize(load_manifest(_manifest(Path(directory))))
            try:
                runtime.scheduler.commit_initial_trim({"category_ids": []})
                runtime.scheduler.submit_stuck_report(
                    "B-stuck", {"summary": "All work repeats one direct mechanism."}
                )
                runtime.scheduler.apply_trim_review_decision(
                    "trim", "A discovery sprint is warranted."
                )
                root_id = runtime.scheduler.state["root"]["obligation_id"]
                root_record = runtime.store.get(root_id).to_dict()
                distant_ids = []
                for index in (1, 2):
                    result = runtime.store.add_memo(
                        f"sprint-distant-memo-{index}",
                        {
                            "abstract": f"Distant mechanism {index}",
                            "genre": "normal",
                            "content": f"A deliberately distant bridge prompt {index}.",
                            "related_route_ids": [],
                        },
                    )
                    assert result.canonical_id is not None
                    distant_ids.append(result.canonical_id)
                common = {
                    "main_obligation_ids": [root_id],
                    "assignment_portfolio": _portfolio(),
                    "reason": "This lane is deliberately isolated from existing routes.",
                }
                plan = {
                    "target_obligation": {
                        "id": root_id,
                        "revision": root_record["revision"],
                        "statement": root_record["statement"],
                    },
                    "repeated_mechanism": "Direct unfolding",
                    "unchanged_obstacle": "The boundary case",
                    "lanes": [
                        {
                            **common,
                            "mode": "brainstorm",
                            "objective": "Find an independent attack on the boundary case.",
                        },
                        {
                            **common,
                            "mode": "multi-discipline",
                            "objective": "Translate the boundary case categorically.",
                            "selected_new_perspective": "derived categorical invariants",
                        },
                        {
                            **common,
                            "mode": "computation",
                            "objective": "Test explicit boundary examples.",
                            "computation_portfolio": [
                                "Enumerate the smallest boundary examples."
                            ],
                        },
                        {
                            **common,
                            "mode": "associate",
                            "objective": "Seek a remote bridge to the boundary case.",
                            "assignment_portfolio": {
                                **_portfolio(),
                                "memo": distant_ids,
                            },
                        },
                    ],
                }
                runtime.scheduler.persist_sprint_plan("S-1", plan)
                launched_batches: list[list[str]] = []
                runtime._run_worker_batch = lambda task_ids: (
                    launched_batches.append(list(task_ids)) or True
                )
                self.assertTrue(runtime._advance_sprint())
                sprint = runtime.scheduler.state["sprints"]["S-1"]
                self.assertEqual(sprint["status"], "running")
                self.assertEqual(len(sprint["task_ids"]), 4)
                self.assertEqual(launched_batches, [sprint["task_ids"]])
                for index, task_id in enumerate(sprint["task_ids"]):
                    task = runtime.scheduler.state["tasks"][task_id]
                    self.assertEqual(task["sprint_lane"], "ABCD"[index])
                    policy = task["task_card"]["access_policy"]
                    self.assertTrue(policy["sealed_workspace"])
                    self.assertFalse(policy["internal_search"])
                    if task["sprint_lane"] == "C":
                        self.assertEqual(
                            task["task_card"]["computation_portfolio"],
                            ["Enumerate the smallest boundary examples."],
                        )

                workspace = runtime.layout.workspaces / "SPRINT-TW-S-1"
                artifacts = sorted((workspace / "outbox/task-writing").glob("*.json"))
                self.assertEqual(len(artifacts), 4)
                activity = [
                    json.loads(line)
                    for line in (workspace / "outbox/skill_activity.jsonl")
                    .read_text(encoding="utf-8")
                    .splitlines()
                ]
                self.assertEqual([item["skill"] for item in activity], ["task-writing"] * 4)
            finally:
                runtime.close()


if __name__ == "__main__":
    unittest.main()
