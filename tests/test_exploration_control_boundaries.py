from __future__ import annotations

import ast
import copy
import json
import unittest
from pathlib import Path

from franta.contracts.workflows import GateState
from franta.exploration_control import runtime as exploration_runtime
from franta.exploration_control import state as exploration_state
from franta.exploration_control import validation as exploration_validation
from franta.scheduler import Scheduler, SchedulerError
from franta.testing import FakeControlStore


def _digest(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _append_event(state, event_type, payload):
    state.setdefault("events", []).append(
        {
            "event_id": len(state.get("events", [])) + 1,
            "type": event_type,
            "payload": copy.deepcopy(dict(payload)),
        }
    )
    return len(state["events"])


class ExplorationControlBoundaryTests(unittest.TestCase):
    def test_block10_modules_do_not_import_implementation_owners(self) -> None:
        root = Path(__file__).parents[1] / "src" / "franta" / "exploration_control"
        forbidden = {
            "franta.scheduler",
            "franta.runtime",
            "franta.store",
            "franta.categories",
            "franta.trim_category",
            "franta.execution_gateway",
        }
        for path in root.glob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            imported: set[str] = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imported.update(alias.name for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imported.add(node.module.lstrip("."))
            with self.subTest(module=path.name):
                self.assertFalse(
                    any(
                        name == owner or name.startswith(owner + ".")
                        for name in imported
                        for owner in forbidden
                    ),
                    imported,
                )

    def test_cancellation_is_planned_without_mutating_shared_state(self) -> None:
        state = {
            "sprints": {
                "S-1": {
                    "sprint_id": "S-1",
                    "status": "waiting_for_slots",
                    "task_ids": [],
                    "plan": {"decision": "plan"},
                    "frozen_synthesis_input": None,
                }
            },
            "tasks": {},
            "calls": {},
        }
        original = copy.deepcopy(state)
        plan = exploration_state.plan_sprint_cancellation(
            state,
            "S-1",
            authorized_by=" operator ",
            reason=" redirect ",
        )
        self.assertEqual(state, original)
        self.assertFalse(plan.replay)
        self.assertEqual(plan.actor, "operator")
        self.assertEqual(plan.reason, "redirect")
        self.assertEqual(plan.task_ids_to_close, ())

        status, event = exploration_state.commit_sprint_cancellation(
            state,
            plan,
            frozen_input={
                "plan": {"decision": "plan"},
                "lane_results": [],
                "cancellation": copy.deepcopy(plan.cancellation),
            },
            cancellation={
                **dict(plan.cancellation or {}),
                "cancelled_at": "2026-08-21T00:00:00+00:00",
            },
            stable_digest=_digest,
        )
        self.assertEqual(status, "trimmer_continuation")
        self.assertEqual(event["authorized_by"], "operator")
        self.assertEqual(
            state["sprints"]["S-1"]["status"], "trimmer_continuation"
        )

        committed = copy.deepcopy(state)
        replay = exploration_state.plan_sprint_cancellation(
            state,
            "S-1",
            authorized_by="operator",
            reason="redirect",
        )
        self.assertTrue(replay.replay)
        self.assertEqual(state, committed)

    def test_guidance_reducer_preserves_pause_resume_and_exact_replay(self) -> None:
        state = {
            "gate": GateState.TRIMMING.value,
            "guidance": {"active": None, "history": []},
            "events": [],
        }

        def transition_gate(current, target):
            current["gate"] = target.value

        exploration_state.request_human_guidance(
            state,
            request_id="HG-1",
            report_ref="private/human-guidance/CALL/report.pdf",
            question="Which route?",
            event_cursor=lambda current: len(current["events"]),
            transition_gate=transition_gate,
            append_event=_append_event,
            stable_digest=_digest,
        )
        self.assertEqual(state["gate"], GateState.WAITING_FOR_HUMAN.value)
        self.assertEqual(state["guidance"]["active"]["snapshot_event_id"], 0)

        state["gate"] = GateState.TRIMMING.value
        exploration_state.request_human_guidance(
            state,
            request_id="HG-1",
            report_ref="private/human-guidance/CALL/report.pdf",
            question="Which route?",
            event_cursor=lambda current: len(current["events"]),
            transition_gate=transition_gate,
            append_event=_append_event,
            stable_digest=_digest,
        )
        self.assertEqual(len(state["events"]), 1)
        state["gate"] = GateState.WAITING_FOR_HUMAN.value

        exploration_state.resolve_human_guidance(
            state,
            request_id="HG-1",
            response="Try the second route.",
            cancelled=False,
            event_cursor=lambda current: len(current["events"]),
            transition_gate=transition_gate,
            append_event=_append_event,
        )
        self.assertEqual(state["gate"], GateState.TRIMMING.value)
        self.assertIsNone(state["guidance"]["active"])
        self.assertEqual(state["guidance"]["history"][0]["status"], "answered")
        self.assertEqual(state["guidance"]["history"][0]["resolved_event_id"], 2)

    def test_task_writing_payload_adapter_is_deterministic_and_nonmutating(self) -> None:
        sprint = {
            "plan": {
                "target_obligation": {"id": "O-1"},
                "lanes": [
                    {
                        "mode": mode,
                        "objective": f"lane {label}",
                        "main_obligation_ids": ["O-1"],
                        "assignment_portfolio": {
                            "fact": [],
                            "route": [],
                            "memo": [],
                            "claim": [],
                            "obligation": [],
                            "computation": [],
                        },
                        "reason": "different mechanism",
                        **(
                            {"selected_new_perspective": "derived geometry"}
                            if label == "B"
                            else {}
                        ),
                        **(
                            {"computation_portfolio": ["enumerate examples"]}
                            if label == "C"
                            else {}
                        ),
                    }
                    for label, mode in zip(
                        "ABCD",
                        ("brainstorm", "multi-discipline", "computation", "associate"),
                        strict=True,
                    )
                ],
            }
        }
        original = copy.deepcopy(sprint)
        first = exploration_runtime.sprint_task_writing_payloads("S-X", sprint)
        second = exploration_runtime.sprint_task_writing_payloads("S-X", sprint)
        self.assertEqual(first, second)
        self.assertEqual(sprint, original)
        self.assertEqual([item["sprint_lane"] for item in first], list("ABCD"))
        self.assertEqual(
            [item["operation_id"] for item in first],
            ["S-X-lane-1", "S-X-lane-2", "S-X-lane-3", "S-X-lane-4"],
        )

    def test_target_validator_preserves_exact_revision_and_statement(self) -> None:
        record = {
            "id": "O-1",
            "type": "obligation",
            "revision": 3,
            "statement": "Prove X.",
            "active": True,
        }
        self.assertEqual(
            exploration_validation.validate_sprint_target(
                {"id": "O-1", "revision": 3, "statement": "Prove X."},
                record_lookup=lambda _memory_id: record,
            ),
            "O-1",
        )
        with self.assertRaisesRegex(
            exploration_validation.ExplorationValidationError,
            "stale sprint target revision",
        ):
            exploration_validation.validate_sprint_target(
                {"id": "O-1", "revision": 2, "statement": "Prove X."},
                record_lookup=lambda _memory_id: record,
            )

    def test_scheduler_maps_a_store_without_get_to_its_legacy_error(self) -> None:
        scheduler = object.__new__(Scheduler)
        scheduler.store = object()
        with self.assertRaisesRegex(
            SchedulerError,
            "invalid sprint target obligation O-1",
        ):
            scheduler._validate_sprint_target(
                {"id": "O-1", "revision": 1, "statement": "Prove X."}
            )

    def test_scheduler_cancellation_replay_ignores_a_stale_summarizer_pointer(
        self,
    ) -> None:
        scheduler = Scheduler(FakeControlStore())
        with scheduler._mutate() as state:
            state["sprints"]["S-REPLAY"] = {
                "sprint_id": "S-REPLAY",
                "status": "integrated",
                "summarizer_call_id": "CALL-MISSING",
                "cancellation": {
                    "authorized_by": "operator",
                    "reason": "redirect",
                    "prior_status": "needs_attention",
                    "result_status": "trimmer_continuation",
                },
            }
            state["needs_attention"].append(
                {
                    "attention_id": "call:CALL-MISSING",
                    "reason": "old",
                    "details": {},
                    "created_at": "earlier",
                    "resolved_at": None,
                }
            )
        before = copy.deepcopy(scheduler.state)
        self.assertEqual(
            scheduler.cancel_sprint(
                "S-REPLAY",
                authorized_by="operator",
                reason="redirect",
            ),
            "trimmer_continuation",
        )
        self.assertEqual(scheduler.state, before)


if __name__ == "__main__":
    unittest.main()
