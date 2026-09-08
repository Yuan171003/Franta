from __future__ import annotations

import json
import sys
import tempfile
import threading
import types
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Any, Callable


SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from franta.access import BrokerClient, BrokerError, policy_for  # noqa: E402
from franta.config import load_manifest  # noqa: E402
from franta.explorer.contracts import ExplorerReadScope  # noqa: E402
from franta.prompts import ModelConfig  # noqa: E402
from franta.runtime import AgentCall, FrantaRuntime, InvalidAgentOutput  # noqa: E402
from franta.skill_runtime import SkillContext, SkillRuntime  # noqa: E402
from explorer_system.agents import EXPLORER_GUIDANCE_VARIANTS  # noqa: E402


class _InspectingTransport:
    """Small transport double that exercises the issued broker capability."""

    def __init__(
        self,
        state_dir: Path,
        inspect: Callable[[BrokerClient, Any], None] | None = None,
    ) -> None:
        self.state_dir = state_dir
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.inspect = inspect
        self.requests: list[Any] = []
        self.ledger = types.SimpleNamespace(resolve=lambda _session_key: None)

    def invoke(self, request: Any) -> Any:
        self.requests.append(request)
        binding = request.broker_binding
        if binding is None:
            raise AssertionError("Explorer calls must receive a broker binding")
        client = BrokerClient(binding.socket_path, binding.token)
        if self.inspect is not None:
            self.inspect(client, request)
        return types.SimpleNamespace(final_message='{"ok": true}')


def _write_manifest(root: Path) -> Path:
    manifest = root / "bootstrap.toml"
    manifest.write_text(
        """
[project]
name = "explorer-runtime-integration"
directory = "project"
root_problem = "Prove the ROOT statement."
foundation_policy = "Use only explicitly recorded assumptions."

[explorer]
max_workers = 1

[initial]
""".strip()
        + "\n",
        encoding="utf-8",
    )
    return manifest


def _runtime(root: Path, transport: _InspectingTransport | None = None) -> FrantaRuntime:
    selected_transport = transport or _InspectingTransport(root / "transport")
    return FrantaRuntime.initialize(
        load_manifest(_write_manifest(root)), transport=selected_transport
    )


def _explorer_call(
    runtime: FrantaRuntime,
    call_id: str,
    *,
    mode: str,
    attempt_number: int,
    source_high_water_seq: int,
    turn_id: str = "ETURN-1",
    worker_session_id: str = "EWORK-1",
) -> AgentCall:
    policy = policy_for("explorer-worker", mode=mode)
    payload = {
        "explorer_turn_id": turn_id,
        "worker_session_id": worker_session_id,
        "attempt_number": attempt_number,
        "source_high_water_seq": source_high_water_seq,
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
        session_key=None,
        resume=False,
        output_schema=runtime.layout.schemas / "explorer-worker.schema.json",
        model_config=ModelConfig("gpt-6-astra", "max"),
        lease_epoch=lease_epoch,
        launch_attempt=launch_attempt,
    )


def _scratch_payload(operation_id: str, marker: str) -> dict[str, object]:
    return {
        "operation_id": operation_id,
        "record_kind": "idea",
        "abstract": f"Spectral filtration {marker}",
        "content": f"Use the {marker} filtration to attack the ROOT statement.",
        "related_memory_ids": [],
        "cas_operation_ids": [],
    }


def _stage(
    call: AgentCall, skill: str, payload: dict[str, object]
) -> dict[str, Any]:
    return SkillRuntime(SkillContext.load(call.workspace.path)).invoke(skill, payload)


def _trust_staged(
    runtime: FrantaRuntime,
    call: AgentCall,
    skill: str,
    payload: dict[str, object],
    staged: dict[str, Any],
) -> dict[str, Any]:
    return runtime._record_broker_skill_result(
        call=call,
        skill=skill,
        payload=payload,
        result=staged,
        capability_token=f"capability-for-{call.call_id}",
    )


