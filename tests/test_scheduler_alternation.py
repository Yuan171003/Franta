from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from franta.contracts.workflows import CallState
from franta.explorer_control.state import ExplorerStateError
from franta.phase_control.state import PhaseConflictError
from franta.runtime import FrantaRuntime
from franta.scheduler import Scheduler
from franta.testing import FakeControlStore


UTC = timezone.utc
T0 = datetime(2026, 8, 25, 0, 0, tzinfo=UTC)
EXPLORER_SECONDS = 12
FRANTA_SECONDS = 15


def alternation_settings() -> dict[str, int]:
    return {
        "attempts_per_worker": 3,
        "attempt_seconds": 3 * 60 * 60,
        "explorer_admission_seconds": EXPLORER_SECONDS,
        "franta_admission_seconds": FRANTA_SECONDS,
    }


class SchedulerAlternationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = FakeControlStore()
        self.scheduler = Scheduler(self.store)
        self.scheduler.bootstrap(root_problem="Prove or disprove ROOT")
        self.scheduler.commit_initial_trim({"category_ids": []})

    def _enable(self) -> None:
        self.scheduler.configure_alternation(alternation_settings(), now=T0)

    def _complete_explorer_call(
        self,
        call_id: str,
        *,
        outcome: str = "progress",
        root_candidate: dict[str, str] | None = None,
        now: datetime = T0,
    ) -> tuple[str, ...]:
        lease_epoch, _ = self.scheduler.mark_call_running(call_id)
        self.scheduler.accept_call_result(call_id, lease_epoch, {"ok": True})
        return self.scheduler.commit_explorer_attempt(
            call_id,
            outcome=outcome,
            root_candidate=root_candidate,
            now=now,
        )

    def _close_explorer_admission(self) -> None:
        self.assertEqual(
            self.scheduler.tick_alternation(
                now=T0 + timedelta(seconds=EXPLORER_SECONDS)
            ),
            "explorer_drain",
        )
        self.assertTrue(self.scheduler.explorer_is_drained())

    def _prepare_sort(
        self,
        *,
        sort_run_id: str = "SORT-RUN-1",
        session_key: str = "main:cycle:1",
    ) -> tuple[str, str]:
        task_id = self.scheduler.prepare_main_sort_task(
            sort_run_id=sort_run_id,
            turn_id="XTURN-00000001",
            source_high_water_seq=0,
            source_set_digest="a" * 64,
            snapshot_format_version=1,
            snapshot_digest="b" * 64,
        )
        call_id = self.scheduler.begin_main_sort_task_attempt(
            task_id,
            sort_run_id=sort_run_id,
            session_key=session_key,
            now=T0 + timedelta(seconds=EXPLORER_SECONDS + 1),
        )
        return task_id, call_id

    def test_legacy_scheduler_is_an_opt_in_no_op(self) -> None:
        state_before = self.scheduler.state
        revision_before = self.scheduler.revision

        self.assertIsNone(self.scheduler.alternation_phase)
        self.assertIsNone(self.scheduler.tick_alternation(now=T0 + timedelta(days=1)))
        self.assertEqual(self.scheduler.state, state_before)
        self.assertEqual(self.scheduler.revision, revision_before)
        self.assertNotIn("phase_control", self.scheduler.state)
        self.assertNotIn("explorer_control", self.scheduler.state)

    def test_one_lineage_runs_exactly_three_planned_attempts(self) -> None:
        self._enable()
        lineage_id = self.scheduler.admit_explorer_lineage(now=T0)
        prepared: list[dict[str, object]] = []

        for number in range(1, 4):
            attempt = self.scheduler.start_explorer_attempt(
                lineage_id,
                source_high_water_seq=number,
                now=T0 + timedelta(seconds=number),
            )
            prepared.append(attempt)
            self.assertEqual(attempt["attempt_number"], number)
            self.assertEqual(attempt["first_attempt_clean_room"], number == 1)
            self.assertEqual(
                self._complete_explorer_call(
                    str(attempt["call_id"]),
                    now=T0 + timedelta(seconds=number, microseconds=1),
                ),
                (),
            )

        lineage = self.scheduler.state["explorer_control"]["lineages"][lineage_id]
        self.assertEqual(lineage["attempts_started"], 3)
        self.assertEqual(lineage["status"], "closed")
        self.assertTrue(self.scheduler.explorer_is_drained())
        self.assertEqual(len({item["call_id"] for item in prepared}), 3)
        with self.assertRaises(ExplorerStateError):
            self.scheduler.start_explorer_attempt(
                lineage_id,
                source_high_water_seq=4,
                now=T0 + timedelta(seconds=4),
            )

    def test_sort_and_planning_are_bound_to_the_same_main_session(self) -> None:
        self._enable()
        self._close_explorer_admission()
        _, sort_call_id = self._prepare_sort(session_key="main:cycle:1")
        state_before_conflict = self.scheduler.state

        with self.assertRaises(PhaseConflictError):
            self.scheduler.open_franta_run(
                sort_call_id=sort_call_id,
                planning_call_id="CALL-MAIN-1",
                session_key="main:another-session",
                now=T0 + timedelta(seconds=EXPLORER_SECONDS + 2),
            )
        self.assertEqual(self.scheduler.state, state_before_conflict)

        self.scheduler.open_franta_run(
            sort_call_id=sort_call_id,
            planning_call_id="CALL-MAIN-1",
            session_key="main:cycle:1",
            now=T0 + timedelta(seconds=EXPLORER_SECONDS + 2),
        )
        phase = self.scheduler.state["phase_control"]
        self.assertEqual(phase["phase"], "franta_run")
        self.assertEqual(phase["sort"]["main_session_key"], "main:cycle:1")
        self.assertEqual(phase["sort"]["planning_call_id"], "CALL-MAIN-1")

    def test_root_candidate_cancels_other_calls_and_fully_stops_explorer(self) -> None:
        self._enable()
        first_lineage = self.scheduler.admit_explorer_lineage(now=T0)
        second_lineage = self.scheduler.admit_explorer_lineage(now=T0)
        first = self.scheduler.start_explorer_attempt(
            first_lineage, source_high_water_seq=0, now=T0
        )
        second = self.scheduler.start_explorer_attempt(
            second_lineage, source_high_water_seq=0, now=T0
        )

        cancelled = self._complete_explorer_call(
            str(first["call_id"]),
            root_candidate={
                "candidate_id": "ROOT-CANDIDATE-1",
                "scratch_id": "ES-ROOT-PROOF-1",
                "candidate_outcome": "proved",
            },
            now=T0 + timedelta(seconds=1),
        )

        self.assertEqual(cancelled, (second["call_id"],))
        state = self.scheduler.state
        self.assertEqual(
            state["calls"][str(second["call_id"])]["status"],
            CallState.CANCELLED.value,
        )
        self.assertEqual(state["phase_control"]["phase"], "explorer_drain")
        candidate = state["phase_control"]["explorer"]["root_candidate"]
        self.assertEqual(candidate["status"], "unverified_candidate")
        self.assertIsNone(state["root"].get("solution_fact_id"))
        self.assertTrue(self.scheduler.explorer_is_drained())

        sort_task_id = self.scheduler.prepare_main_sort_task(
            sort_run_id="SORT-ROOT-CANDIDATE",
            turn_id="XTURN-00000001",
            source_high_water_seq=1,
            source_set_digest="c" * 64,
            snapshot_format_version=1,
            snapshot_digest="d" * 64,
        )
        task = self.scheduler.state["tasks"][sort_task_id]
        self.assertEqual(task["task_card"]["root_candidate"], candidate)
        self.assertEqual(task["assign_record"]["root_candidate"], candidate)

        restarted = Scheduler(self.store)
        state_before_replay = restarted.state
        self.assertEqual(
            restarted.commit_explorer_attempt(
                str(first["call_id"]),
                outcome="progress",
                root_candidate={
                    "candidate_id": "ROOT-CANDIDATE-1",
                    "scratch_id": "ES-ROOT-PROOF-1",
                    "candidate_outcome": "proved",
                },
                now=T0 + timedelta(seconds=2),
            ),
            (),
        )
        self.assertEqual(restarted.state, state_before_replay)

    def test_deadlines_close_admission_then_drain_into_the_next_cycle(self) -> None:
        self._enable()
        self.assertEqual(
            self.scheduler.tick_alternation(
                now=T0 + timedelta(seconds=EXPLORER_SECONDS - 1)
            ),
            "explorer_admission",
        )
        self._close_explorer_admission()
        _, sort_call_id = self._prepare_sort()
        franta_started = T0 + timedelta(seconds=EXPLORER_SECONDS + 2)
        self.scheduler.open_franta_run(
            sort_call_id=sort_call_id,
            planning_call_id="CALL-MAIN-1",
            session_key="main:cycle:1",
            now=franta_started,
        )
        self.assertEqual(
            self.scheduler.tick_alternation(
                now=franta_started + timedelta(seconds=FRANTA_SECONDS - 1)
            ),
            "franta_run",
        )
        self.assertEqual(
            self.scheduler.tick_alternation(
                now=franta_started + timedelta(seconds=FRANTA_SECONDS)
            ),
            "franta_drain",
        )

        self.scheduler.complete_franta_drain(
            now=franta_started + timedelta(seconds=FRANTA_SECONDS + 1)
        )
        state = self.scheduler.state
        self.assertEqual(state["phase_control"]["phase"], "explorer_admission")
        self.assertEqual(state["phase_control"]["cycle"], 2)
        self.assertEqual(len(state["phase_control"]["history"]), 1)
        self.assertEqual(len(state["explorer_history"]), 1)
        self.assertEqual(state["explorer_control"]["lineages"], {})

    def test_restart_replays_sort_ownership_and_call_without_duplication(self) -> None:
        self._enable()
        self._close_explorer_admission()
        task_id, call_id = self._prepare_sort()
        state_before_restart = self.scheduler.state

        restarted = Scheduler(self.store)
        restarted.configure_alternation(alternation_settings(), now=T0 + timedelta(days=1))
        replay_task_id = restarted.prepare_main_sort_task(
            sort_run_id="SORT-RUN-1",
            turn_id="XTURN-00000001",
            source_high_water_seq=0,
            source_set_digest="a" * 64,
            snapshot_format_version=1,
            snapshot_digest="b" * 64,
        )
        replay_call_id = restarted.begin_main_sort_task_attempt(
            replay_task_id,
            sort_run_id="SORT-RUN-1",
            session_key="main:cycle:1",
            now=T0 + timedelta(days=1),
        )

        self.assertEqual(replay_task_id, task_id)
        self.assertEqual(replay_call_id, call_id)
        self.assertEqual(len(restarted.state["main_sort_tasks"]), 1)
        self.assertEqual(
            len(
                [
                    item
                    for item in restarted.state["calls"].values()
                    if item.get("kind") == "main-sort"
                ]
            ),
            1,
        )
        self.assertEqual(
            restarted.state["phase_control"], state_before_restart["phase_control"]
        )

    def test_main_sort_uses_no_worker_slot_and_skips_ordinary_selector(self) -> None:
        self._enable()
        self._close_explorer_admission()
        free_before = self.scheduler.free_non_verifier_slots()
        task_id = self.scheduler.prepare_main_sort_task(
            sort_run_id="SORT-RUN-SELECTOR",
            turn_id="XTURN-00000001",
            source_high_water_seq=0,
            source_set_digest="b" * 64,
            snapshot_format_version=1,
            snapshot_digest="c" * 64,
        )
        task = self.scheduler.state["tasks"][task_id]

        self.assertTrue(task["non_slot_task"])
        self.assertFalse(task["slot_reserved"])
        self.assertEqual(self.scheduler.running_non_verifier_count(), 0)
        self.assertEqual(self.scheduler.free_non_verifier_slots(), free_before)

        runtime = object.__new__(FrantaRuntime)
        runtime.scheduler = self.scheduler
        self.assertNotIn(task_id, runtime._ordinary_worker_launches())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
