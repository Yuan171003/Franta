from __future__ import annotations

import copy
import inspect
import unittest

import franta.categories as legacy_categories
import franta.trim_category as trim_category
from franta.contracts.workflows import GateState
from franta.scheduler import IdempotencyConflict, Scheduler
from franta.testing import FakeControlStore
from franta.trim_category import control


def _trim_state() -> dict[str, object]:
    return {
        "assignment_reports": [],
        "round": 0,
        "session_id": None,
        "session_rounds": 0,
        "active_review": None,
        "active_trim": None,
        "portfolio": None,
        "portfolio_revision": 0,
    }


class Block9BoundaryTests(unittest.TestCase):
    def test_legacy_category_api_preserves_exact_object_identity(self) -> None:
        self.assertIs(legacy_categories, trim_category.repository)
        self.assertEqual(
            legacy_categories.CATEGORY_SCHEMA_VERSION,
            trim_category.CATEGORY_SCHEMA_VERSION,
        )
        self.assertIs(
            legacy_categories.CATEGORY_MEMBER_TYPES,
            trim_category.CATEGORY_MEMBER_TYPES,
        )
        self.assertIs(legacy_categories.CategoryStore, trim_category.CategoryStore)
        self.assertIs(
            legacy_categories.CategoryOperationResult,
            trim_category.CategoryOperationResult,
        )
        self.assertIs(legacy_categories.render_category, trim_category.render_category)
        self.assertIs(legacy_categories.render_portfolio, trim_category.render_portfolio)

    def test_block9_domain_and_control_do_not_import_orchestrators(self) -> None:
        repository_source = inspect.getsource(trim_category.repository)
        control_source = inspect.getsource(control)
        for forbidden in ("franta.scheduler", "..scheduler", "franta.runtime", "..runtime"):
            self.assertNotIn(forbidden, repository_source)
            self.assertNotIn(forbidden, control_source)
        self.assertNotIn("exploration_control", control_source)
        self.assertNotIn("MemoryStore", control_source)


class PureTrimControlTests(unittest.TestCase):
    def test_crossing_boundary_keeps_the_entire_atomic_batch(self) -> None:
        original = _trim_state()
        first = control.accumulate_assignment_reports(
            original,
            [{"report": index} for index in range(6)],
            interval=8,
            base_event_id=10,
        )
        self.assertEqual(original, _trim_state())
        self.assertIsNone(first.gate_target)
        crossed = control.accumulate_assignment_reports(
            first.trim,
            [{"report": index} for index in range(6, 10)],
            interval=8,
            base_event_id=17,
        )
        self.assertEqual(crossed.gate_target, GateState.REVIEWING_TRIM)
        self.assertEqual(len(crossed.trim["assignment_reports"]), 10)
        self.assertEqual(len(crossed.trim["active_review"]["reports"]), 10)
        self.assertEqual(crossed.trim["active_review"]["base_event_id"], 17)

    def test_stuck_replay_and_changed_replay_match_existing_rules(self) -> None:
        original = _trim_state()
        created = control.receive_stuck_report(
            original,
            gate=GateState.OPEN,
            batch_id="B-STUCK",
            report={"summary": "same obstacle"},
            report_digest="digest-one",
            base_event_id=4,
        )
        self.assertEqual(original, _trim_state())
        self.assertEqual(created.gate_target, GateState.REVIEWING_TRIM)
        replay = control.receive_stuck_report(
            created.trim,
            gate=GateState.REVIEWING_TRIM,
            batch_id="B-STUCK",
            report={"summary": "same obstacle"},
            report_digest="digest-one",
            base_event_id=999,
        )
        self.assertFalse(replay.changed)
        self.assertEqual(replay.trim, created.trim)
        with self.assertRaisesRegex(
            control.TrimIdempotencyError,
            "stuck report replayed with different content",
        ):
            control.receive_stuck_report(
                created.trim,
                gate=GateState.REVIEWING_TRIM,
                batch_id="B-STUCK",
                report={"summary": "changed"},
                report_digest="digest-two",
                base_event_id=999,
            )

    def test_review_phase_and_commit_are_copy_on_write(self) -> None:
        trim = _trim_state()
        trim["active_review"] = {
            "trigger": "stuck",
            "session_id": "TRIM-SESSION-1",
            "session_round": 1,
            "round": 1,
        }
        before = copy.deepcopy(trim)
        decided = control.apply_review_decision(
            trim,
            gate=GateState.REVIEWING_TRIM,
            decision="trim",
            reason="redraw directions",
            cutoff_event_id=11,
        )
        self.assertEqual(trim, before)
        self.assertEqual(decided.gate_target, GateState.TRIMMING)
        self.assertEqual(decided.trim["active_trim"]["phase"], "maintain")
        self.assertEqual(decided.trim["active_trim"]["cutoff_event_id"], 11)

        selected = control.set_trim_phase(
            decided.trim, gate=GateState.TRIMMING, phase="select"
        )
        plan = control.plan_portfolio_commit(
            selected.trim,
            {"snapshot_id": "CP-TEST"},
            gate=GateState.TRIMMING,
            expected_portfolio_revision=0,
            confirmed_through_event_id=11,
            commit_event_cursor=13,
        )
        self.assertIsNotNone(selected.trim["active_trim"])
        self.assertIsNone(plan.trim["active_trim"])
        self.assertEqual(plan.trim["portfolio_revision"], 1)
        self.assertEqual(plan.trim["last_trim"]["commit_event_cursor"], 13)
        self.assertEqual(plan.effect.event_type, "trim_committed")

    def test_root_supersession_only_changes_block9_substate(self) -> None:
        trim = _trim_state()
        trim["active_review"] = {"trigger": "stuck"}
        trim["active_trim"] = {"phase": "maintain"}
        before = copy.deepcopy(trim)
        superseded = control.supersede_for_root_resolution(trim)
        self.assertEqual(trim, before)
        self.assertIsNone(superseded.trim["active_review"])
        self.assertIsNone(superseded.trim["active_trim"])
        self.assertEqual(superseded.trim["portfolio_revision"], 0)


