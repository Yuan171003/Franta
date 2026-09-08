from __future__ import annotations

import copy
from concurrent.futures import Future
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping
import unittest

from explorer_system import control
from explorer_system.program import ExplorerProgram


UTC = timezone.utc
T0 = datetime(2040, 1, 1, tzinfo=UTC)
FOUR_HOURS = 4 * 60 * 60


def _result(call_id: str) -> dict[str, Any]:
    return {
        "attempt_ended": True,
        "final_summary_id": f"summary-{call_id}",
        "root_candidate_scratch_id": None,
        "root_candidate_outcome": None,
        "stop_reason": "attempt_complete",
    }


@dataclass
class _Invocation:
    function: Callable[..., Mapping[str, Any]]
    args: tuple[Any, ...]
    kwargs: dict[str, Any]
    future: Future[Mapping[str, Any]]


class _ManualExecutor:
    """A single-threaded, manually completed executor for scheduler tests."""

    def __init__(
        self,
        owner: "_ManualExecutorFactory",
        max_workers: int,
    ) -> None:
        self.owner = owner
        self.max_workers = max_workers
        self.invocations: dict[str, _Invocation] = {}

    def __enter__(self) -> "_ManualExecutor":
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> bool:
        if exc_type is None:
            unfinished = [
                call_id
                for call_id, invocation in self.invocations.items()
                if not invocation.future.done()
            ]
            if unfinished:
                raise AssertionError(
                    f"executor exited before physical calls did: {unfinished}"
                )
        return False

    def shutdown(self, wait: bool = True, *, cancel_futures: bool = False) -> None:
        # Futures are driven explicitly by the test clock.  In particular, this
        # must not turn logical cancellation into physical process completion.
        return None

    def submit(
        self,
        function: Callable[..., Mapping[str, Any]],
        *args: Any,
        **kwargs: Any,
    ) -> Future[Mapping[str, Any]]:
        call_id = str(args[0])
        if call_id in self.invocations:
            raise AssertionError(f"call was submitted twice in one pool: {call_id}")
        future: Future[Mapping[str, Any]] = Future()
        self.invocations[call_id] = _Invocation(
            function=function,
            args=args,
            kwargs=dict(kwargs),
            future=future,
        )
        self.owner.trace.append(("submit", call_id))
        active = len(self.owner.unfinished_call_ids())
        self.owner.max_active = max(self.owner.max_active, active)
        if active > self.max_workers:
            raise AssertionError(
                f"physical concurrency {active} exceeded pool size {self.max_workers}"
            )
        return future


class _ManualExecutorFactory:
    def __init__(self, trace: list[tuple[str, str]]) -> None:
        self.trace = trace
        self.executors: list[_ManualExecutor] = []
        self.max_active = 0

    def __call__(self, *args: Any, **kwargs: Any) -> _ManualExecutor:
        max_workers = int(kwargs.get("max_workers", args[0] if args else 1))
        executor = _ManualExecutor(self, max_workers)
        self.executors.append(executor)
        return executor

    def unfinished_call_ids(self) -> tuple[str, ...]:
        return tuple(
            call_id
            for executor in self.executors
            for call_id, invocation in executor.invocations.items()
            if not invocation.future.done()
        )

    def future_for(self, call_id: str) -> Future[Mapping[str, Any]] | None:
        for executor in reversed(self.executors):
            invocation = executor.invocations.get(call_id)
            if invocation is not None:
                return invocation.future
        return None

    def complete(self, call_id: str) -> None:
        for executor in reversed(self.executors):
            invocation = executor.invocations.get(call_id)
            if invocation is None or invocation.future.done():
                continue
            try:
                value = invocation.function(*invocation.args, **invocation.kwargs)
            except BaseException as exc:
                invocation.future.set_exception(exc)
            else:
                invocation.future.set_result(value)
            return
        raise AssertionError(f"no unfinished physical call {call_id}")


