from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import unittest
from pathlib import Path
from unittest import mock

from franta.access import InMemoryBackend, policy_for
from franta.execution_gateway import broker as broker_module
from franta.execution_gateway import cas_process, skills
from franta.execution_gateway.broker import BrokerClient, BrokerError, MemoryBroker
from franta.execution_gateway.cas_process import CASCancelled, CASProcessScope


class _ThreadResult:
    def __init__(self, target):
        self.value = None
        self.error = None
        self.traceback = ""
        self.done = threading.Event()

        def run():
            try:
                self.value = target()
            except BaseException as exc:
                self.error = exc
                self.traceback = traceback.format_exc()
            finally:
                self.done.set()

        self.thread = threading.Thread(target=run, daemon=True)
        self.thread.start()


@unittest.skipUnless(os.name == "posix", "CAS process groups require POSIX")
class CASProcessLifecycleTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="franta-cas-lifecycle-")
        self.root = Path(temporary.name)
        self.addCleanup(temporary.cleanup)
        self.scopes = []
        self.threads = []
        self.processes = []
        self.child_pids = set()
        original_popen = subprocess.Popen

        def launch(*args, **kwargs):
            process = original_popen(*args, **kwargs)
            self.processes.append(process)
            return process

        self.launch = launch
        patcher = mock.patch.object(cas_process.subprocess, "Popen", side_effect=launch)
        self.popen = patcher.start()
        self.addCleanup(patcher.stop)
        # Every test subprocess also has a short, independent natural exit.
        # Exact private process groups are the final fallback if an assertion
        # exposes a cancellation regression.
        self.addCleanup(self._cleanup_processes)

    def _cleanup_processes(self):
        errors = []
        for scope in self.scopes:
            try:
                scope.close()
            except Exception as exc:
                errors.append(exc)
        for process in self.processes:
            if process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            process.wait(timeout=2)
        for pid in self.child_pids:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        for result in self.threads:
            result.thread.join(timeout=3)
            self.assertFalse(result.thread.is_alive(), "CAS test thread survived cleanup")
        if errors:
            raise errors[0]

    def _scope(self):
        scope = CASProcessScope()
        self.scopes.append(scope)
        return scope

    def _thread(self, target):
        result = _ThreadResult(target)
        self.threads.append(result)
        return result

    def _run(self, scope, code, *, deadline=None, input_text=None):
        return scope.run(
            [sys.executable, "-c", code],
            input_text=input_text,
            cwd=self.root,
            env=os.environ.copy(),
            deadline=time.monotonic() + 8 if deadline is None else deadline,
        )

    def _wait_file(self, name, *, timeout=3):
        path = self.root / name
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if path.exists() and path.read_text():
                value = path.read_text()
                if name == "child.ready":
                    self.child_pids.add(int(value))
                return value
            time.sleep(0.01)
        self.fail(f"test subprocess did not become ready: {name}")

    def _assert_dead(self, pid):
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return
            # Minimal Linux containers may not reap adopted zombies promptly;
            # they cannot execute or retain CPU resources.
            status = Path(f"/proc/{pid}/stat")
            try:
                if status.read_text().rsplit(")", 1)[1].split()[0] == "Z":
                    return
            except FileNotFoundError:
                pass
            time.sleep(0.01)
        self.fail(f"test subprocess {pid} is still alive")

    @staticmethod
    def _sleeping_code(ready):
        return (
            "import os, pathlib, time\n"
            f"pathlib.Path({ready!r}).write_text(str(os.getpid()))\n"
            "time.sleep(8)\n"
        )

    @staticmethod
    def _tree_code(*, parent_exits):
        child = (
            "import os, pathlib, signal, time\n"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            "pathlib.Path('child.ready').write_text(str(os.getpid()))\n"
            "time.sleep(8)\n"
        )
        return (
            "import os, pathlib, subprocess, sys, time\n"
            f"subprocess.Popen([sys.executable, '-c', {child!r}], "
            "stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, "
            "stderr=subprocess.DEVNULL)\n"
            "pathlib.Path('parent.ready').write_text(str(os.getpid()))\n"
            "limit = time.monotonic() + 4\n"
            "while not pathlib.Path('child.ready').exists() and time.monotonic() < limit:\n"
            "    time.sleep(0.01)\n"
            + ("print('parent finished')\n" if parent_exits else "time.sleep(8)\n")
        )

    def test_deadline_cleans_parent_and_term_ignoring_child(self):
        scope = self._scope()
        result = self._thread(
            lambda: self._run(
                scope, self._tree_code(parent_exits=False),
                deadline=time.monotonic() + 1.5,
            )
        )
        parent_pid = int(self._wait_file("parent.ready"))
        child_pid = int(self._wait_file("child.ready"))
        self.assertTrue(result.done.wait(4))
        self.assertIsInstance(result.error, subprocess.TimeoutExpired)
        self._assert_dead(parent_pid)
        self._assert_dead(child_pid)

    def test_successful_parent_exit_still_cleans_term_ignoring_child(self):
        scope = self._scope()
        result = self._thread(lambda: self._run(scope, self._tree_code(parent_exits=True)))
        parent_pid = int(self._wait_file("parent.ready"))
        child_pid = int(self._wait_file("child.ready"))
        self.assertTrue(result.done.wait(3))
        self.assertIsNone(result.error)
        self.assertEqual(result.value.returncode, 0)
        self.assertEqual(result.value.stdout.strip(), "parent finished")
        self._assert_dead(parent_pid)
        self._assert_dead(child_pid)

    def test_close_waits_for_launch_in_progress_and_prevents_future_launches(self):
        scope = self._scope()
        launching = threading.Event()
        release_launch = threading.Event()
        self.addCleanup(release_launch.set)

        def blocked_launch(*args, **kwargs):
            launching.set()
            if not release_launch.wait(3):
                raise AssertionError("test launch barrier was not released")
            return self.launch(*args, **kwargs)

        self.popen.side_effect = blocked_launch
        running = self._thread(lambda: self._run(scope, self._sleeping_code("race.ready")))
        self.assertTrue(launching.wait(2))
        closing_started = threading.Event()

        def close():
            closing_started.set()
            scope.close()

        closing = self._thread(close)
        try:
            self.assertTrue(closing_started.wait(2))
            self.assertFalse(closing.done.is_set())
        finally:
            release_launch.set()
        self.assertTrue(closing.done.wait(3))
        self.assertIsNone(closing.error)
        self.assertTrue(running.done.wait(1))
        self.assertIsInstance(running.error, CASCancelled, running.traceback)
        self.assertEqual(len(self.processes), 1)
        self._assert_dead(self.processes[0].pid)
        with self.assertRaises(CASCancelled):
            self._run(scope, "print('must not launch')")
        self.assertEqual(len(self.processes), 1)

    def test_closing_one_agent_scope_does_not_cancel_another(self):
        first_scope, second_scope = self._scope(), self._scope()
        first = self._thread(lambda: self._run(first_scope, self._sleeping_code("one.ready")))
        second_code = (
            "import os, pathlib, time\n"
            "pathlib.Path('two.ready').write_text(str(os.getpid()))\n"
            "limit = time.monotonic() + 8\n"
            "while not pathlib.Path('two.release').exists() and time.monotonic() < limit:\n"
            "    time.sleep(0.01)\n"
            "print('second survived')\n"
        )
        second = self._thread(lambda: self._run(second_scope, second_code))
        first_pid = int(self._wait_file("one.ready"))
        second_pid = int(self._wait_file("two.ready"))
        first_scope.close()
        self.assertTrue(first.done.wait(1))
        self.assertIsInstance(first.error, CASCancelled, first.traceback)
        self._assert_dead(first_pid)
        self.assertFalse(second.done.is_set())
        os.kill(second_pid, 0)
        (self.root / "two.release").write_text("release")
        self.assertTrue(second.done.wait(2))
        self.assertIsNone(second.error)
        self.assertEqual(second.value.stdout.strip(), "second survived")

    def test_expired_scope_deadline_never_starts_process(self):
        with self.assertRaises(subprocess.TimeoutExpired):
            self._run(self._scope(), "print('must not launch')", deadline=time.monotonic() - 1)
        self.assertEqual(self.processes, [])

    def test_transient_group_probe_permission_error_does_not_skip_cleanup(self):
        scope = self._scope()
        code = (
            "import signal\n"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            + self._sleeping_code("permission.ready")
        )
        running = self._thread(lambda: self._run(scope, code))
        pid = int(self._wait_file("permission.ready"))
        real_killpg = os.killpg
        injected = False
        delivered = []

        def transient_error(pgid, sig):
            nonlocal injected
            if pgid == pid and sig == 0 and not injected:
                injected = True
                raise PermissionError("simulated transient macOS group probe")
            delivered.append((pgid, sig))
            return real_killpg(pgid, sig)

        with mock.patch.object(cas_process.os, "killpg", side_effect=transient_error):
            scope.close()
        self.assertTrue(injected)
        self.assertIn((pid, signal.SIGKILL), delivered)
        self.assertTrue(running.done.wait(1))
        self.assertIsInstance(running.error, CASCancelled, running.traceback)
        self._assert_dead(pid)

    def test_compute_and_version_share_deadline_and_version_is_reaped(self):
        scope = self._scope()
        context = skills.SkillContext(self.root, policy_for("verifier"))
        computation = (
            "import pathlib, time\n"
            "pathlib.Path('compute.ready').write_text('ready')\n"
            "limit = time.monotonic() + 8\n"
            "while not pathlib.Path('compute.release').exists() and time.monotonic() < limit:\n"
            "    time.sleep(0.01)\n"
            "print('computed')\n"
        )
        payload = {
            "software": "python", "exact_input": "",
            "arguments": ["-c", computation],
            "version_arguments": ["-c", self._sleeping_code("version.ready")],
        }
        deadline = time.monotonic() + 1.5
        with (
            mock.patch.object(
                skills, "_constrained_command",
                side_effect=lambda exe, args, **kw: [str(exe), *args],
            ),
            mock.patch.object(skills, "_clean_tool_environment", return_value=os.environ.copy()),
            mock.patch.object(scope, "run", wraps=scope.run) as run,
            mock.patch.object(skills.SkillRuntime, "invoke") as stage,
        ):
            result = self._thread(
                lambda: skills.execute_cas(
                    context, payload, configured_executables={"python": sys.executable},
                    process_scope=scope, deadline=deadline,
                )
            )
            self._wait_file("compute.ready")
            (self.root / "compute.release").write_text("release")
            version_pid = int(self._wait_file("version.ready"))
            self.assertTrue(result.done.wait(3))
            self.assertIsInstance(result.error, skills.SkillRuntimeError)
            self.assertIsInstance(
                result.error.__cause__, subprocess.TimeoutExpired, result.traceback,
            )
            self.assertEqual(len(run.call_args_list), 2)
            self.assertEqual(
                [call.kwargs["deadline"] for call in run.call_args_list],
                [deadline, deadline],
            )
            stage.assert_not_called()
        self._assert_dead(version_pid)

    def _broker(self, scope, handler):
        broker = MemoryBroker(InMemoryBackend())
        # Exercise the supported spool transport without depending on host
        # socket permissions; the actual CAS subprocess remains real.
        with mock.patch.object(broker_module, "_BrokerUnixServer", side_effect=PermissionError):
            broker.start(self.root / f"broker-{len(self.scopes)}.sock")
        self.addCleanup(broker.stop)
        binding = broker.issue(
            policy_for("verifier"), caller_id="CAS-LIFECYCLE",
            staging_handler=lambda skill, payload: {}, staging_skills={"CAS"},
            cas_software_names={"python"}, cas_handler=handler, on_revoke=scope.close,
        )
        return broker, binding

    def test_broker_client_timeout_also_stops_host_computation(self):
        scope = self._scope()
        host_done = threading.Event()
        host_errors = []

        def handler(payload, deadline):
            try:
                self._run(scope, self._sleeping_code("broker.ready"), deadline=deadline)
                return {}
            except Exception as exc:
                host_errors.append(exc)
                raise
            finally:
                host_done.set()

        _, binding = self._broker(scope, handler)
        client = BrokerClient(binding.socket_path, binding.token, timeout=1.5)
        result = self._thread(lambda: client.call("execute_cas", {"software": "python"}))
        pid = int(self._wait_file("broker.ready"))
        self.assertTrue(result.done.wait(3))
        self.assertIsInstance(result.error, BrokerError)
        self.assertTrue(host_done.wait(2))
        self.assertEqual(len(host_errors), 1)
        self.assertIsInstance(host_errors[0], subprocess.TimeoutExpired)
        self._assert_dead(pid)
        # Timing out one request must leave its live agent able to retry.
        self.assertEqual(self._run(scope, "print('retry')").stdout.strip(), "retry")

    def test_broker_revoke_reaps_active_cas_and_rejects_new_requests(self):
        scope = self._scope()

        def handler(payload, deadline):
            self._run(scope, self._sleeping_code("revoke.ready"), deadline=deadline)
            return {}

        broker, binding = self._broker(scope, handler)
        request = {
            "token": binding.token, "operation": "execute_cas",
            "arguments": {"software": "python"},
        }
        result = self._thread(lambda: broker.dispatch(request))
        pid = int(self._wait_file("revoke.ready"))
        broker.revoke(binding)
        self.assertTrue(result.done.wait(1))
        self.assertIsInstance(result.error, CASCancelled, result.traceback)
        self._assert_dead(pid)
        with self.assertRaises(BrokerError):
            broker.dispatch(request)
        with self.assertRaises(CASCancelled):
            self._run(scope, "print('must not launch')")

    def test_broker_stop_reaps_active_cas(self):
        scope = self._scope()

        def handler(payload, deadline):
            self._run(scope, self._sleeping_code("stop.ready"), deadline=deadline)
            return {}

        broker, binding = self._broker(scope, handler)
        result = self._thread(lambda: broker.dispatch({
            "token": binding.token, "operation": "execute_cas", "arguments": {"software": "python"},
        }))
        pid = int(self._wait_file("stop.ready"))
        broker.stop()
        self.assertTrue(result.done.wait(1))
        self.assertIsInstance(result.error, CASCancelled, result.traceback)
        self._assert_dead(pid)


