from __future__ import annotations

import copy
import unittest
from typing import Any, Mapping

from franta.recovery import canonical_json
from franta.scheduler import (
    CapacityError,
    IdempotencyConflict,
    Scheduler,
    StaleLeaseError,
)
from franta.testing import FakeControlStore
from franta.workflows import (
    CallState,
    OperationState,
    SchedulerLimits,
    TaskOutcome,
    TaskState,
)


_PORTFOLIO_TYPES = ("fact", "route", "memo", "claim", "obligation", "computation")


def _portfolio(**updates: list[str]) -> dict[str, list[str]]:
    value = {kind: [] for kind in _PORTFOLIO_TYPES}
    value.update(updates)
    return value


class DiscoverySprintLifecycleTests(unittest.TestCase):
    def _ready_scheduler(
        self, *, limits: SchedulerLimits | None = None
    ) -> tuple[FakeControlStore, Scheduler]:
        store = FakeControlStore()
        store.records["O-target"] = {
            "id": "O-target",
            "type": "obligation",
            "revision": 3,
            "statement": "Prove the exact target.",
            "active": True,
            "status": "active",
        }
        for index in range(1, 3):
            store.records[f"R-distant-{index}"] = {
                "id": f"R-distant-{index}",
                "type": "route",
                "revision": 1,
                "abstract": f"Distant route {index}.",
                "active": True,
                "status": "active",
            }
        scheduler = Scheduler(store, limits=limits)
        scheduler.bootstrap(root_problem="Prove ROOT.")
        scheduler.commit_initial_trim({"category_ids": []})
        scheduler.submit_stuck_report("STUCK-SPRINT", {"summary": "Still stuck."})
        scheduler.apply_trim_review_decision("trim", "Run a discovery sprint.")
        return store, scheduler

    @staticmethod
    def _plan() -> dict[str, Any]:
        return {
            "decision": "plan",
            "target_obligation": {
                "id": "O-target",
                "revision": 3,
                "statement": "Prove the exact target.",
            },
            "repeated_mechanism": "One direct reduction.",
            "unchanged_obstacle": "One boundary term.",
            "lanes": [
                {
                    "lane": "A",
                    "mode": "brainstorm",
                    "objective": "Find a clean-room proof.",
                    "main_obligation_ids": ["O-target"],
                    "assignment_portfolio": _portfolio(),
                    "reason": "Supply only the target.",
                },
                {
                    "lane": "B",
                    "mode": "multi-discipline",
                    "objective": "Change representations.",
                    "main_obligation_ids": ["O-target"],
                    "selected_new_perspective": "spectral representation",
                    "assignment_portfolio": _portfolio(),
                    "reason": "Supply one new perspective.",
                },
                {
                    "lane": "C",
                    "mode": "computation",
                    "objective": "Test boundary cases.",
                    "main_obligation_ids": ["O-target"],
                    "computation_portfolio": ["Enumerate the smallest cases."],
                    "assignment_portfolio": _portfolio(),
                    "reason": "Supply one reproducible experiment.",
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

    def test_plan_rejects_a_configuration_that_cannot_launch_four_lanes(self) -> None:
        _store, scheduler = self._ready_scheduler(
            limits=SchedulerLimits(max_non_verifier_workers=3)
        )
        with self.assertRaisesRegex(CapacityError, "exactly four configured"):
            scheduler.persist_sprint_plan("S-BAD-SLOTS", self._plan())
        self.assertNotIn("S-BAD-SLOTS", scheduler.state["sprints"])

    def test_stale_torn_launch_is_fenced_then_cancelled_without_relaunch(self) -> None:
        store, scheduler = self._ready_scheduler()
        plan = self._plan()
        scheduler.persist_sprint_plan("S-STALE-TORN", plan)
        task_ids = scheduler.launch_sprint(
            "S-STALE-TORN",
            self._reports(plan),
            batch_id="B-SPRINT-STALE-TORN",
        )

        loaded = store.load_control_state(Scheduler.CONTROL_KEY)
        assert loaded is not None
        revision, torn = loaded
        torn["sprints"]["S-STALE-TORN"]["status"] = "waiting_for_slots"
        torn["sprints"]["S-STALE-TORN"]["batch_id"] = None
        torn["sprints"]["S-STALE-TORN"]["task_ids"] = []
        store.compare_and_swap_control_state(Scheduler.CONTROL_KEY, revision, torn)
        store.records["O-target"]["revision"] = 4

        resumed = Scheduler(store)
        recovery = resumed.recover()
        self.assertEqual(recovery.relaunch_task_ids, ())
        sprint = resumed.state["sprints"]["S-STALE-TORN"]
        self.assertEqual(sprint["status"], "needs_attention")
        self.assertEqual(sprint["launch_attention"]["reason"], "stale_target")
        self.assertEqual(sprint["task_ids"], task_ids)
        for task_id in task_ids:
            task = resumed.state["tasks"][task_id]
            self.assertEqual(task["state"], TaskState.NEEDS_ATTENTION.value)
            self.assertFalse(task["slot_reserved"])
            self.assertFalse(task["launch_intent"])

        self.assertEqual(
            resumed.cancel_sprint(
                "S-STALE-TORN", authorized_by="operator", reason="Target was revised."
            ),
            "trimmer_continuation",
        )
        cancelled = resumed.state["sprints"]["S-STALE-TORN"]
        self.assertEqual(cancelled["status"], "trimmer_continuation")
        self.assertEqual(len(cancelled["frozen_synthesis_input"]["lane_results"]), 4)
        for lane in cancelled["frozen_synthesis_input"]["lane_results"]:
            self.assertEqual(lane["final_status"], TaskOutcome.INTERRUPTED.value)
            self.assertEqual(
                lane["final_summary"]["kind"], "sprint_cancellation"
            )
        self.assertTrue(
            all(
                resumed.state["tasks"][task_id]["state"] == TaskState.CLOSED.value
                for task_id in task_ids
            )
        )
        self.assertFalse(
            any(
                item["attention_id"] == "sprint:S-STALE-TORN"
                and item["resolved_at"] is None
                for item in resumed.state["needs_attention"]
            )
        )

        restarted = Scheduler(store)
        self.assertEqual(restarted.recover().relaunch_task_ids, ())
        active = restarted.state["trim"]["active_trim"]
        restarted.set_trim_phase("select")
        restarted.commit_trim(
            {"category_ids": []},
            expected_portfolio_revision=1,
            confirmed_through_event_id=int(active["cutoff_event_id"]),
        )
        restarted.complete_sprint_continuation("S-STALE-TORN")
        self.assertEqual(
            restarted.state["sprints"]["S-STALE-TORN"]["status"], "integrated"
        )
        self.assertEqual(
            restarted.cancel_sprint(
                "S-STALE-TORN", authorized_by="operator", reason="Target was revised."
            ),
            "trimmer_continuation",
        )
        with self.assertRaises(IdempotencyConflict):
            restarted.cancel_sprint(
                "S-STALE-TORN", authorized_by="operator", reason="Different reason."
            )

    def test_waiting_sprint_cancellation_routes_directly_to_trimmer(self) -> None:
        _store, scheduler = self._ready_scheduler()
        plan = self._plan()
        scheduler.persist_sprint_plan("S-WAIT-CANCEL", plan)
        self.assertEqual(
            scheduler.cancel_sprint(
                "S-WAIT-CANCEL", authorized_by="operator", reason="Redirect the trim."
            ),
            "trimmer_continuation",
        )
        sprint = scheduler.state["sprints"]["S-WAIT-CANCEL"]
        self.assertEqual(sprint["status"], "trimmer_continuation")
        self.assertEqual(sprint["frozen_synthesis_input"]["lane_results"], [])
        self.assertEqual(
            sprint["frozen_synthesis_input"]["cancellation"]["reason"],
            "Redirect the trim.",
        )

    def test_summary_attention_cancellation_preserves_input_and_fences_call(self) -> None:
        store, scheduler = self._ready_scheduler()
        frozen = {
            "plan": self._plan(),
            "lane_results": [{"lane": "A", "artifact": "preserve exactly"}],
        }
        with scheduler._mutate() as state:
            state["sprints"]["S-SUMMARY-CANCEL"] = {
                "sprint_id": "S-SUMMARY-CANCEL",
                "plan": self._plan(),
                "status": "awaiting_summary",
                "task_ids": [],
                "frozen_synthesis_input": copy.deepcopy(frozen),
                "frozen_synthesis_digest": "original-digest",
                "summary": None,
            }
            state["active_sprint_id"] = "S-SUMMARY-CANCEL"
        call_id = scheduler.prepare_sprint_summarizer_call("S-SUMMARY-CANCEL")
        for _ in range(4):
            lease_epoch, _attempt = scheduler.mark_call_running(call_id)
            scheduler.mark_call_failed(call_id, lease_epoch, "offline")
        self.assertEqual(
            scheduler.state["calls"][call_id]["status"],
            CallState.NEEDS_ATTENTION.value,
        )
        fenced_epoch = int(scheduler.state["calls"][call_id]["lease_epoch"])

        scheduler.cancel_sprint(
            "S-SUMMARY-CANCEL",
            authorized_by="operator",
            reason="Proceed with the four available lane records.",
        )
        sprint = scheduler.state["sprints"]["S-SUMMARY-CANCEL"]
        self.assertEqual(sprint["frozen_synthesis_input"], frozen)
        self.assertEqual(sprint["frozen_synthesis_digest"], "original-digest")
        call = scheduler.state["calls"][call_id]
        self.assertEqual(call["status"], CallState.CANCELLED.value)
        self.assertIn(fenced_epoch, call["fenced_epochs"])
        with self.assertRaises(StaleLeaseError):
            scheduler.accept_call_result(call_id, fenced_epoch, {"late": True})
        self.assertFalse(
            any(
                item["attention_id"] in {f"call:{call_id}", "sprint:S-SUMMARY-CANCEL"}
                and item["resolved_at"] is None
                for item in scheduler.state["needs_attention"]
            )
        )
        recovery = Scheduler(store).recover()
        self.assertNotIn(call_id, recovery.retry_call_ids)
        self.assertNotIn(call_id, recovery.commit_call_ids)

    def test_barrier_freezes_only_lane_owned_terminal_material(self) -> None:
        store, scheduler = self._ready_scheduler()
        plan = self._plan()
        scheduler.persist_sprint_plan("S-FREEZE", plan)
        task_ids = scheduler.launch_sprint(
            "S-FREEZE", self._reports(plan), batch_id="B-SPRINT-FREEZE"
        )
        update_patch = {
            "id": "R-change",
            "abstract": "The accepted updated route.",
            "expected_revision": 1,
        }
        direct_update_patch = {
            "target_id": "R-change",
            "expected_base_revision": 1,
            "set": {"abstract": "A direct lane-authored update."},
            "append": {},
            "add_ids": {},
            "remove_ids": {},
            "explanation": "Apply the lane's direct update.",
            "supporting_memory_ids": [],
        }
        store.records.update(
            {
                "R-change": {
                    "id": "R-change",
                    "type": "route",
                    "revision": 2,
                    "abstract": "The post-update canonical route.",
                },
                "M-duplicate": {
                    "id": "M-duplicate",
                    "type": "memo",
                    "abstract": "DUPLICATE_SNAPSHOT_SENTINEL",
                },
                "C-lane": {
                    "id": "C-lane",
                    "type": "computation",
                    "output": "7",
                },
                "F-challenged": {
                    "id": "F-challenged",
                    "type": "fact",
                    "status": "revoked",
                    "active": False,
                    "statement": "A challenged statement.",
                    "proof": "The invalid proof.",
                },
                "M-unrelated": {
                    "id": "M-unrelated",
                    "type": "memo",
                    "abstract": "UNRELATED_SENTINEL",
                },
            }
        )
        with scheduler._mutate() as state:
            for index, task_id in enumerate(task_ids):
                task = state["tasks"][task_id]
                task["state"] = TaskState.CLOSED.value
                task["slot_reserved"] = False
                task["final_status"] = TaskOutcome.PROGRESS.value
                task["final_summary"] = {"summary": f"Lane {'ABCD'[index]} result."}
            lane_a = state["tasks"][task_ids[0]]
            lane_a["operation_ids"] = [
                "OP-UPDATE",
                "OP-DIRECT-UPDATE",
                "OP-REJECTED-DIRECT-UPDATE",
                "OP-DUPLICATE",
            ]
            state["operations"]["OP-UPDATE"] = {
                "operation_id": "OP-UPDATE",
                "task_id": task_ids[0],
                "kind": "route_add",
                "state": OperationState.COMMITTED.value,
                "canonical_id": "R-change",
                "synthesizer_result": {
                    "resolution": "update",
                    "patch": copy.deepcopy(update_patch),
                },
                "error": None,
            }
            state["operations"]["OP-DUPLICATE"] = {
                "operation_id": "OP-DUPLICATE",
                "task_id": task_ids[0],
                "kind": "memo",
                "state": OperationState.COMMITTED.value,
                "canonical_id": "M-duplicate",
                "synthesizer_result": {"resolution": "duplicate"},
                "error": None,
            }
            state["operations"]["OP-DIRECT-UPDATE"] = {
                "operation_id": "OP-DIRECT-UPDATE",
                "task_id": task_ids[0],
                "kind": "route_update",
                "payload": copy.deepcopy(direct_update_patch),
                "state": OperationState.COMMITTED.value,
                "canonical_id": "R-change",
                "synthesizer_result": None,
                "error": None,
            }
            state["operations"]["OP-REJECTED-DIRECT-UPDATE"] = {
                "operation_id": "OP-REJECTED-DIRECT-UPDATE",
                "task_id": task_ids[0],
                "kind": "route_update",
                "payload": {
                    **copy.deepcopy(direct_update_patch),
                    "set": {"abstract": "This rejected patch was never accepted."},
                },
                "state": OperationState.REJECTED.value,
                "canonical_id": None,
                "synthesizer_result": None,
                "error": "stale base revision",
            }
            lane_c = state["tasks"][task_ids[2]]
            lane_c["computation_ids"] = ["COMP-LANE"]
            state["computations"]["COMP-LANE"] = {
                "staging_id": "COMP-LANE",
                "task_id": task_ids[2],
                "state": OperationState.COMMITTED.value,
                "payload": {"code": "3 + 4", "output": "7"},
                "canonical_id": "C-lane",
                "error": None,
            }
            lane_d = state["tasks"][task_ids[3]]
            lane_d["challenge_ids"] = ["CH-LANE"]
            state["challenges"]["CH-LANE"] = {
                "challenge_id": "CH-LANE",
                "task_id": task_ids[3],
                "state": OperationState.COMMITTED.value,
                "payload": {
                    "challenge_id": "CH-LANE",
                    "fact_id": "F-challenged",
                    "alleged_failure": "The boundary step is false.",
                },
                "resolution": "confirmed_invalid",
                "report": {"justification": "A counterexample invalidates the step."},
                "resolution_tail": {
                    "status": "applied",
                    "kind": "fact_revocation",
                    "operation_id": "challenge-revocation:CH-LANE",
                },
            }
            state["revocation_applications"]["challenge-revocation:CH-LANE"] = {
                "operation_id": "challenge-revocation:CH-LANE",
                "fact_id": "F-challenged",
                "affected_ids": ["F-challenged"],
                "status": "applied",
            }

        self.assertEqual(scheduler.advance_sprint("S-FREEZE"), "awaiting_summary")
        frozen = scheduler.sprint_summary_input("S-FREEZE")
        self.assertEqual([item["lane"] for item in frozen["lane_results"]], list("ABCD"))
        lane_a = frozen["lane_results"][0]
        update = next(
            item for item in lane_a["terminal_operations"]
            if item["operation_id"] == "OP-UPDATE"
        )
        self.assertEqual(update["accepted_update_patch"], update_patch)
        direct_update = next(
            item
            for item in lane_a["terminal_operations"]
            if item["operation_id"] == "OP-DIRECT-UPDATE"
        )
        self.assertEqual(
            direct_update["accepted_update_patch"], direct_update_patch
        )
        rejected_update = next(
            item
            for item in lane_a["terminal_operations"]
            if item["operation_id"] == "OP-REJECTED-DIRECT-UPDATE"
        )
        self.assertIsNone(rejected_update["accepted_update_patch"])
        self.assertNotIn(
            "OP-REJECTED-DIRECT-UPDATE",
            {item["operation_id"] for item in lane_a["canonical_changes"]},
        )
        self.assertEqual(
            lane_a["canonical_changes"][0]["canonical_snapshot"]["revision"], 2
        )
        self.assertNotIn(
            "OP-DUPLICATE",
            {item["operation_id"] for item in lane_a["canonical_changes"]},
        )
        self.assertEqual(
            frozen["lane_results"][2]["computations"][0]["payload"]["output"], "7"
        )
        challenge = frozen["lane_results"][3]["challenges"][0]
        self.assertEqual(challenge["fact_id"], "F-challenged")
        self.assertEqual(challenge["resolution"], "confirmed_invalid")
        self.assertEqual(challenge["revocation_result"]["status"], "applied")
        self.assertEqual(challenge["challenged_fact_snapshot"]["status"], "revoked")
        serialized = canonical_json(frozen)
        self.assertNotIn("UNRELATED_SENTINEL", serialized)
        self.assertNotIn("DUPLICATE_SNAPSHOT_SENTINEL", serialized)

        store.records["R-change"]["abstract"] = "A later canonical edit."
        self.assertEqual(
            scheduler.sprint_summary_input("S-FREEZE"), frozen
        )


if __name__ == "__main__":
    unittest.main()
