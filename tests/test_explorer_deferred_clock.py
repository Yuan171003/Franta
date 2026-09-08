from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from franta.config import load_manifest
from franta.runtime import FrantaRuntime
from franta.scheduler import WorkflowError


UTC = timezone.utc
LATE_START = datetime(2036, 1, 2, 3, 4, 5, tzinfo=UTC)


def _write_manifest(root: Path, *, explorer: bool) -> Path:
    explorer_section = """
[explorer]
max_workers = 1
attempts_per_worker = 3
attempt_seconds = 30
explorer_admission_seconds = 60
franta_admission_seconds = 90
""" if explorer else ""
    manifest = root / "bootstrap.toml"
    manifest.write_text(
        f"""
[project]
name = "deferred-explorer-clock"
directory = "project"
root_problem = "Prove the deterministic ROOT statement."
foundation_policy = "Use only the declared test axioms."
{explorer_section}
[initial]
""".strip()
        + "\n",
        encoding="utf-8",
    )
    return manifest


class ExplorerDeferredClockTests(unittest.TestCase):
    def test_init_days_before_run_does_not_consume_explorer_admission(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            runtime = FrantaRuntime.initialize(
                load_manifest(_write_manifest(Path(raw), explorer=True))
            )
            try:
                initial = runtime.scheduler.state["phase_control"]
                stale_deadline = datetime.fromisoformat(
                    initial["explorer"]["admission_deadline"]
                )
                late_start = stale_deadline + timedelta(days=30)
                revision = runtime.scheduler.revision

                self.assertEqual(
                    runtime.scheduler.tick_alternation(now=late_start),
                    "explorer_admission",
                )
                self.assertEqual(runtime.scheduler.revision, revision)
                with self.assertRaisesRegex(
                    WorkflowError, "admission clock has not started"
                ):
                    runtime.scheduler.admit_explorer_lineage(now=late_start)

                self.assertTrue(
                    runtime.scheduler.activate_alternation(now=late_start)
                )
                activated = runtime.scheduler.state["phase_control"]
                self.assertEqual(
                    datetime.fromisoformat(
                        activated["explorer"]["admission_started_at"]
                    ),
                    late_start,
                )
                self.assertEqual(
                    datetime.fromisoformat(
                        activated["explorer"]["admission_deadline"]
                    ),
                    late_start + timedelta(seconds=60),
                )
                self.assertEqual(
                    runtime.scheduler.tick_alternation(
                        now=late_start + timedelta(seconds=59)
                    ),
                    "explorer_admission",
                )
                self.assertEqual(
                    runtime.scheduler.tick_alternation(
                        now=late_start + timedelta(seconds=60)
                    ),
                    "explorer_drain",
                )
            finally:
                runtime.close()

    def test_bootstrap_delay_precedes_automatic_clock_activation(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            runtime = FrantaRuntime.initialize(
                load_manifest(_write_manifest(Path(raw), explorer=True))
            )
            bootstrap_observations: list[bool] = []

            def finish_delayed_bootstrap() -> bool:
                phase = runtime.scheduler.state["phase_control"]
                bootstrap_observations.append(
                    "alternation_clock_started_at" in phase
                )
                return True

            try:
                with (
                    mock.patch.object(
                        runtime,
                        "_advance_bootstrap_proposals",
                        side_effect=finish_delayed_bootstrap,
                    ),
                    mock.patch.object(
                        runtime, "_bootstrap_state", return_value=(1, {"stable": True})
                    ),
                    mock.patch.object(
                        runtime.scheduler, "_reducer_now", return_value=LATE_START
                    ),
                    mock.patch.object(
                        runtime.explorer_program, "run_wave", return_value=False
                    ),
                ):
                    runtime.run(max_cycles=1)

                self.assertEqual(bootstrap_observations, [False])
                phase = runtime.scheduler.state["phase_control"]
                self.assertEqual(
                    phase["alternation_clock_started_at"], LATE_START.isoformat()
                )
                self.assertEqual(
                    datetime.fromisoformat(
                        phase["explorer"]["admission_deadline"]
                    ),
                    LATE_START + timedelta(seconds=60),
                )
            finally:
                runtime.close()

    def test_reopen_and_recover_preserve_an_unactivated_clock(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            runtime = FrantaRuntime.initialize(
                load_manifest(_write_manifest(Path(raw), explorer=True))
            )
            project_dir = runtime.layout.root
            stale_deadline = datetime.fromisoformat(
                runtime.scheduler.state["phase_control"]["explorer"][
                    "admission_deadline"
                ]
            )
            runtime.close()

            reopened = FrantaRuntime.open(project_dir)
            try:
                reopened.recover()
                phase = reopened.scheduler.state["phase_control"]
                self.assertTrue(phase["deferred_start"])
                self.assertNotIn("alternation_clock_started_at", phase)
                late_start = stale_deadline + timedelta(days=10)
                self.assertEqual(
                    reopened.scheduler.tick_alternation(now=late_start),
                    "explorer_admission",
                )
                self.assertTrue(
                    reopened.scheduler.activate_alternation(now=late_start)
                )
                self.assertEqual(
                    datetime.fromisoformat(
                        reopened.scheduler.state["phase_control"]["explorer"][
                            "admission_deadline"
                        ]
                    ),
                    late_start + timedelta(seconds=60),
                )
            finally:
                reopened.close()

    def test_activation_and_reopen_cannot_refresh_an_active_window(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            runtime = FrantaRuntime.initialize(
                load_manifest(_write_manifest(Path(raw), explorer=True))
            )
            project_dir = runtime.layout.root
            try:
                self.assertTrue(
                    runtime.scheduler.activate_alternation(now=LATE_START)
                )
                deadline = runtime.scheduler.state["phase_control"]["explorer"][
                    "admission_deadline"
                ]
                events = [
                    event
                    for event in runtime.scheduler.state["events"]
                    if event["type"] == "alternation_clock_started"
                ]
                self.assertEqual(len(events), 1)
                self.assertFalse(
                    runtime.scheduler.activate_alternation(
                        now=LATE_START + timedelta(days=1)
                    )
                )
                self.assertEqual(
                    runtime.scheduler.state["phase_control"]["explorer"][
                        "admission_deadline"
                    ],
                    deadline,
                )
            finally:
                runtime.close()

            reopened = FrantaRuntime.open(project_dir)
            try:
                reopened.recover()
                self.assertFalse(
                    reopened.scheduler.activate_alternation(
                        now=LATE_START + timedelta(days=2)
                    )
                )
                phase = reopened.scheduler.state["phase_control"]
                self.assertEqual(
                    phase["explorer"]["admission_deadline"], deadline
                )
                self.assertEqual(
                    len(
                        [
                            event
                            for event in reopened.scheduler.state["events"]
                            if event["type"] == "alternation_clock_started"
                        ]
                    ),
                    1,
                )
            finally:
                reopened.close()

    def test_legacy_run_never_invokes_alternation_activation(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            runtime = FrantaRuntime.initialize(
                load_manifest(_write_manifest(Path(raw), explorer=False))
            )
            try:
                with (
                    mock.patch.object(
                        runtime.scheduler,
                        "activate_alternation",
                        side_effect=AssertionError("legacy activation was invoked"),
                    ),
                    mock.patch.object(runtime, "_run_trim", return_value=None),
                ):
                    runtime.run(max_cycles=1)
                self.assertIsNone(runtime.scheduler.alternation_phase)
                self.assertNotIn("phase_control", runtime.scheduler.state)
                self.assertNotIn("explorer_control", runtime.scheduler.state)
            finally:
                runtime.close()


if __name__ == "__main__":
    unittest.main()