class CASBrokerDeadlineTests(unittest.TestCase):
    def setUp(self):
        self.broker = MemoryBroker(InMemoryBackend())
        # dispatch-only tests do not need a listening transport.
        self.broker.socket_path = "unused-broker.sock"
        self.handler = mock.Mock(return_value={"operation_id": "CAS-1"})
        self.binding = self.broker.issue(
            policy_for("verifier"), caller_id="CAS-DEADLINE",
            staging_handler=lambda skill, payload: {}, staging_skills={"CAS"},
            cas_software_names={"python"}, cas_handler=self.handler,
        )

    def _request(self, deadline):
        return {
            "token": self.binding.token, "operation": "execute_cas",
            "arguments": {"software": "python"}, "deadline": deadline,
        }

    def test_expired_or_invalid_deadline_does_not_execute(self):
        for deadline in (time.monotonic() - 1, float("nan"), float("inf"), True, "300", None):
            with self.subTest(deadline=deadline):
                with self.assertRaises(BrokerError):
                    self.broker.dispatch(self._request(deadline))
        self.handler.assert_not_called()

    def test_request_deadline_can_shorten_but_cannot_extend_300_seconds(self):
        with mock.patch.object(broker_module.time, "monotonic", return_value=1000.0):
            self.broker.dispatch(self._request(1001.0))
            self.assertEqual(self.handler.call_args.args[1], 1001.0)
            self.broker.dispatch(self._request(2000.0))
            self.assertEqual(self.handler.call_args.args[1], 1300.0)
            request = self._request(0)
            del request["deadline"]
            self.broker.dispatch(request)
            self.assertEqual(self.handler.call_args.args[1], 1300.0)

    def test_cas_uses_300_seconds_other_calls_keep_30_and_explicit_override_works(self):
        for timeout, operation, expected in (
            (None, "execute_cas", 300.0), (None, "describe", 30.0),
            (2.0, "execute_cas", 2.0), (2.0, "describe", 2.0),
        ):
            with self.subTest(timeout=timeout, operation=operation):
                connection = mock.MagicMock()
                connection.recv.return_value = b'{"ok": true, "result": {}}\n'
                socket_context = mock.MagicMock()
                socket_context.__enter__.return_value = connection
                with (
                    mock.patch.object(broker_module.socket, "socket", return_value=socket_context),
                    mock.patch.object(broker_module.time, "monotonic", return_value=1000.0),
                ):
                    client = BrokerClient("unused.sock", "token", timeout=timeout)
                    client.call(operation, {})
                self.assertEqual(connection.settimeout.call_args_list[0].args, (expected,))
                request = json.loads(connection.sendall.call_args.args[0])
                if operation == "execute_cas":
                    self.assertEqual(request["deadline"], 1000.0 + expected)
                else:
                    self.assertNotIn("deadline", request)


if __name__ == "__main__":
    unittest.main()
