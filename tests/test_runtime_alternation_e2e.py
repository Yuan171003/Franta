from __future__ import annotations

import json
import tempfile
import threading
import unittest
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

from franta.config import load_manifest
from franta.explorer_adapter import (
    build_main_sort_explorer_snapshot_for_frozen_turn,
)
from franta.materialize import explorer_snapshot_digest
from franta.runtime import AgentCall, FrantaRuntime
from franta.skill_runtime import SkillContext, SkillRuntime


def _write_manifest(root: Path) -> Path:
    manifest = root / "bootstrap.toml"
    manifest.write_text(
        """
[project]
name = "runtime-alternation-e2e"
directory = "project"
root_problem = "Prove or disprove the deterministic ROOT statement."
foundation_policy = "Use only the declared test axioms."

[explorer]
max_workers = 1
attempts_per_worker = 3
attempt_seconds = 30
explorer_admission_seconds = 1
franta_admission_seconds = 30

[initial]
""".strip()
        + "\n",
        encoding="utf-8",
    )
    return manifest


def _write_two_worker_manifest(root: Path) -> Path:
    manifest = root / "bootstrap-two-workers.toml"
    manifest.write_text(
        """
[project]
name = "runtime-alternation-failed-attempt"
directory = "p"
root_problem = "Prove or disprove the deterministic ROOT statement."
foundation_policy = "Use only the declared test axioms."

[limits]
max_non_verifier_workers = 2

[explorer]
max_workers = 2
attempts_per_worker = 3
attempt_seconds = 30
explorer_admission_seconds = 30
franta_admission_seconds = 30

[retries]
worker_interruptions = 1

[initial]
""".strip()
        + "\n",
        encoding="utf-8",
    )
    return manifest


def _write_legacy_manifest(root: Path) -> Path:
    manifest = root / "bootstrap-legacy.toml"
    manifest.write_text(
        """
[project]
name = "runtime-legacy-control-recovery"
directory = "legacy"
root_problem = "Prove or disprove the deterministic ROOT statement."
foundation_policy = "Use only the declared test axioms."

[initial]
""".strip()
        + "\n",
        encoding="utf-8",
    )
    return manifest


def _enter_test_franta_run(runtime: FrantaRuntime) -> None:
    repository = runtime.explorer_repository
    assert repository is not None
    runtime.scheduler.activate_alternation()
    phase = runtime.scheduler.state["phase_control"]
    explorer_deadline = datetime.fromisoformat(
        phase["explorer"]["admission_deadline"]
    )
    runtime.scheduler.tick_alternation(now=explorer_deadline)
    turn_id = f"XTURN-{int(phase['cycle']):08d}"
    frozen = repository.freeze_turn(turn_id)
    sort_run_id = "SORT-CONTROL-DEADLINE"
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
    sort_call_id = runtime.scheduler.begin_main_sort_task_attempt(
        task_id,
        sort_run_id=sort_run_id,
        session_key="main:control-deadline",
        now=explorer_deadline,
    )
    runtime.scheduler.open_franta_run(
        sort_call_id=sort_call_id,
        planning_call_id="CALL-CONTROL-DEADLINE-SEED",
        session_key="main:control-deadline",
        now=explorer_deadline,
    )


def _open_test_franta_run(runtime: FrantaRuntime) -> tuple[str, str]:
    _enter_test_franta_run(runtime)
    main_call_id = runtime.scheduler.prepare_call(
        "main",
        {"event_cursor": 0, "portfolio_revision": 0},
        continuation={"reserved_batch_id": "BATCH-CONTROL-DEADLINE"},
    )
    trimmer_call_id = runtime.scheduler.prepare_call(
        "trimmer",
        {"phase": "review"},
        continuation={"phase": "review", "session_id": "deadline"},
    )
    return main_call_id, trimmer_call_id


def _brainstorm_assignment(runtime: FrantaRuntime, marker: str) -> dict[str, Any]:
    return {
        "report_id": f"AR-{marker}",
        "objective": f"Independently investigate ROOT for {marker}.",
        "if_resume": None,
        "mode": "brainstorm",
        "main_route_ids": [],
        "main_obligation_ids": [
            str(runtime.scheduler.state["root"]["obligation_id"])
        ],
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
        "reason": "Exercise the worker admission boundary.",
    }


