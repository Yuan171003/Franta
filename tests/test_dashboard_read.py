from __future__ import annotations

import hashlib
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from franta.dashboard_read import FrantaDashboardRead, _read_connection
from franta.contracts.canonical import MemoryType
from franta.render import render_record
from franta.store import MemoryStore
from explorer_system.contracts import ExplorerWriteContext
from explorer_system.repository import ExplorerRepository


def obligation(**changes):
    return {
        "abstract": "An auxiliary proposition", "statement": "The obstruction vanishes.",
        "importance": "It resolves the problem.", "predecessor_fact_ids": [],
        "partial_progress": [], "related_route_ids": [], "relations": [], **changes,
    }


def route(**changes):
    return {
        "abstract": "A degeneration route", "strategy_description": "Use degeneration.",
        "value_assessment": {key: "Promising" for key in ("confidence", "success_gain", "failure_gain", "relevance", "novelty")},
        "progress": [], "next_steps": [], "obstacles": [], "related_obligation_ids": [],
        "active_fact_ids": [], "relevant_memo_ids": [], "relevant_claim_ids": [], **changes,
    }


class DashboardReadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "private").mkdir()
        (self.root / "private/runtime-config.json").write_text(json.dumps({
            "project_name": "Dashboard fixture", "root_problem": "Prove ROOT.",
        }))
        self.store = MemoryStore(self.root / "scheduler.sqlite3", projection_dir=False)
        self.addCleanup(self.store.close)
        self.explorer = ExplorerRepository(self.root / "private/explorer.sqlite3")
        self.addCleanup(self.explorer.close)
        self.state = {"root": {"problem": "Prove ROOT."}, "tasks": {}, "calls": {}, "gate": "open"}
        self.save_state()
        self.read = FrantaDashboardRead(self.root)

    def save_state(self):
        self.store.save_control_state("scheduler.v1", self.state)

    def fact(self, **changes):
        task_id = self.store.allocate_id(MemoryType.TASK)
        result = self.store.add_fact("fact-" + task_id, {
            "abstract": "A verified lemma", "statement": "The base case holds.",
            "proof": "By direct calculation.", "predecessor_fact_ids": [],
            "originating_task_id": task_id, "foundation_policy_version": 1,
            "introduced_notation": [], "external_references": [], "root_resolution": None,
            "keywords": [], "related_route_ids": [], **changes,
        })
        self.assertEqual(result.status, "committed", result)
        return result.canonical_id

    def scratch(self, label, *, trusted=True, attempt=1, session="EWORK-1", summary_sources=None):
        receipt = hashlib.sha256(label.encode()).hexdigest()
        context = ExplorerWriteContext(turn_id="ETURN-1", worker_session_id=session, attempt_no=attempt, call_id="CALL-" + label)
        payload = {"operation_id": "OP-" + label, "abstract": "Idea " + label, "content": "Proof text " + label}
        if summary_sources is None:
            payload.update(skill="record-scratch", record_id="ES-" + label, record_kind="idea", related_memory_ids=[], cas_operation_ids=[])
            item = self.explorer.prepare_scratch(context, payload, receipt)
        else:
            payload.update(skill="record-summary", record_id="ESUM-" + label, directions_tried=["Use the filtration"], main_progress="A reduction", main_obstacles="A boundary map", source_scratch_ids=summary_sources)
            item = self.explorer.prepare_summary(context, payload, receipt)
        if trusted:
            self.explorer.trust_receipt(receipt)
        return item.record_id

    def log(self, name, lines, *, append=False):
        path = self.root / "private/transport/calls" / (name + ".jsonl")
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a" if append else "w") as handle:
            for line in lines:
                handle.write(json.dumps(line) + "\n")
        return path

    @staticmethod
    def start(second=0, attempt=1):
        return {"type": "transport.call_started", "time": f"2026-01-01T00:00:{second:02d}Z", "lease_epoch": 1, "launch_attempt": attempt}

    @staticmethod
    def usage(input_tokens=100, cached=70, output=20):
        return {"type": "transport.codex_event", "event": {"type": "turn.completed", "usage": {"input_tokens": input_tokens, "cached_input_tokens": cached, "output_tokens": output}}}

    @staticmethod
    def end(second):
        return {"type": "transport.call_ended", "time": f"2026-01-01T00:00:{second:02d}Z", "returncode": 0}

    def test_readonly_connection_rejects_writes_and_preserves_wal_visibility(self):
        fact_id = self.fact()
        with _read_connection(self.root / "scheduler.sqlite3") as connection:
            self.assertEqual(connection.execute("SELECT memory_id FROM memories").fetchone()[0], fact_id)
            for sql in ("DELETE FROM memories", "CREATE TABLE rogue(x)", "PRAGMA query_only=OFF", "ATTACH DATABASE ':memory:' AS rogue"):
                with self.subTest(sql=sql), self.assertRaises(sqlite3.DatabaseError):
                    connection.execute(sql)
        self.assertEqual(self.read.main_record(fact_id)["status"], "active")

    def test_no_runtime_or_repository_initialization_or_source_mutation(self):
        fact_id = self.fact()
        self.scratch("visible")
        paths = [self.root / "scheduler.sqlite3", self.root / "scheduler.sqlite3-wal", self.root / "private/explorer.sqlite3", self.root / "private/explorer.sqlite3-wal"]
        before = {path: path.read_bytes() for path in paths}
        state_before = self.store.load_control_state("scheduler.v1")
        audit_before = self.store.list_read_audit()
        with patch.object(MemoryStore, "__init__", side_effect=AssertionError("writer constructor")), patch.object(ExplorerRepository, "__init__", side_effect=AssertionError("writer constructor")):
            read = FrantaDashboardRead(self.root)
            read.overview()
            read.main_record(fact_id)
            read.main_memory()
            read.explorer_memory(record_type="scratch")
            read.monitor_snapshot()
        self.assertEqual(before, {path: path.read_bytes() for path in paths})
        self.assertEqual(state_before, self.store.load_control_state("scheduler.v1"))
        self.assertEqual(audit_before, self.store.list_read_audit())
        self.assertFalse((self.root / "audit").exists())

    def test_canonical_effective_status_and_reciprocal_links_match_store(self):
        route_id = self.store.add_route("route", route()).canonical_id
        fact_id = self.fact(related_route_ids=[route_id])
        obligation_id = self.store.add_obligation("obligation", obligation(predecessor_fact_ids=[fact_id], related_route_ids=[route_id])).canonical_id
        for record_id in (route_id, fact_id, obligation_id):
            self.assertEqual(self.read.main_record(record_id)["content"], render_record(self.store.get(record_id)))
        self.assertIn({"kind": "related_route", "target_id": route_id}, self.read.main_record(fact_id)["relations"])
        self.assertIn({"kind": "active_fact", "target_id": fact_id}, self.read.main_record(route_id)["relations"])
        self.store.revoke_fact(fact_id, reason="counterexample", actor="test")
        self.assertEqual(self.read.main_record(fact_id)["status"], "revoked")
        self.assertEqual(self.read.main_record(obligation_id)["status"], "unsupported")
        self.assertFalse(self.read.main_record(fact_id)["active"])

    def test_deferred_relation_is_projected_with_original_semantics(self):
        source = self.store.add_obligation("pending-relation", obligation(relations=[{
            "relation_type": "suffices", "premise_memory_ids": ["TMP-PREMISE"],
            "conclusion": "ROOT", "explanation": "This premise suffices.", "supporting_fact_ids": [],
        }])).canonical_id
        self.assertIsNotNone(source)
        before = self.read.main_record(source)
        self.assertEqual(before["content"], render_record(self.store.get(source)))
        self.assertEqual(before["relations"][0]["target_id"], "TMP-PREMISE")
        self.assertEqual(before["relations"][0]["relation_kind"], "suffices")
        target = self.store.add_obligation("publish-premise", obligation(), proposal_id="TMP-PREMISE").canonical_id
        after = self.read.main_record(source)
        self.assertEqual(after["content"], render_record(self.store.get(source)))
        self.assertEqual(after["relations"][0]["target_id"], target)

    def test_main_root_resolution_and_withdrawn_claim(self):
        root_id = self.store.add_obligation("root", obligation()).canonical_id
        self.store.set_root_obligation(root_id)
        self.fact(root_resolution={"target": "ROOT", "outcome": "proved"})
        self.assertEqual(self.read.main_record(root_id)["status"], "resolved")
        claim_id = self.store.add_claim("claim", {"abstract": "A conjecture", "content": "Might hold.", "related_route_ids": []}).canonical_id
        # Inactive status is an independent canonical overlay, not text in core_json.
        self.store._connection.execute("UPDATE memories SET active=0 WHERE memory_id=?", (claim_id,))
        self.assertEqual(self.read.main_record(claim_id)["status"], "withdrawn")

    def test_main_filter_search_pagination_and_private_types(self):
        fact_id = self.fact()
        self.store.add_route("route", route())
        memo_id = self.store.add_memo("memo", {"abstract": "A note", "genre": "high-level", "content": "A heuristic.", "related_route_ids": []}).canonical_id
        self.assertEqual(self.read.main_memory()["total"], 2)
        self.assertEqual(self.read.main_memory(kind="fact")["items"][0]["id"], fact_id)
        self.assertEqual(self.read.main_memory(query="VERIFIED")["total"], 1)
        self.assertEqual(self.read.main_memory(query="%_")["total"], 0)
        first = self.read.main_memory(limit=1)
        second = self.read.main_memory(limit=1, offset=1)
        self.assertNotEqual(first["items"][0]["id"], second["items"][0]["id"])
        self.assertEqual(self.read.main_memory(offset=99)["items"], [])
        self.assertIn(memo_id, self.read.monitor_snapshot()["source_ids"])
        self.assertIsNone(self.read.main_record("does-not-exist"))
        with self.assertRaises(ValueError):
            self.read.main_memory(kind="task")

    def test_memory_graph_includes_all_public_types_and_deduplicates_links(self):
        route_id = self.store.add_route("route", route()).canonical_id
        fact_id = self.fact(related_route_ids=[route_id])
        obligation_id = self.store.add_obligation("obligation", obligation(predecessor_fact_ids=[fact_id], related_route_ids=[route_id])).canonical_id
        claim_id = self.store.add_claim("claim", {"abstract": "A conjectural bridge", "content": "The bridge may hold.", "related_route_ids": [route_id]}).canonical_id
        graph = self.read.memory_graph()
        self.assertEqual({item["type"] for item in graph["nodes"]}, {"route", "fact", "obligation", "claim"})
        self.assertEqual({item["id"] for item in graph["nodes"]}, {route_id, fact_id, obligation_id, claim_id})
        pairs = [{edge["source"], edge["target"]} for edge in graph["edges"]]
        self.assertIn({route_id, fact_id}, pairs)
        self.assertIn({fact_id, obligation_id}, pairs)
        self.assertIn({route_id, claim_id}, pairs)
        self.assertEqual(sum(pair == {route_id, fact_id} for pair in pairs), 1)

    def test_explorer_trust_sequence_pagination_and_summary_sources(self):
        untrusted = self.scratch("untrusted", trusted=False)
        first = self.scratch("first")
        second = self.scratch("second")
        summary = self.scratch("summary", summary_sources=[first, second])
        page = self.read.explorer_memory(record_type="scratch", limit=1)
        self.assertEqual(page["total"], 2)
        self.assertEqual(page["items"][0]["id"], second)
        self.assertEqual(self.read.explorer_memory(record_type="scratch", offset=1)["items"][0]["id"], first)
        self.assertEqual(self.read.explorer_memory(record_type="summary")["items"][0]["data"]["source_scratch_ids"], [first, second])
        snapshot = self.read.monitor_snapshot()
        self.assertEqual([item["id"] for item in snapshot["explorer"]], [summary, second, first])
        self.assertNotIn(untrusted, snapshot["source_ids"])
        self.assertEqual(snapshot["explorer_high_water_seq"], 3)
        self.assertIsInstance(snapshot["source_revision"], int)
        self.assertIn("run", snapshot)

    def test_monitor_snapshot_identifies_cycle_start_through_phase_changes(self):
        started = "2026-09-07T09:00:00+00:00"
        for phase in ("explorer_admission", "franta_sort", "franta_run", "franta_drain"):
            self.state["phase_control"] = {
                "cycle": 3, "phase": phase,
                "explorer": {"admission_started_at": started},
            }
            self.save_state()
            with self.subTest(phase=phase):
                run = self.read.monitor_snapshot()["run"]
                self.assertEqual(run["cycle"], 3)
                self.assertEqual(run["cycle_started_at"], started)
                self.assertEqual(run["phase"], phase)

    def test_latest_means_visibility_not_creation_time(self):
        first_created = self.scratch("prepared-first", trusted=False)
        second_created = self.scratch("trusted-first")
        self.explorer.trust_receipt(hashlib.sha256(b"prepared-first").hexdigest())
        records = self.read.explorer_memory(record_type="scratch")["items"]
        self.assertEqual([item["id"] for item in records], [first_created, second_created])

    def test_tokens_partial_lines_duplicates_and_physical_retries(self):
        self.state["calls"] = {"CALL-1": {"status": "committed", "attempt": 2}, "CALL-MISSING": {"status": "running", "attempt": 1}}
        self.save_state()
        path = self.log("CALL-1", [self.start()])
        completed = (json.dumps(self.usage()) + "\n").encode()
        with path.open("ab") as handle:
            handle.write(completed[:25])
        self.assertIsNone(self.read.overview()["usage"]["total_tokens"])
        with path.open("ab") as handle:
            handle.write(completed[25:])
        self.assertEqual(self.read.overview()["usage"]["total_tokens"], 120)
        self.log("CALL-1", [self.usage(), self.end(10), self.start(12, attempt=2), self.usage(), self.end(20)], append=True)
        usage = self.read.overview()["usage"]
        self.assertEqual((usage["input_tokens"], usage["cached_input_tokens"], usage["output_tokens"], usage["total_tokens"]), (200, 140, 40, 240))
        self.assertEqual((usage["reported_calls"], usage["unreported_calls"]), (2, 1))
        self.assertEqual(self.read.overview()["usage"], usage)

    def test_unavailable_tokens_are_null_not_zero(self):
        usage = self.read.overview()["usage"]
        self.assertIsNone(usage["input_tokens"])
        self.assertIsNone(usage["total_tokens"])
        self.log("CALL-1", [self.start(), self.end(5)])
        self.assertEqual(self.read.overview()["usage"]["unreported_calls"], 1)
        self.assertIsNone(self.read.overview()["usage"]["total_tokens"])

    def test_token_index_reloads_truncated_log_and_ignores_bad_records(self):
        self.log("CALL-1", [self.start(), self.usage(10000, 0, 10000), self.end(20)])
        self.assertEqual(self.read.overview()["usage"]["total_tokens"], 20000)
        path = self.log("CALL-1", [self.start(), self.usage(1, 0, 2)])
        with path.open("ab") as handle:
            handle.write(b"bad json\n\xff\n")
        self.assertEqual(self.read.overview()["usage"]["total_tokens"], 3)

    def test_token_index_detects_same_size_log_replacement(self):
        path = self.log("CALL-1", [self.start(), self.usage(1, 0, 2)])
        self.assertEqual(self.read.overview()["usage"]["total_tokens"], 3)
        previous_time = path.stat().st_mtime_ns
        self.log("CALL-1", [self.start(), self.usage(2, 0, 3)])
        os.utime(path, ns=(previous_time + 1000000, previous_time + 1000000))
        self.assertEqual(self.read.overview()["usage"]["total_tokens"], 5)

    def test_active_duration_unions_parallel_calls(self):
        self.log("CALL-1", [self.start(0), self.end(20)])
        self.log("CALL-2", [self.start(10), self.end(30)])
        run = self.read.overview()["run"]
        self.assertEqual(run["active_seconds"], 30)
        self.assertEqual(run["elapsed_seconds"], 30)

    def test_live_call_duration_updates_but_orphans_do_not_bridge_restarts(self):
        self.log("CALL-1", [self.start(0)])
        self.state["calls"] = {"CALL-1": {"status": "running", "attempt": 1, "lease_epoch": 1}}
        self.save_state()
        runner = self.root / "private/dashboard/runner.json"
        runner.parent.mkdir()
        runner.write_text(json.dumps({"pid": os.getpid(), "status": "running", "started_at": "2026-01-01T00:00:00Z"}))
        now = datetime(2026, 1, 1, 0, 0, 30, tzinfo=timezone.utc)
        self.assertEqual(self.read._usage_and_run(self.state, now)[1]["active_seconds"], 30)
        runner.write_text(json.dumps({"pid": os.getpid(), "status": "running", "started_at": "2026-01-01T00:00:20Z"}))
        self.assertEqual(self.read._usage_and_run(self.state, now)[1]["active_seconds"], 0)

    def test_active_directions_are_reported_from_current_attempt_only(self):
        first = self.scratch("old", attempt=1)
        self.state["tasks"] = {
            "T-1": {"state": "running", "task_card": {"objective": "Compute the obstruction", "mode": "computation"}, "current_attempt": 1, "session_lineage_id": "SESSION-1", "attempts": []},
            "T-CLOSED": {"state": "closed", "task_card": {"objective": "An old direction"}},
        }
        self.state["explorer_control"] = {"lineages": {"EWORK-1": {"status": "running", "attempts": [{"attempt_number": 2}]}}}
        self.save_state()
        overview = self.read.overview()
        self.assertEqual(len(overview["directions"]), 2)
        explorer = next(item for item in overview["directions"] if item["system"] == "explorer")
        self.assertEqual(explorer["objective"], "Research direction not reported")
        self.assertEqual(explorer["source_ids"], [])
        second = self.scratch("new", attempt=2)
        explorer = next(item for item in self.read.overview()["directions"] if item["system"] == "explorer")
        self.assertEqual(explorer["objective"], "Idea new")
        self.assertEqual(explorer["source_ids"], [second])
        self.assertNotIn(first, explorer["source_ids"])

    def test_live_worker_roster_reports_operator_facing_roles_and_modes(self):
        self.state["calls"] = {
            "CALL-MAIN": {"kind": "main", "status": "running", "attempt": 1},
            "CALL-WORKER": {"kind": "worker", "status": "running", "attempt": 2, "input": {"task_card": {"task_id": "T-1", "mode": "research", "objective": "Prove the bridge."}}},
            "CALL-EXPLORER": {"kind": "explorer-worker", "status": "running", "attempt": 1, "input": {"access_mode": "full-memory", "worker_session_id": "XLINEAGE-1"}},
            "CALL-SORT": {"kind": "main-sort", "status": "running", "attempt": 1, "input": {"sort_run_id": "XSORT-1"}},
            "CALL-VERIFIER": {"kind": "verifier", "status": "running", "attempt": 1},
            "CALL-OLD": {"kind": "worker", "status": "committed", "attempt": 1},
        }
        self.save_state()
        runner = self.root / "private/dashboard/runner.json"
        runner.parent.mkdir(exist_ok=True)
        runner.write_text(json.dumps({"pid": os.getpid(), "status": "running", "started_at": "2026-01-01T00:00:00Z"}))
        workers = self.read.overview()["workers"]
        self.assertEqual({item["kind"] for item in workers}, {"main", "worker", "explorer-worker", "main-sort"})
        franta_worker = next(item for item in workers if item["kind"] == "worker")
        self.assertEqual((franta_worker["worker_id"], franta_worker["mode"], franta_worker["objective"]), ("T-1", "research", "Prove the bridge."))
        with patch("franta.dashboard_read.os.kill", side_effect=ProcessLookupError):
            self.assertEqual(self.read.overview()["workers"], [])

    def test_live_runner_does_not_require_frequent_heartbeat(self):
        runner = self.root / "private/dashboard/runner.json"
        runner.parent.mkdir()
        runner.write_text(json.dumps({"pid": os.getpid(), "status": "running", "started_at": "2026-01-01T00:00:00Z", "updated_at": "2026-01-01T00:00:00Z"}))
        self.assertEqual(self.read.overview()["run"]["status"], "running")
        with patch("franta.dashboard_read.os.kill", side_effect=ProcessLookupError):
            self.assertEqual(self.read.overview()["run"]["status"], "stopped")

    def test_advisor_report_and_guidance_are_current_read_views(self):
        report = {"feedback_request_id": "AFB-1", "recommendation": "Study the boundary map"}
        self.state["advisor_control"] = {"active": {"status": "waiting_for_human", "selection_report": report}, "history": []}
        self.state["human_guidance_inbox"] = {"HG-1": {"guidance_id": "HG-1", "text": "Try a filtration", "status": "assigned", "received_at": "2026-01-01T00:00:00Z"}}
        self.save_state()
        overview = self.read.overview()
        self.assertEqual(overview["advisor"]["request_id"], "AFB-1")
        self.assertEqual(overview["advisor"]["report"], report)
        self.assertEqual(overview["guidance"][0]["status"], "assigned")

    def test_monitor_costs_are_separate_and_feedback_receipts_are_visible(self):
        self.log("CALL-1", [self.start(), self.usage(), self.end(10)])
        usage_path = self.root / "private/dashboard/monitor-runs/MON-1/usage.json"
        usage_path.parent.mkdir(parents=True)
        usage_path.write_text(json.dumps({"usage": [{"input_tokens": 30, "cached_input_tokens": 20, "output_tokens": 5}]}))
        receipts = self.root / "private/dashboard/receipts"
        receipts.mkdir()
        (receipts / "AFB-1.json").write_text(json.dumps({"command_id": "AFB-1", "request_id": "REQ-1", "status": "accepted", "processed_at": "2026-01-01T00:00:00Z"}))
        overview = self.read.overview()
        self.assertEqual(overview["usage"]["total_tokens"], 120)
        self.assertEqual(overview["usage"]["monitor"]["total_tokens"], 35)
        self.assertEqual(overview["usage"]["monitor"]["reported_calls"], 1)
        self.assertEqual(overview["advisor"]["feedback_receipts"][0]["status"], "accepted")

    def test_timed_out_monitor_without_usage_is_counted_as_unreported(self):
        path = self.root / "private/dashboard/monitor-runs/MON-TIMEOUT/usage.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({
            "started_at": "2026-01-01T00:00:00Z",
            "ended_at": "2026-01-01T00:05:00Z",
            "returncode": -15, "usage": [],
        }))
        usage = self.read.overview()["usage"]
        self.assertIsNone(usage["monitor"]["total_tokens"])
        self.assertEqual(usage["monitor"]["reported_calls"], 0)
        self.assertEqual(usage["monitor"]["unreported_calls"], 1)
        self.assertEqual(usage["unreported_calls"], 0)

    def test_invalid_pagination_and_empty_project_do_not_create_database(self):
        for arguments in ({"offset": -1}, {"limit": 0}, {"limit": 501}, {"offset": True}):
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                self.read.main_memory(**arguments)
        with self.assertRaises(ValueError):
            self.read.explorer_memory(record_type="all")
        empty = self.root / "empty"
        empty.mkdir()
        read = FrantaDashboardRead(empty)
        self.assertEqual(read.main_memory()["items"], [])
        self.assertEqual(read.explorer_memory(record_type="scratch")["items"], [])
        self.assertIsNone(read.overview()["usage"]["total_tokens"])
        self.assertEqual(list(empty.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