class _FakeHost:
    """Durable portable host with an injected clock and physical executor."""

    def __init__(
        self,
        *,
        max_workers: int,
        max_admissions: int,
        executor_factory: _ManualExecutorFactory,
        admission_seconds: int = 10 * FOUR_HOURS,
        attempt_seconds: int = FOUR_HOURS,
    ) -> None:
        self.max_workers = max_workers
        self.max_admissions = max_admissions
        self.executor_factory = executor_factory
        self.trace = executor_factory.trace
        self.phase: str | None = "explorer_admission"
        self.now = T0
        self.admission_deadline = T0 + timedelta(seconds=admission_seconds)
        self.state = control.initialize_explorer_state(
            attempt_limit=3,
            attempt_seconds=attempt_seconds,
        )
        self.calls: dict[str, dict[str, Any]] = {}
        self.cancelled: list[str] = []
        self.agent_runs: list[str] = []
        self.tick_hook: Callable[["_FakeHost"], None] = lambda _host: None
        self.tick_count = 0

    def tick(self) -> None:
        self.tick_count += 1
        if self.tick_count > 500:
            raise AssertionError("continuous refill made no deterministic progress")
        if self.phase == "explorer_admission" and self.now >= self.admission_deadline:
            self.phase = "explorer_drain"
            self.trace.append(("phase", "explorer_drain"))
        self.tick_hook(self)

    def controller_snapshot(self) -> Mapping[str, Any]:
        return copy.deepcopy(self.state)

    def visible_high_water(self) -> int:
        return 0

    def admit_lineage(self) -> bool:
        if (
            self.phase != "explorer_admission"
            or len(self.state["admission_order"]) >= self.max_admissions
        ):
            return False
        number = len(self.state["admission_order"]) + 1
        lineage_id = f"L{number}"
        transition = control.admit_lineage(
            self.state,
            lineage_id=lineage_id,
            session_key=f"session-{lineage_id}",
            phase_cycle=1,
            phase_epoch=1,
            admission_allowed=True,
            now=self.now,
            max_slots=self.max_workers,
        )
        self.state = transition.state
        self.trace.append(("admit", lineage_id))
        return True

    def _lineage_for_call(self, call_id: str) -> str:
        return str(self.calls[call_id]["lineage_id"])

    def start_attempt(self, lineage_id: str, *, source_high_water_seq: int) -> str:
        if source_high_water_seq != 0:
            raise AssertionError("unexpected provisional high-water mark")
        physically_live_same_lineage = [
            call_id
            for call_id in self.executor_factory.unfinished_call_ids()
            if self.calls.get(call_id, {}).get("lineage_id") == lineage_id
        ]
        if physically_live_same_lineage:
            raise AssertionError(
                "a continuation started before its prior physical call exited: "
                f"{physically_live_same_lineage}"
            )
        lineage = self.state["lineages"][lineage_id]
        attempt_number = int(lineage["attempts_started"]) + 1
        call_id = f"{lineage_id}-A{attempt_number}"
        transition = control.start_attempt(
            self.state,
            lineage_id=lineage_id,
            call_id=call_id,
            now=self.now,
            attempt_limit=3,
            attempt_seconds=int(self.state["settings"]["attempt_seconds"]),
        )
        self.state = transition.state
        self.calls[call_id] = {
            "call_id": call_id,
            "lineage_id": lineage_id,
            "attempt_number": attempt_number,
            "status": "prepared",
            "input": {"explorer_turn_id": "XTURN-1"},
            "result": None,
        }
        self.trace.append(("start", call_id))
        return call_id

    def call_is_launchable(self, call_id: str) -> bool:
        return self.calls[call_id]["status"] in {
            "prepared",
            "retry_pending",
            "completed",
        }

    def prepare_launch(self, call_id: str) -> Mapping[str, Any]:
        return {"call_id": call_id}

    def run_launch(
        self, call_id: str, launch: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        if launch != {"call_id": call_id}:
            raise AssertionError("launch description drifted")
        call = self.calls[call_id]
        self.trace.append(("physical_exit", call_id))
        if call["status"] == "cancelled":
            raise RuntimeError("cancelled transport has now exited")
        if call["status"] == "completed":
            return copy.deepcopy(call["result"])
        if call["status"] not in {"prepared", "retry_pending"}:
            raise AssertionError(f"call {call_id} launched from {call['status']}")
        self.agent_runs.append(call_id)
        call["status"] = "completed"
        call["result"] = _result(call_id)
        return copy.deepcopy(call["result"])

    def call_snapshot(self, call_id: str) -> Mapping[str, Any]:
        return copy.deepcopy(self.calls[call_id])

    def validate_result(self, call_id: str, result: Mapping[str, Any]) -> None:
        if result != _result(call_id):
            raise AssertionError("invalid deterministic Explorer result")

    def fail_attempt(self, call_id: str, *, reason: str) -> None:
        call = self.calls[call_id]
        call["status"] = "failed"
        lineage_id = self._lineage_for_call(call_id)
        transition = control.acknowledge_attempt_end(
            self.state,
            lineage_id=lineage_id,
            call_id=call_id,
            outcome="failed",
            now=self.now,
        )
        self.state = transition.state
        self.trace.append(("fail", call_id))

    def commit_attempt(
        self,
        call_id: str,
        *,
        outcome: str,
        root_candidate: Mapping[str, str] | None,
    ) -> tuple[str, ...]:
        if root_candidate is not None:
            raise AssertionError("these tests do not exercise root candidates")
        call = self.calls[call_id]
        if call["status"] == "committed":
            return ()
        if call["status"] != "completed":
            raise AssertionError(f"call {call_id} committed from {call['status']}")
        lineage_id = self._lineage_for_call(call_id)
        transition = control.acknowledge_attempt_end(
            self.state,
            lineage_id=lineage_id,
            call_id=call_id,
            outcome=outcome,
            now=self.now,
        )
        self.state = transition.state
        call["status"] = "committed"
        self.trace.append(("commit", call_id))
        return ()

    def expire_attempts(self) -> tuple[str, ...]:
        requested = control.request_expired_attempt_stops(self.state, now=self.now)
        self.state = requested.state
        expired: list[str] = []
        for call_id in requested.cancel_call_ids:
            call = self.calls[call_id]
            call["status"] = "cancelled"
            lineage_id = self._lineage_for_call(call_id)
            ended = control.acknowledge_attempt_end(
                self.state,
                lineage_id=lineage_id,
                call_id=call_id,
                outcome="timed_out",
                now=self.now,
            )
            self.state = ended.state
            expired.append(call_id)
            self.trace.append(("logical_timeout", call_id))
        return tuple(expired)

    def cancel_call(self, call_id: str, *, reason: str) -> None:
        self.cancelled.append(call_id)
        self.trace.append(("cancel", call_id))

    def explorer_is_drained(self) -> bool:
        return control.drained(self.state)

    def seed_continuation(self) -> str:
        if not self.state["admission_order"]:
            if not self.admit_lineage():
                raise AssertionError("could not seed lineage")
        lineage_id = str(self.state["admission_order"][0])
        call_id = self.start_attempt(lineage_id, source_high_water_seq=0)
        self.calls[call_id]["status"] = "completed"
        self.calls[call_id]["result"] = _result(call_id)
        self.commit_attempt(call_id, outcome="progress", root_candidate=None)
        return call_id


def _complete_everything(
    factory: _ManualExecutorFactory,
) -> Callable[[_FakeHost], None]:
    def complete(_host: _FakeHost) -> None:
        for call_id in factory.unfinished_call_ids():
            factory.complete(call_id)

    return complete


def _program(
    host: _FakeHost, factory: _ManualExecutorFactory
) -> ExplorerProgram:
    return ExplorerProgram(
        host,
        poll_seconds=0,
        executor_factory=factory,
        continuous_refill=True,
    )


class ExplorerProgramContinuousRefillTests(unittest.TestCase):
    def test_fast_lineage_continues_while_slow_peer_is_still_running(self) -> None:
        trace: list[tuple[str, str]] = []
        factory = _ManualExecutorFactory(trace)
        host = _FakeHost(
            max_workers=2,
            max_admissions=2,
            executor_factory=factory,
        )
        stage = {"fast_first_done": False, "observed_refill": False}

        def drive(_host: _FakeHost) -> None:
            pending = set(factory.unfinished_call_ids())
            if (
                not stage["fast_first_done"]
                and {"L1-A1", "L2-A1"} <= pending
            ):
                factory.complete("L1-A1")
                stage["fast_first_done"] = True
                return
            if (
                stage["fast_first_done"]
                and not stage["observed_refill"]
                and {"L1-A2", "L2-A1"} <= pending
            ):
                stage["observed_refill"] = True
                factory.complete("L1-A2")
                factory.complete("L2-A1")
                return
            if stage["observed_refill"]:
                for call_id in factory.unfinished_call_ids():
                    factory.complete(call_id)

        host.tick_hook = drive
        self.assertTrue(_program(host, factory).run_wave())

        self.assertTrue(stage["observed_refill"])
        self.assertLess(
            trace.index(("submit", "L1-A2")),
            trace.index(("physical_exit", "L2-A1")),
        )
        self.assertEqual(factory.max_active, 2)
        self.assertEqual(
            {call_id for event, call_id in trace if event == "submit"},
            {f"L{lineage}-A{attempt}" for lineage in (1, 2) for attempt in (1, 2, 3)},
        )

    def test_closed_lineage_is_replaced_inside_the_same_run_wave(self) -> None:
        trace: list[tuple[str, str]] = []
        factory = _ManualExecutorFactory(trace)
        host = _FakeHost(
            max_workers=1,
            max_admissions=2,
            executor_factory=factory,
        )
        host.tick_hook = _complete_everything(factory)
        program = _program(host, factory)

        self.assertTrue(program.run_wave())
        submissions_after_first_run = tuple(
            call_id for event, call_id in trace if event == "submit"
        )
        self.assertEqual(
            submissions_after_first_run,
            tuple(f"L{lineage}-A{attempt}" for lineage in (1, 2) for attempt in (1, 2, 3)),
        )
        self.assertLess(
            trace.index(("commit", "L1-A3")),
            trace.index(("admit", "L2")),
        )
        self.assertLess(
            trace.index(("admit", "L2")),
            trace.index(("submit", "L2-A1")),
        )
        self.assertFalse(program.run_wave())
        self.assertEqual(
            tuple(call_id for event, call_id in trace if event == "submit"),
            submissions_after_first_run,
        )

    def test_timeout_waits_for_physical_exit_before_same_lineage_continues(self) -> None:
        trace: list[tuple[str, str]] = []
        factory = _ManualExecutorFactory(trace)
        host = _FakeHost(
            max_workers=1,
            max_admissions=1,
            executor_factory=factory,
            attempt_seconds=FOUR_HOURS,
        )
        stage = {"value": 0, "saw_cancelled_future": False}

        def drive(current: _FakeHost) -> None:
            pending = set(factory.unfinished_call_ids())
            if stage["value"] == 0 and "L1-A1" in pending:
                current.now = T0 + timedelta(seconds=FOUR_HOURS - 1)
                stage["value"] = 1
                return
            if stage["value"] == 1:
                self.assertEqual(current.cancelled, [])
                current.now = T0 + timedelta(seconds=FOUR_HOURS)
                stage["value"] = 2
                return
            if stage["value"] == 2 and current.cancelled:
                self.assertIn("L1-A1", pending)
                stage["saw_cancelled_future"] = True
                stage["value"] = 3
                return
            if stage["value"] == 3:
                factory.complete("L1-A1")
                stage["value"] = 4
                return
            if stage["value"] >= 4:
                for call_id in factory.unfinished_call_ids():
                    factory.complete(call_id)

        host.tick_hook = drive
        self.assertTrue(_program(host, factory).run_wave())

        first_attempt = host.state["lineages"]["L1"]["attempts"][0]
        self.assertEqual(
            datetime.fromisoformat(first_attempt["deadline"])
            - datetime.fromisoformat(first_attempt["started_at"]),
            timedelta(hours=4),
        )
        self.assertTrue(stage["saw_cancelled_future"])
        self.assertEqual(host.cancelled, ["L1-A1"])
        self.assertLess(
            trace.index(("logical_timeout", "L1-A1")),
            trace.index(("physical_exit", "L1-A1")),
        )
        self.assertLess(
            trace.index(("physical_exit", "L1-A1")),
            trace.index(("start", "L1-A2")),
        )

    def test_admission_deadline_blocks_replacement_but_not_continuations(self) -> None:
        trace: list[tuple[str, str]] = []
        factory = _ManualExecutorFactory(trace)
        host = _FakeHost(
            max_workers=1,
            max_admissions=2,
            executor_factory=factory,
            admission_seconds=10,
        )
        self.assertTrue(host.admit_lineage())
        stage = {"moved_to_cutoff": False}

        def drive(current: _FakeHost) -> None:
            pending = set(factory.unfinished_call_ids())
            if "L1-A1" in pending and not stage["moved_to_cutoff"]:
                current.now = current.admission_deadline
                stage["moved_to_cutoff"] = True
                return
            if current.phase == "explorer_drain":
                for call_id in factory.unfinished_call_ids():
                    factory.complete(call_id)

        host.tick_hook = drive
        self.assertTrue(_program(host, factory).run_wave())

        self.assertEqual(host.phase, "explorer_drain")
        self.assertEqual(host.state["admission_order"], ["L1"])
        self.assertEqual(
            tuple(call_id for event, call_id in trace if event == "submit"),
            ("L1-A1", "L1-A2", "L1-A3"),
        )
        self.assertLess(
            trace.index(("phase", "explorer_drain")),
            trace.index(("start", "L1-A2")),
        )
        self.assertTrue(host.explorer_is_drained())

    def test_restart_reuses_prepared_retry_and_continuation_state(self) -> None:
        for durable_state in ("prepared", "retry_pending", "continuation_pending"):
            with self.subTest(durable_state=durable_state):
                trace: list[tuple[str, str]] = []
                factory = _ManualExecutorFactory(trace)
                host = _FakeHost(
                    max_workers=1,
                    max_admissions=1,
                    executor_factory=factory,
                )
                self.assertTrue(host.admit_lineage())
                if durable_state == "continuation_pending":
                    existing_call = host.seed_continuation()
                else:
                    existing_call = host.start_attempt("L1", source_high_water_seq=0)
                    host.calls[existing_call]["status"] = durable_state

                # Constructing a new program represents a process restart.  Its
                # only launch plan must be derived from the durable host state.
                persisted_attempts = int(
                    host.state["lineages"]["L1"]["attempts_started"]
                )
                host.tick_hook = _complete_everything(factory)
                program = _program(host, factory)
                self.assertTrue(program.run_wave())
                submissions = [
                    call_id for event, call_id in trace if event == "submit"
                ]

                if durable_state == "continuation_pending":
                    self.assertNotIn(existing_call, submissions)
                    self.assertEqual(submissions, ["L1-A2", "L1-A3"])
                else:
                    self.assertEqual(submissions.count(existing_call), 1)
                    self.assertEqual(host.agent_runs.count(existing_call), 1)
                self.assertEqual(persisted_attempts, 1)
                self.assertEqual(
                    host.state["lineages"]["L1"]["attempts_started"], 3
                )
                self.assertEqual(len(host.calls), 3)

                before_replay = copy.deepcopy(host.state)
                before_submissions = list(submissions)
                replay = _program(host, factory)
                self.assertFalse(replay.run_wave())
                self.assertEqual(host.state, before_replay)
                self.assertEqual(
                    [call_id for event, call_id in trace if event == "submit"],
                    before_submissions,
                )


if __name__ == "__main__":
    unittest.main()
