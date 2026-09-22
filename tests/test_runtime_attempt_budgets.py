from __future__ import annotations

import copy
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from franta.config import load_manifest
from franta.dashboard_commands import dashboard_directory, publish_json
from franta.explorer_adapter import build_main_sort_explorer_snapshot_for_frozen_turn
from franta.materialize import explorer_snapshot_digest
from franta.phase_control import state as phase_controller
from franta.runtime import AgentCall, FrantaRuntime
from franta.scheduler import StaleLeaseError
from test_runtime_alternation_e2e import _brainstorm_assignment, _deadline_test_result


def _manifest(root: Path) -> Path:
    path = root / "budget.toml"
    path.write_text(
        """[project]
name = "runtime-attempt-budgets"
directory = "project"
root_problem = "Prove or disprove the deterministic ROOT statement."
foundation_policy = "Use only the declared test axioms."

[limits]
max_non_verifier_workers = 2

[explorer]
max_workers = 2
attempts_per_worker = 3
attempt_seconds = 30

[initial]
""",
        encoding="utf-8",
    )
    return path


def _enter_franta(runtime: FrantaRuntime) -> None:
    """Create a real sort barrier without relying on obsolete deadlines."""

    runtime.start_services()
    scheduler = runtime.scheduler
    scheduler.commit_initial_trim({"category_ids": []})
    scheduler.activate_alternation()
    at = scheduler._reducer_now()
    with scheduler._mutate() as state:
        transition = phase_controller.request_explorer_drain(
            state["phase_control"], reason="test_empty_explorer", now=at
        )
        state["phase_control"] = copy.deepcopy(transition.state)
        scheduler._append_reducer_events_locked(state, transition.events)
    repository = runtime.explorer_repository
    assert repository is not None
    cycle = int(scheduler.state["phase_control"]["cycle"])
    frozen = repository.freeze_turn(f"XTURN-{cycle:08d}")
    sort_run_id = f"SORT-BUDGET-{cycle}"
    snapshot = build_main_sort_explorer_snapshot_for_frozen_turn(
        repository, sort_run_id=sort_run_id, frozen_turn=frozen
    )
    task_id = scheduler.prepare_main_sort_task(
        sort_run_id=sort_run_id,
        turn_id=frozen.turn_id,
        source_high_water_seq=frozen.high_water_seq,
        source_set_digest=frozen.source_set_digest,
        snapshot_format_version=1,
        snapshot_digest=explorer_snapshot_digest(snapshot),
    )
    repository.create_sort_run(sort_run_id, frozen, task_id)
    sort_call_id = scheduler.begin_main_sort_task_attempt(
        task_id, sort_run_id=sort_run_id, session_key="main:project", now=at
    )
    scheduler.open_franta_run(
        sort_call_id=sort_call_id,
        planning_call_id=f"CALL-BUDGET-SEED-{cycle}",
        session_key="main:project",
        now=at,
    )


def _set_limit(runtime: FrantaRuntime, limit: int, command: str) -> None:
    now = runtime.scheduler._reducer_now()
    runtime.scheduler.queue_attempt_limits(
        explorer_limit=20,
        franta_limit=limit,
        command_id=command,
        submitted_at=(now - timedelta(seconds=120)).isoformat(),
    )
    runtime.scheduler.tick_alternation(now=now)


def _wait_for(predicate: Any, message: str) -> None:
    deadline = time.monotonic() + 8
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError(message)
        time.sleep(0.02)


