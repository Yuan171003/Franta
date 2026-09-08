from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from franta.advisor_adapter import advisor_agent_call_spec
from franta.config import load_manifest
from franta.explorer_adapter import (
    build_main_sort_explorer_snapshot_for_frozen_turn,
    explorer_agent_call_spec,
)
from franta.human_guidance import submit_human_guidance
from franta.materialize import explorer_snapshot_digest
from franta.runtime import AgentCall, FrantaRuntime
from franta.skill_runtime import SkillContext, SkillRuntime

from test_advisor_runtime_integration import (
    _AdvisorExecutor,
    _enter_franta_drain,
    _write_manifest,
)
from test_human_guidance_runtime import _assign, _baseline


GUIDANCE = "先固定一条退化族，再研究其特殊纤维。\n"


def _enter_sort(runtime: FrantaRuntime) -> str:
    """Freeze an empty Explorer turn through the normal sort ownership ports."""

    runtime.scheduler.activate_alternation()
    phase = runtime.scheduler.state["phase_control"]
    deadline = datetime.fromisoformat(phase["explorer"]["admission_deadline"])
    runtime.scheduler.tick_alternation(now=deadline)
    repository = runtime.explorer_repository
    assert repository is not None
    turn_id = f"XTURN-{int(phase['cycle']):08d}"
    frozen = repository.freeze_turn(turn_id)
    sort_run_id = "SORT-GUIDANCE-ARRIVAL"
    snapshot = build_main_sort_explorer_snapshot_for_frozen_turn(
        repository, sort_run_id=sort_run_id, frozen_turn=frozen
    )
    task_id = runtime.scheduler.prepare_main_sort_task(
        sort_run_id=sort_run_id,
        turn_id=turn_id,
        source_high_water_seq=frozen.high_water_seq,
        source_set_digest=frozen.source_set_digest,
        snapshot_format_version=1,
        snapshot_digest=explorer_snapshot_digest(snapshot),
    )
    repository.create_sort_run(sort_run_id, frozen, task_id)
    return runtime.scheduler.begin_main_sort_task_attempt(
        task_id,
        sort_run_id=sort_run_id,
        session_key="main:guidance-sort",
        now=deadline,
    )


def _empty_sort_result(call: AgentCall) -> dict:
    progress_id = "PRG-GUIDANCE-SORT-FINAL"
    SkillRuntime(SkillContext.load(call.workspace.path)).invoke(
        "record-progress",
        {
            "operation_id": "RP-GUIDANCE-SORT-FINAL",
            "progress_id": progress_id,
            "sequence": 1,
            "is_final": True,
            "outcome_status": "progress",
            "progress_since_previous": "The frozen Explorer turn has no records to promote.",
            "operations": [],
            "computation_operation_ids": [],
            "explorer_computation_promotions": [],
            "fact_challenges": [],
            "completion_evidence_ids": [],
            "attempt_summary": {
                "work_mode": "main-sort",
                "task": call.payload["task_card"]["objective"],
                "proposed_outcome": "progress",
                "cumulative_important_progress": "Reviewed the empty frozen turn.",
                "completion_evidence_operation_ids": [],
                "most_promising_next_steps": "Proceed to ordinary Main planning.",
            },
        },
    )
    return {
        "sort_ended": True,
        "final_progress_id": progress_id,
        "selected_explorer_record_ids": [],
        "deferred_computation_record_ids": [],
    }


