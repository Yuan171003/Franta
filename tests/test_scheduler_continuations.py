from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any, Mapping

from franta.config import load_manifest
from franta.runtime import BOOTSTRAP_STATE_KEY, AgentCall, FrantaRuntime
from franta.scheduler import Scheduler
from franta.store import MemoryStore
from franta.testing import FakeControlStore
from franta.workflows import (
    CallState,
    GateState,
    OperationState,
    TaskState,
    WorkflowError,
)


MEMORY_TYPES = ("fact", "route", "memo", "claim", "obligation", "computation")


def _portfolio() -> dict[str, list[str]]:
    return {memory_type: [] for memory_type in MEMORY_TYPES}


def _assignment(index: int = 1) -> dict[str, Any]:
    return {
        "report_id": f"AR-CONTINUATION-{index}",
        "objective": "Check the durable continuation protocol.",
        "if_resume": None,
        "mode": "associate",
        "main_route_ids": [],
        "main_obligation_ids": [],
        "perspective": None,
        "portfolio": _portfolio(),
        "reason": "Exercise an approved recovery boundary.",
    }


def _progress(
    task_id: str,
    attempt: int,
    *,
    progress_id: str,
    operations: list[dict[str, Any]],
    evidence: list[str],
    outcome: str = "progress",
) -> dict[str, Any]:
    return {
        "progress_id": progress_id,
        "task_id": task_id,
        "attempt": attempt,
        "sequence": 1,
        "is_final": True,
        "outcome_status": outcome,
        "attempt_summary": {
            "summary": "The exact continuation result is durable.",
            "proposed_outcome": outcome,
            "completion_evidence_operation_ids": list(evidence),
        },
        "operations": operations,
        "completion_evidence_ids": list(evidence),
    }


def _fact_candidate(operation_id: str) -> dict[str, Any]:
    return {
        "operation_id": operation_id,
        "kind": "fact",
        "proposal_id": f"TMP-{operation_id}",
        "candidate_id": f"FC-{operation_id}",
        "candidate_version": 1,
        "statement": "Every test object has property P.",
        "proof": "The foundation policy's defining axiom proves property P directly.",
        "predecessor_fact_ids": [],
        "abstract": "The defining axiom gives property P.",
        "keywords": ["test object", "property P"],
        "introduced_notation": [],
        "external_references": [],
        "related_route_ids": [],
    }


def _exact_verifier_report(bundle: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "verdict": "correct",
        "candidate_id": bundle["candidate_id"],
        "candidate_version": bundle["candidate_version"],
        "operation_id": bundle["operation_id"],
        "bundle_digest": bundle["bundle_digest"],
        "verifier_attempt_id": bundle["verifier_attempt_id"],
        "predecessor_ids": bundle["predecessor_ids"],
        "introduced_notation": bundle["introduced_notation"],
        "external_references": bundle["external_references"],
        "root_resolution": bundle["root_resolution"],
        "errors": [],
    }


class _CrashAfterSemanticTail(BaseException):
    pass


class _CrashBeforeCallCommitScheduler(Scheduler):
    crash_kind: str | None = None

    def mark_call_committed(self, call_id: str) -> None:
        call = self.state["calls"][call_id]
        if self.crash_kind == call["kind"]:
            self.crash_kind = None
            raise _CrashAfterSemanticTail("simulated stop before call commit")
        super().mark_call_committed(call_id)


class _CrashBeforeChallengeTailScheduler(Scheduler):
    crash_before_challenge_tail = False

    def _apply_fact_challenge_resolution_tail(self, challenge_id: str) -> None:
        if self.crash_before_challenge_tail:
            self.crash_before_challenge_tail = False
            raise _CrashAfterSemanticTail(
                "simulated stop after challenge verdict but before revocation"
            )
        super()._apply_fact_challenge_resolution_tail(challenge_id)


class _FactPublicationCrashStore(MemoryStore):
    crash_mode: str | None = None
    crash_operation_id: str | None = None

    def apply_operation(
        self,
        operation_id: str,
        operation_type: Any,
        payload: Mapping[str, Any],
        *,
        proposal_id: str | None = None,
        actor: str = "scheduler",
    ):
        should_crash = (
            str(operation_type) in {"fact", "OperationType.FACT"}
            and operation_id == self.crash_operation_id
            and self.crash_mode is not None
        )
        if should_crash and self.crash_mode == "before":
            self.crash_mode = None
            raise RuntimeError("simulated outage before canonical fact commit")
        result = super().apply_operation(
            operation_id,
            operation_type,
            payload,
            proposal_id=proposal_id,
            actor=actor,
        )
        if should_crash and self.crash_mode == "after":
            self.crash_mode = None
            raise RuntimeError("simulated disconnect after canonical fact commit")
        return result


class _UpdatePublicationCrashStore(MemoryStore):
    crash_operation_id: str | None = None

    def apply_operation(
        self,
        operation_id: str,
        operation_type: Any,
        payload: Mapping[str, Any],
        *,
        proposal_id: str | None = None,
        actor: str = "scheduler",
    ):
        result = super().apply_operation(
            operation_id,
            operation_type,
            payload,
            proposal_id=proposal_id,
            actor=actor,
        )
        if operation_id == self.crash_operation_id and str(operation_type) in {
            "route_update",
            "OperationType.ROUTE_UPDATE",
        }:
            self.crash_operation_id = None
            raise _CrashAfterSemanticTail(
                "simulated stop after atomic update before scheduler tail"
            )
        return result


class _CrashBootstrapStateRuntime(FrantaRuntime):
    crash_next_bootstrap_save = False

    def _save_bootstrap_state(self, revision: int, state: Mapping[str, Any]) -> None:
        if self.crash_next_bootstrap_save:
            self.crash_next_bootstrap_save = False
            raise RuntimeError("simulated stop before bootstrap owner state commit")
        super()._save_bootstrap_state(revision, state)


