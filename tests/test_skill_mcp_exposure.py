from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from franta.access import (
    AccessError,
    AuditLog,
    BrokerClient,
    BrokerError,
    InMemoryBackend,
    MemoryBroker,
    policy_for,
)
from franta.config import load_manifest
from franta.materialize import WorkspaceMaterializer
from franta.prompts import prompt_for
from franta.runtime import FrantaRuntime
from franta.scheduler import SchedulerError
from franta.skill_runtime import (
    SkillContext,
    SkillRuntime,
    SkillRuntimeError,
    _constrained_command,
    _tool_definitions,
    compile_human_guidance,
    execute_cas,
)


ROOT = "Prove the stated root problem."

# Optional integration executables can be selected without editing this file.
# Values may be PATH names or executable paths.
def _integration_tool(name: str, default: str) -> str | None:
    return shutil.which(os.environ.get(f"FRANTA_TEST_{name}", default))


SAGE_EXECUTABLE = _integration_tool("SAGE", "sage")
MACAULAY2_EXECUTABLE = _integration_tool("MACAULAY2", "M2")
TECTONIC_EXECUTABLE = _integration_tool("TECTONIC", "tectonic")


class _EmptyTasks:
    def get_task_summary(self, task_id):
        return None

    def get_task_artifact(self, task_id, artifact_id):
        return None


