from __future__ import annotations

import copy
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest

from dashboard_system.monitor import QUALITY_DIMENSIONS, ResearchMonitor, validate_summary


def summary() -> dict:
    return {"directions": [{
        "title": "Study the boundary", "summary": "Reduce to the boundary case.",
        "why_promising": "An existing lemma handles the boundary.",
        "obstacles": "The reduction remains unproved.", "next_step": "Check the reduction.",
        "source_ids": ["FACT-1"],
    }], "quality": {dimension: {
        "score": 6, "reason": "A boundary lemma is established; the main reduction remains open.",
        "source_ids": ["FACT-1"],
    } for dimension in QUALITY_DIMENSIONS}}


class ReadPort:
    def __init__(self) -> None:
        self.snapshot = {"as_of": "2026-09-06T00:00:00Z", "source_ids": ["FACT-1"],
                         "run": {"status": "running", "cycle": 1}}

    def monitor_snapshot(self) -> dict:
        return copy.deepcopy(self.snapshot)


class ModelPort:
    def __init__(self) -> None:
        self.calls = 0
        self.error = None
        self.result = summary()
        self.entered = threading.Event()
        self.release = threading.Event()
        self.release.set()

    def summarize(self, snapshot: dict) -> dict:
        self.calls += 1
        self.entered.set()
        if not self.release.wait(timeout=5):
            raise RuntimeError("Test model was not released")
        if self.error:
            raise self.error
        return copy.deepcopy(self.result)


class DashboardMonitorTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.read = ReadPort()
        self.model = ModelPort()

    def monitor(self, **kwargs) -> ResearchMonitor:
        monitor = ResearchMonitor(self.read, self.model, self.root, **kwargs)
        self.addCleanup(monitor.stop)
        return monitor

    def wait_for(self, predicate) -> None:
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.005)
        self.fail("Dashboard monitor did not reach expected state")

    def test_initial_refresh_saves_durable_result_and_source_timestamp(self) -> None:
        monitor = self.monitor()
        self.wait_for(lambda: monitor.status()["status"] == "ready")
        state = monitor.status()
        self.assertEqual(state["result"], summary())
        self.assertEqual(state["source_as_of"], self.read.snapshot["as_of"])
        self.assertIsNotNone(state["generated_at"])
        cached = json.loads((self.root / "monitor.json").read_text())
        self.assertEqual(cached["result"], summary())
        self.assertEqual(cached["source_ids"], ["FACT-1"])
        self.assertEqual(cached["source_cycle"], 1)
        self.assertEqual(state["source_cycle"], 1)
        self.assertEqual(list(self.root.glob(".monitor-*.tmp")), [])

    def test_refresh_clicks_coalesce_while_summary_is_in_flight(self) -> None:
        self.model.release.clear()
        monitor = self.monitor(auto_start=False)
        monitor.refresh()
        monitor.start()
        self.assertTrue(self.model.entered.wait(timeout=2))
        for _ in range(20):
            self.assertEqual(monitor.refresh()["status"], "running")
        self.model.release.set()
        self.wait_for(lambda: monitor.status()["status"] == "ready")
        time.sleep(0.025)
        self.assertEqual(self.model.calls, 1)

    def test_periodic_refresh_runs_without_browser_requests(self) -> None:
        monitor = self.monitor(auto_refresh_seconds=0.04)
        self.wait_for(lambda: self.model.calls >= 2)
        monitor.stop()
        self.assertGreaterEqual(self.model.calls, 2)

    def test_cached_summary_is_restored_without_immediate_model_call(self) -> None:
        first = self.monitor()
        self.wait_for(lambda: first.status()["status"] == "ready")
        first.stop()
        self.model.calls = 0
        second = self.monitor()
        self.assertEqual(second.status()["result"], summary())
        time.sleep(0.025)
        self.assertEqual(self.model.calls, 0)

    def test_failure_retains_previous_result_and_disk_cache(self) -> None:
        monitor = self.monitor()
        self.wait_for(lambda: monitor.status()["status"] == "ready")
        previous = (self.root / "monitor.json").read_bytes()
        self.model.error = RuntimeError("Model unavailable")
        monitor.refresh()
        self.wait_for(lambda: monitor.status()["status"] == "error")
        self.assertEqual(monitor.status()["result"], summary())
        self.assertEqual(monitor.status()["error"], "Model unavailable")
        self.assertEqual((self.root / "monitor.json").read_bytes(), previous)

    def test_snapshot_failure_is_contained(self) -> None:
        self.read.monitor_snapshot = lambda: (_ for _ in ()).throw(RuntimeError("Read view unavailable"))
        monitor = self.monitor()
        self.wait_for(lambda: monitor.status()["status"] == "error")
        self.assertIn("Read view unavailable", monitor.status()["error"])
        self.assertEqual(self.model.calls, 0)

    def test_no_memory_does_not_launch_model(self) -> None:
        self.read.snapshot["source_ids"] = []
        monitor = self.monitor(auto_refresh_seconds=0.02)
        time.sleep(0.06)
        self.assertEqual(monitor.status()["status"], "idle")
        self.assertEqual(self.model.calls, 0)

    def test_completed_research_only_allows_manual_refresh(self) -> None:
        self.read.snapshot["run"]["status"] = "completed"
        monitor = self.monitor(auto_refresh_seconds=0.03)
        time.sleep(0.05)
        self.assertEqual(self.model.calls, 0)
        monitor.refresh()
        self.wait_for(lambda: monitor.status()["status"] == "ready")
        self.assertEqual(self.model.calls, 1)
        time.sleep(0.06)
        self.assertEqual(self.model.calls, 1)

    def test_waiting_for_human_still_allows_automatic_summary(self) -> None:
        self.read.snapshot["run"]["status"] = "waiting_for_human"
        monitor = self.monitor()
        self.wait_for(lambda: monitor.status()["status"] == "ready")
        self.assertEqual(self.model.calls, 1)

    def test_unknown_state_does_not_trigger_perpetual_automatic_calls(self) -> None:
        self.read.snapshot.pop("run")
        monitor = self.monitor(auto_refresh_seconds=0.02)
        self.wait_for(lambda: monitor.status()["status"] == "ready")
        time.sleep(0.06)
        self.assertEqual(self.model.calls, 1)

    def test_stop_during_model_call_is_bounded_and_does_not_publish_after_close(self) -> None:
        self.model.release.clear()
        monitor = self.monitor()
        self.assertTrue(self.model.entered.wait(timeout=2))
        monitor.stop(timeout=0.01)
        with self.assertRaisesRegex(RuntimeError, "stopped"):
            monitor.refresh()
        self.model.release.set()
        monitor.stop(timeout=1)
        self.assertFalse((self.root / "monitor.json").exists())

    def test_stop_during_snapshot_read_does_not_start_a_model_call(self) -> None:
        entered = threading.Event()
        release = threading.Event()
        snapshot = copy.deepcopy(self.read.snapshot)

        def blocking_snapshot() -> dict:
            entered.set()
            if not release.wait(timeout=3):
                raise RuntimeError("Test snapshot was not released")
            return snapshot

        self.read.monitor_snapshot = blocking_snapshot
        monitor = self.monitor()
        self.assertTrue(entered.wait(timeout=2))
        monitor.stop(timeout=0.01)
        release.set()
        monitor.stop(timeout=1)
        self.assertEqual(self.model.calls, 0)
        self.assertFalse((self.root / "monitor.json").exists())

    def test_corrupt_cache_does_not_prevent_fresh_summary(self) -> None:
        (self.root / "monitor.json").write_text("{incomplete")
        monitor = self.monitor()
        self.wait_for(lambda: monitor.status()["status"] == "ready")
        self.assertEqual(self.model.calls, 1)

    def test_result_cannot_cite_nonexistent_memory(self) -> None:
        self.model.result["directions"][0]["source_ids"] = ["MADE-UP-FACT"]
        monitor = self.monitor()
        self.wait_for(lambda: monitor.status()["status"] == "error")
        self.assertIn("existing snapshot", monitor.status()["error"])
        self.assertFalse((self.root / "monitor.json").exists())

    def test_summary_validation_rejects_missing_fields_and_too_many_directions(self) -> None:
        for field in ("title", "summary", "why_promising", "obstacles", "next_step", "source_ids"):
            with self.subTest(field=field):
                value = summary()
                del value["directions"][0][field]
                with self.assertRaises(ValueError):
                    validate_summary(value, {"FACT-1"})
        with self.assertRaisesRegex(ValueError, "at most five"):
            validate_summary({"directions": summary()["directions"] * 6}, {"FACT-1"})
        self.assertEqual(validate_summary({"directions": []}, set()), {"directions": []})

    def test_status_is_an_independent_copy(self) -> None:
        monitor = self.monitor()
        self.wait_for(lambda: monitor.status()["status"] == "ready")
        state = monitor.status()
        state["result"]["directions"].clear()
        self.assertEqual(monitor.status()["result"], summary())

    def test_quality_validation_rejects_invalid_scores_reasons_and_citations(self) -> None:
        for score in (0, 11, True, 5.5, "6", None):
            with self.subTest(score=score):
                value = summary()
                value["quality"]["creativity"]["score"] = score
                with self.assertRaisesRegex(ValueError, "integers from 1 to 10"):
                    validate_summary(value, {"FACT-1"}, require_quality=True)
        for field, invalid in (("reason", " "), ("source_ids", []), ("source_ids", ["MISSING"])):
            with self.subTest(field=field, invalid=invalid):
                value = summary()
                value["quality"]["creativity"][field] = invalid
                with self.assertRaises(ValueError):
                    validate_summary(value, {"FACT-1"}, require_quality=True)
        for dimension in QUALITY_DIMENSIONS:
            value = summary()
            del value["quality"][dimension]
            with self.assertRaisesRegex(ValueError, "all five"):
                validate_summary(value, {"FACT-1"}, require_quality=True)

    def test_quality_is_required_for_new_results_but_legacy_cache_still_loads(self) -> None:
        first = self.monitor()
        self.wait_for(lambda: first.status()["status"] == "ready")
        first.stop()
        path = self.root / "monitor.json"
        cached = json.loads(path.read_text())
        del cached["result"]["quality"]
        cached.pop("source_cycle")
        path.write_text(json.dumps(cached))
        second = self.monitor(auto_start=False)
        self.assertEqual(second.status()["result"], {"directions": summary()["directions"]})
        self.assertIsNone(second.status()["source_cycle"])
        self.model.result.pop("quality")
        second.refresh()
        second.start()
        self.wait_for(lambda: second.status()["status"] == "error")
        self.assertIn("all five", second.status()["error"])
        self.assertEqual(json.loads(path.read_text()), cached)

    def test_quality_can_be_reported_without_promising_directions(self) -> None:
        self.model.result["directions"] = []
        monitor = self.monitor()
        self.wait_for(lambda: monitor.status()["status"] == "ready")
        self.assertEqual(monitor.status()["result"]["quality"], summary()["quality"])

    def test_failed_new_cycle_refresh_retains_the_previous_assessment_cycle(self) -> None:
        monitor = self.monitor()
        self.wait_for(lambda: monitor.status()["status"] == "ready")
        self.read.snapshot["run"]["cycle"] = 2
        self.model.error = RuntimeError("Model unavailable")
        monitor.refresh()
        self.wait_for(lambda: monitor.status()["status"] == "error")
        self.assertEqual(monitor.status()["source_cycle"], 1)
        self.assertEqual(monitor.status()["result"], summary())


if __name__ == "__main__":
    unittest.main()
