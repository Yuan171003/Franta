from __future__ import annotations

import copy
import importlib
import os
from pathlib import Path
import subprocess
import sys
import unittest

from franta.contracts.workflows import CallState, GateState
from franta.exploration_control import snapshots as exploration_snapshots
from franta.exploration_control import state as exploration_state
from franta.recovery import append_event, default_scheduler_state, stable_digest
from franta.scheduler import Scheduler
from franta.testing import FakeControlStore
from franta.trim_category import control as trim_control


def _transition_gate(state: dict[str, object], target: GateState) -> None:
    current = GateState(str(state["gate"]))
    if current == target:
        return
    state["gate"] = target.value
    append_event(
        state,
        "gate_transition",
        {"from": current.value, "to": target.value},
    )


def _ready_scheduler() -> Scheduler:
    scheduler = Scheduler(FakeControlStore())
    scheduler.bootstrap(root_problem="Prove ROOT.")
    scheduler.commit_initial_trim({"category_ids": []})
    return scheduler


def _ready_trim_scheduler() -> Scheduler:
    scheduler = _ready_scheduler()
    scheduler.submit_stuck_report("STUCK-GOLD", {"summary": "One mechanism repeats."})
    scheduler.apply_trim_review_decision("trim", "Run a qualitative trim.")
    return scheduler


class Block910ImportBoundaryTests(unittest.TestCase):
    def test_legacy_category_module_is_the_repository_module_object(self) -> None:
        legacy = importlib.import_module("franta.categories")
        repository = importlib.import_module("franta.trim_category.repository")

        self.assertIs(legacy, repository)
        self.assertIs(legacy.CategoryStore, repository.CategoryStore)
        self.assertIs(
            legacy.CategoryOperationResult,
            repository.CategoryOperationResult,
        )
        self.assertIs(legacy.render_category, repository.render_category)
        self.assertIs(legacy.render_portfolio, repository.render_portfolio)

    def test_block_packages_have_no_scheduler_or_runtime_back_edge(self) -> None:
        source_root = Path(__file__).resolve().parents[1] / "src"
        environment = dict(os.environ)
        environment["PYTHONPATH"] = str(source_root)
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        probes = (
            (
                "import sys; import franta.trim_category.control; "
                "blocked={'franta.scheduler','franta.runtime',"
                "'franta.exploration_control'}; "
                "present=sorted(blocked & set(sys.modules)); "
                "raise SystemExit('back edge: '+repr(present) if present else 0)"
            ),
            (
                "import sys; import franta.exploration_control; "
                "blocked={'franta.scheduler','franta.runtime',"
                "'franta.trim_category','franta.store','franta.read_access'}; "
                "present=sorted(blocked & set(sys.modules)); "
                "raise SystemExit('back edge: '+repr(present) if present else 0)"
            ),
        )
        for probe in probes:
            with self.subTest(probe=probe):
                result = subprocess.run(
                    [sys.executable, "-c", probe],
                    capture_output=True,
                    check=False,
                    env=environment,
                    text=True,
                )
                self.assertEqual(
                    result.returncode,
                    0,
                    result.stderr or result.stdout,
                )


