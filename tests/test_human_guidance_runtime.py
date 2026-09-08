from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from franta.access import policy_for
from franta.config import load_manifest
from franta.human_guidance import submit_human_guidance
from franta.prompts import prompt_for
from franta.runtime import AgentCall, FrantaRuntime
from franta.skill_runtime import SkillContext, SkillRuntime


def _manifest(root: Path, *, explorer: bool = False) -> Path:
    path = root / "bootstrap.toml"
    path.write_text(
        '[project]\nname = "guidance-runtime"\ndirectory = "project"\n'
        'root_problem = "Prove the ROOT statement."\n'
        'foundation_policy = "Use only explicitly recorded assumptions."\n'
        + ('\n[explorer]\nmax_workers = 2\n' if explorer else '')
        + '\n[initial]\n',
        encoding="utf-8",
    )
    return path


def _assign(call: AgentCall, *, count: int = 1, include_guidance: bool = True) -> dict:
    skill = SkillRuntime(SkillContext.load(call.workspace.path))
    ids = []
    for index in range(count):
        objective = "Investigate the root obligation independently."
        guidance = call.payload.get("human_guidance")
        if index == 0 and guidance is not None and include_guidance:
            objective = "Investigate the supplied approach:\n" + guidance["text"] + "\nRecord the evidence."
        artifact = skill.invoke(
            "task-writing",
            {
                "operation_id": f"AR-{call.call_id}-{index}",
                "batch_finalized": True,
                "batch_id": call.payload["reserved_batch_id"],
                "objective": objective,
                "work_mode": "brainstorm",
                "if_resume": None,
                "main_route_ids": [],
                "main_obligation_ids": [call.payload["root"]["obligation_id"]],
                "selected_new_perspective": None,
                "assignment_portfolio": {
                    kind: [] for kind in ("fact", "route", "memo", "claim", "obligation", "computation")
                },
                "reason": "Exercise the assigned research direction.",
                "root_solution_fact_id": None,
            },
        )["artifact"]
        ids.append(artifact["operation_id"])
    return {
        "decision": "assignments",
        "batch_id": call.payload["reserved_batch_id"],
        "assignment_report_ids": ids,
        "wait_for_task_ids": [],
        "decline_proof_writer": False,
    }


def _baseline(call: AgentCall) -> str:
    return prompt_for(
        call.role,
        root_problem=call.workspace.root_problem_path.read_text(encoding="utf-8").rstrip(),
        mode=call.mode,
        policy=call.policy,
        input_path="input/task_card.json" if call.kind == "worker" else "input/context.json",
        available_skills={
            p.name for p in (call.workspace.path / ".agents" / "skills").iterdir()
            if p.is_dir() and not p.is_symlink()
        },
    )


