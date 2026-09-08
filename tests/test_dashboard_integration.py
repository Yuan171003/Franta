from __future__ import annotations

import io
import json
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import time
import unittest
from unittest.mock import Mock, patch
from urllib.request import urlopen

from franta.cli import main as cli_main
from franta.config import load_manifest
from franta.dashboard_adapter import (
    FrantaOperatorCommands, FrantaReadOnlyMonitor, _resume_project,
    _descriptor, ensure_dashboard, runner_active,
)
from franta.dashboard_commands import (
    consume_advisor_commands, dashboard_directory, pending_advisor_commands, publish_json,
)
from franta.dashboard_read import FrantaDashboardRead
from franta.runtime import FrantaRuntime
from test_dashboard_monitor import summary
from test_advisor_runtime_integration import _AdvisorExecutor, _enter_franta_drain, _write_manifest


class DashboardIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.runtime = FrantaRuntime.initialize(
            load_manifest(_write_manifest(self.root, advisor=True)), executor=_AdvisorExecutor(),
        )
        self.project = self.runtime.layout.root
        self.read = FrantaDashboardRead(self.project)
        self.commands = FrantaOperatorCommands(self.project, self.read)

    def tearDown(self) -> None:
        self.runtime.close()
        self.temp.cleanup()

    def wait_for_advisor(self) -> str:
        self.runtime.start_services()
        _enter_franta_drain(self.runtime, marker="dashboard")
        self.runtime._advance_advisor()
        return self.read.overview()["advisor"]["request_id"]

    def test_operator_commands_do_not_write_research_and_runner_consumes_once(self) -> None:
        request = self.wait_for_advisor()
        response = {"choices": [{"kind": "custom", "statement": "Prove the specialization bridge."}]}
        revision = self.runtime.scheduler.revision
        result = self.commands.submit_advisor_feedback(request, response)
        self.assertEqual(result["status"], "queued")
        self.assertEqual(self.runtime.scheduler.revision, revision)
        self.assertEqual(self.commands.submit_advisor_feedback(request, response), result)
        self.assertEqual(len(pending_advisor_commands(self.project)), 1)
        with self.assertRaises(ValueError):
            self.commands.submit_advisor_feedback(request, {"choices": [{"kind": "custom", "statement": "Different"}]})
        with self.runtime.lock:
            self.assertTrue(consume_advisor_commands(self.runtime))
            self.assertFalse(consume_advisor_commands(self.runtime))
        accepted = self.commands.submit_advisor_feedback(request, response)
        self.assertEqual(accepted["status"], "accepted")
        self.runtime._advance_advisor()
        self.assertEqual(self.runtime.scheduler.alternation_phase, "explorer_admission")
        self.assertIn("specialization bridge", self.runtime.scheduler.effective_problem()["problem_text"])

    def test_guidance_endpoint_adapter_uses_existing_immutable_inbox(self) -> None:
        revision = self.runtime.scheduler.revision
        receipt = self.commands.submit_guidance("先分析特殊纤维。\n")
        self.assertEqual(self.runtime.scheduler.revision, revision)
        self.assertEqual((self.project / receipt["relative_path"]).read_text(), receipt["text"])
        self.assertEqual(self.read.overview()["guidance"][0]["status"], "pending")
        self.runtime._ingest_human_guidance()
        self.assertIn(receipt["guidance_id"], self.runtime.scheduler.state["human_guidance_inbox"])

    def test_resume_adapter_does_not_open_runtime_while_runner_lock_is_held(self) -> None:
        with self.runtime.lock, patch.object(FrantaRuntime, "open") as opening:
            self.assertTrue(runner_active(self.project))
            _resume_project(self.project)
            opening.assert_not_called()
        self.assertFalse(runner_active(self.project))

    def test_queued_feedback_is_consumed_on_resumed_advisor_boundary(self) -> None:
        request = self.wait_for_advisor()
        self.commands.submit_advisor_feedback(request, {"choices": [{"kind": "listed", "obligation_id": "ADV-1-1"}]})
        with self.runtime.lock:
            self.runtime._advance_advisor()
        self.assertEqual(self.runtime.scheduler.alternation_phase, "explorer_admission")
        self.assertFalse(pending_advisor_commands(self.project))

    def test_historical_pending_receipt_does_not_resume_another_cycle(self) -> None:
        request = self.wait_for_advisor()
        submitted = self.commands.submit_advisor_feedback(request, {"choices": [{"kind": "listed", "obligation_id": "ADV-1-1"}]})
        consume_advisor_commands(self.runtime)
        self.runtime._advance_advisor()
        (self.project / "private/dashboard/receipts" / (submitted["command_id"] + ".json")).unlink()
        with patch.object(FrantaRuntime, "run") as run:
            _resume_project(self.project)
            run.assert_not_called()
        self.assertFalse(pending_advisor_commands(self.project))

    def test_current_feedback_resumes_only_after_lock_and_acceptance(self) -> None:
        request = self.wait_for_advisor()
        self.commands.submit_advisor_feedback(request, {"choices": [{"kind": "listed", "obligation_id": "ADV-1-1"}]})
        def resumed(*, resume, _lock_held):
            self.assertTrue(resume)
            self.assertTrue(_lock_held)
            self.assertTrue(runner_active(self.project))
        with patch.object(FrantaRuntime, "run", side_effect=resumed) as run:
            _resume_project(self.project)
            run.assert_called_once()
        self.assertFalse(pending_advisor_commands(self.project))

    def test_malformed_dashboard_descriptor_is_ignored(self) -> None:
        directory = dashboard_directory(self.project)
        (directory / "server.json").write_text("[]")
        self.assertIsNone(_descriptor(self.project))

    def test_dashboard_starts_reuses_and_survives_a_stopped_research_run(self) -> None:
        directory = dashboard_directory(self.project)
        publish_json(directory / "runner.json", {"status": "stopped", "pid": os.getpid()})
        pid = None
        try:
            url = ensure_dashboard(self.project, port=1113)
            descriptor = json.loads((directory / "server.json").read_text())
            pid = descriptor["pid"]
            self.assertEqual(ensure_dashboard(self.project, port=1113), url)
            self.runtime.run(max_cycles=0)
            for route in ("/", "/main-memory", "/explorer-memory"):
                with urlopen(url + route, timeout=2) as response:
                    self.assertEqual(response.status, 200)
                    self.assertIn(b"<!", response.read())
            with urlopen(url + "/api/overview", timeout=2) as response:
                self.assertEqual(json.load(response)["project"]["name"], "advisor-runtime-integration")
            self.assertIsNone(self.read.overview()["usage"]["total_tokens"])
        finally:
            if pid:
                os.kill(pid, signal.SIGTERM)
                for _ in range(50):
                    try:
                        with urlopen(url + "/api/health", timeout=0.1):
                            pass
                    except OSError:
                        break
                    time.sleep(0.02)

    def test_cli_dashboard_does_not_construct_a_writable_runtime(self) -> None:
        with patch("franta.dashboard_adapter.ensure_dashboard", return_value="http://127.0.0.1:1113") as start:
            with patch("franta.cli._open", side_effect=AssertionError("must not open runtime")):
                output = io.StringIO()
                self.assertEqual(cli_main(["dashboard", str(self.project)], output=output), 0)
                self.assertEqual(json.loads(output.getvalue())["dashboard_url"], "http://127.0.0.1:1113")
            start.assert_called_once_with(str(self.project), port=1113)

    def test_monitor_uses_isolated_readonly_snapshot_and_no_research_call(self) -> None:
        snapshot = self.read.monitor_snapshot()
        revision = self.runtime.scheduler.revision
        result = summary()
        result["directions"] = []
        for assessment in result["quality"].values():
            assessment["source_ids"] = snapshot["source_ids"][:1]
        events = [
            {"type": "thread.started", "thread_id": "11111111-1111-4111-8111-111111111111"},
            {"type": "item.completed", "item": {"type": "agent_message", "text": json.dumps(result)}},
            {"type": "turn.completed", "usage": {"input_tokens": 120, "cached_input_tokens": 20, "output_tokens": 5}},
        ]
        process = Mock(returncode=0)
        def start(*args, **kwargs):
            kwargs["stdout"].write("\n".join(json.dumps(e) for e in events))
            return process
        with patch("franta.dashboard_adapter.subprocess.Popen", side_effect=start) as launch:
            self.assertEqual(FrantaReadOnlyMonitor(self.project).summarize(snapshot), result)
        command = launch.call_args.args[0]
        self.assertEqual(command[command.index("--sandbox") + 1], "read-only")
        self.assertIn("--ignore-user-config", command)
        self.assertNotIn("--ephemeral", command)
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", command)
        directory = launch.call_args.kwargs["cwd"]
        self.assertTrue(directory.is_relative_to(self.project / "private/dashboard/monitor-sessions"))
        self.assertTrue(list((self.project / "private/dashboard/monitor-runs").glob("*/index.json")))
        self.assertEqual(self.runtime.scheduler.revision, revision)
        self.assertFalse(self.runtime.scheduler.state["calls"])

    def test_timed_out_monitor_is_terminated_and_usage_is_retained(self) -> None:
        process = Mock(returncode=-15, pid=999999)
        process.poll.return_value = None
        process.communicate.side_effect = [
            subprocess.TimeoutExpired("codex", 1),
            ('{"type":"turn.completed","usage":{"input_tokens":10,"cached_input_tokens":0,"output_tokens":2}}', ""),
        ]
        def start(*args, **kwargs):
            kwargs["stdout"].write('{"type":"turn.completed","usage":{"input_tokens":10,"cached_input_tokens":0,"output_tokens":2}}\n')
            return process
        with patch("franta.dashboard_adapter.subprocess.Popen", side_effect=start), patch("franta.dashboard_adapter.os.killpg") as kill:
            with self.assertRaisesRegex(RuntimeError, "time limit"):
                FrantaReadOnlyMonitor(self.project, timeout_seconds=1).summarize(self.read.monitor_snapshot())
            kill.assert_called_once_with(process.pid, signal.SIGTERM)
        receipts = list((self.project / "private/dashboard/monitor-runs").glob("*/usage.json"))
        self.assertEqual(len(receipts), 1)
        receipt = json.loads(receipts[0].read_text())
        self.assertTrue(receipt["timed_out"])
        self.assertEqual(receipt["usage"][0]["input_tokens"], 10)

    def test_monitor_close_cancels_only_its_own_process_group(self) -> None:
        monitor = FrantaReadOnlyMonitor(self.project)
        process = Mock(pid=999999)
        process.poll.return_value = None
        monitor._process = process
        with patch("franta.dashboard_adapter.os.killpg") as kill:
            monitor.close()
            kill.assert_called_once_with(process.pid, signal.SIGTERM)
        with patch("franta.dashboard_adapter.subprocess.Popen") as launch:
            with self.assertRaisesRegex(RuntimeError, "stopped"):
                monitor.summarize(self.read.monitor_snapshot())
            launch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
