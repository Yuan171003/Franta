from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path

from franta.config import load_manifest
from franta.runtime import AgentCall, FrantaRuntime
from franta.skill_runtime import SkillContext, SkillRuntime
from franta.workflows import GateState, OperationState, TaskState


def _manifest(root: Path, name: str) -> Path:
    path = root / "bootstrap.toml"
    path.write_text(
        "\n".join(
            [
                "[project]",
                f'name = "{name}"',
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


def _ordinary_reports(root_id: str, count: int) -> list[dict[str, object]]:
    return [
        {
            "operation_id": f"AR-ORDINARY-{index}",
            "objective": f"Independently attack the root obligation, lane {index}.",
            "work_mode": "brainstorm",
            "if_resume": None,
            "main_route_ids": [],
            "main_obligation_ids": [root_id],
            "selected_new_perspective": None,
            "assignment_portfolio": _portfolio(),
            "reason": "Use one clean ordinary attempt.",
            "root_solution_fact_id": None,
        }
        for index in range(1, count + 1)
    ]


def _stage_final(
    call: AgentCall,
    *,
    sequence: int = 1,
    outcome_status: str = "failed",
    operations: list[dict[str, object]] | None = None,
    completion_evidence_ids: list[str] | None = None,
) -> dict[str, object]:
    card = json.loads(call.workspace.task_card_path.read_text(encoding="utf-8"))
    progress_id = f"PRG-{card['task_id']}-{card['attempt']}"
    evidence = list(completion_evidence_ids or [])
    SkillRuntime(SkillContext.load(call.workspace.path)).invoke(
        "record-progress",
        {
            "operation_id": f"FINAL-{card['task_id']}-{card['attempt']}",
            "progress_id": progress_id,
            "sequence": sequence,
            "is_final": True,
            "outcome_status": outcome_status,
            "progress_since_previous": (
                "Submitted a rigorous fact candidate."
                if operations or evidence
                else "No rigorous advance survived checking."
            ),
            "operations": list(operations or []),
            "computation_operation_ids": [],
            "fact_challenges": [],
            "completion_evidence_ids": evidence,
            "attempt_summary": {
                "work_mode": card["mode"],
                "task": card["objective"],
                "proposed_outcome": outcome_status,
                "cumulative_important_progress": (
                    "Submitted a rigorous fact candidate."
                    if operations or evidence
                    else "No significant progress."
                ),
                "completion_evidence_operation_ids": evidence,
                "most_promising_next_steps": (
                    "Use the verified fact."
                    if operations or evidence
                    else "Try a different mechanism."
                ),
            },
        },
    )
    return {"attempt_ended": True, "final_progress_id": progress_id}


def _stage_failed_final(
    call: AgentCall, *, sequence: int = 1
) -> dict[str, object]:
    return _stage_final(call, sequence=sequence)


def _stage_intermediate(
    workspace_path: Path,
    progress_id: str,
    *,
    operations: list[dict[str, object]] | None = None,
) -> None:
    SkillRuntime(SkillContext.load(workspace_path)).invoke(
        "record-progress",
        {
            "operation_id": f"INTERMEDIATE-{progress_id}",
            "progress_id": progress_id,
            "sequence": 1,
            "is_final": False,
            "progress_since_previous": "A safe intermediate checkpoint.",
            "operations": list(operations or []),
            "computation_operation_ids": [],
            "fact_challenges": [],
            "completion_evidence_ids": [],
        },
    )


def _fact_operation(operation_id: str) -> dict[str, object]:
    return {
        "operation_id": operation_id,
        "kind": "fact",
        "proposal_id": f"TMP-{operation_id}",
        "candidate_id": f"FC-{operation_id}",
        "candidate_version": 1,
        "statement": "Every test object has property P.",
        "proof": "The declared definition gives property P directly.",
        "predecessor_fact_ids": [],
        "abstract": "Every test object has property P by the declared definition.",
        "keywords": ["test object", "property P"],
        "introduced_notation": [],
        "external_references": [],
        "related_route_ids": [],
    }


def _correct_verifier_report(bundle: dict[str, object]) -> dict[str, object]:
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


def _submit_full_batch(runtime: FrantaRuntime, batch_id: str) -> list[str]:
    runtime.scheduler.commit_initial_trim({"category_ids": []})
    root_id = str(runtime.scheduler.state["root"]["obligation_id"])
    return runtime.scheduler.submit_batch(
        batch_id, _ordinary_reports(root_id, 4)
    )


class OrdinaryWorkerRuntimeTests(unittest.TestCase):
    def test_task_attention_allows_healthy_sibling_retry_next_cycle(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            attempts: list[int] = []
            healthy_task_id: str | None = None

            def executor(call: AgentCall) -> dict[str, object]:
                card = json.loads(
                    call.workspace.task_card_path.read_text(encoding="utf-8")
                )
                self.assertEqual(card["task_id"], healthy_task_id)
                attempt = int(card["attempt"])
                attempts.append(attempt)
                if attempt == 1:
                    raise ConnectionError("force a healthy sibling retry")
                return _stage_failed_final(call)

            runtime = FrantaRuntime.initialize(
                load_manifest(_manifest(Path(directory), "task-attention-sibling")),
                executor=executor,
            )
            try:
                runtime.scheduler.commit_initial_trim({"category_ids": []})
                root_id = str(runtime.scheduler.state["root"]["obligation_id"])
                bad_task_id, healthy_task_id = runtime.scheduler.submit_batch(
                    "B-TASK-ATTENTION-SIBLING", _ordinary_reports(root_id, 2)
                )
                runtime.scheduler.start_task_attempt(bad_task_id)
                runtime.scheduler.mark_worker_resume_unavailable(
                    bad_task_id, "simulated missing worker thread"
                )
                runtime._main_should_run = lambda: False

                runtime.run(max_cycles=2)

                state = runtime.scheduler.state
                self.assertEqual(attempts, [1, 2])
                self.assertEqual(
                    state["tasks"][healthy_task_id]["state"], TaskState.CLOSED.value
                )
                self.assertEqual(
                    state["tasks"][bad_task_id]["state"],
                    TaskState.NEEDS_ATTENTION.value,
                )
                attention = next(
                    item
                    for item in state["needs_attention"]
                    if item["attention_id"] == f"task:{bad_task_id}:worker-resume"
                )
                self.assertEqual(attention["scope"], "task")
                self.assertEqual(attention["owner_id"], bad_task_id)
            finally:
                runtime.close()

    def test_blocking_attention_stops_healthy_sibling_before_retry_cycle(self) -> None:
        for attention_scope in ("project", "legacy"):
            with (
                self.subTest(attention_scope=attention_scope),
                tempfile.TemporaryDirectory() as directory,
            ):
                attempts: list[int] = []
                healthy_task_id: str | None = None

                def executor(call: AgentCall) -> dict[str, object]:
                    card = json.loads(
                        call.workspace.task_card_path.read_text(encoding="utf-8")
                    )
                    self.assertEqual(card["task_id"], healthy_task_id)
                    attempts.append(int(card["attempt"]))
                    raise ConnectionError("force a healthy sibling retry")

                runtime = FrantaRuntime.initialize(
                    load_manifest(
                        _manifest(
                            Path(directory),
                            f"{attention_scope}-attention-sibling",
                        )
                    ),
                    executor=executor,
                )
                try:
                    runtime.scheduler.commit_initial_trim({"category_ids": []})
                    root_id = str(
                        runtime.scheduler.state["root"]["obligation_id"]
                    )
                    bad_task_id, healthy_task_id = runtime.scheduler.submit_batch(
                        f"B-{attention_scope.upper()}-ATTENTION-SIBLING",
                        _ordinary_reports(root_id, 2),
                    )
                    runtime.scheduler.start_task_attempt(bad_task_id)
                    runtime.scheduler.mark_worker_resume_unavailable(
                        bad_task_id, "simulated missing worker thread"
                    )
                    attention_id = f"task:{bad_task_id}:worker-resume"
                    with runtime.scheduler._mutate() as state:
                        attention = next(
                            item
                            for item in state["needs_attention"]
                            if item["attention_id"] == attention_id
                        )
                        if attention_scope == "legacy":
                            attention.pop("scope", None)
                            attention.pop("owner_id", None)
                        else:
                            attention["scope"] = "project"
                            attention["owner_id"] = None
                    runtime._main_should_run = lambda: False

                    runtime.run(max_cycles=2)

                    state = runtime.scheduler.state
                    self.assertEqual(attempts, [1])
                    self.assertEqual(
                        state["tasks"][healthy_task_id]["state"],
                        TaskState.RETRY_PENDING.value,
                    )
                    self.assertEqual(
                        state["tasks"][healthy_task_id]["current_attempt"], 1
                    )
                finally:
                    runtime.close()

    def test_live_outbox_poison_interrupts_only_its_worker(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            poison_ready = threading.Event()
            healthy_live_ingested = threading.Event()
            release_workers = threading.Event()
            bad_task_id: str | None = None

            def executor(call: AgentCall) -> dict[str, object]:
                card = json.loads(
                    call.workspace.task_card_path.read_text(encoding="utf-8")
                )
                if card["task_id"] == bad_task_id:
                    poison_dir = call.workspace.outbox_path / "record-progress"
                    poison_dir.mkdir(parents=True, exist_ok=True)
                    (poison_dir / "BROKEN.json").write_text(
                        "{not-json", encoding="utf-8"
                    )
                    poison_ready.set()
                    release_workers.wait(timeout=5)
                    return _stage_failed_final(call)
                _stage_intermediate(
                    call.workspace.path,
                    f"PRG-LIVE-HEALTHY-{card['task_id']}",
                )
                release_workers.wait(timeout=5)
                return _stage_failed_final(call, sequence=2)

            runtime = FrantaRuntime.initialize(
                load_manifest(_manifest(Path(directory), "live-worker-poison")),
                executor=executor,
            )
            try:
                runtime.scheduler.commit_initial_trim({"category_ids": []})
                root_id = str(runtime.scheduler.state["root"]["obligation_id"])
                task_ids = runtime.scheduler.submit_batch(
                    "B-LIVE-POISON", _ordinary_reports(root_id, 2)
                )
                bad_task_id, healthy_task_id = task_ids
                healthy_progress_id = f"PRG-LIVE-HEALTHY-{healthy_task_id}"
                original_ingest = runtime._ingest_worker_outbox

                def ingest_and_release(*args: object, **kwargs: object) -> bool:
                    try:
                        changed = original_ingest(*args, **kwargs)
                    except Exception:
                        if poison_ready.is_set() and healthy_live_ingested.is_set():
                            release_workers.set()
                        raise
                    if healthy_progress_id in runtime.scheduler.state["progress"]:
                        healthy_live_ingested.set()
                    if poison_ready.is_set() and healthy_live_ingested.is_set():
                        release_workers.set()
                    return changed

                runtime._ingest_worker_outbox = ingest_and_release  # type: ignore[method-assign]
                cancelled: list[str] = []
                runtime.transport.cancel = (  # type: ignore[method-assign]
                    lambda call_id, *, reason="": cancelled.append(call_id) or True
                )

                runtime.start_services()
                self.assertTrue(runtime._run_worker_batch(task_ids))

                state = runtime.scheduler.state
                self.assertTrue(healthy_live_ingested.is_set(), state)
                self.assertIn(healthy_progress_id, state["progress"])
                self.assertEqual(
                    state["tasks"][healthy_task_id]["state"], TaskState.CLOSED.value
                )
                bad_task = state["tasks"][bad_task_id]
                self.assertEqual(bad_task["state"], TaskState.RETRY_PENDING.value)
                self.assertEqual(bad_task["attempts"][-1]["state"], "interrupted")
                bad_call_id = str(bad_task["attempts"][-1]["call_id"])
                self.assertEqual(state["calls"][bad_call_id]["status"], "superseded")
                self.assertEqual(cancelled, [bad_call_id])
                self.assertFalse(
                    any(
                        item.get("resolved_at") is None
                        for item in state["needs_attention"]
                    )
                )
            finally:
                release_workers.set()
                runtime.close()

    def test_nonfinal_fact_synthesizes_live_then_verifies_after_owner_ends(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            operation_id = "OP-LIVE-NONFINAL-FACT"
            owner_task_id: str | None = None
            sibling_started = threading.Event()
            release_owner = threading.Event()
            release_sibling = threading.Event()
            synthesis_committed = threading.Event()
            fact_published = threading.Event()
            review_order: list[str] = []

            def executor(call: AgentCall) -> dict[str, object]:
                if call.kind == "synthesizer":
                    return {
                        "resolution": "new",
                        "operation_digest": call.payload["operation_digest"],
                        "explanation": "The proposed fact is not yet represented.",
                        "canonical_id": None,
                        "relied_on": [],
                        "patch": None,
                    }
                if call.kind == "verifier":
                    return _correct_verifier_report(dict(call.payload))
                self.assertEqual(call.kind, "worker")
                card = json.loads(
                    call.workspace.task_card_path.read_text(encoding="utf-8")
                )
                if card["task_id"] == owner_task_id:
                    _stage_intermediate(
                        call.workspace.path,
                        f"PRG-LIVE-NONFINAL-{card['task_id']}",
                        operations=[_fact_operation(operation_id)],
                    )
                    release_owner.wait()
                    return _stage_final(
                        call,
                        sequence=2,
                        outcome_status="finished",
                        completion_evidence_ids=[operation_id],
                    )
                sibling_started.set()
                release_sibling.wait()
                return _stage_failed_final(call)

            runtime = FrantaRuntime.initialize(
                load_manifest(_manifest(Path(directory), "live-nonfinal-fact")),
                executor=executor,
            )
            batch_errors: list[BaseException] = []
            batch_results: list[bool] = []
            batch_thread: threading.Thread | None = None
            try:
                runtime.scheduler.commit_initial_trim({"category_ids": []})
                root_id = str(runtime.scheduler.state["root"]["obligation_id"])
                owner_task_id, sibling_task_id = runtime.scheduler.submit_batch(
                    "B-LIVE-NONFINAL-FACT", _ordinary_reports(root_id, 2)
                )
                original_review = runtime._commit_operation_review_call

                def observe_review(call_id: str, workspace: object) -> object:
                    kind = str(runtime.scheduler.state["calls"][call_id]["kind"])
                    result = original_review(call_id, workspace)  # type: ignore[arg-type]
                    review_order.append(kind)
                    if kind == "synthesizer":
                        synthesis_committed.set()
                    elif kind == "verifier":
                        fact_published.set()
                    return result

                runtime._commit_operation_review_call = observe_review  # type: ignore[method-assign]

                def run_batch() -> None:
                    try:
                        batch_results.append(runtime._run_worker_batch(
                            [owner_task_id, sibling_task_id]
                        ))
                    except BaseException as exc:
                        batch_errors.append(exc)

                runtime.start_services()
                batch_thread = threading.Thread(target=run_batch, daemon=True)
                batch_thread.start()

                self.assertTrue(sibling_started.wait(timeout=10))
                self.assertTrue(
                    synthesis_committed.wait(timeout=10), runtime.scheduler.state
                )
                synthesized = runtime.scheduler.state
                self.assertEqual(
                    synthesized["tasks"][owner_task_id]["state"],
                    TaskState.RUNNING.value,
                )
                self.assertEqual(
                    synthesized["operations"][operation_id]["state"],
                    OperationState.VERIFYING.value,
                )
                self.assertIsNone(
                    synthesized["operations"][operation_id]["canonical_id"]
                )
                self.assertFalse(
                    any(
                        call["kind"] == "verifier"
                        for call in synthesized["calls"].values()
                    ),
                    synthesized["calls"],
                )

                release_owner.set()
                self.assertTrue(
                    fact_published.wait(timeout=10), runtime.scheduler.state
                )
                published = runtime.scheduler.state
                operation = published["operations"][operation_id]
                self.assertEqual(operation["state"], OperationState.COMMITTED.value)
                self.assertIsNotNone(operation["canonical_id"])
                self.assertEqual(
                    runtime.store.get(str(operation["canonical_id"]))["type"], "fact"
                )
                self.assertEqual(
                    published["tasks"][owner_task_id]["state"], TaskState.CLOSED.value
                )
                self.assertEqual(
                    published["tasks"][sibling_task_id]["state"],
                    TaskState.RUNNING.value,
                )
                self.assertEqual(review_order, ["synthesizer", "verifier"])
            finally:
                release_owner.set()
                release_sibling.set()
                if batch_thread is not None:
                    batch_thread.join(timeout=10)
                runtime.close()

            self.assertIsNotNone(batch_thread)
            self.assertFalse(batch_thread.is_alive())
            if batch_errors:
                raise batch_errors[0]
            self.assertEqual(batch_results, [True])

    def test_final_fact_from_early_worker_publishes_before_sibling_finishes(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            operation_id = "OP-EARLY-FINAL-FACT"
            owner_task_id: str | None = None
            sibling_started = threading.Event()
            release_sibling = threading.Event()
            fact_published = threading.Event()
            review_order: list[str] = []

            def executor(call: AgentCall) -> dict[str, object]:
                if call.kind == "synthesizer":
                    return {
                        "resolution": "new",
                        "operation_digest": call.payload["operation_digest"],
                        "explanation": "The proposed fact is not yet represented.",
                        "canonical_id": None,
                        "relied_on": [],
                        "patch": None,
                    }
                if call.kind == "verifier":
                    return _correct_verifier_report(dict(call.payload))
                self.assertEqual(call.kind, "worker")
                card = json.loads(
                    call.workspace.task_card_path.read_text(encoding="utf-8")
                )
                if card["task_id"] == owner_task_id:
                    sibling_started.wait(timeout=10)
                    return _stage_final(
                        call,
                        outcome_status="finished",
                        operations=[_fact_operation(operation_id)],
                        completion_evidence_ids=[operation_id],
                    )
                sibling_started.set()
                release_sibling.wait()
                return _stage_failed_final(call)

            runtime = FrantaRuntime.initialize(
                load_manifest(_manifest(Path(directory), "early-final-fact")),
                executor=executor,
            )
            batch_errors: list[BaseException] = []
            batch_results: list[bool] = []
            batch_thread: threading.Thread | None = None
            try:
                runtime.scheduler.commit_initial_trim({"category_ids": []})
                root_id = str(runtime.scheduler.state["root"]["obligation_id"])
                owner_task_id, sibling_task_id = runtime.scheduler.submit_batch(
                    "B-EARLY-FINAL-FACT", _ordinary_reports(root_id, 2)
                )
                original_review = runtime._commit_operation_review_call

                def observe_review(call_id: str, workspace: object) -> object:
                    kind = str(runtime.scheduler.state["calls"][call_id]["kind"])
                    result = original_review(call_id, workspace)  # type: ignore[arg-type]
                    review_order.append(kind)
                    if kind == "verifier":
                        fact_published.set()
                    return result

                runtime._commit_operation_review_call = observe_review  # type: ignore[method-assign]

                def run_batch() -> None:
                    try:
                        batch_results.append(runtime._run_worker_batch(
                            [owner_task_id, sibling_task_id]
                        ))
                    except BaseException as exc:
                        batch_errors.append(exc)

                runtime.start_services()
                batch_thread = threading.Thread(target=run_batch, daemon=True)
                batch_thread.start()

                self.assertTrue(sibling_started.wait(timeout=10))
                self.assertTrue(
                    fact_published.wait(timeout=10), runtime.scheduler.state
                )
                published = runtime.scheduler.state
                operation = published["operations"][operation_id]
                self.assertEqual(operation["state"], OperationState.COMMITTED.value)
                self.assertIsNotNone(operation["canonical_id"])
                self.assertIn(
                    f"PRG-{owner_task_id}-1", published["progress"]
                )
                self.assertEqual(
                    published["tasks"][owner_task_id]["state"], TaskState.CLOSED.value
                )
                self.assertEqual(
                    published["tasks"][sibling_task_id]["state"],
                    TaskState.RUNNING.value,
                )
                self.assertEqual(review_order, ["synthesizer", "verifier"])
            finally:
                release_sibling.set()
                if batch_thread is not None:
                    batch_thread.join(timeout=10)
                runtime.close()

            self.assertIsNotNone(batch_thread)
            self.assertFalse(batch_thread.is_alive())
            if batch_errors:
                raise batch_errors[0]
            self.assertEqual(batch_results, [True])

    def test_interrupted_owner_releases_synthesized_fact_before_sibling_finishes(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            operation_id = "OP-INTERRUPTED-LIVE-FACT"
            owner_task_id: str | None = None
            sibling_started = threading.Event()
            interrupt_owner = threading.Event()
            release_sibling = threading.Event()
            synthesis_committed = threading.Event()
            fact_published = threading.Event()

            def executor(call: AgentCall) -> dict[str, object]:
                if call.kind == "synthesizer":
                    return {
                        "resolution": "new",
                        "operation_digest": call.payload["operation_digest"],
                        "explanation": "The proposed fact is not yet represented.",
                        "canonical_id": None,
                        "relied_on": [],
                        "patch": None,
                    }
                if call.kind == "verifier":
                    return _correct_verifier_report(dict(call.payload))
                self.assertEqual(call.kind, "worker")
                card = json.loads(
                    call.workspace.task_card_path.read_text(encoding="utf-8")
                )
                if card["task_id"] == owner_task_id:
                    _stage_intermediate(
                        call.workspace.path,
                        f"PRG-INTERRUPTED-LIVE-{card['task_id']}",
                        operations=[_fact_operation(operation_id)],
                    )
                    interrupt_owner.wait()
                    raise ConnectionError("disconnect after live synthesis")
                sibling_started.set()
                release_sibling.wait()
                return _stage_failed_final(call)

            runtime = FrantaRuntime.initialize(
                load_manifest(_manifest(Path(directory), "interrupted-live-fact")),
                executor=executor,
            )
            batch_errors: list[BaseException] = []
            batch_results: list[bool] = []
            batch_thread: threading.Thread | None = None
            try:
                runtime.scheduler.commit_initial_trim({"category_ids": []})
                root_id = str(runtime.scheduler.state["root"]["obligation_id"])
                owner_task_id, sibling_task_id = runtime.scheduler.submit_batch(
                    "B-INTERRUPTED-LIVE-FACT", _ordinary_reports(root_id, 2)
                )
                original_review = runtime._commit_operation_review_call

                def observe_review(call_id: str, workspace: object) -> object:
                    kind = str(runtime.scheduler.state["calls"][call_id]["kind"])
                    result = original_review(call_id, workspace)  # type: ignore[arg-type]
                    if kind == "synthesizer":
                        synthesis_committed.set()
                    elif kind == "verifier":
                        fact_published.set()
                    return result

                runtime._commit_operation_review_call = observe_review  # type: ignore[method-assign]

                def run_batch() -> None:
                    try:
                        batch_results.append(runtime._run_worker_batch(
                            [owner_task_id, sibling_task_id]
                        ))
                    except BaseException as exc:
                        batch_errors.append(exc)

                runtime.start_services()
                batch_thread = threading.Thread(target=run_batch, daemon=True)
                batch_thread.start()

                self.assertTrue(sibling_started.wait(timeout=10))
                self.assertTrue(
                    synthesis_committed.wait(timeout=10), runtime.scheduler.state
                )
                synthesized = runtime.scheduler.state
                self.assertEqual(
                    synthesized["tasks"][owner_task_id]["state"],
                    TaskState.RUNNING.value,
                )
                self.assertEqual(
                    synthesized["operations"][operation_id]["state"],
                    OperationState.VERIFYING.value,
                )
                self.assertFalse(
                    any(
                        call["kind"] == "verifier"
                        for call in synthesized["calls"].values()
                    )
                )

                interrupt_owner.set()
                self.assertTrue(
                    fact_published.wait(timeout=10), runtime.scheduler.state
                )
                published = runtime.scheduler.state
                owner = published["tasks"][owner_task_id]
                self.assertEqual(owner["state"], TaskState.RETRY_PENDING.value)
                self.assertEqual(owner["interruption_retry_count"], 1)
                self.assertEqual(owner["attempts"][-1]["state"], "interrupted")
                self.assertEqual(
                    published["operations"][operation_id]["state"],
                    OperationState.COMMITTED.value,
                )
                self.assertEqual(
                    published["tasks"][sibling_task_id]["state"],
                    TaskState.RUNNING.value,
                )
            finally:
                interrupt_owner.set()
                release_sibling.set()
                if batch_thread is not None:
                    batch_thread.join(timeout=10)
                runtime.close()

            self.assertIsNotNone(batch_thread)
            self.assertFalse(batch_thread.is_alive())
            if batch_errors:
                raise batch_errors[0]
            self.assertEqual(batch_results, [True])

    def test_resume_skips_poisoned_workspace_and_relaunches_tasks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            initial = FrantaRuntime.initialize(
                load_manifest(_manifest(Path(directory), "resume-worker-poison")),
                executor=lambda call: _stage_failed_final(call),
            )
            initial.scheduler.commit_initial_trim({"category_ids": []})
            root_id = str(initial.scheduler.state["root"]["obligation_id"])
            task_ids = initial.scheduler.submit_batch(
                "B-RESUME-POISON", _ordinary_reports(root_id, 2)
            )
            bad_task_id, healthy_task_id = task_ids
            bad_call_id, _, bad_workspace = initial._prepare_worker_workspace(
                bad_task_id
            )
            healthy_call_id, _, healthy_workspace = initial._prepare_worker_workspace(
                healthy_task_id
            )
            poison_dir = bad_workspace.outbox_path / "record-progress"
            poison_dir.mkdir(parents=True, exist_ok=True)
            poison_path = poison_dir / "BROKEN.json"
            poison_path.write_text("{not-json", encoding="utf-8")
            healthy_progress_id = f"PRG-RESUME-HEALTHY-{healthy_task_id}"
            _stage_intermediate(healthy_workspace.path, healthy_progress_id)
            project = initial.layout.root
            initial.close()

            resumed = FrantaRuntime.open(
                project, executor=lambda call: _stage_failed_final(call)
            )
            try:
                resumed.recover()
                resumed.recover()

                recovered = resumed.scheduler.state
                self.assertIn(healthy_progress_id, recovered["progress"])
                for task_id, call_id in (
                    (bad_task_id, bad_call_id),
                    (healthy_task_id, healthy_call_id),
                ):
                    task = recovered["tasks"][task_id]
                    self.assertEqual(task["state"], TaskState.RETRY_PENDING.value)
                    self.assertEqual(task["interruption_retry_count"], 1)
                    self.assertEqual(task["attempts"][-1]["state"], "interrupted")
                    self.assertEqual(recovered["calls"][call_id]["status"], "superseded")
                self.assertTrue(poison_path.exists())
                self.assertFalse(
                    any(
                        item.get("resolved_at") is None
                        for item in recovered["needs_attention"]
                    )
                )

                resumed.start_services()
                self.assertTrue(resumed._run_worker_batch(task_ids))
                final_state = resumed.scheduler.state
                self.assertTrue(
                    all(
                        final_state["tasks"][task_id]["state"]
                        == TaskState.CLOSED.value
                        for task_id in task_ids
                    ),
                    final_state["tasks"],
                )
            finally:
                resumed.close()

    def test_fresh_full_reserved_batch_launches_despite_zero_nominal_free_slots(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            launched: list[str] = []

            def executor(call: AgentCall) -> dict[str, object]:
                self.assertEqual(call.kind, "worker")
                card = json.loads(
                    call.workspace.task_card_path.read_text(encoding="utf-8")
                )
                launched.append(str(card["task_id"]))
                return _stage_failed_final(call)

            runtime = FrantaRuntime.initialize(
                load_manifest(_manifest(Path(directory), "fresh-full-batch")),
                executor=executor,
            )
            try:
                task_ids = _submit_full_batch(runtime, "B-FRESH-FULL")
                self.assertEqual(runtime.scheduler.free_non_verifier_slots(), 0)
                runtime._main_should_run = lambda: False

                runtime.run(max_cycles=1)

                self.assertCountEqual(launched, task_ids)
                self.assertTrue(
                    all(
                        runtime.scheduler.state["tasks"][task_id]["state"]
                        == TaskState.CLOSED.value
                        for task_id in task_ids
                    )
                )
            finally:
                runtime.close()

    def test_recovered_full_reserved_batch_relaunches_all_four_intents(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            initial = FrantaRuntime.initialize(
                load_manifest(_manifest(Path(directory), "recovered-full-batch"))
            )
            project = initial.layout.root
            task_ids = _submit_full_batch(initial, "B-RECOVERED-FULL")
            self.assertEqual(initial.scheduler.free_non_verifier_slots(), 0)
            initial.close()

            launched: list[str] = []

            def executor(call: AgentCall) -> dict[str, object]:
                card = json.loads(
                    call.workspace.task_card_path.read_text(encoding="utf-8")
                )
                launched.append(str(card["task_id"]))
                return _stage_failed_final(call)

            resumed = FrantaRuntime.open(project, executor=executor)
            try:
                resumed._main_should_run = lambda: False
                resumed.run(resume=True, max_cycles=1)

                self.assertCountEqual(launched, task_ids)
                self.assertTrue(
                    all(
                        resumed.scheduler.state["tasks"][task_id]["state"]
                        == TaskState.CLOSED.value
                        for task_id in task_ids
                    )
                )
            finally:
                resumed.close()

    def test_disconnect_retry_runs_before_new_main_planning(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            order: list[str] = []

            def executor(call: AgentCall) -> dict[str, object]:
                card = json.loads(
                    call.workspace.task_card_path.read_text(encoding="utf-8")
                )
                attempt = int(card["attempt"])
                order.append(f"worker:{attempt}")
                if attempt == 1:
                    raise ConnectionError("simulated worker disconnect")
                return _stage_failed_final(call)

            runtime = FrantaRuntime.initialize(
                load_manifest(_manifest(Path(directory), "retry-before-main")),
                executor=executor,
            )
            try:
                runtime.scheduler.commit_initial_trim({"category_ids": []})
                root_id = str(runtime.scheduler.state["root"]["obligation_id"])
                task_id = runtime.scheduler.submit_batch(
                    "B-RETRY-BEFORE-MAIN", _ordinary_reports(root_id, 1)
                )[0]
                runtime._run_main = lambda: order.append("main") or True

                runtime.run(max_cycles=2)

                self.assertEqual(order, ["worker:1", "worker:2", "main"])
                task = runtime.scheduler.state["tasks"][task_id]
                self.assertEqual(task["state"], TaskState.CLOSED.value)
                self.assertEqual(task["current_attempt"], 2)
            finally:
                runtime.close()

    def test_resolution_pending_terminal_main_is_not_deferred_by_retry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            order: list[str] = []

            def executor(call: AgentCall) -> dict[str, object]:
                card = json.loads(
                    call.workspace.task_card_path.read_text(encoding="utf-8")
                )
                order.append(f"worker:{card['attempt']}")
                raise ConnectionError("disconnect while resolution is pending")

            runtime = FrantaRuntime.initialize(
                load_manifest(_manifest(Path(directory), "terminal-main-order")),
                executor=executor,
            )
            try:
                runtime.scheduler.commit_initial_trim({"category_ids": []})
                root_id = str(runtime.scheduler.state["root"]["obligation_id"])
                task_id = runtime.scheduler.submit_batch(
                    "B-TERMINAL-MAIN-ORDER", _ordinary_reports(root_id, 1)
                )[0]
                runtime.scheduler.start_task_attempt(task_id)
                runtime.scheduler.record_worker_interruption(
                    task_id, "disconnect before the resumed run"
                )
                with runtime.scheduler._mutate() as state:
                    runtime.scheduler._record_root_resolution_in_state(
                        state,
                        "F-TEST-ROOT-RESOLUTION",
                        "proved",
                        operation_id="OP-TEST-ROOT-RESOLUTION",
                    )
                runtime._run_main = lambda: order.append("terminal-main") or True

                runtime.run(max_cycles=1)

                self.assertEqual(order, ["worker:2", "terminal-main"])
                self.assertEqual(runtime.scheduler.gate, GateState.RESOLUTION_PENDING)
                self.assertEqual(
                    runtime.scheduler.state["tasks"][task_id]["state"],
                    TaskState.RETRY_PENDING.value,
                )
            finally:
                runtime.close()


if __name__ == "__main__":
    unittest.main()