class HumanGuidanceAlternationTests(unittest.TestCase):
    def _advisor_deferral(self, arrival: str) -> None:
        with tempfile.TemporaryDirectory() as raw:
            advisor_executor = _AdvisorExecutor()
            records = []

            def executor(call: AgentCall) -> dict:
                if call.kind == arrival:
                    records.append(submit_human_guidance(runtime.layout.root, GUIDANCE))
                return advisor_executor(call)

            manifest = _write_manifest(Path(raw), advisor=True)
            # Avoid a wall-clock race while materializing the next-cycle workspace.
            manifest.write_text(
                manifest.read_text().replace(
                    "explorer_admission_seconds = 1", "explorer_admission_seconds = 60"
                ),
                encoding="utf-8",
            )
            runtime = FrantaRuntime.initialize(load_manifest(manifest), executor=executor)
            try:
                runtime.scheduler.commit_initial_trim({"category_ids": []})
                _enter_franta_drain(runtime, marker=f"GUIDANCE-{arrival}")
                if arrival == "franta_drain":
                    records.append(submit_human_guidance(runtime.layout.root, GUIDANCE))
                waiting = runtime.run(max_cycles=1)
                self.assertEqual(waiting["advisor"]["status"], "waiting_for_human")
                self.assertEqual(runtime.scheduler.alternation_phase, "franta_drain")
                runtime.submit_advisor_feedback(
                    waiting["advisor"]["feedback_request_id"],
                    {
                        "feedback_id": "HUMAN-CHOICE-GUIDANCE-BOUNDARY",
                        "choices": [{"kind": "listed", "obligation_id": "ADV-1-1"}],
                        "instructions": "Use the selected obligation for the next cycle.",
                    },
                )
                runtime.run(max_cycles=1)
                self.assertEqual(runtime.scheduler.alternation_phase, "explorer_admission")
                self.assertEqual(runtime.scheduler.state["phase_control"]["cycle"], 2)
                self.assertEqual(len(records), 1)
                self.assertEqual(runtime.status()["human_guidance_inbox"][0]["status"], "pending")
                self.assertEqual(
                    [call.kind for call in advisor_executor.calls],
                    ["advisor-proposal", "advisor-finalize"],
                )
                for call in advisor_executor.calls:
                    persisted = runtime.scheduler.state["calls"][call.call_id]
                    continuation = persisted["continuation"]
                    baseline = advisor_agent_call_spec(
                        stage=continuation["stage"],
                        advisor_index=continuation["advisor_index"],
                        settings=runtime.config["advisor"],
                    ).prompt
                    self.assertEqual(call.prompt, baseline)
                    self.assertNotIn("human_guidance", call.payload)
                    serialized = json.dumps(call.payload, ensure_ascii=False)
                    self.assertNotIn(GUIDANCE.rstrip(), serialized)
                    self.assertNotIn(records[0]["guidance_id"], serialized)
                    self.assertNotIn(GUIDANCE, call.prompt)
                    context = json.loads(
                        (call.workspace.input_path / "context.json").read_text()
                    )
                    self.assertEqual(context, call.payload)

                lineage = runtime.scheduler.admit_explorer_lineage()
                call_id = runtime.explorer_program.host.start_attempt(
                    lineage, source_high_water_seq=0
                )
                policy, workspace, session_key, resume = runtime._explorer_attempt_workspace(call_id)
                explorer = runtime._call_spec(
                    call_id, workspace=workspace, policy=policy, mode=policy.mode,
                    session_key=session_key, resume=resume,
                )
                self.assertEqual(explorer.payload["attempt_number"], 1)
                self.assertEqual(explorer.payload["explorer_turn_id"], "XTURN-00000002")
                self.assertEqual(explorer.payload["human_guidance"], records[0])
                self.assertIn(GUIDANCE, explorer.prompt)
                self.assertEqual(
                    runtime.scheduler.state["human_guidance_inbox"][records[0]["guidance_id"]]["call_id"],
                    call_id,
                )
            finally:
                runtime.close()

    def test_guidance_waiting_at_drain_skips_advisor_and_reaches_next_explorer(self) -> None:
        self._advisor_deferral("franta_drain")

    def test_guidance_arriving_during_proposal_reaches_next_explorer(self) -> None:
        self._advisor_deferral("advisor-proposal")

    def test_guidance_arriving_during_finalize_reaches_next_explorer(self) -> None:
        self._advisor_deferral("advisor-finalize")

    def test_guidance_arriving_during_sort_reaches_first_post_sort_main(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            calls = []
            records = []

            def executor(call: AgentCall) -> dict:
                calls.append(call)
                if call.kind == "main-sort":
                    records.append(submit_human_guidance(runtime.layout.root, GUIDANCE))
                    return _empty_sort_result(call)
                if call.kind == "main":
                    return _assign(call)
                raise AssertionError(f"Unexpected transport call {call.kind}")

            runtime = FrantaRuntime.initialize(
                load_manifest(_write_manifest(Path(raw), advisor=False)), executor=executor
            )
            try:
                runtime.start_services()
                runtime.scheduler.commit_initial_trim({"category_ids": []})
                sort_call_id = _enter_sort(runtime)
                frozen_sort_input = runtime.scheduler.state["calls"][sort_call_id]["input"]
                self.assertTrue(runtime._run_franta_sort_barrier())
                self.assertEqual([call.kind for call in calls], ["main-sort", "main"])
                sorter, main = calls
                baseline = explorer_agent_call_spec(
                    "main-sort",
                    root_problem=sorter.workspace.root_problem_path.read_text().rstrip(),
                    mode=None,
                    policy=sorter.policy,
                    input_path="input/task_card.json",
                ).prompt
                self.assertEqual(sorter.prompt, baseline)
                self.assertNotIn("human_guidance", sorter.payload)
                self.assertNotIn("human_guidance", sorter.payload["task_card"])
                self.assertNotIn(GUIDANCE, sorter.prompt)
                self.assertEqual(
                    runtime.scheduler.state["calls"][sort_call_id]["input"],
                    frozen_sort_input,
                )
                self.assertEqual(main.payload["human_guidance"], records[0])
                self.assertIn(GUIDANCE, main.prompt)
                self.assertTrue(main.prompt.startswith(_baseline(main)))
                self.assertEqual(
                    runtime.scheduler.state["calls"][main.call_id]["continuation"]["post_sort_call_id"],
                    sort_call_id,
                )
                self.assertEqual(runtime.scheduler.alternation_phase, "franta_run")
                row = runtime.scheduler.state["human_guidance_inbox"][records[0]["guidance_id"]]
                self.assertEqual(row["status"], "assigned")
                self.assertEqual(row["call_id"], main.call_id)
                self.assertEqual(
                    runtime.scheduler.state["tasks"][row["task_id"]]["task_card"]["human_guidance"],
                    records[0],
                )
            finally:
                runtime.close()


if __name__ == "__main__":
    unittest.main()
