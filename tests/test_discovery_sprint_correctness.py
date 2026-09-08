from __future__ import annotations

import copy
import unittest
from typing import Any, Mapping

from franta.access import policy_for
from franta.scheduler import Scheduler, SchedulerError
from franta.testing import FakeControlStore


PORTFOLIO_TYPES = ("fact", "route", "memo", "claim", "obligation", "computation")


def _portfolio(**updates: list[str]) -> dict[str, list[str]]:
    value = {kind: [] for kind in PORTFOLIO_TYPES}
    value.update(updates)
    return value


class _RecordingStore(FakeControlStore):
    def __init__(self) -> None:
        super().__init__()
        self.snapshots: list[dict[str, Any]] = []

    def compare_and_swap_control_state(
        self, key: str, expected_revision: int | None, payload: Mapping[str, Any]
    ) -> int:
        revision = super().compare_and_swap_control_state(
            key, expected_revision, payload
        )
        self.snapshots.append(copy.deepcopy(dict(payload)))
        return revision


class DiscoverySprintCorrectnessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = _RecordingStore()
        self.store.records["O-target"] = {
            "id": "O-target",
            "type": "obligation",
            "revision": 3,
            "statement": "Prove the exact target.",
            "active": True,
            "status": "active",
        }
        for index in range(1, 7):
            self.store.records[f"R-distant-{index}"] = {
                "id": f"R-distant-{index}",
                "type": "route",
                "revision": 1,
                "active": True,
                "status": "active",
            }
        self.scheduler = Scheduler(self.store)
        self.scheduler.bootstrap(root_problem="Prove ROOT.")
        self.scheduler.commit_initial_trim({"category_ids": []})
        self.scheduler.submit_stuck_report(
            "STUCK-SPRINT", {"summary": "One mechanism repeats."}
        )
        self.scheduler.apply_trim_review_decision("trim", "Run a qualitative sprint.")

    def _plan(self) -> dict[str, Any]:
        return {
            "decision": "plan",
            "target_obligation": {
                "id": "O-target",
                "revision": 3,
                "statement": "Prove the exact target.",
            },
            "repeated_mechanism": "The same direct reduction.",
            "unchanged_obstacle": "The same boundary term.",
            "lanes": [
                {
                    "lane": "A",
                    "mode": "brainstorm",
                    "objective": "Find a clean-room proof.",
                    "main_obligation_ids": ["O-target"],
                    "assignment_portfolio": _portfolio(),
                    "reason": "Supply only the target and omit project mechanisms.",
                },
                {
                    "lane": "B",
                    "mode": "multi-discipline",
                    "objective": "Translate the target.",
                    "main_obligation_ids": ["O-target"],
                    "selected_new_perspective": "spectral representation",
                    "assignment_portfolio": _portfolio(),
                    "reason": "Supply only the target and one new perspective.",
                },
                {
                    "lane": "C",
                    "mode": "computation",
                    "objective": "Test boundary cases.",
                    "main_obligation_ids": ["O-target"],
                    "computation_portfolio": ["Enumerate the smallest cases."],
                    "assignment_portfolio": _portfolio(),
                    "reason": "Supply an explicit reproducible experiment.",
                },
                {
                    "lane": "D",
                    "mode": "associate",
                    "objective": "Seek a remote bridge.",
                    "main_obligation_ids": ["O-target"],
                    "assignment_portfolio": _portfolio(
                        route=["R-distant-1", "R-distant-2"]
                    ),
                    "reason": "Supply exactly two distant routes.",
                },
            ],
        }

    @staticmethod
    def _reports(plan: Mapping[str, Any]) -> list[dict[str, Any]]:
        reports: list[dict[str, Any]] = []
        for index, lane in enumerate(plan["lanes"]):
            reports.append(
                {
                    "report_id": f"AR-SPRINT-{index}",
                    "sprint_lane": "ABCD"[index],
                    "objective": lane["objective"],
                    "if_resume": None,
                    "mode": lane["mode"],
                    "main_route_ids": [],
                    "main_obligation_ids": ["O-target"],
                    "perspective": lane.get("selected_new_perspective"),
                    "computation_portfolio": lane.get("computation_portfolio"),
                    "portfolio": copy.deepcopy(lane["assignment_portfolio"]),
                    "reason": lane["reason"],
                }
            )
        return reports

    def test_documented_no_sprint_decision_is_persisted(self) -> None:
        status = self.scheduler.persist_sprint_plan(
            "S-NO", {"decision": "no_sprint", "reason": "The mechanisms differ."}
        )
        self.assertEqual(status, "no_sprint")
        self.assertEqual(self.scheduler.state["sprints"]["S-NO"]["status"], "no_sprint")
        self.assertIsNone(self.scheduler.state["active_sprint_id"])

    def test_exact_active_target_is_checked_without_rewriting_revision(self) -> None:
        for field, replacement, message in (
            ("revision", 2, "stale sprint target revision"),
            ("statement", "A different target.", "does not match canonical"),
        ):
            with self.subTest(field=field):
                plan = self._plan()
                plan["target_obligation"][field] = replacement
                with self.assertRaisesRegex(SchedulerError, message):
                    self.scheduler.persist_sprint_plan("S-BAD-TARGET", plan)
        self.store.records["O-target"]["active"] = False
        self.store.records["O-target"]["status"] = "removed"
        with self.assertRaisesRegex(SchedulerError, "inactive"):
            self.scheduler.persist_sprint_plan("S-BAD-TARGET", self._plan())

    def test_lane_target_material_and_access_contracts_are_mechanical(self) -> None:
        invalid_plans: list[tuple[str, dict[str, Any]]] = []
        wrong_target = self._plan()
        wrong_target["lanes"][0]["main_obligation_ids"] = []
        invalid_plans.append(("target", wrong_target))
        broad_access = self._plan()
        broad_access["lanes"][2]["access_policy"] = {
            "canonical_memory": True,
            "internal_search": False,
            "sealed_workspace": True,
        }
        invalid_plans.append(("access", broad_access))
        no_perspective = self._plan()
        no_perspective["lanes"][1]["selected_new_perspective"] = None
        invalid_plans.append(("perspective", no_perspective))
        no_computation_portfolio = self._plan()
        no_computation_portfolio["lanes"][2]["computation_portfolio"] = []
        invalid_plans.append(("computation", no_computation_portfolio))
        short_remote_bundle = self._plan()
        short_remote_bundle["lanes"][3]["assignment_portfolio"] = _portfolio(
            route=["R-distant-1"]
        )
        invalid_plans.append(("remote bundle", short_remote_bundle))
        conflicting_aliases = self._plan()
        conflicting_aliases["lanes"][3]["portfolio"] = _portfolio(
            route=["R-distant-3", "R-distant-4"]
        )
        invalid_plans.append(("conflicting portfolio aliases", conflicting_aliases))
        for label, plan in invalid_plans:
            with self.subTest(label=label):
                with self.assertRaises(SchedulerError):
                    self.scheduler.persist_sprint_plan("S-BAD-LANE", plan)
        self.assertNotIn("S-BAD-LANE", self.scheduler.state["sprints"])

    def test_target_is_rechecked_before_the_waiting_sprint_launches(self) -> None:
        plan = self._plan()
        self.scheduler.persist_sprint_plan("S-STALE-WAIT", plan)
        self.store.records["O-target"]["revision"] = 4
        with self.assertRaisesRegex(SchedulerError, "stale sprint target revision"):
            self.scheduler.launch_sprint(
                "S-STALE-WAIT",
                self._reports(plan),
                batch_id="B-SPRINT-STALE-WAIT",
            )
        self.assertNotIn("B-SPRINT-STALE-WAIT", self.scheduler.state["batches"])
        self.assertEqual(
            self.scheduler.state["sprints"]["S-STALE-WAIT"]["status"],
            "needs_attention",
        )
        self.assertEqual(
            self.scheduler.state["sprints"]["S-STALE-WAIT"]["launch_attention"][
                "reason"
            ],
            "stale_target",
        )
        self.assertTrue(
            any(
                item["attention_id"] == "sprint:S-STALE-WAIT"
                and item["resolved_at"] is None
                for item in self.scheduler.state["needs_attention"]
            )
        )

    def test_launch_is_atomic_and_recovery_restores_lane_identity_before_relaunch(self) -> None:
        plan = self._plan()
        self.scheduler.persist_sprint_plan("S-ATOMIC", plan)
        task_ids = self.scheduler.launch_sprint(
            "S-ATOMIC", self._reports(plan), batch_id="B-SPRINT-ATOMIC"
        )
        self.assertEqual(
            self.scheduler.launch_sprint(
                "S-ATOMIC", self._reports(plan), batch_id="B-SPRINT-ATOMIC"
            ),
            task_ids,
        )
        changed_reports = self._reports(plan)
        changed_reports[0]["objective"] = "A different clean-room objective."
        with self.assertRaises(SchedulerError):
            self.scheduler.launch_sprint(
                "S-ATOMIC", changed_reports, batch_id="B-SPRINT-ATOMIC"
            )
        self.assertFalse(
            any(
                "B-SPRINT-ATOMIC" in snapshot.get("batches", {})
                and snapshot["sprints"]["S-ATOMIC"]["status"] == "waiting_for_slots"
                for snapshot in self.store.snapshots
            )
        )
        for index, task_id in enumerate(task_ids):
            task = self.scheduler.state["tasks"][task_id]
            lane = "ABCD"[index]
            self.assertEqual(task["sprint_lane"], lane)
            self.assertEqual(task["task_card"]["sprint_lane"], lane)
            policy = policy_for("worker", mode=task["task_card"]["mode"], sprint_lane=lane)
            self.assertFalse(policy.project_memory_api)
            self.assertFalse(policy.direct_canonical_mount)

        loaded = self.store.load_control_state(Scheduler.CONTROL_KEY)
        assert loaded is not None
        revision, torn = loaded
        torn["sprints"]["S-ATOMIC"]["status"] = "waiting_for_slots"
        torn["sprints"]["S-ATOMIC"]["batch_id"] = None
        torn["sprints"]["S-ATOMIC"]["task_ids"] = []
        for task_id in task_ids:
            torn["tasks"][task_id].pop("sprint_lane", None)
            torn["tasks"][task_id]["task_card"].pop("sprint_lane", None)
        self.store.compare_and_swap_control_state(
            Scheduler.CONTROL_KEY, revision, torn
        )

        restarted = Scheduler(self.store)
        recovery = restarted.recover()
        self.assertEqual(set(recovery.relaunch_task_ids), set(task_ids))
        sprint = restarted.state["sprints"]["S-ATOMIC"]
        self.assertEqual(sprint["status"], "running")
        self.assertEqual(sprint["task_ids"], task_ids)
        for index, task_id in enumerate(task_ids):
            self.assertEqual(
                restarted.state["tasks"][task_id]["sprint_lane"], "ABCD"[index]
            )


if __name__ == "__main__":
    unittest.main()