class Block9PureControlTests(unittest.TestCase):
    def test_stuck_review_round_and_trim_plan_are_copying_transitions(self) -> None:
        original = default_scheduler_state()["trim"]
        original["assignment_reports"] = [{"report_id": "AR-OLD"}]
        before = copy.deepcopy(original)

        received = trim_control.receive_stuck_report(
            original,
            gate=GateState.OPEN,
            batch_id="B-STUCK",
            report={"summary": "Still stuck."},
            report_digest="digest-stuck",
            base_event_id=41,
        )
        self.assertEqual(original, before)
        self.assertEqual(received.gate_target, GateState.REVIEWING_TRIM)
        self.assertEqual(
            received.effects,
            (
                trim_control.TrimEffect(
                    "stuck_report_received", {"batch_id": "B-STUCK"}
                ),
            ),
        )
        self.assertEqual(
            received.trim["active_review"],
            {
                "trigger": "stuck",
                "batch_id": "B-STUCK",
                "report": {"summary": "Still stuck."},
                "report_digest": "digest-stuck",
                "reports": [{"report_id": "AR-OLD"}],
                "base_event_id": 41,
            },
        )

        started = trim_control.start_review_round(
            received.trim,
            gate=GateState.REVIEWING_TRIM,
            rounds_per_session=3,
            new_session_id="TRIM-SESSION-GOLD",
        )
        self.assertEqual(started.value, "TRIM-SESSION-GOLD")
        self.assertEqual(started.trim["round"], 1)
        self.assertEqual(started.trim["session_rounds"], 1)
        self.assertEqual(
            started.effects[0].payload,
            {
                "session_id": "TRIM-SESSION-GOLD",
                "session_round": 1,
                "round": 1,
            },
        )

        decided = trim_control.apply_review_decision(
            started.trim,
            gate=GateState.REVIEWING_TRIM,
            decision="trim",
            reason="Change direction.",
            cutoff_event_id=45,
        )
        self.assertEqual(decided.gate_target, GateState.TRIMMING)
        self.assertEqual(decided.value, "TRIM-SESSION-GOLD")
        self.assertEqual(
            decided.trim["active_trim"],
            {
                "round": 1,
                "session_id": "TRIM-SESSION-GOLD",
                "session_round": 1,
                "cutoff_event_id": 45,
                "review": {
                    **started.trim["active_review"],
                    "decision": "trim",
                    "reason": "Change direction.",
                },
                "phase": "maintain",
            },
        )

        selected = trim_control.set_trim_phase(
            decided.trim,
            gate=GateState.TRIMMING,
            phase="select",
        )
        commit = trim_control.plan_portfolio_commit(
            selected.trim,
            {"category_ids": ["CAT-GOLD"]},
            gate=GateState.TRIMMING,
            expected_portfolio_revision=0,
            confirmed_through_event_id=47,
            commit_event_cursor=49,
        )
        self.assertEqual(commit.revision, 1)
        self.assertIsNone(commit.trim["active_trim"])
        self.assertEqual(commit.trim["assignment_reports"], [])
        self.assertEqual(commit.trim["portfolio"], {"category_ids": ["CAT-GOLD"]})
        self.assertEqual(commit.active_trim["confirmed_through_event_id"], 47)
        self.assertEqual(commit.active_trim["commit_event_cursor"], 49)
        self.assertEqual(
            commit.effect,
            trim_control.TrimEffect(
                "trim_committed",
                {"portfolio_revision": 1, "confirmed_through": 47},
            ),
        )

    def test_no_trim_effect_intentionally_omits_the_actual_session_id(self) -> None:
        trim = default_scheduler_state()["trim"]
        trim["active_review"] = {
            "trigger": "stuck",
            "session_id": "TRIM-SESSION-GOLD",
            "session_round": 2,
            "round": 2,
        }
        transition = trim_control.apply_review_decision(
            trim,
            gate=GateState.REVIEWING_TRIM,
            decision="no_trim",
            reason="Retain the portfolio.",
            cutoff_event_id=10,
        )

        self.assertEqual(
            transition.trim["last_review"]["session_id"],
            "TRIM-SESSION-GOLD",
        )
        self.assertIsNone(transition.effects[0].payload["session_id"])
        self.assertEqual(transition.gate_target, GateState.OPEN)


