from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC = PROJECT_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from franta.access import CodexPermissionProfile, policy_for  # noqa: E402
from franta.transport import (  # noqa: E402
    CodexRequest,
    CodexTransport,
    CodexTransportError,
)


def _workspace(base: Path) -> Path:
    workspace = base / "workspace"
    for relative in ("outbox", "artifacts", "tmp"):
        (workspace / relative).mkdir(parents=True, exist_ok=True)
    return workspace


def _request(base: Path, call_id: str) -> CodexRequest:
    workspace = _workspace(base)
    policy = policy_for("worker", mode="research")
    profile = CodexPermissionProfile.for_policy(
        policy,
        canonical_path=base / "canonical.sqlite",
        private_paths=(base / "private",),
    )
    return CodexRequest(
        call_id=call_id,
        role="worker",
        prompt="work",
        workspace=workspace,
        policy=policy,
        permission_profile=profile,
        session_key=f"session-{call_id}",
    )


def _python_factory(script: str, calls: list[dict[str, object]]):
    def launch(command, **kwargs):
        calls.append({"command": command, **kwargs})
        return subprocess.Popen([sys.executable, "-u", "-c", script], **kwargs)

    return launch


class StreamingTransportTests(unittest.TestCase):
    def test_events_are_durable_before_exit_and_call_is_cancellable(self) -> None:
        script = "\n".join(
            (
                "import json, time",
                "print(json.dumps({'type':'thread.started','thread_id':'stream-thread'}), flush=True)",
                "time.sleep(0.1)",
                "print(json.dumps({'type':'item.completed','item':{'id':'tool-1','type':'mcp_tool_call','server':'franta','tool':'record_progress','status':'completed'}}), flush=True)",
                "time.sleep(30)",
            )
        )
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            calls: list[dict[str, object]] = []
            transport = CodexTransport(
                base / "state",
                runner=_python_factory(script, calls),
                source_root=SRC,
                host_codex_home=base / "host-codex-home",
                termination_grace_seconds=0.2,
            )
            request = _request(base, "stream-call")
            failures: list[BaseException] = []

            def invoke() -> None:
                try:
                    transport.invoke(request)
                except BaseException as exc:  # captured for the test thread
                    failures.append(exc)

            thread = threading.Thread(target=invoke)
            thread.start()
            audit = base / "state/calls/stream-call.jsonl"
            try:
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    if transport.successful_skill_invocation_count(
                        "stream-call", "record-progress"
                    ):
                        break
                    if not thread.is_alive():
                        break
                    time.sleep(0.02)
                self.assertTrue(thread.is_alive(), "process exited before streaming was observed")
                self.assertEqual(
                    transport.successful_skill_invocation_count(
                        "stream-call", "record-progress"
                    ),
                    1,
                )
                self.assertIn("stream-thread", audit.read_text(encoding="utf-8"))
                self.assertTrue(transport.cancel("stream-call", reason="obsolete task"))
            finally:
                # A failed assertion must not leave the sleeping child and its
                # reader thread alive for the remaining test suite.
                transport.cancel("stream-call", reason="streaming test cleanup")
                thread.join(timeout=5)
            self.assertFalse(thread.is_alive())
            self.assertEqual(len(failures), 1)
            self.assertIsInstance(failures[0], CodexTransportError)
            self.assertIn("obsolete task", str(failures[0]))
            records = [json.loads(line) for line in audit.read_text().splitlines()]
            self.assertIn("transport.process_started", {item["type"] for item in records})
            self.assertIn("transport.cancel_requested", {item["type"] for item in records})
            self.assertEqual(records[-1]["type"], "transport.call_ended")
            self.assertEqual(records[-1]["cancel_kind"], "scheduler")
            self.assertTrue(calls[0]["start_new_session"])

    def test_default_timeout_is_persisted_and_terminates_process(self) -> None:
        script = "\n".join(
            (
                "import json, time",
                "print(json.dumps({'type':'thread.started','thread_id':'timeout-thread'}), flush=True)",
                "time.sleep(30)",
            )
        )
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            transport = CodexTransport(
                base / "state",
                runner=_python_factory(script, []),
                source_root=SRC,
                host_codex_home=base / "host-codex-home",
                default_timeout_seconds=0.15,
                termination_grace_seconds=0.1,
            )
            with self.assertRaisesRegex(CodexTransportError, "configured timeout"):
                transport.invoke(_request(base, "timeout-call"))
            records = [
                json.loads(line)
                for line in (base / "state/calls/timeout-call.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            started = next(
                item for item in records if item["type"] == "transport.process_started"
            )
            self.assertEqual(started["timeout_seconds"], 0.15)
            self.assertIsNotNone(started["deadline_at"])
            self.assertEqual(records[-1]["type"], "transport.call_ended")
            self.assertEqual(records[-1]["cancel_kind"], "timeout")


if __name__ == "__main__":
    unittest.main()
