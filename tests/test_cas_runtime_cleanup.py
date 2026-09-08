from __future__ import annotations

import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from franta.contracts.agent_access import policy_for
from franta.execution_gateway.broker import BrokerBinding, BrokerError, MemoryBroker
from franta.execution_gateway.permissions import CodexPermissionProfile
from franta.execution_gateway.skills import SkillRuntimeError
from franta.execution_gateway.transport import (
    CodexRequest,
    CodexResult,
    CodexTransport,
    CodexTransportError,
)
from franta.project import ProjectLayout
from franta.prompts import model_config
from franta.read_access.materialization import WorkspaceMaterializer
from franta.read_access.memory import InMemoryBackend
from franta.runtime import AgentCall, FrantaRuntime, InvalidAgentOutput


class CASRuntimeCleanupTests(unittest.TestCase):
    def test_agent_exit_reaps_cas_before_returning_and_revokes_capability(self) -> None:
        for outcome in ("success", "cancelled", "exception", "invalid-result"):
            with self.subTest(outcome=outcome), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                policy = policy_for("verifier")
                layout = ProjectLayout.at(root)
                workspace = WorkspaceMaterializer(layout.workspaces).create(
                    "CALL-CAS-CLEANUP", root_problem="Test CAS ownership.", policy=policy
                )
                call = AgentCall(
                    call_id="CALL-CAS-CLEANUP",
                    kind="verifier",
                    role="verifier",
                    mode=None,
                    payload={},
                    workspace=workspace,
                    policy=policy,
                    prompt="",
                    session_key=None,
                    resume=False,
                    output_schema=root / "unused.schema.json",
                    model_config=model_config("verifier"),
                )
                broker = MemoryBroker(InMemoryBackend())
                broker.start(root / "broker.sock")
                runtime = object.__new__(FrantaRuntime)
                runtime.layout = layout
                runtime.config = {"tools": {"sage": sys.executable}}
                runtime.executor = None
                runtime.broker = broker
                started = threading.Event()
                processes: list[subprocess.Popen[str]] = []
                threads: list[threading.Thread] = []
                bindings: list[BrokerBinding] = []
                failures: list[BaseException] = []
                real_popen = subprocess.Popen

                def observe_launch(*args, **kwargs):
                    process = real_popen(*args, **kwargs)
                    processes.append(process)
                    started.set()
                    return process

                def invoke(request: CodexRequest) -> CodexResult:
                    binding = request.broker_binding
                    self.assertIsNotNone(binding)
                    bindings.append(binding)

                    def compute() -> None:
                        try:
                            broker.dispatch(
                                {
                                    "token": binding.token,
                                    "operation": "execute_cas",
                                    "arguments": {
                                        "software": "sage",
                                        "exact_input": "import time; time.sleep(15)",
                                    },
                                }
                            )
                        except BaseException as exc:
                            failures.append(exc)

                    thread = threading.Thread(target=compute, daemon=True)
                    threads.append(thread)
                    thread.start()
                    self.assertTrue(started.wait(5), f"CAS did not start: {failures}")
                    self.assertIsNone(processes[0].poll())
                    if outcome == "cancelled":
                        raise CodexTransportError("agent cancelled")
                    if outcome == "exception":
                        raise RuntimeError("unexpected transport failure")
                    return CodexResult(
                        call_id=request.call_id,
                        thread_id=None,
                        resumed=False,
                        returncode=0,
                        final_message="{}" if outcome == "success" else "invalid JSON",
                    )

                runtime.transport = SimpleNamespace(invoke=invoke)
                try:
                    # Only the OS confinement wrapper is bypassed. Execution,
                    # ownership, broker dispatch and the runtime finally are real.
                    with (
                        mock.patch(
                            "franta.execution_gateway.skills._constrained_command",
                            side_effect=lambda executable, arguments, **kwargs: [
                                str(executable), *arguments
                            ],
                        ),
                        mock.patch(
                            "franta.execution_gateway.cas_process.subprocess.Popen",
                            side_effect=observe_launch,
                        ),
                    ):
                        if outcome == "success":
                            self.assertEqual(runtime._invoke_agent(call), {})
                        else:
                            expected = {
                                "cancelled": CodexTransportError,
                                "exception": RuntimeError,
                                "invalid-result": InvalidAgentOutput,
                            }[outcome]
                            with self.assertRaises(expected):
                                runtime._invoke_agent(call)

                        # Check before joining the request thread: cleanup must
                        # have completed when the owning agent invocation ends.
                        self.assertIsNotNone(processes[0].poll())
                        with self.assertRaisesRegex(BrokerError, "revoked"):
                            broker.dispatch(
                                {
                                    "token": bindings[0].token,
                                    "operation": "describe",
                                    "arguments": {},
                                }
                            )
                        threads[0].join(timeout=5)
                        self.assertFalse(threads[0].is_alive())
                        self.assertEqual(len(processes), 1)
                        self.assertEqual(len(failures), 1)
                        self.assertIsInstance(failures[0], SkillRuntimeError)
                        self.assertIn("cancelled", str(failures[0]))
                finally:
                    # A regression must not leave the test's own sleeper behind.
                    for process in processes:
                        if process.poll() is None:
                            process.kill()
                        process.wait(timeout=5)
                    for thread in threads:
                        thread.join(timeout=5)
                    broker.stop()

    def test_cas_launches_allow_300_seconds_including_resumed_agents(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            policy = policy_for("verifier")
            profile = CodexPermissionProfile.for_policy(
                policy, canonical_path=root / "canonical", private_paths=()
            )
            transport = CodexTransport(
                root / "transport", host_codex_home=root / "empty-codex-home"
            )
            for tools in (None, ("internal_search",), ("execute_cas",)):
                for thread_id in (None, "resumed-agent"):
                    with self.subTest(tools=tools, thread_id=thread_id):
                        binding = (
                            BrokerBinding(str(root / "broker.sock"), "test-token", tools)
                            if tools is not None
                            else None
                        )
                        request = CodexRequest(
                            call_id="CALL-CAS-TIMEOUT",
                            role="verifier",
                            prompt="",
                            workspace=root,
                            policy=policy,
                            permission_profile=profile,
                            broker_binding=binding,
                        )
                        command = transport.build_command(request, thread_id=thread_id)
                        timeouts = [
                            arg for arg in command
                            if arg.startswith("mcp_servers.franta.tool_timeout_sec=")
                        ]
                        self.assertEqual(
                            timeouts,
                            ["mcp_servers.franta.tool_timeout_sec=300"]
                            if tools == ("execute_cas",)
                            else [],
                        )


if __name__ == "__main__":
    unittest.main()