class RuntimeAlternationEndToEndTests(unittest.TestCase):
    def test_accepted_worker_is_not_launched_after_franta_deadline(self) -> None:
        """Only processes already started at the boundary belong to the drain."""

        with tempfile.TemporaryDirectory() as raw:
            runtime = FrantaRuntime.initialize(
                load_manifest(_write_two_worker_manifest(Path(raw)))
            )
            try:
                runtime.scheduler.commit_initial_trim({"category_ids": []})
                _enter_test_franta_run(runtime)
                task_id = runtime.scheduler.submit_batch(
                    "BATCH-PENDING-AT-DEADLINE",
                    [_brainstorm_assignment(runtime, "PENDING-DEADLINE")],
                )[0]
                deadline = datetime.fromisoformat(
                    runtime.scheduler.state["phase_control"]["franta"][
                        "admission_deadline"
                    ]
                )
                original_clock = runtime.scheduler._reducer_now
                runtime.scheduler._reducer_now = (  # type: ignore[method-assign]
                    lambda now=None: deadline if now is None else original_clock(now)
                )
                prepared: list[str] = []

                def forbidden_prepare(selected_task_id: str) -> Any:
                    prepared.append(selected_task_id)
                    raise AssertionError("a post-deadline worker launch was prepared")

                runtime._prepare_worker_workspace = (  # type: ignore[method-assign]
                    forbidden_prepare
                )

                self.assertFalse(runtime._run_worker_batch([task_id]))

                state = runtime.scheduler.state
                self.assertEqual(prepared, [])
                self.assertEqual(state["phase_control"]["phase"], "franta_drain")
                self.assertEqual(state["tasks"][task_id]["state"], "launching")
                self.assertEqual(state["tasks"][task_id]["attempts"], [])

                self.assertEqual(
                    runtime.scheduler.stop_unlaunched_franta_tasks_for_drain(),
                    (task_id,),
                )
                stopped = runtime.scheduler.state["tasks"][task_id]
                self.assertEqual(stopped["state"], "closed")
                self.assertEqual(stopped["final_status"], "interrupted")
                self.assertEqual(
                    stopped["final_summary"]["kind"], "alternation_drain"
                )
                self.assertFalse(stopped["slot_reserved"])
                self.assertTrue(runtime._franta_tail_is_drained())

                runtime.scheduler.complete_franta_drain(now=deadline)
                next_phase = runtime.scheduler.state["phase_control"]
                self.assertEqual(next_phase["phase"], "explorer_admission")
                self.assertEqual(next_phase["cycle"], 2)

                next_explorer_deadline = datetime.fromisoformat(
                    next_phase["explorer"]["admission_deadline"]
                )
                runtime.scheduler.tick_alternation(now=next_explorer_deadline)
                repository = runtime.explorer_repository
                assert repository is not None
                frozen = repository.freeze_turn("XTURN-00000002")
                sort_run_id = "SORT-PENDING-DEADLINE-NEXT-CYCLE"
                snapshot = build_main_sort_explorer_snapshot_for_frozen_turn(
                    repository,
                    sort_run_id=sort_run_id,
                    frozen_turn=frozen,
                )
                sort_task_id = runtime.scheduler.prepare_main_sort_task(
                    sort_run_id=sort_run_id,
                    turn_id=frozen.turn_id,
                    source_high_water_seq=frozen.high_water_seq,
                    source_set_digest=frozen.source_set_digest,
                    snapshot_format_version=1,
                    snapshot_digest=explorer_snapshot_digest(snapshot),
                )
                repository.create_sort_run(sort_run_id, frozen, sort_task_id)
                sort_call_id = runtime.scheduler.begin_main_sort_task_attempt(
                    sort_task_id,
                    sort_run_id=sort_run_id,
                    session_key="main:pending-deadline-next-cycle",
                    now=next_explorer_deadline,
                )
                runtime.scheduler.open_franta_run(
                    sort_call_id=sort_call_id,
                    planning_call_id="CALL-PENDING-DEADLINE-NEXT-CYCLE",
                    session_key="main:pending-deadline-next-cycle",
                    now=next_explorer_deadline,
                )

                self.assertEqual(
                    runtime.scheduler.state["tasks"][task_id]["state"], "closed"
                )
                self.assertNotIn(task_id, runtime._ordinary_worker_launches())
            finally:
                runtime.close()

    def test_legacy_worker_launch_path_has_no_alternation_fence(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            runtime = FrantaRuntime.initialize(
                load_manifest(_write_legacy_manifest(Path(raw)))
            )
            try:
                runtime.scheduler.commit_initial_trim({"category_ids": []})
                task_id = runtime.scheduler.submit_batch(
                    "BATCH-LEGACY-LAUNCH",
                    [_brainstorm_assignment(runtime, "LEGACY-LAUNCH")],
                )[0]
                prepared: list[str] = []

                def observe_prepare(selected_task_id: str) -> Any:
                    prepared.append(selected_task_id)
                    raise RuntimeError("stop after observing legacy launch admission")

                runtime._prepare_worker_workspace = (  # type: ignore[method-assign]
                    observe_prepare
                )

                self.assertTrue(runtime._run_worker_batch([task_id]))
                self.assertEqual(prepared, [task_id])
                self.assertIsNone(runtime.scheduler.alternation_phase)
            finally:
                runtime.close()

    def test_main_result_returning_at_franta_deadline_cannot_submit_tasks(
        self,
    ) -> None:
        """A result accepted after the deadline is fenced before its commit tail."""

        with tempfile.TemporaryDirectory() as raw:
            runtime: FrantaRuntime | None = None

            def executor(call: AgentCall) -> dict[str, Any]:
                assert runtime is not None
                if call.kind != "main":
                    raise AssertionError(f"unexpected deterministic call {call.kind}")
                batch_id = str(call.payload["reserved_batch_id"])
                root_id = str(call.payload["root"]["obligation_id"])
                artifact = SkillRuntime(
                    SkillContext.load(call.workspace.path)
                ).invoke(
                    "task-writing",
                    {
                        "operation_id": "AR-POST-DEADLINE",
                        "batch_finalized": True,
                        "batch_id": batch_id,
                        "objective": "This assignment must be fenced at the deadline.",
                        "work_mode": "brainstorm",
                        "if_resume": None,
                        "main_route_ids": [],
                        "main_obligation_ids": [root_id],
                        "selected_new_perspective": None,
                        "assignment_portfolio": {
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
                        "reason": "Exercise the synchronous post-return boundary.",
                        "root_solution_fact_id": None,
                    },
                )["artifact"]
                deadline = datetime.fromisoformat(
                    runtime.scheduler.state["phase_control"]["franta"][
                        "admission_deadline"
                    ]
                )
                original_clock = runtime.scheduler._reducer_now
                runtime.scheduler._reducer_now = (  # type: ignore[method-assign]
                    lambda now=None: deadline if now is None else original_clock(now)
                )
                return {
                    "decision": "assignments",
                    "batch_id": batch_id,
                    "assignment_report_ids": [artifact["operation_id"]],
                    "wait_for_task_ids": [],
                    "report": None,
                    "decline_proof_writer": False,
                }

            runtime = FrantaRuntime.initialize(
                load_manifest(_write_two_worker_manifest(Path(raw))),
                executor=executor,
            )
            try:
                runtime.start_services()
                runtime.scheduler.commit_initial_trim({"category_ids": []})
                _enter_test_franta_run(runtime)
                tasks_before = set(runtime.scheduler.state["tasks"])

                self.assertTrue(runtime._run_main())

                state = runtime.scheduler.state
                self.assertEqual(state["phase_control"]["phase"], "franta_drain")
                self.assertEqual(set(state["tasks"]), tasks_before)
                main_calls = [
                    call
                    for call in state["calls"].values()
                    if call.get("kind") == "main"
                ]
                self.assertEqual(len(main_calls), 1)
                self.assertEqual(main_calls[0]["status"], "cancelled")
                self.assertEqual(
                    main_calls[0].get("cancellation", {}).get("authorized_by"),
                    "alternation-controller",
                )
            finally:
                runtime.close()

    def test_franta_deadline_fences_pending_control_calls_until_after_explorer(
        self,
    ) -> None:
        """Pending main/trimmer work cannot cross the Franta admission boundary."""

        with tempfile.TemporaryDirectory() as raw:
            runtime = FrantaRuntime.initialize(
                load_manifest(_write_two_worker_manifest(Path(raw))),
                executor=lambda _call: (_ for _ in ()).throw(
                    AssertionError("a fenced control call was invoked")
                ),
            )
            try:
                main_call_id, trimmer_call_id = _open_test_franta_run(runtime)
                deadline = datetime.fromisoformat(
                    runtime.scheduler.state["phase_control"]["franta"][
                        "admission_deadline"
                    ]
                )
                self.assertEqual(
                    runtime.scheduler.tick_alternation(now=deadline),
                    "franta_drain",
                )
                state = runtime.scheduler.state
                for call_id in (main_call_id, trimmer_call_id):
                    with self.subTest(call_id=call_id):
                        self.assertEqual(
                            state["calls"][call_id]["status"], "cancelled"
                        )
                        cancellation = state["calls"][call_id].get("cancellation", {})
                        self.assertEqual(
                            cancellation.get("authorized_by"),
                            "alternation-controller",
                        )

                invoked: list[str] = []
                runtime._run_prepared_call = (  # type: ignore[method-assign]
                    lambda call_id, **_kwargs: invoked.append(call_id) or {}
                )
                runtime._commit_main_result = (  # type: ignore[method-assign]
                    lambda *_args, **_kwargs: None
                )
                runtime._commit_trimmer_result = (  # type: ignore[method-assign]
                    lambda *_args, **_kwargs: None
                )
                self.assertFalse(runtime._recover_control_calls())
                self.assertEqual(invoked, [])

                runtime.scheduler.complete_franta_drain(now=deadline)
                self.assertEqual(
                    runtime.scheduler.alternation_phase, "explorer_admission"
                )
                self.assertFalse(runtime._recover_control_calls())
                self.assertEqual(invoked, [])
            finally:
                runtime.close()

    def test_legacy_control_call_recovery_remains_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            runtime = FrantaRuntime.initialize(
                load_manifest(_write_legacy_manifest(Path(raw)))
            )
            try:
                main_call_id = runtime.scheduler.prepare_call(
                    "main",
                    {"event_cursor": 0, "portfolio_revision": 0},
                    continuation={"reserved_batch_id": "BATCH-LEGACY-RECOVERY"},
                )
                trimmer_call_id = runtime.scheduler.prepare_call(
                    "trimmer",
                    {"phase": "review"},
                    continuation={"phase": "review", "session_id": "legacy"},
                )
                invoked: list[str] = []
                runtime._run_prepared_call = (  # type: ignore[method-assign]
                    lambda call_id, **_kwargs: invoked.append(call_id) or {}
                )
                runtime._commit_main_result = (  # type: ignore[method-assign]
                    lambda *_args, **_kwargs: None
                )
                runtime._commit_trimmer_result = (  # type: ignore[method-assign]
                    lambda *_args, **_kwargs: None
                )
                self.assertTrue(runtime._recover_control_calls())
                self.assertCountEqual(invoked, [main_call_id, trimmer_call_id])
                self.assertIsNone(runtime.scheduler.alternation_phase)
            finally:
                runtime.close()

    def test_failed_planned_attempt_advances_its_lineage_without_killing_peer(
        self,
    ) -> None:
        """One exhausted call must not leak a running lineage or abort its peers."""

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            runtime: FrantaRuntime | None = None

            def trusted_stage(
                call: AgentCall,
                skill: str,
                payload: Mapping[str, Any],
            ) -> dict[str, Any]:
                assert runtime is not None
                staged = SkillRuntime(SkillContext.load(call.workspace.path)).invoke(
                    skill, dict(payload)
                )
                return runtime._record_broker_skill_result(
                    call=call,
                    skill=skill,
                    payload=dict(payload),
                    result=staged,
                    capability_token=(
                        f"failed-attempt-broker:{call.call_id}:"
                        f"{skill}:{payload.get('operation_id')}"
                    ),
                )

            def executor(call: AgentCall) -> dict[str, Any]:
                if call.kind != "explorer-worker":
                    raise AssertionError(f"unexpected deterministic call {call.kind}")
                lineage_id = str(call.payload["worker_session_id"])
                if lineage_id.endswith("00000001"):
                    raise RuntimeError("deterministic exhausted Explorer attempt")

                suffix = lineage_id.rsplit("-", 1)[-1]
                progress = trusted_stage(
                    call,
                    "record-scratch",
                    {
                        "operation_id": f"OP-PROGRESS-{suffix}",
                        "record_kind": "progress",
                        "abstract": "The surviving peer records useful progress.",
                        "content": (
                            "The peer completed an independent attack and identified "
                            "a concrete reduction to refine. ROOT remains open."
                        ),
                        "related_memory_ids": [],
                        "cas_operation_ids": [],
                    },
                )
                summary = trusted_stage(
                    call,
                    "record-summary",
                    {
                        "operation_id": f"OP-SUMMARY-{suffix}",
                        "abstract": "The surviving peer completed its first attempt.",
                        "content": "The peer produced one provisional line of progress.",
                        "directions_tried": ["Independent surviving peer direction"],
                        "main_progress": "One peer attempt completed normally.",
                        "main_obstacles": "ROOT remains unproved.",
                        "source_scratch_ids": [str(progress["record_id"])],
                    },
                )
                return {
                    "attempt_ended": True,
                    "final_summary_id": str(summary["record_id"]),
                    "root_candidate_scratch_id": None,
                    "root_candidate_outcome": None,
                    "stop_reason": "attempt_complete",
                }

            runtime = FrantaRuntime.initialize(
                load_manifest(_write_two_worker_manifest(root)), executor=executor
            )
            try:
                runtime.start_services()
                runtime.scheduler.activate_alternation()
                self.assertTrue(runtime._run_explorer_wave())
                control = runtime.scheduler.state["explorer_control"]
                self.assertEqual(len(control["lineages"]), 2)
                failed = control["lineages"]["XLINEAGE-00000001"]
                peer = control["lineages"]["XLINEAGE-00000002"]
                self.assertEqual(failed["attempts_started"], 1)
                self.assertEqual(failed["attempts"][-1]["outcome"], "failed")
                self.assertEqual(failed["status"], "continuation_pending")
                self.assertEqual(peer["attempts_started"], 1)
                self.assertEqual(peer["attempts"][-1]["outcome"], "progress")
                self.assertEqual(peer["status"], "continuation_pending")
            finally:
                runtime.close()

    def test_overdue_recovered_attempt_expires_before_any_relaunch(self) -> None:
        """A retry whose planned-attempt clock elapsed while down never executes."""

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            runtime: FrantaRuntime | None = None
            invoked_attempts: list[int] = []
            expiration_ran = threading.Event()

            def trusted_stage(
                call: AgentCall,
                skill: str,
                payload: Mapping[str, Any],
            ) -> dict[str, Any]:
                assert runtime is not None
                staged = SkillRuntime(
                    SkillContext.load(call.workspace.path)
                ).invoke(skill, dict(payload))
                return runtime._record_broker_skill_result(
                    call=call,
                    skill=skill,
                    payload=dict(payload),
                    result=staged,
                    capability_token=(
                        f"overdue-recovery:{call.call_id}:"
                        f"{skill}:{payload.get('operation_id')}"
                    ),
                )

            def executor(call: AgentCall) -> dict[str, Any]:
                if call.kind != "explorer-worker":
                    raise AssertionError(f"unexpected deterministic call {call.kind}")
                attempt = int(call.payload["attempt_number"])
                invoked_attempts.append(attempt)
                if attempt == 1:
                    # In the broken ordering the stale retry reaches the pool
                    # before expiration. Let the main thread perform that
                    # expiration so the test cannot hang on executor timing.
                    if not expiration_ran.wait(timeout=5):
                        raise AssertionError("overdue expiration never ran")
                    raise RuntimeError("an overdue recovered attempt was relaunched")

                progress = trusted_stage(
                    call,
                    "record-scratch",
                    {
                        "operation_id": "OP-OVERDUE-PROGRESS-2",
                        "record_kind": "progress",
                        "abstract": "The continuation starts only after expiration.",
                        "content": (
                            "The next planned attempt started after the expired attempt "
                            "and tried a fresh reduction; ROOT remains open."
                        ),
                        "related_memory_ids": [],
                        "cas_operation_ids": [],
                    },
                )
                summary = trusted_stage(
                    call,
                    "record-summary",
                    {
                        "operation_id": "OP-OVERDUE-SUMMARY-2",
                        "abstract": "The post-expiration continuation ran normally.",
                        "content": "No stale attempt code was executed after recovery.",
                        "directions_tried": ["Fresh post-expiration reduction"],
                        "main_progress": "The second planned attempt started cleanly.",
                        "main_obstacles": "ROOT remains open.",
                        "source_scratch_ids": [str(progress["record_id"])],
                    },
                )
                return {
                    "attempt_ended": True,
                    "final_summary_id": str(summary["record_id"]),
                    "root_candidate_scratch_id": None,
                    "root_candidate_outcome": None,
                    "stop_reason": "attempt_complete",
                }

            runtime = FrantaRuntime.initialize(
                load_manifest(_write_manifest(root)), executor=executor
            )
            try:
                runtime.start_services()
                runtime.scheduler.activate_alternation()
                phase = runtime.scheduler.state["phase_control"]
                started = datetime.fromisoformat(
                    phase["explorer"]["admission_started_at"]
                )
                lineage_id = runtime.scheduler.admit_explorer_lineage(now=started)
                prepared = runtime.scheduler.start_explorer_attempt(
                    lineage_id,
                    source_high_water_seq=0,
                    now=started,
                )
                stale_call_id = str(prepared["call_id"])
                runtime.scheduler.mark_call_running(stale_call_id)
                runtime.scheduler.recover()
                self.assertEqual(
                    runtime.scheduler.state["calls"][stale_call_id]["status"],
                    "retry_pending",
                )
                deadline = datetime.fromisoformat(
                    runtime.scheduler.state["explorer_control"]["lineages"][
                        lineage_id
                    ]["attempts"][-1]["deadline"]
                )
                original_clock = runtime.scheduler._reducer_now
                runtime.scheduler._reducer_now = (  # type: ignore[method-assign]
                    lambda now=None: deadline if now is None else original_clock(now)
                )
                original_expire = runtime.scheduler.expire_explorer_attempts

                def expire_then_release(*, now: datetime | None = None) -> tuple[str, ...]:
                    expired = original_expire(now=now)
                    expiration_ran.set()
                    return expired

                runtime.scheduler.expire_explorer_attempts = (  # type: ignore[method-assign]
                    expire_then_release
                )

                self.assertTrue(runtime._run_explorer_wave())

                self.assertEqual(invoked_attempts, [2])
                state = runtime.scheduler.state
                self.assertEqual(state["calls"][stale_call_id]["status"], "cancelled")
                lineage = state["explorer_control"]["lineages"][lineage_id]
                self.assertEqual(lineage["attempts_started"], 2)
                self.assertEqual(
                    [item["outcome"] for item in lineage["attempts"]],
                    ["timed_out", "progress"],
                )
                self.assertEqual(lineage["status"], "continuation_pending")
            finally:
                expiration_ran.set()
                runtime.close()

    def test_simultaneous_root_candidates_persist_only_one_and_stop_both_lineages(
        self,
    ) -> None:
        """A completed peer is cancelled and acknowledged after the first claim."""

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            runtime: FrantaRuntime | None = None

            def trusted_stage(
                call: AgentCall,
                skill: str,
                payload: Mapping[str, Any],
            ) -> dict[str, Any]:
                assert runtime is not None
                staged = SkillRuntime(
                    SkillContext.load(call.workspace.path)
                ).invoke(skill, dict(payload))
                return runtime._record_broker_skill_result(
                    call=call,
                    skill=skill,
                    payload=dict(payload),
                    result=staged,
                    capability_token=(
                        f"simultaneous-candidate:{call.call_id}:"
                        f"{skill}:{payload.get('operation_id')}"
                    ),
                )

            def executor(call: AgentCall) -> dict[str, Any]:
                if call.kind != "explorer-worker":
                    raise AssertionError(f"unexpected deterministic call {call.kind}")
                lineage_id = str(call.payload["worker_session_id"])
                suffix = lineage_id.rsplit("-", 1)[-1]
                proof = trusted_stage(
                    call,
                    "record-scratch",
                    {
                        "operation_id": f"OP-PROOF-{suffix}",
                        "record_kind": "proof",
                        "abstract": f"Lineage {suffix} claims a ROOT proof.",
                        "content": (
                            f"Provisional complete proof from lineage {suffix}; "
                            "Franta has not verified this argument."
                        ),
                        "related_memory_ids": [],
                        "cas_operation_ids": [],
                    },
                )
                summary = trusted_stage(
                    call,
                    "record-summary",
                    {
                        "operation_id": f"OP-SUMMARY-CANDIDATE-{suffix}",
                        "abstract": f"Lineage {suffix} produced a ROOT candidate.",
                        "content": (
                            "The attempt ended with an unverified proof candidate for "
                            "later Franta checking."
                        ),
                        "directions_tried": [f"Direct proof route {suffix}"],
                        "main_progress": "A self-contained provisional proof was recorded.",
                        "main_obstacles": "The proof has not been checked by Franta.",
                        "source_scratch_ids": [str(proof["record_id"])],
                    },
                )
                return {
                    "attempt_ended": True,
                    "final_summary_id": str(summary["record_id"]),
                    "root_candidate_scratch_id": str(proof["record_id"]),
                    "root_candidate_outcome": "proved",
                    "stop_reason": "root_candidate",
                }

            runtime = FrantaRuntime.initialize(
                load_manifest(_write_two_worker_manifest(root)), executor=executor
            )
            try:
                runtime.start_services()
                runtime.scheduler.activate_alternation()
                original_run = runtime._run_explorer_attempt_call
                both_accepted = threading.Barrier(2)

                def synchronized_run(*args: Any, **kwargs: Any) -> Mapping[str, Any]:
                    result = original_run(*args, **kwargs)
                    both_accepted.wait(timeout=5)
                    return result

                runtime._run_explorer_attempt_call = (  # type: ignore[method-assign]
                    synchronized_run
                )
                self.assertTrue(runtime._run_explorer_wave())

                state = runtime.scheduler.state
                self.assertEqual(state["phase_control"]["phase"], "explorer_drain")
                explorer_candidate = state["explorer_control"]["root_candidate"]
                phase_candidate = state["phase_control"]["explorer"][
                    "root_candidate"
                ]
                self.assertIsNotNone(explorer_candidate)
                for key in (
                    "candidate_id",
                    "scratch_id",
                    "lineage_id",
                    "attempt_number",
                    "candidate_outcome",
                    "status",
                ):
                    self.assertEqual(explorer_candidate[key], phase_candidate[key])
                self.assertEqual(explorer_candidate["candidate_outcome"], "proved")

                explorer_calls = [
                    call
                    for call in state["calls"].values()
                    if call.get("kind") == "explorer-worker"
                ]
                self.assertEqual(len(explorer_calls), 2)
                self.assertEqual(
                    sorted(call["status"] for call in explorer_calls),
                    ["cancelled", "committed"],
                )
                cancelled = next(
                    call for call in explorer_calls if call["status"] == "cancelled"
                )
                self.assertEqual(
                    cancelled.get("cancellation", {}).get("authorized_by"),
                    "explorer-controller",
                )
                self.assertEqual(
                    sum(
                        event["type"] == "explorer_root_candidate_claimed"
                        for event in state["events"]
                    ),
                    1,
                )
                for lineage in state["explorer_control"]["lineages"].values():
                    self.assertEqual(lineage["status"], "stopped")
                    self.assertIsNone(lineage["current_call_id"])
                    self.assertEqual(lineage["attempts"][-1]["outcome"], "stopped")
            finally:
                runtime.close()

    def test_full_turn_promotes_one_memo_then_drains_to_next_explorer_cycle(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            runtime: FrantaRuntime | None = None
            explorer_calls: list[dict[str, Any]] = []
            sort_calls: list[dict[str, Any]] = []
            main_calls: list[dict[str, Any]] = []
            promotion_source_id: str | None = None

            def trusted_stage(
                call: AgentCall,
                skill: str,
                payload: Mapping[str, Any],
            ) -> dict[str, Any]:
                assert runtime is not None
                staged = SkillRuntime(
                    SkillContext.load(call.workspace.path)
                ).invoke(skill, dict(payload))
                return runtime._record_broker_skill_result(
                    call=call,
                    skill=skill,
                    payload=dict(payload),
                    result=staged,
                    capability_token=(
                        f"deterministic-broker:{call.call_id}:"
                        f"{skill}:{payload.get('operation_id')}"
                    ),
                )

            def explorer_result(call: AgentCall) -> dict[str, Any]:
                nonlocal promotion_source_id
                assert runtime is not None
                attempt = int(call.payload["attempt_number"])
                explorer_calls.append(
                    {
                        "call_id": call.call_id,
                        "attempt": attempt,
                        "mode": call.mode,
                        "session_key": call.session_key,
                        "resume": call.resume,
                        "first_attempt_clean_room": call.payload[
                            "first_attempt_clean_room"
                        ],
                    }
                )

                idea_payload = {
                    "operation_id": f"OP-EXPLORER-IDEA-{attempt}",
                    "record_kind": "idea",
                    "abstract": (
                        f"Attempt {attempt} isolates a deterministic ROOT reduction."
                    ),
                    "content": (
                        f"Attempt {attempt} studies a provisional reduction of ROOT; "
                        "the argument remains intentionally unverified."
                    ),
                    "related_memory_ids": [],
                    "cas_operation_ids": [],
                }
                idea = trusted_stage(call, "record-scratch", idea_payload)
                if promotion_source_id is None:
                    promotion_source_id = str(idea["record_id"])

                summary_payload = {
                    "operation_id": f"OP-EXPLORER-SUMMARY-{attempt}",
                    "abstract": (
                        f"Attempt {attempt} records a useful but provisional reduction."
                    ),
                    "content": (
                        "The attempt produced one provisional idea; it has not "
                        "been integrated into canonical Franta memory."
                    ),
                    "directions_tried": [
                        f"Deterministic orthogonal direction {attempt}"
                    ],
                    "main_progress": f"Completed provisional attempt {attempt}.",
                    "main_obstacles": "The ROOT statement remains unverified.",
                    "source_scratch_ids": [str(idea["record_id"])],
                }
                summary = trusted_stage(
                    call, "record-summary", summary_payload
                )

                if attempt == 1:
                    deadline = datetime.fromisoformat(
                        runtime.scheduler.state["phase_control"]["explorer"][
                            "admission_deadline"
                        ]
                    )
                    self.assertEqual(
                        runtime.scheduler.tick_alternation(now=deadline),
                        "explorer_drain",
                    )

                return {
                    "attempt_ended": True,
                    "final_summary_id": str(summary["record_id"]),
                    "root_candidate_scratch_id": None,
                    "root_candidate_outcome": None,
                    "stop_reason": "attempt_complete",
                }

            def main_sort_result(call: AgentCall) -> dict[str, Any]:
                assert promotion_source_id is not None
                card = json.loads(
                    call.workspace.task_card_path.read_text(encoding="utf-8")
                )
                sort_calls.append(
                    {
                        "call_id": call.call_id,
                        "session_key": call.session_key,
                        "resume": call.resume,
                        "sort_run_id": card["sort_run_id"],
                    }
                )
                operation_id = "OP-SORT-PROMOTED-MEMO"
                progress_id = "PRG-SORT-FINAL"
                progress_payload = {
                    "operation_id": "RP-SORT-FINAL",
                    "progress_id": progress_id,
                    "sequence": 1,
                    "is_final": True,
                    "outcome_status": "finished",
                    "progress_since_previous": (
                        "Selected one useful Explorer idea for Franta synthesis."
                    ),
                    "operations": [
                        {
                            "operation_id": operation_id,
                            "kind": "memo",
                            "proposal_id": "TMP-SORT-PROMOTED-MEMO",
                            "abstract": "A provisional ROOT reduction worth retaining.",
                            "genre": "normal",
                            "content": (
                                "The Explorer reduction is useful as a high-level "
                                "Franta memo, without treating it as a proved claim."
                            ),
                            "related_route_ids": [],
                            "explorer_provenance": {
                                "sort_run_id": card["sort_run_id"],
                                "source_record_ids": [promotion_source_id],
                            },
                        }
                    ],
                    "computation_operation_ids": [],
                    "explorer_computation_promotions": [],
                    "fact_challenges": [],
                    "completion_evidence_ids": [operation_id],
                    "attempt_summary": {
                        "work_mode": "main-sort",
                        "task": card["objective"],
                        "proposed_outcome": "finished",
                        "cumulative_important_progress": (
                            "Promoted one provisional idea as a Franta memo."
                        ),
                        "completion_evidence_operation_ids": [operation_id],
                        "most_promising_next_steps": (
                            "Let ordinary Franta planning decide how to use the memo."
                        ),
                    },
                }
                trusted_stage(call, "record-progress", progress_payload)
                return {
                    "sort_ended": True,
                    "final_progress_id": progress_id,
                    "selected_explorer_record_ids": [promotion_source_id],
                    "deferred_computation_record_ids": [],
                }

            def executor(call: AgentCall) -> dict[str, Any]:
                if call.kind == "explorer-worker":
                    return explorer_result(call)
                if call.kind == "main-sort":
                    return main_sort_result(call)
                if call.kind == "synthesizer":
                    return {
                        "resolution": "new",
                        "operation_digest": call.payload["operation_digest"],
                        "explanation": "Publish the selected Explorer idea as a memo.",
                        "canonical_id": None,
                        "relied_on": [],
                        "patch": None,
                    }
                if call.kind == "main":
                    main_calls.append(
                        {
                            "call_id": call.call_id,
                            "session_key": call.session_key,
                            "resume": call.resume,
                        }
                    )
                    batch_id = str(call.payload["reserved_batch_id"])
                    trusted_stage(
                        call,
                        "task-writing",
                        {
                            "operation_id": "AR-POST-SORT-FOLLOWUP",
                            "batch_finalized": True,
                            "batch_id": batch_id,
                            "objective": "Investigate ROOT using the promoted Explorer memo.",
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
                            "reason": "Exercise ordinary planning after the promoted memo.",
                            "root_solution_fact_id": None,
                        },
                    )
                    return {
                        "decision": "assignments",
                        "batch_id": batch_id,
                        "assignment_report_ids": ["AR-POST-SORT-FOLLOWUP"],
                        "wait_for_task_ids": [],
                        "report": None,
                        "decline_proof_writer": False,
                    }
                raise AssertionError(f"unexpected deterministic call {call.kind}")

            runtime = FrantaRuntime.initialize(
                load_manifest(_write_manifest(root)), executor=executor
            )
            try:
                runtime.scheduler.commit_initial_trim({"category_ids": []})
                runtime._main_should_run = lambda: False
                # One cycle for each planned Explorer attempt; the third cycle
                # also executes the sort barrier and the project's first Main
                # continuation.  Stop before ordinary Franta trim review begins.
                runtime.run(max_cycles=3)

                self.assertEqual(runtime.scheduler.alternation_phase, "franta_run")
                self.assertEqual(
                    [
                        (
                            item["attempt"],
                            item["mode"],
                            item["resume"],
                            item["first_attempt_clean_room"],
                        )
                        for item in explorer_calls
                    ],
                    [
                        (1, "check-result", False, False),
                        (2, "portfolio", True, False),
                        (3, "full-memory", True, False),
                    ],
                )
                self.assertEqual(
                    len({item["session_key"] for item in explorer_calls}), 1
                )
                archived_lineages = runtime.scheduler.state["explorer_control"]
                self.assertEqual(len(archived_lineages["lineages"]), 1)
                lineage = next(iter(archived_lineages["lineages"].values()))
                self.assertEqual(lineage["attempts_started"], 3)
                self.assertEqual(lineage["status"], "closed")

                self.assertEqual(len(sort_calls), 1)
                self.assertEqual(len(main_calls), 1)
                self.assertEqual(
                    sort_calls[0]["session_key"], "main:explorer-cycle:00000001"
                )
                self.assertEqual(main_calls[0]["session_key"], "main:project")
                self.assertFalse(main_calls[0]["resume"])
                phase_sort = runtime.scheduler.state["phase_control"]["sort"]
                self.assertEqual(
                    phase_sort["planning_call_id"], main_calls[0]["call_id"]
                )
                self.assertEqual(
                    runtime.scheduler.state["calls"][main_calls[0]["call_id"]][
                        "continuation"
                    ]["post_sort_call_id"],
                    sort_calls[0]["call_id"],
                )
                pending_followups = runtime._ordinary_worker_launches()
                self.assertEqual(len(pending_followups), 1)
                followup_task_id = pending_followups[0]
                self.assertEqual(
                    runtime.scheduler.state["tasks"][followup_task_id]["attempts"], []
                )

                repository = runtime.explorer_repository
                assert repository is not None
                promotion = repository.get_promotion("OP-SORT-PROMOTED-MEMO")
                self.assertIsNotNone(promotion)
                assert promotion is not None
                self.assertEqual(promotion.state, "committed")
                self.assertEqual(promotion.target_kind, "memo")
                self.assertEqual(
                    promotion.source_record_ids, (promotion_source_id,)
                )
                self.assertIsNotNone(promotion.canonical_id)
                canonical = runtime.store.get(str(promotion.canonical_id))
                self.assertEqual(
                    canonical["content"],
                    (
                        "The Explorer reduction is useful as a high-level Franta "
                        "memo, without treating it as a proved claim."
                    ),
                )

                franta_deadline = datetime.fromisoformat(
                    runtime.scheduler.state["phase_control"]["franta"][
                        "admission_deadline"
                    ]
                )
                self.assertEqual(
                    runtime.scheduler.tick_alternation(now=franta_deadline),
                    "franta_drain",
                )
                runtime.run(max_cycles=1)
                state = runtime.scheduler.state
                self.assertEqual(
                    state["phase_control"]["phase"], "explorer_admission"
                )
                self.assertEqual(state["phase_control"]["cycle"], 2)
                self.assertEqual(len(state["phase_control"]["history"]), 1)
                self.assertEqual(len(state["explorer_history"]), 1)
                self.assertEqual(state["explorer_control"]["lineages"], {})
                self.assertEqual(state["tasks"][followup_task_id]["state"], "closed")
                self.assertEqual(
                    state["tasks"][followup_task_id]["final_summary"]["kind"],
                    "alternation_drain",
                )
            finally:
                runtime.close()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