class Block10PureControlTests(unittest.TestCase):
    def test_guidance_event_cursors_preserve_the_pre_extraction_offsets(self) -> None:
        state = default_scheduler_state()
        state["gate"] = GateState.TRIMMING.value
        state["events"] = [
            {
                "event_id": 8,
                "type": "prior_event",
                "time": "fixed",
                "payload": {},
            }
        ]

        exploration_state.request_human_guidance(
            state,
            request_id="HG-GOLD",
            report_ref="private/report.pdf",
            question="Which route?",
            event_cursor=lambda value: int(value["events"][-1]["event_id"]),
            transition_gate=_transition_gate,
            append_event=append_event,
            stable_digest=stable_digest,
        )
        active = state["guidance"]["active"]
        self.assertEqual(active["snapshot_event_id"], 8)
        self.assertEqual(state["gate"], GateState.WAITING_FOR_HUMAN.value)
        self.assertEqual(
            [(event["event_id"], event["type"]) for event in state["events"][-2:]],
            [(9, "gate_transition"), (10, "human_guidance_requested")],
        )

        exploration_state.resolve_human_guidance(
            state,
            request_id="HG-GOLD",
            response="Use route B.",
            cancelled=False,
            event_cursor=lambda value: int(value["events"][-1]["event_id"]),
            transition_gate=_transition_gate,
            append_event=append_event,
        )
        history = state["guidance"]["history"]
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["resolved_event_id"], 11)
        self.assertEqual(history[0]["status"], "answered")
        self.assertEqual(
            [(event["event_id"], event["type"]) for event in state["events"][-2:]],
            [(11, "gate_transition"), (12, "human_guidance_resolved")],
        )

    def test_root_supersession_preserves_the_approved_status_and_call_matrix(self) -> None:
        for sprint_status in (
            "planned",
            "waiting_for_slots",
            "running",
            "awaiting_summary",
            "trimmer_continuation",
            "needs_attention",
        ):
            with self.subTest(sprint_status=sprint_status):
                state = default_scheduler_state()
                state["guidance"]["active"] = {
                    "request_id": "HG-ROOT",
                    "status": "waiting",
                }
                state["sprints"]["S-ROOT"] = {
                    "sprint_id": "S-ROOT",
                    "status": sprint_status,
                    "skip_synthesis_and_trim": False,
                }
                state["active_sprint_id"] = "S-ROOT"
                state["calls"] = {
                    "TRIM-P": {"kind": "trimmer", "status": "prepared"},
                    "SUM-R": {"kind": "summarizer", "status": "retry_pending"},
                    "TRIM-R": {"kind": "trimmer", "status": "running"},
                    "MAIN-P": {"kind": "main", "status": "prepared"},
                }
                cancelled: list[dict[str, object]] = []

                def cancel(call: dict[str, object]) -> None:
                    cancelled.append(call)
                    call["status"] = CallState.CANCELLED.value

                exploration_state.supersede_for_root_resolution(
                    state,
                    cancel_call=cancel,
                )

                self.assertIsNone(state["guidance"]["active"])
                self.assertEqual(
                    state["guidance"]["history"][0]["status"],
                    "cancelled_by_root_resolution",
                )
                self.assertEqual(
                    {id(call) for call in cancelled},
                    {
                        id(state["calls"]["TRIM-P"]),
                        id(state["calls"]["SUM-R"]),
                    },
                )
                self.assertEqual(state["calls"]["TRIM-R"]["status"], "running")
                self.assertEqual(state["calls"]["MAIN-P"]["status"], "prepared")
                sprint = state["sprints"]["S-ROOT"]
                if sprint_status in {"planned", "waiting_for_slots"}:
                    self.assertEqual(
                        sprint["status"], "cancelled_by_root_resolution"
                    )
                    self.assertIsNone(state["active_sprint_id"])
                    self.assertFalse(sprint["skip_synthesis_and_trim"])
                else:
                    self.assertEqual(
                        sprint["status"], "draining_after_root_resolution"
                    )
                    self.assertEqual(state["active_sprint_id"], "S-ROOT")
                    self.assertTrue(sprint["skip_synthesis_and_trim"])

    def test_sprint_change_classification_keeps_duplicate_and_update_rules(self) -> None:
        committed = {
            "state": "committed",
            "canonical_id": "R-GOLD",
            "kind": "route_add",
            "synthesizer_result": {"resolution": "duplicate"},
        }
        self.assertIsNone(
            exploration_snapshots.sprint_operation_change_kind(committed)
        )
        committed["duplicate_root_exception"] = True
        self.assertEqual(
            exploration_snapshots.sprint_operation_change_kind(committed),
            "published",
        )
        committed["kind"] = "route_update"
        committed["synthesizer_result"] = None
        self.assertEqual(
            exploration_snapshots.sprint_operation_change_kind(committed),
            "updated",
        )
        committed["kind"] = "obligation_remove"
        self.assertEqual(
            exploration_snapshots.sprint_operation_change_kind(committed),
            "removed",
        )