class SchedulerTrimAdapterTests(unittest.TestCase):
    def _scheduler(self) -> Scheduler:
        scheduler = Scheduler(FakeControlStore())
        scheduler.bootstrap(root_problem="Prove the test property.")
        scheduler.commit_initial_trim({"category_ids": []})
        return scheduler

    def test_fourth_review_uses_a_fresh_three_round_session(self) -> None:
        scheduler = self._scheduler()
        sessions: list[str] = []
        for index in range(4):
            scheduler.submit_stuck_report(
                f"B-STUCK-{index}", {"summary": f"obstacle {index}"}
            )
            sessions.append(scheduler.start_trim_review_round())
            self.assertIsNone(
                scheduler.apply_trim_review_decision("no_trim", "retain portfolio")
            )
        self.assertEqual(sessions[0], sessions[1])
        self.assertEqual(sessions[1], sessions[2])
        self.assertNotEqual(sessions[2], sessions[3])
        self.assertEqual(scheduler.state["trim"]["round"], 4)
        self.assertEqual(scheduler.state["trim"]["session_rounds"], 1)

    def test_adapter_preserves_stuck_idempotency_exception(self) -> None:
        scheduler = self._scheduler()
        scheduler.submit_stuck_report("B-STUCK", {"summary": "first"})
        with self.assertRaisesRegex(
            IdempotencyConflict, "stuck report replayed with different content"
        ):
            scheduler.submit_stuck_report("B-STUCK", {"summary": "changed"})

    def test_trim_commit_keeps_call_event_and_gate_order(self) -> None:
        scheduler = self._scheduler()
        scheduler.submit_stuck_report("B-STUCK", {"summary": "stuck"})
        session_id = scheduler.start_trim_review_round()
        scheduler.apply_trim_review_decision("trim", "redraw")
        cutoff = int(scheduler.state["trim"]["active_trim"]["cutoff_event_id"])
        scheduler.set_trim_phase("select")
        call_id = scheduler.prepare_call(
            "trimmer",
            {"phase": "select"},
            continuation={"phase": "select", "session_id": session_id},
        )
        lease_epoch, _ = scheduler.mark_call_running(call_id)
        scheduler.accept_call_result(call_id, lease_epoch, {"decision": "commit"})
        revision = scheduler.commit_trim(
            {"snapshot_id": "CP-ADAPTER"},
            expected_portfolio_revision=1,
            confirmed_through_event_id=cutoff,
            continuation_call_id=call_id,
        )
        state = scheduler.state
        self.assertEqual(revision, 2)
        self.assertEqual(state["trim"]["portfolio"], {"snapshot_id": "CP-ADAPTER"})
        self.assertEqual(state["calls"][call_id]["status"], "committed")
        self.assertEqual(state["gate"], GateState.OPEN.value)
        event_types = [event["type"] for event in state["events"]]
        trim_index = max(
            index for index, value in enumerate(event_types) if value == "trim_committed"
        )
        call_index = max(
            index
            for index, value in enumerate(event_types)
            if value == "call_result_committed"
        )
        gate_index = max(
            index for index, value in enumerate(event_types) if value == "gate_transition"
        )
        self.assertLess(trim_index, call_index)
        self.assertLess(call_index, gate_index)


if __name__ == "__main__":
    unittest.main()