class ExplorerRuntimeIntegrationTests(unittest.TestCase):
    def test_persisted_guidance_variant_rebuilds_the_same_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            runtime = _runtime(Path(raw))
            try:
                runtime.scheduler.activate_alternation()
                phase = runtime.scheduler.state["phase_control"]
                started = datetime.fromisoformat(
                    phase["explorer"]["admission_started_at"]
                )
                lineage_id = runtime.scheduler.admit_explorer_lineage(now=started)
                self.assertIsNotNone(runtime.explorer_program)
                call_id = runtime.explorer_program.host.start_attempt(
                    lineage_id,
                    source_high_water_seq=0,
                )
                persisted = runtime.scheduler.state["calls"][call_id]["input"]
                variant = persisted["guidance_variant"]
                self.assertIn(
                    variant,
                    EXPLORER_GUIDANCE_VARIANTS["check-result"],
                )

                policy, workspace, session_key, resume = (
                    runtime._explorer_attempt_workspace(call_id)
                )
                first = runtime._call_spec(
                    call_id,
                    workspace=workspace,
                    policy=policy,
                    mode=policy.mode,
                    session_key=session_key,
                    resume=resume,
                )
                runtime.scheduler.reload()
                second = runtime._call_spec(
                    call_id,
                    workspace=workspace,
                    policy=policy,
                    mode=policy.mode,
                    session_key=session_key,
                    resume=resume,
                )
                self.assertEqual(first.prompt, second.prompt)
                self.assertEqual(first.payload["guidance_variant"], variant)
                self.assertEqual(first.model_config.reasoning_effort, "max")
                self.assertNotIn("{host}", first.prompt)
                self.assertNotIn("{think_guidance}", first.prompt)
            finally:
                runtime.close()

    def test_cancelled_attempt_cannot_trust_a_late_staged_scratch(self) -> None:
        """A callback racing the attempt deadline cannot publish after cancel."""

        with tempfile.TemporaryDirectory() as raw:
            runtime = _runtime(Path(raw))
            try:
                runtime.scheduler.activate_alternation()
                phase = runtime.scheduler.state["phase_control"]
                started = datetime.fromisoformat(
                    phase["explorer"]["admission_started_at"]
                )
                lineage_id = runtime.scheduler.admit_explorer_lineage(now=started)
                planned = runtime.scheduler.start_explorer_attempt(
                    lineage_id,
                    source_high_water_seq=0,
                    now=started,
                )
                call_id = str(planned["call_id"])
                policy, workspace, session_key, resume = (
                    runtime._explorer_attempt_workspace(call_id)
                )
                runtime.scheduler.mark_call_running(call_id)
                call = runtime._call_spec(
                    call_id,
                    workspace=workspace,
                    policy=policy,
                    mode=policy.mode,
                    session_key=session_key,
                    resume=resume,
                )
                payload = _scratch_payload("OP-LATE-AFTER-CANCEL", "late")
                staged = _stage(call, "record-scratch", payload)
                record_id = str(staged["record_id"])

                deadline = datetime.fromisoformat(
                    runtime.scheduler.state["explorer_control"]["lineages"][
                        lineage_id
                    ]["attempts"][-1]["deadline"]
                )
                self.assertEqual(
                    runtime.scheduler.expire_explorer_attempts(now=deadline),
                    (call_id,),
                )
                self.assertEqual(
                    runtime.scheduler.state["calls"][call_id]["status"],
                    "cancelled",
                )

                with self.assertRaisesRegex(
                    InvalidAgentOutput,
                    "stale|not running|cancelled",
                ):
                    _trust_staged(
                        runtime,
                        call,
                        "record-scratch",
                        payload,
                        staged,
                    )

                repository = runtime.explorer_repository
                assert repository is not None
                self.assertEqual(repository.visible_high_water(), 0)
                self.assertIsNone(
                    repository.fetch(
                        ExplorerReadScope(frozenset({str(planned["turn_id"])})),
                        record_id,
                    )
                )
            finally:
                runtime.close()

    def test_cancellation_wins_between_callback_precheck_and_trust_commit(self) -> None:
        """The final generation guard closes the callback precheck TOCTOU gap."""

        with tempfile.TemporaryDirectory() as raw:
            runtime = _runtime(Path(raw))
            try:
                runtime.scheduler.activate_alternation()
                phase = runtime.scheduler.state["phase_control"]
                started = datetime.fromisoformat(
                    phase["explorer"]["admission_started_at"]
                )
                lineage_id = runtime.scheduler.admit_explorer_lineage(now=started)
                planned = runtime.scheduler.start_explorer_attempt(
                    lineage_id,
                    source_high_water_seq=0,
                    now=started,
                )
                call_id = str(planned["call_id"])
                policy, workspace, session_key, resume = (
                    runtime._explorer_attempt_workspace(call_id)
                )
                runtime.scheduler.mark_call_running(call_id)
                call = runtime._call_spec(
                    call_id,
                    workspace=workspace,
                    policy=policy,
                    mode=policy.mode,
                    session_key=session_key,
                    resume=resume,
                )
                payload = _scratch_payload("OP-CANCEL-TOCTOU", "cancel-race")
                staged = _stage(call, "record-scratch", payload)

                # The callback has passed its cheap initial generation check
                # before it computes the workspace generation. Hold it there,
                # let the persisted deadline fence the call, and then release it
                # toward the final prepare/receipt/trust commit guard.
                entered = threading.Event()
                release = threading.Event()
                original_generation = runtime._workspace_generation

                def delayed_generation(value: Any) -> str:
                    entered.set()
                    self.assertTrue(release.wait(timeout=5))
                    return original_generation(value)

                runtime._workspace_generation = delayed_generation  # type: ignore[method-assign]
                outcome: list[object] = []

                def finish_callback() -> None:
                    try:
                        outcome.append(
                            _trust_staged(
                                runtime,
                                call,
                                "record-scratch",
                                payload,
                                staged,
                            )
                        )
                    except Exception as exc:  # asserted below
                        outcome.append(exc)

                callback = threading.Thread(target=finish_callback)
                callback.start()
                self.assertTrue(entered.wait(timeout=5))
                deadline = datetime.fromisoformat(
                    runtime.scheduler.state["explorer_control"]["lineages"]
                    [lineage_id]["attempts"][-1]["deadline"]
                )
                self.assertEqual(
                    runtime.scheduler.expire_explorer_attempts(now=deadline),
                    (call_id,),
                )
                release.set()
                callback.join(timeout=5)
                self.assertFalse(callback.is_alive())

                self.assertEqual(len(outcome), 1)
                self.assertIsInstance(outcome[0], InvalidAgentOutput)
                self.assertIn("stale or its call is not running", str(outcome[0]))
                repository = runtime.explorer_repository
                assert repository is not None
                self.assertIsNone(
                    repository.fetch(
                        ExplorerReadScope(frozenset({str(planned["turn_id"])})),
                        str(staged["record_id"]),
                    )
                )
                self.assertEqual(repository.visible_high_water(), 0)
            finally:
                runtime.close()

    def test_stale_launch_cannot_trust_scratch_after_call_retry(self) -> None:
        """Receipt authority belongs to the exact current lease and launch."""

        with tempfile.TemporaryDirectory() as raw:
            runtime = _runtime(Path(raw))
            try:
                runtime.scheduler.activate_alternation()
                phase = runtime.scheduler.state["phase_control"]
                started = datetime.fromisoformat(
                    phase["explorer"]["admission_started_at"]
                )
                lineage_id = runtime.scheduler.admit_explorer_lineage(now=started)
                planned = runtime.scheduler.start_explorer_attempt(
                    lineage_id,
                    source_high_water_seq=0,
                    now=started,
                )
                call_id = str(planned["call_id"])
                policy, workspace, session_key, resume = (
                    runtime._explorer_attempt_workspace(call_id)
                )
                runtime.scheduler.mark_call_running(call_id)
                stale_call = runtime._call_spec(
                    call_id,
                    workspace=workspace,
                    policy=policy,
                    mode=policy.mode,
                    session_key=session_key,
                    resume=resume,
                )
                payload = _scratch_payload("OP-STALE-LAUNCH", "stale-launch")
                staged = _stage(stale_call, "record-scratch", payload)
                record_id = str(staged["record_id"])

                runtime.scheduler.recover()
                self.assertEqual(
                    runtime.scheduler.state["calls"][call_id]["status"],
                    "retry_pending",
                )
                runtime.scheduler.mark_call_running(call_id)
                current = runtime.scheduler.state["calls"][call_id]
                self.assertEqual(current["status"], "running")
                self.assertNotEqual(
                    (current["lease_epoch"], current["attempt"]),
                    (stale_call.lease_epoch, stale_call.launch_attempt),
                )

                with self.assertRaisesRegex(
                    InvalidAgentOutput,
                    "lease|launch|stale|provenance",
                ):
                    _trust_staged(
                        runtime,
                        stale_call,
                        "record-scratch",
                        payload,
                        staged,
                    )

                repository = runtime.explorer_repository
                assert repository is not None
                self.assertEqual(repository.visible_high_water(), 0)
                self.assertIsNone(
                    repository.fetch(
                        ExplorerReadScope(frozenset({str(planned["turn_id"])})),
                        record_id,
                    )
                )
            finally:
                runtime.close()

    def test_parallel_sessions_reuse_operation_labels_and_fetch_owned_cas(
        self,
    ) -> None:
        """Shared caller labels never cross-wire scratch-to-CAS provenance."""

        with tempfile.TemporaryDirectory() as raw:
            runtime = _runtime(Path(raw))
            try:
                calls = [
                    _explorer_call(
                        runtime,
                        f"EXPLORER-SHARED-{marker}",
                        mode="explore",
                        attempt_number=2,
                        source_high_water_seq=0,
                        turn_id="ETURN-SHARED",
                        worker_session_id=f"EWORK-{marker}",
                    )
                    for marker in ("A", "B")
                ]
                stage_together = threading.Barrier(2)

                def stage_owned(call: AgentCall, marker: str) -> tuple[str, str]:
                    cas_payload: dict[str, object] = {
                        "operation_id": "CAS-SHARED",
                        "software": "Python",
                        "software_version": "3.14",
                        "exact_input": f"print({marker!r})",
                        "exact_output": marker + "\n",
                        "exit_status": 0,
                        "description": f"Compute worker {marker}'s exact model.",
                        "assumptions": "Exact string output.",
                        "environment_versions": {"python": "3.14"},
                        "random_seed": None,
                        "interpretation": f"The model returned {marker}.",
                        "related_ids": {},
                    }
                    stage_together.wait(timeout=5)
                    staged_cas = _stage(call, "CAS", cas_payload)
                    _trust_staged(runtime, call, "CAS", cas_payload, staged_cas)
                    scratch_payload: dict[str, object] = {
                        "operation_id": "OP-SHARED-SCRATCH",
                        "record_kind": "computation",
                        "abstract": f"Worker {marker}'s exact model computation",
                        "content": f"The independently owned computation returned {marker}.",
                        "related_memory_ids": [],
                        "cas_operation_ids": ["CAS-SHARED"],
                    }
                    staged_scratch = _stage(
                        call, "record-scratch", scratch_payload
                    )
                    _trust_staged(
                        runtime,
                        call,
                        "record-scratch",
                        scratch_payload,
                        staged_scratch,
                    )
                    return str(staged_scratch["record_id"]), f"EWORK-{marker}"

                with ThreadPoolExecutor(max_workers=2) as pool:
                    futures = [
                        pool.submit(stage_owned, call, marker)
                        for call, marker in zip(calls, ("A", "B"), strict=True)
                    ]
                    owned = [future.result() for future in futures]

                self.assertEqual(len({record_id for record_id, _ in owned}), 2)
                repository = runtime.explorer_repository
                assert repository is not None
                reader = _explorer_call(
                    runtime,
                    "EXPLORER-SHARED-READER",
                    mode="explore",
                    attempt_number=2,
                    source_high_water_seq=repository.visible_high_water(),
                    turn_id="ETURN-SHARED",
                    worker_session_id="EWORK-READER",
                )
                api = runtime._explorer_api_for_call(reader)
                assert api is not None
                evidence_ids: set[str] = set()
                for record_id, expected_session in owned:
                    fetched = api.fetch(record_id)
                    self.assertEqual(fetched["cas_operation_ids"], ["CAS-SHARED"])
                    self.assertEqual(len(fetched["cas_evidence"]), 1)
                    evidence = fetched["cas_evidence"][0]
                    self.assertEqual(evidence["operation_id"], "CAS-SHARED")
                    self.assertEqual(
                        evidence["worker_session_id"], expected_session
                    )
                    evidence_ids.add(str(evidence["evidence_id"]))
                self.assertEqual(len(evidence_ids), 2)
            finally:
                runtime.close()

    def test_scratch_and_summary_become_visible_only_after_receipts(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            runtime = _runtime(Path(raw))
            try:
                call = _explorer_call(
                    runtime,
                    "EXPLORER-WRITE-1",
                    mode="clean-room",
                    attempt_number=1,
                    source_high_water_seq=0,
                )
                repository = runtime.explorer_repository
                assert repository is not None
                scope = ExplorerReadScope(frozenset({"ETURN-1"}))

                scratch_payload = _scratch_payload("OP-SCRATCH-1", "trusted-first")
                scratch = _stage(call, "record-scratch", scratch_payload)
                scratch_id = str(scratch["record_id"])
                self.assertRegex(scratch_id, r"^ES-")
                self.assertIsNone(repository.fetch(scope, scratch_id))
                self.assertEqual(repository.visible_high_water(), 0)

                _trust_staged(
                    runtime, call, "record-scratch", scratch_payload, scratch
                )
                trusted_scratch = repository.fetch(scope, scratch_id)
                self.assertIsNotNone(trusted_scratch)
                self.assertEqual(repository.visible_high_water(), 1)

                summary_payload: dict[str, object] = {
                    "operation_id": "OP-SUMMARY-1",
                    "abstract": "The spectral filtration exposes one obstruction.",
                    "content": "The first attempt isolates a boundary-map obstacle.",
                    "directions_tried": ["Spectral filtration"],
                    "main_progress": "Reduced ROOT to one boundary map.",
                    "main_obstacles": "No proof that the boundary map vanishes.",
                    "source_scratch_ids": [scratch_id],
                }
                summary = _stage(call, "record-summary", summary_payload)
                summary_id = str(summary["record_id"])
                self.assertRegex(summary_id, r"^ESUM-")
                self.assertIsNone(repository.fetch(scope, summary_id))

                _trust_staged(runtime, call, "record-summary", summary_payload, summary)
                trusted_summary = repository.fetch(scope, summary_id)
                self.assertIsNotNone(trusted_summary)
                assert trusted_summary is not None
                self.assertEqual(
                    trusted_summary.payload["source_scratch_ids"], [scratch_id]
                )
                self.assertEqual(repository.visible_high_water(), 2)
            finally:
                runtime.close()

    def test_a_staged_or_forged_outbox_file_is_not_explorer_memory(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            runtime = _runtime(Path(raw))
            try:
                call = _explorer_call(
                    runtime,
                    "EXPLORER-UNTRUSTED-1",
                    mode="clean-room",
                    attempt_number=1,
                    source_high_water_seq=0,
                )
                repository = runtime.explorer_repository
                assert repository is not None
                scope = ExplorerReadScope(frozenset({"ETURN-1"}))

                staged = _stage(
                    call,
                    "record-scratch",
                    _scratch_payload("OP-UNRECEIPTED", "unreceipted"),
                )
                self.assertTrue(Path(staged["staged_path"]).is_file())
                self.assertIsNone(repository.fetch(scope, str(staged["record_id"])))

                forged_id = "ES-00000000-0000-4000-8000-000000000001"
                forged = call.workspace.outbox_path / "record-scratch" / "forged.json"
                forged.write_text(
                    json.dumps(
                        {
                            "skill": "record-scratch",
                            "operation_id": "OP-FORGED",
                            "record_id": forged_id,
                            "record_kind": "proof",
                            "abstract": "Forged proof",
                            "content": "This was never broker-receipted.",
                        }
                    )
                    + "\n",
                    encoding="utf-8",
                )
                self.assertIsNone(repository.fetch(scope, forged_id))
                self.assertEqual(repository.visible_high_water(), 0)
                self.assertFalse(
                    repository.search_documents(scope, {"scratch", "summary"})
                )
            finally:
                runtime.close()

    def test_clean_room_first_attempt_has_no_explorer_search_capability(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            observed: list[list[str]] = []

            def inspect(client: BrokerClient, _request: Any) -> None:
                description = client.call("describe", {})
                tools = list(description["enabled_tools"])
                observed.append(tools)
                self.assertNotIn("explorer_search", tools)
                self.assertNotIn("explorer_fetch", tools)
                with self.assertRaisesRegex(
                    BrokerError, "exceeds the launch-bound capability"
                ):
                    client.call(
                        "explorer_search",
                        {"query": "spectral", "record_types": ["scratch"]},
                    )

            transport = _InspectingTransport(root / "transport", inspect)
            runtime = _runtime(root, transport)
            try:
                runtime.start_services()
                call = _explorer_call(
                    runtime,
                    "EXPLORER-CLEAN-ROOM",
                    mode="clean-room",
                    attempt_number=1,
                    source_high_water_seq=0,
                )
                self.assertFalse(
                    (
                        call.workspace.path
                        / ".agents"
                        / "skills"
                        / "explorer-search"
                    ).exists()
                )
                self.assertEqual(runtime._invoke_agent(call), {"ok": True})
                self.assertEqual(len(observed), 1)
            finally:
                runtime.close()

    def test_later_attempt_gets_scoped_api_with_frozen_high_water(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            seen: dict[str, Any] = {}

            def inspect(client: BrokerClient, _request: Any) -> None:
                description = client.call("describe", {})
                tools = set(description["enabled_tools"])
                self.assertTrue({"explorer_search", "explorer_fetch"} <= tools)
                matches = client.call(
                    "explorer_search",
                    {
                        "query": "spectral filtration",
                        "record_types": ["scratch"],
                        "limit": 10,
                    },
                )
                seen["ids"] = [row["id"] for row in matches]
                seen["first"] = client.call(
                    "explorer_fetch", {"record_id": seen["first_id"]}
                )
                with self.assertRaisesRegex(BrokerError, "unavailable"):
                    client.call(
                        "explorer_fetch", {"record_id": seen["later_id"]}
                    )

            transport = _InspectingTransport(root / "transport", inspect)
            runtime = _runtime(root, transport)
            try:
                first_call = _explorer_call(
                    runtime,
                    "EXPLORER-SEED-1",
                    mode="clean-room",
                    attempt_number=1,
                    source_high_water_seq=0,
                )
                first_payload = _scratch_payload("OP-SEED-1", "before-freeze")
                first = _stage(first_call, "record-scratch", first_payload)
                _trust_staged(
                    runtime, first_call, "record-scratch", first_payload, first
                )
                repository = runtime.explorer_repository
                assert repository is not None
                frozen_high_water = repository.visible_high_water()
                self.assertEqual(frozen_high_water, 1)

                later_call = _explorer_call(
                    runtime,
                    "EXPLORER-SEED-2",
                    mode="explore",
                    attempt_number=2,
                    source_high_water_seq=frozen_high_water,
                )
                later_payload = _scratch_payload("OP-SEED-2", "after-freeze")
                later = _stage(later_call, "record-scratch", later_payload)
                _trust_staged(
                    runtime, later_call, "record-scratch", later_payload, later
                )
                self.assertEqual(repository.visible_high_water(), 2)

                seen["first_id"] = str(first["record_id"])
                seen["later_id"] = str(later["record_id"])
                search_call = _explorer_call(
                    runtime,
                    "EXPLORER-SEARCH-2",
                    mode="explore",
                    attempt_number=2,
                    source_high_water_seq=frozen_high_water,
                )
                self.assertTrue(
                    (
                        search_call.workspace.path
                        / ".agents"
                        / "skills"
                        / "explorer-search"
                    ).is_dir()
                )
                runtime.start_services()
                self.assertEqual(runtime._invoke_agent(search_call), {"ok": True})
                self.assertEqual(seen["ids"], [seen["first_id"]])
                self.assertEqual(seen["first"]["id"], seen["first_id"])
            finally:
                runtime.close()


if __name__ == "__main__":
    unittest.main()
