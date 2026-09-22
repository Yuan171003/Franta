from __future__ import annotations

import copy
import unittest
from datetime import datetime, timedelta, timezone

from franta.attempt_budgets import attempt_budget_status
from franta.contracts.configuration import ExplorerSettings
from franta.contracts.workflows import WorkflowError
from franta.phase_control import state as phase_control
from franta.scheduler import Scheduler
from franta.testing import FakeControlStore


T0 = datetime(2040, 1, 1, tzinfo=timezone.utc)


def settings(*, budgets: bool = True, explorer: int = 20, franta: int = 30) -> dict[str, int]:
    result = {
        "attempts_per_worker": 3, "attempt_seconds": 3600,
        "explorer_admission_seconds": 10, "franta_admission_seconds": 10,
    }
    if budgets:
        result.update(explorer_attempt_limit=explorer, franta_attempt_limit=franta)
    return result


class AttemptBudgetTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = FakeControlStore()
        self.scheduler = Scheduler(self.store)
        self.scheduler.bootstrap(root_problem="Test ROOT")
        obligation = self.scheduler.state["root"]["obligation_id"]
        self.store.records[obligation] = {"id": obligation, "type": "obligation", "statement": "Test ROOT"}
        self.scheduler.commit_initial_trim({"category_ids": []})
        self.now = T0
        self.scheduler._reducer_now = lambda now=None: self.now if now is None else now

    def enable(self, *, explorer: int = 20, franta: int = 30, budgets: bool = True) -> None:
        self.scheduler.configure_alternation(settings(budgets=budgets, explorer=explorer, franta=franta), now=T0)

    def edit(self, command: str, *, explorer: int = 20, franta: int = 30, at: datetime | None = None) -> bool:
        return self.scheduler.queue_attempt_limits(
            explorer_limit=explorer, franta_limit=franta,
            command_id=command, submitted_at=at or self.now,
        )

    def finish_explorer(self, call_id: str) -> None:
        epoch, _ = self.scheduler.mark_call_running(call_id)
        self.scheduler.accept_call_result(call_id, epoch, {"ok": True})
        self.scheduler.commit_explorer_attempt(call_id, outcome="progress", now=self.now)

    def enter_franta(self) -> None:
        with self.scheduler._mutate() as state:
            state["phase_control"] = phase_control.request_explorer_drain(
                state["phase_control"], reason="test_handoff", now=self.now,
            ).state
        task_id = self.scheduler.prepare_main_sort_task(
            sort_run_id="SORT-1", turn_id="XTURN-00000001",
            source_high_water_seq=0, source_set_digest="a" * 64,
            snapshot_format_version=1, snapshot_digest="b" * 64,
        )
        call_id = self.scheduler.begin_main_sort_task_attempt(
            task_id, sort_run_id="SORT-1", session_key="main:cycle:1", now=self.now,
        )
        self.scheduler.open_franta_run(
            sort_call_id=call_id, planning_call_id="MAIN-SEED",
            session_key="main:cycle:1", now=self.now,
        )

    def admit_tasks(self, count: int = 1, *, batch: str = "BATCH-1") -> list[str]:
        return self.scheduler.submit_batch(batch, [
            {
                "report_id": f"AR-{batch}-{index}", "objective": "Investigate ROOT",
                "if_resume": None, "mode": "brainstorm", "main_route_ids": [],
                "main_obligation_ids": [self.scheduler.state["root"]["obligation_id"]],
                "perspective": None, "portfolio": {
                    kind: [] for kind in ("fact", "route", "memo", "claim", "obligation", "computation")
                }, "reason": "Exercise admitted worker continuation",
            }
            for index in range(count)
        ])

    def test_defaults_and_old_admission_clocks_no_longer_close_budget_turn(self) -> None:
        config = ExplorerSettings()
        self.assertEqual((config.explorer_attempt_limit, config.franta_attempt_limit), (20, 30))
        self.enable()
        self.assertEqual(self.scheduler.tick_alternation(now=T0 + timedelta(days=1)), "explorer_admission")
        self.assertEqual(attempt_budget_status(self.scheduler.state)["explorer"]["used"], 0)

    def test_explorer_soft_cutoff_keeps_all_existing_lineage_attempts(self) -> None:
        self.enable(explorer=1)
        lineage = self.scheduler.admit_explorer_lineage(now=self.now)
        for number in range(1, 4):
            attempt = self.scheduler.start_explorer_attempt(lineage, source_high_water_seq=0, now=self.now)
            self.assertEqual(attempt["attempt_number"], number)
            self.assertEqual(self.scheduler.alternation_phase, "explorer_drain")
            self.assertEqual(self.scheduler.state["calls"][attempt["call_id"]]["status"], "prepared")
            self.finish_explorer(attempt["call_id"])
        with self.assertRaises(WorkflowError):
            self.scheduler.admit_explorer_lineage(now=self.now)
        self.assertEqual(attempt_budget_status(self.scheduler.state)["explorer"], {"limit": 1, "used": 3, "completed": 3, "running": 0})

    def test_last_edit_debounces_for_two_minutes_and_stale_commands_cannot_replace_it(self) -> None:
        self.enable()
        self.assertTrue(self.edit("edit-1", explorer=1))
        self.assertTrue(self.edit("edit-2", explorer=8, at=T0 + timedelta(seconds=90)))
        self.assertFalse(self.edit("edit-older", explorer=2, at=T0 + timedelta(seconds=30)))
        self.scheduler.tick_alternation(now=T0 + timedelta(seconds=120))
        status = attempt_budget_status(self.scheduler.state)
        self.assertEqual(status["explorer"]["limit"], 20)
        self.assertEqual(status["pending"]["command_id"], "edit-2")
        self.scheduler.tick_alternation(now=T0 + timedelta(seconds=210))
        status = attempt_budget_status(self.scheduler.state)
        self.assertEqual(status["explorer"]["limit"], 8)
        self.assertIsNone(status["pending"])
        self.assertEqual(status["latest_command_id"], "edit-2")

    def test_invalid_limit_does_not_replace_pending_edit(self) -> None:
        self.enable()
        self.edit("valid", explorer=7)
        before = self.scheduler.state
        for value in (0, -1, True, 1.5):
            with self.assertRaises(ValueError):
                self.edit("invalid", explorer=value, at=T0 + timedelta(seconds=1))
            self.assertEqual(self.scheduler.state, before)

    def test_lowering_then_raising_reopens_current_explorer_without_cancelling_call(self) -> None:
        self.enable()
        lineage = self.scheduler.admit_explorer_lineage(now=self.now)
        call = self.scheduler.start_explorer_attempt(lineage, source_high_water_seq=0, now=self.now)["call_id"]
        self.edit("lower", explorer=1)
        self.now += timedelta(seconds=120)
        self.assertEqual(self.scheduler.tick_alternation(), "explorer_drain")
        self.assertEqual(self.scheduler.state["calls"][call]["status"], "prepared")
        self.edit("raise", explorer=9)
        self.now += timedelta(seconds=120)
        self.assertEqual(self.scheduler.tick_alternation(), "explorer_admission")
        self.assertEqual(self.scheduler.state["calls"][call]["status"], "prepared")

    def test_root_candidate_drain_does_not_reopen(self) -> None:
        self.enable()
        with self.scheduler._mutate() as state:
            state["phase_control"] = phase_control.request_explorer_drain(state["phase_control"], reason="root_candidate", now=self.now).state
        self.edit("raise", explorer=100)
        self.now += timedelta(seconds=120)
        self.assertEqual(self.scheduler.tick_alternation(), "explorer_drain")

    def test_franta_admitted_batch_and_infrastructure_retries_finish_over_limit(self) -> None:
        self.enable(franta=1)
        self.enter_franta()
        first, second = self.admit_tasks(2)
        self.scheduler.start_task_attempt(first)
        self.assertEqual(self.scheduler.alternation_phase, "franta_drain")
        self.assertTrue(self.scheduler.franta_worker_admission_open(second))
        self.assertEqual(self.scheduler.stop_unlaunched_franta_tasks_for_drain(), ())
        self.scheduler.start_task_attempt(second)
        with self.assertRaises(WorkflowError):
            self.admit_tasks(batch="BATCH-NEW")
        self.scheduler.record_worker_interruption(first, "test network disconnect")
        self.scheduler.start_task_attempt(first)
        status = attempt_budget_status(self.scheduler.state)
        self.assertEqual(status["franta"]["used"], 2)
        self.assertEqual(status["franta"]["running"], 2)
        attempts = self.scheduler.state["tasks"][first]["attempts"]
        self.assertEqual(attempts[0]["attempt_budget_charge"], attempts[1]["attempt_budget_charge"])

    def test_semantic_revision_is_another_logical_attempt(self) -> None:
        self.enable(franta=1)
        self.enter_franta()
        task_id = self.admit_tasks()[0]
        self.scheduler.start_task_attempt(task_id)
        with self.scheduler._mutate() as state:
            task = state["tasks"][task_id]
            task["state"] = "revision_pending"
            task["attempts"][-1]["state"] = "finished"
            task["pending_attempt_supplement"] = {"kind": "verifier_revision"}
        self.scheduler.start_task_attempt(task_id)
        self.assertEqual(attempt_budget_status(self.scheduler.state)["franta"]["used"], 2)

    def test_cutoff_cancels_main_and_reopen_cannot_resurrect_it(self) -> None:
        self.enable(franta=1)
        self.enter_franta()
        main = self.scheduler.prepare_call("main", {"test": True})
        task_id = self.admit_tasks()[0]
        self.scheduler.start_task_attempt(task_id)
        self.assertEqual(self.scheduler.state["calls"][main]["status"], "cancelled")
        with self.assertRaises(WorkflowError):
            self.scheduler.prepare_call("main", {"test": "closed"})
        self.edit("raise", franta=10)
        self.now += timedelta(seconds=120)
        self.assertEqual(self.scheduler.tick_alternation(), "franta_run")
        self.assertEqual(self.scheduler.state["calls"][main]["status"], "cancelled")
        new_main = self.scheduler.prepare_call("main", {"test": "new"})
        self.assertNotEqual(main, new_main)

    def test_advisor_barrier_blocks_reopening(self) -> None:
        self.enable(franta=1)
        self.enter_franta()
        self.scheduler.start_task_attempt(self.admit_tasks()[0])
        with self.scheduler._mutate() as state:
            state["advisor_control"] = {"active": {"source_cycle": 1}}
        self.edit("raise", franta=10)
        self.now += timedelta(seconds=120)
        self.assertEqual(self.scheduler.tick_alternation(), "franta_drain")

    def test_migration_counts_active_legacy_attempts_and_reconfigure_preserves_edit(self) -> None:
        self.enable(budgets=False)
        self.enter_franta()
        task_id = self.admit_tasks()[0]
        self.scheduler.start_task_attempt(task_id)
        self.scheduler.record_worker_interruption(task_id, "legacy infrastructure retry")
        self.scheduler.start_task_attempt(task_id)
        self.enable()
        self.assertEqual(attempt_budget_status(self.scheduler.state)["franta"]["used"], 1)
        self.edit("operator", franta=8)
        self.now += timedelta(seconds=120)
        self.scheduler.tick_alternation()
        self.scheduler = Scheduler(self.store)
        self.enable()
        self.assertEqual(attempt_budget_status(self.scheduler.state)["franta"]["limit"], 8)
        self.assertEqual(attempt_budget_status(self.scheduler.state)["franta"]["used"], 1)

    def test_next_cycle_resets_used_counts_and_retains_limits(self) -> None:
        self.enable(franta=1)
        self.enter_franta()
        task_id = self.admit_tasks()[0]
        self.scheduler.start_task_attempt(task_id)
        self.scheduler.complete_franta_drain(now=self.now)
        status = attempt_budget_status(self.scheduler.state)
        self.assertEqual(status["franta"]["used"], 0)
        self.assertEqual(status["franta"]["limit"], 1)
        self.assertEqual(self.scheduler.state["phase_control"]["history"][-1]["franta"]["attempts_used"], 1)

    def test_status_projection_never_mutates_durable_state(self) -> None:
        self.enable()
        before = self.scheduler.state
        snapshot = copy.deepcopy(before)
        attempt_budget_status(snapshot)
        self.assertEqual(snapshot, before)

    def test_budget_handoff_archives_sprint_and_trim_only_at_final_boundary(self) -> None:
        self.enable(franta=1)
        self.enter_franta()
        task_id = self.admit_tasks()[0]
        self.scheduler.start_task_attempt(task_id)
        with self.scheduler._mutate() as state:
            state["tasks"][task_id]["state"] = "closed"
            state["tasks"][task_id]["slot_reserved"] = False
            state["tasks"][task_id]["attempts"][-1]["state"] = "finished"
            state["gate"] = "trimming"
            state["trim"]["active_trim"] = {"session_id": "trim-old"}
            state["sprints"]["SPRINT-OLD"] = {"status": "trimmer_continuation", "task_ids": [task_id], "summary": {"bridges": []}}
            state["active_sprint_id"] = "SPRINT-OLD"
        self.edit("raise", franta=10)
        self.now += timedelta(seconds=120)
        self.assertEqual(self.scheduler.tick_alternation(), "franta_run")
        self.assertEqual(self.scheduler.state["active_sprint_id"], "SPRINT-OLD")
        self.edit("lower", franta=1)
        self.now += timedelta(seconds=120)
        self.assertEqual(self.scheduler.tick_alternation(), "franta_drain")
        self.scheduler.complete_franta_drain(now=self.now)
        state = self.scheduler.state
        self.assertIsNone(state["active_sprint_id"])
        self.assertEqual(state["sprints"]["SPRINT-OLD"]["status"], "trimmer_continuation")
        self.assertEqual(state["sprints"]["SPRINT-OLD"]["summary"], {"bridges": []})
        self.assertIn("phase_budget_closure", state["sprints"]["SPRINT-OLD"])
        self.assertEqual(state["gate"], "open")
        self.assertIsNone(state["trim"]["active_trim"])
        self.assertEqual(state["trim"]["phase_budget_history"][-1]["active_trim"], {"session_id": "trim-old"})

    def test_budget_handoff_refuses_unfinished_sprint_postprocessing(self) -> None:
        self.enable(franta=1)
        self.enter_franta()
        task_id = self.admit_tasks()[0]
        self.scheduler.start_task_attempt(task_id)
        with self.scheduler._mutate() as state:
            state["tasks"][task_id]["state"] = "closed"
            state["sprints"]["SPRINT-SUMMARY"] = {"status": "awaiting_summary", "task_ids": [task_id]}
            state["active_sprint_id"] = "SPRINT-SUMMARY"
        with self.assertRaisesRegex(WorkflowError, "postprocessing"):
            self.scheduler.complete_franta_drain(now=self.now)
        self.assertEqual(self.scheduler.alternation_phase, "franta_drain")


if __name__ == "__main__":
    unittest.main()
