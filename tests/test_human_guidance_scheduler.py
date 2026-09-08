from __future__ import annotations

import hashlib
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from franta.contracts.workflows import CallState, TaskState
from franta.recovery import stable_digest
from franta.scheduler import IdempotencyConflict, Scheduler, SchedulerError
from franta.testing import FakeControlStore


T0 = datetime(2026, 9, 6, tzinfo=timezone.utc)


def guidance(index: int = 1, *, text: str | None = None) -> dict[str, str]:
    body = text if text is not None else f"尝试建议 {index}：先研究特殊情形。\n"
    return {
        "guidance_id": f"HG-{index}",
        "text": body,
        "sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
        "relative_path": f"human_guidance/HG-{index}.md",
        "received_at": f"2026-09-06T00:00:{index:02d}+00:00",
    }


def report(index: int = 1) -> dict:
    return {
        "report_id": f"AR-{index}",
        "objective": f"Investigate route {index}",
        "if_resume": None,
        "mode": "research",
        "main_route_ids": ["R-test"],
        "main_obligation_ids": [],
        "perspective": None,
        "portfolio": {},
        "reason": "test",
    }


class HumanGuidanceSchedulerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = FakeControlStore()
        self.store.records["R-test"] = {
            "id": "R-test", "type": "route", "active": True, "status": "active"
        }
        self.scheduler = Scheduler(self.store)
        self.scheduler.bootstrap(root_problem="Prove ROOT")
        self.scheduler.commit_initial_trim({"category_ids": []})

    def main(self, batch_id: str = "B-main", **extra: object) -> str:
        return self.scheduler.prepare_call(
            "main",
            {
                "reserved_batch_id": batch_id,
                "free_non_verifier_slots": self.scheduler.free_non_verifier_slots(),
                **extra,
            },
            continuation={"reserved_batch_id": batch_id},
        )

    def complete(self, call_id: str, *, commit: bool = True) -> None:
        lease, _ = self.scheduler.mark_call_running(call_id)
        self.scheduler.accept_call_result(call_id, lease, {"ok": True})
        if commit:
            self.scheduler.mark_call_committed(call_id)

    def enable_explorer(self) -> None:
        self.scheduler.configure_alternation(
            {
                "attempts_per_worker": 3,
                "attempt_seconds": 3 * 60 * 60,
                "explorer_admission_seconds": 12,
                "franta_admission_seconds": 15,
            },
            now=T0,
        )

    def start_explorer(self, lineage_id: str, number: int = 1) -> str:
        mode, variant = {
            1: ("check-result", "check-result-promising"),
            2: ("portfolio", "portfolio-synthesize"),
            3: ("full-memory", "full-memory-adaptive"),
        }[number]
        result = self.scheduler.start_explorer_attempt(
            lineage_id,
            source_high_water_seq=0,
            guidance_variant=variant,
            access_grant={
                "access_policy_version": 2,
                "access_mode": mode,
                "grant_id": f"GRANT-{lineage_id}-{number}",
                "grant_digest": "a" * 64,
            },
            now=T0,
        )
        return result["call_id"]

    def test_empty_and_identical_imports_are_read_only_and_content_is_immutable(self) -> None:
        initial = self.scheduler.state
        revision = self.scheduler.revision
        self.assertEqual(self.scheduler.enqueue_human_guidance([]), ())
        self.assertEqual(self.scheduler.reconcile_human_guidance(), ())
        self.assertEqual(self.scheduler.state, initial)
        self.assertEqual(self.scheduler.revision, revision)

        item = guidance()
        self.assertEqual(self.scheduler.enqueue_human_guidance([item, item]), ("HG-1",))
        snapshot = self.scheduler.state
        revision = self.scheduler.revision
        self.assertEqual(self.scheduler.enqueue_human_guidance([item]), ())
        self.assertEqual(self.scheduler.state, snapshot)
        self.assertEqual(self.scheduler.revision, revision)
        with self.assertRaises(IdempotencyConflict):
            self.scheduler.enqueue_human_guidance([guidance(text="Changed direction")])
        with self.assertRaises(SchedulerError):
            self.scheduler.enqueue_human_guidance([{**guidance(2), "sha256": "b" * 64}])
        self.assertEqual(self.scheduler.state, snapshot)
        self.assertEqual(self.scheduler.revision, revision)

    def test_main_claims_oldest_once_and_retry_preserves_the_frozen_input(self) -> None:
        self.scheduler.enqueue_human_guidance([guidance(2), guidance(1)])
        call_id = self.main()
        frozen = self.scheduler.state["calls"][call_id]
        self.assertEqual(frozen["input"]["human_guidance"], guidance(1))
        self.assertEqual(frozen["input_digest"], stable_digest(frozen["input"]))
        lease, _ = self.scheduler.mark_call_running(call_id)
        self.assertTrue(self.scheduler.mark_call_failed(call_id, lease, "transport lost"))
        self.scheduler = Scheduler(self.store)
        self.assertEqual(self.scheduler.reconcile_human_guidance(), ())
        lease, attempt = self.scheduler.mark_call_running(call_id)
        self.assertEqual(attempt, 2)
        self.assertEqual(self.scheduler.state["calls"][call_id]["input"], frozen["input"])
        self.assertEqual(self.scheduler.state["calls"][call_id]["input_digest"], frozen["input_digest"])
        self.assertEqual(self.scheduler.state["human_guidance_inbox"]["HG-2"]["status"], "pending")
        claimed = [e for e in self.scheduler.state["events"] if e["type"] == "human_guidance_claimed"]
        self.assertEqual(len(claimed), 1)

    def test_prepared_main_does_not_receive_later_guidance(self) -> None:
        call_id = self.main()
        frozen = self.scheduler.state["calls"][call_id]["input"]
        self.scheduler.enqueue_human_guidance([guidance()])
        self.scheduler.reload()
        self.assertEqual(self.scheduler.state["calls"][call_id]["input"], frozen)
        self.assertNotIn("human_guidance", frozen)
        self.complete(call_id)
        next_call = self.main("B-next")
        self.assertEqual(self.scheduler.state["calls"][next_call]["input"]["human_guidance"], guidance())

    def test_other_roles_terminal_main_and_main_without_capacity_do_not_claim(self) -> None:
        self.scheduler.enqueue_human_guidance([guidance()])
        for role in ("advisor-proposal", "advisor-finalize", "main-sort", "trimmer"):
            call_id = self.scheduler.prepare_call(role, {}, retry_limit=2)
            self.assertNotIn("human_guidance", self.scheduler.state["calls"][call_id]["input"])
            self.complete(call_id)
        for extra in ({"terminal_resolution_call": True}, {"free_non_verifier_slots": 0}):
            call_id = self.main(**extra)
            self.assertNotIn("human_guidance", self.scheduler.state["calls"][call_id]["input"])
            self.complete(call_id)
        self.assertEqual(self.scheduler.state["human_guidance_inbox"]["HG-1"]["status"], "pending")

    def test_only_explorer_attempt_one_claims_and_advisor_does_not_consume(self) -> None:
        self.enable_explorer()
        self.scheduler.enqueue_human_guidance([guidance()])
        advisor = self.scheduler.prepare_call("advisor-proposal", {}, retry_limit=2)
        self.complete(advisor)
        lineage = self.scheduler.admit_explorer_lineage(now=T0)
        for number in range(1, 4):
            call_id = self.start_explorer(lineage, number)
            payload = self.scheduler.state["calls"][call_id]["input"]
            if number == 1:
                self.assertEqual(payload["human_guidance"], guidance())
                self.scheduler.enqueue_human_guidance([guidance(2)])
            else:
                self.assertNotIn("human_guidance", payload)
            self.complete(call_id, commit=False)
            self.scheduler.commit_explorer_attempt(call_id, outcome="progress", now=T0)
        next_lineage = self.scheduler.admit_explorer_lineage(now=T0)
        next_call = self.start_explorer(next_lineage)
        self.assertEqual(self.scheduler.state["calls"][next_call]["input"]["human_guidance"], guidance(2))

    def test_parallel_explorer_planning_claims_one_record_only_once(self) -> None:
        self.enable_explorer()
        self.scheduler.enqueue_human_guidance([guidance()])
        lineages = [self.scheduler.admit_explorer_lineage(now=T0) for _ in range(2)]
        with ThreadPoolExecutor(max_workers=2) as pool:
            calls = list(pool.map(self.start_explorer, lineages))
        guided = [call_id for call_id in calls if "human_guidance" in self.scheduler.state["calls"][call_id]["input"]]
        self.assertEqual(len(guided), 1)
        self.assertEqual(self.scheduler.state["human_guidance_inbox"]["HG-1"]["call_id"], guided[0])

    def test_main_batch_binds_only_the_first_card_and_replays_the_same_assignment(self) -> None:
        self.scheduler.enqueue_human_guidance([guidance()])
        call_id = self.main()
        self.complete(call_id, commit=False)
        reports = [report(1), report(2)]
        task_ids = self.scheduler.submit_batch("B-main", reports, human_guidance_call_id=call_id)
        first, second = [self.scheduler.state["tasks"][task_id] for task_id in task_ids]
        self.assertEqual(first["task_card"]["human_guidance"], guidance())
        self.assertEqual(first["task_card_digest"], stable_digest(first["task_card"]))
        self.assertNotIn("human_guidance", second["task_card"])
        self.assertEqual(first["assign_record"], reports[0])
        self.assertEqual(self.scheduler.state["human_guidance_inbox"]["HG-1"]["status"], "assigned")
        self.assertEqual(self.scheduler.state["human_guidance_inbox"]["HG-1"]["task_id"], task_ids[0])
        self.scheduler = Scheduler(self.store)
        self.assertEqual(self.scheduler.submit_batch("B-main", reports, human_guidance_call_id=call_id), task_ids)
        self.assertEqual(self.scheduler.reconcile_human_guidance(), ())
        self.scheduler.start_task_attempt(task_ids[0])
        self.assertEqual(self.scheduler.worker_call_lease(task_ids[0])["input"]["task_card"]["human_guidance"], guidance())

    def test_invalid_batch_binding_and_partial_assignment_roll_back(self) -> None:
        self.scheduler.enqueue_human_guidance([guidance()])
        call_id = self.main()
        self.complete(call_id, commit=False)
        before = self.scheduler.state
        revision = self.scheduler.revision
        for batch_id, reports, owner in (
            ("B-wrong", [report()], call_id),
            ("B-main", [report()], "CALL-missing"),
            ("B-main", [], call_id),
            ("B-main", [report(), {**report(2), "mode": "invalid"}], call_id),
        ):
            with self.assertRaises(SchedulerError):
                self.scheduler.submit_batch(batch_id, reports, human_guidance_call_id=owner)
            self.assertEqual(self.scheduler.state, before)
            self.assertEqual(self.scheduler.revision, revision)

    def test_unguided_batches_and_cards_retain_their_original_shape(self) -> None:
        call_id = self.main()
        self.assertNotIn("human_guidance", self.scheduler.state["calls"][call_id]["input"])
        task_id = self.scheduler.submit_batch("B-unguided", [report()])[0]
        self.assertNotIn("human_guidance", self.scheduler.state["tasks"][task_id]["task_card"])
        self.assertNotIn("human_guidance_call_id", self.scheduler.state["batches"]["B-unguided"])
        self.assertNotIn("human_guidance_inbox", self.scheduler.state)

    def test_cancelled_and_superseded_main_claims_requeue_without_changing_frozen_calls(self) -> None:
        self.scheduler.enqueue_human_guidance([guidance()])
        for status in (CallState.CANCELLED.value, CallState.SUPERSEDED.value):
            call_id = self.main(f"B-{status}")
            frozen = self.scheduler.state["calls"][call_id]["input"]
            self.scheduler.mark_call_running(call_id)
            with self.scheduler._mutate() as state:
                state["calls"][call_id]["status"] = status
            self.assertEqual(self.scheduler.reconcile_human_guidance(), ("HG-1",))
            self.assertEqual(self.scheduler.state["calls"][call_id]["input"], frozen)
            row = self.scheduler.state["human_guidance_inbox"]["HG-1"]
            self.assertEqual(row["status"], "pending")
            self.assertNotIn("call_id", row)
            with self.assertRaises(SchedulerError):
                self.scheduler.submit_batch(f"B-{status}", [report()], human_guidance_call_id=call_id)

    def test_cancelled_explorer_requeues_only_if_it_never_started(self) -> None:
        self.enable_explorer()
        self.scheduler.enqueue_human_guidance([guidance(1), guidance(2)])
        for index, started in enumerate((False, True), start=1):
            lineage = self.scheduler.admit_explorer_lineage(now=T0)
            call_id = self.start_explorer(lineage)
            if started:
                self.scheduler.mark_call_running(call_id)
            with self.scheduler._mutate() as state:
                state["calls"][call_id]["status"] = CallState.CANCELLED.value
            expected = ("HG-1",) if not started else ()
            self.assertEqual(self.scheduler.reconcile_human_guidance(), expected)
            if not started:
                # The next lineage receives the reclaimed oldest item.
                self.assertEqual(self.scheduler.state["human_guidance_inbox"]["HG-1"]["status"], "pending")
        self.assertEqual(self.scheduler.state["human_guidance_inbox"]["HG-1"]["status"], "claimed")

    def test_drain_requeues_an_assigned_task_that_never_launched(self) -> None:
        self.scheduler.enqueue_human_guidance([guidance()])
        call_id = self.main()
        self.complete(call_id)
        task_id = self.scheduler.submit_batch("B-main", [report()], human_guidance_call_id=call_id)[0]
        with self.scheduler._mutate() as state:
            state["phase_control"] = {"enabled": True, "phase": "franta_drain"}
        self.assertEqual(self.scheduler.stop_unlaunched_franta_tasks_for_drain(), (task_id,))
        self.assertEqual(self.scheduler.state["tasks"][task_id]["state"], TaskState.CLOSED.value)
        self.assertEqual(self.scheduler.state["tasks"][task_id]["attempts"], [])
        self.assertEqual(self.scheduler.reconcile_human_guidance(), ("HG-1",))
        self.assertEqual(self.scheduler.state["human_guidance_inbox"]["HG-1"]["status"], "pending")

    def test_started_worker_and_retry_pending_worker_keep_their_assignment(self) -> None:
        self.scheduler.enqueue_human_guidance([guidance()])
        call_id = self.main()
        self.complete(call_id)
        task_id = self.scheduler.submit_batch("B-main", [report()], human_guidance_call_id=call_id)[0]
        self.scheduler.start_task_attempt(task_id)
        self.assertEqual(self.scheduler.reconcile_human_guidance(), ())
        self.scheduler.record_worker_interruption(task_id, "transport lost")
        self.assertEqual(self.scheduler.state["tasks"][task_id]["state"], TaskState.RETRY_PENDING.value)
        self.assertEqual(self.scheduler.reconcile_human_guidance(), ())
        with self.scheduler._mutate() as state:
            state["phase_control"] = {"enabled": True, "phase": "franta_drain"}
        self.scheduler.stop_unlaunched_franta_tasks_for_drain()
        self.assertEqual(self.scheduler.reconcile_human_guidance(), ())
        self.assertEqual(self.scheduler.state["human_guidance_inbox"]["HG-1"]["task_id"], task_id)


if __name__ == "__main__":
    unittest.main()
