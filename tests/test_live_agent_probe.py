from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from franta.skill_runtime import SkillContext, SkillRuntime
from evals.live_agent_probe import (
    REPAIR_V1_CANDIDATE_ID,
    REPAIR_V2_CANDIDATE_ID,
    REPAIR_V2_OPERATION_ID,
    REPAIR_V2_PROPOSAL_ID,
    ROOT_PROBLEM,
    ProbeHarness,
    _classify_full_record_reads,
    probe_repair_stop,
)


ROOT = Path(__file__).resolve().parents[1]


class LiveAgentProbeTest(unittest.TestCase):
    def test_read_classification_separates_related_unrelated_and_redundant(self) -> None:
        actions = [
            {
                "action": "internal_search",
                "result_ids": ["R-1", "F-1", "R-unrelated"],
            },
            {"action": "memory_fetch", "memory_id": "R-1"},
            {"action": "memory_fetch", "memory_id": "F-1"},
            {"action": "memory_fetch", "memory_id": "R-1"},
            {"action": "memory_fetch", "memory_id": "R-unrelated"},
        ]
        result = _classify_full_record_reads(
            actions,
            required_ids=["R-1"],
            related_ids=["F-1"],
        )
        self.assertEqual(result["required_record_reads"], ["R-1"])
        self.assertEqual(result["defensible_related_record_reads"], ["F-1"])
        self.assertEqual(
            result["clearly_unrelated_record_reads"], ["R-unrelated"]
        )
        self.assertEqual(result["repeated_full_record_reads"], {"R-1": 2})
        self.assertEqual(
            [
                item["memory_id"]
                for item in result["clearly_redundant_full_record_reads"]
            ],
            ["R-1"],
        )

    def test_main_and_trimmer_probes_have_no_answer_bearing_condition(self) -> None:
        source = (ROOT / "evals/live_agent_probe.py").read_text(encoding="utf-8")
        self.assertNotIn('"probe_condition"', source)

    def test_repair_stop_probe_runs_deterministically_through_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            harness = ProbeHarness(
                Path(raw) / "harness",
                codex_binary="unused-codex",
                timeout_seconds=None,
            )

            def append(path: Path, value: dict[str, object]) -> None:
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(value, sort_keys=True) + "\n")

            def executor(call: object) -> dict[str, object]:
                append(
                    harness.state / "calls" / f"{call.call_id}.jsonl",
                    {
                        "type": "transport.call_started",
                        "call_id": call.call_id,
                        "role": call.role,
                        "model": call.model_config.model,
                        "reasoning_effort": call.model_config.reasoning_effort,
                    },
                )
                if call.kind == "synthesizer":
                    return {
                        "resolution": "new",
                        "operation_digest": call.payload["operation_digest"],
                        "explanation": "The fresh repair Fact is not yet represented.",
                        "canonical_id": None,
                        "relied_on": [],
                        "patch": None,
                    }
                if call.kind == "verifier":
                    bundle = call.payload
                    verdict = (
                        "incorrect"
                        if bundle["candidate_id"] == REPAIR_V1_CANDIDATE_ID
                        else "correct"
                    )
                    return {
                        "verdict": verdict,
                        **{
                            field: bundle[field]
                            for field in (
                                "candidate_id",
                                "candidate_version",
                                "operation_id",
                                "bundle_digest",
                                "verifier_attempt_id",
                                "predecessor_ids",
                                "introduced_notation",
                                "external_references",
                                "root_resolution",
                            )
                        },
                        "errors": (
                            [
                                {
                                    "location": "proof",
                                    "message": (
                                        "Closed does not imply edge-spanning; justify "
                                        "residual-trail splicing."
                                    ),
                                }
                            ]
                            if verdict == "incorrect"
                            else []
                        ),
                    }
                if call.kind == "worker":
                    card = json.loads(
                        (call.workspace.path / "input" / "task_card.json").read_text(
                            encoding="utf-8"
                        )
                    )
                    predecessor_id = card["portfolio"]["fact"][0]
                    operation_id = REPAIR_V2_OPERATION_ID
                    operation = {
                        "operation_id": operation_id,
                        "kind": "fact",
                        "proposal_id": REPAIR_V2_PROPOSAL_ID,
                        "candidate_id": REPAIR_V2_CANDIDATE_ID,
                        "candidate_version": 1,
                        "statement": ROOT_PROBLEM,
                        "proof": (
                            f"Use {predecessor_id} to obtain a closed trail. Removing its "
                            "edges preserves even residual degrees. Whenever an unused edge "
                            "remains, connectedness supplies a trail vertex incident to the "
                            "residual graph; obtain and splice another closed trail there. "
                            "Each splice adds an edge, so finiteness yields one closed trail "
                            "containing every edge of the component."
                        ),
                        "predecessor_fact_ids": [predecessor_id],
                        "originating_task_id": card["task_id"],
                        "foundation_policy_version": 1,
                        "introduced_notation": [],
                        "external_references": [],
                        "root_resolution": {
                            "target": "ROOT",
                            "outcome": "proved",
                        },
                        "abstract": (
                            "Finite connected even-degree graphs have Eulerian circuits "
                            "by residual closed-trail splicing."
                        ),
                        "keywords": ["Eulerian circuit", "splicing"],
                        "related_route_ids": [],
                    }
                    progress_id = "PRG-LIVE-ROOT-REPAIR-V2"
                    staged = SkillRuntime(
                        SkillContext.load(call.workspace.path)
                    ).invoke(
                        "record-progress",
                        {
                            "operation_id": "REC-LIVE-ROOT-REPAIR-V2",
                            "progress_id": progress_id,
                            "sequence": 1,
                            "is_final": True,
                            "outcome_status": "finished",
                            "progress_since_previous": (
                                "Repaired the missing residual-trail splicing step."
                            ),
                            "operations": [operation],
                            "computation_operation_ids": [],
                            "fact_challenges": [],
                            "completion_evidence_ids": [operation_id],
                            "attempt_summary": {
                                "work_mode": "associate",
                                "task": card["objective"],
                                "proposed_outcome": "finished",
                                "cumulative_important_progress": (
                                    "Completed the root proof by finite trail splicing."
                                ),
                                "completion_evidence_operation_ids": [
                                    operation_id
                                ],
                                "most_promising_next_steps": (
                                    "Verify the repaired proof."
                                ),
                            },
                        },
                    )["artifact"]
                    append(
                        harness.state / "tool_activity.jsonl",
                        {
                            "type": "tool_activity",
                            "call_id": call.call_id,
                            "event_type": "item.completed",
                            "status": "completed",
                            "franta_skill_invocations": ["record-progress"],
                        },
                    )
                    return {
                        "attempt_ended": True,
                        "final_progress_id": staged["progress_id"],
                    }
                if call.kind == "main":
                    return {
                        "decision": "terminal",
                        "batch_id": None,
                        "assignment_report_ids": [],
                        "wait_for_task_ids": [],
                        "decline_proof_writer": True,
                    }
                raise AssertionError(f"unexpected call kind {call.kind}")

            append(
                harness.state / "single-agent-preflight.jsonl",
                {
                    "type": "transport.single_agent_preflight",
                    "model": "gpt-6-astra",
                    "reasoning_effort": "ultra",
                    "status": "passed",
                },
            )
            append(
                harness.state / "single-agent-preflight.jsonl",
                {
                    "type": "transport.single_agent_preflight",
                    "model": "gpt-6-astra",
                    "reasoning_effort": "max",
                    "status": "passed",
                },
            )
            result = probe_repair_stop(harness, executor=executor)
            self.assertEqual(result["status"], "passed")
            self.assertTrue(all(result["checks"].values()))
            self.assertEqual(result["final_gate"], "completed")


if __name__ == "__main__":
    unittest.main()
