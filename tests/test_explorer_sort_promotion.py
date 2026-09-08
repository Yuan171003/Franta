from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone
import hashlib
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from typing import Any


SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from franta.access import policy_for  # noqa: E402
from franta.config import load_manifest  # noqa: E402
from franta.contracts.canonical import MemoryType  # noqa: E402
from franta.explorer.contracts import (  # noqa: E402
    ExplorerNotFoundError,
    ExplorerValidationError,
    ExplorerWriteContext,
)
from franta.explorer_adapter import (  # noqa: E402
    build_main_sort_explorer_snapshot_for_frozen_turn,
)
from franta.materialize import (  # noqa: E402
    MaterializationError,
    WorkspaceMaterializer,
    explorer_snapshot_digest,
)
from franta.prompts import ModelConfig  # noqa: E402
from franta.runtime import AgentCall, FrantaRuntime, InvalidAgentOutput  # noqa: E402
from franta.scheduler import Scheduler  # noqa: E402
from franta.skill_runtime import (  # noqa: E402
    SkillContext,
    SkillRuntime,
    SkillRuntimeError,
)
from franta.testing import FakeControlStore  # noqa: E402
from franta.workflows import CallState, OperationState, TaskState  # noqa: E402


TURN = "XTURN-00000001"
OTHER_TURN = "XTURN-00000002"
SORT_RUN = "SORT-XCAS-1"


def _manifest(root: Path) -> Path:
    path = root / "bootstrap.toml"
    path.write_text(
        """
[project]
name = "explorer-sort-promotion"
directory = "project"
root_problem = "Prove the ROOT statement."
foundation_policy = "Use only declared assumptions."

[explorer]
max_workers = 1

[initial]
""".strip()
        + "\n",
        encoding="utf-8",
    )
    return path


def _runtime(root: Path) -> FrantaRuntime:
    transport = types.SimpleNamespace(state_dir=root / "transport")
    return FrantaRuntime.initialize(
        load_manifest(_manifest(root)), transport=transport
    )


def _explorer_call(
    runtime: FrantaRuntime,
    call_id: str,
    *,
    turn_id: str,
    worker_session_id: str,
) -> AgentCall:
    policy = policy_for("explorer-worker", mode="clean-room")
    payload = {
        "explorer_turn_id": turn_id,
        "worker_session_id": worker_session_id,
        "attempt_number": 1,
        "source_high_water_seq": 0,
    }
    runtime.scheduler.prepare_call(
        "explorer-worker",
        payload,
        call_id=call_id,
        retry_limit=1,
        continuation={
            "lineage_id": worker_session_id,
            "attempt_number": 1,
            "turn_id": turn_id,
            "session_key": f"explorer:{worker_session_id}",
        },
    )
    lease_epoch, launch_attempt = runtime.scheduler.mark_call_running(call_id)
    workspace = runtime._make_workspace(
        call_id=call_id,
        policy=policy,
        context=payload,
    )
    return AgentCall(
        call_id=call_id,
        kind="explorer-worker",
        role="explorer-worker",
        mode="clean-room",
        payload=payload,
        workspace=workspace,
        policy=policy,
        prompt="",
        session_key=None,
        resume=False,
        output_schema=runtime.layout.schemas / "explorer-worker.schema.json",
        model_config=ModelConfig("gpt-6-astra", "ultra"),
        lease_epoch=lease_epoch,
        launch_attempt=launch_attempt,
    )