def _manifest(root: Path) -> Path:
    path = root / "bootstrap.toml"
    path.write_text(
        "\n".join(
            [
                "[project]",
                'name = "continuation-bootstrap"',
                'directory = "project"',
                'root_problem = "Prove that every test object has property P."',
                'foundation_policy = "Use only the declared test-object axiom."',
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


class SchedulerContinuationTests(unittest.TestCase):
    def _fake_scheduler(
        self, scheduler_type: type[Scheduler] = Scheduler
    ) -> tuple[FakeControlStore, Scheduler]:
        store = FakeControlStore()
        scheduler = scheduler_type(store)
        scheduler.bootstrap(root_problem="Prove the test property.")
        scheduler.commit_initial_trim({"category_ids": []})
        return store, scheduler

    @staticmethod
    def _exhaust_transport_retries(scheduler: Scheduler, call_id: str) -> None:
        while True:
            lease_epoch, _ = scheduler.mark_call_running(call_id)
            if not scheduler.mark_call_failed(
                call_id, lease_epoch, "simulated repeated disconnection"
            ):
                return

    def test_restart_reconciles_and_deduplicates_legacy_orphan_synthesizer_calls(
        self,
    ) -> None:
        store, scheduler = self._fake_scheduler()
        task_id = scheduler.submit_batch("B-CONT-ORPHAN", [_assignment(1)])[0]
        attempt = scheduler.start_task_attempt(task_id)
        operation_id = "OP-CONT-ROUTE"
        scheduler.ingest_progress(
            _progress(
                task_id,
                attempt,
                progress_id="PRG-CONT-ORPHAN",
                operations=[
                    {
                        "operation_id": operation_id,
                        "kind": "route_add",
                        "proposal_id": "TMP-CONT-ROUTE",
                        "abstract": "A durable route proposal.",
                    }
                ],
                evidence=[],
            )
        )
        exact_input = scheduler.synthesizer_input(operation_id)
        first = scheduler.prepare_call(
            "synthesizer",
            exact_input,
            continuation={"operation_id": operation_id},
        )
        second = scheduler.prepare_call(
            "synthesizer",
            exact_input,
            continuation={"operation_id": operation_id},
        )
        self.assertIsNone(
            scheduler.state["operations"][operation_id].get("synthesizer_call_id")
        )

        resumed = Scheduler(store)
        resumed.recover()
        resumed.reconcile_pending_ingestion()
        state = resumed.state
        retained = min(first, second)
        discarded = max(first, second)
        self.assertEqual(
            state["operations"][operation_id]["synthesizer_call_id"], retained
        )
        self.assertEqual(state["calls"][discarded]["status"], CallState.SUPERSEDED.value)
        self.assertEqual(
            sum(
                call["status"] != CallState.SUPERSEDED.value
                for call in state["calls"].values()
                if call["kind"] == "synthesizer"
                and call["continuation"] == {"operation_id": operation_id}
            ),
            1,
        )

    def test_recovery_opens_exact_attempt_verifier_gate_for_nonfinal_fact(
        self,
    ) -> None:
        store, scheduler = self._fake_scheduler()
        task_id = scheduler.submit_batch(
            "B-CONT-NONFINAL-GATE", [_assignment(11)]
        )[0]
        first_attempt = scheduler.start_task_attempt(task_id)
        operation_id = "OP-CONT-NONFINAL-GATE"
        scheduler.ingest_progress(
            {
                "progress_id": "PRG-CONT-NONFINAL-GATE",
                "task_id": task_id,
                "attempt": first_attempt,
                "sequence": 1,
                "is_final": False,
                "operations": [_fact_candidate(operation_id)],
                "completion_evidence_ids": [],
            }
        )
        synthesizer_call = scheduler.prepare_synthesizer_call(operation_id)
        epoch, _ = scheduler.mark_call_running(synthesizer_call)
        operation_digest = scheduler.state["operations"][operation_id][
            "input_digest"
        ]
        scheduler.accept_call_result(
            synthesizer_call,
            epoch,
            {
                "resolution": "new",
                "operation_digest": operation_digest,
                "relied_on": [],
            },
        )
        scheduler.commit_synthesizer_call(synthesizer_call)

        live = scheduler.state
        self.assertEqual(
            live["operations"][operation_id]["state"],
            OperationState.VERIFYING.value,
        )
        self.assertEqual(
            live["tasks"][task_id]["state"], TaskState.RUNNING.value
        )
        self.assertFalse(scheduler.source_attempt_terminal(operation_id))
        self.assertFalse(scheduler.verifier_ready(operation_id))
        with self.assertRaisesRegex(WorkflowError, "before its worker attempt ends"):
            scheduler.prepare_verifier_call(operation_id)
        self.assertFalse(
            any(call["kind"] == "verifier" for call in scheduler.state["calls"].values())
        )

        resumed = Scheduler(store)
        recovery = resumed.recover()
        recovered = resumed.state
        self.assertIn(task_id, recovery.relaunch_task_ids)
        self.assertEqual(
            recovered["tasks"][task_id]["state"], TaskState.RETRY_PENDING.value
        )
        self.assertEqual(
            recovered["tasks"][task_id]["attempts"][0]["state"], "interrupted"
        )
        self.assertEqual(
            recovered["operations"][operation_id]["state"],
            OperationState.VERIFYING.value,
        )
        self.assertTrue(resumed.source_attempt_terminal(operation_id))
        self.assertTrue(resumed.verifier_ready(operation_id))

        second_attempt = resumed.start_task_attempt(task_id)
        self.assertEqual(second_attempt, first_attempt + 1)
        retrying = resumed.state
        self.assertEqual(
            retrying["tasks"][task_id]["state"], TaskState.RUNNING.value
        )
        self.assertEqual(
            retrying["operations"][operation_id]["attempt"], first_attempt
        )
        self.assertTrue(resumed.verifier_ready(operation_id))
        verifier_call = resumed.prepare_verifier_call(operation_id)
        resumed.mark_call_running(verifier_call)
        self.assertEqual(
            resumed.state["calls"][verifier_call]["status"],
            CallState.RUNNING.value,
        )

    def test_closure_result_tail_replays_after_stop_before_call_commit(self) -> None:
        store, scheduler = self._fake_scheduler(_CrashBeforeCallCommitScheduler)
        task_id = scheduler.submit_batch("B-CONT-CLOSURE", [_assignment(2)])[0]
        attempt = scheduler.start_task_attempt(task_id)
        operation_id = "OP-CONT-ABANDON"
        scheduler.ingest_progress(
            _progress(
                task_id,
                attempt,
                progress_id="PRG-CONT-CLOSURE",
                operations=[
                    {
                        "operation_id": operation_id,
                        "kind": "route_add",
                        "proposal_id": "TMP-CONT-ABANDON",
                        "abstract": "An ambiguous route proposal.",
                    }
                ],
                evidence=[operation_id],
            )
        )
        scheduler.abandon_operation(
            operation_id,
            authorized_by="operator",
            reason="The nonessential route is redirected.",
        )
        call_id = scheduler.prepare_main_closure_review_call(task_id)
        epoch, _ = scheduler.mark_call_running(call_id)
        result = {
            "outcome": "progress",
            "summary": {"summary": "The task retains useful partial progress."},
        }
        scheduler.accept_call_result(call_id, epoch, result)
        scheduler.crash_kind = "main-closure-review"
        with self.assertRaises(_CrashAfterSemanticTail):
            scheduler.commit_main_closure_review_call(call_id)
        self.assertEqual(scheduler.state["tasks"][task_id]["state"], TaskState.CLOSED.value)
        self.assertEqual(scheduler.state["calls"][call_id]["status"], CallState.COMPLETED.value)

        resumed = Scheduler(store)
        resumed.recover()
        resumed.commit_main_closure_review_call(call_id)
        state = resumed.state
        self.assertEqual(state["calls"][call_id]["status"], CallState.COMMITTED.value)
        self.assertEqual(state["tasks"][task_id]["final_status"], "progress")
        self.assertEqual(
            sum(event["type"] == "closure_review_result_applied" for event in state["events"]),
            1,
        )

    def test_sprint_summary_tail_replays_after_stop_before_call_commit(self) -> None:
        store, scheduler = self._fake_scheduler(_CrashBeforeCallCommitScheduler)
        sprint_id = "S-CONT-SUMMARY"
        frozen = {"plan": {"target": "O-ROOT"}, "lane_results": []}
        with scheduler._mutate() as state:
            state["sprints"][sprint_id] = {
                "sprint_id": sprint_id,
                "status": "awaiting_summary",
                "frozen_synthesis_input": frozen,
                "frozen_synthesis_digest": "fixture",
                "summary": None,
            }
        call_id = scheduler.prepare_sprint_summarizer_call(sprint_id)
        epoch, _ = scheduler.mark_call_running(call_id)
        result = {
            "mechanism_fingerprints": [],
            "differences": [],
            "shared_bottlenecks": [],
            "bridges": [],
            "negative_results": [],
            "follow_up_tasks": [],
        }
        scheduler.accept_call_result(call_id, epoch, result)
        scheduler.crash_kind = "summarizer"
        with self.assertRaises(_CrashAfterSemanticTail):
            scheduler.commit_sprint_summarizer_call(call_id)
        self.assertEqual(
            scheduler.state["sprints"][sprint_id]["status"], "trimmer_continuation"
        )
        self.assertEqual(scheduler.state["calls"][call_id]["status"], CallState.COMPLETED.value)

        resumed = Scheduler(store)
        resumed.recover()
        resumed.commit_sprint_summarizer_call(call_id)
        state = resumed.state
        self.assertEqual(state["calls"][call_id]["status"], CallState.COMMITTED.value)
        self.assertEqual(state["sprints"][sprint_id]["summary"], result)
        self.assertEqual(
            sum(event["type"] == "sprint_summary_accepted" for event in state["events"]),
            1,
        )

    def test_verified_fact_publication_replays_without_a_second_verifier(self) -> None:
        for crash_mode in ("before", "after"):
            with self.subTest(crash_mode=crash_mode), tempfile.TemporaryDirectory() as raw:
                database = Path(raw) / "memory.sqlite3"
                store = _FactPublicationCrashStore(database, projection_dir=False)
                try:
                    scheduler = Scheduler(store)
                    root_id = scheduler.bootstrap(root_problem="Prove the test property.")
                    root = store.add_obligation(
                        "bootstrap-continuation-root",
                        {
                            "id": root_id,
                            "abstract": "The root test property.",
                            "statement": "Every test object has property P.",
                            "importance": "This is the project target.",
                            "predecessor_fact_ids": [],
                            "partial_progress": [],
                            "related_route_ids": [],
                            "relations": [],
                        },
                    )
                    self.assertEqual(root.status, "committed")
                    scheduler.bind_canonical_root_obligation()
                    scheduler.commit_initial_trim({"category_ids": []})
                    task_id = scheduler.submit_batch(
                        f"B-CONT-FACT-{crash_mode}", [_assignment(3)]
                    )[0]
                    attempt = scheduler.start_task_attempt(task_id)
                    operation_id = f"OP-CONT-FACT-{crash_mode.upper()}"
                    scheduler.ingest_progress(
                        _progress(
                            task_id,
                            attempt,
                            progress_id=f"PRG-CONT-FACT-{crash_mode.upper()}",
                            operations=[_fact_candidate(operation_id)],
                            evidence=[operation_id],
                            outcome="finished",
                        )
                    )
                    digest = scheduler.state["operations"][operation_id]["input_digest"]
                    scheduler.apply_synthesizer_result(
                        operation_id,
                        {
                            "resolution": "new",
                            "operation_digest": digest,
                            "relied_on": [],
                        },
                    )
                    call_id = scheduler.prepare_verifier_call(operation_id)
                    epoch, _ = scheduler.mark_call_running(call_id)
                    bundle = scheduler.state["calls"][call_id]["input"]
                    scheduler.accept_call_result(
                        call_id, epoch, _exact_verifier_report(bundle)
                    )
                    store.crash_operation_id = operation_id
                    store.crash_mode = crash_mode
                    self.assertIsNone(scheduler.commit_verifier_call(call_id))
                    failed = scheduler.state
                    self.assertEqual(
                        failed["operations"][operation_id]["state"],
                        OperationState.NEEDS_ATTENTION.value,
                    )
                    self.assertTrue(failed["operations"][operation_id]["verified_correct"])
                    self.assertEqual(
                        failed["calls"][call_id]["status"], CallState.COMMITTED.value
                    )
                finally:
                    store.close()

                resumed_store = _FactPublicationCrashStore(
                    database, projection_dir=False
                )
                try:
                    resumed = Scheduler(resumed_store)
                    resumed.recover()
                    resumed.reconcile_pending_ingestion()
                    state = resumed.state
                    operation = state["operations"][operation_id]
                    self.assertEqual(operation["state"], OperationState.COMMITTED.value)
                    self.assertIsNotNone(operation["canonical_id"])
                    self.assertEqual(
                        state["calls"][call_id]["status"], CallState.COMMITTED.value
                    )
                    self.assertEqual(
                        sum(
                            call["kind"] == "verifier"
                            and call["continuation"] == {"operation_id": operation_id}
                            for call in state["calls"].values()
                        ),
                        1,
                    )
                    self.assertEqual(
                        state["tasks"][task_id]["state"], TaskState.CLOSED.value
                    )
                    self.assertEqual(
                        resumed_store.get(str(operation["canonical_id"]))["statement"],
                        "Every test object has property P.",
                    )
                    unresolved = [
                        item
                        for item in state["needs_attention"]
                        if item["attention_id"] == f"operation:{operation_id}"
                        and item.get("resolved_at") is None
                    ]
                    self.assertEqual(unresolved, [])
                finally:
                    resumed_store.close()

    def test_runtime_recovery_commits_completed_verifier_tail_without_model_call(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            runtime = FrantaRuntime.initialize(
                load_manifest(_manifest(Path(raw))),
                executor=lambda call: (_ for _ in ()).throw(
                    AssertionError(f"unexpected setup model call {call.kind}")
                ),
            )
            try:
                scheduler = runtime.scheduler
                scheduler.commit_initial_trim({"category_ids": []})
                task_id = scheduler.submit_batch(
                    "B-CONT-COMPLETED-VERIFIER", [_assignment(12)]
                )[0]
                attempt = scheduler.start_task_attempt(task_id)
                operation_id = "OP-CONT-COMPLETED-VERIFIER"
                scheduler.ingest_progress(
                    _progress(
                        task_id,
                        attempt,
                        progress_id="PRG-CONT-COMPLETED-VERIFIER",
                        operations=[_fact_candidate(operation_id)],
                        evidence=[operation_id],
                        outcome="finished",
                    )
                )
                operation_digest = scheduler.state["operations"][operation_id][
                    "input_digest"
                ]
                scheduler.apply_synthesizer_result(
                    operation_id,
                    {
                        "resolution": "new",
                        "operation_digest": operation_digest,
                        "relied_on": [],
                    },
                )
                verifier_call = scheduler.prepare_verifier_call(operation_id)
                epoch, _ = scheduler.mark_call_running(verifier_call)
                bundle = scheduler.state["calls"][verifier_call]["input"]
                scheduler.accept_call_result(
                    verifier_call, epoch, _exact_verifier_report(bundle)
                )

                original_mark_committed = scheduler.mark_call_committed
                crash_pending = True

                def crash_before_verifier_call_commit(call_id: str) -> None:
                    nonlocal crash_pending
                    call = scheduler.state["calls"][call_id]
                    if crash_pending and call["kind"] == "verifier":
                        crash_pending = False
                        raise _CrashAfterSemanticTail(
                            "simulated stop after Fact publication before verifier call commit"
                        )
                    original_mark_committed(call_id)

                scheduler.mark_call_committed = (  # type: ignore[method-assign]
                    crash_before_verifier_call_commit
                )
                with self.assertRaisesRegex(
                    _CrashAfterSemanticTail, "after Fact publication"
                ):
                    scheduler.commit_verifier_call(verifier_call)

                stopped = scheduler.state
                canonical_id = stopped["operations"][operation_id]["canonical_id"]
                self.assertIsNotNone(canonical_id)
                fact_id = str(canonical_id)
                self.assertEqual(
                    stopped["operations"][operation_id]["state"],
                    OperationState.COMMITTED.value,
                )
                self.assertEqual(
                    stopped["calls"][verifier_call]["status"],
                    CallState.COMPLETED.value,
                )
                project_dir = runtime.layout.root
            finally:
                runtime.close()

            model_calls: list[str] = []

            def reject_second_model_call(call: AgentCall) -> dict[str, Any]:
                model_calls.append(call.kind)
                raise AssertionError(f"recovery relaunched {call.kind}")

            resumed = FrantaRuntime.open(
                project_dir, executor=reject_second_model_call
            )
            try:
                resumed.start_services()
                resumed.recover()
                before_tail = resumed.scheduler.state
                self.assertEqual(
                    before_tail["calls"][verifier_call]["status"],
                    CallState.COMPLETED.value,
                )
                self.assertEqual(
                    before_tail["operations"][operation_id]["canonical_id"],
                    fact_id,
                )

                self.assertTrue(resumed._commit_completed_calls())
                recovered = resumed.scheduler.state
                self.assertEqual(
                    recovered["calls"][verifier_call]["status"],
                    CallState.COMMITTED.value,
                )
                self.assertEqual(
                    recovered["operations"][operation_id]["state"],
                    OperationState.COMMITTED.value,
                )
                self.assertEqual(
                    recovered["operations"][operation_id]["canonical_id"],
                    fact_id,
                )
                self.assertEqual(
                    sum(
                        call["kind"] == "verifier"
                        and call["continuation"] == {"operation_id": operation_id}
                        for call in recovered["calls"].values()
                    ),
                    1,
                )
                self.assertEqual(
                    len(resumed.store.list_records(types=["fact"])), 1
                )
                self.assertEqual(model_calls, [])
            finally:
                resumed.close()

    def test_verified_fact_store_rejection_is_task_local_and_terminal(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            store = MemoryStore(Path(raw) / "memory.sqlite3", projection_dir=False)
            try:
                scheduler = Scheduler(store)
                root_id = scheduler.bootstrap(root_problem="Prove the test property.")
                root = store.add_obligation(
                    "bootstrap-fact-rejection-root",
                    {
                        "id": root_id,
                        "abstract": "The root test property.",
                        "statement": "Every test object has property P.",
                        "importance": "This is the project target.",
                        "predecessor_fact_ids": [],
                        "partial_progress": [],
                        "related_route_ids": [],
                        "relations": [],
                    },
                )
                self.assertEqual(root.status, "committed")
                scheduler.bind_canonical_root_obligation()
                scheduler.commit_initial_trim({"category_ids": []})
                task_id = scheduler.submit_batch(
                    "B-FACT-STORE-REJECTION", [_assignment(4)]
                )[0]
                attempt = scheduler.start_task_attempt(task_id)
                operation_id = "OP-FACT-STORE-REJECTION"
                candidate = _fact_candidate(operation_id)
                candidate["worker_only_debug_field"] = "not canonical Fact data"
                scheduler.ingest_progress(
                    _progress(
                        task_id,
                        attempt,
                        progress_id="PRG-FACT-STORE-REJECTION",
                        operations=[candidate],
                        evidence=[operation_id],
                        outcome="finished",
                    )
                )
                digest = scheduler.state["operations"][operation_id]["input_digest"]
                scheduler.apply_synthesizer_result(
                    operation_id,
                    {
                        "resolution": "new",
                        "operation_digest": digest,
                        "relied_on": [],
                    },
                )
                call_id = scheduler.prepare_verifier_call(operation_id)
                epoch, _ = scheduler.mark_call_running(call_id)
                bundle = scheduler.state["calls"][call_id]["input"]
                scheduler.accept_call_result(
                    call_id, epoch, _exact_verifier_report(bundle)
                )

                self.assertIsNone(scheduler.commit_verifier_call(call_id))
                self.assertIsNone(scheduler.commit_verifier_call(call_id))

                state = scheduler.state
                operation = state["operations"][operation_id]
                proposal_id = str(operation["payload"]["proposal_id"])
                durable_result = store.operation_status(operation_id)
                self.assertIsNotNone(durable_result)
                self.assertEqual(durable_result.status, "rejected")
                self.assertEqual(operation["state"], OperationState.REJECTED.value)
                self.assertIsNone(operation["canonical_id"])
                self.assertIn("unknown fields", str(operation["error"]))
                self.assertEqual(
                    state["calls"][call_id]["status"], CallState.COMMITTED.value
                )
                self.assertEqual(state["fact_proposals"][proposal_id]["state"], "rejected")
                self.assertTrue(
                    state["fact_proposals"][proposal_id]["cascade_applied"]
                )
                self.assertEqual(
                    state["tasks"][task_id]["state"], TaskState.CLOSED.value
                )
                self.assertFalse(
                    any(
                        item["attention_id"] == f"operation:{operation_id}"
                        and item.get("resolved_at") is None
                        for item in state["needs_attention"]
                    )
                )
                self.assertEqual(
                    sum(
                        event["type"] == "verified_fact_publication_rejected"
                        and event["payload"]["operation_id"] == operation_id
                        for event in state["events"]
                    ),
                    1,
                )
            finally:
                store.close()

    def test_confirmed_challenge_revocation_tail_recovers_after_exact_stop(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            database = Path(raw) / "memory.sqlite3"
            store = MemoryStore(database, projection_dir=False)
            try:
                scheduler = _CrashBeforeChallengeTailScheduler(store)
                root_id = scheduler.bootstrap(root_problem="Prove the test property.")
                store.add_obligation(
                    "bootstrap-challenge-root",
                    {
                        "id": root_id,
                        "abstract": "The root test property.",
                        "statement": "Every test object has property P.",
                        "importance": "This is the project target.",
                        "predecessor_fact_ids": [],
                        "partial_progress": [],
                        "related_route_ids": [],
                        "relations": [],
                    },
                )
                scheduler.bind_canonical_root_obligation()
                scheduler.commit_initial_trim({"category_ids": []})
                fact = store.add_fact(
                    "challenge-tail-fact",
                    {
                        "statement": "The challenged assertion holds.",
                        "proof": "The claimed boundary step proves it.",
                        "predecessor_fact_ids": [],
                        "originating_task_id": store.allocate_id("task"),
                        "foundation_policy_version": 1,
                        "introduced_notation": [],
                        "external_references": [],
                        "root_resolution": None,
                        "abstract": "A fact with a challenged boundary step.",
                        "keywords": ["boundary"],
                        "related_route_ids": [],
                    },
                )
                task_id = scheduler.submit_batch(
                    "B-CONT-CHALLENGE", [_assignment(4)]
                )[0]
                attempt = scheduler.start_task_attempt(task_id)
                progress = _progress(
                    task_id,
                    attempt,
                    progress_id="PRG-CONT-CHALLENGE",
                    operations=[],
                    evidence=[],
                    outcome="finished",
                )
                progress["fact_challenges"] = [
                    {
                        "challenge_id": "CH-CONT-REVOKE",
                        "fact_id": fact.canonical_id,
                        "alleged_failure": "The boundary step is false.",
                    }
                ]
                scheduler.ingest_progress(progress)
                call_id = scheduler.prepare_challenge_verifier_call(
                    "CH-CONT-REVOKE"
                )
                lease_epoch, _ = scheduler.mark_call_running(call_id)
                bundle = scheduler.state["calls"][call_id]["input"]
                report = {
                    "challenge_id": "CH-CONT-REVOKE",
                    "bundle_digest": bundle["bundle_digest"],
                    "resolution": "confirmed_invalid",
                    "justification": "The cited boundary implication is reversed.",
                }
                scheduler.accept_call_result(call_id, lease_epoch, report)
                scheduler.crash_before_challenge_tail = True
                with self.assertRaises(_CrashAfterSemanticTail):
                    scheduler.commit_challenge_verifier_call(call_id)
                failed = scheduler.state
                self.assertEqual(
                    failed["challenges"]["CH-CONT-REVOKE"]["state"],
                    OperationState.COMMITTED.value,
                )
                self.assertEqual(
                    failed["challenges"]["CH-CONT-REVOKE"]["resolution_tail"][
                        "status"
                    ],
                    "pending",
                )
                self.assertEqual(failed["calls"][call_id]["status"], "completed")
                self.assertEqual(store.get(fact.canonical_id).status, "active")
                # Reproduce the historical bad replay state as well: the call
                # was committed after the terminal challenge returned early,
                # while the canonical fact was still active.
                scheduler.mark_call_committed(call_id)
                self.assertEqual(
                    scheduler.state["calls"][call_id]["status"], "committed"
                )
                self.assertEqual(store.get(fact.canonical_id).status, "active")
            finally:
                store.close()

            resumed_store = MemoryStore(database, projection_dir=False)
            try:
                resumed = Scheduler(resumed_store)
                resumed.recover()
                resumed.reconcile_pending_ingestion()
                resumed.commit_challenge_verifier_call(call_id)
                resumed.reconcile_pending_ingestion()
                state = resumed.state
                self.assertEqual(resumed_store.get(fact.canonical_id).status, "revoked")
                self.assertEqual(
                    state["challenges"]["CH-CONT-REVOKE"]["resolution_tail"][
                        "status"
                    ],
                    "applied",
                )
                self.assertEqual(state["calls"][call_id]["status"], "committed")
                self.assertEqual(
                    sum(
                        event["type"] == "fact_revocation_applied"
                        for event in state["events"]
                    ),
                    1,
                )
                self.assertEqual(
                    sum(
                        event["type"] == "fact_challenge_resolution_tail_applied"
                        for event in state["events"]
                    ),
                    1,
                )
                self.assertIsNotNone(
                    resumed_store.operation_status(
                        "challenge-revocation:CH-CONT-REVOKE"
                    )
                )
            finally:
                resumed_store.close()

    def test_sprint_post_trim_tail_recovers_without_rerunning_trim(self) -> None:
        store, scheduler = self._fake_scheduler()
        scheduler.submit_stuck_report(
            "B-CONT-SPRINT-TRIM", {"summary": "The prior mechanism is stuck."}
        )
        session_id = scheduler.apply_trim_review_decision(
            "trim", "Run a persisted sprint continuation."
        )
        sprint_id = "S-CONT-POST-TRIM"
        with scheduler._mutate() as state:
            state["sprints"][sprint_id] = {
                "sprint_id": sprint_id,
                "status": "trimmer_continuation",
                "summary": {"shared_bottlenecks": []},
            }
            state["active_sprint_id"] = sprint_id
        call_id = scheduler.prepare_call(
            "trimmer",
            {"phase": "maintain", "sprint_id": sprint_id},
            continuation={"phase": "maintain", "session_id": session_id},
        )
        lease_epoch, _ = scheduler.mark_call_running(call_id)
        scheduler.accept_call_result(call_id, lease_epoch, {"decision": "commit"})
        active = scheduler.state["trim"]["active_trim"]
        scheduler.set_trim_phase("select")
        scheduler.commit_trim(
            {"category_ids": ["CAT-CONTINUATION"]},
            expected_portfolio_revision=1,
            confirmed_through_event_id=int(active["cutoff_event_id"]),
            continuation_call_id=call_id,
        )
        stopped = scheduler.state
        self.assertEqual(stopped["calls"][call_id]["status"], "committed")
        self.assertIsNone(stopped["trim"]["active_trim"])
        self.assertEqual(scheduler.gate, GateState.OPEN)
        self.assertEqual(
            stopped["sprints"][sprint_id]["trim_integration"]["status"],
            "trim_committed",
        )

        resumed = Scheduler(store)
        resumed.recover()
        resumed.reconcile_pending_ingestion()
        resumed.complete_sprint_continuation(sprint_id)
        state = resumed.state
        self.assertEqual(state["sprints"][sprint_id]["status"], "integrated")
        self.assertIsNone(state["active_sprint_id"])
        self.assertEqual(state["trim"]["portfolio_revision"], 2)
        self.assertEqual(
            sum(event["type"] == "trim_committed" for event in state["events"]), 1
        )
        self.assertEqual(
            sum(event["type"] == "sprint_integrated" for event in state["events"]),
            1,
        )

    def test_authorized_retries_restore_exact_continuation_owners(self) -> None:
        store, scheduler = self._fake_scheduler()

        task_id = scheduler.submit_batch("B-CONT-RETRY-SYNTH", [_assignment(5)])[0]
        attempt = scheduler.start_task_attempt(task_id)
        operation_id = "OP-CONT-RETRY-SYNTH"
        scheduler.ingest_progress(
            _progress(
                task_id,
                attempt,
                progress_id="PRG-CONT-RETRY-SYNTH",
                operations=[
                    {
                        "operation_id": operation_id,
                        "kind": "route_add",
                        "proposal_id": "TMP-CONT-RETRY-SYNTH",
                        "abstract": "A route whose review will be retried.",
                    }
                ],
                evidence=[],
            )
        )
        synth_call = scheduler.prepare_synthesizer_call(operation_id)
        self._exhaust_transport_retries(scheduler, synth_call)
        self.assertEqual(
            scheduler.state["operations"][operation_id]["state"], "needs_attention"
        )
        scheduler.retry_attention_call(synth_call)
        self.assertEqual(
            scheduler.state["operations"][operation_id]["state"], "synthesizing"
        )
        lease_epoch, _ = scheduler.mark_call_running(synth_call)
        scheduler.accept_call_result(
            synth_call,
            lease_epoch,
            {
                "resolution": "new",
                "operation_digest": scheduler.state["operations"][operation_id][
                    "input_digest"
                ],
                "relied_on": [],
            },
        )
        scheduler.commit_synthesizer_call(synth_call)
        self.assertEqual(scheduler.state["calls"][synth_call]["status"], "committed")

        sprint_id = "S-CONT-RETRY-SUMMARY"
        frozen = {"plan": {"target": "O-ROOT"}, "lane_results": []}
        with scheduler._mutate() as state:
            state["sprints"][sprint_id] = {
                "sprint_id": sprint_id,
                "status": "awaiting_summary",
                "frozen_synthesis_input": frozen,
                "frozen_synthesis_digest": "fixture",
                "summary": None,
            }
        summary_call = scheduler.prepare_sprint_summarizer_call(sprint_id)
        self._exhaust_transport_retries(scheduler, summary_call)
        self.assertEqual(scheduler.state["sprints"][sprint_id]["status"], "needs_attention")
        scheduler.retry_attention_call(summary_call)
        self.assertEqual(
            scheduler.state["sprints"][sprint_id]["status"], "awaiting_summary"
        )
        lease_epoch, _ = scheduler.mark_call_running(summary_call)
        summary = {
            "mechanism_fingerprints": [],
            "differences": [],
            "shared_bottlenecks": [],
            "bridges": [],
            "negative_results": [],
            "follow_up_tasks": [],
        }
        scheduler.accept_call_result(summary_call, lease_epoch, summary)
        scheduler.commit_sprint_summarizer_call(summary_call)
        self.assertEqual(
            scheduler.state["sprints"][sprint_id]["status"],
            "trimmer_continuation",
        )

        fact_id = "F-CONT-RETRY-CHALLENGE"
        store.records[fact_id] = {
            "id": fact_id,
            "type": "fact",
            "active": True,
            "status": "active",
            "statement": "The challenged fact holds.",
            "proof": "Proof.",
            "predecessor_fact_ids": [],
            "external_references": [],
            "introduced_notation": [],
        }
        challenge_task = scheduler.submit_batch(
            "B-CONT-RETRY-CHALLENGE", [_assignment(6)]
        )[0]
        challenge_attempt = scheduler.start_task_attempt(challenge_task)
        challenge_progress = _progress(
            challenge_task,
            challenge_attempt,
            progress_id="PRG-CONT-RETRY-CHALLENGE",
            operations=[],
            evidence=[],
            outcome="finished",
        )
        challenge_progress["fact_challenges"] = [
            {
                "challenge_id": "CH-CONT-RETRY",
                "fact_id": fact_id,
                "alleged_failure": "Check the boundary case.",
            }
        ]
        scheduler.ingest_progress(challenge_progress)
        challenge_call = scheduler.prepare_challenge_verifier_call(
            "CH-CONT-RETRY"
        )
        while True:
            lease_epoch, attempt_no = scheduler.mark_call_running(challenge_call)
            scheduler.accept_call_result(
                challenge_call, lease_epoch, {"invalid_attempt": attempt_no}
            )
            if not scheduler.reject_call_result(
                challenge_call, "simulated invalid verifier report"
            ):
                break
        self.assertEqual(
            scheduler.state["challenges"]["CH-CONT-RETRY"]["state"],
            "needs_attention",
        )
        scheduler.retry_attention_call(challenge_call)
        self.assertEqual(
            scheduler.state["challenges"]["CH-CONT-RETRY"]["state"], "verifying"
        )
        lease_epoch, _ = scheduler.mark_call_running(challenge_call)
        bundle_digest = scheduler.state["calls"][challenge_call]["input"][
            "bundle_digest"
        ]
        scheduler.accept_call_result(
            challenge_call,
            lease_epoch,
            {
                "challenge_id": "CH-CONT-RETRY",
                "bundle_digest": bundle_digest,
                "resolution": "challenge_rejected",
                "justification": "The boundary case is covered.",
            },
        )
        scheduler.commit_challenge_verifier_call(challenge_call)
        self.assertEqual(
            scheduler.state["calls"][challenge_call]["status"], "committed"
        )

    def test_bootstrap_synthesizer_owner_reconciles_after_cross_key_stop(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            synth_calls = 0

            def executor(call: AgentCall) -> dict[str, Any]:
                nonlocal synth_calls
                if call.kind != "synthesizer":
                    raise AssertionError(f"unexpected call {call.kind}")
                synth_calls += 1
                return {
                    "resolution": "new",
                    "operation_digest": call.payload["operation_digest"],
                    "relied_on": [],
                }

            runtime = _CrashBootstrapStateRuntime.initialize(
                load_manifest(_manifest(Path(raw))), executor=executor
            )
            try:
                revision, bootstrap = runtime.store.load_control_state(
                    BOOTSTRAP_STATE_KEY
                )
                root_id = runtime.scheduler.state["root"]["obligation_id"]
                payload = {
                    "proposal_id": "BOOT-CONTINUATION-ROUTE",
                    "abstract": "A direct bootstrap route.",
                    "strategy_description": "Apply the defining axiom directly.",
                    "value_assessment": {
                        "confidence": "plausible",
                        "success_gain": "resolves the root target",
                        "failure_gain": "isolates the missing boundary clause",
                        "relevance": "central",
                        "novelty": "baseline direct mechanism",
                    },
                    "progress": [],
                    "related_obligation_ids": [root_id],
                    "next_steps": ["Check the defining axiom."],
                    "obstacles": [],
                    "active_fact_ids": [],
                    "relevant_memo_ids": [],
                    "relevant_claim_ids": [],
                }
                bootstrap.update(
                    {
                        "proposals": [
                            {
                                "operation_id": "bootstrap:routes:continuation",
                                "kind": "route_add",
                                "proposal_id": "BOOT-CONTINUATION-ROUTE",
                                "payload": payload,
                                "status": "pending",
                            }
                        ],
                        "mappings": {},
                        "stable": False,
                    }
                )
                runtime.store.compare_and_swap_control_state(
                    BOOTSTRAP_STATE_KEY, revision, bootstrap
                )
                runtime.start_services()
                runtime.crash_next_bootstrap_save = True
                with self.assertRaisesRegex(
                    RuntimeError, "before bootstrap owner state commit"
                ):
                    runtime._advance_bootstrap_proposals()
                calls = [
                    call
                    for call in runtime.scheduler.state["calls"].values()
                    if call["continuation"]
                    == {"bootstrap_operation_id": "bootstrap:routes:continuation"}
                ]
                self.assertEqual(len(calls), 1)
                self.assertEqual(
                    calls[0]["status"], CallState.COMMITTED.value, calls[0]
                )
                operation_status = runtime.store.operation_status(
                    "bootstrap:routes:continuation"
                )
                self.assertEqual(
                    operation_status.status, "committed", operation_status
                )
                project_dir = runtime.layout.root
            finally:
                runtime.close()

            def no_second_call(call: AgentCall) -> dict[str, Any]:
                raise AssertionError(f"recovery relaunched {call.kind}")

            resumed = FrantaRuntime.open(project_dir, executor=no_second_call)
            try:
                resumed.start_services()
                resumed.recover()
                self.assertTrue(resumed._advance_bootstrap_proposals())
                _revision, bootstrap = resumed.store.load_control_state(
                    BOOTSTRAP_STATE_KEY
                )
                self.assertTrue(bootstrap["stable"])
                self.assertEqual(
                    bootstrap["proposals"][0]["status"],
                    "committed",
                    bootstrap["proposals"][0],
                )
                self.assertEqual(
                    bootstrap["proposals"][0]["synthesizer_call_id"],
                    calls[0]["call_id"],
                )
                self.assertEqual(synth_calls, 1)
                self.assertEqual(
                    sum(
                        call["kind"] == "synthesizer"
                        and call["continuation"]
                        == {
                            "bootstrap_operation_id":
                            "bootstrap:routes:continuation"
                        }
                        for call in resumed.scheduler.state["calls"].values()
                    ),
                    1,
                )
            finally:
                resumed.close()

    def test_synthesized_update_recovers_after_store_commit_before_scheduler_tail(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            db_path = Path(raw) / "memory.sqlite3"
            store = _UpdatePublicationCrashStore(db_path, projection_dir=False)
            scheduler = Scheduler(store)
            root_id = scheduler.bootstrap(root_problem="Prove the test property.")
            store.add_obligation(
                "bootstrap-update-continuation-root",
                {
                    "id": root_id,
                    "abstract": "The root problem",
                    "statement": "Every test object has property P.",
                    "importance": "This is the project target.",
                    "predecessor_fact_ids": [],
                    "partial_progress": [],
                    "related_route_ids": [],
                    "relations": [],
                },
            )
            scheduler.bind_canonical_root_obligation()
            existing = store.add_route(
                "existing-update-continuation-route",
                {
                    "abstract": "Existing continuation route",
                    "strategy_description": "Use the defining axiom directly.",
                    "value_assessment": {
                        "confidence": "plausible",
                        "success_gain": "resolves the target",
                        "failure_gain": "isolates the missing clause",
                        "relevance": "central",
                        "novelty": "baseline",
                    },
                    "progress": [],
                    "related_obligation_ids": [root_id],
                    "next_steps": [],
                    "obstacles": [],
                    "active_fact_ids": [],
                    "relevant_memo_ids": [],
                    "relevant_claim_ids": [],
                },
            )
            route_id = existing.canonical_id
            assert route_id
            scheduler.commit_initial_trim({"category_ids": []})

            source_task = scheduler.submit_batch(
                "B-CONT-UPDATE-SOURCE", [_assignment(20)]
            )[0]
            source_attempt = scheduler.start_task_attempt(source_task)
            scheduler.ingest_progress(
                _progress(
                    source_task,
                    source_attempt,
                    progress_id="PRG-CONT-UPDATE-SOURCE",
                    operations=[
                        {
                            "operation_id": "OP-CONT-UPDATE-ROUTE",
                            "kind": "route_add",
                            "proposal_id": "TMP-CONT-UPDATE-ROUTE",
                            "abstract": "A refinement of the continuation route",
                            "strategy_description": "Use the defining axiom directly.",
                            "value_assessment": {
                                "confidence": "stronger",
                                "success_gain": "resolves the target",
                                "failure_gain": "isolates the missing clause",
                                "relevance": "central",
                                "novelty": "refinement",
                            },
                            "progress": ["A sharper continuation estimate is available."],
                            "related_obligation_ids": [root_id],
                            "next_steps": [],
                            "obstacles": [],
                            "active_fact_ids": [],
                            "relevant_memo_ids": [],
                            "relevant_claim_ids": [],
                        },
                        {
                            "operation_id": "OP-CONT-UPDATE-MEMO",
                            "kind": "memo",
                            "proposal_id": "TMP-CONT-UPDATE-MEMO",
                            "abstract": "Memo waiting across an update crash",
                            "genre": "normal",
                            "content": "Attach this memo to the synthesized update.",
                            "related_route_ids": ["TMP-CONT-UPDATE-ROUTE"],
                        }
                    ],
                    evidence=[],
                )
            )
            operation_id = "OP-CONT-UPDATE-ROUTE"
            call_id = scheduler.prepare_synthesizer_call(operation_id)
            lease, _ = scheduler.mark_call_running(call_id)
            scheduler.accept_call_result(
                call_id,
                lease,
                {
                    "resolution": "update",
                    "operation_digest": scheduler.state["operations"][operation_id][
                        "input_digest"
                    ],
                    "patch": {
                        "target_id": route_id,
                        "expected_base_revision": 1,
                        "set": {},
                        "append": {
                            "progress": ["A sharper continuation estimate is available."]
                        },
                        "add_ids": {},
                        "remove_ids": {},
                        "explanation": "Commit the durable synthesized update.",
                        "supporting_memory_ids": [],
                    },
                },
            )
            store.crash_operation_id = operation_id
            with self.assertRaisesRegex(
                _CrashAfterSemanticTail, "after atomic update"
            ):
                scheduler.commit_synthesizer_call(call_id)
            self.assertEqual(store.get(route_id).revision, 2)
            self.assertEqual(
                scheduler.state["operations"][operation_id]["state"],
                OperationState.RECEIVED.value,
            )
            store.close()

            resumed_store = MemoryStore(db_path, projection_dir=False)
            try:
                resumed = Scheduler(resumed_store)
                resumed.recover()
                resumed.reconcile_pending_ingestion()
                self.assertEqual(
                    resumed.state["operations"][operation_id]["state"],
                    OperationState.COMMITTED.value,
                )
                resumed.commit_synthesizer_call(call_id)
                self.assertEqual(
                    resumed.state["calls"][call_id]["status"],
                    CallState.COMMITTED.value,
                )
                self.assertEqual(
                    resumed.state["operations"][operation_id]["state"],
                    OperationState.COMMITTED.value,
                )
                self.assertEqual(resumed_store.get(route_id).revision, 2)
                memo_id = resumed.state["operations"]["OP-CONT-UPDATE-MEMO"][
                    "canonical_id"
                ]
                self.assertEqual(
                    resumed_store.get(memo_id)["related_route_ids"], [route_id]
                )
                self.assertEqual(
                    resumed.state["tasks"][source_task]["state"],
                    TaskState.CLOSED.value,
                )
                self.assertEqual(
                    sum(
                        call["kind"] == "synthesizer"
                        and call["continuation"] == {"operation_id": operation_id}
                        for call in resumed.state["calls"].values()
                    ),
                    1,
                )
            finally:
                resumed_store.close()


if __name__ == "__main__":
    unittest.main()
