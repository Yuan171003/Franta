"""Executable orchestration layer joining storage, access, Codex, and workflows.

The scheduler remains deterministic.  This module is its process driver: it
materializes one sealed workspace per logical call, launches the configured
agent, validates/stores skill artifacts, and advances only persisted workflow
states.  A stopped process can be reconstructed solely from the project files.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import os
import shutil
import socket
import stat
import threading
import time
import uuid
from contextlib import nullcontext
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Protocol, Sequence

from .trim_category import CategoryStore
from .contracts.agent_access import AccessPolicy, STAGING_TOOL_BY_SKILL, policy_for
from .contracts.configuration import BootstrapManifest
from .contracts.responses import write_schemas
from .contracts.workflows import (
    CallState,
    FINAL_OPERATION_STATES,
    GateState,
    OperationState,
    RetryPolicy,
    SchedulerLimits,
    TaskState,
    WorkflowError,
)
from .execution_gateway.broker import MemoryBroker
from .execution_gateway.cas_process import CASProcessScope
from .execution_gateway.permissions import CodexPermissionProfile
from .execution_gateway.skills import (
    SkillContext,
    SkillRuntime,
    SkillRuntimeError,
    allowed_skills,
    compile_human_guidance,
    execute_cas,
)
from .execution_gateway.transport import (
    CodexRequest,
    CodexTransport,
    CodexTransportError,
)
from .explorer_adapter import (
    AuditedExplorerAPI,
    MainSortContractError,
    build_explorer_program,
    build_explorer_service,
    build_main_sort_explorer_snapshot,
    explorer_agent_call_spec,
    explorer_api_for_runtime_call,
    explorer_skill_source,
    is_explorer_agent_role,
    prepare_explorer_staged_result,
    validate_main_sort_submission,
    write_explorer_schemas,
)
from .advisor_adapter import (
    AdvisorCycleContext,
    AdvisorFinalization,
    AdvisorLaunchSpec,
    HumanFeedback,
    ProblemAssignment,
    SelectionReport,
    advisor_agent_call_spec,
    advisor_breakthrough_evidence_freshness,
    advisor_context,
    advisor_skill_source,
    advisor_status,
    build_advisor_program,
    commit_assignment_transition,
    feedback_from_operator,
    is_advisor_call_kind,
    render_selection_report,
    write_advisor_schemas,
)
from .explorer.contracts import ExplorerReadScope
from .explorer.repository import ExplorerRepository
from .exploration_control import runtime as exploration_runtime
from .human_guidance import read_human_guidance_inbox
from .locking import ProjectLock
from .project import ProjectLayout
from .prompts import ModelConfig, model_config, prompt_for, with_human_guidance
from .read_access.audit import AuditLog
from .read_access.materialization import (
    MaterializedWorkspace,
    WorkspaceMaterializer,
    validate_explorer_snapshot,
)
from .read_access.main_memory_snapshot import (
    MAIN_MEMORY_SNAPSHOT_RELATIVE_PATH,
    MainMemorySnapshot,
    MainMemorySnapshotError,
    recover_main_memory_snapshot,
    validate_main_memory_snapshot,
)
from .read_access.snapshots import (
    MemorySnapshot,
    assignment_snapshots_from_task_card,
    snapshot_from_record,
)
from .recovery import canonical_json, stable_digest
from .render import render_record
from .scheduler import (
    CapacityError,
    Scheduler,
    SchedulerError,
    TransportFailure,
)
from .store import MemoryStore
from .util import atomic_write_bytes, atomic_write_text


RUNTIME_CONFIG_VERSION = 1
RUNTIME_STATE_KEY = "runtime.v1"
BOOTSTRAP_STATE_KEY = "bootstrap-proposals.v1"
WORKER_CALL_TIMEOUT_SECONDS = 4 * 60 * 60
MAIN_SESSION_KEY = "main:project"
_MAIN_MEMORY_SNAPSHOT_CONTEXT_KEYS = frozenset(
    {
        "snapshot_id",
        "snapshot_digest",
        "source_state_digest",
        "high_water_mark",
        "record_count",
        "relative_path",
    }
)


class RuntimeErrorBase(RuntimeError):
    """Base class for executable-runtime failures."""


class InvalidAgentOutput(RuntimeErrorBase):
    """A model turn ended normally but violated its structured contract."""


class ProjectNeedsAttention(RuntimeErrorBase):
    """The durable workflow needs an authorized operator action."""


@dataclass(frozen=True)
class AgentCall:
    """Complete testable description of one materialized agent invocation."""

    call_id: str
    kind: str
    role: str
    mode: str | None
    payload: Mapping[str, Any]
    workspace: MaterializedWorkspace
    policy: AccessPolicy
    prompt: str
    session_key: str | None
    resume: bool
    output_schema: Path
    model_config: ModelConfig
    lease_epoch: int = 1
    launch_attempt: int = 1


@dataclass
class _LiveOperationReview:
    call_id: str
    workspace: MaterializedWorkspace
    policy: AccessPolicy
    future: Future[dict[str, Any]]


class AgentExecutor(Protocol):
    """Optional deterministic replacement for Codex in integration tests."""

    def __call__(self, call: AgentCall) -> Mapping[str, Any]: ...


def _json_object(text: str) -> dict[str, Any]:
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise InvalidAgentOutput("agent final response is not JSON") from exc
    if not isinstance(value, Mapping):
        raise InvalidAgentOutput("agent final response must be a JSON object")
    return copy.deepcopy(dict(value))


def _safe_component(value: str, label: str) -> str:
    if not value or value in {".", ".."} or any(ch not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-" for ch in value):
        raise RuntimeErrorBase(f"unsafe {label}: {value!r}")
    return value


def _manifest_payload(manifest: BootstrapManifest) -> dict[str, Any]:
    payload = {
        "version": RUNTIME_CONFIG_VERSION,
        "manifest_digest": manifest.manifest_digest,
        "project_name": manifest.project_name,
        "project_dir": str(manifest.project_dir),
        "root_problem": manifest.root_problem,
        "foundation_policy": manifest.foundation_policy,
        "context_budgets": dict(manifest.context_budgets),
        "default_model": asdict(manifest.default_model),
        "synthesizer_model": asdict(manifest.synthesizer_model),
        "retries": asdict(manifest.retries),
        "timeouts": asdict(manifest.timeouts),
        "limits": asdict(manifest.limits),
        "tools": {
            **asdict(manifest.tools),
            "extra_cas": dict(manifest.tools.extra_cas),
        },
        "initial": {name: list(values) for name, values in vars(manifest.initial).items()},
        "native_web_search": manifest.native_web_search,
    }
    if manifest.explorer is not None:
        payload["explorer"] = asdict(manifest.explorer)
    if manifest.advisor is not None:
        payload["advisor"] = asdict(manifest.advisor)
    return payload


def _load_runtime_payload(layout: ProjectLayout) -> dict[str, Any]:
    path = layout.private / "runtime-config.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeErrorBase(f"cannot load runtime configuration from {path}: {exc}") from exc
    if not isinstance(value, Mapping) or value.get("version") != RUNTIME_CONFIG_VERSION:
        raise RuntimeErrorBase("unsupported or malformed runtime configuration")
    return copy.deepcopy(dict(value))


def _retry_policy(config: Mapping[str, Any]) -> RetryPolicy:
    raw = config["retries"]
    return RetryPolicy(
        worker=int(raw["worker_interruptions"]),
        verifier=int(raw["verifier_transport"]),
        synthesizer=int(raw["synthesizer_transport"]),
        summarizer=int(raw["summarizer_transport"]),
        main=int(raw["main_transport"]),
        trimmer=int(raw["trimmer_transport"]),
    )


def _scheduler_limits(config: Mapping[str, Any]) -> SchedulerLimits:
    raw = config["limits"]
    return SchedulerLimits(
        max_non_verifier_workers=int(raw["max_non_verifier_workers"]),
        max_parallel_verifiers=int(raw["max_parallel_verifiers"]),
        portfolio_max_memories=int(raw["max_portfolio_memories"]),
        search_max_results=int(raw["max_search_results"]),
        explicit_worker_resumes=int(raw["worker_resume_launches"]),
        trim_assignment_interval=int(raw["assignments_per_trim_review"]),
        trimmer_rounds_per_session=int(raw["trimmer_rounds_per_session"]),
    )


def _foundation(config: Mapping[str, Any]) -> dict[str, Any]:
    text = str(config["foundation_policy"])
    return {
        "version": 1,
        "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "text": text,
    }


def _root_obligation(root_problem: str, root_id: str) -> dict[str, Any]:
    first_line = next((line.strip() for line in root_problem.splitlines() if line.strip()), "ROOT")
    return {
        "id": root_id,
        "abstract": f"Root target: {first_line[:220]}",
        "statement": root_problem,
        "importance": "This is the defining root problem of the project.",
        "predecessor_fact_ids": [],
        "partial_progress": [],
        "related_route_ids": [],
        "relations": [],
    }


def _workspace_from_path(path: Path) -> MaterializedWorkspace:
    return MaterializedWorkspace(
        path=path,
        input_path=path / "input",
        outbox_path=path / "outbox",
        artifacts_path=path / "artifacts",
        tmp_path=path / "tmp",
        task_card_path=(path / "input/task_card.json") if (path / "input/task_card.json").is_file() else None,
        root_problem_path=path / "input/root_problem.md",
        portfolio_index_path=(path / "input/portfolio/index.json") if (path / "input/portfolio/index.json").is_file() else None,
        access_manifest_path=path / "input/access_policy.json",
    )


class _RuntimeTaskBackend:
    """Expose closed task summaries and content-addressed archived artifacts."""

    def __init__(self, scheduler: Scheduler, archive_root: Path) -> None:
        self.scheduler = scheduler
        self.archive_root = archive_root.resolve()

    @staticmethod
    def _artifact_id(reference: Mapping[str, Any]) -> str:
        material = (
            str(reference.get("relative_path") or "")
            + "\0"
            + str(reference.get("sha256") or "")
        )
        return "TA-" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]

    def get_task_summary(self, task_id: str) -> Mapping[str, Any] | None:
        state = self.scheduler.state
        task = state["tasks"].get(task_id)
        if task is None or task.get("state") != TaskState.CLOSED.value:
            return None
        artifacts = [
            {
                "artifact_id": self._artifact_id(reference),
                "kind": reference.get("kind", "artifact"),
                "sha256": reference.get("sha256"),
            }
            for reference in task.get("artifact_references", [])
        ]
        return {
            "task_id": task_id,
            "mode": task.get("task_card", {}).get("mode"),
            "objective": task.get("task_card", {}).get("objective"),
            "state": task.get("state"),
            "final_status": task.get("final_status"),
            "final_summary": copy.deepcopy(task.get("final_summary")),
            "canonical_changes": [
                {
                    "operation_id": operation_id,
                    "state": state["operations"].get(operation_id, {}).get("state"),
                    "canonical_id": state["operations"].get(operation_id, {}).get(
                        "canonical_id"
                    ),
                }
                for operation_id in task.get("operation_ids", [])
            ],
            "artifacts": artifacts,
        }

    def get_task_artifact(
        self, task_id: str, artifact_id: str
    ) -> Mapping[str, Any] | None:
        state = self.scheduler.state
        task = state["tasks"].get(task_id)
        if task is None or task.get("state") != TaskState.CLOSED.value:
            return None
        selected = next(
            (
                reference
                for reference in task.get("artifact_references", [])
                if self._artifact_id(reference) == artifact_id
            ),
            None,
        )
        if selected is None:
            return None
        relative = Path(str(selected.get("relative_path") or ""))
        if relative.is_absolute() or ".." in relative.parts:
            raise RuntimeErrorBase("unsafe archived task-artifact reference")
        path = (self.archive_root / relative).resolve(strict=True)
        if self.archive_root != path and self.archive_root not in path.parents:
            raise RuntimeErrorBase("archived task artifact escaped the task archive")
        if not path.is_file() or path.is_symlink():
            raise RuntimeErrorBase("archived task artifact is not a regular file")
        data = path.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        if digest != selected.get("sha256"):
            raise RuntimeErrorBase("archived task artifact failed its hash check")
        try:
            content = data.decode("utf-8")
            encoding = "utf-8"
        except UnicodeDecodeError:
            content = base64.b64encode(data).decode("ascii")
            encoding = "base64"
        return {
            "task_id": task_id,
            "artifact_id": artifact_id,
            "kind": selected.get("kind", "artifact"),
            "sha256": digest,
            "encoding": encoding,
            "content": content,
        }


class FrantaRuntime:
    """Long-running, restartable process driver for one project."""

    def __init__(
        self,
        layout: ProjectLayout,
        config: Mapping[str, Any],
        *,
        executor: AgentExecutor | None = None,
        transport: CodexTransport | None = None,
    ) -> None:
        self.layout = layout
        self.config = copy.deepcopy(dict(config))
        # Schema additions are scheduler-owned and deterministic; refreshing
        # them also upgrades resumable projects without touching research data.
        write_schemas(layout.schemas)
        if self.config.get("explorer", {}).get("enabled") is True:
            write_explorer_schemas(layout.schemas)
        if self.config.get("advisor", {}).get("enabled") is True:
            report_root = self.layout.advisor_reports
            if report_root.is_symlink():
                raise RuntimeErrorBase("Advisor report directory must not be a symlink")
            report_root.mkdir(parents=True, exist_ok=True)
            if (
                not report_root.is_dir()
                or report_root.resolve(strict=True).parent
                != self.layout.root.resolve(strict=True)
            ):
                raise RuntimeErrorBase("Advisor report directory escaped the project")
            write_advisor_schemas(layout.schemas)
        self.store = MemoryStore(layout.database, projection_dir=layout.canonical)
        self.scheduler = Scheduler(
            self.store,
            retry_policy=_retry_policy(self.config),
            limits=_scheduler_limits(self.config),
        )
        self.explorer_repository: ExplorerRepository | None = None
        self.explorer_service = None
        if self.config.get("explorer", {}).get("enabled") is True:
            self.layout.explorer_cas_archive.mkdir(parents=True, exist_ok=True)
            self.explorer_repository = ExplorerRepository(
                self.layout.explorer_database
            )
            self.explorer_service = build_explorer_service(
                self.explorer_repository, self.config["explorer"]
            )
            self.scheduler.configure_alternation(
                self.config["explorer"], defer_start=True
            )
        self.advisor_program = None
        if self.config.get("advisor", {}).get("enabled") is True:
            if self.explorer_repository is None:
                raise RuntimeErrorBase("Advisor requires the Explorer block")
            self.scheduler.configure_advisor(self.config["advisor"])
        self.categories = CategoryStore(self.store, layout.root)
        additional_skill_sources: list[Path] = []
        if self.explorer_repository is not None:
            additional_skill_sources.append(explorer_skill_source())
        if self.config.get("advisor", {}).get("enabled") is True:
            additional_skill_sources.append(advisor_skill_source())
        self.materializer = WorkspaceMaterializer(
            layout.workspaces,
            additional_skill_sources=tuple(additional_skill_sources),
        )
        self.transport = transport or CodexTransport(
            layout.private / "transport",
            codex_binary=str(self.config["tools"].get("codex") or "codex"),
            default_timeout_seconds=(
                self.config.get("timeouts", {}).get("agent_call_seconds")
            ),
        )
        self.executor = executor
        self.explorer_program = (
            build_explorer_program(self)
            if self.explorer_repository is not None
            else None
        )
        if self.config.get("advisor", {}).get("enabled") is True:
            self.advisor_program = build_advisor_program(self)
        self.read_audit = AuditLog(layout.audit / "agent-reads.jsonl")
        self.broker = MemoryBroker(
            self.store.as_memory_backend(),
            task_backend=_RuntimeTaskBackend(self.scheduler, layout.task_archive),
            audit=self.read_audit,
        )
        self.lock = ProjectLock(layout.lock_file)
        self._trusted_skill_receipt_lock = threading.Lock()
        self._services_started = False

    @classmethod
    def initialize(
        cls,
        manifest: BootstrapManifest,
        *,
        executor: AgentExecutor | None = None,
        transport: CodexTransport | None = None,
    ) -> "FrantaRuntime":
        layout = ProjectLayout.at(manifest.project_dir)
        layout.create()
        config = _manifest_payload(manifest)
        config_path = layout.private / "runtime-config.json"
        if config_path.exists():
            existing = _load_runtime_payload(layout)
            if existing.get("manifest_digest") != manifest.manifest_digest:
                raise RuntimeErrorBase(
                    "project already exists with a different bootstrap manifest"
                )
        else:
            atomic_write_text(config_path, json.dumps(config, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
            atomic_write_bytes(layout.manifest_snapshot, manifest.source.read_bytes())
            atomic_write_text(layout.root_problem, manifest.root_problem.rstrip() + "\n", mode=0o444)
            atomic_write_text(layout.foundation, manifest.foundation_policy.rstrip() + "\n", mode=0o444)
            write_schemas(layout.schemas)
            if manifest.explorer is not None:
                write_explorer_schemas(layout.schemas)
            if manifest.advisor is not None:
                write_advisor_schemas(layout.schemas)
        runtime = cls(layout, config, executor=executor, transport=transport)
        runtime._bootstrap_canonical_state()
        return runtime

    @classmethod
    def open(
        cls,
        project_dir: str | os.PathLike[str],
        *,
        executor: AgentExecutor | None = None,
        transport: CodexTransport | None = None,
    ) -> "FrantaRuntime":
        layout = ProjectLayout.at(project_dir)
        return cls(layout, _load_runtime_payload(layout), executor=executor, transport=transport)

    def close(self) -> None:
        self.stop_services()
        if self.explorer_repository is not None:
            self.explorer_repository.close()
        self.store.close()

    def __enter__(self) -> "FrantaRuntime":
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        del exc_type, exc, traceback
        self.close()

    # -------------------------------------------------------------- bootstrap

    def _bootstrap_canonical_state(self) -> None:
        foundation = _foundation(self.config)
        root_id = self.scheduler.bootstrap(
            root_problem=str(self.config["root_problem"]),
            foundation_policy=foundation,
        )
        self.store.add_obligation(
            "bootstrap:root-obligation",
            _root_obligation(str(self.config["root_problem"]), root_id),
            actor="bootstrap",
        )
        self.scheduler.bind_canonical_root_obligation()

        initial = self.config.get("initial", {})
        for index, raw in enumerate(initial.get("obligations", []), 1):
            self.store.add_obligation(
                f"bootstrap:obligation:{index}", copy.deepcopy(dict(raw)), actor="bootstrap"
            )

        if self.store.load_control_state(BOOTSTRAP_STATE_KEY) is None:
            proposals: list[dict[str, Any]] = []
            for kind, manifest_key in (("route_add", "routes"), ("memo", "memos"), ("claim_add", "claims")):
                for index, raw in enumerate(initial.get(manifest_key, []), 1):
                    payload = copy.deepcopy(dict(raw))
                    proposal_id = str(payload.get("proposal_id") or f"BOOT-{manifest_key.upper()}-{index}")
                    payload["proposal_id"] = proposal_id
                    proposals.append(
                        {
                            "operation_id": f"bootstrap:{manifest_key}:{index}",
                            "kind": kind,
                            "proposal_id": proposal_id,
                            "payload": payload,
                            "status": "pending",
                        }
                    )
            self.store.compare_and_swap_control_state(
                BOOTSTRAP_STATE_KEY,
                None,
                {"proposals": proposals, "mappings": {}, "stable": not proposals},
            )
        seeds = list(initial.get("seed_theorems", []))
        seed_path = self.layout.private / "seed-theorems.json"
        if not seed_path.exists():
            atomic_write_text(seed_path, json.dumps(seeds, ensure_ascii=False, indent=2, sort_keys=True) + "\n")

    # --------------------------------------------------------------- services

    def start_services(self) -> None:
        if self._services_started:
            return
        socket_path = self.layout.private / "memory-broker.sock"
        # The single-scheduler file lock is acquired by run().  At that point a
        # leftover socket cannot belong to a live project runtime.
        if socket_path.exists() or socket_path.is_symlink():
            socket_path.unlink()
        stale_spool = Path(str(socket_path) + ".spool")
        if stale_spool.exists():
            shutil.rmtree(stale_spool)
        self.broker.start(socket_path)
        self._services_started = True

    def stop_services(self) -> None:
        if self._services_started:
            self.broker.stop()
            self._services_started = False

    # ---------------------------------------------------------- materializing

    def _snapshot(self, memory_id: str) -> MemorySnapshot:
        record = self.store.get(memory_id)
        return snapshot_from_record(
            record,
            content=render_record(record),
            requested_id=memory_id,
        )

    def _new_main_memory_snapshot(
        self,
    ) -> tuple[dict[str, Any], MainMemorySnapshot]:
        """Freeze one matching scheduler-state and canonical-memory wake view."""

        snapshot_id = f"MMS-{uuid.uuid4().hex}"
        self.layout.main_memory_snapshots.mkdir(parents=True, exist_ok=True)
        return self.scheduler.capture_main_memory_snapshot(
            self.layout.main_memory_snapshots / snapshot_id,
            snapshot_id=snapshot_id,
        )

    def _new_advisor_memory_snapshot(
        self,
    ) -> tuple[dict[str, Any], MainMemorySnapshot]:
        """Freeze the same complete memory view through Advisor's input port."""

        # Reuse the immutable Main-snapshot wire format; the dedicated Advisor
        # directory and context binding distinguish ownership without forking
        # or weakening the already-validated snapshot contract.
        snapshot_id = f"MMS-{uuid.uuid4().hex}"
        self.layout.advisor_memory_snapshots.mkdir(parents=True, exist_ok=True)
        return self.scheduler.capture_main_memory_snapshot(
            self.layout.advisor_memory_snapshots / snapshot_id,
            snapshot_id=snapshot_id,
        )

    @staticmethod
    def _main_memory_snapshot_descriptor(
        snapshot: MainMemorySnapshot,
    ) -> dict[str, Any]:
        return {
            "snapshot_id": snapshot.snapshot_id,
            "snapshot_digest": snapshot.snapshot_digest,
            "source_state_digest": snapshot.source_state_digest,
            "high_water_mark": snapshot.source_event_cursor,
            "record_count": snapshot.record_count,
            "relative_path": MAIN_MEMORY_SNAPSHOT_RELATIVE_PATH.as_posix(),
        }

    def _main_memory_snapshot_from_context(
        self,
        context: Mapping[str, Any],
        *,
        descriptor_key: str = "memory_snapshot",
        snapshot_root: Path | None = None,
        owner: str = "Main",
    ) -> MainMemorySnapshot:
        raw = context.get(descriptor_key)
        if not isinstance(raw, Mapping) or set(raw) != _MAIN_MEMORY_SNAPSHOT_CONTEXT_KEYS:
            raise RuntimeErrorBase(
                f"{owner} call lacks its exact complete-memory snapshot identity"
            )
        snapshot_id = raw.get("snapshot_id")
        snapshot_digest = raw.get("snapshot_digest")
        high_water_mark = raw.get("high_water_mark")
        record_count = raw.get("record_count")
        if (
            not isinstance(snapshot_id, str)
            or not isinstance(snapshot_digest, str)
            or raw.get("relative_path")
            != MAIN_MEMORY_SNAPSHOT_RELATIVE_PATH.as_posix()
            or not isinstance(high_water_mark, int)
            or isinstance(high_water_mark, bool)
            or high_water_mark < 0
            or not isinstance(record_count, int)
            or isinstance(record_count, bool)
            or record_count < 0
        ):
            raise RuntimeErrorBase(f"{owner} memory snapshot identity is malformed")
        try:
            snapshot = recover_main_memory_snapshot(
                (snapshot_root or self.layout.main_memory_snapshots) / snapshot_id,
                expected_snapshot_id=snapshot_id,
                expected_snapshot_digest=snapshot_digest,
            )
        except MainMemorySnapshotError as exc:
            raise RuntimeErrorBase(str(exc)) from exc
        if (
            snapshot.source_state_digest != raw.get("source_state_digest")
            or snapshot.source_event_cursor != high_water_mark
            or snapshot.record_count != record_count
            or context.get("event_cursor") != high_water_mark
        ):
            raise RuntimeErrorBase(
                f"{owner} memory snapshot metadata drifted from the persisted call input"
            )
        return snapshot

    def _portfolio(self, task_card: Mapping[str, Any], mode: str) -> list[MemorySnapshot]:
        return assignment_snapshots_from_task_card(
            task_card,
            mode,
            snapshot_lookup=self._snapshot,
        )

    def _enrich_task_card(
        self, task: Mapping[str, Any], attempt: int, supplement: Mapping[str, Any] | None
    ) -> dict[str, Any]:
        card = copy.deepcopy(dict(task["task_card"]))
        card["attempt"] = int(attempt)
        card["batch_id"] = task["batch_id"]
        card["attempt_supplement"] = copy.deepcopy(dict(supplement or {})) or None
        card["foundation_policy"] = _foundation(self.config)
        targets: list[dict[str, Any]] = []
        for obligation_id in card.get("main_obligation_ids", []):
            try:
                record = self.store.get(str(obligation_id)).to_dict()
            except Exception:
                continue
            targets.append(
                {
                    "id": record["id"],
                    "revision": record["revision"],
                    "status": record["status"],
                    "statement": record.get("statement"),
                    "abstract": record.get("abstract"),
                }
            )
        card["target_obligations"] = targets
        return card

    def _is_legacy_snapshotless_main_context(
        self, context: Mapping[str, Any]
    ) -> bool:
        """Recognize immutable Main inputs written by the original topology."""

        return bool(
            "memory_snapshot" not in context
            and "explorer" not in self.config
            and "advisor" not in self.config
        )

    def _make_workspace(
        self,
        *,
        call_id: str,
        policy: AccessPolicy,
        task_card: Mapping[str, Any] | None = None,
        portfolio: Sequence[MemorySnapshot] = (),
        context: Mapping[str, Any] | None = None,
        frozen_input: Mapping[str, Any] | None = None,
    ) -> MaterializedWorkspace:
        explorer_snapshot: Mapping[str, Any] | None = None
        main_memory_snapshot: MainMemorySnapshot | None = None
        persisted_call = self.scheduler.state.get("calls", {}).get(call_id, {})
        call_kind = str(persisted_call.get("kind") or "")
        if task_card is not None and task_card.get("root_problem"):
            root_problem = str(task_card["root_problem"])
        elif context is not None and context.get("root_problem"):
            root_problem = str(context["root_problem"])
        elif frozen_input is not None and frozen_input.get("root_problem"):
            root_problem = str(frozen_input["root_problem"])
        elif persisted_call.get("input", {}).get("root_problem"):
            root_problem = str(persisted_call["input"]["root_problem"])
        else:
            root_problem = str(self.config["root_problem"])
        if policy.role == "main-sort":
            if task_card is None or self.explorer_repository is None:
                raise RuntimeErrorBase(
                    "main-sort requires a task-bound Explorer snapshot"
                )
            explorer_snapshot = build_main_sort_explorer_snapshot(
                self.explorer_repository,
                task_card,
            )
        if policy.project_memory_snapshot:
            if call_kind == "main":
                if context is None:
                    raise RuntimeErrorBase(
                        "Main workspace requires its persisted snapshot context"
                    )
                # Projects persisted before complete Main snapshots were
                # introduced can contain an already-prepared legacy Main call
                # whose immutable input has no snapshot descriptor.  Preserve
                # that exact recovery surface only for the original Franta-only
                # topology.  Explorer/Advisor projects, and every newly
                # prepared Main call, remain fail-closed on a missing snapshot.
                legacy_snapshotless_main = (
                    self._is_legacy_snapshotless_main_context(context)
                )
                if not legacy_snapshotless_main:
                    main_memory_snapshot = self._main_memory_snapshot_from_context(
                        context
                    )
            elif is_advisor_call_kind(call_kind):
                if context is None:
                    raise RuntimeErrorBase(
                        "Advisor workspace requires its persisted snapshot context"
                    )
                main_memory_snapshot = self._main_memory_snapshot_from_context(
                    context,
                    descriptor_key="host_memory_snapshot",
                    snapshot_root=self.layout.advisor_memory_snapshots,
                    owner="Advisor",
                )
        path = self.layout.workspaces / _safe_component(call_id, "call ID")
        if path.is_dir() and not path.is_symlink():
            workspace = _workspace_from_path(path)
            if main_memory_snapshot is not None:
                self._validate_main_workspace(
                    workspace,
                    policy=policy,
                    context=context,
                    memory_snapshot=main_memory_snapshot,
                    expected_root_problem=root_problem,
                    owner=("Advisor" if is_advisor_call_kind(call_kind) else "Main"),
                )
            if explorer_snapshot is not None:
                self._validate_main_sort_workspace(
                    workspace,
                    policy=policy,
                    task_card=task_card,
                    explorer_snapshot=explorer_snapshot,
                    context=context,
                )
            return workspace
        skills = set(allowed_skills(policy))
        if not self._configured_cas_executables():
            skills.discard("CAS")
        if not self.config["tools"].get("tectonic"):
            skills.discard("human-guidance")
        workspace = self.materializer.create(
            call_id,
            root_problem=root_problem,
            policy=policy,
            task_card=task_card,
            portfolio=portfolio,
            context=context,
            frozen_input=frozen_input,
            explorer_snapshot=explorer_snapshot,
            main_memory_snapshot=main_memory_snapshot,
            skills=sorted(skills),
        )
        if explorer_snapshot is not None:
            self._validate_main_sort_workspace(
                workspace,
                policy=policy,
                task_card=task_card,
                explorer_snapshot=explorer_snapshot,
                context=context,
            )
        if main_memory_snapshot is not None:
            self._validate_main_workspace(
                workspace,
                policy=policy,
                context=context,
                memory_snapshot=main_memory_snapshot,
                expected_root_problem=root_problem,
                owner=("Advisor" if is_advisor_call_kind(call_kind) else "Main"),
            )
        return workspace

    def _validate_main_workspace(
        self,
        workspace: MaterializedWorkspace,
        *,
        policy: AccessPolicy,
        context: Mapping[str, Any] | None,
        memory_snapshot: MainMemorySnapshot,
        expected_root_problem: str | None = None,
        owner: str = "Main",
    ) -> None:
        """Revalidate a complete-memory control workspace before launch."""

        if not isinstance(context, Mapping):
            raise RuntimeErrorBase(f"{owner} workspace lacks its persisted context")

        def read_json(path: Path, label: str) -> Any:
            if (
                path.is_symlink()
                or not path.is_file()
                or stat.S_IMODE(path.lstat().st_mode) != 0o444
            ):
                raise RuntimeErrorBase(f"{owner} workspace has unsafe {label}")
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise RuntimeErrorBase(f"{owner} workspace has invalid {label}") from exc

        try:
            validate_main_memory_snapshot(
                workspace.path / MAIN_MEMORY_SNAPSHOT_RELATIVE_PATH,
                expected_snapshot_id=memory_snapshot.snapshot_id,
                expected_snapshot_digest=memory_snapshot.snapshot_digest,
            )
        except MainMemorySnapshotError as exc:
            raise RuntimeErrorBase(str(exc)) from exc
        if read_json(workspace.input_path / "context.json", "context") != dict(
            context
        ):
            raise RuntimeErrorBase(f"{owner} workspace context drifted")
        if read_json(
            workspace.access_manifest_path, "access policy"
        ) != policy.as_public_dict():
            raise RuntimeErrorBase(f"{owner} workspace access policy drifted")
        root_path = workspace.root_problem_path
        if (
            root_path.is_symlink()
            or not root_path.is_file()
            or stat.S_IMODE(root_path.lstat().st_mode) != 0o444
        ):
            raise RuntimeErrorBase(f"{owner} workspace root problem is unsafe")
        expected_root = expected_root_problem or str(self.config["root_problem"])
        if root_path.read_text(encoding="utf-8") != (
            expected_root.rstrip() + "\n"
        ):
            raise RuntimeErrorBase(f"{owner} workspace root problem drifted")
        expected_inputs = {
            "root_problem.md",
            "access_policy.json",
            "context.json",
            MAIN_MEMORY_SNAPSHOT_RELATIVE_PATH.name,
        }
        if workspace.input_path.is_symlink() or {
            path.name for path in workspace.input_path.iterdir()
        } != expected_inputs:
            raise RuntimeErrorBase(f"{owner} workspace received unexpected frozen input")
        skills_root = workspace.path / ".agents" / "skills"
        expected_skills = set(allowed_skills(policy))
        actual_skills = {
            path.name
            for path in skills_root.glob("*")
            if path.is_dir() and not path.is_symlink()
        }
        if actual_skills != expected_skills or any(
            path.is_symlink() for path in skills_root.rglob("*")
        ):
            raise RuntimeErrorBase(f"{owner} workspace skill set drifted")

    def _validate_main_sort_workspace(
        self,
        workspace: MaterializedWorkspace,
        *,
        policy: AccessPolicy,
        task_card: Mapping[str, Any] | None,
        explorer_snapshot: Mapping[str, Any],
        context: Mapping[str, Any] | None,
    ) -> None:
        """Revalidate the frozen inputs before every sorter launch/retry."""

        validate_explorer_snapshot(workspace.path, explorer_snapshot)
        if not isinstance(task_card, Mapping):
            raise RuntimeErrorBase("main-sort workspace lacks its task card")

        def read_json(path: Path, label: str) -> Any:
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise RuntimeErrorBase(
                    f"main-sort workspace has invalid {label}"
                ) from exc

        expected_policy = policy.as_public_dict()
        if read_json(
            workspace.access_manifest_path, "access policy"
        ) != expected_policy:
            raise RuntimeErrorBase("main-sort workspace access policy drifted")
        expected_card = copy.deepcopy(dict(task_card))
        expected_root_problem = str(
            expected_card.get("root_problem") or self.config["root_problem"]
        )
        expected_card["root_problem"] = expected_root_problem
        expected_card["access_policy"] = expected_policy
        if workspace.task_card_path is None or read_json(
            workspace.task_card_path, "task card"
        ) != expected_card:
            raise RuntimeErrorBase("main-sort workspace task card drifted")
        if workspace.root_problem_path.read_text(encoding="utf-8") != (
            expected_root_problem.rstrip() + "\n"
        ):
            raise RuntimeErrorBase("main-sort workspace root problem drifted")

        context_path = workspace.input_path / "context.json"
        if context_path.exists() or context_path.is_symlink():
            raise RuntimeErrorBase(
                "main-sort role received unexpected context"
            )

        expected_skills = set(allowed_skills(policy))
        if not self._configured_cas_executables():
            expected_skills.discard("CAS")
        if not self.config["tools"].get("tectonic"):
            expected_skills.discard("human-guidance")
        skills_root = workspace.path / ".agents" / "skills"
        actual_skills = {
            path.name
            for path in skills_root.glob("*")
            if path.is_dir() and not path.is_symlink()
        }
        if actual_skills != expected_skills:
            raise RuntimeErrorBase("main-sort workspace skill set drifted")

    def _configured_cas_executables(self) -> dict[str, str]:
        """Return frozen trusted CAS names and paths; paths stay scheduler-private."""

        return {
            name: str(value)
            for name, value in {
                "sage": self.config["tools"].get("sage"),
                "macaulay2": self.config["tools"].get("macaulay2"),
                **dict(self.config["tools"].get("extra_cas", {})),
            }.items()
            if value
        }

    def _permission_profile(self, policy: AccessPolicy) -> CodexPermissionProfile:
        return CodexPermissionProfile.for_policy(
            policy,
            canonical_path=self.layout.canonical,
            private_paths=self.layout.scheduler_private_paths,
        )

    def _explorer_api_for_call(self, call: AgentCall) -> AuditedExplorerAPI | None:
        """Compatibility forwarding hook into the Franta Explorer adapter."""

        return explorer_api_for_runtime_call(self, call)

    def _archive_explorer_cas_output(
        self, call: AgentCall, artifact: Mapping[str, Any]
    ) -> str | None:
        output = artifact.get("output_artifact")
        if output is None:
            return None
        if not isinstance(output, Mapping):
            raise InvalidAgentOutput("Explorer CAS output artifact is malformed")
        relative = Path(str(output.get("path") or ""))
        digest = str(output.get("sha256") or "").lower()
        source = (call.workspace.path / relative).resolve(strict=True)
        workspace_root = call.workspace.path.resolve()
        if (
            relative.is_absolute()
            or ".." in relative.parts
            or workspace_root not in source.parents
            or not source.is_file()
            or source.is_symlink()
            or hashlib.sha256(source.read_bytes()).hexdigest() != digest
        ):
            raise InvalidAgentOutput("Explorer CAS output failed archive validation")
        shard = self.layout.explorer_cas_archive / digest[:2]
        shard.mkdir(parents=True, exist_ok=True)
        target = shard / digest
        data = source.read_bytes()
        if target.exists():
            if target.is_symlink() or hashlib.sha256(target.read_bytes()).hexdigest() != digest:
                raise RuntimeErrorBase("Explorer CAS archive hash collision")
        else:
            atomic_write_bytes(target, data, mode=0o600)
        return target.relative_to(self.layout.private).as_posix()

    def _prepare_explorer_object_for_receipt(
        self,
        *,
        call: AgentCall,
        skill: str,
        artifact: Mapping[str, Any],
        receipt: Mapping[str, Any],
    ) -> None:
        service = getattr(self, "explorer_service", None)
        if service is None or call.kind != "explorer-worker":
            return
        if skill not in {"record-scratch", "record-summary", "CAS"}:
            return
        archived = (
            self._archive_explorer_cas_output(call, artifact)
            if skill == "CAS"
            else None
        )
        prepare_explorer_staged_result(
            self,
            call,
            skill=skill,
            artifact=artifact,
            receipt=receipt,
            archived_artifact_relpath=archived,
        )

    # ------------------------------------------------------------- agent calls

    def _call_spec(
        self,
        call_id: str,
        *,
        workspace: MaterializedWorkspace,
        policy: AccessPolicy,
        mode: str | None = None,
        session_key: str | None = None,
        resume: bool = False,
    ) -> AgentCall:
        persisted = self.scheduler.state["calls"][call_id]
        kind = str(persisted["kind"])
        human_guidance = (
            persisted["input"].get("task_card", {}).get("human_guidance")
            if kind == "worker"
            else persisted["input"].get("human_guidance")
        )
        validation_kind = self._validation_kind(persisted)
        role = (
            "worker"
            if kind == "worker"
            else "advisor"
            if is_advisor_call_kind(kind)
            else kind
        )
        root_problem = workspace.root_problem_path.read_text(encoding="utf-8").rstrip()
        input_path = "input/frozen_sprint.json" if kind == "summarizer" else "input/context.json"
        if kind in {"worker", "main-sort"}:
            input_path = "input/task_card.json"
        if is_advisor_call_kind(kind):
            continuation = persisted.get("continuation") or {}
            stage = str(continuation.get("stage") or "")
            advisor_index = int(continuation.get("advisor_index", 0))
            advisor_spec = advisor_agent_call_spec(
                stage=stage,
                advisor_index=advisor_index,
                settings=self.config["advisor"],
            )
            if session_key not in {None, advisor_spec.session_key}:
                raise RuntimeErrorBase("Advisor call has the wrong session key")
            prompt = advisor_spec.prompt
            schema_name = advisor_spec.schema_name
            selected_model = advisor_spec.model_config
        elif is_explorer_agent_role(role):
            explorer_spec = explorer_agent_call_spec(
                role,
                root_problem=root_problem,
                mode=mode,
                guidance_variant=persisted["input"].get("guidance_variant"),
                policy=policy,
                input_path=input_path,
                **(
                    {"human_guidance": human_guidance["text"]}
                    if human_guidance is not None
                    else {}
                ),
            )
            prompt = explorer_spec.prompt
            schema_name = explorer_spec.schema_name
            selected_model = explorer_spec.model_config
        else:
            prompt = prompt_for(
                role,
                root_problem=root_problem,
                mode=mode,
                policy=policy,
                input_path=input_path,
                available_skills={
                    path.name
                    for path in (workspace.path / ".agents" / "skills").glob("*")
                    if path.is_dir() and not path.is_symlink()
                },
            )
            schema_name = "worker" if kind == "worker" else validation_kind
            if kind in {"worker", "summarizer"}:
                selected_model = model_config(role, mode=mode)
            else:
                configured_model = (
                    self.config["synthesizer_model"]
                    if kind == "synthesizer"
                    else self.config["default_model"]
                )
                selected_model = ModelConfig(
                    model=str(configured_model["model"]),
                    reasoning_effort=str(configured_model["reasoning_effort"]),
                )
        if human_guidance is not None and kind in {"main", "worker"}:
            prompt = with_human_guidance(
                prompt,
                role=kind,
                guidance_id=human_guidance["guidance_id"],
                text=human_guidance["text"],
            )
        return AgentCall(
            call_id=call_id,
            kind=kind,
            role=role,
            mode=mode,
            payload=copy.deepcopy(persisted["input"]),
            workspace=workspace,
            policy=policy,
            prompt=prompt,
            session_key=session_key,
            resume=resume,
            output_schema=self.layout.schemas / f"{schema_name}.schema.json",
            model_config=selected_model,
            lease_epoch=int(persisted.get("lease_epoch", 1)),
            launch_attempt=int(persisted.get("attempt", 1)),
        )

    def _trusted_skill_receipt_path(self) -> Path:
        state_dir = getattr(self.transport, "state_dir", None)
        if state_dir is None:
            raise RuntimeErrorBase("transport does not expose scheduler-private state")
        return Path(state_dir) / "trusted-skill-results.jsonl"

    def _append_trusted_skill_receipt(self, receipt: Mapping[str, Any]) -> None:
        """Durably retain a broker result outside the agent-writable workspace."""

        path = self._trusted_skill_receipt_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.is_symlink():
            raise RuntimeErrorBase("trusted skill receipt path is a symlink")
        lock = getattr(self, "_trusted_skill_receipt_lock", None)
        if lock is None:
            # A few narrow unit tests construct a runtime without __init__.
            # Production always creates this lock in __init__.
            lock = threading.Lock()
            self._trusted_skill_receipt_lock = lock
        encoded = (
            json.dumps(dict(receipt), ensure_ascii=False, sort_keys=True) + "\n"
        ).encode("utf-8")
        with lock:
            flags = os.O_RDWR | os.O_CREAT
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            descriptor = os.open(path, flags, 0o600)
            with os.fdopen(descriptor, "r+b") as handle:
                if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                    raise RuntimeErrorBase("trusted skill receipt store is not regular")
                handle.seek(0, os.SEEK_END)
                size = handle.tell()
                if size:
                    handle.seek(0)
                    existing = handle.read()
                    if not existing.endswith(b"\n"):
                        boundary = existing.rfind(b"\n") + 1
                        tail = existing[boundary:]
                        try:
                            recovered = json.loads(tail.decode("utf-8"))
                        except (UnicodeDecodeError, json.JSONDecodeError):
                            # Only an unterminated, invalid tail can be torn.
                            # Completed rows, including malformed terminated
                            # rows, are never rewritten here.
                            handle.seek(boundary)
                            handle.truncate()
                        else:
                            if not isinstance(recovered, Mapping):
                                handle.seek(boundary)
                                handle.truncate()
                            else:
                                handle.seek(0, os.SEEK_END)
                                handle.write(b"\n")
                handle.seek(0, os.SEEK_END)
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())

    def _replay_explorer_trusted_receipts(self) -> None:
        """Finish the receipt-to-Explorer visibility tail after a process death.

        A staged Explorer row is prepared before the private JSONL receipt is
        appended.  Only a syntactically complete receipt whose scheduler-owned
        digest verifies may be replayed; torn or forged rows authenticate
        nothing.  The repository then idempotently exposes only objects that
        were already linked to one of those receipts.
        """

        service = self.explorer_service
        if service is None:
            return
        path = self._trusted_skill_receipt_path()
        if not path.exists():
            return
        if path.is_symlink() or not path.is_file():
            raise RuntimeErrorBase("trusted skill receipt store is unsafe")
        receipt_hashes: list[str] = []
        for raw_line in path.read_bytes().splitlines():
            try:
                decoded = json.loads(raw_line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            if not isinstance(decoded, Mapping):
                continue
            receipt = dict(decoded)
            receipt_sha256 = receipt.pop("receipt_sha256", None)
            receipt.pop("time_ns", None)
            if (
                receipt.get("type") != "trusted_skill_result"
                or receipt.get("status") != "succeeded"
                or receipt.get("source") != "broker_mcp"
                or receipt.get("skill")
                not in {"record-scratch", "record-summary", "CAS"}
                or not isinstance(receipt_sha256, str)
                or stable_digest(receipt) != receipt_sha256
            ):
                continue
            receipt_hashes.append(receipt_sha256)
        service.replay_trusted_receipts(receipt_hashes)

    @staticmethod
    def _workspace_generation(workspace: MaterializedWorkspace) -> str:
        """Identify one workspace directory across retries and process restarts."""

        if workspace.path.is_symlink() or not workspace.path.is_dir():
            raise RuntimeErrorBase("agent workspace is missing or unsafe")
        status = workspace.path.stat()
        return f"{int(status.st_dev)}:{int(status.st_ino)}"

    def _record_broker_skill_result(
        self,
        *,
        call: AgentCall,
        skill: str,
        payload: Mapping[str, Any],
        result: Mapping[str, Any],
        capability_token: str,
    ) -> dict[str, Any]:
        """Bind one completed broker stage to its exact bytes and capability."""

        if skill not in STAGING_TOOL_BY_SKILL:
            raise InvalidAgentOutput(f"unknown staged skill result: {skill}")
        explorer_record_write = call.kind == "explorer-worker" and skill in {
            "record-scratch",
            "record-summary",
            "CAS",
        }
        if explorer_record_write:
            try:
                self.scheduler.validate_explorer_skill_write(
                    call.call_id, call.lease_epoch, call.launch_attempt
                )
            except SchedulerError as exc:
                raise InvalidAgentOutput(str(exc)) from exc
        operation_id = str(result.get("operation_id") or "")
        artifact = result.get("artifact")
        staged_path = result.get("staged_path")
        if (
            not operation_id
            or not isinstance(artifact, Mapping)
            or not isinstance(staged_path, str)
            or not staged_path
        ):
            raise InvalidAgentOutput(f"{skill} returned an incomplete staged result")
        expected_name = operation_id.replace(":", "-") + ".json"
        expected = call.workspace.outbox_path / skill / expected_name
        path = Path(staged_path)
        if not path.is_absolute() or path.is_symlink():
            raise InvalidAgentOutput(f"{skill} returned an unsafe staged path")
        try:
            resolved = path.resolve(strict=True)
            expected_resolved = expected.resolve(strict=True)
        except OSError as exc:
            raise InvalidAgentOutput(f"{skill} staged result is missing") from exc
        if resolved != expected_resolved or not resolved.is_file():
            raise InvalidAgentOutput(f"{skill} returned the wrong staged path")
        try:
            data = resolved.read_bytes()
            stored = json.loads(data)
        except (OSError, json.JSONDecodeError) as exc:
            raise InvalidAgentOutput(f"{skill} staged an invalid artifact") from exc
        artifact_value = copy.deepcopy(dict(artifact))
        if (
            not isinstance(stored, Mapping)
            or dict(stored) != artifact_value
            or artifact_value.get("skill") != skill
            or str(artifact_value.get("operation_id") or "") != operation_id
        ):
            raise InvalidAgentOutput(f"{skill} result does not match its staged artifact")
        referenced_artifacts: list[dict[str, str]] = []
        referenced_values: list[tuple[str, Any]] = []
        if skill == "human-guidance":
            referenced_values.append(("compiled_pdf", artifact_value.get("pdf_path")))
        elif skill == "selection-report":
            referenced_values.append(
                ("selection_report", artifact_value.get("report_path"))
            )
        elif skill == "CAS" and artifact_value.get("output_artifact") is not None:
            output_artifact = artifact_value.get("output_artifact")
            if not isinstance(output_artifact, Mapping):
                raise InvalidAgentOutput("CAS returned an invalid output artifact")
            referenced_values.append(("cas_output", output_artifact.get("path")))
        for kind, raw_reference in referenced_values:
            if not isinstance(raw_reference, str) or not raw_reference:
                raise InvalidAgentOutput(f"{skill} returned an invalid referenced artifact")
            reference = Path(raw_reference)
            if reference.is_absolute() or ".." in reference.parts:
                raise InvalidAgentOutput(f"{skill} referenced artifact escaped its workspace")
            reference_path = call.workspace.path / reference
            try:
                reference_resolved = reference_path.resolve(strict=True)
            except OSError as exc:
                raise InvalidAgentOutput(
                    f"{skill} referenced artifact is missing"
                ) from exc
            if (
                reference_path.is_symlink()
                or not reference_resolved.is_file()
                or (
                    call.workspace.path.resolve() != reference_resolved
                    and call.workspace.path.resolve() not in reference_resolved.parents
                )
            ):
                raise InvalidAgentOutput(f"{skill} referenced artifact is unsafe")
            reference_digest = hashlib.sha256(reference_resolved.read_bytes()).hexdigest()
            if kind == "cas_output" and output_artifact.get("sha256") != reference_digest:
                raise InvalidAgentOutput("CAS output artifact failed its declared hash check")
            referenced_artifacts.append(
                {
                    "kind": kind,
                    "relative_path": reference_resolved.relative_to(
                        call.workspace.path.resolve()
                    ).as_posix(),
                    "sha256": reference_digest,
                }
            )
        relative_path = resolved.relative_to(call.workspace.path.resolve()).as_posix()
        result_value = copy.deepcopy(dict(result))
        core = {
            "type": "trusted_skill_result",
            "status": "succeeded",
            "source": "broker_mcp",
            "call_id": call.call_id,
            "lease_epoch": int(call.lease_epoch),
            "launch_attempt": int(call.launch_attempt),
            "workspace_generation": self._workspace_generation(call.workspace),
            "role": call.role,
            "mode": call.mode,
            "workspace": str(call.workspace.path.resolve()),
            "capability_sha256": hashlib.sha256(
                capability_token.encode("utf-8")
            ).hexdigest(),
            "skill": skill,
            "operation_id": operation_id,
            "relative_path": relative_path,
            "staged_path": str(resolved),
            "artifact_sha256": hashlib.sha256(data).hexdigest(),
            "artifact_payload_sha256": stable_digest(artifact_value),
            "referenced_artifacts": referenced_artifacts,
            "payload_sha256": stable_digest(dict(payload)),
            "result_sha256": stable_digest(result_value),
        }
        if skill == "CAS":
            raw_exit_status = artifact_value.get("exit_status")
            core["execution_succeeded"] = (
                not isinstance(raw_exit_status, bool)
                and str(raw_exit_status).strip() == "0"
            )
        receipt = {**core, "receipt_sha256": stable_digest(core), "time_ns": time.time_ns()}
        if explorer_record_write:
            try:
                with self.scheduler.explorer_skill_write_guard(
                    call.call_id, call.lease_epoch, call.launch_attempt
                ):
                    self._prepare_explorer_object_for_receipt(
                        call=call,
                        skill=skill,
                        artifact=artifact_value,
                        receipt=receipt,
                    )
                    self._append_trusted_skill_receipt(receipt)
                    explorer_service = getattr(self, "explorer_service", None)
                    if explorer_service is not None:
                        explorer_service.trust_receipt(
                            str(receipt["receipt_sha256"])
                        )
            except SchedulerError as exc:
                raise InvalidAgentOutput(str(exc)) from exc
        else:
            self._append_trusted_skill_receipt(receipt)
        return result_value

    def _trusted_skill_results(
        self,
        *,
        call_id: str,
        skill: str,
        workspace: MaterializedWorkspace,
        expected_lease_epoch: int,
        expected_launch_attempt: int,
    ) -> list[dict[str, Any]]:
        path = self._trusted_skill_receipt_path()
        if not path.exists():
            return []
        if path.is_symlink() or not path.is_file():
            raise InvalidAgentOutput("trusted skill receipt store is unsafe")
        active_generation = self._workspace_generation(workspace)
        selected: list[dict[str, Any]] = []
        for raw_line in path.read_bytes().splitlines():
            try:
                value = json.loads(raw_line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                # A process death may tear only the last append.  Such a row
                # authenticates nothing and the one-to-one check fails closed.
                continue
            if not isinstance(value, Mapping):
                continue
            receipt = dict(value)
            if (
                receipt.get("type") != "trusted_skill_result"
                or receipt.get("status") != "succeeded"
                or receipt.get("source") != "broker_mcp"
                or receipt.get("call_id") != call_id
                or receipt.get("lease_epoch") != int(expected_lease_epoch)
                or receipt.get("launch_attempt")
                != int(expected_launch_attempt)
                or receipt.get("workspace_generation") != active_generation
                or receipt.get("skill") != skill
                or receipt.get("workspace") != str(workspace.path.resolve())
            ):
                continue
            checksum = receipt.pop("receipt_sha256", None)
            receipt.pop("time_ns", None)
            if not isinstance(checksum, str) or checksum != stable_digest(receipt):
                continue
            receipt["receipt_sha256"] = checksum
            selected.append(receipt)
        return selected

    def _completed_mcp_skill_results(
        self,
        *,
        call_id: str,
        skill: str,
        workspace: MaterializedWorkspace,
        expected_lease_epoch: int,
        expected_launch_attempt: int,
    ) -> list[dict[str, Any]]:
        """Read only successful terminal Franta MCP results from the call audit."""

        safe_call_id = _safe_component(call_id, "call ID")
        state_dir = getattr(self.transport, "state_dir", None)
        if state_dir is None:
            raise RuntimeErrorBase("transport does not expose scheduler-private state")
        path = Path(state_dir) / "calls" / f"{safe_call_id}.jsonl"
        if not path.exists():
            return []
        if path.is_symlink() or not path.is_file():
            raise InvalidAgentOutput("Codex call audit is unsafe")
        try:
            tool_name = STAGING_TOOL_BY_SKILL[skill]
        except KeyError as exc:
            raise InvalidAgentOutput(f"unknown staged skill: {skill}") from exc
        active_generation = self._workspace_generation(workspace)
        launch_allows_tool = False
        launch_index = 0
        results: list[dict[str, Any]] = []
        for raw_line in path.read_bytes().splitlines():
            try:
                row = json.loads(raw_line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            if not isinstance(row, Mapping):
                continue
            if row.get("type") == "transport.call_started":
                launch_index += 1
                tools = row.get("broker_tools")
                generation_matches = (
                    row.get("workspace_generation") == active_generation
                    and row.get("lease_epoch") == int(expected_lease_epoch)
                    and row.get("launch_attempt")
                    == int(expected_launch_attempt)
                )
                launch_allows_tool = (
                    row.get("call_id") == call_id
                    and generation_matches
                    and isinstance(tools, Sequence)
                    and not isinstance(tools, (str, bytes, bytearray))
                    and tool_name in tools
                )
                continue
            if row.get("type") != "transport.codex_event" or not launch_allows_tool:
                continue
            event = row.get("event")
            if not isinstance(event, Mapping) or event.get("type") not in {
                "item.completed",
                "item_completed",
            }:
                continue
            item = event.get("item")
            item_error = item.get("error") if isinstance(item, Mapping) else None
            if not isinstance(item, Mapping) or (
                item.get("type") != "mcp_tool_call"
                or item.get("server") != "franta"
                or item.get("tool") != tool_name
                or str(item.get("status") or "").casefold()
                not in {"completed", "complete", "succeeded", "success"}
                or (item_error is not None and item_error != "")
            ):
                continue
            arguments = item.get("arguments")
            raw_result = item.get("result")
            if not isinstance(arguments, Mapping) or not isinstance(raw_result, Mapping):
                continue
            if skill in {
                "task-writing",
                "selection-report",
                "record-progress",
                "discovery-sprint",
                "record-scratch",
                "record-summary",
            }:
                if set(arguments) != {"payload"} or not isinstance(
                    arguments.get("payload"), Mapping
                ):
                    continue
                payload = dict(arguments["payload"])
            else:
                payload = dict(arguments)
            structured = raw_result.get(
                "structured_content", raw_result.get("structuredContent")
            )
            result = structured.get("result") if isinstance(structured, Mapping) else None
            if not isinstance(result, Mapping):
                continue
            artifact = result.get("artifact")
            operation_id = str(result.get("operation_id") or "")
            staged_path = result.get("staged_path")
            item_id = item.get("id")
            if (
                not operation_id
                or not isinstance(artifact, Mapping)
                or artifact.get("skill") != skill
                or str(artifact.get("operation_id") or "") != operation_id
                or not isinstance(staged_path, str)
                or not staged_path
                or not isinstance(item_id, str)
                or not item_id
            ):
                continue
            results.append(
                {
                    "item_id": item_id,
                    "launch_index": launch_index,
                    "operation_id": operation_id,
                    "staged_path": str(Path(staged_path).resolve()),
                    "artifact_payload_sha256": stable_digest(dict(artifact)),
                    "payload_sha256": stable_digest(payload),
                    "result_sha256": stable_digest(dict(result)),
                }
            )
        return results

    @staticmethod
    def _validation_kind(call_state: Mapping[str, Any]) -> str:
        continuation = call_state.get("continuation")
        if (
            call_state.get("kind") == "trimmer"
            and isinstance(continuation, Mapping)
            and continuation.get("phase") == "review"
        ):
            return "trimmer-review"
        return str(call_state.get("kind") or "")

    def _invoke_agent(self, call: AgentCall, *, root_fact_id: str | None = None) -> dict[str, Any]:
        binding = None
        staging_skills = allowed_skills(call.policy) & {
            "task-writing",
            "selection-report",
            "record-progress",
            "CAS",
            "human-guidance",
            "discovery-sprint",
            "record-scratch",
            "record-summary",
        }
        configured_cas = self._configured_cas_executables()
        if not configured_cas:
            staging_skills = staging_skills - {"CAS"}
        if not self.config["tools"].get("tectonic"):
            staging_skills = staging_skills - {"human-guidance"}
        if (
            call.policy.project_memory_api
            or call.policy.dependency_closure_only
            or call.policy.task_summary_api
            or call.policy.task_artifact_api
            or staging_skills
        ):
            skill_runtime = SkillRuntime(SkillContext.load(call.workspace.path))
            skill_context = skill_runtime.context
            timeout_value = self.config.get("timeouts", {}).get("agent_call_seconds")
            tool_timeout = float(timeout_value) if timeout_value is not None else None
            denied_tool_paths = [
                child
                for child in self.layout.root.iterdir()
                if child.resolve() != self.layout.workspaces.resolve()
            ]
            denied_tool_paths.extend(
                sibling
                for sibling in self.layout.workspaces.iterdir()
                if sibling.resolve() != call.workspace.path.resolve()
            )
            binding_holder: dict[str, str] = {}
            cas_processes = CASProcessScope()

            def stage_with_receipt(
                skill: str, payload: Mapping[str, Any], *, deadline: float | None = None
            ) -> Mapping[str, Any]:
                if skill == "CAS":
                    staged = execute_cas(
                        skill_context,
                        payload,
                        configured_executables=configured_cas,
                        timeout_seconds=tool_timeout,
                        denied_paths=denied_tool_paths,
                        process_scope=cas_processes,
                        deadline=deadline,
                    )
                elif skill == "human-guidance":
                    staged = compile_human_guidance(
                        skill_context,
                        payload,
                        tectonic_executable=self.config["tools"].get("tectonic"),
                        timeout_seconds=tool_timeout,
                        denied_paths=denied_tool_paths,
                    )
                else:
                    staged = skill_runtime.invoke(skill, payload)
                token = binding_holder.get("token")
                if token is None:
                    raise RuntimeErrorBase("broker staging capability is not bound")
                recorded = self._record_broker_skill_result(
                    call=call,
                    skill=skill,
                    payload=payload,
                    result=staged,
                    capability_token=token,
                )
                if skill == "CAS" and not bool(
                    staged.get("execution_succeeded")
                ):
                    artifact = staged.get("artifact", {})
                    raise SkillRuntimeError(
                        "CAS execution failed with exit status "
                        f"{artifact.get('exit_status')}; failed run retained as "
                        f"{staged.get('operation_id')}"
                    )
                return recorded

            binding = self.broker.issue(
                call.policy,
                caller_id=call.call_id,
                root_fact_id=root_fact_id,
                explorer_api=self._explorer_api_for_call(call),
                staging_handler=stage_with_receipt,
                staging_skills=staging_skills,
                cas_software_names=(
                    sorted(configured_cas) if "CAS" in staging_skills else ()
                ),
                cas_handler=(
                    lambda payload, deadline: stage_with_receipt(
                        "CAS", payload, deadline=deadline
                    )
                ) if "CAS" in staging_skills else None,
                on_revoke=cas_processes.close if "CAS" in staging_skills else None,
            )
            binding_holder["token"] = binding.token
        try:
            if self.executor is not None:
                value = self.executor(call)
                if not isinstance(value, Mapping):
                    raise InvalidAgentOutput("test executor returned a non-object")
                return copy.deepcopy(dict(value))
            request = CodexRequest(
                call_id=call.call_id,
                role=call.role if call.kind != "worker" else "worker",
                prompt=call.prompt,
                workspace=call.workspace.path,
                policy=call.policy,
                permission_profile=self._permission_profile(call.policy),
                session_key=call.session_key,
                resume=call.resume,
                broker_binding=binding,
                timeout_seconds=(
                    int(self.config["explorer"]["attempt_seconds"])
                    if call.kind == "explorer-worker"
                    else WORKER_CALL_TIMEOUT_SECONDS
                    if call.kind == "worker"
                    else None
                ),
                output_schema=call.output_schema,
                model_config=call.model_config,
                lease_epoch=call.lease_epoch,
                launch_attempt=call.launch_attempt,
            )
            result = self.transport.invoke(request)
            return _json_object(result.final_message)
        finally:
            if binding is not None:
                self.broker.revoke(binding)

    @staticmethod
    def _validate_control_result(kind: str, value: Mapping[str, Any]) -> None:
        if kind == "main" and value.get("decision") not in {
            "assignments", "wait_for_results", "terminal"
        }:
            raise InvalidAgentOutput("main returned an invalid decision")
        if kind == "advisor-proposal" and (
            value.get("stage") != "proposal"
            or value.get("call_ended") is not True
        ):
            raise InvalidAgentOutput("Advisor proposal returned an invalid decision")
        if kind == "advisor-finalize" and (
            value.get("stage") != "finalize"
            or value.get("call_ended") is not True
        ):
            raise InvalidAgentOutput("Advisor finalization returned an invalid decision")
        if kind == "trimmer-review" and value.get("decision") not in {"no_trim", "trim"}:
            raise InvalidAgentOutput("trimmer review returned an invalid decision")
        if kind == "trimmer" and value.get("decision") not in {
            "commit", "human_guidance", "discovery_sprint"
        }:
            raise InvalidAgentOutput("trimmer returned an invalid decision")
        if kind == "synthesizer" and value.get("resolution") not in {
            "new", "duplicate", "update"
        }:
            raise InvalidAgentOutput("synthesizer returned an invalid resolution")
        if kind == "verifier" and value.get("verdict") not in {"correct", "incorrect"}:
            raise InvalidAgentOutput("verifier returned an invalid verdict")
        if kind == "challenge-verifier" and value.get("resolution") not in {
            "confirmed_invalid", "challenge_rejected", "inconclusive"
        }:
            raise InvalidAgentOutput("fact-challenge verifier returned an invalid resolution")
        if kind == "main-closure-review" and value.get("outcome") not in {
            "finished", "progress", "failed"
        }:
            raise InvalidAgentOutput("main closure review returned an invalid outcome")
        if kind == "explorer-worker":
            if value.get("attempt_ended") is not True:
                raise InvalidAgentOutput("Explorer normal exit must end its attempt")
            if value.get("stop_reason") not in {
                "attempt_complete",
                "time_limit",
                "root_candidate",
            }:
                raise InvalidAgentOutput("Explorer returned an invalid stop reason")
            candidate_id = value.get("root_candidate_scratch_id")
            candidate_outcome = value.get("root_candidate_outcome")
            if (candidate_id is None) != (candidate_outcome is None):
                raise InvalidAgentOutput(
                    "Explorer root candidate ID and outcome must be supplied together"
                )
            if candidate_outcome not in {None, "proved", "disproved"}:
                raise InvalidAgentOutput("Explorer returned an invalid root outcome")
            if (candidate_id is not None) != (
                value.get("stop_reason") == "root_candidate"
            ):
                raise InvalidAgentOutput(
                    "Explorer root-candidate signal must use stop_reason=root_candidate"
                )
        if kind == "main-sort" and value.get("sort_ended") is not True:
            raise InvalidAgentOutput("main-sort normal exit must set sort_ended=true")

    def _run_prepared_call(
        self,
        call_id: str,
        *,
        workspace: MaterializedWorkspace,
        policy: AccessPolicy,
        mode: str | None = None,
        session_key: str | None = None,
        resume: bool = False,
        root_fact_id: str | None = None,
        pre_accept: Callable[[Mapping[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        while True:
            advisor_semantic_failure = False
            call_state = self.scheduler.state["calls"][call_id]
            if call_state["status"] == CallState.COMPLETED.value:
                return copy.deepcopy(dict(call_state["result"]))
            if (
                call_state["kind"] == "main"
                and call_state["input"].get("human_guidance") is not None
                and call_state["status"] == CallState.RETRY_PENDING.value
                and any((workspace.outbox_path / "task-writing").glob("*.json"))
            ):
                # A rejected guided assignment is immutable. A retry, including
                # after restart, needs fresh staging with the same frozen input.
                self._retire_invalid_control_workspace(call_id, workspace)
                workspace = self._make_workspace(
                    call_id=call_id, policy=policy, context=call_state["input"]
                )
            lease_epoch, _ = self.scheduler.mark_call_running(call_id)
            spec = self._call_spec(
                call_id,
                workspace=workspace,
                policy=policy,
                mode=mode,
                session_key=session_key,
                resume=resume,
            )
            try:
                if spec.kind == "main-sort":
                    task_id = str(
                        call_state.get("continuation", {}).get("task_id") or ""
                    )
                    task = self.scheduler.state.get("tasks", {}).get(task_id)
                    if (
                        not isinstance(task, Mapping)
                        or self.explorer_repository is None
                    ):
                        raise RuntimeErrorBase(
                            "main-sort call lacks its frozen Explorer snapshot identity"
                        )
                    card = self._enrich_task_card(
                        task, int(task.get("current_attempt", 1)), None
                    )
                    snapshot = build_main_sort_explorer_snapshot(
                        self.explorer_repository,
                        card,
                    )
                    self._validate_main_sort_workspace(
                        workspace,
                        policy=policy,
                        task_card=card,
                        explorer_snapshot=snapshot,
                        context=None,
                    )
                value = self._invoke_agent(spec, root_fact_id=root_fact_id)
                try:
                    self._validate_control_result(
                        self._validation_kind(call_state), value
                    )
                    if pre_accept is not None:
                        pre_accept(value)
                except Exception:
                    # Advisor proposal artifacts are immutable and bound to a
                    # particular launch generation.  Preserve a returned but
                    # invalid value as an audited invalid result so retry can
                    # fence that generation and start from a fresh workspace.
                    if is_advisor_call_kind(spec.kind):
                        self.scheduler.accept_call_result(
                            call_id, lease_epoch, value
                        )
                        advisor_semantic_failure = True
                    raise
                self.scheduler.accept_call_result(call_id, lease_epoch, value)
                return value
            except Exception as exc:
                current_status = self.scheduler.state["calls"].get(call_id, {}).get(
                    "status"
                )
                if current_status in {
                    CallState.CANCELLED.value,
                    CallState.SUPERSEDED.value,
                }:
                    raise TransportFailure(
                        f"call {call_id} was fenced while its process was exiting"
                    ) from exc
                can_retry = (
                    self.scheduler.reject_call_result(call_id, str(exc))
                    if advisor_semantic_failure
                    else self.scheduler.mark_call_failed(
                        call_id, lease_epoch, str(exc)
                    )
                )
                if spec.kind == "advisor-proposal":
                    # A proposal may have staged the immutable report before
                    # returning a malformed final response.  Retire that exact
                    # launch generation even when its retry budget is now
                    # exhausted; an operator-authorized retry must never
                    # rematerialize on top of stale outbox/artifact files.
                    self._retire_invalid_control_workspace(call_id, workspace)
                if not can_retry:
                    raise TransportFailure(f"retry limit exhausted for {call_id}") from exc
                if spec.kind == "main":
                    resume = self._main_session_resume(call_id)
                elif is_advisor_call_kind(spec.kind):
                    if spec.kind == "advisor-proposal":
                        workspace = self._advisor_workspace(
                            call_id, stage="proposal"
                        )
                    resume = self._advisor_session_resume(call_id)
                else:
                    resume = bool(
                        session_key and self.transport.ledger.resolve(session_key)
                    )

    # -------------------------------------------------------------- artifacts

    def _call_receipt_generation(self, call_id: str) -> tuple[int, int]:
        """Return the exact persisted lease and launch attempt for one call."""

        scheduler = getattr(self, "scheduler", None)
        if scheduler is None:
            # Narrow unit drivers do not own scheduler state. Production
            # runtimes always take the exact generation from the scheduler.
            return 1, 1
        call = scheduler.state.get("calls", {}).get(call_id) if scheduler else None
        if not isinstance(call, Mapping):
            raise RuntimeErrorBase(f"unknown call receipt generation: {call_id}")
        lease_epoch = int(call.get("lease_epoch", 0))
        launch_attempt = int(call.get("attempt", 0))
        if lease_epoch <= 0 or launch_attempt <= 0:
            raise RuntimeErrorBase(f"invalid call receipt generation: {call_id}")
        return lease_epoch, launch_attempt

    @staticmethod
    def _skill_artifact_snapshot(
        workspace: MaterializedWorkspace, skill: str
    ) -> list[tuple[Path, dict[str, Any]]]:
        """Read a skill artifact snapshot from one directory enumeration."""

        directory = workspace.outbox_path / skill
        if not directory.is_dir() or directory.is_symlink():
            return []
        snapshot: list[tuple[Path, dict[str, Any]]] = []
        for path in sorted(directory.glob("*.json")):
            if path.is_symlink() or path.parent.resolve() != directory.resolve():
                raise InvalidAgentOutput("outbox artifact escaped its skill directory")
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise InvalidAgentOutput(f"invalid {skill} artifact {path.name}") from exc
            if not isinstance(value, Mapping) or value.get("skill") != skill:
                raise InvalidAgentOutput(f"malformed {skill} artifact {path.name}")
            snapshot.append((path, copy.deepcopy(dict(value))))
        return snapshot

    @classmethod
    def _skill_artifacts(
        cls, workspace: MaterializedWorkspace, skill: str
    ) -> list[dict[str, Any]]:
        return [value for _, value in cls._skill_artifact_snapshot(workspace, skill)]

    @staticmethod
    def _trusted_references_match(
        workspace: MaterializedWorkspace, receipt: Mapping[str, Any]
    ) -> bool:
        references = receipt.get("referenced_artifacts")
        if not isinstance(references, list):
            return False
        workspace_root = workspace.path.resolve()
        for value in references:
            if not isinstance(value, Mapping) or set(value) != {
                "kind",
                "relative_path",
                "sha256",
            }:
                return False
            relative = value.get("relative_path")
            digest = value.get("sha256")
            if (
                not isinstance(relative, str)
                or not relative
                or not isinstance(digest, str)
                or len(digest) != 64
            ):
                return False
            path_value = Path(relative)
            if path_value.is_absolute() or ".." in path_value.parts:
                return False
            path = workspace.path / path_value
            try:
                resolved = path.resolve(strict=True)
            except OSError:
                return False
            if (
                path.is_symlink()
                or not resolved.is_file()
                or (workspace_root != resolved and workspace_root not in resolved.parents)
                or hashlib.sha256(resolved.read_bytes()).hexdigest() != digest
            ):
                return False
        return True

    def _verified_skill_artifacts(
        self,
        workspace: MaterializedWorkspace,
        skill: str,
        call_id: str,
        *,
        expected_lease_epoch: int,
        expected_launch_attempt: int,
        allow_pending: bool = False,
    ) -> list[dict[str, Any]]:
        """Bind staged files one-to-one to private, completed MCP results.

        The workspace ``skill_activity.jsonl`` remains useful diagnostic and
        archive evidence, but it is agent-writable and is never a trust root.
        The scheduler-private broker receipt is the completion boundary: it is
        appended only after staging succeeds.  A terminal Codex MCP event, when
        present, must corroborate it, but is not required because the Codex
        process may disconnect after the broker has durably completed the call.
        """

        # Staging publishes complete files with an atomic rename.  Anything
        # published after this snapshot belongs to the next live poll; a
        # second enumeration here would misclassify that append as tampering.
        snapshot = self._skill_artifact_snapshot(workspace, skill)
        values = [value for _, value in snapshot]
        # Deterministic executors are scheduler-injected test infrastructure;
        # they do not traverse MCP and are trusted directly by construction.
        if self.executor is not None:
            for path, value in snapshot:
                operation_id = str(value.get("operation_id") or "")
                if path.name != operation_id.replace(":", "-") + ".json":
                    raise InvalidAgentOutput(f"{skill} artifact has the wrong filename")
            return values

        receipts = self._trusted_skill_results(
            call_id=call_id,
            skill=skill,
            workspace=workspace,
            expected_lease_epoch=expected_lease_epoch,
            expected_launch_attempt=expected_launch_attempt,
        )
        terminal_results = self._completed_mcp_skill_results(
            call_id=call_id,
            skill=skill,
            workspace=workspace,
            expected_lease_epoch=expected_lease_epoch,
            expected_launch_attempt=expected_launch_attempt,
        )
        verified: list[dict[str, Any]] = []
        used_receipts: set[str] = set()
        terminal_receipts: set[str] = set()
        terminal_items: set[tuple[int, str]] = set()
        for result in terminal_results:
            matches = [
                receipt
                for receipt in receipts
                if receipt.get("operation_id") == result.get("operation_id")
                and receipt.get("staged_path") == result.get("staged_path")
                and receipt.get("artifact_payload_sha256")
                == result.get("artifact_payload_sha256")
                and receipt.get("payload_sha256") == result.get("payload_sha256")
                and receipt.get("result_sha256") == result.get("result_sha256")
            ]
            item_key = (int(result["launch_index"]), str(result["item_id"]))
            if len(matches) != 1 or item_key in terminal_items:
                raise InvalidAgentOutput(
                    f"{skill} terminal MCP result does not match one private broker receipt"
                )
            receipt_key = str(matches[0]["receipt_sha256"])
            if receipt_key in terminal_receipts:
                raise InvalidAgentOutput(
                    f"{skill} private broker receipt is reused by terminal MCP results"
                )
            terminal_receipts.add(receipt_key)
            terminal_items.add(item_key)
        for path, value in snapshot:
            operation_id = str(value.get("operation_id") or "")
            expected_name = operation_id.replace(":", "-") + ".json"
            relative = path.relative_to(workspace.path).as_posix()
            resolved = str(path.resolve())
            data = path.read_bytes()
            digest = hashlib.sha256(data).hexdigest()
            payload_digest = stable_digest(value)
            private_matches = [
                receipt
                for receipt in receipts
                if str(receipt.get("operation_id") or "") == operation_id
                and receipt.get("relative_path") == relative
                and receipt.get("staged_path") == resolved
                and receipt.get("artifact_sha256") == digest
                and receipt.get("artifact_payload_sha256") == payload_digest
                and isinstance(receipt.get("capability_sha256"), str)
                and len(str(receipt.get("capability_sha256"))) == 64
                and all(
                    character in "0123456789abcdef"
                    for character in str(receipt.get("capability_sha256"))
                )
                and self._trusted_references_match(workspace, receipt)
            ]
            if path.name == expected_name and len(private_matches) == 1:
                private_key = str(private_matches[0]["receipt_sha256"])
                if private_key in used_receipts:
                    raise InvalidAgentOutput(
                        f"{skill} artifact {path.name} reuses trusted staging evidence"
                    )
                used_receipts.add(private_key)
                verified.append(value)
                continue
            if allow_pending:
                # While a worker is still running, the file may become visible
                # just before its private broker receipt.  It remains
                # ineligible until that scheduler-private completion exists.
                continue
            if path.name != expected_name or len(private_matches) != 1:
                raise InvalidAgentOutput(
                    f"{skill} artifact {path.name} lacks one matching private broker result"
                )
        if not allow_pending and (
            len(receipts) != len(values)
            or len(verified) != len(values)
        ):
            raise InvalidAgentOutput(
                f"{skill} staged artifacts and private broker results are not one-to-one"
            )
        return verified

    def _archive_workspace_artifacts(
        self, task_id: str, call_id: str, workspace: MaterializedWorkspace
    ) -> list[dict[str, Any]]:
        target_root = self.layout.task_archive / task_id / call_id
        references: list[dict[str, Any]] = []
        for source_root_name in ("outbox", "artifacts"):
            source_root = workspace.path / source_root_name
            if not source_root.exists():
                continue
            for source in sorted(source_root.rglob("*")):
                if not source.is_file() or source.is_symlink():
                    continue
                relative = Path(source_root_name) / source.relative_to(source_root)
                data = source.read_bytes()
                digest = hashlib.sha256(data).hexdigest()
                # The skill audit is append-only while an attempt runs.  Keep
                # content-addressed snapshots instead of treating its changing
                # tail as mutation of one immutable archived file.
                if relative == Path("outbox/skill_activity.jsonl"):
                    relative = Path("outbox/skill_activity") / f"{digest}.jsonl"
                target = target_root / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.exists():
                    if hashlib.sha256(target.read_bytes()).hexdigest() != digest:
                        raise InvalidAgentOutput(
                            f"archived artifact changed for {relative.as_posix()}"
                        )
                else:
                    atomic_write_bytes(target, data, mode=0o600)
                references.append(
                    {
                        "relative_path": str(target.relative_to(self.layout.task_archive)),
                        "sha256": digest,
                        "kind": source_root_name,
                    }
                )
        if references:
            self.scheduler.register_task_artifacts(task_id, references)
        return references

    def _archive_human_guidance_report(
        self,
        call_id: str,
        workspace: MaterializedWorkspace,
        pdf_reference: str,
    ) -> str:
        """Persist one authenticated trimmer PDF outside its transient workspace."""

        relative = Path(pdf_reference)
        if relative.is_absolute() or ".." in relative.parts:
            raise InvalidAgentOutput("human-guidance report escaped its workspace")
        source = workspace.path / relative
        try:
            resolved = source.resolve(strict=True)
            artifacts = workspace.artifacts_path.resolve(strict=True)
        except OSError as exc:
            raise InvalidAgentOutput("human-guidance report is missing") from exc
        if (
            source.is_symlink()
            or not resolved.is_file()
            or (resolved != artifacts and artifacts not in resolved.parents)
        ):
            raise InvalidAgentOutput("human-guidance report is unsafe")
        data = resolved.read_bytes()
        if not data.startswith(b"%PDF-"):
            raise InvalidAgentOutput("human-guidance report is not a PDF")
        digest = hashlib.sha256(data).hexdigest()
        archive_root = self.layout.private / "human-guidance"
        if archive_root.exists() and archive_root.is_symlink():
            raise InvalidAgentOutput("human-guidance archive is unsafe")
        archive_root.mkdir(mode=0o700, exist_ok=True)
        call_root = archive_root / _safe_component(call_id, "call ID")
        if call_root.exists() and call_root.is_symlink():
            raise InvalidAgentOutput("human-guidance call archive is unsafe")
        call_root.mkdir(mode=0o700, exist_ok=True)
        target = call_root / f"{digest}.pdf"
        if target.exists():
            if (
                target.is_symlink()
                or not target.is_file()
                or hashlib.sha256(target.read_bytes()).hexdigest() != digest
            ):
                raise InvalidAgentOutput("human-guidance archive changed")
        else:
            atomic_write_bytes(target, data, mode=0o600)
        return target.relative_to(self.layout.root).as_posix()

    @staticmethod
    def _assignment_report(artifact: Mapping[str, Any]) -> dict[str, Any]:
        report = copy.deepcopy(dict(artifact))
        report.pop("skill", None)
        report.setdefault("report_id", str(report.get("operation_id") or ""))
        report["mode"] = str(report.get("mode") or report.get("work_mode") or "")
        report["portfolio"] = copy.deepcopy(
            report.get("portfolio", report.get("assignment_portfolio", {})) or {}
        )
        if "perspective" not in report:
            report["perspective"] = report.get("selected_new_perspective")
        return report

    @staticmethod
    def _computation_record(artifact: Mapping[str, Any]) -> dict[str, Any]:
        value = copy.deepcopy(dict(artifact))
        value.pop("skill", None)
        # CAS launcher controls are archived with the raw artifact but are not
        # fields of the canonical computation record.
        value.pop("arguments", None)
        value.pop("version_arguments", None)
        software = value.get("software")
        if isinstance(software, str):
            value["software"] = {
                "name": software,
                "version": str(value.pop("software_version", "unknown")),
            }
        if "output" not in value and "exact_output" in value:
            value["output"] = value.pop("exact_output")
        if "related_memory_ids" not in value and "related_ids" in value:
            value["related_memory_ids"] = value.pop("related_ids")
        value.setdefault("staging_id", value.get("operation_id"))
        value.setdefault("fact_candidate_operation_ids", [])
        return value

    @staticmethod
    def _computation_execution_succeeded(artifact: Mapping[str, Any]) -> bool:
        raw_exit_status = artifact.get("exit_status")
        return (
            not isinstance(raw_exit_status, bool)
            and str(raw_exit_status).strip() == "0"
        )

    def _progress_artifacts(
        self,
        workspace: MaterializedWorkspace,
        *,
        verified_progress: Sequence[Mapping[str, Any]] | None = None,
        verified_computations: Sequence[Mapping[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        progress = (
            [copy.deepcopy(dict(item)) for item in verified_progress]
            if verified_progress is not None
            else self._skill_artifacts(workspace, "record-progress")
        )
        progress.sort(key=lambda item: (int(item.get("attempt", 0)), int(item.get("sequence", 0))))
        computations: dict[str, dict[str, Any]] = {}
        computation_artifacts = (
            [copy.deepcopy(dict(value)) for value in verified_computations]
            if verified_computations is not None
            else self._skill_artifacts(workspace, "CAS")
        )
        for artifact in computation_artifacts:
            operation_id = str(artifact.get("operation_id") or "")
            if not operation_id:
                raise InvalidAgentOutput("CAS artifact has no operation ID")
            if operation_id in computations:
                raise InvalidAgentOutput(f"duplicate CAS operation ID {operation_id}")
            computations[operation_id] = self._computation_record(artifact)
        for item in progress:
            if "computations" in item:
                raise InvalidAgentOutput(
                    "record-progress cannot supply inline computation bodies"
                )
            requested = item.pop("computation_operation_ids", [])
            if (
                not isinstance(requested, list)
                or not all(isinstance(operation_id, str) and operation_id for operation_id in requested)
                or len(set(requested)) != len(requested)
            ):
                raise InvalidAgentOutput(
                    "computation_operation_ids must be a duplicate-free list of string IDs"
                )
            attached: list[dict[str, Any]] = []
            for operation_id in requested or []:
                if str(operation_id) not in computations:
                    raise InvalidAgentOutput(f"progress references unknown CAS artifact {operation_id}")
                computation = computations[str(operation_id)]
                if not self._computation_execution_succeeded(computation):
                    raise InvalidAgentOutput(
                        f"progress references failed CAS artifact {operation_id}"
                    )
                attached.append(computation)
            item["computations"] = attached
        for item in progress:
            item.pop("skill", None)
        return progress

    def _main_sort_progress_artifacts(
        self,
        workspace: MaterializedWorkspace,
        *,
        task_id: str,
        sort_run_id: str,
        verified_progress: Sequence[Mapping[str, Any]],
    ) -> list[dict[str, Any]]:
        """Resolve cross-call Explorer CAS references from private receipts.

        A main-sort model supplies only an XCAS evidence ID and selected ES
        source IDs.  The exact software/input/output bytes come from the
        private Explorer registry and are rebound to the scheduler-owned sort
        task here; a model-provided computation body is never accepted.
        """

        repository = self.explorer_repository
        if repository is None:
            raise RuntimeErrorBase("main-sort requires the Explorer repository")
        progress = self._progress_artifacts(
            workspace,
            verified_progress=verified_progress,
            verified_computations=(),
        )
        for item in progress:
            requested = item.pop("explorer_computation_promotions", [])
            if not isinstance(requested, list):
                raise InvalidAgentOutput(
                    "Explorer computation promotions must be a list"
                )
            computations = list(item.get("computations", []))
            for declaration in requested:
                if not isinstance(declaration, Mapping):
                    raise InvalidAgentOutput(
                        "Explorer computation promotion must be an object"
                    )
                evidence_id = str(declaration.get("evidence_id") or "")
                source_ids = declaration.get("source_record_ids")
                if not isinstance(source_ids, list):
                    raise InvalidAgentOutput(
                        "Explorer computation promotion lacks source IDs"
                    )
                computation = repository.resolve_computation_for_sort(
                    sort_run_id,
                    evidence_id,
                    [str(source_id) for source_id in source_ids],
                    task_id,
                )
                computation["explorer_provenance"] = {
                    "sort_run_id": sort_run_id,
                    "source_record_ids": [str(source_id) for source_id in source_ids],
                    "evidence_id": evidence_id,
                }
                computations.append(computation)
            item["computations"] = computations
        return progress

    def _stage_main_sort_promotions(
        self,
        *,
        sort_run_id: str,
        progress: Sequence[Mapping[str, Any]],
    ) -> None:
        repository = self.explorer_repository
        if repository is None:
            raise RuntimeErrorBase("main-sort requires the Explorer repository")
        for record in progress:
            for raw_operation in record.get("operations", []):
                if not isinstance(raw_operation, Mapping):
                    raise InvalidAgentOutput("main-sort operation must be an object")
                operation = copy.deepcopy(dict(raw_operation))
                provenance = operation.get("explorer_provenance")
                if not isinstance(provenance, Mapping):
                    raise InvalidAgentOutput(
                        "main-sort operation lacks Explorer provenance"
                    )
                repository.stage_promotion(
                    sort_run_id,
                    str(operation.get("operation_id") or ""),
                    str(operation.get("kind") or operation.get("operation_type") or ""),
                    [str(item) for item in provenance.get("source_record_ids", [])],
                    stable_digest(operation),
                )
            for raw_computation in record.get("computations", []):
                if not isinstance(raw_computation, Mapping):
                    raise InvalidAgentOutput("main-sort computation must be an object")
                computation = copy.deepcopy(dict(raw_computation))
                provenance = computation.get("explorer_provenance")
                if not isinstance(provenance, Mapping):
                    raise InvalidAgentOutput(
                        "main-sort computation lacks Explorer provenance"
                    )
                staging_id = str(
                    computation.get("staging_id")
                    or computation.get("operation_id")
                    or ""
                )
                repository.stage_promotion(
                    sort_run_id,
                    staging_id,
                    "computation",
                    [str(item) for item in provenance.get("source_record_ids", [])],
                    stable_digest(computation),
                )

    def _reconcile_explorer_promotions(
        self, *, sort_run_id: str | None = None
    ) -> bool:
        """Mirror terminal scheduler resolutions into the append-only ledger."""

        repository = self.explorer_repository
        if repository is None:
            return False
        changed = False
        state = self.scheduler.state
        for promotion in repository.list_received_promotions(
            sort_run_id=sort_run_id
        ):
            if promotion.target_kind == "computation":
                item = state.get("computations", {}).get(
                    promotion.franta_operation_id
                )
                resolution = "published"
            else:
                item = state.get("operations", {}).get(
                    promotion.franta_operation_id
                )
                synthesis = item.get("synthesizer_result", {}) if item else {}
                resolution = str(synthesis.get("resolution") or "published")
            if not isinstance(item, Mapping):
                continue
            item_state = str(item.get("state") or "")
            if item_state == OperationState.COMMITTED.value:
                canonical_id = item.get("canonical_id")
                if not isinstance(canonical_id, str) or not canonical_id:
                    continue
                repository.resolve_promotion(
                    promotion.franta_operation_id,
                    "committed",
                    canonical_id=canonical_id,
                    resolution=resolution,
                )
            elif item_state == OperationState.REJECTED.value:
                repository.resolve_promotion(
                    promotion.franta_operation_id,
                    "rejected",
                    resolution=resolution,
                    error=str(item.get("error") or "promotion rejected"),
                )
            elif item_state == OperationState.ABANDONED.value:
                repository.resolve_promotion(
                    promotion.franta_operation_id,
                    "abandoned",
                    resolution=resolution,
                    error=str(item.get("error") or "promotion abandoned"),
                )
            elif item_state == OperationState.NEEDS_ATTENTION.value:
                repository.resolve_promotion(
                    promotion.franta_operation_id,
                    "needs_attention",
                    resolution=resolution,
                    error=str(item.get("error") or "promotion needs attention"),
                )
            else:
                continue
            changed = True
        return changed

    # -------------------------------------------------------------- contexts

    @staticmethod
    def _category_summary(category: Mapping[str, Any]) -> dict[str, Any]:
        return {
            key: copy.deepcopy(category.get(key))
            for key in (
                "id", "revision", "status", "name", "description", "main_progress",
                "current_obstacles", "members", "member_entries",
            )
        }

    def _task_summary(self, task: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "task_id": task["task_id"],
            "mode": task["task_card"]["mode"],
            "objective": task["task_card"].get("objective"),
            "state": task["state"],
            "final_status": task.get("final_status"),
            "final_summary": copy.deepcopy(task.get("final_summary")),
            "canonical_changes": [
                {
                    "operation_id": operation_id,
                    "state": self.scheduler.state["operations"].get(operation_id, {}).get("state"),
                    "canonical_id": self.scheduler.state["operations"].get(operation_id, {}).get("canonical_id"),
                }
                for operation_id in task.get("operation_ids", [])
            ],
        }

    def _main_context(self, reserved_batch_id: str) -> dict[str, Any]:
        state, memory_snapshot = self._new_main_memory_snapshot()
        checkpoint = int(state["main_checkpoint"].get("event_cursor", 0))
        event_cursor = int(memory_snapshot.source_event_cursor or 0)
        self.read_audit.append(
            {
                "action": "main_memory_snapshot_issued",
                "caller_id": reserved_batch_id,
                "snapshot_id": memory_snapshot.snapshot_id,
                "snapshot_digest": memory_snapshot.snapshot_digest,
                "source_event_cursor": event_cursor,
                "record_count": memory_snapshot.record_count,
                "allowed": True,
            }
        )
        completed_ids = {
            event["payload"]["task_id"]
            for event in state["events"]
            if int(event["event_id"]) > checkpoint
            and event["type"] == "task_closed"
            and event.get("payload", {}).get("task_id")
        }
        return {
            "root_problem": self.scheduler.effective_problem()["problem_text"],
            **(
                {"problem_assignment": self.scheduler.effective_problem()}
                if self.advisor_program is not None
                else {}
            ),
            "root": copy.deepcopy(state["root"]),
            "foundation_policy": _foundation(self.config),
            "reserved_batch_id": reserved_batch_id,
            "free_non_verifier_slots": self.scheduler.free_non_verifier_slots(),
            "new_task_summaries": [
                self._task_summary(state["tasks"][task_id]) for task_id in sorted(completed_ids)
            ],
            "running_task_cards": [
                copy.deepcopy(task["task_card"])
                for task in state["tasks"].values()
                if task["state"] != TaskState.CLOSED.value
            ],
            "seed_theorems": json.loads(
                (self.layout.private / "seed-theorems.json").read_text(encoding="utf-8")
            ),
            "event_cursor": event_cursor,
            "memory_snapshot": self._main_memory_snapshot_descriptor(
                memory_snapshot
            ),
        }

    def _advisor_context(self) -> AdvisorCycleContext:
        """Freeze all Franta memory and prior assignments for Advisor i."""

        state, memory_snapshot = self._new_advisor_memory_snapshot()
        phase = state.get("phase_control") or {}
        source_cycle = int(phase.get("cycle", 0))
        if source_cycle < 1 or phase.get("phase") != "franta_drain":
            raise RuntimeErrorBase("Advisor context requires the active Franta drain")
        control = state.get("advisor_control") or {}
        previous = [
            copy.deepcopy(item["problem_assignment"])
            for item in control.get("history", [])
        ]
        descriptor = {
            "snapshot_id": memory_snapshot.snapshot_id,
            "snapshot_digest": memory_snapshot.snapshot_digest,
            "source_event_cursor": int(memory_snapshot.source_event_cursor or 0),
            "relative_path": MAIN_MEMORY_SNAPSHOT_RELATIVE_PATH.as_posix(),
        }
        self.read_audit.append(
            {
                "action": "advisor_memory_snapshot_issued",
                "caller_id": f"advisor:{source_cycle}",
                "snapshot_id": memory_snapshot.snapshot_id,
                "snapshot_digest": memory_snapshot.snapshot_digest,
                "source_event_cursor": memory_snapshot.source_event_cursor,
                "record_count": memory_snapshot.record_count,
                "allowed": True,
            }
        )
        return advisor_context(
            advisor_index=source_cycle,
            original_problem=str(self.config["root_problem"]),
            memory_snapshot=descriptor,
            previous_assignments=previous,
        )

    @staticmethod
    def _advisor_snapshot_revisions(
        snapshot: MainMemorySnapshot,
    ) -> dict[str, int]:
        """Read the revision identity from one already-authenticated catalog."""

        try:
            rows = [
                json.loads(line)
                for line in snapshot.catalog_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeErrorBase("Advisor memory catalog is unreadable") from exc
        revisions: dict[str, int] = {}
        for row in rows:
            if not isinstance(row, Mapping):
                raise RuntimeErrorBase("Advisor memory catalog has a malformed row")
            memory_id = row.get("id")
            revision = row.get("revision")
            if (
                not isinstance(memory_id, str)
                or not memory_id
                or memory_id in revisions
                or not isinstance(revision, int)
                or isinstance(revision, bool)
                or revision < 1
            ):
                raise RuntimeErrorBase("Advisor memory catalog has invalid revisions")
            revisions[memory_id] = revision
        if len(revisions) != snapshot.record_count:
            raise RuntimeErrorBase("Advisor memory catalog record count drifted")
        return revisions

    def _advisor_evidence_freshness(
        self,
        context: AdvisorCycleContext,
        current_snapshot: MainMemorySnapshot,
    ) -> dict[str, Any]:
        """Derive evidence freshness from each prior assignment's bound snapshot."""

        history = self.scheduler.advisor_state.get("history", [])
        if len(history) != len(context.previous_assignments):
            raise RuntimeErrorBase("Advisor assignment history drifted")
        previous_revisions: dict[str, Mapping[str, Any]] = {}
        for expected, raw_history in zip(
            context.previous_assignments, history, strict=True
        ):
            if not isinstance(raw_history, Mapping):
                raise RuntimeErrorBase("Advisor assignment history is malformed")
            try:
                persisted = ProblemAssignment.from_dict(
                    raw_history["problem_assignment"]
                )
                prior_context = AdvisorCycleContext.from_dict(raw_history["context"])
            except (KeyError, TypeError, ValueError) as exc:
                raise RuntimeErrorBase("Advisor assignment history is malformed") from exc
            if (
                persisted.digest != expected.digest
                or raw_history.get("context_digest") != prior_context.digest
            ):
                raise RuntimeErrorBase("Advisor assignment history binding drifted")
            prior_descriptor = prior_context.memory_snapshot
            try:
                prior_snapshot = recover_main_memory_snapshot(
                    self.layout.advisor_memory_snapshots
                    / prior_descriptor.snapshot_id,
                    expected_snapshot_id=prior_descriptor.snapshot_id,
                    expected_snapshot_digest=prior_descriptor.snapshot_digest,
                )
            except MainMemorySnapshotError as exc:
                raise RuntimeErrorBase(
                    "Advisor historical memory snapshot is unavailable"
                ) from exc
            previous_revisions[persisted.assignment_id] = (
                self._advisor_snapshot_revisions(prior_snapshot)
            )
        return advisor_breakthrough_evidence_freshness(
            context,
            current_revisions=self._advisor_snapshot_revisions(current_snapshot),
            previous_revisions=previous_revisions,
        )

    def _advisor_call_context(self, context: AdvisorCycleContext) -> dict[str, Any]:
        """Add host-authenticated snapshot metadata to the portable context."""

        snapshot_id = context.memory_snapshot.snapshot_id
        snapshot = recover_main_memory_snapshot(
            self.layout.advisor_memory_snapshots / snapshot_id,
            expected_snapshot_id=snapshot_id,
            expected_snapshot_digest=context.memory_snapshot.snapshot_digest,
        )
        descriptor = self._main_memory_snapshot_descriptor(snapshot)
        return {
            **context.to_dict(),
            "stage": "proposal",
            "root_problem": str(self.config["root_problem"]),
            "event_cursor": int(snapshot.source_event_cursor or 0),
            "host_memory_snapshot": descriptor,
            "breakthrough_evidence_freshness": self._advisor_evidence_freshness(
                context, snapshot
            ),
            "problem_assignment": self.scheduler.effective_problem(),
        }

    def _trimmer_context(self, *, phase: str) -> dict[str, Any]:
        state = self.scheduler.state
        configured_slots = int(state["limits"]["max_non_verifier_workers"])
        active_sprint = state["sprints"].get(state.get("active_sprint_id"))
        context = {
            "phase": phase,
            "root_problem": self.scheduler.effective_problem()["problem_text"],
            "root": copy.deepcopy(state["root"]),
            "foundation_policy": _foundation(self.config),
            "active_review": copy.deepcopy(state["trim"].get("active_review")),
            "active_trim": copy.deepcopy(state["trim"].get("active_trim")),
            "current_portfolio": self.categories.active_portfolio(),
            "all_category_summaries": [
                self._category_summary(item)
                for item in self.categories.list_categories(active_only=False)
            ],
            "task_summaries": [
                self._task_summary(task)
                for task in state["tasks"].values()
                if task["state"] == TaskState.CLOSED.value
            ],
            "event_cursor": self.scheduler.event_cursor,
            "human_guidance": copy.deepcopy(state["guidance"]),
            "active_sprint": copy.deepcopy(active_sprint),
            "configured_non_verifier_slots": configured_slots,
            "discovery_sprint_available": configured_slots == 4,
        }
        if self.advisor_program is not None:
            context["problem_assignment"] = self.scheduler.effective_problem()
        if active_sprint and active_sprint.get("status") == "trimmer_continuation":
            active_trim = state["trim"].get("active_trim") or {}
            cutoff = int(active_trim.get("cutoff_event_id", 0))
            context["event_deltas_since_trim_cutoff"] = [
                copy.deepcopy(event)
                for event in state["events"]
                if int(event["event_id"]) > cutoff
            ]
        return context

    # -------------------------------------------------------- typed call work

    def _run_review_until_committed(
        self,
        call_id: str,
        *,
        workspace: MaterializedWorkspace,
        policy: AccessPolicy,
        commit: Callable[[str], Any],
    ) -> None:
        """Retry invalid semantic review output under the call's role budget."""

        while True:
            self._run_prepared_call(call_id, workspace=workspace, policy=policy)
            try:
                commit(call_id)
                return
            except (SchedulerError, WorkflowError, InvalidAgentOutput, ValueError) as exc:
                current = self.scheduler.state["calls"][call_id]
                if current["status"] != CallState.COMPLETED.value:
                    raise
                if not self.scheduler.reject_call_result(call_id, str(exc)):
                    raise TransportFailure(
                        f"invalid-output retry limit exhausted for {call_id}"
                    ) from exc

    def _commit_verifier_with_computations(
        self,
        call_id: str,
        workspace: MaterializedWorkspace,
        commit: Callable[[str], Any],
    ) -> Any:
        """Publish optional authenticated CAS aid records, then the report."""

        lease_epoch, launch_attempt = self._call_receipt_generation(call_id)
        artifacts = self._verified_skill_artifacts(
            workspace,
            "CAS",
            call_id,
            expected_lease_epoch=lease_epoch,
            expected_launch_attempt=launch_attempt,
        )
        computations = [
            self._computation_record(item)
            for item in artifacts
            if self._computation_execution_succeeded(item)
        ]
        if computations:
            self.scheduler.ingest_review_computations(call_id, computations)
        if artifacts:
            state = self.scheduler.state
            call = state["calls"][call_id]
            continuation = call.get("continuation", {})
            if call.get("kind") == "verifier":
                source = state["operations"].get(
                    str(continuation.get("operation_id") or ""), {}
                )
            else:
                source = state["challenges"].get(
                    str(continuation.get("challenge_id") or ""), {}
                )
            task_id = str(source.get("task_id") or "")
            if not task_id:
                raise InvalidAgentOutput(
                    "verifier CAS artifact has no originating task"
                )
            self._archive_workspace_artifacts(task_id, call_id, workspace)
        return commit(call_id)

    def _operation_review_context(
        self, call_id: str
    ) -> tuple[MaterializedWorkspace, AccessPolicy]:
        persisted = self.scheduler.state["calls"][call_id]
        kind = str(persisted["kind"])
        if kind == "synthesizer":
            operation_id = str(persisted["continuation"]["operation_id"])
            memory_type = {
                "fact": "fact",
                "route_add": "route",
                "obligation_add": "obligation",
            }[self.scheduler.state["operations"][operation_id]["kind"]]
            policy = policy_for("synthesizer", review_memory_type=memory_type)
        elif kind == "verifier":
            operation_id = str(persisted["continuation"]["operation_id"])
            if not self.scheduler.source_attempt_terminal(operation_id):
                raise WorkflowError(
                    f"operation {operation_id} cannot be verified before its worker attempt ends"
                )
            policy = policy_for("verifier")
        else:
            raise SchedulerError(f"call {call_id} is not an operation review call")
        workspace = self._make_workspace(
            call_id=call_id, policy=policy, context=persisted["input"]
        )
        return workspace, policy

    def _commit_operation_review_call(
        self, call_id: str, workspace: MaterializedWorkspace
    ) -> Any:
        kind = str(self.scheduler.state["calls"][call_id]["kind"])
        if kind == "synthesizer":
            return self.scheduler.commit_synthesizer_call(call_id)
        if kind == "verifier":
            return self._commit_verifier_with_computations(
                call_id, workspace, self.scheduler.commit_verifier_call
            )
        raise SchedulerError(f"call {call_id} is not an operation review call")

    def _run_memory_review_call(self, call_id: str) -> None:
        persisted = self.scheduler.state["calls"][call_id]
        kind = str(persisted["kind"])
        if persisted["status"] == CallState.COMMITTED.value:
            return
        if kind in {"synthesizer", "verifier"}:
            workspace, policy = self._operation_review_context(call_id)
            self._run_review_until_committed(
                call_id,
                workspace=workspace,
                policy=policy,
                commit=lambda current: self._commit_operation_review_call(
                    current, workspace
                ),
            )
        elif kind == "challenge-verifier":
            policy = policy_for("verifier")
            workspace = self._make_workspace(
                call_id=call_id, policy=policy, context=persisted["input"]
            )
            self._run_review_until_committed(
                call_id,
                workspace=workspace,
                policy=policy,
                commit=lambda current: self._commit_verifier_with_computations(
                    current,
                    workspace,
                    self.scheduler.commit_challenge_verifier_call,
                ),
            )
        elif kind == "main-closure-review":
            policy = policy_for("main")
            workspace = self._make_workspace(
                call_id=call_id, policy=policy, context=persisted["input"]
            )
            self._run_review_until_committed(
                call_id,
                workspace=workspace,
                policy=policy,
                commit=self.scheduler.commit_main_closure_review_call,
            )
        elif kind == "summarizer":
            policy = policy_for("summarizer")
            workspace = self._make_workspace(
                call_id=call_id, policy=policy, frozen_input=persisted["input"]
            )
            self._run_review_until_committed(
                call_id,
                workspace=workspace,
                policy=policy,
                commit=self.scheduler.commit_sprint_summarizer_call,
            )
        else:
            raise SchedulerError(f"call {call_id} is not a memory review call")

    def _run_contained_memory_review_call(self, call_id: str) -> bool:
        """Contain an exhausted task-owned review while preserving other failures."""

        call = self.scheduler.state["calls"].get(call_id)
        if (
            call
            and call.get("status") == CallState.NEEDS_ATTENTION.value
            and self.scheduler.attention_is_task_local(f"call:{call_id}")
        ):
            return False
        try:
            self._run_memory_review_call(call_id)
        except TransportFailure:
            call = self.scheduler.state["calls"].get(call_id)
            if not (
                call
                and call.get("status") == CallState.NEEDS_ATTENTION.value
                and self.scheduler.attention_is_task_local(f"call:{call_id}")
            ):
                raise
        return True

    def _next_operation_review_call(self) -> str | None:
        state = self.scheduler.state
        for operation_id in sorted(state["operations"]):
            operation = state["operations"][operation_id]
            try:
                if operation["state"] == OperationState.SYNTHESIZING.value:
                    call_id = self.scheduler.prepare_synthesizer_call(operation_id)
                elif (
                    operation["state"] == OperationState.VERIFYING.value
                    and self.scheduler.verifier_ready(operation_id)
                ):
                    call_id = self.scheduler.prepare_verifier_call(operation_id)
                else:
                    continue
            except CapacityError:
                continue
            if self.scheduler.state["calls"][call_id]["status"] in {
                CallState.PREPARED.value,
                CallState.RETRY_PENDING.value,
                CallState.COMPLETED.value,
            }:
                return call_id
        return None

    def _start_live_operation_review(
        self, pool: ThreadPoolExecutor, call_id: str
    ) -> _LiveOperationReview:
        workspace, policy = self._operation_review_context(call_id)
        future = pool.submit(
            self._run_prepared_call,
            call_id,
            workspace=workspace,
            policy=policy,
        )
        return _LiveOperationReview(call_id, workspace, policy, future)

    def _finish_live_operation_review(
        self, review: _LiveOperationReview
    ) -> bool:
        """Commit one completed live result; return false for semantic retry."""

        try:
            review.future.result()
        except Exception as exc:
            call = self.scheduler.state["calls"].get(review.call_id, {})
            if call.get("status") in {
                CallState.CANCELLED.value,
                CallState.SUPERSEDED.value,
            }:
                return True
            if not (
                isinstance(exc, TransportFailure)
                and call.get("status") == CallState.NEEDS_ATTENTION.value
                and self.scheduler.attention_is_task_local(
                    f"call:{review.call_id}"
                )
            ):
                raise
            return True
        call = self.scheduler.state["calls"].get(review.call_id, {})
        if call.get("status") in {
            CallState.CANCELLED.value,
            CallState.SUPERSEDED.value,
            CallState.COMMITTED.value,
        }:
            return True
        if (
            call.get("status") == CallState.NEEDS_ATTENTION.value
            and self.scheduler.attention_is_task_local(f"call:{review.call_id}")
        ):
            return True
        try:
            self._commit_operation_review_call(review.call_id, review.workspace)
            return True
        except (SchedulerError, WorkflowError, InvalidAgentOutput, ValueError) as exc:
            current = self.scheduler.state["calls"][review.call_id]
            if current["status"] != CallState.COMPLETED.value:
                raise
            if not self.scheduler.reject_call_result(review.call_id, str(exc)):
                current = self.scheduler.state["calls"][review.call_id]
                if (
                    current["status"] == CallState.NEEDS_ATTENTION.value
                    and self.scheduler.attention_is_task_local(
                        f"call:{review.call_id}"
                    )
                ):
                    return True
                raise TransportFailure(
                    f"invalid-output retry limit exhausted for {review.call_id}"
                ) from exc
            return False

    def _advance_operations(self) -> bool:
        changed = False
        while True:
            state = self.scheduler.state
            pending = [
                operation_id
                for operation_id, operation in state["operations"].items()
                if operation["state"] in {
                    OperationState.SYNTHESIZING.value,
                    OperationState.VERIFYING.value,
                }
                and (
                    operation["state"] != OperationState.VERIFYING.value
                    or self.scheduler.verifier_ready(operation_id)
                )
            ]
            if not pending:
                break
            progressed = False
            for operation_id in sorted(pending):
                operation = self.scheduler.state["operations"][operation_id]
                if operation["state"] == OperationState.SYNTHESIZING.value:
                    call_id = self.scheduler.prepare_synthesizer_call(operation_id)
                else:
                    call_id = self.scheduler.prepare_verifier_call(operation_id)
                if self._run_contained_memory_review_call(call_id):
                    changed = progressed = True
            if not progressed:
                break
        for challenge_id, challenge in sorted(
            self.scheduler.state["challenges"].items()
        ):
            if challenge["state"] != OperationState.VERIFYING.value:
                continue
            call_id = self.scheduler.prepare_challenge_verifier_call(challenge_id)
            if self._run_contained_memory_review_call(call_id):
                changed = True
        for task_id, task in sorted(self.scheduler.state["tasks"].items()):
            if not task.get("closure_review_required"):
                continue
            call_id = self.scheduler.prepare_main_closure_review_call(task_id)
            if self._run_contained_memory_review_call(call_id):
                changed = True
        return changed

    # ------------------------------------------------------- Explorer workers

    def _validate_explorer_attempt_result(
        self,
        call_id: str,
        value: Mapping[str, Any],
    ) -> None:
        if self.explorer_program is None:
            raise RuntimeErrorBase("Explorer is not enabled")
        self.explorer_program.host.validate_result(call_id, value)

    def _explorer_attempt_workspace(
        self, call_id: str
    ) -> tuple[AccessPolicy, MaterializedWorkspace, str, bool]:
        if self.explorer_program is None:
            raise SchedulerError("Explorer is not enabled")
        return self.explorer_program.host.prepare_launch(call_id)

    def _run_explorer_attempt_call(
        self,
        call_id: str,
        policy: AccessPolicy,
        workspace: MaterializedWorkspace,
        session_key: str,
        resume: bool,
    ) -> Mapping[str, Any]:
        if self.explorer_program is None:
            raise RuntimeErrorBase("Explorer is not enabled")
        return self.explorer_program.host.run_launch_default(
            call_id, policy, workspace, session_key, resume
        )

    def _admit_and_prepare_explorer_wave(self) -> list[str]:
        """Compatibility forwarding hook for the portable program."""

        if self.explorer_program is None:
            return []
        return list(self.explorer_program._plan_wave())

    def _run_explorer_wave(self) -> bool:
        if self.explorer_program is None:
            return False
        return self.explorer_program.run_wave()

    # ---------------------------------------------------------- Franta sorting

    def _sort_owner(self) -> tuple[str, str, str]:
        """Return ownership created by the accepted Explorer handoff."""

        repository = self.explorer_repository
        if repository is None:
            raise RuntimeErrorBase("Franta sort requires the Explorer repository")
        state = self.scheduler.state
        phase_control = state.get("phase_control", {})
        phase = str(phase_control.get("phase") or "")
        if phase != "franta_sort":
            raise WorkflowError("there is no active Franta sort barrier")
        sort = phase_control.get("sort") or {}
        sort_run_id = str(sort.get("sort_id") or "")
        task_id = str(state.get("main_sort_tasks", {}).get(sort_run_id) or "")
        call_id = str(sort.get("sort_call_id") or "")
        if not sort_run_id or not task_id or not call_id:
            raise RuntimeErrorBase("persisted Franta sort ownership is incomplete")
        return sort_run_id, task_id, call_id

    def _resume_main_after_sort(
        self,
        *,
        sort_call_id: str,
        session_key: str,
    ) -> bool:
        """Open Franta admission and resume the project's one Main session."""

        phase_session_key = session_key
        main_session_key = MAIN_SESSION_KEY
        if (
            self.scheduler.gate == GateState.TRIMMING
            and self.scheduler.state["trim"].get("portfolio") is None
            and self.scheduler.state["trim"].get("active_trim") is None
        ):
            self.scheduler.open_assignment_without_trim()

        if self.scheduler.gate not in {
            GateState.OPEN,
            GateState.RESOLUTION_PENDING,
        }:
            return False
        state = self.scheduler.state
        existing = next(
            (
                call
                for call in state["calls"].values()
                if call.get("kind") == "main"
                and call.get("continuation", {}).get("post_sort_call_id")
                == sort_call_id
                and call.get("status")
                not in {CallState.CANCELLED.value, CallState.SUPERSEDED.value}
            ),
            None,
        )
        terminal = self.scheduler.gate == GateState.RESOLUTION_PENDING
        if existing is None:
            self._ingest_human_guidance()
            reserved_batch_id = f"BATCH-MAIN-{self.scheduler.event_cursor + 1:08d}"
            context = self._main_context(reserved_batch_id)
            context["terminal_resolution_call"] = terminal
            planning_call_id = self.scheduler.prepare_call(
                "main",
                context,
                continuation={
                    "reserved_batch_id": reserved_batch_id,
                    "post_sort_call_id": sort_call_id,
                    "session_key": main_session_key,
                },
                expected_event_cursor=int(context["event_cursor"]),
            )
            context = copy.deepcopy(
                self.scheduler.state["calls"][planning_call_id]["input"]
            )
        else:
            planning_call_id = str(existing["call_id"])
            context = copy.deepcopy(dict(existing["input"]))
            reserved_batch_id = str(
                existing.get("continuation", {}).get("reserved_batch_id") or ""
            )
        if self.scheduler.alternation_phase == "franta_sort":
            self.scheduler.open_franta_run(
                sort_call_id=sort_call_id,
                planning_call_id=planning_call_id,
                session_key=phase_session_key,
            )
        policy = policy_for("main")
        workspace = self._make_workspace(
            call_id=planning_call_id,
            policy=policy,
            context=context,
        )
        self._run_prepared_call(
            planning_call_id,
            workspace=workspace,
            policy=policy,
            session_key=main_session_key,
            resume=self._main_session_resume(planning_call_id),
            pre_accept=lambda value: self._validate_main_task_writing(
                value,
                workspace=workspace,
                call_id=planning_call_id,
                reserved_batch_id=reserved_batch_id,
                terminal=terminal,
            ),
        )
        if not self._franta_control_result_may_commit(planning_call_id):
            return True
        self._commit_main_result(planning_call_id, workspace)
        return True

    def _run_franta_sort_barrier(self) -> bool:
        sort_run_id, task_id, call_id = self._sort_owner()
        state = self.scheduler.state
        phase_sort = state.get("phase_control", {}).get("sort") or {}
        session_key = str(phase_sort.get("main_session_key") or "")
        task = state["tasks"][task_id]
        call = state["calls"][call_id]
        if call.get("status") != CallState.COMMITTED.value:
            attempt = int(task.get("current_attempt", 1))
            card = self._enrich_task_card(task, attempt, None)
            policy = policy_for("main-sort")
            workspace = self._make_workspace(
                call_id=call_id,
                policy=policy,
                task_card=card,
            )
            result = self._run_prepared_call(
                call_id,
                workspace=workspace,
                policy=policy,
                session_key=session_key,
                resume=bool(self.transport.ledger.resolve(session_key)),
                pre_accept=lambda value: self._validated_main_sort_progress(
                    task_id,
                    call_id,
                    attempt,
                    workspace,
                    value,
                    expected_lease_epoch=self._call_receipt_generation(call_id)[0],
                    expected_launch_attempt=self._call_receipt_generation(call_id)[1],
                ),
            )
            lease_epoch, launch_attempt = self._call_receipt_generation(call_id)
            self._ingest_main_sort_outbox(
                task_id,
                call_id,
                workspace,
                result,
                expected_lease_epoch=lease_epoch,
                expected_launch_attempt=launch_attempt,
            )
        self._advance_operations()
        self._reconcile_explorer_promotions(sort_run_id=sort_run_id)
        task = self.scheduler.state["tasks"][task_id]
        if task.get("state") != TaskState.CLOSED.value:
            return True
        if self.explorer_repository is not None and self.explorer_repository.list_received_promotions(
            sort_run_id=sort_run_id
        ):
            return True
        return self._resume_main_after_sort(
            sort_call_id=call_id,
            session_key=session_key,
        )

    # --------------------------------------------------------------- workers

    def _worker_lineage_call_ids(self, task: Mapping[str, Any]) -> list[str]:
        """Return scheduler-linked worker calls preceding the active attempt."""

        state = self.scheduler.state
        lineage = state.get("worker_session_lineages", {}).get(
            task.get("session_lineage_id"), {}
        )
        current_call_id = None
        attempts = task.get("attempts") or []
        if attempts:
            current_call_id = attempts[-1].get("call_id")
        result: list[str] = []
        for task_id in lineage.get("task_ids", []):
            member = state.get("tasks", {}).get(task_id)
            if not isinstance(member, Mapping):
                continue
            for attempt_record in member.get("attempts", []):
                call_id = str(attempt_record.get("call_id") or "")
                if call_id and call_id != current_call_id:
                    result.append(call_id)
        return result

    def _worker_session(self, task: Mapping[str, Any], attempt: int) -> tuple[str, bool]:
        key = f"worker:{task['session_lineage_id']}"
        explicit_resume = bool((task.get("task_card") or {}).get("if_resume"))
        resume_requested = int(attempt) > 1 or explicit_resume
        if not resume_requested:
            return key, False
        # Deterministic executors have no Codex conversation to resume; the
        # AgentCall flag still records the intended continuation semantics.
        if self.executor is not None:
            return key, True
        evidence_error: Exception | None = None
        try:
            thread_id = self.transport.ledger.resolve(key)
            if thread_id is None:
                recover = getattr(self.transport, "recover_thread_binding", None)
                if callable(recover):
                    thread_id = recover(
                        session_key=key,
                        call_ids=self._worker_lineage_call_ids(task),
                        role="worker",
                    )
        except Exception as exc:
            evidence_error = exc
            thread_id = None
        if thread_id is not None:
            return key, True
        if explicit_resume:
            reason = (
                "explicit if_resume has no recoverable audited Codex thread "
                f"for lineage {task['session_lineage_id']}"
            )
            if evidence_error is not None:
                reason += f": {evidence_error}"
            self.scheduler.mark_worker_resume_unavailable(
                str(task["task_id"]), reason
            )
            raise ProjectNeedsAttention(reason) from evidence_error
        # An infrastructure/revision attempt may relaunch when no prior Codex
        # thread ever started.  This is not an explicit if_resume continuation.
        return key, False

    def _invoke_worker_process(
        self, task_id: str, call_id: str, attempt: int, workspace: MaterializedWorkspace
    ) -> Mapping[str, Any]:
        task = self.scheduler.state["tasks"][task_id]
        mode = str(task["task_card"]["mode"])
        sprint_lane = task.get("sprint_lane")
        if task.get("sprint_id") and (
            sprint_lane not in ("A", "B", "C", "D")
        ):
            raise SchedulerError(
                f"discovery-sprint task {task_id} has no persisted lane identity"
            )
        policy = policy_for(
            "proof-writer" if mode == "proof-writer" else "worker",
            mode=mode,
            sprint_lane=sprint_lane,
        )
        key, resume = self._worker_session(task, attempt)
        spec = self._call_spec(
            call_id,
            workspace=workspace,
            policy=policy,
            mode=mode,
            session_key=key,
            resume=resume,
        )
        return self._invoke_agent(
            spec,
            root_fact_id=task["task_card"].get("root_solution_fact_id")
            if mode == "proof-writer"
            else None,
        )

    def _prepare_worker_workspace(self, task_id: str) -> tuple[str, int, MaterializedWorkspace]:
        attempt = self.scheduler.start_task_attempt(task_id)
        lease = self.scheduler.worker_call_lease(task_id)
        call_id = str(lease["call_id"])
        task = self.scheduler.state["tasks"][task_id]
        mode = str(task["task_card"]["mode"])
        sprint_lane = task.get("sprint_lane")
        if task.get("sprint_id") and (
            sprint_lane not in ("A", "B", "C", "D")
        ):
            raise SchedulerError(
                f"discovery-sprint task {task_id} has no persisted lane identity"
            )
        policy = policy_for(
            "proof-writer" if mode == "proof-writer" else "worker",
            mode=mode,
            sprint_lane=sprint_lane,
        )
        supplement = task["attempts"][-1].get("supplement")
        card = self._enrich_task_card(task, attempt, supplement)
        workspace = self._make_workspace(
            call_id=call_id,
            policy=policy,
            task_card=card,
            portfolio=self._portfolio(card, mode),
        )
        return call_id, attempt, workspace

    def _ingest_worker_outbox(
        self,
        task_id: str,
        call_id: str,
        workspace: MaterializedWorkspace,
        *,
        expected_lease_epoch: int,
        expected_launch_attempt: int,
        verified_final_progress_ids: Iterable[str] = (),
    ) -> bool:
        changed = False
        authenticated = self._verified_skill_artifacts(
            workspace,
            "record-progress",
            call_id,
            expected_lease_epoch=expected_lease_epoch,
            expected_launch_attempt=expected_launch_attempt,
            allow_pending=True,
        )
        authenticated_computations = self._verified_skill_artifacts(
            workspace,
            "CAS",
            call_id,
            expected_lease_epoch=expected_lease_epoch,
            expected_launch_attempt=expected_launch_attempt,
            allow_pending=True,
        )
        progress_values = self._progress_artifacts(
            workspace,
            verified_progress=authenticated,
            verified_computations=authenticated_computations,
        )
        verified_finals = frozenset(str(item) for item in verified_final_progress_ids)
        eligible = [
            progress
            for progress in progress_values
            if not progress.get("is_final")
            or str(progress.get("progress_id") or "") in verified_finals
        ]
        known_progress_ids = set(self.scheduler.state["progress"])
        newly_eligible: list[dict[str, Any]] = []
        for progress in eligible:
            progress_id = str(progress.get("progress_id") or "")
            if progress_id in known_progress_ids:
                continue
            known_progress_ids.add(progress_id)
            newly_eligible.append(progress)
        if newly_eligible:
            self._archive_workspace_artifacts(task_id, call_id, workspace)
        for progress in newly_eligible:
            self.scheduler.ingest_progress(progress, authenticated_computations=True)
            changed = True
        return changed

    def _verified_worker_final_progress_id(
        self,
        task_id: str,
        call_id: str,
        attempt: int,
        workspace: MaterializedWorkspace,
        result: Mapping[str, Any],
        *,
        expected_lease_epoch: int,
        expected_launch_attempt: int,
    ) -> str:
        if result.get("attempt_ended") is not True:
            raise InvalidAgentOutput("worker normal exit must set attempt_ended=true")
        final_progress_id = str(result.get("final_progress_id") or "")
        if not final_progress_id:
            raise InvalidAgentOutput(
                "worker normal exit must name its final record-progress ID"
            )
        # Validate every staged record-progress receipt.  This prevents an
        # unrelated successful command from authenticating a hand-written
        # final outbox file.
        authenticated = self._verified_skill_artifacts(
            workspace,
            "record-progress",
            call_id,
            expected_lease_epoch=expected_lease_epoch,
            expected_launch_attempt=expected_launch_attempt,
        )
        authenticated_computations = self._verified_skill_artifacts(
            workspace,
            "CAS",
            call_id,
            expected_lease_epoch=expected_lease_epoch,
            expected_launch_attempt=expected_launch_attempt,
        )
        finals = [
            progress
            for progress in self._progress_artifacts(
                workspace,
                verified_progress=authenticated,
                verified_computations=authenticated_computations,
            )
            if progress.get("is_final")
        ]
        if len(finals) != 1:
            raise InvalidAgentOutput(
                "worker normal exit requires exactly one final record-progress artifact"
            )
        final = finals[0]
        if (
            str(final.get("progress_id") or "") != final_progress_id
            or str(final.get("task_id") or "") != task_id
            or int(final.get("attempt", 0)) != int(attempt)
        ):
            raise InvalidAgentOutput(
                "worker final response does not match its final record-progress artifact"
            )
        return final_progress_id

    def _validated_main_sort_progress(
        self,
        task_id: str,
        call_id: str,
        attempt: int,
        workspace: MaterializedWorkspace,
        result: Mapping[str, Any],
        *,
        expected_lease_epoch: int,
        expected_launch_attempt: int,
    ) -> tuple[str, list[dict[str, Any]]]:
        """Authenticate a sorter's final progress and exact selected sources."""

        repository = self.explorer_repository
        if repository is None:
            raise RuntimeErrorBase("main-sort requires the Explorer repository")
        task = self.scheduler.state["tasks"].get(task_id)
        if not isinstance(task, Mapping) or task.get("agent_system") != "franta-sort":
            raise InvalidAgentOutput("main-sort result does not own a sort task")
        card = task.get("task_card") or {}
        if workspace is not None:
            validate_explorer_snapshot(
                workspace.path,
                build_main_sort_explorer_snapshot(repository, card),
            )
        sort_run_id = str(card.get("sort_run_id") or "")
        authenticated = self._verified_skill_artifacts(
            workspace,
            "record-progress",
            call_id,
            expected_lease_epoch=expected_lease_epoch,
            expected_launch_attempt=expected_launch_attempt,
        )
        progress = self._main_sort_progress_artifacts(
            workspace,
            task_id=task_id,
            sort_run_id=sort_run_id,
            verified_progress=authenticated,
        )
        scope = ExplorerReadScope(
            allowed_turn_ids=frozenset({str(card["explorer_turn_id"])}),
            max_seq=int(card["source_high_water_seq"]),
            label=f"main-sort-validation:{sort_run_id}",
        )
        try:
            submission = validate_main_sort_submission(
                result,
                progress,
                task_id=task_id,
                attempt=attempt,
                sort_run_id=sort_run_id,
                source_is_allowed=lambda record_id: repository.fetch(
                    scope, record_id
                )
                is not None,
            )
        except MainSortContractError as exc:
            raise InvalidAgentOutput(str(exc)) from exc
        self._stage_main_sort_promotions(
            sort_run_id=sort_run_id,
            progress=progress,
        )
        return submission.final_progress_id, progress

    def _ingest_main_sort_outbox(
        self,
        task_id: str,
        call_id: str,
        workspace: MaterializedWorkspace,
        result: Mapping[str, Any],
        *,
        expected_lease_epoch: int,
        expected_launch_attempt: int,
    ) -> bool:
        final_progress_id, progress = self._validated_main_sort_progress(
            task_id,
            call_id,
            int(self.scheduler.state["tasks"][task_id]["current_attempt"]),
            workspace,
            result,
            expected_lease_epoch=expected_lease_epoch,
            expected_launch_attempt=expected_launch_attempt,
        )
        known = set(self.scheduler.state["progress"])
        eligible = [
            item
            for item in progress
            if not item.get("is_final")
            or str(item.get("progress_id") or "") == final_progress_id
        ]
        newly_eligible = [
            item
            for item in eligible
            if str(item.get("progress_id") or "") not in known
        ]
        if newly_eligible:
            self._archive_workspace_artifacts(task_id, call_id, workspace)
        for item in newly_eligible:
            self.scheduler.ingest_progress(
                item,
                authenticated_computations=True,
            )
        return bool(newly_eligible)

    def _salvage_staged_final_progress(
        self,
        task_id: str,
        call_id: str,
        attempt: int,
        workspace: MaterializedWorkspace,
        *,
        expected_lease_epoch: int,
        expected_launch_attempt: int,
    ) -> bool:
        """Keep authenticated operations when a final response is lost.

        The record-progress receipt proves that the worker durably staged the
        file, but only a matching normal final response may end an attempt.
        Recovery therefore ingests the record in interrupted-salvage mode and
        leaves attempt classification to the ordinary interruption path.
        """

        authenticated = self._verified_skill_artifacts(
            workspace,
            "record-progress",
            call_id,
            expected_lease_epoch=expected_lease_epoch,
            expected_launch_attempt=expected_launch_attempt,
        )
        authenticated_computations = self._verified_skill_artifacts(
            workspace,
            "CAS",
            call_id,
            expected_lease_epoch=expected_lease_epoch,
            expected_launch_attempt=expected_launch_attempt,
        )
        finals = [
            progress
            for progress in self._progress_artifacts(
                workspace,
                verified_progress=authenticated,
                verified_computations=authenticated_computations,
            )
            if progress.get("is_final")
        ]
        if not finals:
            return False
        if len(finals) != 1:
            raise InvalidAgentOutput(
                "an interrupted attempt staged more than one final record-progress file"
            )
        final = finals[0]
        if (
            str(final.get("task_id") or "") != task_id
            or int(final.get("attempt", 0)) != int(attempt)
        ):
            raise InvalidAgentOutput(
                "interrupted final record-progress does not match its task attempt"
            )
        progress_id = str(final.get("progress_id") or "")
        existing = self.scheduler.state["progress"].get(progress_id)
        if existing is None:
            self._archive_workspace_artifacts(task_id, call_id, workspace)
            self.scheduler.ingest_progress(
                final,
                interrupted_salvage=True,
                authenticated_computations=True,
            )
        elif not existing.get("interrupted_salvage"):
            return False
        return True

    def _contain_worker_attempt_failure(
        self,
        task_id: str,
        reason: str,
        *,
        call_id: str | None = None,
        cancel: bool = False,
    ) -> None:
        """Fence one failed worker attempt without disturbing its siblings."""

        if cancel and call_id:
            try:
                self.transport.cancel(call_id, reason=reason)
            except Exception:
                # Cancellation is best-effort containment.  The persisted
                # worker-call fence below is the authoritative boundary.
                pass
        task = self.scheduler.state.get("tasks", {}).get(task_id)
        if not isinstance(task, Mapping):
            return
        task_state = task.get("state")
        if task_state == TaskState.STOPPING.value:
            self.scheduler.record_worker_stopped(
                task_id,
                "proof-writer process stopped after its root fact was revoked",
            )
        elif task_state in {
            TaskState.RUNNING.value,
            TaskState.LAUNCHING.value,
        }:
            self.scheduler.record_worker_interruption(task_id, reason)

    def _run_worker_batch(self, task_ids: Sequence[str]) -> bool:
        if not task_ids:
            return False
        if self.scheduler.alternation_phase is not None:
            # Check the admission clock at the last scheduler boundary before
            # any new worker process is prepared.  Tasks accepted earlier but
            # still pending are not "current workers" for graceful drain.
            if self.scheduler.tick_alternation() != "franta_run":
                return False
        launches: dict[str, tuple[str, int, MaterializedWorkspace]] = {}
        launch_generations: dict[str, tuple[int, int]] = {}
        for task_id in task_ids:
            try:
                launch = self._prepare_worker_workspace(task_id)
                generation = self._call_receipt_generation(launch[0])
            except Exception as exc:
                task = self.scheduler.state.get("tasks", {}).get(task_id, {})
                attempts = task.get("attempts", []) if isinstance(task, Mapping) else []
                call_id = str(attempts[-1].get("call_id") or "") if attempts else ""
                self._contain_worker_attempt_failure(
                    task_id,
                    f"worker_attempt_preparation_error: {type(exc).__name__}: {exc}",
                    call_id=call_id or None,
                )
                continue
            launches[task_id] = launch
            launch_generations[task_id] = generation
        if not launches:
            return True
        futures: dict[str, Future[Mapping[str, Any]]] = {}
        with (
            ThreadPoolExecutor(max_workers=len(launches)) as pool,
            ThreadPoolExecutor(max_workers=1) as review_pool,
        ):
            for task_id, (call_id, attempt, workspace) in launches.items():
                futures[task_id] = pool.submit(
                    self._invoke_worker_process, task_id, call_id, attempt, workspace
                )
            unfinished = set(futures)
            faulted_task_ids: set[str] = set()
            live_review: _LiveOperationReview | None = None

            def pump_live_review(*, start_new: bool = True) -> None:
                nonlocal live_review
                while True:
                    if live_review is not None:
                        if not live_review.future.done():
                            return
                        if not self._finish_live_operation_review(live_review):
                            live_review.future = review_pool.submit(
                                self._run_prepared_call,
                                live_review.call_id,
                                workspace=live_review.workspace,
                                policy=live_review.policy,
                            )
                            return
                        live_review = None
                    if not start_new:
                        return
                    call_id = self._next_operation_review_call()
                    if call_id is None:
                        return
                    live_review = self._start_live_operation_review(
                        review_pool, call_id
                    )

            while unfinished:
                if self.scheduler.alternation_phase is not None:
                    self.scheduler.tick_alternation()
                for task_id in list(unfinished):
                    future = futures[task_id]
                    if task_id in faulted_task_ids:
                        if future.done():
                            unfinished.remove(task_id)
                        continue
                    call_id, _, workspace = launches[task_id]
                    lease_epoch, launch_attempt = launch_generations[task_id]
                    try:
                        self._ingest_worker_outbox(
                            task_id,
                            call_id,
                            workspace,
                            expected_lease_epoch=lease_epoch,
                            expected_launch_attempt=launch_attempt,
                        )
                        if (
                            not future.done()
                            and (
                                self.scheduler.state["tasks"][task_id]["state"]
                                == TaskState.STOPPING.value
                                or self.scheduler.state["calls"][call_id]["status"]
                                in {
                                    CallState.CANCELLED.value,
                                    CallState.SUPERSEDED.value,
                                    CallState.NEEDS_ATTENTION.value,
                                }
                            )
                        ):
                            try:
                                self.transport.cancel(
                                    call_id,
                                    reason=(
                                        "the scheduler invalidated this running assignment"
                                    ),
                                )
                            except Exception:
                                # A failed targeted cancel must not stop
                                # unrelated workers in this batch.
                                pass
                    except Exception as exc:
                        faulted_task_ids.add(task_id)
                        self._contain_worker_attempt_failure(
                            task_id,
                            f"worker_attempt_outbox_error: {type(exc).__name__}: {exc}",
                            call_id=call_id,
                            cancel=not future.done(),
                        )
                        if future.done():
                            unfinished.remove(task_id)
                        continue
                    if not future.done():
                        continue
                    unfinished.remove(task_id)
                    normal_exit = False
                    try:
                        result = future.result()
                        normal_exit = True
                        if not isinstance(result, Mapping):
                            raise InvalidAgentOutput("worker returned a non-object")
                        final_progress_id = self._verified_worker_final_progress_id(
                            task_id,
                            call_id,
                            launches[task_id][1],
                            workspace,
                            result,
                            expected_lease_epoch=lease_epoch,
                            expected_launch_attempt=launch_attempt,
                        )
                        self._ingest_worker_outbox(
                            task_id,
                            call_id,
                            workspace,
                            expected_lease_epoch=lease_epoch,
                            expected_launch_attempt=launch_attempt,
                            verified_final_progress_ids=(final_progress_id,),
                        )
                        task = self.scheduler.state["tasks"][task_id]
                        if task["state"] == TaskState.RUNNING.value:
                            raise InvalidAgentOutput(
                                "final record-progress did not end the running attempt"
                            )
                    except Exception as exc:
                        # Durable progress already staged remains valid.  Only
                        # the unfinished attempt is classified as interrupted.
                        try:
                            self._salvage_staged_final_progress(
                                task_id,
                                call_id,
                                launches[task_id][1],
                                workspace,
                                expected_lease_epoch=lease_epoch,
                                expected_launch_attempt=launch_attempt,
                            )
                        except Exception:
                            # An unauthenticated or ambiguous final artifact
                            # cannot affect workflow state.  The attempt is
                            # still handled as the original interruption.
                            pass
                        reason = (
                            "normal_exit_missing_final_record_progress: " + str(exc)
                            if normal_exit
                            else str(exc)
                        )
                        self._contain_worker_attempt_failure(task_id, reason)
                pump_live_review()
                if unfinished:
                    time.sleep(0.2)
            while live_review is not None:
                if not live_review.future.done():
                    try:
                        live_review.future.result()
                    except Exception:
                        # The normal harvest path below classifies the exact
                        # durable call state and re-raises unexpected failures.
                        pass
                pump_live_review(start_new=False)
        return True

    def _recover_staged_worker_progress(self) -> None:
        """Ingest completed outbox files before fencing processes lost to restart."""

        state = self.scheduler.state
        for task_id, task in state["tasks"].items():
            # Main-sort has a stricter, frozen-source recovery path below.  It
            # must never pass through the ordinary worker salvage routine,
            # which intentionally knows nothing about ES/ESUM scope or XCAS.
            if task.get("agent_system") == "franta-sort":
                continue
            if task["state"] not in {
                TaskState.RUNNING.value,
                TaskState.STOPPING.value,
            } or not task.get("attempts"):
                continue
            attempt_record = task["attempts"][-1]
            call_id = str(attempt_record.get("call_id") or "")
            workspace_path = self.layout.workspaces / call_id
            if call_id and workspace_path.is_dir() and not workspace_path.is_symlink():
                call = state["calls"].get(call_id, {})
                lease_epoch = int(
                    attempt_record.get("lease_epoch", call.get("lease_epoch", 0))
                )
                launch_attempt = int(
                    attempt_record.get("call_attempt", call.get("attempt", 0))
                )
                if lease_epoch <= 0 or launch_attempt <= 0:
                    continue
                workspace = _workspace_from_path(workspace_path)
                try:
                    self._ingest_worker_outbox(
                        task_id,
                        call_id,
                        workspace,
                        expected_lease_epoch=lease_epoch,
                        expected_launch_attempt=launch_attempt,
                    )
                    normal_final_ingested = False
                    final_message = (
                        self.transport.completed_final_message(call_id)
                        if self.executor is None
                        else None
                    )
                    if final_message:
                        result = _json_object(final_message)
                        final_progress_id = self._verified_worker_final_progress_id(
                            task_id,
                            call_id,
                            int(task["attempts"][-1]["attempt"]),
                            workspace,
                            result,
                            expected_lease_epoch=lease_epoch,
                            expected_launch_attempt=launch_attempt,
                        )
                        self._ingest_worker_outbox(
                            task_id,
                            call_id,
                            workspace,
                            expected_lease_epoch=lease_epoch,
                            expected_launch_attempt=launch_attempt,
                            verified_final_progress_ids=(final_progress_id,),
                        )
                        normal_final_ingested = True
                    if not normal_final_ingested:
                        self._salvage_staged_final_progress(
                            task_id,
                            call_id,
                            int(task["attempts"][-1]["attempt"]),
                            workspace,
                            expected_lease_epoch=lease_epoch,
                            expected_launch_attempt=launch_attempt,
                        )
                except Exception:
                    # This workspace is a per-worker trust boundary.  Once any
                    # staged artifact is poisoned, do not inspect or salvage it
                    # again during this recovery pass; scheduler recovery will
                    # fence and retry only its owning task.
                    continue

    def _recover_staged_main_sort_progress(self) -> None:
        """Recover only a fully authenticated main-sort normal exit.

        Unlike an ordinary worker, a sorter may publish only records selected
        from its exact frozen Explorer turn.  Recovery therefore requires the
        completed final response and reruns the dedicated source/high-water,
        receipt, provenance, and XCAS validation before ingesting anything.
        A partial sort workspace is never salvaged.
        """

        state = self.scheduler.state
        for task_id, task in state.get("tasks", {}).items():
            if (
                task.get("agent_system") != "franta-sort"
                or task.get("state") not in {
                    TaskState.RUNNING.value,
                    TaskState.STOPPING.value,
                }
                or not task.get("attempts")
            ):
                continue
            attempt_record = task["attempts"][-1]
            call_id = str(attempt_record.get("call_id") or "")
            call = state.get("calls", {}).get(call_id, {})
            workspace_path = self.layout.workspaces / call_id
            if (
                not call_id
                or not workspace_path.is_dir()
                or workspace_path.is_symlink()
            ):
                continue
            result: Mapping[str, Any] | None = None
            if call.get("status") == CallState.COMPLETED.value and isinstance(
                call.get("result"), Mapping
            ):
                result = copy.deepcopy(dict(call["result"]))
            elif self.executor is None:
                final_message = self.transport.completed_final_message(call_id)
                if final_message:
                    try:
                        result = _json_object(final_message)
                    except Exception:
                        result = None
            if result is None:
                continue
            lease_epoch = int(
                attempt_record.get("lease_epoch", call.get("lease_epoch", 0))
            )
            launch_attempt = int(
                attempt_record.get("call_attempt", call.get("attempt", 0))
            )
            if lease_epoch <= 0 or launch_attempt <= 0:
                continue
            workspace = _workspace_from_path(workspace_path)
            try:
                self._validate_control_result("main-sort", result)
                self._ingest_main_sort_outbox(
                    str(task_id),
                    call_id,
                    workspace,
                    result,
                    expected_lease_epoch=lease_epoch,
                    expected_launch_attempt=launch_attempt,
                )
            except Exception:
                # No subset of a malformed or out-of-scope sorter workspace is
                # safe to salvage.  The fenced logical call will retry through
                # the normal franta_sort barrier after scheduler recovery.
                continue

    # ------------------------------------------------------------ main/trim

    def _main_should_run(self) -> bool:
        state = self.scheduler.state
        if self.scheduler.gate not in {GateState.OPEN, GateState.RESOLUTION_PENDING}:
            return False
        if state.get("halt_requested"):
            return False
        if self.scheduler.gate == GateState.OPEN and self.scheduler.free_non_verifier_slots() <= 0:
            return False
        if self.scheduler.gate == GateState.RESOLUTION_PENDING:
            return not bool(state["root"].get("terminal_main_decision_done"))
        checkpoint = int(state["main_checkpoint"].get("event_cursor", 0))
        return self.scheduler.event_cursor > checkpoint

    def _validate_main_task_writing(
        self,
        value: Mapping[str, Any],
        *,
        workspace: MaterializedWorkspace,
        call_id: str,
        reserved_batch_id: str,
        terminal: bool,
    ) -> None:
        decision = str(value.get("decision") or "")
        scheduler = getattr(self, "scheduler", None)
        guidance = (
            scheduler.state["calls"][call_id]["input"].get("human_guidance")
            if scheduler is not None
            else None
        )
        if guidance is not None and decision != "assignments":
            raise InvalidAgentOutput(
                "human guidance requires a worker assignment in this Main call"
            )
        assignment_branch = decision == "assignments" or (
            terminal and decision == "terminal" and not value.get("decline_proof_writer")
        )
        if not assignment_branch:
            return
        lease_epoch, launch_attempt = self._call_receipt_generation(call_id)
        artifacts = self._verified_skill_artifacts(
            workspace,
            "task-writing",
            call_id,
            expected_lease_epoch=lease_epoch,
            expected_launch_attempt=launch_attempt,
        )
        requested = [str(item) for item in value.get("assignment_report_ids", [])]
        operation_ids = [str(item.get("operation_id") or "") for item in artifacts]
        if (
            not requested
            or len(requested) != len(set(requested))
            or len(operation_ids) != len(set(operation_ids))
            or set(requested) != set(operation_ids)
        ):
            raise InvalidAgentOutput(
                "each assignment must have one matching successful task-writing artifact"
            )
        if terminal and len(artifacts) != 1:
            raise InvalidAgentOutput(
                "terminal proof-writer branch requires exactly one task-writing artifact"
            )
        for artifact in artifacts:
            if str(artifact.get("batch_id") or "") != reserved_batch_id:
                raise InvalidAgentOutput(
                    "task-writing artifact changed the scheduler-reserved batch ID"
                )
        if guidance is not None:
            first = next(
                item for item in artifacts
                if str(item.get("operation_id")) == requested[0]
            )
            objective = str(self._assignment_report(first).get("objective") or "")
            if guidance["text"] not in objective:
                raise InvalidAgentOutput(
                    "the first worker objective must include the human guidance verbatim"
                )

    def _commit_main_result(
        self,
        call_id: str,
        workspace: MaterializedWorkspace,
    ) -> None:
        """Apply one persisted main result idempotently, including after restart."""

        call = self.scheduler.state["calls"][call_id]
        if call["status"] == CallState.COMMITTED.value:
            return
        if call["status"] != CallState.COMPLETED.value:
            raise WorkflowError("main call has no completed result")
        result = copy.deepcopy(dict(call["result"]))
        continuation = call.get("continuation", {})
        reserved_batch_id = str(
            continuation.get("reserved_batch_id")
            or call["input"].get("reserved_batch_id")
            or ""
        )
        if not reserved_batch_id:
            raise InvalidAgentOutput("main call lacks its reserved batch ID")
        terminal = bool(call["input"].get("terminal_resolution_call"))
        self._validate_main_task_writing(
            result,
            workspace=workspace,
            call_id=call_id,
            reserved_batch_id=reserved_batch_id,
            terminal=terminal,
        )
        decision = str(result["decision"])
        lease_epoch, launch_attempt = self._call_receipt_generation(call_id)
        waiting_for: list[str] = []
        if terminal:
            if decision != "terminal":
                raise InvalidAgentOutput("resolution-pending main call must be terminal")
            reports = [
                self._assignment_report(item)
                for item in self._verified_skill_artifacts(
                    workspace,
                    "task-writing",
                    call_id,
                    expected_lease_epoch=lease_epoch,
                    expected_launch_attempt=launch_attempt,
                )
            ]
            if result.get("decline_proof_writer"):
                if reports:
                    raise InvalidAgentOutput("declined proof-writer but staged an assignment")
                self.scheduler.record_terminal_main_decision()
            else:
                if len(reports) != 1 or reports[0].get("mode") != "proof-writer":
                    raise InvalidAgentOutput("terminal call must stage exactly one proof-writer")
                # submit_batch is keyed by reserved_batch_id.  If a crash
                # occurred between that commit and this control commit, reuse
                # the already-created proof-writer instead of creating another.
                if not self.scheduler.state.get("proof_writer_task_id"):
                    self.scheduler.record_terminal_main_decision(
                        reports[0], batch_id=reserved_batch_id
                    )
                elif not self.scheduler.state["root"].get(
                    "terminal_main_decision_done"
                ):
                    self.scheduler.record_terminal_main_decision()
        elif decision == "assignments":
            artifacts = {
                str(item.get("operation_id")): self._assignment_report(item)
                for item in self._verified_skill_artifacts(
                    workspace,
                    "task-writing",
                    call_id,
                    expected_lease_epoch=lease_epoch,
                    expected_launch_attempt=launch_attempt,
                )
            }
            requested = [
                str(item) for item in result.get("assignment_report_ids", [])
            ]
            if not requested or set(requested) != set(artifacts):
                raise InvalidAgentOutput(
                    "main assignment IDs must exactly match task-writing artifacts"
                )
            batch_id = str(result.get("batch_id") or reserved_batch_id)
            if batch_id != reserved_batch_id:
                raise InvalidAgentOutput("main changed the scheduler-reserved batch ID")
            self.scheduler.submit_batch(
                batch_id, [artifacts[item] for item in requested],
                **(
                    {"human_guidance_call_id": call_id}
                    if call["input"].get("human_guidance") is not None
                    else {}
                ),
            )
        elif decision == "wait_for_results":
            waiting_for = [
                str(item) for item in result.get("wait_for_task_ids", [])
            ]
            if not waiting_for:
                raise InvalidAgentOutput("wait_for_results must identify pending work")
            current = self.scheduler.state
            for item in waiting_for:
                task = current["tasks"].get(item)
                operation = current["operations"].get(item)
                if not (
                    (task and task["state"] != TaskState.CLOSED.value)
                    or (
                        operation
                        and operation["state"] not in FINAL_OPERATION_STATES
                    )
                ):
                    raise InvalidAgentOutput(
                        f"wait target {item} cannot produce another event"
                    )
        else:
            raise InvalidAgentOutput(f"invalid nonterminal main decision {decision}")
        self.scheduler.mark_call_committed(call_id)
        self.scheduler.record_main_checkpoint(
            call_id=call_id,
            observed_event_cursor=int(call["input"].get("event_cursor", 0)),
            waiting_for=waiting_for,
        )

    def _franta_control_result_may_commit(self, call_id: str) -> bool:
        """Recheck the admission clock after a synchronous control call exits."""

        phase = self.scheduler.alternation_phase
        if phase is None:
            return True
        phase = self.scheduler.tick_alternation()
        call = self.scheduler.state.get("calls", {}).get(call_id, {})
        return bool(
            phase in {"franta_sort", "franta_run"}
            and call.get("status")
            not in {CallState.CANCELLED.value, CallState.SUPERSEDED.value}
        )

    def _advisor_session_resume(self, call_id: str) -> bool:
        """Recover the one persistent Advisor conversation or fail closed."""

        state = self.scheduler.state
        call = state.get("calls", {}).get(call_id)
        if not isinstance(call, Mapping) or not is_advisor_call_kind(
            str(call.get("kind") or "")
        ):
            raise RuntimeErrorBase(f"{call_id} is not a persisted Advisor call")
        session_key = str(self.config["advisor"]["session_key"])
        if call.get("continuation", {}).get("session_key") != session_key:
            raise RuntimeErrorBase(
                f"Advisor call {call_id} is not bound to {session_key}"
            )
        call_ids = [
            str(candidate_id)
            for candidate_id, candidate in state.get("calls", {}).items()
            if is_advisor_call_kind(str(candidate.get("kind") or ""))
            and candidate.get("continuation", {}).get("session_key")
            == session_key
        ]
        persisted_session_id = state.get("advisor_control", {}).get("session_id")
        thread_id = self.transport.ledger.resolve(session_key)
        if (
            thread_id is not None
            and persisted_session_id is not None
            and thread_id != persisted_session_id
        ):
            raise ProjectNeedsAttention(
                "Advisor session ledger conflicts with durable Advisor state"
            )
        if thread_id is not None:
            return True
        if self.executor is not None:
            return bool(
                persisted_session_id
                or any(
                    candidate_id != call_id
                    and state["calls"][candidate_id].get("status")
                    == CallState.COMMITTED.value
                    for candidate_id in call_ids
                )
            )
        recover = getattr(self.transport, "recover_thread_binding", None)
        try:
            if callable(recover):
                thread_id = recover(
                    session_key=session_key,
                    call_ids=call_ids,
                    role="advisor",
                )
        except Exception as exc:
            raise ProjectNeedsAttention(
                "Advisor session has conflicting or unreadable audited thread evidence"
            ) from exc
        if thread_id is not None:
            if persisted_session_id is not None and thread_id != persisted_session_id:
                raise ProjectNeedsAttention(
                    "Recovered Advisor session conflicts with durable Advisor state"
                )
            return True
        completed_without_thread = any(
            state["calls"][candidate_id].get("status")
            in {CallState.COMPLETED.value, CallState.COMMITTED.value}
            for candidate_id in call_ids
        )
        if completed_without_thread or persisted_session_id is not None:
            raise ProjectNeedsAttention(
                "Advisor session has durable work but no recoverable Codex thread"
            )
        return False

    def _advisor_session_id(self, call_id: str) -> str:
        session_key = str(self.config["advisor"]["session_key"])
        if self.executor is not None:
            return str(
                self.scheduler.advisor_state.get("session_id")
                or f"test-session:{session_key}"
            )
        self._advisor_session_resume(call_id)
        thread_id = self.transport.ledger.resolve(session_key)
        if not thread_id:
            raise ProjectNeedsAttention(
                "Advisor call completed without a persistent session binding"
            )
        return str(thread_id)

    def _advisor_workspace(self, call_id: str, *, stage: str) -> MaterializedWorkspace:
        call = self.scheduler.state.get("calls", {}).get(call_id)
        if not isinstance(call, Mapping):
            raise RuntimeErrorBase(f"unknown Advisor call {call_id}")
        policy = policy_for("advisor", mode=stage)
        workspace_path = self.layout.workspaces / _safe_component(call_id, "call ID")
        expected_generation: dict[str, Any] | None = None
        if stage == "proposal" and call.get("status") in {
            CallState.PREPARED.value,
            CallState.RETRY_PENDING.value,
        }:
            # A recovered RUNNING proposal is fenced into RETRY_PENDING while
            # its workspace survives.  The agent may already have staged the
            # immutable selection report, so reusing that directory would make
            # the next selection_report call collide with the old generation.
            # Bind every materialization to the lease/attempt it is intended to
            # launch under; a fresh-but-not-yet-launched retry remains reusable
            # across another restart, while a fenced launch is retired.
            expected_generation = {
                "schema_version": 1,
                "call_id": call_id,
                "lease_epoch": int(call.get("lease_epoch", 0)),
                "launch_attempt": int(call.get("attempt", 0)) + 1,
            }
            if workspace_path.is_dir() and not workspace_path.is_symlink():
                existing = _workspace_from_path(workspace_path)
                expected_generation["workspace_generation"] = (
                    self._workspace_generation(existing)
                )
                if self._advisor_workspace_generation_record(
                    call_id
                ) != expected_generation:
                    self._retire_invalid_control_workspace(call_id, existing)

        workspace = self._make_workspace(
            call_id=call_id,
            policy=policy,
            context=call["input"],
        )
        if expected_generation is not None:
            expected_generation["workspace_generation"] = self._workspace_generation(
                workspace
            )
            atomic_write_text(
                self._advisor_workspace_generation_path(call_id),
                json.dumps(
                    expected_generation,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n",
            )
        return workspace

    def _advisor_workspace_generation_path(self, call_id: str) -> Path:
        return (
            self.layout.private
            / "advisor-workspace-generations"
            / f"{_safe_component(call_id, 'call ID')}.json"
        )

    def _advisor_workspace_generation_record(
        self, call_id: str
    ) -> dict[str, Any] | None:
        path = self._advisor_workspace_generation_path(call_id)
        if not path.exists():
            return None
        if path.is_symlink() or not path.is_file():
            raise RuntimeErrorBase("Advisor workspace generation record is unsafe")
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeErrorBase(
                "Advisor workspace generation record is invalid"
            ) from exc
        if not isinstance(value, Mapping):
            raise RuntimeErrorBase("Advisor workspace generation record is invalid")
        return dict(value)

    def _validated_advisor_report(
        self,
        call_id: str,
        response: Mapping[str, Any],
        workspace: MaterializedWorkspace,
    ) -> SelectionReport:
        expected_fields = {
            "stage",
            "call_ended",
            "selection_report_id",
            "selection_report_digest",
            "feedback_request_id",
            "report_path",
        }
        if set(response) != expected_fields:
            raise InvalidAgentOutput("Advisor proposal response has an invalid shape")
        call = self.scheduler.state["calls"][call_id]
        artifacts = self._verified_skill_artifacts(
            workspace,
            "selection-report",
            call_id,
            expected_lease_epoch=int(call.get("lease_epoch", 1)),
            expected_launch_attempt=int(call.get("attempt", 1)),
        )
        if len(artifacts) != 1:
            raise InvalidAgentOutput(
                "Advisor proposal must call selection_report exactly once"
            )
        try:
            report = SelectionReport.from_dict(artifacts[0])
            context = AdvisorCycleContext.from_dict(call["input"])
            report.validate_for_context(context)
        except Exception as exc:
            raise InvalidAgentOutput(f"invalid Advisor selection report: {exc}") from exc
        if (
            response.get("stage") != "proposal"
            or response.get("call_ended") is not True
            or response.get("selection_report_id") != report.selection_report_id
            or response.get("selection_report_digest") != report.digest
            or response.get("feedback_request_id") != report.feedback_request_id
            or response.get("report_path") != report.report_path
        ):
            raise InvalidAgentOutput(
                "Advisor proposal response does not match its selection report"
            )
        relative = Path(report.report_path)
        source = workspace.path / relative
        try:
            resolved = source.resolve(strict=True)
            artifact_root = workspace.artifacts_path.resolve(strict=True)
        except OSError as exc:
            raise InvalidAgentOutput("Advisor selection report is missing") from exc
        if (
            relative.is_absolute()
            or ".." in relative.parts
            or source.is_symlink()
            or not resolved.is_file()
            or artifact_root not in resolved.parents
            or resolved.read_text(encoding="utf-8") != render_selection_report(report)
        ):
            raise InvalidAgentOutput("Advisor selection report Markdown is invalid")
        return report

    def _archive_advisor_report(
        self, report: SelectionReport, workspace: MaterializedWorkspace
    ) -> str:
        source = (workspace.path / report.report_path).resolve(strict=True)
        data = source.read_bytes()
        report_root = self._validated_advisor_report_root()
        target = report_root / (
            f"advisor-{report.advisor_index:04d}-{report.digest[:16]}.md"
        )
        if target.exists():
            if (
                target.is_symlink()
                or not target.is_file()
                or target.read_bytes() != data
            ):
                raise RuntimeErrorBase("Advisor report archive changed")
        else:
            atomic_write_bytes(target, data, mode=0o444)
        return target.relative_to(self.layout.root).as_posix()

    def _validated_advisor_report_root(self) -> Path:
        """Revalidate the host-owned report boundary immediately before writes."""

        report_root = self.layout.advisor_reports
        try:
            resolved = report_root.resolve(strict=True)
            project_root = self.layout.root.resolve(strict=True)
        except OSError as exc:
            raise RuntimeErrorBase("Advisor report directory is unavailable") from exc
        if (
            report_root.is_symlink()
            or not report_root.is_dir()
            or resolved.parent != project_root
        ):
            raise RuntimeErrorBase("Advisor report directory escaped the project")
        return resolved

    def _validated_advisor_finalization(
        self, call_id: str, response: Mapping[str, Any]
    ) -> AdvisorFinalization:
        expected_fields = {
            "stage",
            "call_ended",
            "selection_report_id",
            "selection_report_digest",
            "feedback_id",
            "feedback_digest",
            "selected_subproblems",
        }
        if set(response) != expected_fields:
            raise InvalidAgentOutput("Advisor finalization response has an invalid shape")
        if response.get("stage") != "finalize" or response.get("call_ended") is not True:
            raise InvalidAgentOutput("Advisor finalization did not end its call")
        state = self.scheduler.advisor_state
        active = state.get("active")
        if not isinstance(active, Mapping) or active.get("finalize_call_id") != call_id:
            raise InvalidAgentOutput("Advisor finalization is not the active round")
        try:
            report = SelectionReport.from_dict(active["selection_report"])
            feedback = HumanFeedback.from_dict(active["human_feedback"])
            finalization = AdvisorFinalization.from_dict(response)
            finalization.validate_bindings(report, feedback)
        except Exception as exc:
            raise InvalidAgentOutput(f"Advisor did not follow human feedback: {exc}") from exc
        return finalization

    def _execute_advisor_call(
        self, call_id: str, launch: AdvisorLaunchSpec
    ) -> Mapping[str, Any]:
        call = self.scheduler.state.get("calls", {}).get(call_id)
        expected_kind = f"advisor-{launch.stage}"
        if (
            not isinstance(call, Mapping)
            or call.get("kind") != expected_kind
            or call.get("continuation", {}).get("advisor_index")
            != launch.advisor_index
        ):
            raise RuntimeErrorBase("portable Advisor launch does not match its Franta call")
        policy = policy_for("advisor", mode=launch.stage)
        workspace = self._advisor_workspace(call_id, stage=launch.stage)
        resume = self._advisor_session_resume(call_id)
        if launch.resume_required and not resume:
            raise ProjectNeedsAttention(
                "Advisor continuation requires the previous Advisor session"
            )
        return self._run_prepared_call(
            call_id,
            workspace=workspace,
            policy=policy,
            mode=launch.stage,
            session_key=launch.session_key,
            resume=resume,
            pre_accept=(
                lambda value: self._validated_advisor_report(
                    call_id, value, workspace
                )
                if launch.stage == "proposal"
                else self._validated_advisor_finalization(call_id, value)
            ),
        )

    def _commit_advisor_proposal(
        self, call_id: str, response: Mapping[str, Any]
    ) -> SelectionReport:
        workspace = self._advisor_workspace(call_id, stage="proposal")
        report = self._validated_advisor_report(call_id, response, workspace)
        archived = self._archive_advisor_report(report, workspace)
        self.scheduler.commit_advisor_proposal(
            call_id,
            report.to_dict(),
            session_id=self._advisor_session_id(call_id),
            archived_report_path=archived,
        )
        return report

    def _prepare_advisor_proposal(self, context: AdvisorCycleContext) -> str:
        """Enrich the portable context and persist it through Scheduler's port."""

        return self.scheduler.prepare_advisor_proposal(
            self._advisor_call_context(context),
            retry_limit=int(self.config["retries"]["main_transport"]),
        )

    def _commit_advisor_finalize(
        self, call_id: str, response: Mapping[str, Any]
    ) -> ProblemAssignment:
        finalization = self._validated_advisor_finalization(call_id, response)
        active = self.scheduler.advisor_state["active"]
        advisor_index = int(active["advisor_index"])
        assignment_id = f"ADVISOR-ASSIGNMENT-{advisor_index + 1:08d}"
        preview = commit_assignment_transition(
            self.scheduler.advisor_state,
            finalize_call_id=call_id,
            assignment_id=assignment_id,
            finalization=finalization.to_dict(),
        ).value
        if not isinstance(preview, ProblemAssignment):
            raise RuntimeErrorBase("Advisor block did not construct an assignment")
        report_root = self._validated_advisor_report_root()
        target = report_root / (
            f"problem-cycle-{preview.target_cycle:08d}.md"
        )
        data = str(preview.problem_text).encode("utf-8")
        if target.exists():
            if (
                target.is_symlink()
                or not target.is_file()
                or target.read_bytes() != data
            ):
                raise RuntimeErrorBase("Advisor problem assignment file changed")
        else:
            atomic_write_bytes(target, data, mode=0o444)
        persisted = self.scheduler.commit_advisor_assignment_and_complete_drain(
            call_id,
            finalization.to_dict(),
            assignment_id=assignment_id,
            assignment_file=target.relative_to(self.layout.root).as_posix(),
        )
        return ProblemAssignment.from_dict(persisted)

    def _advance_advisor(self) -> bool:
        if self.advisor_program is None:
            return False
        self._ingest_dashboard_commands()
        state = self.scheduler.advisor_state
        status = advisor_status(state)
        context = (
            self._advisor_context()
            if status == "idle"
            else None
        )
        try:
            advance = self.advisor_program.advance(context)
            return bool(advance.progressed)
        except Exception as exc:
            current = self.scheduler.advisor_state.get("active") or {}
            call_id = str(
                current.get("finalize_call_id")
                or current.get("proposal_call_id")
                or ""
            )
            call = self.scheduler.state.get("calls", {}).get(call_id)
            if isinstance(call, Mapping) and call.get("status") == CallState.COMPLETED.value:
                if not self.scheduler.reject_call_result(call_id, str(exc)):
                    raise TransportFailure(
                        f"invalid-output retry limit exhausted for {call_id}"
                    ) from exc
                self._retire_invalid_control_workspace(
                    call_id,
                    _workspace_from_path(
                        self.layout.workspaces / _safe_component(call_id, "call ID")
                    ),
                )
                return True
            raise

    def _main_session_resume(self, call_id: str) -> bool:
        """Recover the project's single Main thread or fail closed on drift."""

        state = self.scheduler.state
        call = state.get("calls", {}).get(call_id)
        if not isinstance(call, Mapping) or call.get("kind") != "main":
            raise RuntimeErrorBase(f"{call_id} is not a persisted Main call")
        persisted_key = call.get("continuation", {}).get("session_key")
        if persisted_key != MAIN_SESSION_KEY:
            raise RuntimeErrorBase(
                f"Main call {call_id} is not bound to {MAIN_SESSION_KEY}"
            )
        main_call_ids = [
            str(candidate_id)
            for candidate_id, candidate in state.get("calls", {}).items()
            if candidate.get("kind") == "main"
            and candidate.get("continuation", {}).get("session_key")
            == MAIN_SESSION_KEY
        ]
        thread_id = self.transport.ledger.resolve(MAIN_SESSION_KEY)
        if thread_id is not None:
            return True

        # Deterministic test executors have no Codex thread audit.  Preserve
        # continuation intent after a prior committed Main call without
        # fabricating a thread-ledger row.
        if self.executor is not None:
            return any(
                candidate_id != call_id
                and state["calls"][candidate_id].get("status")
                == CallState.COMMITTED.value
                for candidate_id in main_call_ids
            )

        recover = getattr(self.transport, "recover_thread_binding", None)
        try:
            if callable(recover):
                thread_id = recover(
                    session_key=MAIN_SESSION_KEY,
                    call_ids=main_call_ids,
                    role="main",
                )
        except Exception as exc:
            raise ProjectNeedsAttention(
                "Main session has conflicting or unreadable audited thread evidence"
            ) from exc
        if thread_id is not None:
            return True

        completed_without_thread = any(
            state["calls"][candidate_id].get("status")
            in {CallState.COMPLETED.value, CallState.COMMITTED.value}
            for candidate_id in main_call_ids
        )
        if completed_without_thread:
            raise ProjectNeedsAttention(
                "Main session has a completed call but no recoverable Codex thread"
            )
        return False

    def _run_main(self) -> bool:
        self._ingest_human_guidance()
        terminal = self.scheduler.gate == GateState.RESOLUTION_PENDING
        reserved_batch_id = f"BATCH-MAIN-{self.scheduler.event_cursor + 1:08d}"
        context = self._main_context(reserved_batch_id)
        context["terminal_resolution_call"] = terminal
        session_key = MAIN_SESSION_KEY
        call_id = self.scheduler.prepare_call(
            "main",
            context,
            continuation={
                "reserved_batch_id": reserved_batch_id,
                "session_key": session_key,
            },
            expected_event_cursor=int(context["event_cursor"]),
        )
        context = copy.deepcopy(self.scheduler.state["calls"][call_id]["input"])
        policy = policy_for("main")
        workspace = self._make_workspace(call_id=call_id, policy=policy, context=context)
        resume = self._main_session_resume(call_id)
        result = self._run_prepared_call(
            call_id,
            workspace=workspace,
            policy=policy,
            session_key=session_key,
            resume=resume,
            pre_accept=lambda value: self._validate_main_task_writing(
                value,
                workspace=workspace,
                call_id=call_id,
                reserved_batch_id=reserved_batch_id,
                terminal=terminal,
            ),
        )
        if not self._franta_control_result_may_commit(call_id):
            return True
        self._commit_main_result(call_id, workspace)
        return True

    def _run_trim_review(self) -> bool:
        session_id = self.scheduler.start_trim_review_round()
        context = self._trimmer_context(phase="review")
        call_id = self.scheduler.prepare_call(
            "trimmer", context, continuation={"phase": "review", "session_id": session_id}
        )
        policy = policy_for("trimmer")
        workspace = self._make_workspace(call_id=call_id, policy=policy, context=context)
        session_key = f"trimmer:{session_id}"
        result = self._run_prepared_call(
            call_id,
            workspace=workspace,
            policy=policy,
            session_key=session_key,
            resume=bool(self.transport.ledger.resolve(session_key)),
        )
        if not self._franta_control_result_may_commit(call_id):
            return True
        self._commit_trimmer_result(call_id, workspace)
        return True

    def _commit_category_proposal(
        self,
        operation_id: str,
        proposal: Mapping[str, Any],
        *,
        phase: str,
        continuation_call_id: str | None = None,
    ) -> None:
        portfolio = proposal.get("portfolio")
        if not isinstance(portfolio, Mapping):
            raise InvalidAgentOutput(
                f"trimmer {phase} commit requires a category portfolio"
            )
        result = self.categories.apply_trim_proposal(operation_id, proposal, actor="trimmer")
        if result.status != "committed":
            raise InvalidAgentOutput(result.error or "category proposal was rejected")
        snapshot = self.categories.active_portfolio()
        if snapshot is None:
            raise InvalidAgentOutput("trimmer committed no category portfolio")
        if phase == "initial-maintain-select":
            self.scheduler.commit_initial_trim(snapshot)
        else:
            if phase == "maintain":
                # Preserve support for legacy combined maintain/select outputs.
                # A restart may observe the phase transition without the later
                # call/portfolio commit, so advancing the phase must be replay-safe.
                self._advance_trim_to_select(continuation_call_id)
            elif phase != "select":
                raise InvalidAgentOutput(f"invalid trimmer commit phase {phase!r}")
            active = self.scheduler.state["trim"].get("active_trim") or {}
            self.scheduler.commit_trim(
                snapshot,
                expected_portfolio_revision=int(portfolio.get("expected_portfolio_revision", -1)),
                confirmed_through_event_id=int(
                    portfolio.get("confirmed_through_event_id", active.get("cutoff_event_id", -1))
                ),
                continuation_call_id=continuation_call_id,
            )

    def _advance_trim_to_select(self, continuation_call_id: str | None) -> None:
        """Advance one maintain call, including recovery of its persisted tail."""

        if continuation_call_id is None:
            raise WorkflowError("ordinary trim maintenance has no continuation call")
        state = self.scheduler.state
        call = state["calls"].get(continuation_call_id)
        if not call or call.get("kind") != "trimmer":
            raise WorkflowError("trim maintenance has the wrong call ID")
        active = state["trim"].get("active_trim")
        if not isinstance(active, Mapping):
            raise WorkflowError("trim maintenance has no active trim")
        call_session = str(call.get("continuation", {}).get("session_id") or "")
        active_session = str(active.get("session_id") or "")
        if call_session and call_session != active_session:
            raise WorkflowError("trim maintenance call belongs to a different trim session")
        current_phase = str(active.get("phase") or "")
        if current_phase == "maintain":
            self.scheduler.set_trim_phase("select")
        elif current_phase != "select":
            raise WorkflowError(
                f"trim maintenance cannot complete from phase {current_phase!r}"
            )

    def _commit_category_maintenance(
        self,
        operation_id: str,
        proposal: Mapping[str, Any],
        *,
        continuation_call_id: str,
    ) -> None:
        """Commit category-only maintenance, then leave selection to a new call."""

        unknown = set(proposal) - {
            "expected_state_revision",
            "category_changes",
            "portfolio",
        }
        if unknown:
            raise InvalidAgentOutput(
                f"trim proposal has unknown fields: {sorted(unknown)}"
            )
        category_changes = proposal.get("category_changes")
        if not isinstance(category_changes, list):
            raise InvalidAgentOutput("trimmer maintenance requires category_changes")
        if category_changes:
            result = self.categories.apply_trim_proposal(
                operation_id, proposal, actor="trimmer"
            )
            if result.status != "committed":
                raise InvalidAgentOutput(result.error or "category proposal was rejected")
        else:
            # An unchanged category view is a valid maintenance result.  Avoid
            # sending a deliberate no-op to CategoryStore, whose repository
            # contract correctly rejects proposals with no effective operation.
            expected_revision = proposal.get("expected_state_revision")
            if expected_revision is not None:
                if not isinstance(expected_revision, int) or isinstance(
                    expected_revision, bool
                ):
                    raise InvalidAgentOutput(
                        "expected_state_revision must be an integer"
                    )
                current_revision = int(self.categories.state_revision)
                if expected_revision != current_revision:
                    raise InvalidAgentOutput(
                        "category state revision conflict: "
                        f"expected {expected_revision}, current {current_revision}"
                    )
        self._advance_trim_to_select(continuation_call_id)

    def _commit_trimmer_result(
        self,
        call_id: str,
        workspace: MaterializedWorkspace,
    ) -> None:
        """Apply one persisted trimmer result idempotently after any restart."""

        call = self.scheduler.state["calls"][call_id]
        if call["status"] == CallState.COMMITTED.value:
            return
        if call["status"] != CallState.COMPLETED.value:
            raise WorkflowError("trimmer call has no completed result")
        result = copy.deepcopy(dict(call["result"]))
        phase = str(call.get("continuation", {}).get("phase") or "")
        if phase == "review":
            self._validate_control_result("trimmer-review", result)
            self.scheduler.apply_trim_review_decision(
                str(result["decision"]), str(result.get("reason") or "")
            )
            self.scheduler.mark_call_committed(call_id)
            return
        decision = str(result["decision"])
        lease_epoch, launch_attempt = self._call_receipt_generation(call_id)
        initial = phase == "initial-maintain-select"
        if decision == "commit":
            proposal = result.get("proposal")
            if not isinstance(proposal, Mapping):
                raise InvalidAgentOutput("trimmer commit requires a proposal")
            portfolio = proposal.get("portfolio")
            if phase == "maintain" and portfolio is None:
                self._commit_category_maintenance(
                    f"category:{call_id}",
                    proposal,
                    continuation_call_id=call_id,
                )
            else:
                if phase not in {"initial-maintain-select", "maintain", "select"}:
                    raise InvalidAgentOutput(f"invalid trimmer commit phase {phase!r}")
                if not isinstance(portfolio, Mapping):
                    raise InvalidAgentOutput(
                        f"trimmer {phase} commit requires a category portfolio"
                    )
                self._commit_category_proposal(
                    f"category:{call_id}",
                    proposal,
                    phase=phase,
                    continuation_call_id=None if initial else call_id,
                )
        elif decision == "human_guidance":
            artifacts = self._verified_skill_artifacts(
                workspace,
                "human-guidance",
                call_id,
                expected_lease_epoch=lease_epoch,
                expected_launch_attempt=launch_attempt,
            )
            if len(artifacts) != 1:
                raise InvalidAgentOutput(
                    "human guidance requires exactly one staged request"
                )
            item = artifacts[0]
            report_ref = self._archive_human_guidance_report(
                call_id, workspace, str(item["pdf_path"])
            )
            self.scheduler.request_human_guidance(
                str(item["request_id"]),
                report_ref,
                str(item["question"]),
            )
        elif decision == "discovery_sprint":
            artifacts = self._verified_skill_artifacts(
                workspace,
                "discovery-sprint",
                call_id,
                expected_lease_epoch=lease_epoch,
                expected_launch_attempt=launch_attempt,
            )
            if len(artifacts) != 1:
                raise InvalidAgentOutput(
                    "discovery sprint requires exactly one staged plan"
                )
            item = artifacts[0]
            sprint_id = str(item.get("sprint_id") or item.get("operation_id"))
            if item.get("decision") == "plan":
                target_id = str(item.pop("target_obligation_id"))
                item["target_obligation"] = {
                    "id": target_id,
                    "revision": item.pop("target_obligation_revision"),
                    "statement": item.pop("target_statement"),
                }
            self.scheduler.persist_sprint_plan(sprint_id, item)
        else:
            raise InvalidAgentOutput(
                "initial/ordinary trim returned an invalid control decision"
            )
        self.scheduler.mark_call_committed(call_id)

    def _run_trim(self, *, initial: bool = False) -> bool:
        state = self.scheduler.state
        active = state["trim"].get("active_trim")
        if initial:
            session_id = "initial"
            phase = "initial-maintain-select"
        else:
            if not active:
                raise SchedulerError("trimming gate has no active trim")
            session_id = str(active["session_id"])
            phase = str(active.get("phase", "maintain"))
        context = self._trimmer_context(phase=phase)
        if initial:
            context["initial_trim"] = True
            context["required_portfolio_revision"] = 0
        call_id = self.scheduler.prepare_call(
            "trimmer", context, continuation={"phase": phase, "session_id": session_id}
        )
        policy = policy_for("trimmer")
        workspace = self._make_workspace(call_id=call_id, policy=policy, context=context)
        session_key = f"trimmer:{session_id}"
        result = self._run_prepared_call(
            call_id,
            workspace=workspace,
            policy=policy,
            session_key=session_key,
            resume=bool(self.transport.ledger.resolve(session_key)),
        )
        if not self._franta_control_result_may_commit(call_id):
            return True
        self._commit_trimmer_result(call_id, workspace)
        return True

    # -------------------------------------------------------- worker launch

    def _ordinary_worker_launches(self) -> list[str]:
        """Select accepted non-sprint work without double-counting reservations."""

        state = self.scheduler.state
        launchable_states = {
            TaskState.LAUNCHING.value,
            TaskState.RETRY_PENDING.value,
            TaskState.REVISION_PENDING.value,
        }
        candidates = [
            task_id
            for task_id, task in state["tasks"].items()
            if task.get("sprint_id") is None
            and not task.get("non_slot_task")
            and task.get("agent_system") != "franta-sort"
            and task["state"] in launchable_states
        ]
        reserved = [
            task_id
            for task_id in candidates
            if state["tasks"][task_id].get("slot_reserved")
        ]
        unreserved = [task_id for task_id in candidates if task_id not in reserved]
        return reserved + unreserved[: self.scheduler.free_non_verifier_slots()]

    # --------------------------------------------------------------- sprint

    def _waiting_sprint_predecessor_task_ids(self, sprint_id: str) -> list[str]:
        """Return accepted tasks that must drain before this sprint reserves slots."""
        try:
            return exploration_runtime.waiting_sprint_predecessor_task_ids(
                self.scheduler.state,
                sprint_id,
            )
        except exploration_runtime.ExplorationRuntimeError as exc:
            raise SchedulerError(str(exc)) from exc

    def _waiting_sprint_drain_launches(self, sprint_id: str) -> list[str]:
        """Select only pre-sprint work, preserving existing slot accounting."""
        try:
            return exploration_runtime.waiting_sprint_drain_launches(
                self.scheduler.state,
                sprint_id,
                free_slots=self.scheduler.free_non_verifier_slots(),
            )
        except exploration_runtime.ExplorationRuntimeError as exc:
            raise SchedulerError(str(exc)) from exc

    def _advance_sprint(self) -> bool:
        state = self.scheduler.state
        sprint_id = state.get("active_sprint_id")
        if not sprint_id:
            return False
        sprint = state["sprints"][sprint_id]
        status = sprint["status"]
        if status == "waiting_for_slots":
            if self._waiting_sprint_predecessor_task_ids(sprint_id):
                return False
            if not self.scheduler.preflight_sprint_launch(sprint_id):
                return False
            # The four blueprints are already semantically final. The
            # deterministic scheduler invokes task-writing once per lane to
            # serialize and audit them; the skill is not allowed to plan or
            # revise their mathematics.
            batch_id = f"BATCH-{sprint_id}"
            skill_workspace = self._make_workspace(
                call_id=f"SPRINT-TW-{_safe_component(str(sprint_id), 'sprint ID')}",
                policy=policy_for("scheduler"),
                context={"sprint_id": sprint_id, "batch_id": batch_id},
            )
            staged = {
                str(item.get("operation_id")): item
                for item in self._skill_artifacts(skill_workspace, "task-writing")
            }
            for payload in exploration_runtime.sprint_task_writing_payloads(
                str(sprint_id), sprint
            ):
                operation_id = str(payload["operation_id"])
                if operation_id not in staged:
                    staged[operation_id] = SkillRuntime(
                        SkillContext.load(skill_workspace.path)
                    ).invoke("task-writing", payload)["artifact"]
            reports = [
                self._assignment_report(staged[f"{sprint_id}-lane-{index + 1}"])
                for index in range(4)
            ]
            task_ids = self.scheduler.launch_sprint(
                sprint_id, reports, batch_id=batch_id
            )
            # These are the four tasks just reserved atomically for this
            # sprint.  Start exactly this durable batch; a generic launch scan
            # could otherwise select unrelated work or see no nominally free
            # slots because the sprint reservations already consume all four.
            self._run_worker_batch(task_ids)
            self._advance_operations()
            self.scheduler.advance_sprint(sprint_id)
            return True
        if status in {"running", "draining_after_root_resolution"}:
            pending_task_ids = [
                task_id
                for task_id in sprint.get("task_ids", [])
                if state["tasks"][task_id]["state"]
                in {
                    TaskState.LAUNCHING.value,
                    TaskState.RETRY_PENDING.value,
                    TaskState.REVISION_PENDING.value,
                }
            ]
            if pending_task_ids:
                # Recovery and verifier-requested revisions remain inside the
                # active sprint batch.  No unrelated launchable task is
                # admitted while the sprint owns the four worker slots.
                self._run_worker_batch(pending_task_ids)
                self._advance_operations()
                state = self.scheduler.state
                sprint = state["sprints"][sprint_id]
                status = sprint["status"]
            new_status = self.scheduler.advance_sprint(sprint_id)
            return bool(pending_task_ids) or new_status != status
        if status == "awaiting_summary":
            call_id = self.scheduler.prepare_sprint_summarizer_call(sprint_id)
            self._run_memory_review_call(call_id)
            return True
        if status == "trimmer_continuation":
            if self.scheduler.gate == GateState.WAITING_FOR_HUMAN:
                return False
            if not isinstance(sprint.get("trim_integration"), Mapping):
                self._run_trim(initial=False)
                # human-guidance is a durable pause in this same trimmer
                # continuation.  The integration is attempted only after the
                # answer (or cancellation) lets the trimmer commit a trim.
                if self.scheduler.gate == GateState.WAITING_FOR_HUMAN:
                    return True
                sprint = self.scheduler.state["sprints"][sprint_id]
            integration = sprint.get("trim_integration")
            if not isinstance(integration, Mapping) or integration.get("status") != (
                "trim_committed"
            ):
                return False
            self.scheduler.complete_sprint_continuation(sprint_id)
            return True
        return False

    # -------------------------------------------------------- bootstrap queue

    def _bootstrap_state(self) -> tuple[int, dict[str, Any]]:
        loaded = self.store.load_control_state(BOOTSTRAP_STATE_KEY)
        if loaded is None:
            raise RuntimeErrorBase("bootstrap proposal state is missing")
        return int(loaded[0]), copy.deepcopy(dict(loaded[1]))

    def _save_bootstrap_state(self, revision: int, state: Mapping[str, Any]) -> None:
        self.store.compare_and_swap_control_state(BOOTSTRAP_STATE_KEY, revision, state)

    @staticmethod
    def _substitute(value: Any, mappings: Mapping[str, str]) -> Any:
        if isinstance(value, str):
            return mappings.get(value, value)
        if isinstance(value, Mapping):
            return {key: FrantaRuntime._substitute(item, mappings) for key, item in value.items()}
        if isinstance(value, list):
            return [FrantaRuntime._substitute(item, mappings) for item in value]
        return copy.deepcopy(value)

    @staticmethod
    def _bootstrap_canonical_body(payload: Mapping[str, Any]) -> dict[str, Any]:
        """Separate bootstrap workflow identity from canonical memory content."""

        body = copy.deepcopy(dict(payload))
        body.pop("proposal_id", None)
        return body

    def _reconcile_bootstrap_update_resolutions(self) -> bool:
        """Finish committed bootstrap update tails without reapplying patches."""

        revision, state = self._bootstrap_state()
        changed = False
        errors: list[str] = []
        for proposal in state.get("proposals", []):
            if proposal.get("kind") != "route_add":
                continue
            operation_id = str(proposal.get("operation_id") or "")
            proposal_id = str(proposal.get("proposal_id") or "")
            if not operation_id or not proposal_id:
                continue
            status = self.store.operation_status(operation_id)
            if (
                status is None
                or status.status != "committed"
                or status.operation_type != "route_update"
                or status.resolution != "updated"
                or not status.canonical_id
            ):
                continue
            canonical_id = str(status.canonical_id)
            prior_mapping = state.get("mappings", {}).get(proposal_id)
            prior_canonical = proposal.get("canonical_id")
            if (
                prior_mapping not in {None, canonical_id}
                or prior_canonical not in {None, canonical_id}
            ):
                error = (
                    f"bootstrap update {operation_id} conflicts with its committed "
                    f"canonical target {canonical_id}"
                )
                if proposal.get("update_reference_resolution_error") != error:
                    proposal["update_reference_resolution_error"] = error
                    changed = True
                errors.append(error)
                continue
            try:
                self.store.reconcile_update_resolution(
                    operation_id,
                    proposal_id,
                    canonical_id,
                    actor="bootstrap-recovery",
                )
            except Exception as exc:
                error = f"bootstrap update {operation_id} reconciliation failed: {exc}"
                if proposal.get("update_reference_resolution_error") != error:
                    proposal["update_reference_resolution_error"] = error
                    changed = True
                errors.append(error)
                continue
            if proposal.pop("update_reference_resolution_error", None) is not None:
                changed = True
            if proposal.get("status") != "committed":
                proposal["status"] = "committed"
                changed = True
            if proposal.get("canonical_id") != canonical_id:
                proposal["canonical_id"] = canonical_id
                changed = True
            if not proposal.get("update_reference_resolution_applied"):
                proposal["update_reference_resolution_applied"] = True
                changed = True
            if state.setdefault("mappings", {}).get(proposal_id) != canonical_id:
                state["mappings"][proposal_id] = canonical_id
                changed = True
            call_id = proposal.get("synthesizer_call_id")
            if call_id:
                call = self.scheduler.state.get("calls", {}).get(str(call_id))
                if call and call.get("status") == CallState.COMPLETED.value:
                    self.scheduler.mark_call_committed(str(call_id))
        if changed:
            state["stable"] = all(
                item.get("status") in {"committed", "rejected"}
                for item in state.get("proposals", [])
            )
            self._save_bootstrap_state(revision, state)
        if errors:
            raise ProjectNeedsAttention("; ".join(errors))
        return changed

    def _franta_tail_is_drained(self) -> bool:
        """Return whether no already-running Franta attempt/postprocess remains."""

        state = self.scheduler.state
        active_task_states = {
            TaskState.RUNNING.value,
            TaskState.STOPPING.value,
            TaskState.ATTEMPT_ENDED.value,
            TaskState.POSTPROCESSING.value,
        }
        if any(
            task.get("agent_system") != "franta-sort"
            and task.get("state") in active_task_states
            for task in state.get("tasks", {}).values()
        ):
            return False
        active_operation_states = {
            OperationState.RECEIVED.value,
            OperationState.WAITING_PREDECESSORS.value,
            OperationState.SYNTHESIZING.value,
            OperationState.VERIFYING.value,
        }
        if any(
            item.get("state") in active_operation_states
            for table in ("operations", "computations", "challenges")
            for item in state.get(table, {}).values()
        ):
            return False
        active_call_states = {
            CallState.RUNNING.value,
            CallState.COMPLETED.value,
        }
        return not any(
            call.get("kind")
            in {
                "worker",
                "synthesizer",
                "verifier",
                "challenge-verifier",
                "main-closure-review",
                "summarizer",
            }
            and call.get("status") in active_call_states
            for call in state.get("calls", {}).values()
        )

    def _advance_bootstrap_proposals(self) -> bool:
        reconciled = self._reconcile_bootstrap_update_resolutions()
        revision, state = self._bootstrap_state()
        if state.get("stable"):
            return reconciled
        changed = reconciled
        for proposal in state["proposals"]:
            if proposal["status"] != "pending":
                continue
            payload = self._substitute(proposal["payload"], state["mappings"])
            try:
                if proposal["kind"] == "route_add":
                    review_input = {
                        "operation_id": proposal["operation_id"],
                        "operation_digest": stable_digest(payload),
                        "proposal": payload,
                        "event_cursor": self.scheduler.event_cursor,
                        "bootstrap": True,
                    }
                    continuation = {
                        "bootstrap_operation_id": proposal["operation_id"]
                    }
                    call_id = self.scheduler.prepare_reconciled_call(
                        "synthesizer",
                        review_input,
                        continuation=continuation,
                        identity_fields=(
                            "operation_id",
                            "operation_digest",
                            "proposal",
                            "bootstrap",
                        ),
                    )
                    proposal["synthesizer_call_id"] = call_id
                    persisted_call = self.scheduler.state["calls"][call_id]
                    review_input = copy.deepcopy(dict(persisted_call["input"]))
                    policy = policy_for("synthesizer", review_memory_type="route")
                    workspace = self._make_workspace(
                        call_id=call_id, policy=policy, context=review_input
                    )
                    if persisted_call["status"] in {
                        CallState.COMPLETED.value,
                        CallState.COMMITTED.value,
                    }:
                        report = copy.deepcopy(dict(persisted_call["result"]))
                    else:
                        report = self._run_prepared_call(
                            call_id, workspace=workspace, policy=policy
                        )
                    resolution = report["resolution"]
                    if resolution == "new":
                        result = self.store.add_route(
                            proposal["operation_id"],
                            self._bootstrap_canonical_body(payload),
                            proposal_id=proposal["proposal_id"],
                            actor="bootstrap",
                        )
                        canonical_id = result.canonical_id
                    elif resolution == "duplicate":
                        canonical_id = str(report.get("canonical_id") or "")
                        if not canonical_id:
                            raise InvalidAgentOutput("bootstrap duplicate has no canonical ID")
                        self.store.record_duplicate_resolution(
                            proposal["operation_id"],
                            "route_add",
                            proposal["proposal_id"],
                            canonical_id,
                            payload,
                            actor="bootstrap",
                        )
                    else:
                        patch = report.get("patch")
                        if not isinstance(patch, Mapping):
                            raise InvalidAgentOutput("bootstrap route update has no patch")
                        result = self.store.update_route(
                            proposal["operation_id"],
                            patch,
                            proposal_id=proposal["proposal_id"],
                            actor="bootstrap",
                        )
                        canonical_id = result.canonical_id
                    if (
                        self.scheduler.state["calls"][call_id]["status"]
                        != CallState.COMMITTED.value
                    ):
                        self.scheduler.mark_call_committed(call_id)
                elif proposal["kind"] == "memo":
                    result = self.store.add_memo(
                        proposal["operation_id"],
                        self._bootstrap_canonical_body(payload),
                        proposal_id=proposal["proposal_id"],
                        actor="bootstrap",
                    )
                    canonical_id = result.canonical_id
                else:
                    result = self.store.add_claim(
                        proposal["operation_id"],
                        self._bootstrap_canonical_body(payload),
                        proposal_id=proposal["proposal_id"],
                        actor="bootstrap",
                    )
                    canonical_id = result.canonical_id
                if not canonical_id:
                    raise RuntimeErrorBase("bootstrap operation produced no canonical ID")
                if proposal["kind"] == "route_add" and resolution == "update":
                    proposal["update_reference_resolution_applied"] = True
                proposal["status"] = "committed"
                proposal["canonical_id"] = canonical_id
                state["mappings"][proposal["proposal_id"]] = canonical_id
            except Exception as exc:
                proposal["status"] = "rejected"
                proposal["error"] = str(exc)
            changed = True
        state["stable"] = all(
            item["status"] in {"committed", "rejected"} for item in state["proposals"]
        )
        self._save_bootstrap_state(revision, state)
        return changed

    # ------------------------------------------------------------ recovery/run

    def recover(self) -> dict[str, list[str]]:
        self._replay_explorer_trusted_receipts()
        self._recover_staged_main_sort_progress()
        self._recover_staged_worker_progress()
        self.scheduler.recover()
        pending = self.scheduler.reconcile_pending_ingestion()
        self._reconcile_explorer_promotions()
        self._reconcile_bootstrap_update_resolutions()
        self._reconcile_committed_main_checkpoints()
        return pending

    def _reconcile_committed_main_checkpoints(self) -> None:
        """Finish the small durable tail after a main result was committed."""

        state = self.scheduler.state
        current_cursor = int(state["main_checkpoint"].get("event_cursor", 0))
        candidates = [
            call
            for call in state["calls"].values()
            if call.get("kind") == "main"
            and call.get("status") == CallState.COMMITTED.value
            and int(call.get("input", {}).get("event_cursor", 0)) > current_cursor
        ]
        for call in sorted(
            candidates,
            key=lambda item: int(item.get("input", {}).get("event_cursor", 0)),
        ):
            result = call.get("result") or {}
            waiting = (
                result.get("wait_for_task_ids", [])
                if result.get("decision") == "wait_for_results"
                else []
            )
            self.scheduler.record_main_checkpoint(
                call_id=str(call["call_id"]),
                observed_event_cursor=int(call["input"].get("event_cursor", 0)),
                waiting_for=[str(item) for item in waiting],
            )

    def _retire_invalid_control_workspace(
        self, call_id: str, workspace: MaterializedWorkspace
    ) -> None:
        if not workspace.path.exists():
            return
        call = self.scheduler.state["calls"][call_id]
        destination_root = self.layout.private / "invalid-control-workspaces"
        destination_root.mkdir(parents=True, exist_ok=True)
        destination_name = (
            f"{_safe_component(call_id, 'call ID')}-attempt-"
            f"{int(call.get('attempt', 0)):04d}"
        )
        if call.get("kind") == "advisor-proposal":
            workspace_generation = self._workspace_generation(workspace).replace(
                ":", "-"
            )
            destination_name += f"-generation-{workspace_generation}"
        destination = destination_root / destination_name
        if destination.exists():
            raise RuntimeErrorBase(
                f"invalid control workspace archive already exists: {destination}"
            )
        os.replace(workspace.path, destination)

    def _recover_control_calls(self) -> bool:
        """Resume or commit exact persisted main/trimmer calls before new planning."""

        phase = self.scheduler.alternation_phase
        if phase not in {None, "franta_run"}:
            return False
        changed = False
        for call_id, initial in list(self.scheduler.state["calls"].items()):
            if initial.get("kind") not in {"main", "trimmer"} or initial.get(
                "status"
            ) not in {
                CallState.PREPARED.value,
                CallState.RETRY_PENDING.value,
                CallState.COMPLETED.value,
            }:
                continue
            while True:
                call = self.scheduler.state["calls"][call_id]
                policy = policy_for(str(call["kind"]))
                workspace = self._make_workspace(
                    call_id=call_id,
                    policy=policy,
                    context=call["input"],
                )
                if call["kind"] == "main":
                    continuation = call.get("continuation", {})
                    if (
                        continuation.get("session_key") is None
                        and self._is_legacy_snapshotless_main_context(
                            call["input"]
                        )
                    ):
                        revision = int(
                            call["input"].get("portfolio_revision", 0)
                        )
                        session_key = f"main:portfolio:{revision}"
                        resume = bool(
                            self.transport.ledger.resolve(session_key)
                        )
                    else:
                        session_key = MAIN_SESSION_KEY
                        resume = self._main_session_resume(call_id)
                else:
                    session_id = str(
                        call.get("continuation", {}).get("session_id") or "initial"
                    )
                    session_key = f"trimmer:{session_id}"
                    resume = bool(self.transport.ledger.resolve(session_key))
                self._run_prepared_call(
                    call_id,
                    workspace=workspace,
                    policy=policy,
                    session_key=session_key,
                    resume=resume,
                )
                if not self._franta_control_result_may_commit(call_id):
                    changed = True
                    break
                try:
                    if call["kind"] == "main":
                        self._commit_main_result(call_id, workspace)
                    else:
                        self._commit_trimmer_result(call_id, workspace)
                    changed = True
                    break
                except (
                    SchedulerError,
                    WorkflowError,
                    InvalidAgentOutput,
                    ValueError,
                ) as exc:
                    current = self.scheduler.state["calls"][call_id]
                    if current["status"] != CallState.COMPLETED.value:
                        raise
                    if not self.scheduler.reject_call_result(call_id, str(exc)):
                        raise TransportFailure(
                            f"invalid-output retry limit exhausted for {call_id}"
                        ) from exc
                    self._retire_invalid_control_workspace(call_id, workspace)
        return changed

    def _commit_completed_calls(self) -> bool:
        changed = False
        for call_id, call in list(self.scheduler.state["calls"].items()):
            if call["status"] != CallState.COMPLETED.value:
                continue
            if call["kind"] == "verifier" and not self.scheduler.source_attempt_terminal(
                str(call.get("continuation", {}).get("operation_id") or "")
            ):
                continue
            if call["kind"] in {
                "synthesizer",
                "verifier",
                "challenge-verifier",
                "main-closure-review",
                "summarizer",
            }:
                if self._run_contained_memory_review_call(call_id):
                    changed = True
        return changed

    def run(self, *, resume: bool = False, max_cycles: int | None = None,
            _lock_held: bool = False) -> dict[str, Any]:
        """Run until completion, attention/human pause, or durable quiescence.

        ``max_cycles`` is a process/testing bound only; it creates no agent or
        memory quota and leaves all unfinished state recoverable.
        """

        with (nullcontext() if _lock_held else self.lock):
            self._dashboard_run_started = time.time()
            self._record_dashboard_run("running")
            run_failed = False
            try:
                self.start_services()
                if resume:
                    self.recover()
                cycles = 0
                while max_cycles is None or cycles < max_cycles:
                    cycles += 1
                    progressed = self._ingest_human_guidance()
                    if self._ingest_dashboard_commands():
                        progressed = True
                    opened_without_trim = False
                    defer_main_planning = False
                    recovered_control_call = False
                    phase = self.scheduler.alternation_phase
                    if phase in {"franta_sort", "franta_run", "franta_drain"}:
                        after_phase = self.scheduler.tick_alternation()
                        if after_phase != phase:
                            progressed = True
                    if self._recover_control_calls():
                        progressed = True
                        defer_main_planning = True
                        recovered_control_call = True
                    if self._commit_completed_calls():
                        progressed = True
                        defer_main_planning = True
                    if self._advance_bootstrap_proposals():
                        progressed = True
                        defer_main_planning = True
                    bootstrap_stable = self._bootstrap_state()[1].get("stable", False)
                    if (
                        bootstrap_stable
                        and self.scheduler.alternation_phase is not None
                        and self.scheduler.activate_alternation()
                    ):
                        progressed = True
                    if (
                        self.scheduler.alternation_phase is None
                        and bootstrap_stable
                        and self.scheduler.gate == GateState.TRIMMING
                        and self.scheduler.state["trim"].get("portfolio") is None
                        and self.scheduler.state["trim"].get("active_review") is None
                        and self.scheduler.state["trim"].get("active_trim") is None
                    ):
                        self.scheduler.open_assignment_without_trim()
                        progressed = True
                        opened_without_trim = True
                        defer_main_planning = True
                    if self._advance_operations():
                        progressed = True
                        defer_main_planning = True
                    handled_by_alternation = False
                    if self.explorer_program is not None:
                        explorer_advance = self.explorer_program.advance_turn()
                        handled_by_alternation = explorer_advance.handled
                        if explorer_advance.progressed:
                            progressed = True
                    phase = self.scheduler.alternation_phase
                    if not handled_by_alternation and phase is not None:
                        before_phase = phase
                        phase = self.scheduler.tick_alternation()
                        if phase != before_phase:
                            progressed = True
                    if phase == "franta_sort":
                        handled_by_alternation = True
                        self._run_franta_sort_barrier()
                        progressed = True
                    elif not handled_by_alternation and phase == "franta_drain":
                        handled_by_alternation = True
                        if self.scheduler.stop_unlaunched_franta_tasks_for_drain():
                            progressed = True
                        if self._reconcile_explorer_promotions():
                            progressed = True
                        if self._franta_tail_is_drained():
                            if (
                                self.advisor_program is not None
                                and self.scheduler.gate != GateState.COMPLETED
                            ):
                                if self._advance_advisor():
                                    progressed = True
                            elif self.advisor_program is None:
                                self.scheduler.complete_franta_drain()
                                progressed = True

                    if (
                        not handled_by_alternation
                        and not opened_without_trim
                        and not recovered_control_call
                    ):
                        active_sprint_id = self.scheduler.state.get("active_sprint_id")
                        active_sprint = self.scheduler.state["sprints"].get(active_sprint_id)
                        if active_sprint and active_sprint.get("status") == (
                            "waiting_for_slots"
                        ):
                            drain_task_ids = self._waiting_sprint_drain_launches(
                                str(active_sprint_id)
                            )
                            if drain_task_ids:
                                self._run_worker_batch(drain_task_ids)
                                progressed = True
                                self._advance_operations()
                            if (
                                not self._waiting_sprint_predecessor_task_ids(
                                    str(active_sprint_id)
                                )
                                and self._advance_sprint()
                            ):
                                progressed = True
                        elif active_sprint:
                            if self._advance_sprint():
                                progressed = True
                        else:
                            launchable = self._ordinary_worker_launches()
                            if launchable:
                                self._run_worker_batch(launchable)
                                progressed = True
                                self._advance_operations()
                        gate = self.scheduler.gate
                        if gate == GateState.REVIEWING_TRIM:
                            self._run_trim_review()
                            progressed = True
                        elif gate == GateState.TRIMMING and self.scheduler.state["trim"].get("active_trim") and not self.scheduler.state.get("active_sprint_id"):
                            self._run_trim(initial=False)
                            progressed = True
                        elif (
                            gate == GateState.OPEN
                            and not self.scheduler.state.get("active_sprint_id")
                            and self._ordinary_worker_launches()
                        ):
                            # A disconnect or verifier-requested revision gets its
                            # next accepted attempt before a new planning call.
                            pass
                        elif not defer_main_planning and self._main_should_run():
                            self._run_main()
                            progressed = True

                    state = self.scheduler.state
                    if self.scheduler.gate == GateState.COMPLETED:
                        break
                    if state.get("halt_requested") or self.scheduler.has_blocking_attention():
                        break
                    if self.scheduler.gate == GateState.WAITING_FOR_HUMAN:
                        break
                    if not progressed:
                        break
                return self.status()
            except BaseException:
                run_failed = True
                raise
            finally:
                self.stop_services()
                waiting = self.scheduler.gate == GateState.WAITING_FOR_HUMAN or (
                    self.advisor_program is not None
                    and advisor_status(self.scheduler.advisor_state) == "waiting_for_human"
                )
                self._record_dashboard_run(
                    "error" if run_failed else "completed"
                    if self.scheduler.gate == GateState.COMPLETED else
                    "waiting_for_human" if waiting else "stopped"
                )

    # --------------------------------------------------------------- operator

    def _record_dashboard_run(self, status: str) -> None:
        """Best-effort dashboard telemetry never changes research state."""

        directory = self.layout.private / "dashboard"
        if not directory.is_dir() or directory.is_symlink():
            return
        try:
            from datetime import datetime, timezone
            from .dashboard_commands import publish_json
            now = datetime.now(timezone.utc).isoformat()
            publish_json(directory / "runner.json", {
                "pid": os.getpid(), "status": status,
                "started_at": datetime.fromtimestamp(self._dashboard_run_started, timezone.utc).isoformat(),
                "updated_at": now, "ended_at": None if status == "running" else now,
            }, replace=True)
        except Exception:
            pass

    def _ingest_dashboard_commands(self) -> bool:
        if not (self.layout.private / "dashboard" / "commands").is_dir():
            return False
        try:
            from .dashboard_commands import consume_advisor_commands
            return consume_advisor_commands(self)
        except Exception:
            # Optional UI transport failures must not stop research. An accepted
            # feedback command can be retried idempotently if its receipt failed.
            return False

    def _ingest_human_guidance(self) -> bool:
        """Import operator files through the running scheduler's own writer."""

        received = self.scheduler.enqueue_human_guidance(
            read_human_guidance_inbox(self.layout.root)
        )
        reclaimed = self.scheduler.reconcile_human_guidance()
        return bool(received or reclaimed)

    def status(self) -> dict[str, Any]:
        state = self.scheduler.state
        counts: dict[str, int] = {}
        for record in self.store.list_records():
            key = record.memory_type.value
            counts[key] = counts.get(key, 0) + 1
        return self._format_status(self.layout, self.config, state, counts)

    @classmethod
    def read_status(cls, project_dir: str | os.PathLike[str]) -> dict[str, Any]:
        """Read durable status without constructing or repairing a runtime."""

        from .dashboard_read import _read_connection

        layout = ProjectLayout.at(project_dir)
        config = _load_runtime_payload(layout)
        # Share the dashboard's read-only connection and WAL snapshot semantics.
        # State and memory counts must come from the same committed snapshot.
        with _read_connection(layout.database) as connection:
            if connection is None:
                raise RuntimeErrorBase(f"project database is missing: {layout.database}")
            row = connection.execute(
                "SELECT payload_json FROM control_state WHERE state_key=?",
                (Scheduler.CONTROL_KEY,),
            ).fetchone()
            if row is None:
                raise RuntimeErrorBase("scheduler control state is missing")
            state = json.loads(row["payload_json"])
            counts = {
                row["memory_type"]: int(row["count"])
                for row in connection.execute(
                    "SELECT memory_type,COUNT(*) AS count FROM memories GROUP BY memory_type"
                )
            }
        return cls._format_status(layout, config, state, counts)

    @staticmethod
    def _format_status(
        layout: ProjectLayout,
        config: Mapping[str, Any],
        state: Mapping[str, Any],
        counts: Mapping[str, int],
    ) -> dict[str, Any]:
        events = state.get("events", [])
        result = {
            "project": config["project_name"],
            "project_dir": str(layout.root),
            "gate": GateState(state["gate"]).value,
            "event_cursor": int(events[-1]["event_id"]) if events else 0,
            "root": copy.deepcopy(state["root"]),
            "portfolio_revision": state["trim"]["portfolio_revision"],
            "tasks": {
                name: sum(1 for item in state["tasks"].values() if item["state"] == name)
                for name in sorted({item["state"] for item in state["tasks"].values()})
            },
            "memories": dict(counts),
            "needs_attention": [
                copy.deepcopy(item)
                for item in state["needs_attention"]
                if item.get("resolved_at") is None
            ],
            "guidance": copy.deepcopy(state["guidance"].get("active")),
        }
        inbox = state.get("human_guidance_inbox", {})
        guidance_records = {
            item["guidance_id"]: {**item, "status": "pending"}
            for item in read_human_guidance_inbox(layout.root)
        }
        guidance_records.update(copy.deepcopy(inbox))
        if guidance_records:
            result["human_guidance_inbox"] = sorted(
                guidance_records.values(),
                key=lambda item: (item["received_at"], item["guidance_id"]),
            )
        # Explorer is opt-in.  Keep the exact legacy status surface unchanged,
        # while making the active alternating phase observable for enabled
        # projects and restart diagnostics.
        phase_control = state.get("phase_control")
        if isinstance(phase_control, Mapping) and phase_control.get("enabled") is True:
            result["alternation_phase"] = str(phase_control.get("phase") or "")
            result["alternation_cycle"] = int(phase_control.get("cycle", 0))
        advisor_control = state.get("advisor_control")
        if isinstance(advisor_control, Mapping):
            active = advisor_control.get("active")
            advisor_value: dict[str, Any] = {
                "status": advisor_status(advisor_control),
                "session_key": advisor_control.get("session_key"),
                "session_bound": advisor_control.get("session_id") is not None,
                "completed_rounds": len(advisor_control.get("history", [])),
                "current_problem": copy.deepcopy(Scheduler._effective_problem_locked(state)),
            }
            if isinstance(active, Mapping):
                advisor_value.update(
                    {
                        "advisor_index": active.get("advisor_index"),
                        "feedback_request_id": (
                            (active.get("selection_report") or {}).get(
                                "feedback_request_id"
                            )
                            if isinstance(active.get("selection_report"), Mapping)
                            else None
                        ),
                        "selection_report": copy.deepcopy(
                            active.get("selection_report")
                        ),
                        "selection_report_path": active.get(
                            "archived_report_path"
                        ),
                    }
                )
            result["advisor"] = advisor_value
        return result

    def submit_guidance(self, request_id: str, response: str) -> None:
        self.scheduler.resolve_human_guidance(request_id, response=response)

    def cancel_guidance(self, request_id: str) -> None:
        self.scheduler.resolve_human_guidance(request_id, cancelled=True)

    def submit_advisor_feedback(
        self, request_id: str, response: Mapping[str, Any]
    ) -> None:
        """Bind a human's exact listed/custom choices to the pending report."""

        control = self.scheduler.advisor_state
        rounds: list[Mapping[str, Any]] = []
        active = control.get("active")
        if isinstance(active, Mapping):
            rounds.append(active)
        rounds.extend(
            item
            for item in control.get("history", [])
            if isinstance(item, Mapping)
        )
        matching = [
            item
            for item in rounds
            if isinstance(item.get("selection_report"), Mapping)
            and item["selection_report"].get("feedback_request_id") == request_id
        ]
        if len(matching) != 1:
            raise RuntimeErrorBase("Advisor feedback request ID does not match")
        round_state = matching[0]
        report = round_state["selection_report"]
        feedback = feedback_from_operator(report=report, response=response)
        existing = round_state.get("human_feedback")
        if isinstance(existing, Mapping):
            persisted = HumanFeedback.from_dict(existing)
            if persisted.digest != feedback.digest:
                raise RuntimeErrorBase("Advisor human feedback cannot be replaced")
            return
        if round_state is not active or advisor_status(control) != "waiting_for_human":
            raise RuntimeErrorBase("Advisor is not waiting for human feedback")
        self.scheduler.bind_advisor_feedback(feedback.to_dict())

    def cancel_sprint(self, sprint_id: str, reason: str) -> str:
        return self.scheduler.cancel_sprint(
            sprint_id,
            authorized_by="operator",
            reason=reason,
        )

    def list_temporary_references(
        self, *, state: str | None = None
    ) -> list[dict[str, Any]]:
        return self.store.list_temporary_references(state=state)

    def temporary_reference_status(self, temporary_id: str) -> dict[str, Any]:
        value = self.store.temporary_reference_status(temporary_id)
        if value is None:
            raise RuntimeErrorBase(f"temporary reference not found: {temporary_id}")
        return value

    def resolve_temporary_reference(
        self,
        temporary_id: str,
        canonical_id: str,
        *,
        resolution: str,
        operation_id: str,
    ) -> tuple[str, ...]:
        return self.scheduler.resolve_temporary_reference(
            temporary_id,
            canonical_id,
            resolution=resolution,
            operation_id=operation_id,
            actor="operator",
        )

    def abandon_temporary_reference(
        self,
        temporary_id: str,
        *,
        reason: str,
        operation_id: str,
    ) -> tuple[str, ...]:
        return self.scheduler.abandon_temporary_reference(
            temporary_id,
            reason=reason,
            operation_id=operation_id,
            actor="operator",
        )


__all__ = [
    "AgentCall",
    "AgentExecutor",
    "FrantaRuntime",
    "InvalidAgentOutput",
    "ProjectNeedsAttention",
    "RuntimeErrorBase",
]