class RuntimeAttemptBudgetTests(unittest.TestCase):
    def test_limit_maturing_inside_main_prepare_defers_without_runner_error(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            invoked: list[str] = []

            def executor(call: AgentCall) -> dict[str, Any]:
                invoked.append(call.kind)
                return _deadline_test_result(runtime, call)

            runtime = FrantaRuntime.initialize(load_manifest(_manifest(Path(raw))), executor=executor)
            try:
                clock = [datetime.now(timezone.utc)]
                runtime.scheduler._reducer_now = lambda now=None: now or clock[0]
                _enter_franta(runtime)
                task_id = runtime.scheduler.submit_batch(
                    "BATCH-PREPARE-MATURITY", [_brainstorm_assignment(runtime, "PREPARE-MATURITY")]
                )[0]
                runtime._run_worker_batch([task_id])
                runtime.scheduler.queue_attempt_limits(
                    explorer_limit=20, franta_limit=1, command_id="ATL-PREPARE-MATURITY",
                    submitted_at=clock[0].isoformat(),
                )
                clock[0] += timedelta(seconds=119)
                prepare = runtime.scheduler.prepare_call

                def mature_before_prepare(kind: str, *args: Any, **kwargs: Any) -> str:
                    if kind == "main":
                        clock[0] += timedelta(seconds=1)
                    return prepare(kind, *args, **kwargs)

                runtime.scheduler.prepare_call = mature_before_prepare
                self.assertFalse(runtime._run_main())
                self.assertEqual(invoked, ["worker"])
                self.assertEqual(runtime.scheduler.alternation_phase, "franta_drain")
                self.assertIsNone(runtime.scheduler.state["phase_control"]["attempt_budget"]["pending"])
                self.assertFalse(any(
                    call["kind"] == "main" for call in runtime.scheduler.state["calls"].values()
                ))
                self.assertFalse(runtime.scheduler.has_blocking_attention())
            finally:
                runtime.close()

    def test_limit_maturing_inside_advisor_prepare_reopens_without_runner_error(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            manifest = _manifest(Path(raw))
            manifest.write_text(
                manifest.read_text(encoding="utf-8").replace("[initial]", "[advisor]\n\n[initial]"),
                encoding="utf-8",
            )
            invoked: list[str] = []

            def executor(call: AgentCall) -> dict[str, Any]:
                invoked.append(call.kind)
                return _deadline_test_result(runtime, call)

            runtime = FrantaRuntime.initialize(load_manifest(manifest), executor=executor)
            try:
                clock = [datetime.now(timezone.utc)]
                runtime.scheduler._reducer_now = lambda now=None: now or clock[0]
                _enter_franta(runtime)
                task_id = runtime.scheduler.submit_batch(
                    "BATCH-ADVISOR-MATURITY", [_brainstorm_assignment(runtime, "ADVISOR-MATURITY")]
                )[0]
                runtime._run_worker_batch([task_id])
                _set_limit(runtime, 1, "ATL-ADVISOR-CLOSE")
                runtime.scheduler.queue_attempt_limits(
                    explorer_limit=20, franta_limit=4, command_id="ATL-ADVISOR-MATURITY",
                    submitted_at=clock[0].isoformat(),
                )
                clock[0] += timedelta(seconds=119)
                prepare = runtime.scheduler.prepare_advisor_proposal

                def mature_before_prepare(*args: Any, **kwargs: Any) -> str:
                    clock[0] += timedelta(seconds=1)
                    return prepare(*args, **kwargs)

                runtime.scheduler.prepare_advisor_proposal = mature_before_prepare
                self.assertTrue(runtime._finish_franta_drain())
                self.assertEqual(invoked, ["worker"])
                self.assertEqual(runtime.scheduler.alternation_phase, "franta_run")
                self.assertIsNone(runtime.scheduler.state["advisor_control"]["active"])
                self.assertIsNone(runtime.scheduler.state["phase_control"]["attempt_budget"]["pending"])
                self.assertFalse(any(
                    call["kind"].startswith("advisor-") for call in runtime.scheduler.state["calls"].values()
                ))
                self.assertFalse(runtime.scheduler.has_blocking_attention())
            finally:
                runtime.close()

    def test_worker_return_after_budget_close_cannot_start_main_or_trim(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            invoked: list[str] = []

            def executor(call: AgentCall) -> dict[str, Any]:
                invoked.append(call.kind)
                result = _deadline_test_result(runtime, call)
                if call.kind == "worker":
                    _set_limit(runtime, 1, "ATL-WORKER-RETURN")
                    self.assertEqual(runtime.scheduler.alternation_phase, "franta_drain")
                return result

            runtime = FrantaRuntime.initialize(load_manifest(_manifest(Path(raw))), executor=executor)
            try:
                _enter_franta(runtime)
                task_id = runtime.scheduler.submit_batch(
                    "BATCH-BUDGET-RETURN", [_brainstorm_assignment(runtime, "BUDGET-RETURN")]
                )[0]
                runtime.run(max_cycles=1)
                self.assertEqual(invoked, ["worker"])
                self.assertEqual(runtime.scheduler.alternation_phase, "franta_drain")
                self.assertEqual(runtime.scheduler.state["tasks"][task_id]["state"], "closed")
                self.assertFalse(runtime._run_main())
                self.assertFalse(runtime._run_trim_review())
                self.assertFalse(any(
                    call["kind"] in {"main", "trimmer"}
                    for call in runtime.scheduler.state["calls"].values()
                ))
                runtime.run(max_cycles=1)
                self.assertEqual(runtime.scheduler.alternation_phase, "explorer_admission")
                self.assertEqual(runtime.scheduler.state["phase_control"]["cycle"], 2)
            finally:
                runtime.close()

    def test_previously_admitted_pending_worker_finishes_before_phase_handoff(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            invoked: list[str] = []

            def executor(call: AgentCall) -> dict[str, Any]:
                invoked.append(call.kind)
                return _deadline_test_result(runtime, call)

            runtime = FrantaRuntime.initialize(load_manifest(_manifest(Path(raw))), executor=executor)
            try:
                _enter_franta(runtime)
                first, pending = runtime.scheduler.submit_batch(
                    "BATCH-BUDGET-PENDING",
                    [_brainstorm_assignment(runtime, marker) for marker in ("FIRST", "PENDING")],
                )
                _set_limit(runtime, 1, "ATL-PENDING")
                runtime._run_worker_batch([first])
                self.assertEqual(runtime.scheduler.alternation_phase, "franta_drain")
                self.assertEqual(runtime.scheduler.state["tasks"][pending]["attempts"], [])
                self.assertEqual(runtime.scheduler.stop_unlaunched_franta_tasks_for_drain(), ())
                self.assertFalse(runtime._franta_tail_is_drained())
                runtime.run(max_cycles=1)
                self.assertEqual(invoked, ["worker", "worker"])
                for task_id in (first, pending):
                    task = runtime.scheduler.state["tasks"][task_id]
                    self.assertEqual(task["state"], "closed")
                    self.assertEqual(task["final_status"], "failed")
                    self.assertEqual(len(task["attempts"]), 1)
                self.assertFalse(any(
                    call["kind"] in {"main", "trimmer"}
                    for call in runtime.scheduler.state["calls"].values()
                ))
            finally:
                runtime.close()

    def test_stale_completed_controls_are_fenced_and_stay_fenced_after_reopen(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            runtime = FrantaRuntime.initialize(load_manifest(_manifest(Path(raw))))
            try:
                clock = [datetime.now(timezone.utc)]
                runtime.scheduler._reducer_now = lambda now=None: now or clock[0]
                _enter_franta(runtime)
                task_id = runtime.scheduler.submit_batch(
                    "BATCH-BUDGET-FENCE", [_brainstorm_assignment(runtime, "FENCE")]
                )[0]
                runtime.scheduler.start_task_attempt(task_id)
                completed = {}
                for kind in ("main", "trimmer"):
                    call_id = runtime.scheduler.prepare_call(kind, {})
                    epoch, _ = runtime.scheduler.mark_call_running(call_id)
                    runtime.scheduler.accept_call_result(call_id, epoch, {})
                    completed[call_id] = copy.deepcopy(runtime.scheduler.state["calls"][call_id])
                _set_limit(runtime, 1, "ATL-FENCE-CLOSE")
                self.assertEqual(runtime.scheduler.alternation_phase, "franta_drain")
                # Legacy code persisted completed Main after edge-triggered cleanup.
                with runtime.scheduler._mutate() as state:
                    state["calls"].update(copy.deepcopy(completed))
                runtime.scheduler.tick_alternation()
                fenced = runtime.scheduler.state
                for call_id, stale in completed.items():
                    self.assertEqual(fenced["calls"][call_id]["status"], "cancelled")
                    self.assertGreater(fenced["calls"][call_id]["lease_epoch"], stale["lease_epoch"])
                runtime.scheduler.tick_alternation()
                self.assertEqual(runtime.scheduler.state["calls"], fenced["calls"])
                self.assertEqual(runtime.scheduler.state["events"], fenced["events"])
                clock[0] += timedelta(seconds=121)
                _set_limit(runtime, 4, "ATL-FENCE-REOPEN")
                self.assertEqual(runtime.scheduler.alternation_phase, "franta_run")
                self.assertFalse(runtime._recover_control_calls())
                for call_id, stale in completed.items():
                    self.assertEqual(runtime.scheduler.state["calls"][call_id]["status"], "cancelled")
                    with self.assertRaises(StaleLeaseError):
                        runtime.scheduler.accept_call_result(call_id, stale["lease_epoch"], {})
                runtime.scheduler.prepare_call("main", {"fresh": True})
            finally:
                runtime.close()

    def test_first_reopen_tick_fences_legacy_main_before_admission_reopens(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            runtime = FrantaRuntime.initialize(load_manifest(_manifest(Path(raw))))
            try:
                clock = [datetime.now(timezone.utc)]
                runtime.scheduler._reducer_now = lambda now=None: now or clock[0]
                _enter_franta(runtime)
                task_id = runtime.scheduler.submit_batch(
                    "BATCH-FIRST-REOPEN", [_brainstorm_assignment(runtime, "FIRST-REOPEN")]
                )[0]
                runtime.scheduler.start_task_attempt(task_id)
                call_id = runtime.scheduler.prepare_call("main", {})
                epoch, _ = runtime.scheduler.mark_call_running(call_id)
                runtime.scheduler.accept_call_result(call_id, epoch, {})
                stale = copy.deepcopy(runtime.scheduler.state["calls"][call_id])
                _set_limit(runtime, 1, "ATL-FIRST-CLOSE")
                with runtime.scheduler._mutate() as state:
                    state["calls"][call_id] = stale
                clock[0] += timedelta(seconds=121)
                _set_limit(runtime, 4, "ATL-FIRST-REOPEN")
                self.assertEqual(runtime.scheduler.alternation_phase, "franta_run")
                self.assertEqual(runtime.scheduler.state["calls"][call_id]["status"], "cancelled")
                self.assertFalse(runtime._recover_control_calls())
                with self.assertRaises(StaleLeaseError):
                    runtime.scheduler.accept_call_result(call_id, epoch, {})
            finally:
                runtime.close()

    def test_operator_cutoff_during_main_commit_is_race_safe(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            def executor(call: AgentCall) -> dict[str, Any]:
                return _deadline_test_result(runtime, call)

            runtime = FrantaRuntime.initialize(load_manifest(_manifest(Path(raw))), executor=executor)
            editor: threading.Thread | None = None
            errors: list[BaseException] = []
            try:
                _enter_franta(runtime)
                task_id = runtime.scheduler.submit_batch(
                    "BATCH-COMMIT-RACE", [_brainstorm_assignment(runtime, "COMMIT-RACE")]
                )[0]
                runtime._run_worker_batch([task_id])
                directory = dashboard_directory(runtime.layout.root)
                original_validate = runtime._validate_main_task_writing
                validations = 0
                started = threading.Event()
                finished = threading.Event()

                def validate(*args: Any, **kwargs: Any) -> None:
                    nonlocal validations, editor
                    original_validate(*args, **kwargs)
                    validations += 1
                    if validations != 2:
                        return

                    def apply_during_commit() -> None:
                        try:
                            publish_json(directory / "commands" / "ATL-COMMIT-RACE.json", {
                                "command_id": "ATL-COMMIT-RACE", "kind": "attempt_limits",
                                "explorer_limit": 20, "franta_limit": 1,
                                "created_at": (runtime.scheduler._reducer_now() - timedelta(seconds=120)).isoformat(),
                            })
                            started.set()
                            runtime._tick_alternation()
                        except BaseException as exc:
                            errors.append(exc)
                        finally:
                            finished.set()

                    editor = threading.Thread(target=apply_during_commit, daemon=True)
                    editor.start()
                    self.assertTrue(started.wait(5))
                    # A protected commit blocks this edit briefly; an unlocked
                    # commit observes cancellation before consuming its artifacts.
                    finished.wait(0.1)

                runtime._validate_main_task_writing = validate
                self.assertTrue(runtime._run_main())
                self.assertIsNotNone(editor)
                assert editor is not None
                editor.join(timeout=8)
                self.assertFalse(editor.is_alive())
                if errors:
                    raise errors[0]
                self.assertEqual(runtime.scheduler.alternation_phase, "franta_drain")
                mains = [call for call in runtime.scheduler.state["calls"].values() if call["kind"] == "main"]
                self.assertEqual(len(mains), 1)
                self.assertIn(mains[0]["status"], {"committed", "cancelled"})
                self.assertFalse(runtime.scheduler.has_blocking_attention())
            finally:
                if editor is not None:
                    editor.join(timeout=10)
                runtime.close()

    def test_budget_raise_cannot_reopen_explorer_after_repository_freeze(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            runtime = FrantaRuntime.initialize(load_manifest(_manifest(Path(raw))))
            editor: threading.Thread | None = None
            errors: list[BaseException] = []
            try:
                clock = [datetime.now(timezone.utc)]
                runtime.scheduler._reducer_now = lambda now=None: now or clock[0]
                runtime.scheduler.activate_alternation()
                runtime.scheduler.queue_attempt_limits(
                    explorer_limit=1, franta_limit=30, command_id="ATL-FREEZE-CLOSE",
                    submitted_at=(clock[0] - timedelta(seconds=120)).isoformat(),
                )
                runtime.scheduler.tick_alternation()
                lineage_id = runtime.scheduler.admit_explorer_lineage()
                for _ in range(3):
                    attempt = runtime.scheduler.start_explorer_attempt(lineage_id, source_high_water_seq=0)
                    call_id = str(attempt["call_id"])
                    epoch, _ = runtime.scheduler.mark_call_running(call_id)
                    runtime.scheduler.accept_call_result(call_id, epoch, {})
                    runtime.scheduler.commit_explorer_attempt(call_id, outcome="progress")
                self.assertTrue(runtime.scheduler.explorer_is_drained())
                self.assertEqual(runtime.scheduler.alternation_phase, "explorer_drain")
                directory = dashboard_directory(runtime.layout.root)
                service = runtime.explorer_service
                assert service is not None
                original_handoff = service.create_handoff
                started = threading.Event()
                finished = threading.Event()

                def handoff(*args: Any, **kwargs: Any) -> Any:
                    nonlocal editor
                    result = original_handoff(*args, **kwargs)

                    def raise_after_freeze() -> None:
                        try:
                            clock[0] += timedelta(seconds=121)
                            publish_json(directory / "commands" / "ATL-FREEZE-RAISE.json", {
                                "command_id": "ATL-FREEZE-RAISE", "kind": "attempt_limits",
                                "explorer_limit": 20, "franta_limit": 30,
                                "created_at": (clock[0] - timedelta(seconds=120)).isoformat(),
                            })
                            started.set()
                            runtime._tick_alternation()
                        except BaseException as exc:
                            errors.append(exc)
                        finally:
                            finished.set()

                    editor = threading.Thread(target=raise_after_freeze, daemon=True)
                    editor.start()
                    self.assertTrue(started.wait(5))
                    finished.wait(0.1)
                    return result

                service.create_handoff = handoff
                assert runtime.explorer_program is not None
                runtime.explorer_program.advance_turn()
                self.assertIsNotNone(editor)
                assert editor is not None
                editor.join(timeout=8)
                self.assertFalse(editor.is_alive())
                if errors:
                    raise errors[0]
                self.assertEqual(runtime.scheduler.alternation_phase, "franta_sort")
            finally:
                if editor is not None:
                    editor.join(timeout=10)
                runtime.close()

    def test_budget_raise_at_franta_handoff_does_not_abort_the_runner(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            def executor(call: AgentCall) -> dict[str, Any]:
                return _deadline_test_result(runtime, call)

            runtime = FrantaRuntime.initialize(load_manifest(_manifest(Path(raw))), executor=executor)
            editor: threading.Thread | None = None
            errors: list[BaseException] = []
            try:
                clock = [datetime.now(timezone.utc)]
                runtime.scheduler._reducer_now = lambda now=None: now or clock[0]
                _enter_franta(runtime)
                task_id = runtime.scheduler.submit_batch(
                    "BATCH-HANDOFF-RACE", [_brainstorm_assignment(runtime, "HANDOFF-RACE")]
                )[0]
                runtime._run_worker_batch([task_id])
                _set_limit(runtime, 1, "ATL-HANDOFF-CLOSE")
                directory = dashboard_directory(runtime.layout.root)
                original_drained = runtime._franta_tail_is_drained
                started = threading.Event()
                finished = threading.Event()

                def drained() -> bool:
                    nonlocal editor
                    result = original_drained()
                    if not result or editor is not None:
                        return result

                    def raise_at_handoff() -> None:
                        try:
                            clock[0] += timedelta(seconds=121)
                            publish_json(directory / "commands" / "ATL-HANDOFF-RAISE.json", {
                                "command_id": "ATL-HANDOFF-RAISE", "kind": "attempt_limits",
                                "explorer_limit": 20, "franta_limit": 4,
                                "created_at": (clock[0] - timedelta(seconds=120)).isoformat(),
                            })
                            started.set()
                            runtime._tick_alternation()
                        except BaseException as exc:
                            errors.append(exc)
                        finally:
                            finished.set()

                    editor = threading.Thread(target=raise_at_handoff, daemon=True)
                    editor.start()
                    self.assertTrue(started.wait(5))
                    finished.wait(0.1)
                    return result

                runtime._franta_tail_is_drained = drained
                runtime.run(max_cycles=1)
                self.assertIsNotNone(editor)
                assert editor is not None
                editor.join(timeout=8)
                self.assertFalse(editor.is_alive())
                if errors:
                    raise errors[0]
                self.assertIn(runtime.scheduler.alternation_phase, {"franta_run", "explorer_admission"})
                self.assertFalse(runtime.scheduler.has_blocking_attention())
            finally:
                if editor is not None:
                    editor.join(timeout=10)
                runtime.close()

    def test_resume_fences_stale_completed_main_and_opens_fresh_next_cycle(self) -> None:
        for resume_phase in ("franta_drain", "explorer_admission"):
            with self.subTest(resume_phase=resume_phase), tempfile.TemporaryDirectory() as raw:
                invoked: list[tuple[str, str]] = []

                def executor(call: AgentCall) -> dict[str, Any]:
                    invoked.append((call.kind, call.call_id))
                    return _deadline_test_result(runtime, call)

                runtime = FrantaRuntime.initialize(load_manifest(_manifest(Path(raw))), executor=executor)
                try:
                    _enter_franta(runtime)
                    task_id = runtime.scheduler.submit_batch(
                        "BATCH-BUDGET-RESUME", [_brainstorm_assignment(runtime, "RESUME")]
                    )[0]
                    runtime._run_worker_batch([task_id])
                    stale_id = runtime.scheduler.prepare_call(
                        "main", runtime._main_context("BATCH-STALE-BUDGET"),
                        continuation={"reserved_batch_id": "BATCH-STALE-BUDGET", "session_key": "main:project"},
                    )
                    epoch, _ = runtime.scheduler.mark_call_running(stale_id)
                    runtime.scheduler.accept_call_result(stale_id, epoch, {})
                    stale = copy.deepcopy(runtime.scheduler.state["calls"][stale_id])
                    _set_limit(runtime, 1, "ATL-RESUME")
                    self.assertEqual(runtime.scheduler.alternation_phase, "franta_drain")
                    if resume_phase == "explorer_admission":
                        runtime.scheduler.complete_franta_drain()
                    with runtime.scheduler._mutate() as state:
                        state["calls"][stale_id] = stale
                    project = runtime.layout.root
                finally:
                    runtime.close()

                invoked.clear()
                runtime = FrantaRuntime.open(project, executor=executor)
                try:
                    if resume_phase == "franta_drain":
                        runtime.run(resume=True, max_cycles=1)
                    else:
                        self.assertFalse(runtime._recover_control_calls())
                    self.assertEqual(invoked, [])
                    self.assertEqual(runtime.scheduler.state["calls"][stale_id]["status"], "cancelled")
                    self.assertEqual(runtime.scheduler.alternation_phase, "explorer_admission")
                    self.assertEqual(runtime.scheduler.state["phase_control"]["cycle"], 2)
                    _enter_franta(runtime)
                    self.assertTrue(runtime._run_main())
                    self.assertEqual([kind for kind, _ in invoked], ["main"])
                    self.assertNotEqual(invoked[0][1], stale_id)
                    self.assertEqual(runtime.scheduler.state["calls"][invoked[0][1]]["status"], "committed")
                    self.assertEqual(runtime.scheduler.alternation_phase, "franta_run")
                finally:
                    runtime.close()

    def test_blocked_main_observes_dashboard_edit_after_120_seconds(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            entered = threading.Event()
            release = threading.Event()
            errors: list[BaseException] = []
            invoked: list[str] = []
            clock = [datetime.now(timezone.utc)]

            def executor(call: AgentCall) -> dict[str, Any]:
                invoked.append(call.kind)
                if call.kind == "main":
                    entered.set()
                    if not release.wait(12):
                        raise AssertionError("dashboard edit was not applied while Main waited")
                    return {}
                return _deadline_test_result(runtime, call)

            runtime = FrantaRuntime.initialize(load_manifest(_manifest(Path(raw))), executor=executor)
            editor: threading.Thread | None = None
            try:
                runtime.scheduler._reducer_now = lambda now=None: now or clock[0]
                _enter_franta(runtime)
                task_id = runtime.scheduler.submit_batch(
                    "BATCH-BUDGET-BLOCKING", [_brainstorm_assignment(runtime, "BLOCKING")]
                )[0]
                runtime._run_worker_batch([task_id])
                directory = dashboard_directory(runtime.layout.root)

                def edit_while_blocked() -> None:
                    try:
                        if not entered.wait(8):
                            raise AssertionError("Main never entered its blocking executor")
                        publish_json(directory / "commands" / "ATL-BLOCKING.json", {
                            "command_id": "ATL-BLOCKING", "kind": "attempt_limits",
                            "explorer_limit": 20, "franta_limit": 1,
                            "created_at": clock[0].isoformat(),
                        })
                        _wait_for(
                            lambda: (directory / "receipts" / "ATL-BLOCKING.json").exists(),
                            "blocked runtime did not consume the dashboard command",
                        )
                        self.assertEqual(runtime.scheduler.alternation_phase, "franta_run")
                        clock[0] += timedelta(seconds=119)
                        runtime.scheduler.tick_alternation()
                        self.assertEqual(runtime.scheduler.alternation_phase, "franta_run")
                        clock[0] += timedelta(seconds=1)
                        _wait_for(
                            lambda: runtime.scheduler.alternation_phase == "franta_drain",
                            "blocked runtime did not apply the matured attempt limit",
                        )
                    except BaseException as exc:
                        errors.append(exc)
                    finally:
                        release.set()

                editor = threading.Thread(target=edit_while_blocked, daemon=True)
                editor.start()
                runtime.run(max_cycles=1)
                editor.join(timeout=1)
                if errors:
                    raise errors[0]
                self.assertEqual(invoked, ["worker", "main"])
                mains = [call for call in runtime.scheduler.state["calls"].values() if call["kind"] == "main"]
                self.assertEqual(len(mains), 1)
                self.assertEqual(mains[0]["status"], "cancelled")
                self.assertFalse(runtime.scheduler.has_blocking_attention())
                self.assertFalse(runtime._recover_control_calls())
            finally:
                release.set()
                if editor is not None:
                    editor.join(timeout=10)
                runtime.close()


if __name__ == "__main__":
    unittest.main()
