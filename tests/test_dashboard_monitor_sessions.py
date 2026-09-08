from __future__ import annotations

import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from franta.dashboard_adapter import FrantaReadOnlyMonitor, _MONITOR_SCHEMA
from test_dashboard_monitor import summary


THREAD_1 = "11111111-1111-4111-8111-111111111111"
THREAD_2 = "22222222-2222-4222-8222-222222222222"


class MonitorSessionTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.project = Path(temporary.name)
        (self.project / "private").mkdir()
        (self.project / "private/runtime-config.json").write_text(json.dumps({
            "tools": {"codex": "test-codex"}, "default_model": {"model": "test-model"},
        }))
        self.snapshot = {
            "project": {"name": "Test", "root_problem": "Prove the boundary reduction."},
            "as_of": "2026-09-07T10:00:00Z",
            "run": {"cycle": 1, "phase": "explorer_admission", "status": "running",
                    "cycle_started_at": "2026-09-07T09:00:00Z"},
            "main": [{"id": "FACT-1", "type": "fact", "status": "active", "active": True,
                      "abstract": "The boundary lemma holds.", "revision": 1,
                      "updated_at": "2026-09-07T09:30:00Z"}],
            "explorer": [], "source_ids": ["FACT-1"],
        }

    def session_path(self, cycle=1) -> Path:
        key = f"cycle-{cycle}" if cycle is not None else "legacy"
        return self.project / "private/dashboard/monitor-sessions" / key / "session.json"

    def launch(self, *, thread=THREAD_1, result=None, error="", returncode=0, timeout=False):
        value = summary() if result is None else result
        def start(command, **kwargs):
            events = []
            if thread:
                events.append({"type": "thread.started", "thread_id": thread})
            events.extend([
                {"type": "item.completed", "item": {"type": "agent_message", "text": json.dumps(value)}},
                {"type": "turn.completed", "usage": {"input_tokens": 100, "output_tokens": 10}},
            ])
            kwargs["stdout"].write("\n".join(json.dumps(event) for event in events) + "\n")
            kwargs["stdout"].flush()
            kwargs["stderr"].write(error)
            process = Mock(returncode=returncode, pid=999999)
            process.poll.return_value = returncode
            process.communicate.return_value = (None, None)
            if timeout:
                process.communicate.side_effect = [subprocess.TimeoutExpired(command, 1), (None, None)]
            return process
        return start

    def call(self, **kwargs):
        with patch("franta.dashboard_adapter.subprocess.Popen", side_effect=self.launch(**kwargs)) as launch:
            result = FrantaReadOnlyMonitor(self.project).summarize(copy.deepcopy(self.snapshot))
        return result, launch.call_args

    def test_phase_changes_and_dashboard_restart_resume_the_exact_session(self) -> None:
        result, initial = self.call()
        self.assertEqual(result, summary())
        self.assertNotIn("resume", initial.args[0])
        self.assertNotIn("--ephemeral", initial.args[0])
        self.assertEqual(json.loads(self.session_path().read_text())["thread_id"], THREAD_1)
        for phase in ("explorer_drain", "franta_sort", "franta_run", "franta_drain"):
            self.snapshot["run"]["phase"] = phase
            result, resumed = self.call()
            command = resumed.args[0]
            self.assertEqual(command[:3], ["test-codex", "exec", "resume"])
            self.assertEqual(command[-2:], [THREAD_1, "-"])
            self.assertEqual(resumed.kwargs["cwd"], initial.kwargs["cwd"])
            self.assertIn('sandbox_mode="read-only"', command)
            self.assertIn('approval_policy="never"', command)
            self.assertIn('web_search="disabled"', command)
            self.assertIn("--output-schema", command)
            for unsupported in ("--ephemeral", "--sandbox", "-C", "--color"):
                self.assertNotIn(unsupported, command)

    def test_new_cycle_starts_a_new_session_and_keeps_previous_cycle_state(self) -> None:
        self.call()
        previous = self.session_path().read_bytes()
        self.snapshot["run"]["cycle"] = 2
        result, launched = self.call(thread=THREAD_2)
        self.assertNotIn("resume", launched.args[0])
        self.assertEqual(json.loads(self.session_path(2).read_text())["thread_id"], THREAD_2)
        self.assertEqual(self.session_path().read_bytes(), previous)
        _, resumed = self.call(thread=THREAD_2)
        self.assertEqual(resumed.args[0][-2], THREAD_2)

    def test_legacy_project_reuses_one_session_without_alternation(self) -> None:
        self.snapshot["run"].update(cycle=None, phase=None)
        self.call()
        _, resumed = self.call()
        self.assertEqual(resumed.args[0][-2], THREAD_1)
        self.assertTrue(self.session_path(None).is_file())

    def test_latest_snapshot_records_added_updated_removed_ids_and_cycle_context(self) -> None:
        self.snapshot["main"].append({"id": "FACT-OLD", "status": "active"})
        self.call()
        old_state = json.loads(self.session_path().read_text())
        old_index = self.project / "private/dashboard/monitor-runs" / old_state["last_run"] / "index.json"
        old_bytes = old_index.read_bytes()
        self.snapshot["as_of"] = "2026-09-07T11:00:00Z"
        self.snapshot["main"][0].update(status="revoked", active=False, revision=2)
        self.snapshot["main"][1] = {"id": "MEMO-NEW", "abstract": "The reduction has a gap."}
        self.call()
        state = json.loads(self.session_path().read_text())
        directory = self.project / "private/dashboard/monitor-runs" / state["last_run"]
        index = json.loads((directory / "index.json").read_text())
        self.assertEqual(index["changes_since_previous_snapshot"], {
            "previous_as_of": "2026-09-07T10:00:00Z", "added_ids": ["MEMO-NEW"],
            "updated_ids": ["FACT-1"], "removed_ids": ["FACT-OLD"],
        })
        self.assertEqual(index["run"], self.snapshot["run"])
        record = json.loads((directory / index["records"][0]["path"]).read_text())
        self.assertEqual(record["status"], "revoked")
        self.assertEqual(old_index.read_bytes(), old_bytes)

    def test_timeout_preserves_thread_for_resume_and_retains_usage(self) -> None:
        with patch("franta.dashboard_adapter.subprocess.Popen", side_effect=self.launch(timeout=True)):
            with self.assertRaisesRegex(RuntimeError, "time limit"):
                FrantaReadOnlyMonitor(self.project).summarize(self.snapshot)
        self.assertEqual(json.loads(self.session_path().read_text())["thread_id"], THREAD_1)
        _, resumed = self.call()
        self.assertEqual(resumed.args[0][-2], THREAD_1)
        receipts = [json.loads(path.read_text()) for path in (self.project / "private/dashboard/monitor-runs").glob("*/usage.json")]
        self.assertEqual(len(receipts), 2)
        self.assertEqual(sum(item["timed_out"] for item in receipts), 1)
        self.assertEqual(sum(item["resumed"] for item in receipts), 1)

    def test_invalid_result_does_not_discard_the_session(self) -> None:
        with self.assertRaisesRegex(ValueError, "all five"):
            self.call(result={"directions": []})
        self.assertEqual(json.loads(self.session_path().read_text())["thread_id"], THREAD_1)
        _, resumed = self.call()
        self.assertEqual(resumed.args[0][-2], THREAD_1)

    def test_failed_resume_never_falls_back_to_a_new_session(self) -> None:
        self.call()
        with self.assertRaisesRegex(RuntimeError, "Session unavailable"):
            self.call(thread=None, returncode=1, error="Session unavailable")
        _, resumed = self.call()
        self.assertEqual(resumed.args[0][-2], THREAD_1)

    def test_disk_events_recover_session_after_dashboard_exits_before_saving_thread(self) -> None:
        self.call()
        session = json.loads(self.session_path().read_text())
        session["thread_id"] = None
        self.session_path().write_text(json.dumps(session))
        events = self.project / "private/dashboard/monitor-runs" / session["last_run"] / "events.jsonl"
        with events.open("ab") as stream:
            stream.write(b'{"type":"item.started","partial":"\xe2\x82')
        _, resumed = self.call()
        self.assertEqual(resumed.args[0][-2], THREAD_1)

    def test_missing_thread_blocks_replacement_until_the_next_cycle(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "no resumable session"):
            self.call(thread=None)
        with patch("franta.dashboard_adapter.subprocess.Popen") as start:
            with self.assertRaisesRegex(RuntimeError, "could not be recovered"):
                FrantaReadOnlyMonitor(self.project).summarize(self.snapshot)
            start.assert_not_called()
        self.snapshot["run"]["cycle"] = 2
        _, launched = self.call(thread=THREAD_2)
        self.assertNotIn("resume", launched.args[0])

    def test_wrong_thread_is_rejected_and_never_replaces_the_saved_thread(self) -> None:
        self.call()
        with self.assertRaisesRegex(RuntimeError, "different session"):
            self.call(thread=THREAD_2)
        self.assertEqual(json.loads(self.session_path().read_text())["thread_id"], THREAD_1)

    def test_process_start_failure_can_retry_without_a_phantom_session(self) -> None:
        with patch("franta.dashboard_adapter.subprocess.Popen", side_effect=FileNotFoundError("Missing CLI")):
            with self.assertRaises(FileNotFoundError):
                FrantaReadOnlyMonitor(self.project).summarize(self.snapshot)
        _, launched = self.call()
        self.assertNotIn("resume", launched.args[0])

    def test_simultaneous_refresh_cannot_create_two_conversations(self) -> None:
        start = self.launch()
        def overlapping(command, **kwargs):
            with self.assertRaisesRegex(RuntimeError, "already running"):
                FrantaReadOnlyMonitor(self.project).summarize(self.snapshot)
            return start(command, **kwargs)
        with patch("franta.dashboard_adapter.subprocess.Popen", side_effect=overlapping) as launch:
            FrantaReadOnlyMonitor(self.project).summarize(self.snapshot)
        self.assertEqual(launch.call_count, 1)

    def test_invalid_cycle_or_corrupt_state_cannot_launch_a_session(self) -> None:
        with patch("franta.dashboard_adapter.subprocess.Popen") as start:
            for value in (0, -1, True, "../other", None):
                self.snapshot["run"]["cycle"] = value
                with self.subTest(cycle=value), self.assertRaises(ValueError):
                    FrantaReadOnlyMonitor(self.project).summarize(self.snapshot)
            self.snapshot["run"]["cycle"] = 1
            self.session_path().parent.mkdir(parents=True)
            self.session_path().write_text("{}")
            with self.assertRaisesRegex(RuntimeError, "invalid"):
                FrantaReadOnlyMonitor(self.project).summarize(self.snapshot)
            start.assert_not_called()

    def test_score_schema_enforces_all_five_dimensions(self) -> None:
        self.assertIn("quality", _MONITOR_SCHEMA["required"])
        quality = _MONITOR_SCHEMA["properties"]["quality"]
        self.assertEqual(set(quality["required"]), set(summary()["quality"]))
        for dimension in quality["properties"].values():
            self.assertEqual(dimension["properties"]["score"], {"type": "integer", "minimum": 1, "maximum": 10})

    def test_real_subprocess_preserves_events_stdin_and_cycle_lock(self) -> None:
        popen = subprocess.Popen
        result = summary()
        script = '''
import fcntl, json, pathlib, sys
prompt = sys.stdin.read()
assert 'Current snapshot index:' in prompt
assert 'creativity' in prompt and 'proof_closure_potential' in prompt
assert 'must be in English' in prompt
with pathlib.Path('session.lock').open('a') as handle:
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        pass
    else:
        raise AssertionError('The Monitor did not hold its cycle lock')
print(json.dumps({'type': 'thread.started', 'thread_id': sys.argv[1]}), flush=True)
print(json.dumps({'type': 'item.completed', 'item': {'type': 'agent_message', 'text': sys.argv[2]}}), flush=True)
'''
        commands = []
        def start(command, **kwargs):
            commands.append(command)
            return popen([sys.executable, '-B', '-c', script, THREAD_1, json.dumps(result)], **kwargs)
        with patch("franta.dashboard_adapter.subprocess.Popen", side_effect=start):
            for _ in range(2):
                self.assertEqual(FrantaReadOnlyMonitor(self.project).summarize(self.snapshot), result)
        self.assertNotIn("resume", commands[0])
        self.assertEqual(commands[1][-2], THREAD_1)
        state = json.loads(self.session_path().read_text())
        events = self.project / "private/dashboard/monitor-runs" / state["last_run"] / "events.jsonl"
        self.assertEqual(json.loads(events.read_text().splitlines()[0])["thread_id"], THREAD_1)


if __name__ == "__main__":
    unittest.main()
