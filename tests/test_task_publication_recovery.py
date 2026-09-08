from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

from franta.models import NotFoundError, OperationResult
from franta.runtime import _RuntimeTaskBackend
from franta.scheduler import Scheduler
from franta.store import MemoryStore
from franta.workflows import TaskState


class _RejectFirstTaskPublicationStore(MemoryStore):
    """Durably reject the first primary task-close key."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)
        self.reject_first_task_close = True

    def add_task(
        self,
        idempotency_key: str,
        payload: dict[str, object],
        *,
        actor: str = "scheduler",
    ):
        if self.reject_first_task_close and idempotency_key.startswith("task-close:T-"):
            self.reject_first_task_close = False
            invalid = dict(payload)
            invalid["final_status"] = "invalid-test-status"
            return super().add_task(idempotency_key, invalid, actor=actor)
        return super().add_task(idempotency_key, payload, actor=actor)


class _LegacyMissingTaskPublicationStore(MemoryStore):
    """Emulate an old close whose canonical publication never committed."""

    emulate_legacy_close = True
    block_repair = True

    def add_task(
        self,
        idempotency_key: str,
        payload: dict[str, object],
        *,
        actor: str = "scheduler",
    ) -> OperationResult:
        if self.emulate_legacy_close:
            # Pre-two-phase control state could become CLOSED even though no
            # canonical task record existed.  Return a transient success only
            # to construct that historical snapshot; persist no store result.
            return OperationResult(
                operation_id=idempotency_key,
                operation_type="task_publish",
                status="committed",
                canonical_ids=(str(payload["id"]),),
                resolution="published",
            )
        if self.block_repair:
            raise RuntimeError("persistent canonical task-publication outage")
        return super().add_task(idempotency_key, payload, actor=actor)


class _CrashAfterTaskPublicationScheduler(Scheduler):
    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)
        self.crash_before_control_close = True

    def _finalize_published_task_close(self, *args: object, **kwargs: object) -> None:
        if self.crash_before_control_close:
            self.crash_before_control_close = False
            raise RuntimeError("simulated crash after task publication")
        super()._finalize_published_task_close(*args, **kwargs)


class _CrashBeforeTailMarkerScheduler(Scheduler):
    crash_task_id: str | None = None

    def _mark_task_close_tail_applied(self, task_id: str) -> None:
        if task_id == self.crash_task_id:
            self.crash_task_id = None
            raise RuntimeError("simulated crash after downstream close effects")
        super()._mark_task_close_tail_applied(task_id)


def _report() -> dict[str, object]:
    return {
        "report_id": "AR-TASK-PUBLICATION",
        "objective": "Investigate the test obstruction.",
        "if_resume": None,
        "mode": "associate",
        "main_route_ids": [],
        "main_obligation_ids": [],
        "perspective": None,
        "portfolio": {
            kind: []
            for kind in (
                "fact",
                "route",
                "memo",
                "claim",
                "obligation",
                "computation",
            )
        },
        "reason": "Exercise canonical task publication.",
    }


def _final_progress(task_id: str, attempt: int) -> dict[str, object]:
    return {
        "progress_id": f"PRG-{task_id}-{attempt}",
        "task_id": task_id,
        "attempt": attempt,
        "sequence": 1,
        "is_final": True,
        "outcome_status": "progress",
        "attempt_summary": {
            "summary": "Useful progress",
            "proposed_outcome": "progress",
            "completion_evidence_operation_ids": [],
        },
        "operations": [],
        "completion_evidence_ids": [],
    }


def _scheduler(
    store: MemoryStore, scheduler_type: type[Scheduler] = Scheduler
) -> Scheduler:
    scheduler = scheduler_type(store)
    root_id = scheduler.bootstrap(root_problem="Prove ROOT.")
    result = store.add_obligation(
        "bootstrap-root-task-publication",
        {
            "id": root_id,
            "abstract": "The root target.",
            "statement": "ROOT holds.",
            "importance": "This is the project target.",
            "predecessor_fact_ids": [],
            "partial_progress": [],
            "related_route_ids": [],
            "relations": [],
        },
    )
    if result.status != "committed":
        raise AssertionError(result.error)
    scheduler.bind_canonical_root_obligation()
    scheduler.commit_initial_trim({"category_ids": []})
    return scheduler


class TaskPublicationRecoveryTests(unittest.TestCase):
    def _attach_artifact(
        self,
        scheduler: Scheduler,
        archive_root: Path,
        task_id: str,
    ) -> tuple[str, str]:
        content = "Durable mathematical progress.\n"
        relative_path = f"{task_id}/CALL-1/outbox/record-progress/final.json"
        artifact = archive_root / relative_path
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_text(content, encoding="utf-8")
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        scheduler.register_task_artifacts(
            task_id,
            [
                {
                    "relative_path": relative_path,
                    "kind": "outbox",
                    "sha256": digest,
                }
            ],
        )
        return relative_path, digest

    def test_store_boundary_converts_reference_and_broker_keeps_hash(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            store = MemoryStore(root / "memory.sqlite3", projection_dir=False)
            try:
                scheduler = _scheduler(store)
                task_id = scheduler.submit_batch("B-TASK-PUBLISH", [_report()])[0]
                attempt = scheduler.start_task_attempt(task_id)
                relative_path, digest = self._attach_artifact(
                    scheduler, root / "task-archive", task_id
                )
                scheduler.ingest_progress(_final_progress(task_id, attempt))

                control_reference = scheduler.state["tasks"][task_id][
                    "artifact_references"
                ][0]
                self.assertEqual(control_reference["relative_path"], relative_path)
                self.assertNotIn("path", control_reference)

                canonical_reference = store.get(task_id)["artifact_references"][0]
                self.assertEqual(canonical_reference["path"], relative_path)
                self.assertEqual(canonical_reference["sha256"], digest)
                self.assertNotIn("relative_path", canonical_reference)

                backend = _RuntimeTaskBackend(scheduler, root / "task-archive")
                summary = backend.get_task_summary(task_id)
                assert summary is not None
                artifact_id = summary["artifacts"][0]["artifact_id"]
                fetched = backend.get_task_artifact(task_id, artifact_id)
                assert fetched is not None
                self.assertEqual(fetched["sha256"], digest)
                self.assertEqual(fetched["content"], "Durable mathematical progress.\n")
            finally:
                store.close()

    def test_publication_rejection_keeps_task_open_until_recovery_commits(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            database = root / "memory.sqlite3"
            store = _RejectFirstTaskPublicationStore(database, projection_dir=False)
            try:
                scheduler = _scheduler(store)
                task_id = scheduler.submit_batch("B-TASK-REPAIR", [_report()])[0]
                attempt = scheduler.start_task_attempt(task_id)
                relative_path, digest = self._attach_artifact(
                    scheduler, root / "task-archive", task_id
                )

                scheduler.ingest_progress(_final_progress(task_id, attempt))
                with self.assertRaises(NotFoundError):
                    store.get(task_id)
                pending = scheduler.state["tasks"][task_id]
                self.assertEqual(pending["state"], TaskState.POSTPROCESSING.value)
                self.assertIsNotNone(pending["close_intent"])
                self.assertFalse(pending["after_close_applied"])
                self.assertFalse(
                    any(
                        event["type"] == "task_closed"
                        and event["payload"].get("task_id") == task_id
                        for event in scheduler.state["events"]
                    )
                )
                attention = next(
                    item
                    for item in scheduler.state["needs_attention"]
                    if item["attention_id"] == f"task-publication:{task_id}"
                    and item["resolved_at"] is None
                )
                self.assertEqual(attention["scope"], "task")
                self.assertEqual(attention["owner_id"], task_id)
                self.assertFalse(scheduler.has_blocking_attention())

                restarted = Scheduler(store)
                restarted.recover()
                restarted.reconcile_pending_ingestion()

                repaired = store.operation_status(f"task-close-repair:{task_id}")
                assert repaired is not None
                self.assertEqual(repaired.status, "committed")
                self.assertEqual(
                    restarted.state["tasks"][task_id]["state"],
                    TaskState.CLOSED.value,
                )
                self.assertTrue(
                    restarted.state["tasks"][task_id]["after_close_applied"]
                )
                canonical_reference = store.get(task_id)["artifact_references"][0]
                self.assertEqual(canonical_reference["path"], relative_path)
                self.assertEqual(canonical_reference["sha256"], digest)
                self.assertFalse(
                    any(
                        item["attention_id"] == f"task-publication:{task_id}"
                        and item["resolved_at"] is None
                        for item in restarted.state["needs_attention"]
                    )
                )

                backend = _RuntimeTaskBackend(restarted, root / "task-archive")
                summary = backend.get_task_summary(task_id)
                assert summary is not None
                fetched = backend.get_task_artifact(
                    task_id, summary["artifacts"][0]["artifact_id"]
                )
                assert fetched is not None
                self.assertEqual(fetched["sha256"], digest)

                # Reconciliation remains idempotent after the repair commit.
                restarted.reconcile_pending_ingestion()
                self.assertEqual(
                    store.operation_status(f"task-close-repair:{task_id}").status,
                    "committed",
                )
            finally:
                store.close()

    def test_recovery_finishes_control_close_after_publication_crash(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            store = MemoryStore(root / "memory.sqlite3", projection_dir=False)
            try:
                scheduler = _scheduler(store, _CrashAfterTaskPublicationScheduler)
                task_id = scheduler.submit_batch("B-CRASH-AFTER-PUBLISH", [_report()])[0]
                attempt = scheduler.start_task_attempt(task_id)
                self._attach_artifact(scheduler, root / "task-archive", task_id)

                with self.assertRaisesRegex(RuntimeError, "after task publication"):
                    scheduler.ingest_progress(_final_progress(task_id, attempt))

                self.assertIsNotNone(store.get(task_id))
                interrupted = scheduler.state["tasks"][task_id]
                self.assertEqual(
                    interrupted["state"], TaskState.POSTPROCESSING.value
                )
                self.assertIsNotNone(interrupted["close_intent"])

                restarted = Scheduler(store)
                restarted.recover()
                restarted.reconcile_pending_ingestion()
                closed = restarted.state["tasks"][task_id]
                self.assertEqual(closed["state"], TaskState.CLOSED.value)
                self.assertTrue(closed["after_close_applied"])
                self.assertEqual(
                    sum(
                        event["type"] == "task_closed"
                        and event["payload"].get("task_id") == task_id
                        for event in restarted.state["events"]
                    ),
                    1,
                )
            finally:
                store.close()

    def test_legacy_missing_publication_reopens_until_repair_commits(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            store = _LegacyMissingTaskPublicationStore(
                root / "memory.sqlite3", projection_dir=False
            )
            try:
                scheduler = _scheduler(store)
                task_id = scheduler.submit_batch("B-LEGACY-MISSING-TASK", [_report()])[0]
                attempt = scheduler.start_task_attempt(task_id)
                scheduler.ingest_progress(_final_progress(task_id, attempt))

                with self.assertRaises(NotFoundError):
                    store.get(task_id)
                self.assertEqual(
                    scheduler.state["tasks"][task_id]["state"],
                    TaskState.CLOSED.value,
                )

                # Remove fields and events unavailable to a pre-two-phase
                # snapshot while preserving its historical task_closed event.
                loaded = store.load_control_state(Scheduler.CONTROL_KEY)
                assert loaded is not None
                revision, legacy = loaded
                legacy_task = legacy["tasks"][task_id]
                legacy_task.pop("close_intent", None)
                legacy_task.pop("after_close_applied", None)
                legacy_task.pop("after_close_applied_at", None)
                legacy["events"] = [
                    event
                    for event in legacy["events"]
                    if event["type"]
                    not in {"task_close_intent_persisted", "task_close_tail_applied"}
                ]
                store.compare_and_swap_control_state(
                    Scheduler.CONTROL_KEY, revision, legacy
                )
                store.emulate_legacy_close = False

                restarted = Scheduler(store)
                restarted.recover()
                restarted.reconcile_pending_ingestion()
                pending = restarted.state
                self.assertEqual(
                    pending["tasks"][task_id]["state"],
                    TaskState.POSTPROCESSING.value,
                )
                self.assertTrue(
                    pending["tasks"][task_id]["close_intent"][
                        "recovered_from_legacy_close"
                    ]
                )
                self.assertFalse(
                    pending["tasks"][task_id].get("after_close_applied", False)
                )
                with self.assertRaises(NotFoundError):
                    store.get(task_id)
                self.assertEqual(
                    sum(
                        event["type"] == "task_closed"
                        and event["payload"].get("task_id") == task_id
                        for event in pending["events"]
                    ),
                    1,
                )
                self.assertTrue(
                    any(
                        item["attention_id"] == f"task-publication:{task_id}"
                        and item["resolved_at"] is None
                        for item in pending["needs_attention"]
                    )
                )

                store.block_repair = False
                restarted.reconcile_pending_ingestion()
                repaired = restarted.state
                self.assertIsNotNone(store.get(task_id))
                self.assertEqual(
                    repaired["tasks"][task_id]["state"], TaskState.CLOSED.value
                )
                self.assertTrue(repaired["tasks"][task_id]["after_close_applied"])
                self.assertEqual(
                    sum(
                        event["type"] == "task_closed"
                        and event["payload"].get("task_id") == task_id
                        for event in repaired["events"]
                    ),
                    1,
                )
                self.assertEqual(
                    sum(
                        event["type"] == "legacy_task_close_migrated_for_publication"
                        and event["payload"].get("task_id") == task_id
                        for event in repaired["events"]
                    ),
                    1,
                )
                self.assertEqual(
                    sum(
                        event["type"] == "task_close_tail_applied"
                        and event["payload"].get("task_id") == task_id
                        for event in repaired["events"]
                    ),
                    1,
                )
                self.assertFalse(
                    any(
                        item["attention_id"] == f"task-publication:{task_id}"
                        and item["resolved_at"] is None
                        for item in repaired["needs_attention"]
                    )
                )

                restarted.reconcile_pending_ingestion()
                replayed = restarted.state
                self.assertEqual(
                    sum(
                        event["type"] == "legacy_task_close_migrated_for_publication"
                        for event in replayed["events"]
                    ),
                    1,
                )
                self.assertEqual(
                    sum(
                        event["type"] == "task_close_tail_applied"
                        and event["payload"].get("task_id") == task_id
                        for event in replayed["events"]
                    ),
                    1,
                )
            finally:
                store.close()

    def test_recovery_replays_sprint_tail_without_duplicate_effects(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            store = MemoryStore(root / "memory.sqlite3", projection_dir=False)
            try:
                scheduler = _scheduler(store, _CrashBeforeTailMarkerScheduler)
                root_obligation_id = scheduler.state["root"]["obligation_id"]
                distant_ids: list[str] = []
                for index in (1, 2):
                    result = store.add_memo(
                        f"sprint-tail-distant-{index}",
                        {
                            "abstract": f"Distant sprint-tail mechanism {index}",
                            "genre": "normal",
                            "content": f"Remote bridge prompt {index}.",
                            "related_route_ids": [],
                        },
                    )
                    assert result.canonical_id is not None
                    distant_ids.append(result.canonical_id)
                scheduler.submit_stuck_report("B-STUCK-TAIL", {"summary": "stuck"})
                scheduler.apply_trim_review_decision("trim", "test durable sprint tail")
                empty_portfolio = {
                    kind: []
                    for kind in (
                        "fact",
                        "route",
                        "memo",
                        "claim",
                        "obligation",
                        "computation",
                    )
                }
                lanes = [
                    {
                        "mode": "brainstorm",
                        "objective": "Find a clean-room proof.",
                        "main_obligation_ids": [root_obligation_id],
                        "assignment_portfolio": empty_portfolio,
                        "reason": "Supply only the target.",
                    },
                    {
                        "mode": "multi-discipline",
                        "objective": "Translate the target.",
                        "main_obligation_ids": [root_obligation_id],
                        "selected_new_perspective": "categorical",
                        "assignment_portfolio": empty_portfolio,
                        "reason": "Supply the target and one new perspective.",
                    },
                    {
                        "mode": "computation",
                        "objective": "Test boundary cases.",
                        "main_obligation_ids": [root_obligation_id],
                        "computation_portfolio": ["Enumerate the smallest cases."],
                        "assignment_portfolio": empty_portfolio,
                        "reason": "Supply one explicit experiment.",
                    },
                    {
                        "mode": "associate",
                        "objective": "Seek a remote bridge.",
                        "main_obligation_ids": [root_obligation_id],
                        "assignment_portfolio": {
                            **empty_portfolio,
                            "memo": distant_ids,
                        },
                        "reason": "Supply exactly two distant memos.",
                    },
                ]
                scheduler.persist_sprint_plan(
                    "S-TAIL",
                    {
                        "target_obligation": {
                            "id": root_obligation_id,
                            "revision": 1,
                            "statement": "ROOT holds.",
                        },
                        "lanes": lanes,
                    },
                )
                reports: list[dict[str, object]] = []
                for index, lane in enumerate(lanes):
                    report = _report()
                    report.update(
                        {
                            "report_id": f"AR-SPRINT-TAIL-{index}",
                            "mode": lane["mode"],
                            "objective": lane["objective"],
                            "main_obligation_ids": [root_obligation_id],
                            "perspective": lane.get("selected_new_perspective"),
                            "computation_portfolio": lane.get(
                                "computation_portfolio"
                            ),
                            "portfolio": lane["assignment_portfolio"],
                            "reason": lane["reason"],
                        }
                    )
                    reports.append(report)
                task_ids = scheduler.launch_sprint(
                    "S-TAIL", reports, batch_id="B-SPRINT-TAIL"
                )
                for task_id in task_ids[:-1]:
                    attempt = scheduler.start_task_attempt(task_id)
                    scheduler.ingest_progress(_final_progress(task_id, attempt))

                final_task_id = task_ids[-1]
                scheduler.crash_task_id = final_task_id
                attempt = scheduler.start_task_attempt(final_task_id)
                with self.assertRaisesRegex(RuntimeError, "downstream close effects"):
                    scheduler.ingest_progress(
                        _final_progress(final_task_id, attempt)
                    )

                self.assertEqual(
                    scheduler.state["sprints"]["S-TAIL"]["status"],
                    "awaiting_summary",
                )
                self.assertFalse(
                    scheduler.state["tasks"][final_task_id]["after_close_applied"]
                )
                self.assertEqual(
                    sum(
                        event["type"] == "sprint_barrier_opened"
                        for event in scheduler.state["events"]
                    ),
                    1,
                )

                restarted = Scheduler(store)
                restarted.recover()
                restarted.reconcile_pending_ingestion()
                restarted.reconcile_pending_ingestion()
                state = restarted.state
                self.assertTrue(
                    state["tasks"][final_task_id]["after_close_applied"]
                )
                self.assertEqual(
                    sum(
                        event["type"] == "sprint_barrier_opened"
                        for event in state["events"]
                    ),
                    1,
                )
                self.assertEqual(
                    sum(
                        event["type"] == "task_close_tail_applied"
                        and event["payload"].get("task_id") == final_task_id
                        for event in state["events"]
                    ),
                    1,
                )
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