class SkillMcpExposureTests(unittest.TestCase):
    def _workspace(self, root: Path, name: str, role: str, mode: str | None = None):
        return WorkspaceMaterializer(root / "workspaces").create(
            name,
            root_problem=ROOT,
            policy=policy_for(role, mode=mode),
        )

    def test_role_bound_staging_tools_cannot_be_widened(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            broker = MemoryBroker(InMemoryBackend(), task_backend=_EmptyTasks())
            broker.start(Path(raw) / "broker.sock")
            try:
                with self.assertRaises(AccessError):
                    broker.issue(
                        policy_for("main"),
                        caller_id="MAIN",
                        staging_handler=lambda skill, payload: {},
                        staging_skills={"human-guidance"},
                    )
                with self.assertRaises(AccessError):
                    broker.issue(
                        policy_for("worker", mode="research"),
                        caller_id="WORKER",
                        staging_handler=lambda skill, payload: {},
                        staging_skills={"discovery-sprint"},
                    )
                with self.assertRaises(AccessError):
                    broker.issue(
                        policy_for("verifier"),
                        caller_id="VERIFY",
                        staging_handler=lambda skill, payload: {},
                        staging_skills={"record-progress"},
                    )
            finally:
                broker.stop()

    def test_main_sort_uses_snapshot_instead_of_live_explorer_tools(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            broker = MemoryBroker(InMemoryBackend(), task_backend=_EmptyTasks())
            broker.start(Path(raw) / "broker.sock")
            try:
                binding = broker.issue(
                    policy_for("main-sort"),
                    caller_id="MAIN-SORT",
                    staging_handler=lambda skill, payload: {
                        "operation_id": "PRG-SORT"
                    },
                    staging_skills={"record-progress"},
                )
                self.assertEqual(
                    set(binding.enabled_tools),
                    {
                        "internal_search",
                        "memory_fetch",
                        "task_summary",
                        "task_artifact_fetch",
                        "record_progress",
                    },
                )
                self.assertNotIn("explorer_search", binding.enabled_tools)
                self.assertNotIn("explorer_fetch", binding.enabled_tools)
            finally:
                broker.stop()

    def test_unconfigured_local_tools_are_not_materialized_as_available_skills(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            manifest = root / "bootstrap.toml"
            manifest.write_text(
                "\n".join(
                    (
                        "[project]",
                        'name = "skill-availability"',
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
            runtime = FrantaRuntime.initialize(load_manifest(manifest))
            try:
                worker = runtime._make_workspace(
                    call_id="WORKER-NO-CAS",
                    policy=policy_for("worker", mode="research"),
                    task_card={"task_id": "T-1", "attempt": 1},
                )
                trimmer = runtime._make_workspace(
                    call_id="TRIM-NO-TECTONIC",
                    policy=policy_for("trimmer"),
                    context={},
                )
                self.assertFalse((worker.path / ".agents/skills/CAS").exists())
                self.assertFalse(
                    (trimmer.path / ".agents/skills/human-guidance").exists()
                )
                self.assertTrue(
                    (trimmer.path / ".agents/skills/discovery-sprint").is_dir()
                )
                worker_prompt = prompt_for(
                    "worker",
                    root_problem=ROOT,
                    mode="research",
                    policy=policy_for("worker", mode="research"),
                    available_skills={"internal-search", "record-progress"},
                )
                trimmer_prompt = prompt_for(
                    "trimmer",
                    root_problem=ROOT,
                    available_skills={"discovery-sprint", "internal-search"},
                )
                verifier_prompt = prompt_for(
                    "verifier",
                    available_skills={"internal-search"},
                )
                self.assertNotIn("`CAS`", worker_prompt)
                self.assertNotIn("human-guidance", trimmer_prompt)
                self.assertNotIn("`CAS`", verifier_prompt)
            finally:
                runtime.close()

    def test_exact_tools_are_launch_bound_and_broker_audited(self) -> None:
        staged: list[tuple[str, dict]] = []
        with tempfile.TemporaryDirectory() as raw:
            audit = AuditLog()
            broker = MemoryBroker(
                InMemoryBackend(), task_backend=_EmptyTasks(), audit=audit
            )
            broker.start(Path(raw) / "broker.sock")
            try:
                binding = broker.issue(
                    policy_for("trimmer"),
                    caller_id="TRIM",
                    staging_handler=lambda skill, payload: (
                        staged.append((skill, dict(payload)))
                        or {"operation_id": "SPRINT-1"}
                    ),
                    staging_skills={"human-guidance", "discovery-sprint"},
                )
                client = BrokerClient(binding.socket_path, binding.token)
                enabled = set(client.call("describe", {})["enabled_tools"])
                self.assertIn("human_guidance", enabled)
                self.assertIn("discovery_sprint", enabled)
                self.assertNotIn("execute_cas", enabled)
                client.call(
                    "discovery_sprint",
                    {"payload": {"decision": "no_sprint", "reason": "Not stuck."}},
                )
                self.assertEqual(staged[0][0], "discovery-sprint")
                self.assertIn(
                    "broker_skill_succeeded",
                    [event["action"] for event in audit.events],
                )
                with self.assertRaises(BrokerError):
                    client.call("execute_cas", {"software": "sage"})
            finally:
                broker.stop()

    def test_mcp_definitions_cover_all_audited_skill_tools(self) -> None:
        definitions = _tool_definitions(
            {
                "task_writing",
                "record_progress",
                "discovery_sprint",
                "human_guidance",
                "execute_cas",
            },
            configured_cas_software={"sage", "macaulay2", "custom-cas"},
        )
        names = {item["name"] for item in definitions}
        self.assertEqual(
            names,
            {
                "task_writing",
                "record_progress",
                "discovery_sprint",
                "human_guidance",
                "execute_cas",
            },
        )
        execute_cas_tool = next(
            item for item in definitions if item["name"] == "execute_cas"
        )
        self.assertEqual(
            execute_cas_tool["inputSchema"]["properties"]["software"]["enum"],
            ["custom-cas", "macaulay2", "sage"],
        )
        self.assertEqual(
            execute_cas_tool["inputSchema"]["properties"]["related_ids"],
            {
                "type": "object",
                "properties": {
                    name: {
                        "type": "array",
                        "items": {"type": "string"},
                    }
                    for name in ("fact", "route", "memo", "claim", "obligation")
                },
                "additionalProperties": False,
            },
        )
        self.assertIn("custom-cas", execute_cas_tool["description"])
        self.assertNotIn("/", execute_cas_tool["description"])

    def test_cas_choices_are_launch_bound_names_not_paths(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            broker = MemoryBroker(InMemoryBackend())
            broker.start(Path(raw) / "broker.sock")
            try:
                binding = broker.issue(
                    policy_for("verifier"),
                    caller_id="VERIFY",
                    staging_handler=lambda skill, payload: {"operation_id": "CAS-1"},
                    staging_skills={"CAS"},
                    cas_software_names={"sage", "custom-cas"},
                )
                client = BrokerClient(binding.socket_path, binding.token)
                self.assertEqual(
                    client.call("describe", {})["configured_cas_software"],
                    ["custom-cas", "sage"],
                )
                with self.assertRaises(BrokerError):
                    client.call(
                        "execute_cas",
                        {
                            "software": "macaulay2",
                            "exact_input": "",
                            "description": "Test.",
                            "assumptions": "None.",
                            "interpretation": "Test.",
                        },
                    )
                with self.assertRaises(ValueError):
                    broker.issue(
                        policy_for("verifier"),
                        caller_id="VERIFY-PATH",
                        staging_handler=lambda skill, payload: {},
                        staging_skills={"CAS"},
                        cas_software_names={"/example/cas/bin/M2"},
                    )
            finally:
                broker.stop()

    def test_verifier_cas_uses_private_configuration_without_task_card(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            workspace = self._workspace(Path(raw), "verify", "verifier")
            context = SkillContext.load(workspace.path)
            self.assertIsNone(context.task_card)

            def direct_test_run(context, executable, arguments, **kwargs):
                return subprocess.run(
                    [str(executable), *arguments],
                    input=kwargs.get("input_text"),
                    text=True,
                    capture_output=True,
                    check=False,
                    cwd=context.workspace,
                    shell=False,
                )

            with mock.patch(
                "franta.skill_runtime._run_constrained", side_effect=direct_test_run
            ):
                result = execute_cas(
                    context,
                    {
                        "software": "python",
                        "arguments": ["-c", "import sys; print(sys.stdin.read().upper())"],
                        "version_arguments": ["--version"],
                        "exact_input": "franta",
                        "description": "Upper-case one finite input.",
                        "assumptions": "No mathematical assumptions.",
                        "interpretation": "A reproducibility check only.",
                        "related_ids": {},
                    },
                    configured_executables={"python": sys.executable},
                )
            self.assertEqual(result["artifact"]["exact_output"].strip(), "FRANTA")
            self.assertEqual(result["artifact"]["skill"], "CAS")

    def test_default_sage_uses_one_batch_argument_for_trailing_compound_input(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            workspace = self._workspace(Path(raw), "sage-batch", "verifier")
            context = SkillContext.load(workspace.path)
            exact_input = "for value in [1, 2]:\n    print(value)\n"
            calls: list[dict[str, object]] = []

            def fake_run(context, executable, arguments, **kwargs):
                calls.append(
                    {
                        "arguments": list(arguments),
                        "input_text": kwargs.get("input_text"),
                    }
                )
                if list(arguments) == ["--version"]:
                    return subprocess.CompletedProcess(
                        [str(executable), *arguments], 0, "SageMath 10.test\n", ""
                    )
                return subprocess.CompletedProcess(
                    [str(executable), *arguments], 0, "1\n2\n", ""
                )

            with mock.patch(
                "franta.skill_runtime._run_constrained", side_effect=fake_run
            ):
                result = execute_cas(
                    context,
                    {
                        "software": "sage",
                        "arguments": [],
                        "version_arguments": ["--version"],
                        "exact_input": exact_input,
                        "description": "Run a trailing compound Sage statement.",
                        "assumptions": "Finite integer iteration.",
                        "interpretation": "The two loop iterations were executed.",
                        "related_ids": {},
                    },
                    configured_executables={"sage": sys.executable},
                )

            self.assertEqual(calls[0]["arguments"], ["-c", exact_input])
            self.assertIsNone(calls[0]["input_text"])
            self.assertEqual(
                json.loads(
                    result["artifact"]["environment_versions"][
                        "invocation_arguments"
                    ]
                ),
                ["-c", exact_input],
            )
            self.assertEqual(result["artifact"]["exact_output"], "1\n2\n")

    def test_nonzero_cas_is_retained_and_marked_as_execution_failure(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            workspace = self._workspace(Path(raw), "cas-failure", "verifier")
            context = SkillContext.load(workspace.path)
            calls = 0

            def fake_run(context, executable, arguments, **kwargs):
                nonlocal calls
                calls += 1
                if calls == 1:
                    return subprocess.CompletedProcess(
                        [str(executable), *arguments],
                        7,
                        "partial output\n",
                        "deliberate failure\n",
                    )
                return subprocess.CompletedProcess(
                    [str(executable), *arguments], 0, "CAS 1.test\n", ""
                )

            with mock.patch(
                "franta.skill_runtime._run_constrained", side_effect=fake_run
            ):
                result = execute_cas(
                    context,
                    {
                        "operation_id": "CAS-FAILED-1",
                        "software": "test-cas",
                        "arguments": ["--run"],
                        "version_arguments": ["--version"],
                        "exact_input": "fail()\n",
                        "description": "Exercise a failing CAS invocation.",
                        "assumptions": "None.",
                        "interpretation": "The invocation failed and proves nothing.",
                        "related_ids": {},
                    },
                    configured_executables={"test-cas": sys.executable},
                )

            self.assertFalse(result["execution_succeeded"])
            retained = json.loads(
                (
                    workspace.outbox_path
                    / "CAS"
                    / "CAS-FAILED-1.json"
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(retained["exit_status"], 7)
            self.assertEqual(retained["exact_output"], "partial output\n")
            self.assertEqual(retained["error_output"], "deliberate failure\n")

    def test_task_card_cannot_supply_a_cas_executable_or_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            workspace = WorkspaceMaterializer(Path(raw) / "workspaces").create(
                "untrusted-cas-card",
                root_problem=ROOT,
                policy=policy_for("worker", mode="computation"),
                task_card={
                    "task_id": "T-1",
                    "attempt": 1,
                    "cas_executables": {"python": sys.executable},
                    "cas_timeout_seconds": 99,
                },
            )
            with self.assertRaisesRegex(
                SkillRuntimeError, "not configured for this call"
            ):
                execute_cas(
                    SkillContext.load(workspace.path),
                    {
                        "software": "python",
                        "arguments": ["--version"],
                        "exact_input": "",
                        "description": "Untrusted task-card configuration probe.",
                        "assumptions": "None.",
                        "interpretation": "The call must not execute.",
                    },
                )

    def test_cas_broker_rejects_executable_injection(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            broker = MemoryBroker(InMemoryBackend())
            broker.start(Path(raw) / "broker.sock")
            try:
                binding = broker.issue(
                    policy_for("verifier"),
                    caller_id="VERIFY",
                    staging_handler=lambda skill, payload: {"operation_id": "CAS-1"},
                    staging_skills={"CAS"},
                    cas_software_names={"sage"},
                )
                client = BrokerClient(binding.socket_path, binding.token)
                with self.assertRaises(BrokerError):
                    client.call(
                        "execute_cas",
                        {
                            "software": "sage",
                            "executable": "/bin/sh",
                            "exact_input": "",
                        },
                    )
            finally:
                broker.stop()

    def test_human_guidance_compiles_with_fixed_safe_tectonic_arguments(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            workspace = self._workspace(Path(raw), "trim", "trimmer")
            source = workspace.artifacts_path / "report.tex"
            source.write_text("\\documentclass{article}\\begin{document}x\\end{document}\n")

            def fake_run(command, **kwargs):
                self.assertIsInstance(command, list)
                self.assertEqual(command[0], "/test-tools/bwrap")
                self.assertIn("--unshare-all", command)
                self.assertFalse(kwargs["shell"])
                self.assertNotIn("HTTP_PROXY", kwargs["env"])
                if "--outdir" in command:
                    self.assertIn("--only-cached", command)
                    self.assertIn("--untrusted", command)
                    output = Path(command[command.index("--outdir") + 1]) / "report.pdf"
                    output.write_bytes(b"%PDF-1.7\n%%EOF\n")
                    return subprocess.CompletedProcess(command, 0, "compiled", "")
                return subprocess.CompletedProcess(command, 0, "Tectonic 0.test", "")

            # Exercise the real command builder without requiring a sandbox
            # executable on the machine running this mocked compiler test.
            with (
                mock.patch("franta.skill_runtime.sys.platform", "linux"),
                mock.patch(
                    "franta.skill_runtime.shutil.which", return_value="/test-tools/bwrap"
                ),
                mock.patch("franta.skill_runtime.subprocess.run", side_effect=fake_run),
            ):
                result = compile_human_guidance(
                    SkillContext.load(workspace.path),
                    {
                        "latex_path": "artifacts/report.tex",
                        "question": "Which route should be pursued?",
                    },
                    tectonic_executable=sys.executable,
                )
            artifact = result["artifact"]
            self.assertEqual(artifact["pdf_path"], "artifacts/report.pdf")
            self.assertTrue((workspace.artifacts_path / "report.pdf").is_file())
            self.assertEqual(artifact["compiler"], "tectonic")

    def test_human_guidance_refuses_nontrimmer(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            workspace = self._workspace(Path(raw), "worker", "worker", "research")
            with self.assertRaises(SkillRuntimeError):
                compile_human_guidance(
                    SkillContext.load(workspace.path),
                    {"latex_path": "artifacts/report.tex", "question": "Question?"},
                    tectonic_executable=sys.executable,
                )

    def test_discovery_sprint_preserves_submitted_target_revision(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            manifest = root / "bootstrap.toml"
            manifest.write_text(
                "\n".join(
                    (
                        "[project]",
                        'name = "sprint-target-revision"',
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
            runtime = FrantaRuntime.initialize(load_manifest(manifest))
            try:
                runtime.scheduler.commit_initial_trim({"category_ids": []})
                runtime.scheduler.submit_stuck_report(
                    "STUCK-REVISION", {"summary": "One mechanism repeats."}
                )
                runtime.scheduler.apply_trim_review_decision("trim", "Run a sprint.")
                root_id = runtime.scheduler.state["root"]["obligation_id"]
                root_record = runtime.store.get(root_id).to_dict()
                distant_ids: list[str] = []
                for index in (1, 2):
                    result = runtime.store.add_memo(
                        f"sprint-target-revision-memo-{index}",
                        {
                            "abstract": f"Distant prompt {index}",
                            "genre": "normal",
                            "content": f"Remote bridge material {index}.",
                            "related_route_ids": [],
                        },
                    )
                    assert result.canonical_id is not None
                    distant_ids.append(result.canonical_id)
                empty = {
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
                lanes = [
                    {
                        "mode": "brainstorm",
                        "objective": "Find a clean-room proof.",
                        "main_obligation_ids": [root_id],
                        "assignment_portfolio": empty,
                        "reason": "Supply only the target.",
                    },
                    {
                        "mode": "multi-discipline",
                        "objective": "Translate the target.",
                        "main_obligation_ids": [root_id],
                        "selected_new_perspective": "categorical",
                        "assignment_portfolio": empty,
                        "reason": "Supply one new perspective.",
                    },
                    {
                        "mode": "computation",
                        "objective": "Test boundary cases.",
                        "main_obligation_ids": [root_id],
                        "computation_portfolio": ["Enumerate the smallest cases."],
                        "assignment_portfolio": empty,
                        "reason": "Supply one explicit experiment.",
                    },
                    {
                        "mode": "associate",
                        "objective": "Seek a remote bridge.",
                        "main_obligation_ids": [root_id],
                        "assignment_portfolio": {**empty, "memo": distant_ids},
                        "reason": "Supply exactly two distant memos.",
                    },
                ]
                call_id = runtime.scheduler.prepare_call(
                    "trimmer",
                    {"phase": "maintain"},
                    continuation={"phase": "maintain", "session_id": "TRIM-REVISION"},
                )
                workspace = runtime._make_workspace(
                    call_id=call_id,
                    policy=policy_for("trimmer"),
                    context={"phase": "maintain"},
                )
                payload = {
                        "operation_id": "S-STALE-REVISION",
                        "decision": "plan",
                        "target_obligation_id": root_id,
                        "target_obligation_revision": int(root_record["revision"]) + 1,
                        "target_statement": root_record["statement"],
                        "repeated_mechanism": "One reduction repeats.",
                        "unchanged_obstacle": "The boundary remains.",
                        "lanes": lanes,
                    }
                lease, _ = runtime.scheduler.mark_call_running(call_id)
                call = runtime._call_spec(
                    call_id,
                    workspace=workspace,
                    policy=policy_for("trimmer"),
                    session_key="trimmer:TRIM-REVISION",
                )
                staged = SkillRuntime(SkillContext.load(workspace.path)).invoke(
                    "discovery-sprint", payload
                )
                runtime._record_broker_skill_result(
                    call=call,
                    skill="discovery-sprint",
                    payload=payload,
                    result=staged,
                    capability_token="test-capability",
                )
                runtime.scheduler.accept_call_result(
                    call_id, lease, {"decision": "discovery_sprint"}
                )
                with self.assertRaisesRegex(
                    SchedulerError, "stale sprint target revision"
                ):
                    runtime._commit_trimmer_result(call_id, workspace)
                self.assertIsNone(runtime.scheduler.state["active_sprint_id"])
            finally:
                runtime.close()

    def test_human_guidance_commit_persists_a_call_bound_report_reference(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            manifest = root / "bootstrap.toml"
            manifest.write_text(
                "\n".join(
                    (
                        "[project]",
                        'name = "guidance-persistence"',
                        'directory = "project"',
                        'root_problem = "Prove ROOT."',
                        'foundation_policy = "Use the stated axioms."',
                        "",
                        "[context_budgets]",
                        "main = 1000",
                        "",
                        "[tools]",
                        f'tectonic = "{sys.executable}"',
                        "",
                        "[initial]",
                    )
                )
                + "\n",
                encoding="utf-8",
            )
            runtime = FrantaRuntime.initialize(load_manifest(manifest))
            try:
                call_id = runtime.scheduler.prepare_call(
                    "trimmer",
                    {"phase": "maintain"},
                    continuation={"phase": "maintain", "session_id": "TRIM-1"},
                )
                workspace = runtime._make_workspace(
                    call_id=call_id,
                    policy=policy_for("trimmer"),
                    context={"phase": "maintain"},
                )
                report = workspace.artifacts_path / "report.pdf"
                report.write_bytes(b"%PDF-1.7\nDurable report\n%%EOF\n")
                SkillRuntime(SkillContext.load(workspace.path)).invoke(
                    "human-guidance",
                    {
                        "operation_id": "HG-STAGE-1",
                        "request_id": "HG-1",
                        "pdf_path": "artifacts/report.pdf",
                        "question": "Which route should continue?",
                    },
                )
                runtime.executor = lambda call: {}
                lease, _ = runtime.scheduler.mark_call_running(call_id)
                runtime.scheduler.accept_call_result(
                    call_id, lease, {"decision": "human_guidance"}
                )
                runtime._commit_trimmer_result(call_id, workspace)

                active = runtime.scheduler.state["guidance"]["active"]
                report_ref = active["input"]["report_ref"]
                self.assertTrue(
                    report_ref.startswith(f"private/human-guidance/{call_id}/")
                )
                archived = runtime.layout.root / report_ref
                self.assertEqual(archived.read_bytes(), report.read_bytes())
                self.assertNotEqual(archived.resolve(), report.resolve())
            finally:
                runtime.close()

    def test_unsandboxed_macos_launch_uses_network_denial_profile(self) -> None:
        if sys.platform != "darwin":
            self.skipTest("macOS-specific constrained-launch assertion")
        with mock.patch.dict(
            os.environ,
            {"CODEX_SANDBOX": "", "CODEX_SANDBOX_NETWORK_DISABLED": ""},
        ):
            command = _constrained_command(
                Path(sys.executable).resolve(),
                ["--version"],
                workspace=Path("/private/call-workspace"),
                denied_paths=[Path("/private/project-memory")],
            )
        self.assertEqual(command[0], "/usr/bin/sandbox-exec")
        self.assertIn("(allow default)", command[2])
        self.assertIn("deny file-read-data (require-not", command[2])
        self.assertIn("deny file-read-metadata (require-not", command[2])
        self.assertIn("deny file-write* (require-not", command[2])
        self.assertIn("(deny network*)", command[2])
        self.assertIn("/private/call-workspace", command[2])
        self.assertIn("/private/project-memory", command[2])

    def test_linux_without_bubblewrap_fails_closed(self) -> None:
        with (
            mock.patch("franta.skill_runtime.sys.platform", "linux"),
            mock.patch("franta.skill_runtime.shutil.which", return_value=None),
        ):
            with self.assertRaisesRegex(
                SkillRuntimeError, "confinement is unavailable"
            ):
                _constrained_command(
                    Path(sys.executable).resolve(),
                    ["--version"],
                    workspace=Path("/tmp/call-workspace"),
                )

    @unittest.skipUnless(
        os.environ.get("FRANTA_RUN_CONFINEMENT_INTEGRATION") == "1",
        "requires an unsandboxed parent and an available OS confinement backend",
    )
    def test_real_cas_child_cannot_read_outside_workspace_or_open_network(self) -> None:
        with (
            tempfile.TemporaryDirectory() as raw,
            socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener,
        ):
            listener.bind(("127.0.0.1", 0))
            listener.listen(2)
            address = listener.getsockname()
            # Prove this host endpoint is reachable before checking that the
            # confined child cannot reach it. UDP connect alone sends no data
            # and can succeed inside an isolated Linux network namespace.
            with socket.create_connection(address, timeout=2):
                with listener.accept()[0]:
                    pass
            root = Path(raw)
            secret = root / "scheduler-secret.txt"
            secret.write_text("must-not-leak", encoding="utf-8")
            workspace = self._workspace(root, "confined", "verifier")
            program = "\n".join(
                [
                    "import pathlib, socket",
                    f"p = pathlib.Path({str(secret)!r})",
                    "try:",
                    "    p.read_text()",
                    "    print('filesystem=leaked')",
                    "except OSError:",
                    "    print('filesystem=blocked')",
                    "try:",
                    f"    with socket.create_connection({address!r}, timeout=2):",
                    "        print('network=leaked')",
                    "except OSError:",
                    "    print('network=blocked')",
                ]
            )
            result = execute_cas(
                SkillContext.load(workspace.path),
                {
                    "software": "python",
                    "arguments": ["-c", program],
                    "version_arguments": ["--version"],
                    "exact_input": "",
                    "description": "Probe call-workspace and network confinement.",
                    "assumptions": "No mathematical assumptions.",
                    "interpretation": "Both prohibited capabilities must be blocked.",
                    "related_ids": {},
                },
                configured_executables={"python": sys.executable},
            )
            artifact = result["artifact"]
            self.assertEqual(artifact["exit_status"], 0, artifact["error_output"])
            output = artifact["exact_output"]
            self.assertIn("filesystem=blocked", output)
            self.assertIn("network=blocked", output)
            self.assertNotIn("leaked", output)

    @unittest.skipUnless(
        os.environ.get("FRANTA_RUN_CONFINEMENT_INTEGRATION") == "1"
        and TECTONIC_EXECUTABLE is not None,
        "requires configured Tectonic and an unsandboxed parent",
    )
    def test_real_tectonic_compiles_in_the_confined_handler(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            workspace = self._workspace(root, "tectonic", "trimmer")
            (workspace.artifacts_path / "guidance.tex").write_text(
                "\\documentclass{article}\n"
                "\\begin{document}Choose a route.\\end{document}\n",
                encoding="utf-8",
            )
            result = compile_human_guidance(
                SkillContext.load(workspace.path),
                {
                    "latex_path": "artifacts/guidance.tex",
                    "question": "Which route should continue?",
                },
                tectonic_executable=TECTONIC_EXECUTABLE,
            )
            artifact = result["artifact"]
            self.assertEqual(artifact["pdf_path"], "artifacts/guidance.pdf")
            self.assertTrue((workspace.artifacts_path / "guidance.pdf").is_file())
            self.assertTrue(artifact["compiler_version"])

            secret = root / "operator-secret.tex"
            secret.write_text("SHOULD-NOT-BE-TYPESET", encoding="utf-8")
            (workspace.artifacts_path / "blocked.tex").write_text(
                "\\documentclass{article}\n"
                "\\begin{document}"
                f"\\input{{{secret}}}"
                "\\end{document}\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                SkillRuntimeError, "did not compile successfully"
            ):
                compile_human_guidance(
                    SkillContext.load(workspace.path),
                    {
                        "latex_path": "artifacts/blocked.tex",
                        "question": "This malicious report must be rejected.",
                    },
                    tectonic_executable=TECTONIC_EXECUTABLE,
                )
            self.assertFalse((workspace.artifacts_path / "blocked.pdf").exists())

    @unittest.skipUnless(
        os.environ.get("FRANTA_RUN_CONFINEMENT_INTEGRATION") == "1"
        and SAGE_EXECUTABLE is not None,
        "requires configured Sage and an unsandboxed parent",
    )
    def test_real_default_sage_runs_batch_input_and_reports_exceptions(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            workspace = self._workspace(Path(raw), "real-sage-batch", "verifier")
            context = SkillContext.load(workspace.path)
            executable = SAGE_EXECUTABLE

            completed = execute_cas(
                context,
                {
                    "operation_id": "CAS-SAGE-BATCH-SUCCESS",
                    "software": "sage",
                    "arguments": [],
                    "version_arguments": ["--version"],
                    "exact_input": "for value in [1, 2]:\n    print(value)\n",
                    "description": "Execute a trailing compound statement in batch mode.",
                    "assumptions": "Finite integer iteration.",
                    "interpretation": "Both loop iterations must execute.",
                    "related_ids": {},
                },
                configured_executables={"sage": executable},
            )
            self.assertTrue(completed["execution_succeeded"])
            self.assertEqual(completed["artifact"]["exact_output"].strip(), "1\n2")
            self.assertNotIn(
                "SageMath version", completed["artifact"]["exact_output"]
            )

            failed = execute_cas(
                context,
                {
                    "operation_id": "CAS-SAGE-BATCH-FAILURE",
                    "software": "sage",
                    "arguments": [],
                    "version_arguments": ["--version"],
                    "exact_input": "raise RuntimeError('deliberate-cas-test')\n",
                    "description": "Exercise batch-mode exception reporting.",
                    "assumptions": "None.",
                    "interpretation": "The exception must yield a nonzero exit status.",
                    "related_ids": {},
                },
                configured_executables={"sage": executable},
            )
            self.assertFalse(failed["execution_succeeded"])
            self.assertNotEqual(failed["artifact"]["exit_status"], 0)
            self.assertIn("deliberate-cas-test", failed["artifact"]["error_output"])
            self.assertNotIn("SageMath version", failed["artifact"]["exact_output"])

    @unittest.skipUnless(
        os.environ.get("FRANTA_RUN_CONFINEMENT_INTEGRATION") == "1"
        and SAGE_EXECUTABLE is not None
        and MACAULAY2_EXECUTABLE is not None,
        "requires configured Sage and Macaulay2 with an unsandboxed parent",
    )
    def test_real_configured_sage_and_macaulay2_run_confined(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            workspace = self._workspace(Path(raw), "real-cas", "verifier")
            context = SkillContext.load(workspace.path)
            cases = [
                (
                    "sage",
                    SAGE_EXECUTABLE,
                    [],
                    "for value in [2]:\n    print(value + 2)\n",
                ),
                (
                    "macaulay2",
                    MACAULAY2_EXECUTABLE,
                    [
                        "--silent",
                        "--stop",
                        "--no-prompts",
                        "--no-readline",
                        "--no-tty",
                        "-q",
                        "-e",
                        "print(2 + 2)",
                    ],
                    "print(2 + 2)",
                ),
            ]
            for software, executable, arguments, exact_input in cases:
                with self.subTest(software=software):
                    result = execute_cas(
                        context,
                        {
                            "software": software,
                            "arguments": arguments,
                            "version_arguments": ["--version"],
                            "exact_input": exact_input,
                            "description": "Compute the integer sum two plus two.",
                            "assumptions": "Integer arithmetic.",
                            "interpretation": "The configured CAS returns four.",
                            "related_ids": {},
                        },
                        configured_executables={software: executable},
                    )
                    artifact = result["artifact"]
                    self.assertEqual(
                        artifact["exit_status"], 0, artifact["error_output"]
                    )
                    self.assertIn("4", artifact["exact_output"])

    def test_skill_files_use_tools_not_shell_entrypoints(self) -> None:
        skills = Path(__file__).resolve().parents[1] / ".agents" / "skills"
        expected = {
            "discovery-sprint": "`discovery_sprint`",
            "human-guidance": "`human_guidance`",
            "CAS": "`execute_cas`",
        }
        for name, tool in expected.items():
            text = (skills / name / "SKILL.md").read_text(encoding="utf-8")
            self.assertIn(tool, text)
            self.assertNotIn("python3 -m franta.skill_runtime", text)


if __name__ == "__main__":
    unittest.main()
