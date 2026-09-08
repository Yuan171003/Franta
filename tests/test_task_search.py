from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any, Mapping

from franta.access import (
    AccessError,
    AuditLog,
    AuditedTaskAPI,
    BrokerClient,
    InMemoryBackend,
    MemoryBroker,
    policy_for,
)
from franta.skill_runtime import allowed_skills


class _Tasks:
    def get_task_summary(self, task_id: str) -> Mapping[str, Any] | None:
        if task_id != "T-1":
            return None
        return {
            "task_id": task_id,
            "final_summary": "The summary is sufficient unless the proof log is needed.",
            "artifacts": [{"artifact_id": "TA-proof", "kind": "progress"}],
        }

    def get_task_artifact(
        self, task_id: str, artifact_id: str
    ) -> Mapping[str, Any] | None:
        if (task_id, artifact_id) != ("T-1", "TA-proof"):
            return None
        return {
            "task_id": task_id,
            "artifact_id": artifact_id,
            "encoding": "utf-8",
            "content": "Full proof log.",
        }


class TaskSearchTests(unittest.TestCase):
    def test_only_main_and_trimmer_receive_task_search_skill(self) -> None:
        self.assertIn("task-search", allowed_skills(policy_for("main")))
        self.assertIn("task-search", allowed_skills(policy_for("trimmer")))
        self.assertNotIn(
            "task-search", allowed_skills(policy_for("worker", mode="research"))
        )
        self.assertNotIn("task-search", allowed_skills(policy_for("verifier")))

    def test_task_summary_and_selected_fetch_are_separately_audited(self) -> None:
        audit = AuditLog()
        api = AuditedTaskAPI(
            _Tasks(), policy_for("main"), audit=audit, caller_id="CALL-MAIN"
        )
        summary = api.summary("T-1")
        self.assertNotIn("content", summary["artifacts"][0])
        artifact = api.fetch("T-1", "TA-proof")
        self.assertEqual(artifact["content"], "Full proof log.")
        self.assertEqual(
            [event["action"] for event in audit.events],
            ["task_summary_read", "task_artifact_read"],
        )

        verifier = AuditedTaskAPI(
            _Tasks(), policy_for("verifier"), audit=AuditLog(), caller_id="VERIFY"
        )
        with self.assertRaises(AccessError):
            verifier.summary("T-1")

    def test_broker_exposes_task_and_staging_tools_by_exact_capability(self) -> None:
        staged: list[tuple[str, Mapping[str, Any]]] = []
        with tempfile.TemporaryDirectory() as raw:
            broker = MemoryBroker(InMemoryBackend(), task_backend=_Tasks())
            broker.start(Path(raw) / "broker.sock")
            try:
                binding = broker.issue(
                    policy_for("main"),
                    caller_id="CALL-MAIN",
                    staging_handler=lambda skill, payload: (
                        staged.append((skill, dict(payload)))
                        or {"operation_id": payload["operation_id"]}
                    ),
                    staging_skills={"task-writing"},
                )
                client = BrokerClient(binding.socket_path, binding.token)
                description = client.call("describe", {})
                self.assertEqual(
                    set(description["enabled_tools"]),
                    {
                        "task_summary",
                        "task_artifact_fetch",
                        "task_writing",
                    },
                )
                self.assertNotIn("internal_search", description["enabled_tools"])
                self.assertNotIn("memory_fetch", description["enabled_tools"])
                self.assertEqual(client.call("task_summary", {"task_id": "T-1"})["task_id"], "T-1")
                client.call(
                    "task_writing",
                    {"payload": {"operation_id": "AR-1"}},
                )
                self.assertEqual(staged, [("task-writing", {"operation_id": "AR-1"})])
                with self.assertRaises(Exception):
                    client.call("record_progress", {"payload": {"operation_id": "P-1"}})
            finally:
                broker.stop()


if __name__ == "__main__":
    unittest.main()
