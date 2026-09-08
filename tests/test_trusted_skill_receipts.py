from __future__ import annotations

import copy
import dataclasses
import hashlib
import json
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from franta.access import BrokerClient, BrokerError, policy_for
from franta.config import load_manifest
from franta.materialize import WorkspaceMaterializer
from franta.prompts import ModelConfig
from franta.runtime import AgentCall, FrantaRuntime, InvalidAgentOutput
from franta.skill_runtime import SkillContext, SkillRuntime
from franta.transport import CodexTransportError


def _portfolio() -> dict[str, list[str]]:
    result = {
        kind: []
        for kind in ("fact", "route", "memo", "claim", "obligation", "computation")
    }
    result["route"] = ["R-1"]
    return result


class TrustedSkillReceiptTests(unittest.TestCase):
    def _fixture(
        self, root: Path, *, record_private: bool = True
    ) -> tuple[FrantaRuntime, AgentCall, dict, dict]:
        policy = policy_for("main")
        workspace = WorkspaceMaterializer(root / "workspaces").create(
            "CALL-1",
            root_problem="Prove ROOT.",
            policy=policy,
            context={"reserved_batch_id": "BATCH-1"},
        )
        payload = {
            "operation_id": "AR-1",
            "batch_finalized": True,
            "batch_id": "BATCH-1",
            "objective": "Investigate the route",
            "work_mode": "research",
            "if_resume": None,
            "main_route_ids": ["R-1"],
            "main_obligation_ids": [],
            "selected_new_perspective": None,
            "assignment_portfolio": _portfolio(),
            "reason": "Focused test",
        }
        result = SkillRuntime(SkillContext.load(workspace.path)).invoke(
            "task-writing", payload
        )
        runtime = object.__new__(FrantaRuntime)
        runtime.executor = None
        runtime.transport = types.SimpleNamespace(state_dir=root / "transport-state")
        call = AgentCall(
            call_id="CALL-1",
            kind="main",
            role="main",
            mode=None,
            payload={},
            workspace=workspace,
            policy=policy,
            prompt="",
            session_key=None,
            resume=False,
            output_schema=root / "unused.schema.json",
            model_config=ModelConfig("gpt-6-astra", "ultra"),
        )
        if record_private:
            runtime._record_broker_skill_result(
                call=call,
                skill="task-writing",
                payload=payload,
                result=result,
                capability_token="private-capability-token",
            )
        return runtime, call, payload, result

    @staticmethod
    def _write_tool_event(
        runtime: FrantaRuntime,
        *,
        payload: dict,
        result: dict,
        call_id: str = "CALL-1",
        skill: str = "task-writing",
        event_type: str = "item.completed",
        status: str = "completed",
        error: object = None,
        lease_epoch: int = 1,
        launch_attempt: int = 1,
    ) -> None:
        tool = {
            "task-writing": "task_writing",
            "record-progress": "record_progress",
            "CAS": "execute_cas",
            "human-guidance": "human_guidance",
            "discovery-sprint": "discovery_sprint",
        }[skill]
        path = Path(runtime.transport.state_dir) / "calls" / f"{call_id}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        workspace = Path(result["staged_path"]).parents[2]
        workspace_status = workspace.stat()
        rows = [
            {
                "type": "transport.call_started",
                "call_id": call_id,
                "broker_tools": [tool],
                "lease_epoch": lease_epoch,
                "launch_attempt": launch_attempt,
                "workspace_generation": (
                    f"{int(workspace_status.st_dev)}:{int(workspace_status.st_ino)}"
                ),
            },
            {
                "type": "transport.codex_event",
                "event": {
                    "type": event_type,
                    "item": {
                        "id": "item-1",
                        "type": "mcp_tool_call",
                        "server": "franta",
                        "tool": tool,
                        "status": status,
                        "error": error,
                        "arguments": (
                            {"payload": payload}
                            if skill
                            in {"task-writing", "record-progress", "discovery-sprint"}
                            else payload
                        ),
                        "result": {
                            "structured_content": {"result": result}
                        },
                    },
                },
            },
        ]
        path.write_text(
            "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
            encoding="utf-8",
        )

    def test_unchanged_artifact_matches_private_and_terminal_mcp_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime, call, payload, result = self._fixture(Path(directory))
            # The workspace receipt is diagnostic, not an authority.
            (call.workspace.outbox_path / "skill_activity.jsonl").unlink()
            self._write_tool_event(runtime, payload=payload, result=result)
            values = runtime._verified_skill_artifacts(
                call.workspace,
                "task-writing",
                call.call_id,
                expected_lease_epoch=1,
                expected_launch_attempt=1,
            )
            self.assertEqual([item["operation_id"] for item in values], ["AR-1"])

    def test_broker_completed_progress_survives_missing_terminal_event(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            policy = policy_for("worker", mode="research")
            workspace = WorkspaceMaterializer(root / "workspaces").create(
                "CALL-WORKER",
                root_problem="Prove ROOT.",
                policy=policy,
                task_card={"task_id": "T-1", "attempt": 1, "work_mode": "research"},
            )
            payload = {
                "operation_id": "RP-1",
                "sequence": 1,
                "is_final": True,
                "outcome_status": "failed",
                "completion_evidence_ids": [],
                "attempt_summary": {
                    "work_mode": "research",
                    "task": "Investigate the route",
                    "proposed_outcome": "failed",
                    "cumulative_important_progress": "No useful result",
                    "completion_evidence_operation_ids": [],
                    "most_promising_next_steps": "Try another route",
                },
            }
            result = SkillRuntime(SkillContext.load(workspace.path)).invoke(
                "record-progress", payload
            )
            runtime = object.__new__(FrantaRuntime)
            runtime.executor = None
            runtime.transport = types.SimpleNamespace(state_dir=root / "transport-state")
            call = AgentCall(
                call_id="CALL-WORKER",
                kind="worker",
                role="worker",
                mode="research",
                payload={},
                workspace=workspace,
                policy=policy,
                prompt="",
                session_key=None,
                resume=False,
                output_schema=root / "unused.schema.json",
                model_config=ModelConfig("gpt-6-astra", "ultra"),
            )
            runtime._record_broker_skill_result(
                call=call,
                skill="record-progress",
                payload=payload,
                result=result,
                capability_token="private-worker-capability-token",
            )
            self._write_tool_event(
                runtime,
                payload=payload,
                result=result,
                call_id=call.call_id,
                skill="record-progress",
                event_type="item.started",
                status="in_progress",
            )
            values = runtime._verified_skill_artifacts(
                workspace,
                "record-progress",
                call.call_id,
                expected_lease_epoch=1,
                expected_launch_attempt=1,
            )
            self.assertEqual([item["operation_id"] for item in values], ["RP-1"])

    def test_live_poll_defers_artifact_published_after_enumeration_snapshot(
        self,
    ) -> None:
        for skill, mode in (("record-progress", "research"), ("CAS", "computation")):
            with self.subTest(skill=skill), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                call_id = "CALL-RACE-" + skill.replace("-", "_")
                policy = policy_for("worker", mode=mode)
                workspace = WorkspaceMaterializer(root / "workspaces").create(
                    call_id,
                    root_problem="Prove ROOT.",
                    policy=policy,
                    task_card={
                        "task_id": "T-RACE",
                        "attempt": 1,
                        "work_mode": mode,
                    },
                )
                skill_runtime = SkillRuntime(SkillContext.load(workspace.path))
                runtime = object.__new__(FrantaRuntime)
                runtime.executor = None
                runtime.transport = types.SimpleNamespace(
                    state_dir=root / "transport-state"
                )
                call = AgentCall(
                    call_id=call_id,
                    kind="worker",
                    role="worker",
                    mode=mode,
                    payload={},
                    workspace=workspace,
                    policy=policy,
                    prompt="",
                    session_key=None,
                    resume=False,
                    output_schema=root / "unused.schema.json",
                    model_config=ModelConfig("gpt-6-astra", "ultra"),
                )

                def stage_with_private_receipt(index: int) -> str:
                    operation_id = f"OP-RACE-{index}"
                    payload = {
                        "operation_id": operation_id,
                        "input_marker": f"input-{index}",
                    }
                    result = skill_runtime._stage(
                        skill,
                        {
                            "operation_id": operation_id,
                            "output_marker": f"output-{index}",
                        },
                    )
                    runtime._record_broker_skill_result(
                        call=call,
                        skill=skill,
                        payload=payload,
                        result=result,
                        capability_token="private-race-capability-token",
                    )
                    return operation_id

                first_operation_id = stage_with_private_receipt(1)
                artifact_directory = workspace.outbox_path / skill
                original_glob = Path.glob
                published_during_snapshot: list[str] = []

                def glob_then_publish(
                    path: Path, pattern: str, *args, **kwargs
                ):
                    snapshot = list(original_glob(path, pattern, *args, **kwargs))
                    if (
                        path == artifact_directory
                        and pattern == "*.json"
                        and not published_during_snapshot
                    ):
                        published_during_snapshot.append(
                            stage_with_private_receipt(2)
                        )
                    return iter(snapshot)

                with mock.patch.object(
                    Path,
                    "glob",
                    autospec=True,
                    side_effect=glob_then_publish,
                ):
                    first_poll = runtime._verified_skill_artifacts(
                        workspace,
                        skill,
                        call_id,
                        expected_lease_epoch=1,
                        expected_launch_attempt=1,
                        allow_pending=True,
                    )

                self.assertEqual(published_during_snapshot, ["OP-RACE-2"])
                self.assertEqual(
                    [item["operation_id"] for item in first_poll],
                    [first_operation_id],
                )
                second_poll = runtime._verified_skill_artifacts(
                    workspace,
                    skill,
                    call_id,
                    expected_lease_epoch=1,
                    expected_launch_attempt=1,
                    allow_pending=True,
                )
                self.assertEqual(
                    [item["operation_id"] for item in second_poll],
                    [first_operation_id, "OP-RACE-2"],
                )
                final_validation = runtime._verified_skill_artifacts(
                    workspace,
                    skill,
                    call_id,
                    expected_lease_epoch=1,
                    expected_launch_attempt=1,
                )
                self.assertEqual(
                    [item["operation_id"] for item in final_validation],
                    [first_operation_id, "OP-RACE-2"],
                )

    def test_disconnect_after_broker_completion_salvages_progress_end_to_end(self) -> None:
        class DisconnectingTransport:
            def __init__(self, state_dir: Path) -> None:
                self.state_dir = state_dir
                self.state_dir.mkdir(parents=True)
                self.ledger = types.SimpleNamespace(resolve=lambda key: None)

            def invoke(self, request):
                card = json.loads(
                    (Path(request.workspace) / "input/task_card.json").read_text(
                        encoding="utf-8"
                    )
                )
                BrokerClient(
                    request.broker_binding.socket_path,
                    request.broker_binding.token,
                ).call(
                    "record_progress",
                    {
                        "payload": {
                            "operation_id": "RP-DISCONNECT",
                            "progress_id": "PRG-DISCONNECT",
                            "sequence": 1,
                            "is_final": True,
                            "outcome_status": "progress",
                            "completion_evidence_ids": [],
                            "attempt_summary": {
                                "work_mode": card["mode"],
                                "task": card["objective"],
                                "proposed_outcome": "progress",
                                "cumulative_important_progress": "Durably staged.",
                                "completion_evidence_operation_ids": [],
                                "most_promising_next_steps": "Resume the attempt.",
                            },
                        }
                    },
                )
                raise CodexTransportError(
                    "connection lost after broker completion and before item.completed"
                )

            def cancel(self, call_id: str, *, reason: str = "") -> bool:
                return False

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = root / "bootstrap.toml"
            manifest.write_text(
                "\n".join(
                    (
                        "[project]",
                        'name = "trusted-disconnect"',
                        'directory = "project"',
                        'root_problem = "Prove ROOT."',
                        'foundation_policy = "Use the stated axioms."',
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
            transport = DisconnectingTransport(root / "transport-state")
            runtime = FrantaRuntime.initialize(
                load_manifest(manifest), transport=transport
            )
            try:
                runtime.start_services()
                runtime.scheduler.commit_initial_trim({"category_ids": []})
                task_id = runtime.scheduler.submit_batch(
                    "B-DISCONNECT",
                    [
                        {
                            "report_id": "AR-DISCONNECT",
                            "objective": "Investigate the obstruction.",
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
                            "reason": "Exercise broker-complete disconnect salvage.",
                        }
                    ],
                )[0]
                call_id, attempt, workspace = runtime._prepare_worker_workspace(task_id)
                with self.assertRaises(CodexTransportError):
                    runtime._invoke_worker_process(
                        task_id, call_id, attempt, workspace
                    )
                self.assertTrue(
                    runtime._salvage_staged_final_progress(
                        task_id,
                        call_id,
                        attempt,
                        workspace,
                        expected_lease_epoch=1,
                        expected_launch_attempt=1,
                    )
                )
                progress = runtime.scheduler.state["progress"]["PRG-DISCONNECT"]
                self.assertTrue(progress["interrupted_salvage"])
            finally:
                runtime.close()

    def test_failed_cas_gets_private_receipt_before_broker_surfaces_error(self) -> None:
        class FailingCasTransport:
            def __init__(self, state_dir: Path) -> None:
                self.state_dir = state_dir
                self.state_dir.mkdir(parents=True)
                self.ledger = types.SimpleNamespace(resolve=lambda key: None)
                self.surfaced_error: str | None = None

            def invoke(self, request):
                try:
                    BrokerClient(
                        request.broker_binding.socket_path,
                        request.broker_binding.token,
                    ).call(
                        "execute_cas",
                        {
                            "operation_id": "CAS-FAILED-AUDIT",
                            "software": "test-cas",
                            "arguments": ["--fail"],
                            "version_arguments": ["--version"],
                            "exact_input": "fail()\n",
                            "description": "Retain a deliberately failed CAS run.",
                            "assumptions": "None.",
                            "interpretation": "The failed run proves nothing.",
                            "related_ids": {},
                        },
                    )
                except BrokerError as exc:
                    self.surfaced_error = str(exc)
                else:
                    raise AssertionError("nonzero CAS execution did not surface an error")
                return types.SimpleNamespace(
                    final_message=json.dumps({"observed_error": self.surfaced_error})
                )

            def cancel(self, call_id: str, *, reason: str = "") -> bool:
                return False

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = root / "bootstrap.toml"
            manifest.write_text(
                "\n".join(
                    (
                        "[project]",
                        'name = "trusted-failed-cas"',
                        'directory = "project"',
                        'root_problem = "Prove ROOT."',
                        'foundation_policy = "Use the stated axioms."',
                        "",
                        "[context_budgets]",
                        "main = 1000",
                        "",
                        "[tools.extra_cas]",
                        f'test-cas = {json.dumps(sys.executable)}',
                        "",
                        "[initial]",
                    )
                )
                + "\n",
                encoding="utf-8",
            )
            transport = FailingCasTransport(root / "transport-state")
            runtime = FrantaRuntime.initialize(
                load_manifest(manifest), transport=transport
            )
            try:
                runtime.start_services()
                runtime.scheduler.commit_initial_trim({"category_ids": []})
                task_id = runtime.scheduler.submit_batch(
                    "B-FAILED-CAS",
                    [
                        {
                            "report_id": "AR-FAILED-CAS",
                            "objective": "Exercise failed CAS audit retention.",
                            "if_resume": None,
                            "mode": "computation",
                            "main_route_ids": [],
                            "main_obligation_ids": [
                                runtime.scheduler.state["root"]["obligation_id"]
                            ],
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
                            "reason": "Test receipt-before-error ordering.",
                        }
                    ],
                )[0]
                call_id, attempt, workspace = runtime._prepare_worker_workspace(
                    task_id
                )

                calls = 0

                def fake_run(context, executable, arguments, **kwargs):
                    nonlocal calls
                    calls += 1
                    if list(arguments) == ["--version"]:
                        return subprocess.CompletedProcess(
                            [str(executable), *arguments], 0, "test-cas 1.0\n", ""
                        )
                    self.assertEqual(list(arguments), ["--fail"])
                    return subprocess.CompletedProcess(
                        [str(executable), *arguments],
                        7,
                        "partial output\n",
                        "deliberate failure\n",
                    )

                with mock.patch(
                    "franta.skill_runtime._run_constrained", side_effect=fake_run
                ):
                    result = runtime._invoke_worker_process(
                        task_id, call_id, attempt, workspace
                    )

                self.assertEqual(calls, 2)
                self.assertIn("CAS execution failed with exit status 7", result["observed_error"])
                artifacts = runtime._verified_skill_artifacts(
                    workspace,
                    "CAS",
                    call_id,
                    expected_lease_epoch=1,
                    expected_launch_attempt=1,
                )
                self.assertEqual(
                    [artifact["operation_id"] for artifact in artifacts],
                    ["CAS-FAILED-AUDIT"],
                )
                self.assertEqual(artifacts[0]["exit_status"], 7)
                progress = runtime._progress_artifacts(
                    workspace,
                    verified_progress=[
                        {
                            "attempt": attempt,
                            "sequence": 1,
                            "computation_operation_ids": [],
                        }
                    ],
                    verified_computations=artifacts,
                )
                self.assertEqual(progress[0]["computations"], [])
                self.assertEqual(runtime.scheduler.state["computations"], {})
            finally:
                runtime.close()

    def test_paired_artifact_and_workspace_receipt_drift_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime, call, payload, result = self._fixture(Path(directory))
            self._write_tool_event(runtime, payload=payload, result=result)
            artifact_path = Path(result["staged_path"])
            artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
            artifact["reason"] = "coordinated hand-written replacement"
            artifact_path.write_text(json.dumps(artifact), encoding="utf-8")
            workspace_receipt_path = call.workspace.outbox_path / "skill_activity.jsonl"
            workspace_receipt = json.loads(
                workspace_receipt_path.read_text(encoding="utf-8")
            )
            workspace_receipt["artifact_sha256"] = hashlib.sha256(
                artifact_path.read_bytes()
            ).hexdigest()
            workspace_receipt_path.write_text(
                json.dumps(workspace_receipt) + "\n", encoding="utf-8"
            )
            with self.assertRaisesRegex(
                InvalidAgentOutput, "private broker result"
            ):
                runtime._verified_skill_artifacts(
                    call.workspace,
                    "task-writing",
                    call.call_id,
                    expected_lease_epoch=1,
                    expected_launch_attempt=1,
                )

    def test_mismatched_operation_path_or_payload_is_rejected(self) -> None:
        for mismatch in ("operation", "path", "payload"):
            with self.subTest(mismatch=mismatch), tempfile.TemporaryDirectory() as directory:
                runtime, call, payload, result = self._fixture(Path(directory))
                event_payload = copy.deepcopy(payload)
                event_result = copy.deepcopy(result)
                if mismatch == "operation":
                    event_result["operation_id"] = "AR-OTHER"
                    event_result["artifact"]["operation_id"] = "AR-OTHER"
                elif mismatch == "path":
                    event_result["staged_path"] = str(
                        call.workspace.outbox_path / "task-writing" / "AR-OTHER.json"
                    )
                else:
                    event_payload["reason"] = "different submitted payload"
                self._write_tool_event(
                    runtime, payload=event_payload, result=event_result
                )
                with self.assertRaises(InvalidAgentOutput):
                    runtime._verified_skill_artifacts(
                        call.workspace,
                        "task-writing",
                        call.call_id,
                        expected_lease_epoch=1,
                        expected_launch_attempt=1,
                    )

    def test_started_or_error_tool_events_do_not_authenticate_artifacts(self) -> None:
        cases = (
            ("item.started", "in_progress", None),
            ("item.completed", "failed", {"message": "broker error"}),
        )
        for event_type, status, error in cases:
            with (
                self.subTest(event_type=event_type, status=status),
                tempfile.TemporaryDirectory() as directory,
            ):
                runtime, call, payload, result = self._fixture(
                    Path(directory), record_private=False
                )
                self._write_tool_event(
                    runtime,
                    payload=payload,
                    result=result,
                    event_type=event_type,
                    status=status,
                    error=error,
                )
                self.assertEqual(
                    runtime._verified_skill_artifacts(
                        call.workspace,
                        "task-writing",
                        call.call_id,
                        expected_lease_epoch=1,
                        expected_launch_attempt=1,
                        allow_pending=True,
                    ),
                    [],
                )
                with self.assertRaises(InvalidAgentOutput):
                    runtime._verified_skill_artifacts(
                        call.workspace,
                        "task-writing",
                        call.call_id,
                        expected_lease_epoch=1,
                        expected_launch_attempt=1,
                    )

    def test_duplicate_private_result_is_not_one_to_one(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime, call, payload, result = self._fixture(Path(directory))
            runtime._record_broker_skill_result(
                call=call,
                skill="task-writing",
                payload=payload,
                result=result,
                capability_token="private-capability-token",
            )
            self._write_tool_event(runtime, payload=payload, result=result)
            with self.assertRaises(InvalidAgentOutput):
                runtime._verified_skill_artifacts(
                    call.workspace,
                    "task-writing",
                    call.call_id,
                    expected_lease_epoch=1,
                    expected_launch_attempt=1,
                )

    def test_retry_generation_ignores_retired_workspace_receipts_after_restart(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime, call, payload, result = self._fixture(root)
            retired = root / "retired-workspace"
            call.workspace.path.rename(retired)
            replacement = WorkspaceMaterializer(root / "workspaces").create(
                call.call_id,
                root_problem="Prove ROOT.",
                policy=call.policy,
                context={"reserved_batch_id": "BATCH-1"},
            )
            result = SkillRuntime(SkillContext.load(replacement.path)).invoke(
                "task-writing", payload
            )
            retried_call = dataclasses.replace(
                call,
                workspace=replacement,
                lease_epoch=2,
                launch_attempt=2,
            )
            runtime._record_broker_skill_result(
                call=retried_call,
                skill="task-writing",
                payload=payload,
                result=result,
                capability_token="replacement-workspace-capability",
            )

            restarted = object.__new__(FrantaRuntime)
            restarted.executor = None
            restarted.transport = runtime.transport
            values = restarted._verified_skill_artifacts(
                replacement,
                "task-writing",
                call.call_id,
                expected_lease_epoch=2,
                expected_launch_attempt=2,
            )
            self.assertEqual([item["operation_id"] for item in values], ["AR-1"])

    def test_prior_lease_receipt_cannot_authenticate_same_workspace_retry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime, call, payload, result = self._fixture(Path(directory))
            self._write_tool_event(runtime, payload=payload, result=result)
            with self.assertRaisesRegex(
                InvalidAgentOutput, "private broker result"
            ):
                runtime._verified_skill_artifacts(
                    call.workspace,
                    "task-writing",
                    call.call_id,
                    expected_lease_epoch=2,
                    expected_launch_attempt=2,
                )

    def test_restart_does_not_promote_receipt_from_another_launch_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime, call, payload, result = self._fixture(Path(directory))
            self._write_tool_event(runtime, payload=payload, result=result)
            restarted = object.__new__(FrantaRuntime)
            restarted.executor = None
            restarted.transport = runtime.transport
            with self.assertRaisesRegex(
                InvalidAgentOutput, "private broker result"
            ):
                restarted._verified_skill_artifacts(
                    call.workspace,
                    "task-writing",
                    call.call_id,
                    expected_lease_epoch=1,
                    expected_launch_attempt=2,
                )

    def test_receipt_append_truncates_only_a_torn_unterminated_tail(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = object.__new__(FrantaRuntime)
            runtime.transport = types.SimpleNamespace(
                state_dir=root / "transport-state"
            )
            first = {"type": "test-receipt", "sequence": 1}
            second = {"type": "test-receipt", "sequence": 2}
            runtime._append_trusted_skill_receipt(first)
            path = runtime._trusted_skill_receipt_path()
            with path.open("ab") as handle:
                handle.write(b'{"type":"test-receipt","sequence":')
                handle.flush()
            runtime._append_trusted_skill_receipt(second)

            rows = [json.loads(line) for line in path.read_text().splitlines()]
            self.assertEqual(rows, [first, second])

    def test_optional_staging_skills_are_bound_without_new_timing_rules(self) -> None:
        for skill in ("CAS", "human-guidance", "discovery-sprint"):
            with self.subTest(skill=skill), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                call_id = "CALL-" + skill.replace("-", "_")
                policy = (
                    policy_for("worker", mode="computation")
                    if skill == "CAS"
                    else policy_for("trimmer")
                )
                workspace = WorkspaceMaterializer(root / "workspaces").create(
                    call_id,
                    root_problem="Prove ROOT.",
                    policy=policy,
                    task_card=(
                        {"task_id": "T-1", "attempt": 1, "work_mode": "computation"}
                        if skill == "CAS"
                        else None
                    ),
                    context={} if skill != "CAS" else None,
                )
                payload = {"operation_id": f"OP-{skill}", "input_marker": skill}
                staged_value = {
                    "operation_id": f"OP-{skill}",
                    "output_marker": skill,
                }
                if skill == "human-guidance":
                    pdf = workspace.artifacts_path / "guidance.pdf"
                    pdf.write_bytes(b"%PDF-1.7\n%%EOF\n")
                    staged_value["pdf_path"] = "artifacts/guidance.pdf"
                result = SkillRuntime(SkillContext.load(workspace.path))._stage(
                    skill,
                    staged_value,
                )
                runtime = object.__new__(FrantaRuntime)
                runtime.executor = None
                runtime.transport = types.SimpleNamespace(
                    state_dir=root / "transport-state"
                )
                call = AgentCall(
                    call_id=call_id,
                    kind="worker" if skill == "CAS" else "trimmer",
                    role="worker" if skill == "CAS" else "trimmer",
                    mode="computation" if skill == "CAS" else None,
                    payload={},
                    workspace=workspace,
                    policy=policy,
                    prompt="",
                    session_key=None,
                    resume=False,
                    output_schema=root / "unused.schema.json",
                    model_config=ModelConfig("gpt-6-astra", "ultra"),
                )
                runtime._record_broker_skill_result(
                    call=call,
                    skill=skill,
                    payload=payload,
                    result=result,
                    capability_token="private-optional-capability-token",
                )
                self._write_tool_event(
                    runtime,
                    payload=payload,
                    result=result,
                    call_id=call_id,
                    skill=skill,
                )
                values = runtime._verified_skill_artifacts(
                    workspace,
                    skill,
                    call_id,
                    expected_lease_epoch=1,
                    expected_launch_attempt=1,
                )
                self.assertEqual(
                    [item["operation_id"] for item in values], [f"OP-{skill}"]
                )
                if skill == "human-guidance":
                    pdf.write_bytes(b"%PDF-1.7\nchanged\n%%EOF\n")
                    with self.assertRaises(InvalidAgentOutput):
                        runtime._verified_skill_artifacts(
                            workspace,
                            skill,
                            call_id,
                            expected_lease_epoch=1,
                            expected_launch_attempt=1,
                        )


if __name__ == "__main__":
    unittest.main()
