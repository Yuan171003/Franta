from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from franta.access import policy_for
from franta.config import load_manifest
from franta.explorer.contracts import ExplorerReadScope
from franta.prompts import ModelConfig
from franta.runtime import AgentCall, FrantaRuntime, InvalidAgentOutput
from franta.skill_runtime import SkillContext, SkillRuntime


LEGACY_STATUS_KEYS = {
    "project",
    "project_dir",
    "gate",
    "event_cursor",
    "root",
    "portfolio_revision",
    "tasks",
    "memories",
    "needs_attention",
    "guidance",
}


def _write_manifest(
    root: Path,
    *,
    explorer: bool,
    max_abstract_bytes: int | None = None,
    max_content_bytes: int | None = None,
) -> Path:
    explorer_lines: list[str] = []
    if explorer:
        explorer_lines = [
            "",
            "[explorer]",
            "max_workers = 1",
        ]
        if max_abstract_bytes is not None:
            explorer_lines.append(
                f"max_abstract_bytes = {max_abstract_bytes}"
            )
        if max_content_bytes is not None:
            explorer_lines.append(f"max_content_bytes = {max_content_bytes}")
    manifest = root / "bootstrap.toml"
    manifest.write_text(
        "\n".join(
            [
                "[project]",
                'name = "explorer-config-status-recovery"',
                'directory = "project"',
                'root_problem = "Prove the deterministic ROOT statement."',
                'foundation_policy = "Use only the declared test axioms."',
                *explorer_lines,
                "",
                "[initial]",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return manifest


def _explorer_call(
    runtime: FrantaRuntime,
    call_id: str,
    *,
    attempt_number: int = 1,
    turn_id: str = "XTURN-00000001",
    worker_session_id: str = "XLINEAGE-00000001",
) -> AgentCall:
    mode = "clean-room" if attempt_number == 1 else "explore"
    policy = policy_for("explorer-worker", mode=mode)
    payload = {
        "explorer_turn_id": turn_id,
        "worker_session_id": worker_session_id,
        "attempt_number": attempt_number,
        "source_high_water_seq": 0,
    }
    runtime.scheduler.prepare_call(
        "explorer-worker",
        payload,
        call_id=call_id,
        retry_limit=1,
        continuation={
            "lineage_id": worker_session_id,
            "attempt_number": attempt_number,
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
        mode=mode,
        payload=payload,
        workspace=workspace,
        policy=policy,
        prompt="",
        session_key=f"explorer:{worker_session_id}",
        resume=attempt_number > 1,
        output_schema=runtime.layout.schemas / "explorer-worker.schema.json",
        model_config=ModelConfig("gpt-6-astra", "ultra"),
        lease_epoch=lease_epoch,
        launch_attempt=launch_attempt,
    )


def _stage(
    call: AgentCall, skill: str, payload: dict[str, object]
) -> dict[str, object]:
    return SkillRuntime(SkillContext.load(call.workspace.path)).invoke(skill, payload)


def _record(
    runtime: FrantaRuntime,
    call: AgentCall,
    skill: str,
    payload: dict[str, object],
    staged: dict[str, object],
) -> dict[str, object]:
    return runtime._record_broker_skill_result(
        call=call,
        skill=skill,
        payload=payload,
        result=staged,
        capability_token=f"test-capability:{call.call_id}:{skill}",
    )


class ExplorerConfigurationStatusRecoveryTests(unittest.TestCase):
    def test_explorer_opt_in_is_persisted_but_cannot_retrofit_a_legacy_project(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            manifest_path = _write_manifest(root, explorer=False)
            legacy_runtime = FrantaRuntime.initialize(load_manifest(manifest_path))
            legacy_runtime.close()

            enabled_manifest = _write_manifest(root, explorer=True)
            with self.assertRaisesRegex(
                RuntimeError, "different bootstrap manifest"
            ):
                FrantaRuntime.initialize(load_manifest(enabled_manifest))

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            manifest = load_manifest(_write_manifest(root, explorer=True))
            runtime = FrantaRuntime.initialize(manifest)
            try:
                persisted = json.loads(
                    (runtime.layout.private / "runtime-config.json").read_text(
                        encoding="utf-8"
                    )
                )
                self.assertEqual(
                    persisted["manifest_digest"], manifest.manifest_digest
                )
                self.assertEqual(
                    persisted["explorer"],
                    {
                        "enabled": True,
                        "max_workers": 1,
                        "attempts_per_worker": 3,
                        "attempt_seconds": 10_800,
                        "explorer_admission_seconds": 7_200,
                        "franta_admission_seconds": 28_800,
                        "max_scratch_per_attempt": 256,
                        "max_scratch_per_turn": 4096,
                        "max_abstract_bytes": 4096,
                        "max_content_bytes": 262_144,
                    },
                )
                self.assertIsNotNone(runtime.explorer_repository)
                self.assertTrue(runtime.layout.explorer_database.is_file())
                self.assertTrue(
                    (runtime.layout.schemas / "explorer-worker.schema.json").is_file()
                )
                self.assertTrue(
                    (runtime.layout.schemas / "main-sort.schema.json").is_file()
                )
            finally:
                runtime.close()

    def test_legacy_runtime_payload_status_and_recovery_shape_are_unchanged(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            runtime = FrantaRuntime.initialize(
                load_manifest(_write_manifest(root, explorer=False))
            )
            try:
                persisted = json.loads(
                    (runtime.layout.private / "runtime-config.json").read_text(
                        encoding="utf-8"
                    )
                )
                self.assertNotIn("explorer", persisted)
                self.assertIsNone(runtime.explorer_repository)
                self.assertFalse(runtime.layout.explorer_database.exists())
                self.assertFalse(
                    (runtime.layout.schemas / "explorer-worker.schema.json").exists()
                )
                self.assertNotIn("phase_control", runtime.scheduler.state)
                self.assertNotIn("explorer_control", runtime.scheduler.state)
                before = runtime.status()
                self.assertEqual(set(before), LEGACY_STATUS_KEYS)
                runtime.recover()
                self.assertEqual(runtime.status(), before)
                project_dir = runtime.layout.root
            finally:
                runtime.close()

            reopened = FrantaRuntime.open(project_dir)
            try:
                reopened.recover()
                self.assertIsNone(reopened.explorer_repository)
                self.assertEqual(reopened.status(), before)
                self.assertEqual(set(reopened.status()), LEGACY_STATUS_KEYS)
            finally:
                reopened.close()

    def test_explorer_status_is_conditional_and_recovery_replays_receipt_tail(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            runtime = FrantaRuntime.initialize(
                load_manifest(_write_manifest(root, explorer=True))
            )
            call = _explorer_call(runtime, "CALL-RECOVERY-TAIL")
            payload: dict[str, object] = {
                "operation_id": "OP-RECOVERY-TAIL",
                "record_kind": "idea",
                "abstract": "A durable provisional reduction",
                "content": "Reduce ROOT to a smaller, still unverified statement.",
                "related_memory_ids": [],
                "cas_operation_ids": [],
            }
            staged = _stage(call, "record-scratch", payload)
            record_id = str(staged["record_id"])
            scope = ExplorerReadScope(frozenset({"XTURN-00000001"}))
            repository = runtime.explorer_repository
            assert repository is not None
            try:
                runtime.scheduler.activate_alternation()
                # Simulate the only crash window in the cross-store protocol:
                # Explorer data is prepared and the private receipt is durable,
                # but visibility has not yet been acknowledged in Explorer SQLite.
                with mock.patch.object(
                    repository,
                    "trust_receipt",
                    side_effect=RuntimeError("simulated process death"),
                ):
                    with self.assertRaisesRegex(RuntimeError, "simulated process death"):
                        _record(runtime, call, "record-scratch", payload, staged)
                self.assertIsNone(repository.fetch(scope, record_id))
                self.assertEqual(repository.visible_high_water(), 0)

                deadline = datetime.fromisoformat(
                    runtime.scheduler.state["phase_control"]["explorer"][
                        "admission_deadline"
                    ]
                )
                self.assertEqual(
                    runtime.scheduler.tick_alternation(now=deadline),
                    "explorer_drain",
                )
                project_dir = runtime.layout.root
            finally:
                runtime.close()

            reopened = FrantaRuntime.open(project_dir)
            try:
                reopened_repository = reopened.explorer_repository
                assert reopened_repository is not None
                self.assertIsNone(reopened_repository.fetch(scope, record_id))
                self.assertEqual(reopened_repository.visible_high_water(), 0)

                reopened.recover()
                recovered = reopened_repository.fetch(scope, record_id)
                self.assertIsNotNone(recovered)
                assert recovered is not None
                self.assertEqual(recovered.abstract, payload["abstract"])
                self.assertEqual(reopened_repository.visible_high_water(), 1)
                recovered_status = reopened.status()
                self.assertEqual(
                    set(recovered_status),
                    LEGACY_STATUS_KEYS
                    | {"alternation_phase", "alternation_cycle"},
                )
                self.assertEqual(
                    recovered_status["alternation_phase"], "explorer_drain"
                )
                self.assertEqual(recovered_status["alternation_cycle"], 1)

                # Both durable recovery and status are idempotent.
                reopened.recover()
                self.assertEqual(reopened_repository.visible_high_water(), 1)
                self.assertEqual(reopened.status(), recovered_status)
            finally:
                reopened.close()

    def test_configured_limits_count_utf8_bytes_for_scratch_and_summary(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            runtime = FrantaRuntime.initialize(
                load_manifest(
                    _write_manifest(
                        root,
                        explorer=True,
                        max_abstract_bytes=5,
                        max_content_bytes=7,
                    )
                )
            )
            try:
                call = _explorer_call(runtime, "CALL-BYTE-CAPS")
                repository = runtime.explorer_repository
                assert repository is not None
                scope = ExplorerReadScope(frozenset({"XTURN-00000001"}))

                boundary_payload: dict[str, object] = {
                    "operation_id": "OP-BYTE-BOUNDARY",
                    "record_kind": "idea",
                    "abstract": "éé",  # four UTF-8 bytes
                    "content": "根根",  # six UTF-8 bytes
                    "related_memory_ids": [],
                    "cas_operation_ids": [],
                }
                boundary = _stage(call, "record-scratch", boundary_payload)
                _record(
                    runtime,
                    call,
                    "record-scratch",
                    boundary_payload,
                    boundary,
                )
                self.assertIsNotNone(
                    repository.fetch(scope, str(boundary["record_id"]))
                )

                too_wide_abstract = {
                    **boundary_payload,
                    "operation_id": "OP-BYTE-ABSTRACT-OVER",
                    "abstract": "ééé",  # six UTF-8 bytes
                }
                abstract_result = _stage(
                    call, "record-scratch", too_wide_abstract
                )
                with self.assertRaisesRegex(
                    InvalidAgentOutput,
                    "Explorer abstract exceeds the configured UTF-8 byte limit",
                ):
                    _record(
                        runtime,
                        call,
                        "record-scratch",
                        too_wide_abstract,
                        abstract_result,
                    )

                too_wide_content = {
                    **boundary_payload,
                    "operation_id": "OP-BYTE-CONTENT-OVER",
                    "content": "根根根",  # nine UTF-8 bytes
                }
                content_result = _stage(call, "record-scratch", too_wide_content)
                with self.assertRaisesRegex(
                    InvalidAgentOutput,
                    "Explorer content exceeds the configured UTF-8 byte limit",
                ):
                    _record(
                        runtime,
                        call,
                        "record-scratch",
                        too_wide_content,
                        content_result,
                    )

                summary_payload: dict[str, object] = {
                    "operation_id": "OP-BYTE-SUMMARY-OVER",
                    "abstract": "ok",
                    "content": "根根根",
                    "directions_tried": ["one direction"],
                    "main_progress": "Recorded the boundary case.",
                    "main_obstacles": "The configured byte cap.",
                    "source_scratch_ids": [str(boundary["record_id"])],
                }
                summary = _stage(call, "record-summary", summary_payload)
                with self.assertRaisesRegex(
                    InvalidAgentOutput,
                    "Explorer content exceeds the configured UTF-8 byte limit",
                ):
                    _record(
                        runtime,
                        call,
                        "record-summary",
                        summary_payload,
                        summary,
                    )

                documents = repository.search_documents(
                    scope, {"scratch", "summary"}
                )
                self.assertEqual(
                    [item.record_id for item in documents],
                    [str(boundary["record_id"])],
                )
            finally:
                runtime.close()


if __name__ == "__main__":
    unittest.main()
