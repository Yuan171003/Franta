from __future__ import annotations

import copy
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from franta.cli import main
from franta.config import load_manifest
from franta.runtime import AgentCall, FrantaRuntime
from franta.scheduler import IdempotencyConflict
from franta.skill_runtime import SkillContext, SkillRuntime
from franta.workflows import GateState, TaskState


def _manifest(root: Path) -> Path:
    path = root / "bootstrap.toml"
    path.write_text(
        "\n".join(
            [
                "[project]",
                'name = "sprint-runtime-test"',
                'directory = "project"',
                'root_problem = "Prove that every test object has property P."',
                'foundation_policy = "Use only the declared definition."',
                "",
                "[context_budgets]",
                "main = 1000",
                "",
                "[initial]",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return path


def _portfolio() -> dict[str, list[str]]:
    return {
        kind: []
        for kind in ("fact", "route", "memo", "claim", "obligation", "computation")
    }


def _persist_sprint(
    runtime: FrantaRuntime,
    sprint_id: str = "S-RUNTIME",
    *,
    predecessor_count: int = 0,
) -> list[str]:
    runtime.scheduler.commit_initial_trim({"category_ids": []})
    root_id = runtime.scheduler.state["root"]["obligation_id"]
    predecessor_task_ids: list[str] = []
    if predecessor_count:
        predecessor_task_ids = runtime.scheduler.submit_batch(
            "B-pre-sprint",
            [
                {
                    "operation_id": f"AR-PRE-{index}",
                    "objective": f"Finish accepted pre-sprint task {index}.",
                    "work_mode": "brainstorm",
                    "if_resume": None,
                    "main_route_ids": [],
                    "main_obligation_ids": [root_id],
                    "selected_new_perspective": None,
                    "assignment_portfolio": _portfolio(),
                    "reason": "This accepted task must drain before a sprint.",
                    "root_solution_fact_id": None,
                }
                for index in range(1, predecessor_count + 1)
            ],
        )
    runtime.scheduler.submit_stuck_report(
        "B-stuck", {"summary": "Recent work repeats one mechanism."}
    )
    runtime.scheduler.apply_trim_review_decision(
        "trim", "Try four isolated approaches."
    )
    root_record = runtime.store.get(root_id).to_dict()
    distant_ids: list[str] = []
    for index in (1, 2):
        result = runtime.store.add_memo(
            f"sprint-runtime-distant-{index}",
            {
                "abstract": f"Distant mechanism {index}",
                "genre": "normal",
                "content": f"A deliberately distant prompt {index}.",
                "related_route_ids": [],
            },
        )
        assert result.canonical_id is not None
        distant_ids.append(result.canonical_id)
    common = {
        "main_obligation_ids": [root_id],
        "assignment_portfolio": _portfolio(),
        "reason": "Keep the lane isolated from existing routes.",
    }
    runtime.scheduler.persist_sprint_plan(
        sprint_id,
        {
            "target_obligation": {
                "id": root_id,
                "revision": root_record["revision"],
                "statement": root_record["statement"],
            },
            "repeated_mechanism": "Direct unfolding",
            "unchanged_obstacle": "The boundary case",
            "lanes": [
                {
                    **common,
                    "mode": "brainstorm",
                    "objective": "Find an independent boundary attack.",
                },
                {
                    **common,
                    "mode": "multi-discipline",
                    "objective": "Translate the boundary case.",
                    "selected_new_perspective": "categorical invariants",
                },
                {
                    **common,
                    "mode": "computation",
                    "objective": "Test explicit boundary examples.",
                    "computation_portfolio": ["Enumerate the smallest examples."],
                },
                {
                    **common,
                    "mode": "associate",
                    "objective": "Seek a remote bridge.",
                    "assignment_portfolio": {
                        **_portfolio(),
                        "memo": distant_ids,
                    },
                },
            ],
        },
    )
    return predecessor_task_ids


def _stage_failed_worker_final(call: AgentCall) -> tuple[dict[str, object], dict[str, object]]:
    if call.kind != "worker":
        raise AssertionError(f"unexpected call kind {call.kind}")
    card = json.loads(call.workspace.task_card_path.read_text(encoding="utf-8"))
    progress_id = f"PRG-{card['task_id']}-{card['attempt']}"
    SkillRuntime(SkillContext.load(call.workspace.path)).invoke(
        "record-progress",
        {
            "operation_id": f"FINAL-{card['task_id']}-{card['attempt']}",
            "progress_id": progress_id,
            "sequence": 1,
            "is_final": True,
            "outcome_status": "failed",
            "progress_since_previous": "No rigorous advance survived checking.",
            "operations": [],
            "computation_operation_ids": [],
            "fact_challenges": [],
            "completion_evidence_ids": [],
            "attempt_summary": {
                "work_mode": card["mode"],
                "task": card["objective"],
                "proposed_outcome": "failed",
                "cumulative_important_progress": "No significant progress.",
                "completion_evidence_operation_ids": [],
                "most_promising_next_steps": "Compare the other isolated lanes.",
            },
        },
    )
    return card, {"attempt_ended": True, "final_progress_id": progress_id}


class DiscoverySprintRuntimeTests(unittest.TestCase):
    def test_four_reserved_lanes_execute_and_reach_the_barrier(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            worker_task_ids: list[str] = []

            def executor(call: AgentCall) -> dict[str, object]:
                card, result = _stage_failed_worker_final(call)
                worker_task_ids.append(card["task_id"])
                return result

            runtime = FrantaRuntime.initialize(
                load_manifest(_manifest(Path(directory))), executor=executor
            )
            try:
                _persist_sprint(runtime)
                runtime.start_services()
                try:
                    self.assertTrue(runtime._advance_sprint())
                finally:
                    runtime.stop_services()
                sprint = runtime.scheduler.state["sprints"]["S-RUNTIME"]
                self.assertCountEqual(worker_task_ids, sprint["task_ids"])
                self.assertEqual(len(worker_task_ids), 4)
                self.assertEqual(sprint["status"], "awaiting_summary")
                self.assertEqual(
                    len(sprint["frozen_synthesis_input"]["lane_results"]), 4
                )
                self.assertTrue(
                    all(
                        runtime.scheduler.state["tasks"][task_id]["state"] == "closed"
                        for task_id in sprint["task_ids"]
                    )
                )
            finally:
                runtime.close()

    def test_four_reserved_predecessors_drain_before_four_sprint_lanes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            launch_groups: list[tuple[str, str | None]] = []

            def executor(call: AgentCall) -> dict[str, object]:
                card, result = _stage_failed_worker_final(call)
                launch_groups.append((str(card["task_id"]), card.get("sprint_id")))
                return result

            runtime = FrantaRuntime.initialize(
                load_manifest(_manifest(Path(directory))), executor=executor
            )
            try:
                predecessors = _persist_sprint(runtime, predecessor_count=4)
                self.assertEqual(runtime.scheduler.free_non_verifier_slots(), 0)
                runtime.run(max_cycles=1)

                sprint = runtime.scheduler.state["sprints"]["S-RUNTIME"]
                self.assertCountEqual(
                    [task_id for task_id, sprint_id in launch_groups if sprint_id is None],
                    predecessors,
                )
                self.assertCountEqual(
                    [
                        task_id
                        for task_id, sprint_id in launch_groups
                        if sprint_id == "S-RUNTIME"
                    ],
                    sprint["task_ids"],
                )
                self.assertEqual(len(launch_groups), 8)
                self.assertTrue(
                    all(sprint_id is None for _task_id, sprint_id in launch_groups[:4])
                )
                self.assertTrue(
                    all(
                        sprint_id == "S-RUNTIME"
                        for _task_id, sprint_id in launch_groups[4:]
                    )
                )
                self.assertEqual(sprint["status"], "awaiting_summary")
            finally:
                runtime.close()

    def test_waiting_sprint_drain_includes_preexisting_retries_not_later_work(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime = FrantaRuntime.initialize(load_manifest(_manifest(Path(directory))))
            try:
                predecessors = _persist_sprint(runtime, predecessor_count=3)
                state = runtime.scheduler._state
                state["tasks"][predecessors[1]]["state"] = (
                    TaskState.RETRY_PENDING.value
                )
                state["tasks"][predecessors[1]]["slot_reserved"] = False
                state["tasks"][predecessors[2]]["state"] = (
                    TaskState.REVISION_PENDING.value
                )
                state["tasks"][predecessors[2]]["slot_reserved"] = False

                unrelated_id = "T-LATER-UNRESERVED"
                unrelated = copy.deepcopy(state["tasks"][predecessors[0]])
                unrelated["task_id"] = unrelated_id
                unrelated["task_card"]["task_id"] = unrelated_id
                unrelated["slot_reserved"] = False
                state["tasks"][unrelated_id] = unrelated
                state["events"].append(
                    {
                        "event_id": int(state["events"][-1]["event_id"]) + 1,
                        "type": "task_launch_intent_committed",
                        "payload": {"task_id": unrelated_id},
                    }
                )

                selected = runtime._waiting_sprint_drain_launches("S-RUNTIME")
                self.assertCountEqual(selected, predecessors)
                self.assertNotIn(unrelated_id, selected)
            finally:
                runtime.close()

    def test_stale_target_is_paused_before_task_writing_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime = FrantaRuntime.initialize(load_manifest(_manifest(Path(directory))))
            try:
                _persist_sprint(runtime)
                sprint = runtime.scheduler.state["sprints"]["S-RUNTIME"]
                target = sprint["plan"]["target_obligation"]
                result = runtime.store.update_obligation(
                    "stale-sprint-target",
                    {
                        "target_id": target["id"],
                        "expected_base_revision": target["revision"],
                        "set": {},
                        "append": {"partial_progress": ["A later canonical change."]},
                        "add_ids": {},
                        "remove_ids": {},
                        "explanation": "Revise the target after the sprint plan.",
                        "supporting_memory_ids": [],
                    },
                )
                self.assertEqual(result.status, "committed")

                self.assertFalse(runtime._advance_sprint())
                paused = runtime.scheduler.state["sprints"]["S-RUNTIME"]
                self.assertEqual(paused["status"], "needs_attention")
                self.assertEqual(paused["task_ids"], [])
                task_writing = (
                    runtime.layout.workspaces
                    / "SPRINT-TW-S-RUNTIME"
                    / "outbox"
                    / "task-writing"
                )
                self.assertFalse(task_writing.exists())
            finally:
                runtime.close()

    def test_fresh_and_recovery_launch_only_active_sprint_task_ids(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime = FrantaRuntime.initialize(load_manifest(_manifest(Path(directory))))
            try:
                _persist_sprint(runtime)
                launched: list[list[str]] = []
                runtime._run_worker_batch = lambda task_ids: (
                    launched.append(list(task_ids)) or True
                )
                runtime._advance_operations = lambda: False

                self.assertTrue(runtime._advance_sprint())
                scheduler_state = runtime.scheduler._state
                sprint = scheduler_state["sprints"]["S-RUNTIME"]
                sprint_task_ids = list(sprint["task_ids"])
                self.assertEqual(launched, [sprint_task_ids])

                unrelated_id = "T-UNRELATED-RECOVERY"
                unrelated = copy.deepcopy(
                    scheduler_state["tasks"][sprint_task_ids[0]]
                )
                unrelated.update(
                    {
                        "task_id": unrelated_id,
                        "sprint_id": None,
                        "sprint_lane": None,
                        "slot_reserved": False,
                    }
                )
                unrelated["task_card"]["task_id"] = unrelated_id
                unrelated["task_card"]["sprint_id"] = None
                unrelated["task_card"]["sprint_lane"] = None
                scheduler_state["tasks"][unrelated_id] = unrelated

                launched.clear()
                runtime.start_services = lambda: None
                runtime.stop_services = lambda: None
                runtime.run(max_cycles=1)
                self.assertEqual(launched, [sprint_task_ids])
                self.assertNotIn(unrelated_id, launched[0])
            finally:
                runtime.close()

    def test_trimmer_gets_capacity_and_only_continuation_event_deltas(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime = FrantaRuntime.initialize(load_manifest(_manifest(Path(directory))))
            try:
                _persist_sprint(runtime)
                scheduler_state = runtime.scheduler._state
                sprint = scheduler_state["sprints"]["S-RUNTIME"]
                sprint["status"] = "trimmer_continuation"
                cutoff = int(
                    scheduler_state["trim"]["active_trim"]["cutoff_event_id"]
                )

                context = runtime._trimmer_context(phase="maintain")
                self.assertEqual(context["configured_non_verifier_slots"], 4)
                self.assertTrue(context["discovery_sprint_available"])
                self.assertTrue(context["event_deltas_since_trim_cutoff"])
                self.assertTrue(
                    all(
                        int(event["event_id"]) > cutoff
                        for event in context["event_deltas_since_trim_cutoff"]
                    )
                )

                sprint["status"] = "running"
                ordinary_context = runtime._trimmer_context(phase="maintain")
                self.assertNotIn(
                    "event_deltas_since_trim_cutoff", ordinary_context
                )
            finally:
                runtime.close()

    def test_human_guidance_pauses_and_resumes_sprint_integration(self) -> None:
        class FakeScheduler:
            def __init__(self) -> None:
                self.state = {
                    "active_sprint_id": "S-PAUSE",
                    "sprints": {
                        "S-PAUSE": {
                            "status": "trimmer_continuation",
                            "trim_integration": None,
                        }
                    },
                }
                self.gate = GateState.TRIMMING
                self.completed: list[str] = []

            def complete_sprint_continuation(self, sprint_id: str) -> None:
                self.completed.append(sprint_id)
                self.state["sprints"][sprint_id]["status"] = "integrated"
                self.state["active_sprint_id"] = None

        runtime = object.__new__(FrantaRuntime)
        runtime.scheduler = FakeScheduler()
        trim_calls: list[str] = []

        def request_guidance(*, initial: bool = False) -> bool:
            trim_calls.append("guidance")
            runtime.scheduler.gate = GateState.WAITING_FOR_HUMAN
            return True

        runtime._run_trim = request_guidance
        self.assertTrue(runtime._advance_sprint())
        self.assertEqual(runtime.scheduler.completed, [])
        self.assertFalse(runtime._advance_sprint())
        self.assertEqual(trim_calls, ["guidance"])

        runtime.scheduler.gate = GateState.TRIMMING

        def commit_trim(*, initial: bool = False) -> bool:
            trim_calls.append("commit")
            runtime.scheduler.state["sprints"]["S-PAUSE"]["trim_integration"] = {
                "status": "trim_committed"
            }
            return True

        runtime._run_trim = commit_trim
        self.assertTrue(runtime._advance_sprint())
        self.assertEqual(runtime.scheduler.completed, ["S-PAUSE"])
        self.assertEqual(trim_calls, ["guidance", "commit"])

    def test_runtime_and_cli_delegate_operator_sprint_cancellation(self) -> None:
        class FakeScheduler:
            def __init__(self) -> None:
                self.calls: list[tuple[str, str, str]] = []

            def cancel_sprint(
                self, sprint_id: str, *, authorized_by: str, reason: str
            ) -> str:
                self.calls.append((sprint_id, authorized_by, reason))
                return "trimmer_continuation"

        runtime = object.__new__(FrantaRuntime)
        runtime.scheduler = FakeScheduler()
        self.assertEqual(
            runtime.cancel_sprint("S-CANCEL", "operator redirect"),
            "trimmer_continuation",
        )
        self.assertEqual(
            runtime.scheduler.calls,
            [("S-CANCEL", "operator", "operator redirect")],
        )

        class FakeRuntime:
            def __init__(self) -> None:
                self.calls: list[tuple[str, str]] = []
                self.closed = False

            def cancel_sprint(self, sprint_id: str, reason: str) -> str:
                self.calls.append((sprint_id, reason))
                return "trimmer_continuation"

            def close(self) -> None:
                self.closed = True

        cli_runtime = FakeRuntime()
        output = io.StringIO()
        with patch("franta.cli._open", return_value=cli_runtime):
            self.assertEqual(
                main(
                    [
                        "cancel-sprint",
                        "/project",
                        "S-CANCEL",
                        "--reason",
                        "operator redirect",
                    ],
                    output=output,
                ),
                0,
            )
        self.assertEqual(
            json.loads(output.getvalue()),
            {"sprint_id": "S-CANCEL", "status": "trimmer_continuation"},
        )
        self.assertEqual(cli_runtime.calls, [("S-CANCEL", "operator redirect")])
        self.assertTrue(cli_runtime.closed)

    def test_operator_cancellation_replays_exactly_and_fails_closed_on_change(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime = FrantaRuntime.initialize(load_manifest(_manifest(Path(directory))))
            try:
                _persist_sprint(runtime)
                self.assertEqual(
                    runtime.cancel_sprint("S-RUNTIME", "choose a human-directed route"),
                    "trimmer_continuation",
                )
                self.assertEqual(
                    runtime.cancel_sprint("S-RUNTIME", "choose a human-directed route"),
                    "trimmer_continuation",
                )
                with self.assertRaises(IdempotencyConflict):
                    runtime.cancel_sprint("S-RUNTIME", "different redirect")
                sprint = runtime.scheduler.state["sprints"]["S-RUNTIME"]
                self.assertEqual(sprint["status"], "trimmer_continuation")
                self.assertTrue(sprint["summary_skipped_by_cancellation"])
                self.assertEqual(
                    sprint["cancellation"]["authorized_by"], "operator"
                )
            finally:
                runtime.close()


if __name__ == "__main__":
    unittest.main()