class SchedulerBlock910AdapterGoldenTests(unittest.TestCase):
    def test_stuck_and_trim_cutoffs_include_the_historical_events(self) -> None:
        scheduler = _ready_scheduler()
        self.assertEqual(scheduler.event_cursor, 3)

        scheduler.submit_stuck_report("B-CUTOFF", {"summary": "Stuck."})
        review = scheduler.state["trim"]["active_review"]
        self.assertEqual(review["base_event_id"], 3)
        self.assertEqual(
            [event["type"] for event in scheduler.state["events"][-2:]],
            ["gate_transition", "stuck_report_received"],
        )
        self.assertEqual(scheduler.state["events"][-2]["event_id"], 4)

        scheduler.apply_trim_review_decision("trim", "Change direction.")
        active = scheduler.state["trim"]["active_trim"]
        self.assertEqual(active["cutoff_event_id"], 7)
        self.assertEqual(
            [event["type"] for event in scheduler.state["events"][-3:]],
            [
                "trim_review_round_started",
                "gate_transition",
                "trim_review_decided",
            ],
        )
        self.assertEqual(scheduler.state["events"][-2]["event_id"], 7)

    def test_four_review_rounds_roll_the_session_and_no_trim_payload_stays_null(self) -> None:
        scheduler = _ready_scheduler()
        observed: list[str] = []
        for round_number in range(1, 5):
            scheduler.submit_stuck_report(
                f"B-ROUND-{round_number}",
                {"summary": f"Round {round_number}."},
            )
            observed.append(scheduler.start_trim_review_round())
            scheduler.apply_trim_review_decision(
                "no_trim", f"Keep round {round_number}."
            )

        self.assertEqual(
            observed,
            [
                "TRIM-SESSION-00000001",
                "TRIM-SESSION-00000001",
                "TRIM-SESSION-00000001",
                "TRIM-SESSION-00000002",
            ],
        )
        decisions = [
            event
            for event in scheduler.state["events"]
            if event["type"] == "trim_review_decided"
        ]
        self.assertEqual(len(decisions), 4)
        self.assertTrue(
            all(event["payload"]["session_id"] is None for event in decisions)
        )
        self.assertEqual(
            scheduler.state["trim"]["last_review"]["session_id"],
            "TRIM-SESSION-00000002",
        )

    def test_guidance_adapter_keeps_snapshot_and_resolution_event_offsets(self) -> None:
        scheduler = _ready_trim_scheduler()
        before_request = scheduler.event_cursor
        scheduler.request_human_guidance(
            "HG-ADAPTER",
            "private/report.pdf",
            "Which route?",
        )
        self.assertEqual(
            scheduler.state["guidance"]["active"]["snapshot_event_id"],
            before_request,
        )
        self.assertEqual(
            [event["type"] for event in scheduler.state["events"][-2:]],
            ["gate_transition", "human_guidance_requested"],
        )

        scheduler.resolve_human_guidance(
            "HG-ADAPTER",
            response="Use route B.",
        )
        history = scheduler.state["guidance"]["history"]
        self.assertEqual(history[-1]["resolved_event_id"], scheduler.event_cursor - 1)
        self.assertEqual(
            scheduler.state["events"][-2]["event_id"],
            history[-1]["resolved_event_id"],
        )
        self.assertEqual(
            [event["type"] for event in scheduler.state["events"][-2:]],
            ["gate_transition", "human_guidance_resolved"],
        )

    def test_root_resolution_keeps_the_approved_call_cancellation_matrix(self) -> None:
        cases = (
            ("trimmer", "prepared", "cancelled"),
            ("trimmer", "retry_pending", "cancelled"),
            ("trimmer", "running", "running"),
            ("trimmer", "completed", "completed"),
            ("summarizer", "prepared", "cancelled"),
            ("summarizer", "retry_pending", "cancelled"),
            ("summarizer", "running", "running"),
            ("summarizer", "completed", "completed"),
            ("main", "prepared", "prepared"),
            ("main-closure-review", "retry_pending", "retry_pending"),
        )
        for kind, initial_status, expected_status in cases:
            with self.subTest(kind=kind, initial_status=initial_status):
                scheduler = _ready_trim_scheduler()
                call_id = scheduler.prepare_call(kind, {"case": initial_status})
                if initial_status in {"running", "completed", "retry_pending"}:
                    lease_epoch, _attempt = scheduler.mark_call_running(call_id)
                if initial_status == "completed":
                    scheduler.accept_call_result(
                        call_id,
                        lease_epoch,
                        {"result": "complete"},
                    )
                elif initial_status == "retry_pending":
                    scheduler.mark_call_failed(call_id, lease_epoch, "offline")

                before = copy.deepcopy(scheduler.state["calls"][call_id])
                event_count = len(scheduler.state["events"])
                with scheduler._mutate() as state:
                    scheduler._record_root_resolution_in_state(
                        state,
                        "F-ROOT",
                        "proved",
                        operation_id="OP-ROOT",
                    )

                after = scheduler.state["calls"][call_id]
                self.assertEqual(after["status"], expected_status)
                self.assertEqual(after["lease_epoch"], before["lease_epoch"])
                self.assertEqual(after["fenced_epochs"], before["fenced_epochs"])
                self.assertEqual(scheduler.gate, GateState.RESOLUTION_PENDING)
                self.assertEqual(
                    [
                        event["type"]
                        for event in scheduler.state["events"][event_count:]
                    ],
                    ["gate_transition", "root_resolution_first"],
                )

    def test_root_resolution_archives_guidance_without_a_resolution_event(self) -> None:
        scheduler = _ready_trim_scheduler()
        scheduler.request_human_guidance("HG-ROOT", "report.pdf", "Choose.")
        event_count = len(scheduler.state["events"])

        with scheduler._mutate() as state:
            scheduler._record_root_resolution_in_state(
                state,
                "F-ROOT",
                "proved",
                operation_id="OP-ROOT",
            )

        self.assertIsNone(scheduler.state["guidance"]["active"])
        self.assertEqual(
            scheduler.state["guidance"]["history"][-1]["status"],
            "cancelled_by_root_resolution",
        )
        self.assertIsNone(scheduler.state["trim"]["active_trim"])
        self.assertEqual(
            [event["type"] for event in scheduler.state["events"][event_count:]],
            ["gate_transition", "root_resolution_first"],
        )


if __name__ == "__main__":
    unittest.main()
