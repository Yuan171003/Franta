from __future__ import annotations

import copy
import io
import json
import tempfile
import unittest
from pathlib import Path

from franta.evaluation import (
    LiveSessionObservationParser,
    ScenarioStatus,
    evaluate_files,
    evaluate_snapshot,
    main,
    parse_live_session,
)


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "evals" / "fixtures" / "complete_observation.json"


def fixture() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


class EvaluationTest(unittest.TestCase):
    def test_complete_fixture_passes_every_scenario_and_serializes(self) -> None:
        report = evaluate_files(FIXTURE)
        self.assertEqual(report.status, ScenarioStatus.PASS)
        self.assertTrue(all(item.status is ScenarioStatus.PASS for item in report.scenarios))
        encoded = json.loads(report.to_json())
        self.assertEqual(encoded["status"], "pass")
        self.assertEqual(encoded["schema_version"], 1)

    def test_live_parser_is_read_only_and_retains_malformed_line_diagnostic(self) -> None:
        content = "\n".join(
            [
                json.dumps(
                    {
                        "type": "item.completed",
                        "call_id": "CALL-1",
                        "role": "main",
                        "item": {
                            "type": "mcp_tool_call",
                            "server": "franta",
                            "tool": "internal_search",
                            "status": "completed",
                        },
                    }
                ),
                "{broken",
            ]
        )
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "live.jsonl"
            path.write_text(content, encoding="utf-8")
            before = path.read_bytes()
            parsed = LiveSessionObservationParser().parse_path(path)
            after = path.read_bytes()
        self.assertEqual(before, after)
        self.assertEqual(parsed.events[0].kind, "internal_search")
        self.assertEqual(len(parsed.diagnostics), 1)
        self.assertIn("invalid JSON", parsed.diagnostics[0])

    def test_parse_live_session_accepts_text(self) -> None:
        parsed = parse_live_session('{"action":"fetch","subject_id":"F-1"}\n')
        self.assertEqual(parsed.events[0].kind, "memory_fetch")

    def test_task_search_tools_are_recognized_and_limited_to_main_and_trimmer(self) -> None:
        snapshot = fixture()
        snapshot["tool_activity"] = [
            {
                "action": "task_summary_read",
                "policy": "main",
            },
            {
                "type": "tool_activity",
                "event_type": "item.completed",
                "role": "trimmer",
                "tool": "task_artifact_fetch",
                "status": "completed",
            },
        ]
        result = evaluate_snapshot(snapshot).scenario("skill_use")
        self.assertEqual(result.status, ScenarioStatus.PASS)
        task_search = [
            item
            for item in result.evidence["observed_calls"]
            if item["skill"] == "task-search"
        ]
        self.assertEqual([item["role"] for item in task_search], ["main", "trimmer"])

        snapshot["tool_activity"].append(
            {
                "type": "tool_activity",
                "event_type": "item.completed",
                "policy": "worker-research",
                "tool": "task_summary",
                "status": "completed",
            }
        )
        result = evaluate_snapshot(snapshot).scenario("skill_use")
        self.assertEqual(result.status, ScenarioStatus.FAIL)
        self.assertTrue(
            any("task-search" in finding for finding in result.findings)
        )

    def test_started_or_incomplete_tool_rows_are_not_successes(self) -> None:
        snapshot = {
            "observation_scope": {},
            "tool_activity": [
                {
                    "type": "tool_activity",
                    "event_type": "item.started",
                    "role": "worker",
                    "mode": "research",
                    "tool": "record_progress",
                    "status": "completed",
                },
                {
                    "type": "tool_activity",
                    "event_type": "item.completed",
                    "role": "worker",
                    "mode": "research",
                    "tool": "record_progress",
                    "status": "incomplete",
                },
                {
                    "type": "tool_activity",
                    "event_type": "item.completed",
                    "role": "worker",
                    "mode": "research",
                    "tool": "record_progress",
                    "status": "completed",
                },
            ],
        }
        result = evaluate_snapshot(snapshot).scenario("skill_use")
        self.assertEqual(result.status, ScenarioStatus.INCONCLUSIVE)
        self.assertEqual(
            [item["success"] for item in result.evidence["observed_calls"]],
            [None, None, True],
        )

    def test_started_reads_are_not_counted_or_treated_as_isolated_successes(self) -> None:
        snapshot = fixture()
        snapshot["read_audit"] = []
        snapshot["tool_activity"] = [
            {
                "type": "tool_activity",
                "event_type": "item.started",
                "call_id": "CALL-main",
                "role": "main",
                "tool": "internal_search",
                "status": "in_progress",
                "result_ids": ["F-root"],
            },
            {
                "type": "tool_activity",
                "event_type": "item.completed",
                "call_id": "CALL-main",
                "role": "main",
                "tool": "internal_search",
                "status": "completed",
                "result_ids": ["F-root"],
            },
            {
                "type": "tool_activity",
                "event_type": "item.started",
                "call_id": "CALL-main",
                "role": "main",
                "tool": "memory_fetch",
                "status": "in_progress",
                "memory_id": "F-root",
            },
            {
                "type": "tool_activity",
                "event_type": "item.completed",
                "call_id": "CALL-main",
                "role": "main",
                "tool": "memory_fetch",
                "status": "completed",
                "memory_id": "F-root",
            },
        ]
        disclosure = evaluate_snapshot(snapshot).scenario("progressive_disclosure")
        self.assertEqual(disclosure.status, ScenarioStatus.PASS)
        self.assertEqual(disclosure.evidence["full_reads"], 1)
        self.assertEqual(
            disclosure.evidence["reads_with_prior_summary_or_justification"], 1
        )

        snapshot["tool_activity"] = [
            {
                "type": "tool_activity",
                "event_type": "item.started",
                "task_id": "T-isolated",
                "role": "worker",
                "mode": "brainstorm",
                "tool": "internal_search",
                "status": "in_progress",
            }
        ]
        isolation = evaluate_snapshot(snapshot).scenario("isolated_access")
        self.assertEqual(isolation.status, ScenarioStatus.PASS)
        self.assertFalse(isolation.evidence["successful_access_attempts"])
        self.assertEqual(
            isolation.evidence["incomplete_access_attempts"],
            ["T-isolated:internal_search"],
        )

    def test_broker_reads_are_authoritative_and_transport_rows_deduplicate(self) -> None:
        snapshot = fixture()
        snapshot["read_audit"] = [
            {
                "action": "internal_search",
                "actor": "CALL-main",
                "result_ids": ["F-root"],
                "allowed": True,
            },
            {
                "action": "memory_fetch",
                "actor": "CALL-main",
                "subject_id": "F-root",
                "allowed": True,
            },
        ]
        raw_rows = [
            {
                "type": "transport.codex_event",
                "call_id": "CALL-main",
                "event": {
                    "type": phase,
                    "item": {
                        "id": item_id,
                        "type": "mcp_tool_call",
                        "server": "franta",
                        "tool": tool,
                        "status": status,
                        "arguments": arguments,
                        **(
                            {
                                "result": {
                                    "structured_content": {
                                        "result": [
                                            {"id": "F-root", "abstract": "Root fact."}
                                        ]
                                    }
                                }
                            }
                            if tool == "internal_search" and phase == "item.completed"
                            else {}
                        ),
                    },
                },
            }
            for phase, status, item_id, tool, arguments in (
                ("item.started", "in_progress", "item-search", "internal_search", {}),
                (
                    "item.completed",
                    "completed",
                    "item-search",
                    "internal_search",
                    {"query": "root", "memory_types": ["fact"]},
                ),
                (
                    "item.started",
                    "in_progress",
                    "item-fetch",
                    "memory_fetch",
                    {"memory_id": "F-root"},
                ),
                (
                    "item.completed",
                    "completed",
                    "item-fetch",
                    "memory_fetch",
                    {"memory_id": "F-root"},
                ),
            )
        ]
        snapshot["events"] = raw_rows
        snapshot["tool_activity"] = [
            {
                "type": "tool_activity",
                "event_type": "item.completed",
                "call_id": "CALL-main",
                "item_id": "item-search",
                "tool": "internal_search",
                "status": "completed",
                "result_ids": ["F-root"],
            },
            {
                "type": "tool_activity",
                "event_type": "item.completed",
                "call_id": "CALL-main",
                "item_id": "item-fetch",
                "tool": "memory_fetch",
                "status": "completed",
                "memory_id": "F-root",
            },
        ]
        result = evaluate_snapshot(snapshot).scenario("progressive_disclosure")
        self.assertEqual(result.status, ScenarioStatus.PASS)
        self.assertEqual(result.evidence["full_reads"], 1)

        # Without broker audit, the paired per-call and central transport rows
        # still describe one search and one fetch, not duplicate reads.
        snapshot["read_audit"] = []
        result = evaluate_snapshot(snapshot).scenario("progressive_disclosure")
        self.assertEqual(result.status, ScenarioStatus.PASS)
        self.assertEqual(result.evidence["full_reads"], 1)

    def test_skill_log_parser_correlates_read_only_staged_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw) / "CALL-worker"
            artifact = workspace / "outbox" / "record-progress" / "OP-1.json"
            artifact.parent.mkdir(parents=True)
            artifact.write_text(
                json.dumps(
                    {
                        "skill": "record-progress",
                        "operation_id": "OP-1",
                        "task_id": "T-1",
                        "attempt": 2,
                        "is_final": True,
                    }
                ),
                encoding="utf-8",
            )
            audit = workspace / "outbox" / "skill_activity.jsonl"
            audit.write_text(
                json.dumps(
                    {
                        "skill": "record-progress",
                        "operation_id": "OP-1",
                        "relative_path": "outbox/record-progress/OP-1.json",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            before = (audit.read_bytes(), artifact.read_bytes())
            parsed = LiveSessionObservationParser().parse_path(audit)
            after = (audit.read_bytes(), artifact.read_bytes())
        self.assertEqual(before, after)
        self.assertEqual(parsed.events[0].data["call_id"], "CALL-worker")
        self.assertEqual(parsed.events[0].data["artifact"]["task_id"], "T-1")

    def test_memory_counts_never_create_a_numeric_quota_failure(self) -> None:
        snapshot = fixture()
        snapshot["memories"].extend(
            {
                "id": f"CL-{index}",
                "type": "claim",
                "active": True,
                "abstract": f"Exploratory boundary observation {index}.",
                "content": "Nonauthoritative exploratory content.",
            }
            for index in range(100)
        )
        result = evaluate_snapshot(snapshot).scenario("memory_health")
        self.assertEqual(result.status, ScenarioStatus.PASS)
        self.assertEqual(result.evidence["counts"]["claim"], 100)
        self.assertIn("no numerical health quota", result.evidence["count_policy"])

    def test_memory_health_reports_core_exploratory_and_active_counts_without_quotas(self) -> None:
        snapshot = fixture()
        snapshot["memories"].extend(
            [
                {
                    "id": "M-observation",
                    "type": "memo",
                    "active": True,
                    "abstract": "A reusable high-level observation.",
                },
                {
                    "id": "CL-retired",
                    "type": "claim",
                    "active": False,
                    "abstract": "A superseded exploratory calculation.",
                },
            ]
        )
        snapshot["live_events"][0]["reviewed_memory_types"] = [
            "fact",
            "route",
            "obligation",
            "memo",
            "claim",
        ]
        result = evaluate_snapshot(snapshot).scenario("memory_health")
        self.assertEqual(result.status, ScenarioStatus.PASS)
        self.assertEqual(
            result.evidence["core_memory_counts"],
            {"fact": 1, "route": 0, "obligation": 0},
        )
        self.assertEqual(
            result.evidence["exploratory_memory_counts"],
            {"memo": 1, "claim": 1},
        )
        self.assertEqual(result.evidence["active_counts"]["memo"], 1)
        self.assertEqual(result.evidence["inactive_counts"]["claim"], 1)
        self.assertEqual(
            result.evidence["qualitative_reviews"][0]["reviewed_memory_types"],
            ["claim", "fact", "memo", "obligation", "route"],
        )

    def test_memory_health_without_semantic_review_is_inconclusive(self) -> None:
        snapshot = fixture()
        snapshot["live_events"] = [
            event
            for event in snapshot["live_events"]
            if event["type"] != "memory_health_review"
        ]
        result = evaluate_snapshot(snapshot).scenario("memory_health")
        self.assertEqual(result.status, ScenarioStatus.INCONCLUSIVE)

    def test_direct_full_read_is_inconclusive_but_explicitly_unneeded_read_fails(self) -> None:
        snapshot = fixture()
        snapshot["read_audit"] = [
            {
                "audit_seq": 1,
                "action": "fetch",
                "actor": "CALL-main",
                "subject_id": "F-root",
                "allowed": True,
            }
        ]
        result = evaluate_snapshot(snapshot).scenario("progressive_disclosure")
        self.assertEqual(result.status, ScenarioStatus.INCONCLUSIVE)

        snapshot["read_audit"][0]["needed"] = False
        result = evaluate_snapshot(snapshot).scenario("progressive_disclosure")
        self.assertEqual(result.status, ScenarioStatus.FAIL)

    def test_successful_project_memory_read_by_isolated_worker_fails(self) -> None:
        snapshot = fixture()
        snapshot["live_events"].append(
            {
                "action": "memory_fetch",
                "task_id": "T-isolated",
                "mode": "brainstorm",
                "memory_id": "F-root",
                "status": "completed",
            }
        )
        result = evaluate_snapshot(snapshot).scenario("isolated_access")
        self.assertEqual(result.status, ScenarioStatus.FAIL)
        self.assertTrue(result.evidence["successful_access_attempts"])

    def test_isolation_covers_multi_discipline_sprint_lanes_and_sealed_summary(self) -> None:
        snapshot = fixture()
        sealed = {
            "canonical_memory": False,
            "internal_search": False,
            "sealed_workspace": True,
            "direct_canonical_mount": False,
        }
        snapshot["scheduler"]["tasks"].update(
            {
                "T-multi": {
                    "task_id": "T-multi",
                    "state": "closed",
                    "slot_reserved": False,
                    "task_card": {
                        "mode": "multi-discipline",
                        "access_policy": copy.deepcopy(sealed),
                    },
                    "attempts": [],
                }
            }
        )
        for lane, mode in (
            ("A", "brainstorm"),
            ("B", "multi-discipline"),
            ("C", "computation"),
            ("D", "associate"),
        ):
            task_id = f"T-sprint-{lane}"
            snapshot["scheduler"]["tasks"][task_id] = {
                "task_id": task_id,
                "sprint_id": "S-1",
                "state": "closed",
                "slot_reserved": False,
                "task_card": {
                    "mode": mode,
                    "access_policy": copy.deepcopy(sealed),
                },
                "attempts": [],
            }
        frozen = {
            "plan": {"target_obligation": {"id": "O-root"}},
            "lane_results": [],
        }
        snapshot["scheduler"]["sprints"] = {
            "S-1": {"frozen_synthesis_input": frozen}
        }
        snapshot["scheduler"]["calls"]["CALL-sprint-summary"] = {
            "call_id": "CALL-sprint-summary",
            "kind": "summarizer",
            "status": "committed",
            "continuation": {"sprint_id": "S-1"},
            "input": copy.deepcopy(frozen),
        }
        result = evaluate_snapshot(snapshot).scenario("isolated_access")
        self.assertEqual(result.status, ScenarioStatus.PASS)
        self.assertEqual(
            result.evidence["coverage"],
            {
                "brainstorm": True,
                "multi_discipline": True,
                "discovery_sprint_lanes": True,
                "discovery_sprint_summarizer": True,
            },
        )

        snapshot["scheduler"]["calls"]["CALL-sprint-summary"]["input"][
            "extra_project_context"
        ] = {"route": "R-secret"}
        result = evaluate_snapshot(snapshot).scenario("isolated_access")
        self.assertEqual(result.status, ScenarioStatus.FAIL)
        self.assertTrue(
            any("beyond or different" in item for item in result.findings)
        )

        snapshot["scheduler"]["calls"]["CALL-sprint-summary"]["input"].pop(
            "extra_project_context"
        )
        snapshot["scheduler"]["tasks"].pop("T-sprint-D")
        result = evaluate_snapshot(snapshot).scenario("isolated_access")
        self.assertEqual(result.status, ScenarioStatus.FAIL)
        self.assertTrue(
            any("exactly one sealed lane" in item for item in result.findings)
        )

    def test_multi_discipline_requires_explicit_sealed_policy(self) -> None:
        snapshot = fixture()
        task = snapshot["scheduler"]["tasks"]["T-isolated"]
        task["task_card"]["mode"] = "multi-discipline"
        task["assign_record"]["mode"] = "multi-discipline"
        task["task_card"]["access_policy"].pop("sealed_workspace")
        result = evaluate_snapshot(snapshot).scenario("isolated_access")
        self.assertEqual(result.status, ScenarioStatus.FAIL)
        self.assertTrue(
            any(
                "not marked as a sealed workspace" in item
                for item in result.findings
            )
        )

    def test_wrong_skill_context_fails_even_with_partial_coverage(self) -> None:
        snapshot = fixture()
        snapshot["observation_scope"] = {}
        snapshot["skill_activity"].append(
            {
                "skill": "internal-search",
                "role": "worker",
                "mode": "brainstorm",
                "task_id": "T-isolated",
            }
        )
        result = evaluate_snapshot(snapshot).scenario("skill_use")
        self.assertEqual(result.status, ScenarioStatus.FAIL)

    def test_skill_use_requires_final_only_for_normal_worker_exit(self) -> None:
        snapshot = fixture()
        task = snapshot["scheduler"]["tasks"]["T-isolated"]
        task["attempts"][0].pop("final_progress_id")
        snapshot["skill_activity"] = [
            event
            for event in snapshot["skill_activity"]
            if not (
                event.get("skill") == "record-progress"
                and event.get("artifact", {}).get("task_id") == "T-isolated"
            )
        ]
        result = evaluate_snapshot(snapshot).scenario("skill_use")
        self.assertEqual(result.status, ScenarioStatus.FAIL)
        self.assertTrue(
            any("T-isolated attempt 1" in item for item in result.findings)
        )

        task["attempts"][0]["state"] = "interrupted"
        result = evaluate_snapshot(snapshot).scenario("skill_use")
        self.assertEqual(result.status, ScenarioStatus.PASS)

    def test_declared_imbalance_without_completed_redraw_fails(self) -> None:
        snapshot = fixture()
        snapshot["categories"] = {
            "categories": {"CAT-1": {"id": "CAT-1", "status": "active"}},
            "category_history": {},
            "events": [],
        }
        snapshot["live_events"] = [
            event
            for event in snapshot["live_events"]
            if event["type"] not in {"category_redraw"}
        ]
        result = evaluate_snapshot(snapshot).scenario("category_redraw")
        self.assertEqual(result.status, ScenarioStatus.FAIL)

    def test_committed_category_proposal_with_touched_ids_is_redraw_evidence(self) -> None:
        snapshot = fixture()
        snapshot["categories"] = {
            "categories": {"CAT-new": {"id": "CAT-new", "status": "active"}},
            "category_history": {},
            "events": [
                {
                    "event_index": 1,
                    "event_type": "category_proposal_committed",
                    "details": {
                        "operation_id": "trim-create-only",
                        "category_ids": ["CAT-new"],
                    },
                }
            ],
        }
        snapshot["live_events"] = [
            {
                "type": "category_balance_review",
                "verdict": "unbalanced",
                "rationale": "A newly visible mechanism needs its own category.",
            }
        ]
        result = evaluate_snapshot(snapshot).scenario("category_redraw")
        self.assertEqual(result.status, ScenarioStatus.PASS)
        self.assertIn(
            "event:category_proposal_committed:CAT-new",
            result.evidence["redraw_evidence"],
        )

        snapshot["categories"]["events"][0]["details"]["category_ids"] = []
        result = evaluate_snapshot(snapshot).scenario("category_redraw")
        self.assertEqual(result.status, ScenarioStatus.FAIL)

    def test_repair_loop_rejects_more_than_two_revision_requests(self) -> None:
        snapshot = fixture()
        lineage = snapshot["scheduler"]["fact_lineages"]["FC-root"]
        lineage["revision_requests"] = 3
        lineage["rejected_bundle_digests"] = ["d1", "d2", "d3"]
        result = evaluate_snapshot(snapshot).scenario("fact_repair")
        self.assertEqual(result.status, ScenarioStatus.FAIL)
        self.assertTrue(any("two-request" in finding for finding in result.findings))

    def test_restart_invalid_fence_and_stale_acceptance_fail(self) -> None:
        snapshot = fixture()
        call = snapshot["scheduler"]["calls"]["CALL-main"]
        call["lease_epoch"] = 1
        snapshot["live_events"].append(
            {"type": "stale_call_output_accepted", "call_id": "CALL-main"}
        )
        result = evaluate_snapshot(snapshot).scenario("restart_resume")
        self.assertEqual(result.status, ScenarioStatus.FAIL)

    def test_research_launch_after_root_resolution_fails(self) -> None:
        snapshot = fixture()
        snapshot["scheduler"]["tasks"]["T-late"] = {
            "task_id": "T-late",
            "state": "closed",
            "slot_reserved": False,
            "assign_record": {"report_id": "AR-late", "mode": "research"},
            "task_card": {"mode": "research", "access_policy": {}},
            "attempts": [],
        }
        snapshot["scheduler"]["events"].append(
            {
                "event_id": 10,
                "type": "task_launch_intent_committed",
                "payload": {"task_id": "T-late"},
            }
        )
        result = evaluate_snapshot(snapshot).scenario("root_stopping")
        self.assertEqual(result.status, ScenarioStatus.FAIL)
        self.assertIn("T-late", result.evidence["post_resolution_research_launches"])

    def test_cli_friendly_main_writes_json_and_returns_failure_only_for_fail(self) -> None:
        output = io.StringIO()
        return_code = main(["--snapshot", str(FIXTURE), "--compact"], output=output)
        self.assertEqual(return_code, 0)
        self.assertEqual(json.loads(output.getvalue())["status"], "pass")

        snapshot = fixture()
        snapshot["scheduler"]["gate"] = "not-a-gate"
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "bad.json"
            path.write_text(json.dumps(snapshot), encoding="utf-8")
            output = io.StringIO()
            return_code = main(["--snapshot", str(path), "--compact"], output=output)
        self.assertEqual(return_code, 1)
        self.assertEqual(json.loads(output.getvalue())["status"], "fail")


if __name__ == "__main__":
    unittest.main()