class HumanGuidanceRuntimeTests(unittest.TestCase):
    def test_main_binds_only_first_worker_and_next_main_prompt_is_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            calls = []

            def executor(call: AgentCall) -> dict:
                calls.append(call)
                frozen = json.loads((call.workspace.input_path / "context.json").read_text())
                self.assertEqual(frozen, call.payload)
                return _assign(call, count=2 if len(calls) == 1 else 1)

            runtime = FrantaRuntime.initialize(load_manifest(_manifest(Path(raw))), executor=executor)
            try:
                runtime.start_services()
                runtime.scheduler.commit_initial_trim({"category_ids": []})
                guidance = submit_human_guidance(runtime.layout.root, "先研究退化。\n再检查单值化。\n")
                runtime._run_main()
                first = calls[0]
                self.assertEqual(first.payload["human_guidance"], guidance)
                self.assertIn(guidance["text"], first.prompt)
                self.assertTrue(first.prompt.startswith(_baseline(first)))
                batch = runtime.scheduler.state["batches"][first.payload["reserved_batch_id"]]
                task_ids = batch["task_ids"]
                for index, task_id in enumerate(task_ids):
                    call_id, _attempt, workspace = runtime._prepare_worker_workspace(task_id)
                    worker = runtime._call_spec(
                        call_id, workspace=workspace,
                        policy=policy_for("worker", mode="brainstorm"), mode="brainstorm",
                    )
                    if index == 0:
                        self.assertIn(guidance["text"], worker.prompt)
                        self.assertEqual(worker.payload["task_card"]["human_guidance"], guidance)
                        self.assertTrue(worker.prompt.startswith(_baseline(worker)))
                    else:
                        self.assertNotIn("human_guidance", worker.payload["task_card"])
                        self.assertEqual(worker.prompt, _baseline(worker))
                runtime._run_main()
                self.assertNotIn("human_guidance", calls[1].payload)
                self.assertEqual(calls[1].prompt, _baseline(calls[1]))
                queued = runtime.status()["human_guidance_inbox"][0]
                self.assertEqual(queued["status"], "assigned")
                self.assertEqual(queued["task_id"], task_ids[0])
            finally:
                runtime.close()

    def test_main_retries_ignored_guidance_with_identical_frozen_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            calls = []

            def executor(call: AgentCall) -> dict:
                calls.append(call)
                return _assign(call, include_guidance=len(calls) > 1)

            runtime = FrantaRuntime.initialize(load_manifest(_manifest(Path(raw))), executor=executor)
            try:
                runtime.start_services()
                runtime.scheduler.commit_initial_trim({"category_ids": []})
                record = submit_human_guidance(runtime.layout.root, "Test the limiting mixed structure.")
                runtime._run_main()
                self.assertEqual(len(calls), 2)
                self.assertEqual(calls[0].call_id, calls[1].call_id)
                self.assertEqual(calls[0].prompt, calls[1].prompt)
                self.assertEqual(calls[0].payload, calls[1].payload)
                self.assertEqual(len(runtime.scheduler.state["tasks"]), 1)
                self.assertEqual(runtime.scheduler.state["human_guidance_inbox"][record["guidance_id"]]["status"], "assigned")
            finally:
                runtime.close()

    def test_submission_during_main_waits_for_the_next_call(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            calls = []
            records = []

            def executor(call: AgentCall) -> dict:
                calls.append(call)
                if len(calls) == 1:
                    records.append(submit_human_guidance(runtime.layout.root, "Try a specialization argument."))
                return _assign(call)

            runtime = FrantaRuntime.initialize(load_manifest(_manifest(Path(raw))), executor=executor)
            try:
                runtime.start_services()
                runtime.scheduler.commit_initial_trim({"category_ids": []})
                runtime._run_main()
                self.assertNotIn("human_guidance", calls[0].payload)
                self.assertEqual(calls[0].prompt, _baseline(calls[0]))
                self.assertEqual(runtime.status()["human_guidance_inbox"][0]["status"], "pending")
                runtime._run_main()
                self.assertEqual(calls[1].payload["human_guidance"], records[0])
            finally:
                runtime.close()

    def test_main_restart_reuses_guidance_and_retires_interrupted_staging(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            interrupted = []

            def crash(call: AgentCall) -> dict:
                interrupted.append(call)
                _assign(call, include_guidance=False)
                raise KeyboardInterrupt("simulated process interruption")

            runtime = FrantaRuntime.initialize(load_manifest(_manifest(Path(raw))), executor=crash)
            project = runtime.layout.root
            try:
                runtime.start_services()
                runtime.scheduler.commit_initial_trim({"category_ids": []})
                record = submit_human_guidance(project, "Analyze the specialization kernel.")
                with self.assertRaises(KeyboardInterrupt):
                    runtime._run_main()
            finally:
                runtime.close()

            recovered = []

            def complete(call: AgentCall) -> dict:
                recovered.append(call)
                return _assign(call)

            runtime = FrantaRuntime.open(project, executor=complete)
            try:
                runtime.start_services()
                runtime.recover()
                self.assertTrue(runtime._recover_control_calls())
                self.assertEqual(len(recovered), 1)
                self.assertEqual(recovered[0].call_id, interrupted[0].call_id)
                self.assertEqual(recovered[0].prompt, interrupted[0].prompt)
                self.assertEqual(recovered[0].payload, interrupted[0].payload)
                self.assertEqual(len(runtime.scheduler.state["tasks"]), 1)
                self.assertEqual(runtime.scheduler.state["human_guidance_inbox"][record["guidance_id"]]["status"], "assigned")
            finally:
                runtime.close()

    def test_inbox_poll_wakes_main_and_empty_polls_do_not_mutate_state(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            runtime = FrantaRuntime.initialize(load_manifest(_manifest(Path(raw))))
            try:
                runtime.scheduler.commit_initial_trim({"category_ids": []})
                before = runtime.scheduler.revision
                self.assertFalse(runtime._ingest_human_guidance())
                self.assertEqual(runtime.scheduler.revision, before)
                submit_human_guidance(runtime.layout.root, "Use a family of test curves.")
                before = runtime.scheduler.revision
                self.assertEqual(runtime.status()["human_guidance_inbox"][0]["status"], "pending")
                self.assertEqual(runtime.scheduler.revision, before)
                self.assertTrue(runtime._ingest_human_guidance())
                self.assertTrue(runtime._main_should_run())
                before = runtime.scheduler.revision
                self.assertFalse(runtime._ingest_human_guidance())
                self.assertEqual(runtime.scheduler.revision, before)
            finally:
                runtime.close()

    def test_explorer_only_next_unprepared_attempt_one_receives_guidance_after_reopen(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            runtime = FrantaRuntime.initialize(load_manifest(_manifest(Path(raw), explorer=True)))
            project = runtime.layout.root
            try:
                runtime.scheduler.activate_alternation()
                first_lineage = runtime.scheduler.admit_explorer_lineage()
                first_id = runtime.explorer_program.host.start_attempt(first_lineage, source_high_water_seq=0)
                first_input = copy.deepcopy(runtime.scheduler.state["calls"][first_id]["input"])
                record = submit_human_guidance(project, "Exploit a semistable degeneration.\n")
                second_lineage = runtime.scheduler.admit_explorer_lineage()
                second_id = runtime.explorer_program.host.start_attempt(second_lineage, source_high_water_seq=0)
                self.assertEqual(runtime.scheduler.state["calls"][first_id]["input"], first_input)
                self.assertNotIn("human_guidance", first_input)
                self.assertEqual(runtime.scheduler.state["calls"][second_id]["input"]["human_guidance"], record)
                policy, workspace, key, resume = runtime._explorer_attempt_workspace(second_id)
                first_spec = runtime._call_spec(second_id, workspace=workspace, policy=policy, mode=policy.mode, session_key=key, resume=resume)
                self.assertIn(record["text"], first_spec.prompt)
            finally:
                runtime.close()
            runtime = FrantaRuntime.open(project)
            try:
                policy, workspace, key, resume = runtime._explorer_attempt_workspace(second_id)
                replay = runtime._call_spec(second_id, workspace=workspace, policy=policy, mode=policy.mode, session_key=key, resume=resume)
                self.assertEqual(replay.prompt, first_spec.prompt)
                self.assertEqual(replay.payload, first_spec.payload)
                self.assertFalse(runtime._ingest_human_guidance())
            finally:
                runtime.close()


if __name__ == "__main__":
    unittest.main()