def _trust_skill(
    runtime: FrantaRuntime,
    call: AgentCall,
    skill: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    staged = SkillRuntime(SkillContext.load(call.workspace.path)).invoke(
        skill, payload
    )
    runtime._record_broker_skill_result(
        call=call,
        skill=skill,
        payload=payload,
        result=staged,
        capability_token=f"capability-{call.call_id}",
    )
    return staged


def _cas_payload(operation_id: str, *, exit_status: int) -> dict[str, Any]:
    return {
        "operation_id": operation_id,
        "software": "Python",
        "software_version": "3.14",
        "exact_input": "print(2 + 2)",
        "exact_output": "4\n" if exit_status == 0 else "",
        "exit_status": exit_status,
        "description": "Evaluate a finite model attached to ROOT.",
        "assumptions": "Integer arithmetic.",
        "environment_versions": {"python": "3.14"},
        "random_seed": None,
        "interpretation": "The finite model has value four.",
        "related_ids": {},
    }


def _trust_cas_and_scratch(
    runtime: FrantaRuntime,
    *,
    call_id: str,
    turn_id: str,
    worker_session_id: str,
    cas_operation_id: str,
    exit_status: int,
) -> tuple[str, str]:
    call = _explorer_call(
        runtime,
        call_id,
        turn_id=turn_id,
        worker_session_id=worker_session_id,
    )
    _trust_skill(
        runtime,
        call,
        "CAS",
        _cas_payload(cas_operation_id, exit_status=exit_status),
    )
    repository = runtime.explorer_repository
    assert repository is not None
    evidence = repository.get_cas_evidence_by_operation(cas_operation_id)
    assert evidence is not None
    scratch = _trust_skill(
        runtime,
        call,
        "record-scratch",
        {
            "operation_id": f"SCRATCH-{cas_operation_id}",
            "record_kind": "computation",
            "abstract": f"Finite-model computation {cas_operation_id}",
            "content": "The exact computation may be useful to Franta sorting.",
            "related_memory_ids": [],
            "cas_operation_ids": [cas_operation_id],
        },
    )
    return evidence.evidence_id, str(scratch["record_id"])


def _progress_with_promotion(
    task_id: str,
    evidence_id: str,
    source_record_id: str,
    *,
    final: bool,
) -> dict[str, Any]:
    value: dict[str, Any] = {
        "operation_id": "OP-SORT-PROGRESS",
        "progress_id": "PRG-SORT-PROMOTION",
        "task_id": task_id,
        "attempt": 1,
        "sequence": 1,
        "is_final": final,
        "outcome_status": "progress",
        "operations": [],
        "computation_operation_ids": [],
        "explorer_computation_promotions": [
            {
                "evidence_id": evidence_id,
                "source_record_ids": [source_record_id],
            }
        ],
        "completion_evidence_ids": [],
    }
    if final:
        value["attempt_summary"] = {
            "work_mode": "main-sort",
            "task": "Sort the frozen Explorer turn.",
            "proposed_outcome": "progress",
            "cumulative_important_progress": "Promoted one trusted computation.",
            "completion_evidence_operation_ids": [],
            "most_promising_next_steps": "Use the computation in Franta planning.",
        }
    return value


def _ordinary_report() -> dict[str, Any]:
    return {
        "report_id": "AR-ORDINARY",
        "objective": "Investigate an ordinary route.",
        "if_resume": None,
        "mode": "research",
        "main_route_ids": ["R-ordinary"],
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
        "reason": "Exercise the ingestion boundary.",
    }


def _trust_direct_scratch(
    runtime: FrantaRuntime,
    *,
    record_id: str,
    turn_id: str,
    operation_id: str,
) -> None:
    repository = runtime.explorer_repository
    assert repository is not None
    receipt = hashlib.sha256(record_id.encode()).hexdigest()
    repository.prepare_scratch(
        ExplorerWriteContext(
            turn_id=turn_id,
            worker_session_id=f"EWORK-{turn_id}",
            attempt_no=1,
            call_id=f"CALL-{record_id}",
        ),
        {
            "skill": "record-scratch",
            "operation_id": operation_id,
            "record_id": record_id,
            "record_kind": "idea",
            "abstract": f"Source {record_id}",
            "content": "A provisional idea for sorting.",
            "related_memory_ids": [],
            "cas_operation_ids": [],
        },
        receipt,
    )
    repository.trust_receipt(receipt)


def _begin_sort(
    runtime: FrantaRuntime,
    *,
    sort_run_id: str,
    turn_id: str = TURN,
) -> tuple[str, str]:
    repository = runtime.explorer_repository
    assert repository is not None
    frozen = repository.freeze_turn(turn_id)
    deadline = datetime.now(timezone.utc) + timedelta(days=1)
    runtime.scheduler.activate_alternation()
    runtime.scheduler.tick_alternation(now=deadline)
    snapshot = build_main_sort_explorer_snapshot_for_frozen_turn(
        repository,
        sort_run_id=sort_run_id,
        frozen_turn=frozen,
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
    call_id = runtime.scheduler.begin_main_sort_task_attempt(
        task_id,
        sort_run_id=sort_run_id,
        session_key=f"main:{sort_run_id}",
        now=deadline + timedelta(seconds=1),
    )
    return task_id, call_id


class ExplorerSortPromotionTests(unittest.TestCase):
    def test_main_sort_workspace_has_exact_read_only_frozen_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            runtime = _runtime(Path(raw))
            try:
                evidence_id, scratch_id = _trust_cas_and_scratch(
                    runtime,
                    call_id="CALL-SNAPSHOT-SOURCE",
                    turn_id=TURN,
                    worker_session_id="EWORK-SNAPSHOT",
                    cas_operation_id="CAS-SNAPSHOT",
                    exit_status=0,
                )
                repository = runtime.explorer_repository
                assert repository is not None
                summary_receipt = hashlib.sha256(
                    b"snapshot summary receipt"
                ).hexdigest()
                summary = repository.prepare_summary(
                    ExplorerWriteContext(
                        turn_id=TURN,
                        worker_session_id="EWORK-SNAPSHOT",
                        attempt_no=1,
                        call_id="CALL-SNAPSHOT-SOURCE",
                    ),
                    {
                        "skill": "record-summary",
                        "operation_id": "SUMMARY-SNAPSHOT",
                        "record_id": "ESUM-snapshot",
                        "abstract": "Complete snapshot summary",
                        "content": "The summary indexes its exact computation scratch.",
                        "directions_tried": ["Inspect a finite model."],
                        "main_progress": "Produced one trusted computation.",
                        "main_obstacles": "The global argument remains open.",
                        "source_scratch_ids": [scratch_id],
                    },
                    summary_receipt,
                )
                repository.trust_receipt(summary_receipt)

                task_id, call_id = _begin_sort(
                    runtime, sort_run_id="SORT-SNAPSHOT"
                )
                _trust_direct_scratch(
                    runtime,
                    record_id="ES-after-snapshot",
                    turn_id=TURN,
                    operation_id="OP-AFTER-SNAPSHOT",
                )
                _trust_direct_scratch(
                    runtime,
                    record_id="ES-other-turn-snapshot",
                    turn_id=OTHER_TURN,
                    operation_id="OP-OTHER-TURN-SNAPSHOT",
                )
                untrusted_receipt = hashlib.sha256(
                    b"untrusted snapshot source"
                ).hexdigest()
                repository.prepare_scratch(
                    ExplorerWriteContext(
                        turn_id=TURN,
                        worker_session_id="EWORK-UNTRUSTED-SNAPSHOT",
                        attempt_no=1,
                        call_id="CALL-UNTRUSTED-SNAPSHOT",
                    ),
                    {
                        "skill": "record-scratch",
                        "operation_id": "OP-UNTRUSTED-SNAPSHOT",
                        "record_id": "ES-untrusted-snapshot",
                        "record_kind": "idea",
                        "abstract": "Untrusted snapshot source",
                        "content": "This prepared record must remain invisible.",
                        "related_memory_ids": [],
                        "cas_operation_ids": [],
                    },
                    untrusted_receipt,
                )

                task = runtime.scheduler.state["tasks"][task_id]
                card = runtime._enrich_task_card(task, 1, None)
                policy = policy_for("main-sort")
                workspace = runtime._make_workspace(
                    call_id=call_id,
                    policy=policy,
                    task_card=card,
                )
                snapshot_root = workspace.input_path / "explorer_snapshot"
                manifest = json.loads(
                    (snapshot_root / "manifest.json").read_text(encoding="utf-8")
                )
                self.assertEqual(manifest["record_count"], 2)
                self.assertEqual(manifest["scratch_count"], 1)
                self.assertEqual(manifest["summary_count"], 1)
                self.assertEqual(card["explorer_snapshot_format_version"], 1)
                self.assertEqual(
                    manifest["snapshot_digest"],
                    card["explorer_snapshot_digest"],
                )
                entries = {item["id"]: item for item in manifest["records"]}
                self.assertEqual(set(entries), {scratch_id, summary.record_id})
                self.assertNotIn("ES-after-snapshot", entries)
                self.assertNotIn("ES-other-turn-snapshot", entries)
                self.assertNotIn("ES-untrusted-snapshot", entries)

                scratch = json.loads(
                    (snapshot_root / entries[scratch_id]["path"]).read_text(
                        encoding="utf-8"
                    )
                )
                summary_record = json.loads(
                    (snapshot_root / entries[summary.record_id]["path"]).read_text(
                        encoding="utf-8"
                    )
                )
                self.assertEqual(
                    [item["evidence_id"] for item in scratch["cas_evidence"]],
                    [evidence_id],
                )
                self.assertEqual(summary_record["source_scratch_ids"], [scratch_id])
                self.assertNotIn("exported_record_ids", scratch)
                self.assertEqual(
                    (snapshot_root / entries[scratch_id]["path"]).stat().st_mode
                    & 0o777,
                    0o444,
                )
                self.assertEqual(snapshot_root.stat().st_mode & 0o777, 0o555)
                self.assertFalse(policy.explorer_memory_api)
                self.assertFalse(
                    (workspace.path / ".agents/skills/explorer-search").exists()
                )
                self.assertTrue(
                    (workspace.path / ".agents/skills/record-progress").is_dir()
                )
                call = runtime._call_spec(
                    call_id,
                    workspace=workspace,
                    policy=policy,
                    mode="main-sort",
                )
                self.assertIsNone(runtime._explorer_api_for_call(call))

                repository.stage_promotion(
                    "SORT-SNAPSHOT",
                    "OP-SNAPSHOT-EXPORT",
                    "memo",
                    [scratch_id],
                    "c" * 64,
                )
                repository.resolve_promotion(
                    "OP-SNAPSHOT-EXPORT",
                    "committed",
                    canonical_id="M-snapshot-export",
                )
                self.assertEqual(
                    runtime._make_workspace(
                        call_id=call_id,
                        policy=policy,
                        task_card=card,
                    ).path,
                    workspace.path,
                )

                catalog = snapshot_root / "catalog.jsonl"
                catalog.chmod(0o644)
                catalog.write_text("", encoding="utf-8")
                catalog.chmod(0o444)
                with self.assertRaisesRegex(MaterializationError, "content drifted"):
                    runtime._make_workspace(
                        call_id=call_id,
                        policy=policy,
                        task_card=card,
                    )
            finally:
                runtime.close()

    def test_ordinary_worker_recovery_never_ingests_main_sort_outbox(self) -> None:
        """A restart must not bypass the sorter's frozen Explorer source check."""

        with tempfile.TemporaryDirectory() as raw:
            runtime = _runtime(Path(raw))
            try:
                outside_id = "ES-outside-frozen-sort"
                _trust_direct_scratch(
                    runtime,
                    record_id=outside_id,
                    turn_id=OTHER_TURN,
                    operation_id="OP-OUTSIDE-FROZEN-SORT",
                )
                task_id, call_id = _begin_sort(
                    runtime, sort_run_id="SORT-RECOVERY-BOUNDARY"
                )
                lease_epoch, launch_attempt = runtime.scheduler.mark_call_running(
                    call_id
                )
                task = runtime.scheduler.state["tasks"][task_id]
                card = dict(task["task_card"])
                card["attempt"] = 1
                policy = policy_for("main-sort")
                workspace = runtime._make_workspace(
                    call_id=call_id,
                    policy=policy,
                    task_card=card,
                )
                call = AgentCall(
                    call_id=call_id,
                    kind="main-sort",
                    role="main-sort",
                    mode="main-sort",
                    payload={"task_card": card},
                    workspace=workspace,
                    policy=policy,
                    prompt="",
                    session_key="main:SORT-RECOVERY-BOUNDARY",
                    resume=False,
                    output_schema=runtime.layout.schemas / "main-sort.schema.json",
                    model_config=ModelConfig("gpt-6-astra", "ultra"),
                    lease_epoch=lease_epoch,
                    launch_attempt=launch_attempt,
                )
                progress = {
                    "operation_id": "RP-RECOVERY-OUTSIDE",
                    "progress_id": "PRG-RECOVERY-OUTSIDE",
                    "task_id": task_id,
                    "attempt": 1,
                    "sequence": 1,
                    "is_final": False,
                    "outcome_status": "progress",
                    "operations": [
                        {
                            "operation_id": "OP-RECOVERY-OUTSIDE",
                            "kind": "memo",
                            "proposal_id": "TMP-RECOVERY-OUTSIDE",
                            "abstract": "An out-of-scope provisional idea.",
                            "genre": "high-level",
                            "content": (
                                "This source belongs to another Explorer turn and "
                                "must not cross the recovery boundary."
                            ),
                            "related_route_ids": [],
                            "explorer_provenance": {
                                "sort_run_id": "SORT-RECOVERY-BOUNDARY",
                                "source_record_ids": [outside_id],
                            },
                        }
                    ],
                    "computation_operation_ids": [],
                    "explorer_computation_promotions": [],
                    "fact_challenges": [],
                    "completion_evidence_ids": [],
                }
                _trust_skill(runtime, call, "record-progress", progress)

                # Model a fully persisted RUNNING generation.  Dedicated sort
                # recovery must own this workspace regardless of which build
                # wrote the task-attempt envelope.
                with runtime.scheduler._mutate() as state:
                    attempt = state["tasks"][task_id]["attempts"][-1]
                    attempt["lease_epoch"] = lease_epoch
                    attempt["call_attempt"] = launch_attempt

                runtime._recover_staged_worker_progress()
                state = runtime.scheduler.state
                self.assertNotIn("PRG-RECOVERY-OUTSIDE", state["progress"])
                self.assertNotIn("OP-RECOVERY-OUTSIDE", state["operations"])
            finally:
                runtime.close()

    def test_running_main_sort_restart_resumes_through_dedicated_barrier(self) -> None:
        """Generic worker recovery must not move the one-shot sort task aside."""

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            runtime: FrantaRuntime | None = None
            source_id = "ES-sort-restart-source"
            sort_run_id = "SORT-RESTART-DEDICATED"

            def executor(call: AgentCall) -> dict[str, Any]:
                assert runtime is not None
                if call.kind == "main-sort":
                    card = dict(
                        json.loads(
                            call.workspace.task_card_path.read_text(encoding="utf-8")
                        )
                    )
                    progress = {
                        "operation_id": "RP-SORT-RESTART-FINAL",
                        "progress_id": "PRG-SORT-RESTART-FINAL",
                        "sequence": 1,
                        "is_final": True,
                        "outcome_status": "finished",
                        "progress_since_previous": (
                            "Recovered the dedicated sort call after restart."
                        ),
                        "operations": [
                            {
                                "operation_id": "OP-SORT-RESTART-MEMO",
                                "kind": "memo",
                                "proposal_id": "TMP-SORT-RESTART-MEMO",
                                "abstract": "A recovered Explorer idea.",
                                "genre": "high-level",
                                "content": (
                                    "The dedicated sort barrier validated and "
                                    "promoted this frozen source after restart."
                                ),
                                "related_route_ids": [],
                                "explorer_provenance": {
                                    "sort_run_id": card["sort_run_id"],
                                    "source_record_ids": [source_id],
                                },
                            }
                        ],
                        "computation_operation_ids": [],
                        "explorer_computation_promotions": [],
                        "fact_challenges": [],
                        "completion_evidence_ids": ["OP-SORT-RESTART-MEMO"],
                        "attempt_summary": {
                            "work_mode": "main-sort",
                            "task": card["objective"],
                            "proposed_outcome": "finished",
                            "cumulative_important_progress": (
                                "Promoted one frozen source after restart."
                            ),
                            "completion_evidence_operation_ids": [
                                "OP-SORT-RESTART-MEMO"
                            ],
                            "most_promising_next_steps": (
                                "Continue with ordinary Franta planning."
                            ),
                        },
                    }
                    staged = SkillRuntime(
                        SkillContext.load(call.workspace.path)
                    ).invoke("record-progress", progress)
                    runtime._record_broker_skill_result(
                        call=call,
                        skill="record-progress",
                        payload=progress,
                        result=staged,
                        capability_token="sort-restart-capability",
                    )
                    return {
                        "sort_ended": True,
                        "final_progress_id": "PRG-SORT-RESTART-FINAL",
                        "selected_explorer_record_ids": [source_id],
                        "deferred_computation_record_ids": [],
                    }
                if call.kind == "main":
                    batch_id = str(call.payload["reserved_batch_id"])
                    _trust_skill(
                        runtime,
                        call,
                        "task-writing",
                        {
                            "operation_id": "AR-SORT-RESTART-FOLLOWUP",
                            "batch_finalized": True,
                            "batch_id": batch_id,
                            "objective": "Investigate ROOT using the recovered Explorer idea.",
                            "work_mode": "brainstorm",
                            "if_resume": None,
                            "main_route_ids": [],
                            "main_obligation_ids": [
                                str(call.payload["root"]["obligation_id"])
                            ],
                            "selected_new_perspective": None,
                            "assignment_portfolio": {
                                kind: []
                                for kind in (
                                    "fact", "route", "memo", "claim",
                                    "obligation", "computation",
                                )
                            },
                            "reason": "Continue ordinary planning after dedicated sort recovery.",
                            "root_solution_fact_id": None,
                        },
                    )
                    return {
                        "decision": "assignments",
                        "batch_id": batch_id,
                        "assignment_report_ids": ["AR-SORT-RESTART-FOLLOWUP"],
                        "wait_for_task_ids": [],
                        "report": None,
                        "decline_proof_writer": False,
                    }
                raise AssertionError(f"unexpected deterministic call {call.kind}")

            runtime = FrantaRuntime.initialize(
                load_manifest(_manifest(root)), executor=executor
            )
            try:
                runtime.scheduler.commit_initial_trim({"category_ids": []})
                _trust_direct_scratch(
                    runtime,
                    record_id=source_id,
                    turn_id=TURN,
                    operation_id="OP-SORT-RESTART-SOURCE",
                )
                task_id, call_id = _begin_sort(
                    runtime, sort_run_id=sort_run_id
                )
                runtime.scheduler.mark_call_running(call_id)

                runtime.recover()
                state = runtime.scheduler.state
                self.assertEqual(
                    state["calls"][call_id]["status"],
                    CallState.RETRY_PENDING.value,
                )
                self.assertEqual(
                    state["tasks"][task_id]["state"], TaskState.RUNNING.value
                )

                runtime.start_services()
                self.assertTrue(runtime._run_franta_sort_barrier())
                self.assertEqual(runtime.scheduler.alternation_phase, "franta_run")
                self.assertEqual(
                    runtime.scheduler.state["tasks"][task_id]["state"],
                    TaskState.CLOSED.value,
                )
                state = runtime.scheduler.state
                planning_id = state["phase_control"]["sort"]["planning_call_id"]
                self.assertEqual(
                    state["calls"][planning_id]["status"], CallState.COMMITTED.value
                )
                followup_tasks = [
                    task for key, task in state["tasks"].items() if key != task_id
                ]
                self.assertEqual(len(followup_tasks), 1)
                self.assertEqual(followup_tasks[0]["state"], TaskState.LAUNCHING.value)
            finally:
                runtime.close()

    def test_runtime_accepts_standard_explorer_cas_receipt_contract(self) -> None:
        """CAS registration must not require scratch-only abstract/content."""

        with tempfile.TemporaryDirectory() as raw:
            runtime = _runtime(Path(raw))
            try:
                call = _explorer_call(
                    runtime,
                    "CALL-EXPLORER-CAS-RECEIPT",
                    turn_id=TURN,
                    worker_session_id="EWORK-cas-receipt",
                )
                payload = _cas_payload("CAS-RECEIPT", exit_status=0)
                staged = SkillRuntime(
                    SkillContext.load(call.workspace.path)
                ).invoke("CAS", payload)
                runtime._record_broker_skill_result(
                    call=call,
                    skill="CAS",
                    payload=payload,
                    result=staged,
                    capability_token="explorer-cas-capability",
                )
                repository = runtime.explorer_repository
                assert repository is not None
                evidence = repository.get_cas_evidence_by_operation(
                    "CAS-RECEIPT", require_success=True
                )
                self.assertIsNotNone(evidence)
            finally:
                runtime.close()

    def test_record_progress_provenance_is_exactly_main_sort_only(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            materializer = WorkspaceMaterializer(Path(raw) / "workspaces")
            provenance = {
                "sort_run_id": SORT_RUN,
                "source_record_ids": ["ES-source-1"],
            }
            operation = {
                "operation_id": "OP-MEMO-1",
                "kind": "memo",
                "explorer_provenance": provenance,
            }
            base = {
                "sequence": 1,
                "is_final": False,
                "outcome_status": "progress",
                "operations": [operation],
            }

            ordinary = materializer.create(
                "CALL-ORDINARY",
                root_problem="Prove ROOT.",
                policy=policy_for("worker", mode="research"),
                task_card={"task_id": "T-ordinary", "attempt": 1},
            )
            with self.assertRaisesRegex(
                SkillRuntimeError, "available only to main-sort"
            ):
                SkillRuntime(SkillContext.load(ordinary.path)).invoke(
                    "record-progress", base
                )

            sort = materializer.create(
                "CALL-SORT",
                root_problem="Prove ROOT.",
                policy=policy_for("main-sort"),
                task_card={
                    "task_id": "T-sort",
                    "attempt": 1,
                    "sort_run_id": SORT_RUN,
                },
            )
            sort_runtime = SkillRuntime(SkillContext.load(sort.path))
            without_provenance = {
                **base,
                "operations": [
                    {
                        key: value
                        for key, value in operation.items()
                        if key != "explorer_provenance"
                    }
                ],
            }
            with self.assertRaisesRegex(
                SkillRuntimeError, "requires exact Explorer provenance"
            ):
                sort_runtime.invoke("record-progress", without_provenance)

            wrong_run = {
                **base,
                "operations": [
                    {
                        **operation,
                        "explorer_provenance": {
                            **provenance,
                            "sort_run_id": "SORT-wrong",
                        },
                    }
                ],
            }
            with self.assertRaisesRegex(
                SkillRuntimeError, "outside its frozen source contract"
            ):
                sort_runtime.invoke("record-progress", wrong_run)

            staged = sort_runtime.invoke("record-progress", base)
            self.assertEqual(
                staged["artifact"]["operations"][0]["explorer_provenance"],
                provenance,
            )

    def test_scheduler_rejects_provenance_if_skill_boundary_is_bypassed(self) -> None:
        store = FakeControlStore()
        store.records["R-ordinary"] = {
            "id": "R-ordinary",
            "type": "route",
            "active": True,
            "status": "active",
        }
        scheduler = Scheduler(store)
        scheduler.bootstrap(root_problem="Prove ROOT.")
        scheduler.commit_initial_trim({"category_ids": []})
        task_id = scheduler.submit_batch("B-ordinary", [_ordinary_report()])[0]
        attempt = scheduler.start_task_attempt(task_id)
        statuses = scheduler.ingest_progress(
            {
                "progress_id": "PRG-FORGED-PROVENANCE",
                "task_id": task_id,
                "attempt": attempt,
                "sequence": 1,
                "is_final": False,
                "operations": [
                    {
                        "operation_id": "OP-FORGED-PROVENANCE",
                        "kind": "memo",
                        "abstract": "A forged Explorer-backed memo.",
                        "genre": "high-level",
                        "content": (
                            "An ordinary worker must not claim Explorer authority."
                        ),
                        "related_route_ids": [],
                        "explorer_provenance": {
                            "sort_run_id": SORT_RUN,
                            "source_record_ids": ["ES-forged"],
                        },
                    }
                ],
            }
        )
        self.assertEqual(
            statuses["OP-FORGED-PROVENANCE"], OperationState.REJECTED.value
        )
        operation_state = scheduler.state["operations"]["OP-FORGED-PROVENANCE"]
        self.assertIn("authorized only for the main-sort", operation_state["error"])
        self.assertFalse(
            any(record_id.startswith("M-") for record_id in store.records)
        )

    def test_main_sort_sources_are_exactly_the_frozen_turn_and_high_water(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            runtime = _runtime(Path(raw))
            try:
                repository = runtime.explorer_repository
                assert repository is not None

                def trust_scratch(
                    record_id: str, turn_id: str, operation_id: str
                ) -> None:
                    receipt = hashlib.sha256(record_id.encode()).hexdigest()
                    repository.prepare_scratch(
                        ExplorerWriteContext(
                            turn_id=turn_id,
                            worker_session_id=f"EWORK-{turn_id}",
                            attempt_no=1,
                            call_id=f"CALL-{record_id}",
                        ),
                        {
                            "skill": "record-scratch",
                            "operation_id": operation_id,
                            "record_id": record_id,
                            "record_kind": "idea",
                            "abstract": f"Source {record_id}",
                            "content": "A provisional idea for sorting.",
                            "related_memory_ids": [],
                            "cas_operation_ids": [],
                        },
                        receipt,
                    )
                    repository.trust_receipt(receipt)

                boundary_id = "ES-boundary"
                trust_scratch(boundary_id, TURN, "OP-BOUNDARY")
                frozen = repository.freeze_turn(TURN)
                late_id = "ES-late"
                wrong_turn_id = "ES-wrong-turn"
                trust_scratch(late_id, TURN, "OP-LATE")
                trust_scratch(wrong_turn_id, OTHER_TURN, "OP-WRONG-TURN")

                task_id = "T-sort-source-boundary"
                repository.create_sort_run(SORT_RUN, frozen, task_id)
                card = {
                    "task_id": task_id,
                    "sort_run_id": SORT_RUN,
                    "explorer_turn_id": TURN,
                    "source_high_water_seq": frozen.high_water_seq,
                }
                runtime.scheduler = types.SimpleNamespace(
                    state={
                        "tasks": {
                            task_id: {
                                "agent_system": "franta-sort",
                                "task_card": card,
                            }
                        }
                    }
                )

                def validate(
                    source_id: str,
                    operation_id: str,
                    *,
                    selected_ids: list[str] | None = None,
                ) -> None:
                    progress = {
                        "progress_id": f"PRG-{operation_id}",
                        "task_id": task_id,
                        "attempt": 1,
                        "sequence": 1,
                        "is_final": True,
                        "operations": [
                            {
                                "operation_id": operation_id,
                                "kind": "memo",
                                "explorer_provenance": {
                                    "sort_run_id": SORT_RUN,
                                    "source_record_ids": [source_id],
                                },
                            }
                        ],
                        "computation_operation_ids": [],
                    }
                    runtime._verified_skill_artifacts = (
                        lambda *_args, **_kwargs: [progress]
                    )
                    runtime._validated_main_sort_progress(
                        task_id,
                        "CALL-SORT-SOURCE-BOUNDARY",
                        1,
                        None,  # type: ignore[arg-type]
                        {
                            "final_progress_id": progress["progress_id"],
                            "selected_explorer_record_ids": (
                                [source_id]
                                if selected_ids is None
                                else selected_ids
                            ),
                            "deferred_computation_record_ids": [],
                        },
                        expected_lease_epoch=1,
                        expected_launch_attempt=1,
                    )

                validate(boundary_id, "OP-PROMOTE-BOUNDARY")
                accepted = repository.get_promotion("OP-PROMOTE-BOUNDARY")
                self.assertIsNotNone(accepted)
                assert accepted is not None
                self.assertEqual(accepted.source_record_ids, (boundary_id,))

                rejected_sources = (
                    (late_id, "OP-PROMOTE-LATE"),
                    (wrong_turn_id, "OP-PROMOTE-WRONG-TURN"),
                    ("ES-unknown", "OP-PROMOTE-UNKNOWN"),
                )
                for source_id, operation_id in rejected_sources:
                    with self.subTest(source_id=source_id):
                        with self.assertRaisesRegex(
                            InvalidAgentOutput, "outside the frozen turn"
                        ):
                            validate(source_id, operation_id)
                        self.assertIsNone(repository.get_promotion(operation_id))

                with self.assertRaisesRegex(
                    InvalidAgentOutput, "selected IDs must exactly match"
                ):
                    validate(
                        boundary_id,
                        "OP-PROMOTE-SELECTED-MISMATCH",
                        selected_ids=[],
                    )
                self.assertIsNone(
                    repository.get_promotion("OP-PROMOTE-SELECTED-MISMATCH")
                )
            finally:
                runtime.close()

    def test_trusted_xcas_promotes_across_calls_once_and_rejects_bad_evidence(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            runtime = _runtime(root)
            try:
                repository = runtime.explorer_repository
                assert repository is not None

                successful_evidence, successful_scratch = _trust_cas_and_scratch(
                    runtime,
                    call_id="CALL-EXPLORER-SUCCESS",
                    turn_id=TURN,
                    worker_session_id="EWORK-success",
                    cas_operation_id="CAS-SUCCESS",
                    exit_status=0,
                )
                failed_evidence, failed_scratch = _trust_cas_and_scratch(
                    runtime,
                    call_id="CALL-EXPLORER-FAILED",
                    turn_id=TURN,
                    worker_session_id="EWORK-failed",
                    cas_operation_id="CAS-FAILED",
                    exit_status=1,
                )
                wrong_turn_evidence, _ = _trust_cas_and_scratch(
                    runtime,
                    call_id="CALL-EXPLORER-WRONG-TURN",
                    turn_id=OTHER_TURN,
                    worker_session_id="EWORK-wrong-turn",
                    cas_operation_id="CAS-WRONG-TURN",
                    exit_status=0,
                )

                untrusted = repository.prepare_cas_evidence(
                    ExplorerWriteContext(
                        turn_id=TURN,
                        worker_session_id="EWORK-untrusted",
                        attempt_no=1,
                        call_id="CALL-EXPLORER-UNTRUSTED",
                    ),
                    {
                        "skill": "CAS",
                        **_cas_payload("CAS-UNTRUSTED", exit_status=0),
                    },
                    hashlib.sha256(b"untrusted receipt").hexdigest(),
                )
                self.assertIsNone(repository.get_cas_evidence(untrusted.evidence_id))

                frozen = repository.freeze_turn(TURN)
                deadline = datetime.now(timezone.utc) + timedelta(days=1)
                runtime.scheduler.activate_alternation()
                self.assertEqual(
                    runtime.scheduler.tick_alternation(now=deadline),
                    "explorer_drain",
                )
                snapshot = build_main_sort_explorer_snapshot_for_frozen_turn(
                    repository,
                    sort_run_id=SORT_RUN,
                    frozen_turn=frozen,
                )
                task_id = runtime.scheduler.prepare_main_sort_task(
                    sort_run_id=SORT_RUN,
                    turn_id=TURN,
                    source_high_water_seq=frozen.high_water_seq,
                    source_set_digest=frozen.source_set_digest,
                    snapshot_format_version=1,
                    snapshot_digest=explorer_snapshot_digest(snapshot),
                )
                repository.create_sort_run(SORT_RUN, frozen, task_id)
                sort_call_id = runtime.scheduler.begin_main_sort_task_attempt(
                    task_id,
                    sort_run_id=SORT_RUN,
                    session_key="main:sort-promotion",
                    now=deadline + timedelta(seconds=1),
                )
                lease_epoch, launch_attempt = runtime.scheduler.mark_call_running(
                    sort_call_id
                )
                card = dict(runtime.scheduler.state["tasks"][task_id]["task_card"])
                card["attempt"] = 1
                policy = policy_for("main-sort")
                workspace = runtime._make_workspace(
                    call_id=sort_call_id,
                    policy=policy,
                    task_card=card,
                )
                call = AgentCall(
                    call_id=sort_call_id,
                    kind="main-sort",
                    role="main-sort",
                    mode="main-sort",
                    payload={"task_card": card},
                    workspace=workspace,
                    policy=policy,
                    prompt="",
                    session_key="main:sort-promotion",
                    resume=False,
                    output_schema=runtime.layout.schemas / "main-sort.schema.json",
                    model_config=ModelConfig("gpt-6-astra", "ultra"),
                    lease_epoch=lease_epoch,
                    launch_attempt=launch_attempt,
                )

                rejected_cases = (
                    ("wrong turn", wrong_turn_evidence, successful_scratch),
                    ("failed", failed_evidence, failed_scratch),
                    ("untrusted", untrusted.evidence_id, successful_scratch),
                )
                for label, evidence_id, source_id in rejected_cases:
                    with self.subTest(label=label):
                        with self.assertRaisesRegex(
                            ExplorerNotFoundError,
                            "successful trusted CAS evidence is unavailable",
                        ):
                            runtime._main_sort_progress_artifacts(
                                workspace,
                                task_id=task_id,
                                sort_run_id=SORT_RUN,
                                verified_progress=[
                                    _progress_with_promotion(
                                        task_id,
                                        evidence_id,
                                        source_id,
                                        final=False,
                                    )
                                ],
                            )

                progress_payload = _progress_with_promotion(
                    task_id,
                    successful_evidence,
                    successful_scratch,
                    final=True,
                )
                staged_progress = SkillRuntime(
                    SkillContext.load(workspace.path)
                ).invoke("record-progress", progress_payload)
                runtime._record_broker_skill_result(
                    call=call,
                    skill="record-progress",
                    payload=progress_payload,
                    result=staged_progress,
                    capability_token="main-sort-capability",
                )
                response = {
                    "sort_ended": True,
                    "final_progress_id": staged_progress["artifact"]["progress_id"],
                    "selected_explorer_record_ids": [successful_scratch],
                    "deferred_computation_record_ids": [],
                }

                before = Counter(
                    record.memory_type for record in runtime.store.list_records()
                )
                self.assertTrue(
                    runtime._ingest_main_sort_outbox(
                        task_id,
                        sort_call_id,
                        workspace,
                        response,
                        expected_lease_epoch=lease_epoch,
                        expected_launch_attempt=launch_attempt,
                    )
                )
                self.assertTrue(
                    runtime._reconcile_explorer_promotions(sort_run_id=SORT_RUN)
                )

                computations = runtime.store.list_records(
                    types={MemoryType.COMPUTATION}
                )
                self.assertEqual(len(computations), 1)
                canonical_id = computations[0].memory_id
                self.assertRegex(canonical_id, r"^C-")
                computation = runtime.store.get(canonical_id)
                self.assertEqual(computation["output"], "4\n")
                self.assertEqual(computation["task_id"], task_id)
                self.assertNotIn("explorer_provenance", computation)

                staging_id = (
                    f"explorer-cas:{SORT_RUN}:{successful_evidence}"
                )
                scheduler_computation = runtime.scheduler.state["computations"][
                    staging_id
                ]
                self.assertEqual(
                    scheduler_computation["state"], OperationState.COMMITTED.value
                )
                self.assertEqual(scheduler_computation["canonical_id"], canonical_id)
                promotion = repository.get_promotion(staging_id)
                self.assertIsNotNone(promotion)
                assert promotion is not None
                self.assertEqual(promotion.state, "committed")
                self.assertEqual(promotion.canonical_id, canonical_id)

                self.assertFalse(
                    runtime._ingest_main_sort_outbox(
                        task_id,
                        sort_call_id,
                        workspace,
                        response,
                        expected_lease_epoch=lease_epoch,
                        expected_launch_attempt=launch_attempt,
                    )
                )
                runtime._reconcile_explorer_promotions(sort_run_id=SORT_RUN)
                self.assertEqual(
                    len(
                        runtime.store.list_records(
                            types={MemoryType.COMPUTATION}
                        )
                    ),
                    1,
                )

                after = Counter(
                    record.memory_type for record in runtime.store.list_records()
                )
                self.assertEqual(
                    set(after),
                    set(before) | {MemoryType.TASK, MemoryType.COMPUTATION},
                )
                self.assertEqual(
                    after[MemoryType.COMPUTATION],
                    before[MemoryType.COMPUTATION] + 1,
                )
                self.assertEqual(
                    after[MemoryType.TASK], before[MemoryType.TASK] + 1
                )
                for memory_type in MemoryType:
                    if memory_type not in {MemoryType.TASK, MemoryType.COMPUTATION}:
                        self.assertEqual(after[memory_type], before[memory_type])
                self.assertFalse(
                    any(
                        record.memory_id.startswith(("ES-", "ESUM-", "XCAS-", "XP-"))
                        for record in runtime.store.list_records()
                    )
                )
                self.assertEqual(
                    set(MemoryType),
                    {
                        MemoryType.FACT,
                        MemoryType.ROUTE,
                        MemoryType.MEMO,
                        MemoryType.CLAIM,
                        MemoryType.OBLIGATION,
                        MemoryType.TASK,
                        MemoryType.COMPUTATION,
                    },
                )
            finally:
                runtime.close()


if __name__ == "__main__":
    unittest.main()
