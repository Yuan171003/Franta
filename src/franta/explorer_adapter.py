"""The single Franta integration seam for the portable Explorer block.

Explorer owns prompts, models, schemas, tools, records, search, control, and
worker-wave behavior. This module binds those host-neutral contracts to Franta
IDs, policies, durable calls, workspaces, receipts, and the main-sort handoff.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, replace
import hashlib
import json
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from explorer_system.agents import (
    build_launch_spec,
    owns_agent_role,
    select_guidance_variant,
    write_response_schemas,
)
from explorer_system.contracts import (
    ExplorerAttemptAccessGrant,
    ExplorerError,
    ExplorerHandoff,
    ExplorerPublishedMemoryDocument,
    ExplorerPublishedMemorySnapshot,
    ExplorerReadScope,
    ExplorerRecord,
    FrozenExplorerTurn,
)
from explorer_system.interfaces import ExplorerTurnContext
from explorer_system.main_sort import (
    DEFAULT_MAIN_SORT_BLOCK,
    HostSortTools,
    MainSortBlock,
    MainSortSnapshotBlock,
    MainSortValidationBlock,
    MainSortValidationError as _PortableMainSortValidationError,
    SnapshotError as _PortableSnapshotError,
    ValidatedMainSortSubmission,
)
from explorer_system.repository import ExplorerRepository
from explorer_system.search import AuditedExplorerAPI as PortableAuditedExplorerAPI
from explorer_system.program import ExplorerProgram
from explorer_system.service import ExplorerAttempt, ExplorerLimits, ExplorerService
from explorer_system.settings import (
    ExplorerSettings as PortableExplorerSettings,
    ExplorerSettingsValidationError,
)
from explorer_system.tools import (
    EXPLORER_SKILLS,
    ExplorerToolValidationError,
    explorer_tool_definitions,
    explorer_worker_skills,
    normalize_check_result_payload,
    normalize_portfolio_fetch_payload,
    normalize_portfolio_search_payload,
    normalize_scratch_payload,
    normalize_summary_payload,
)

from .contracts.agent_access import (
    SEARCHABLE_MEMORY_TYPES,
    AccessPolicy,
    MemoryRecord,
    _ID_RE,
    policy_for,
)
from .contracts.workflows import CallState, WorkflowError
from .prompts import ModelConfig
from .read_access.audit import AuditLog
from .read_access.memory import AccessError, InMemoryBackend, MemoryBackend


FRANTA_SORT_TOOLS = HostSortTools(
    published_search="internal-search",
    progress_writer="record-progress",
    provenance_field="explorer_provenance",
    computation_exports_field="explorer_computation_promotions",
)

(
    EXPLORER_SNAPSHOT_FORMAT_VERSION,
    EXPLORER_SNAPSHOT_RELATIVE_PATH,
) = DEFAULT_MAIN_SORT_BLOCK.snapshot_contract()


class MainSortSnapshotError(RuntimeError):
    """Franta adapter error for a rejected portable snapshot."""


class MainSortContractError(ValueError):
    """Franta adapter error for a rejected portable sorter result."""


def main_sort_snapshot_contract(
    *, main_sort_block: MainSortSnapshotBlock = DEFAULT_MAIN_SORT_BLOCK
) -> tuple[int, Path]:
    """Expose the portable snapshot identity through Franta's sole seam."""

    return main_sort_block.snapshot_contract()


def render_main_sort_snapshot_files(
    snapshot: Mapping[str, Any],
    *,
    main_sort_block: MainSortSnapshotBlock = DEFAULT_MAIN_SORT_BLOCK,
) -> dict[Path, str]:
    """Render one frozen snapshot through the isolated main-sort block."""

    try:
        return main_sort_block.render_snapshot_files(snapshot)
    except _PortableSnapshotError as exc:
        raise MainSortSnapshotError(str(exc)) from exc


def main_sort_snapshot_digest(
    snapshot: Mapping[str, Any],
    *,
    main_sort_block: MainSortSnapshotBlock = DEFAULT_MAIN_SORT_BLOCK,
) -> str:
    """Return the block-owned digest for one frozen snapshot."""

    try:
        return main_sort_block.snapshot_digest(snapshot)
    except _PortableSnapshotError as exc:
        raise MainSortSnapshotError(str(exc)) from exc


def validate_main_sort_materialized_snapshot(
    workspace: str | Path,
    snapshot: Mapping[str, Any],
    *,
    main_sort_block: MainSortSnapshotBlock = DEFAULT_MAIN_SORT_BLOCK,
) -> Path:
    """Validate the block-owned immutable workspace input."""

    try:
        return main_sort_block.validate_materialized_snapshot(workspace, snapshot)
    except _PortableSnapshotError as exc:
        raise MainSortSnapshotError(str(exc)) from exc


def validate_main_sort_submission(
    result: Mapping[str, Any],
    progress: Sequence[Mapping[str, Any]],
    *,
    task_id: str,
    attempt: int,
    sort_run_id: str,
    source_is_allowed: Callable[[str], bool],
    main_sort_block: MainSortValidationBlock = DEFAULT_MAIN_SORT_BLOCK,
) -> ValidatedMainSortSubmission:
    """Validate one authenticated result through the isolated block."""

    try:
        return main_sort_block.validate_submission(
            result,
            progress,
            task_id=task_id,
            attempt=attempt,
            sort_run_id=sort_run_id,
            source_is_allowed=source_is_allowed,
        )
    except _PortableMainSortValidationError as exc:
        raise MainSortContractError(str(exc)) from exc


def validate_main_sort_progress_operation(
    operation: Mapping[str, Any],
    *,
    sort_run_id: str,
    main_sort_block: MainSortValidationBlock = DEFAULT_MAIN_SORT_BLOCK,
) -> None:
    """Validate a progress operation through the isolated block contract."""

    try:
        main_sort_block.validate_progress_operation(
            operation, sort_run_id=sort_run_id
        )
    except _PortableMainSortValidationError as exc:
        raise MainSortContractError(str(exc)) from exc


def normalize_main_sort_computation_promotions(
    value: Any,
    *,
    main_sort_block: MainSortValidationBlock = DEFAULT_MAIN_SORT_BLOCK,
) -> list[dict[str, Any]]:
    """Normalize XCAS declarations through the isolated block contract."""

    try:
        return main_sort_block.normalize_computation_promotions(value)
    except _PortableMainSortValidationError as exc:
        raise MainSortContractError(str(exc)) from exc


def main_sort_operation_kinds(
    *, main_sort_block: MainSortValidationBlock = DEFAULT_MAIN_SORT_BLOCK
) -> frozenset[str]:
    """Return the block-owned proposal vocabulary."""

    return main_sort_block.operation_kinds()


def main_sort_operation_has_exact_provenance(
    operation: Mapping[str, Any],
    *,
    sort_run_id: str,
    main_sort_block: MainSortValidationBlock = DEFAULT_MAIN_SORT_BLOCK,
) -> bool:
    """Return whether one operation satisfies the frozen-source contract."""

    return main_sort_block.operation_has_exact_provenance(
        operation, sort_run_id=sort_run_id
    )


def main_sort_computation_has_exact_provenance(
    computation: Mapping[str, Any],
    *,
    sort_run_id: str,
    main_sort_block: MainSortValidationBlock = DEFAULT_MAIN_SORT_BLOCK,
) -> bool:
    """Return whether one trusted computation has exact sort provenance."""

    return main_sort_block.computation_has_exact_provenance(
        computation, sort_run_id=sort_run_id
    )


# Private Franta call sites historically used this name.  Keep it local to the
# adapter so no other Franta module imports the portable block directly.
explorer_snapshot_digest = main_sort_snapshot_digest


class AuditedExplorerAPI(PortableAuditedExplorerAPI):
    """Bind portable joint search to Franta canonical IDs and exceptions."""

    def __init__(
        self,
        canonical_backend: MemoryBackend,
        repository: ExplorerRepository,
        canonical_policy: AccessPolicy,
        explorer_scope: ExplorerReadScope,
        *,
        audit: AuditLog | None = None,
        caller_id: str = "unknown",
    ) -> None:
        super().__init__(
            canonical_backend,
            repository,
            canonical_policy,
            explorer_scope,
            audit=audit,
            caller_id=caller_id,
            canonical_search_types=SEARCHABLE_MEMORY_TYPES,
            canonical_id_pattern=_ID_RE,
            access_error=AccessError,
        )


def _frozen_franta_backend(
    grant: ExplorerAttemptAccessGrant,
) -> InMemoryBackend:
    """Rebuild the exact private Franta snapshot pinned in an Attempt-3 grant."""

    records: list[MemoryRecord] = []
    if not grant.published_documents:
        empty_digest = hashlib.sha256(b"[]").hexdigest()
        if grant.host_snapshot_digest != empty_digest:
            # Early development v2 grants recorded only the snapshot digest.
            # Reopening the live backend would make their retry nondeterministic;
            # fail closed instead. This path does not affect legacy v1 calls.
            raise RuntimeError(
                "Explorer full-memory grant lacks its frozen host snapshot"
            )
        return InMemoryBackend()
    for document in grant.published_documents:
        if document.full_record_json is None:
            raise RuntimeError(
                "Explorer full-memory grant lacks a frozen host record"
            )
        try:
            value = json.loads(document.full_record_json)
        except json.JSONDecodeError as exc:  # contract validation is defensive
            raise RuntimeError("Explorer frozen host record is invalid") from exc
        if not isinstance(value, Mapping):
            raise RuntimeError("Explorer frozen host record is invalid")
        if (
            value.get("id") != document.source_id
            or value.get("memory_type") != document.memory_kind
            or value.get("abstract") != document.abstract
        ):
            raise RuntimeError(
                "Explorer frozen host record does not match its projection"
            )
        predecessor_ids = value.get("predecessor_ids", ())
        metadata = value.get("metadata", {})
        if (
            not isinstance(predecessor_ids, list)
            or not all(isinstance(item, str) for item in predecessor_ids)
            or not isinstance(metadata, Mapping)
        ):
            raise RuntimeError("Explorer frozen host record is malformed")
        status = value.get("status")
        records.append(
            MemoryRecord(
                memory_id=document.source_id,
                memory_type=document.memory_kind,
                abstract=document.abstract,
                content=str(value.get("content") or ""),
                title=str(value.get("title") or ""),
                active=(status != "inactive"),
                withdrawn=(status == "withdrawn"),
                revision=value.get("revision"),
                predecessor_ids=tuple(predecessor_ids),
                metadata=copy.deepcopy(dict(metadata)),
            )
        )
    return InMemoryBackend(records)


def _build_main_sort_explorer_snapshot(
    repository: ExplorerRepository,
    *,
    sort_run_id: str,
    turn_id: str,
    high_water: int,
    source_digest: str,
    source_records: Sequence[ExplorerRecord],
    main_sort_block: MainSortSnapshotBlock = DEFAULT_MAIN_SORT_BLOCK,
) -> dict[str, Any]:
    records: list[dict[str, Any]] = []
    for record in source_records:
        evidence = [
            {
                "evidence_id": item.evidence_id,
                "operation_id": item.operation_id,
                "execution_succeeded": item.execution_succeeded,
                "turn_id": item.turn_id,
                "worker_session_id": item.worker_session_id,
                "attempt_no": item.attempt_no,
                "output_artifact_sha256": item.output_artifact_sha256,
            }
            for item in repository.list_cas_evidence_for_record(record.record_id)
        ]
        records.append(
            {
                "id": record.record_id,
                "record_space": "explorer",
                "record_type": record.record_type,
                "record_kind": record.record_kind,
                "status": "provisional",
                "title": record.title,
                "seq": record.seq,
                "operation_id": record.operation_id,
                "input_digest": record.input_digest,
                "content_digest": record.content_digest,
                "turn_id": record.turn_id,
                "worker_session_id": record.worker_session_id,
                "attempt_no": record.attempt_no,
                "abstract": record.abstract,
                "content": record.content,
                "related_memory_ids": list(record.related_memory_ids),
                "cas_operation_ids": list(record.cas_operation_ids),
                "cas_evidence": evidence,
                "source_scratch_ids": list(record.source_scratch_ids),
                "source_set_digest": record.source_set_digest,
                "directions_tried": list(record.directions_tried),
                "main_progress": record.main_progress,
                "main_obstacles": record.main_obstacles,
                "created_at": record.created_at,
            }
        )
    try:
        return main_sort_block.build_snapshot(
            sort_run_id=sort_run_id,
            turn_id=turn_id,
            source_high_water_seq=high_water,
            source_set_digest=source_digest,
            records=records,
        )
    except _PortableSnapshotError as exc:
        raise MainSortSnapshotError(str(exc)) from exc


def build_main_sort_explorer_snapshot_for_frozen_turn(
    repository: ExplorerRepository,
    *,
    sort_run_id: str,
    frozen_turn: FrozenExplorerTurn,
    main_sort_block: MainSortSnapshotBlock = DEFAULT_MAIN_SORT_BLOCK,
) -> dict[str, Any]:
    """Render and validate a snapshot before its Franta task is allocated."""

    return _build_main_sort_explorer_snapshot(
        repository,
        sort_run_id=sort_run_id,
        turn_id=frozen_turn.turn_id,
        high_water=frozen_turn.high_water_seq,
        source_digest=frozen_turn.source_set_digest,
        source_records=repository.records_for_frozen_turn(frozen_turn),
        main_sort_block=main_sort_block,
    )


def build_main_sort_explorer_snapshot(
    repository: ExplorerRepository,
    task_card: Mapping[str, Any],
    *,
    main_sort_block: MainSortSnapshotBlock = DEFAULT_MAIN_SORT_BLOCK,
) -> dict[str, Any]:
    """Build the complete immutable Explorer input for one pinned sort task."""

    if not isinstance(task_card, Mapping):
        raise RuntimeError("main-sort snapshot requires a task card")
    sort_run_id = str(task_card.get("sort_run_id") or "")
    task_id = str(task_card.get("task_id") or "")
    turn_id = str(task_card.get("explorer_turn_id") or "")
    source_digest = str(task_card.get("source_set_digest") or "")
    source_access_mode = task_card.get("source_access_mode")
    snapshot_path = task_card.get("explorer_snapshot_path")
    snapshot_format_version = task_card.get("explorer_snapshot_format_version")
    pinned_snapshot_digest = task_card.get("explorer_snapshot_digest")
    try:
        high_water = int(task_card["source_high_water_seq"])
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError(
            "main-sort snapshot lacks a frozen high-water sequence"
        ) from exc
    run = repository.get_handoff_run(sort_run_id)
    if run is None:
        raise RuntimeError("main-sort snapshot has no Explorer handoff run")
    if (
        not sort_run_id
        or not task_id
        or not turn_id
        or not source_digest
        or run.host_context_id != task_id
        or run.turn_id != turn_id
        or run.source_high_water_seq != high_water
        or run.source_set_digest != source_digest
        or snapshot_format_version != EXPLORER_SNAPSHOT_FORMAT_VERSION
        or source_access_mode
        != f"explorer-snapshot-v{EXPLORER_SNAPSHOT_FORMAT_VERSION}"
        or snapshot_path != EXPLORER_SNAPSHOT_RELATIVE_PATH.as_posix()
        or not isinstance(pinned_snapshot_digest, str)
    ):
        raise RuntimeError(
            "main-sort snapshot identity does not match its frozen handoff"
        )
    snapshot = _build_main_sort_explorer_snapshot(
        repository,
        sort_run_id=sort_run_id,
        turn_id=turn_id,
        high_water=high_water,
        source_digest=source_digest,
        source_records=repository.records_for_handoff(sort_run_id),
        main_sort_block=main_sort_block,
    )
    if main_sort_snapshot_digest(
        snapshot, main_sort_block=main_sort_block
    ) != pinned_snapshot_digest:
        raise RuntimeError("main-sort snapshot digest drifted from its task identity")
    return snapshot


def explorer_api_for_runtime_call(
    runtime: Any, call: Any
) -> Any | None:
    """Bind one Franta launch to an immutable Explorer read scope."""

    access_version = call.payload.get("access_policy_version")
    access_mode = call.payload.get("access_mode")
    grant: ExplorerAttemptAccessGrant | None = None
    if access_version == 2:
        if access_mode not in {"check-result", "portfolio", "full-memory"}:
            raise RuntimeError("Explorer attempt access mode is invalid")
        service = runtime.explorer_service
        if service is None:
            raise RuntimeError("Explorer access service is unavailable")
        grant = service.repository.get_attempt_access_grant(
            str(call.payload["explorer_turn_id"]),
            str(call.payload["worker_session_id"]),
            int(call.payload["attempt_number"]),
        )
        if grant is None:
            raise RuntimeError("Explorer attempt access grant is unavailable")
        if (
            grant.grant_id != call.payload.get("grant_id")
            or grant.input_digest != call.payload.get("grant_digest")
            or grant.access_mode != access_mode
            or call.policy.explorer_access_policy_version != 2
            or call.policy.explorer_access_mode != access_mode
            or call.policy.explorer_access_grant_id != grant.grant_id
            or call.policy.explorer_access_grant_digest != grant.input_digest
        ):
            raise RuntimeError("Explorer attempt access grant does not match its call")
        if access_mode in {"check-result", "portfolio"}:
            return service.attempt_access_api(
                explorer_attempt_from_runtime_call(call), audit=runtime.read_audit
            )
    # Main-sort reads its complete frozen turn from the sealed workspace
    # snapshot.  It never receives the live Explorer search/fetch API.
    if call.kind == "main-sort":
        return None
    if not call.policy.explorer_memory_api:
        return None
    repository = runtime.explorer_repository
    if repository is None:
        raise RuntimeError("Explorer data store is unavailable")
    if call.kind == "explorer-worker":
        max_seq = int(call.payload.get("source_high_water_seq", 0))
        turn_ids = frozenset(repository.trusted_turn_ids(max_seq=max_seq))
        label = (
            f"explorer:{call.payload['worker_session_id']}:"
            f"{int(call.payload['attempt_number'])}"
        )
    else:
        raise RuntimeError("Explorer API was requested by an unrelated call")
    canonical_backend = (
        _frozen_franta_backend(grant)
        if grant is not None and access_mode == "full-memory"
        else runtime.store.as_memory_backend()
    )
    return AuditedExplorerAPI(
        canonical_backend,
        repository,
        call.policy,
        ExplorerReadScope(
            allowed_turn_ids=turn_ids,
            allowed_worker_session_ids=None,
            max_seq=max_seq,
            label=label,
        ),
        audit=runtime.read_audit,
        caller_id=call.call_id,
    )


class FrantaExplorerHost:
    """The sole lifecycle adapter between Explorer and a Franta runtime.

    Explorer decides admission, wave construction, concurrency, deadlines,
    failure containment, and root-candidate cancellation.  This object only
    translates those requests into Franta persistence, transport, workspace,
    and read-capability primitives.
    """

    def __init__(self, runtime: Any) -> None:
        self.runtime = runtime

    @property
    def phase(self) -> str | None:
        return self.runtime.scheduler.alternation_phase

    @property
    def max_workers(self) -> int:
        return int(self.runtime.config["explorer"]["max_workers"])

    def tick(self) -> None:
        self.runtime.scheduler.tick_alternation()

    def expire_attempts(self) -> tuple[str, ...]:
        return tuple(self.runtime.scheduler.expire_explorer_attempts())

    def cancel_call(self, call_id: str, *, reason: str) -> None:
        try:
            self.runtime.transport.cancel(call_id, reason=reason)
        except Exception:
            # Cancellation is best-effort; the persisted fence is authoritative.
            pass

    def controller_snapshot(self) -> Mapping[str, Any]:
        return copy.deepcopy(
            self.runtime.scheduler.state.get("explorer_control", {})
        )

    def visible_high_water(self) -> int:
        repository = self.runtime.explorer_repository
        return 0 if repository is None else repository.visible_high_water()

    def explorer_memory_snapshot(self) -> ExplorerPublishedMemorySnapshot:
        """Project Franta memory into Explorer's private frozen input port."""

        backend = self.runtime.store.as_memory_backend()
        documents: list[ExplorerPublishedMemoryDocument] = []
        for record in backend.iter_records(
            frozenset(
                {"fact", "route", "obligation", "memo", "claim", "computation"}
            )
        ):
            metadata = dict(record.metadata)
            kind = record.memory_type
            if kind == "route":
                main_content = str(
                    metadata.get("strategy_description") or record.abstract
                )
                eligible = True
            elif kind == "memo":
                main_content = str(metadata.get("content") or record.abstract)
                eligible = metadata.get("genre") == "high-level"
            elif kind == "obligation":
                main_content = str(
                    metadata.get("statement") or record.content or record.abstract
                )
                eligible = True
            elif kind == "fact":
                main_content = str(metadata.get("statement") or record.abstract)
                eligible = bool(record.active)
            elif kind == "claim":
                main_content = str(metadata.get("content") or record.abstract)
                eligible = not bool(record.withdrawn)
            else:
                main_content = "\n".join(
                    str(value)
                    for value in (
                        metadata.get("description"),
                        metadata.get("exact_input"),
                        metadata.get("output"),
                        metadata.get("interpretation"),
                    )
                    if value is not None and value != ""
                ) or record.abstract
                eligible = metadata.get("exit_status", 0) == 0
            documents.append(
                ExplorerPublishedMemoryDocument(
                    source_id=record.memory_id,
                    memory_kind=kind,
                    abstract=record.abstract,
                    main_content=main_content,
                    eligible=eligible,
                    full_record_json=json.dumps(
                        record.full(),
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                )
            )
        revision_payload = [
            {
                "source_id": item.source_id,
                "source_digest": item.source_digest,
            }
            for item in sorted(documents, key=lambda item: item.source_id)
        ]
        revision = hashlib.sha256(
            json.dumps(
                revision_payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        return ExplorerPublishedMemorySnapshot(
            revision=f"franta-{revision}", documents=tuple(documents)
        )

    def admit_lineage(self) -> bool:
        try:
            self.runtime.scheduler.admit_explorer_lineage()
        except WorkflowError:
            return False
        return True

    def start_attempt(
        self, lineage_id: str, *, source_high_water_seq: int
    ) -> str:
        self.runtime._ingest_human_guidance()
        control = self.controller_snapshot()
        lineage = (control.get("lineages") or {}).get(lineage_id)
        if not isinstance(lineage, Mapping):
            raise RuntimeError("Explorer lineage is unavailable")
        attempt_number = int(lineage.get("attempts_started", 0)) + 1
        prior_calls = [
            self.call_snapshot(str(item.get("call_id") or ""))
            for item in lineage.get("attempts", ())
            if item.get("call_id")
        ]
        # A lineage begun under v1 must finish under v1.  This matters after a
        # restart or an expired in-flight attempt: changing the resumed
        # session's memory contract midway would violate its persisted prompt
        # and workspace.  Fresh lineages always use v2 below.
        if prior_calls and all(
            call.get("input", {}).get("access_policy_version") is None
            for call in prior_calls
        ):
            prepared = self.runtime.scheduler.start_explorer_attempt(
                lineage_id,
                source_high_water_seq=source_high_water_seq,
            )
            return str(prepared["call_id"])
        phase = self.runtime.scheduler.state.get("phase_control", {})
        turn_id = f"XTURN-{int(phase.get('cycle', 0)):08d}"
        service = self.runtime.explorer_service
        if service is None:
            raise RuntimeError("Explorer access service is unavailable")
        grant = service.repository.get_attempt_access_grant(
            turn_id, lineage_id, attempt_number
        )
        if grant is None:
            grant = service.create_attempt_access_grant(
                ExplorerAttempt(
                    turn_id=turn_id,
                    worker_session_id=lineage_id,
                    attempt_no=attempt_number,
                    call_id=f"access-plan:{lineage_id}:{attempt_number}",
                ),
                host_snapshot=self.explorer_memory_snapshot(),
                source_high_water_seq=source_high_water_seq,
                fallback_query=str(
                    self.runtime.scheduler.effective_problem()["problem_text"]
                ),
            )
        prepared = self.runtime.scheduler.start_explorer_attempt(
            lineage_id,
            source_high_water_seq=grant.explorer_high_water_seq,
            guidance_variant=select_guidance_variant(
                grant.access_mode,
                entropy=grant.input_digest,
            ),
            access_grant={
                "access_policy_version": 2,
                "access_mode": grant.access_mode,
                "grant_id": grant.grant_id,
                "grant_digest": grant.input_digest,
            },
        )
        return str(prepared["call_id"])

    def call_snapshot(self, call_id: str) -> Mapping[str, Any]:
        return copy.deepcopy(
            self.runtime.scheduler.state.get("calls", {}).get(call_id, {})
        )

    def call_is_launchable(self, call_id: str) -> bool:
        return self.call_snapshot(call_id).get("status") in {
            CallState.PREPARED.value,
            CallState.RETRY_PENDING.value,
            CallState.COMPLETED.value,
        }

    def prepare_launch(self, call_id: str) -> Any:
        call = self.call_snapshot(call_id)
        if call.get("kind") != "explorer-worker":
            raise RuntimeError(f"unknown Explorer call {call_id}")
        payload = call.get("input", {})
        first_attempt = bool(payload.get("first_attempt_clean_room"))
        if payload.get("access_policy_version") == 2:
            mode = str(payload.get("access_mode") or "")
            policy = replace(
                policy_for("explorer-worker", mode=mode),
                explorer_access_grant_id=str(payload.get("grant_id") or ""),
                explorer_access_grant_digest=str(
                    payload.get("grant_digest") or ""
                ),
            )
        else:
            mode = "clean-room" if first_attempt else "explore"
            policy = policy_for("explorer-worker", mode=mode)
        workspace = self.runtime._make_workspace(
            call_id=call_id,
            policy=policy,
            context=call["input"],
        )
        session_key = str(call.get("continuation", {}).get("session_key") or "")
        return policy, workspace, session_key, int(payload.get("attempt_number", 0)) > 1

    def run_launch(self, call_id: str, launch: Any) -> Mapping[str, Any]:
        # Keep the runtime forwarding method as a compatibility interception
        # point for existing deterministic executors and tests.
        return self.runtime._run_explorer_attempt_call(call_id, *launch)

    def run_launch_default(
        self,
        call_id: str,
        policy: AccessPolicy,
        workspace: Any,
        session_key: str,
        resume: bool,
    ) -> Mapping[str, Any]:
        return self.runtime._run_prepared_call(
            call_id,
            workspace=workspace,
            policy=policy,
            mode=policy.mode,
            session_key=session_key,
            resume=resume,
            pre_accept=lambda value: self.runtime._validate_explorer_attempt_result(
                call_id, value
            ),
        )

    def validate_result(self, call_id: str, value: Mapping[str, Any]) -> None:
        from .runtime import InvalidAgentOutput

        service = self.runtime.explorer_service
        if service is None:
            raise InvalidAgentOutput("Explorer result has no repository")
        call = self.call_snapshot(call_id)
        if call.get("kind") != "explorer-worker":
            raise InvalidAgentOutput("Explorer result has no owning call")
        try:
            service.validate_attempt_result(
                explorer_attempt_from_call(call_id, call), value
            )
        except ExplorerError as exc:
            raise InvalidAgentOutput(str(exc)) from exc

    def fail_attempt(self, call_id: str, *, reason: str) -> None:
        self.runtime.scheduler.fail_explorer_attempt(call_id, reason=reason)

    def commit_attempt(
        self,
        call_id: str,
        *,
        outcome: str,
        root_candidate: Mapping[str, str] | None,
    ) -> tuple[str, ...]:
        return tuple(
            self.runtime.scheduler.commit_explorer_attempt(
                call_id,
                outcome=outcome,
                root_candidate=root_candidate,
            )
        )

    def explorer_is_drained(self) -> bool:
        return bool(self.runtime.scheduler.explorer_is_drained())

    def current_turn_context(self) -> ExplorerTurnContext:
        phase_control = self.runtime.scheduler.state.get("phase_control", {})
        if phase_control.get("phase") != "explorer_drain":
            raise RuntimeError("Explorer handoff requires the drain phase")
        cycle = int(phase_control.get("cycle", 0))
        if cycle < 1:
            raise RuntimeError("Explorer handoff lacks a valid turn cycle")
        return ExplorerTurnContext(
            turn_id=f"XTURN-{cycle:08d}",
            root_candidate=copy.deepcopy(
                (phase_control.get("explorer") or {}).get("root_candidate")
            ),
        )


class FrantaExplorerCollaborator:
    """Franta implementation of Explorer's frozen-turn handoff port."""

    def __init__(self, runtime: Any) -> None:
        self.runtime = runtime

    def _cycle(self) -> int:
        cycle = int(
            self.runtime.scheduler.state.get("phase_control", {}).get("cycle", 0)
        )
        if cycle < 1:
            raise RuntimeError("Explorer handoff lacks a valid Franta cycle")
        return cycle

    def handoff_id_for(self, frozen_turn: FrozenExplorerTurn) -> str:
        return f"XSORT-{self._cycle():08d}-{frozen_turn.source_set_digest[:16]}"

    def accept_explorer_handoff(
        self, handoff: ExplorerHandoff
    ) -> tuple[str, str, str]:
        cycle = self._cycle()
        return accept_explorer_handoff(
            self.runtime,
            handoff,
            main_session_key=f"main:explorer-cycle:{cycle:08d}",
        )


def build_explorer_program(runtime: Any) -> ExplorerProgram:
    """Construct the fixed portable program against the Franta host port."""

    service = getattr(runtime, "explorer_service", None)
    if service is None:
        raise RuntimeError("Explorer service is not initialized")
    return service.program(
        FrantaExplorerHost(runtime),
        FrantaExplorerCollaborator(runtime),
    )


def build_explorer_service(
    repository: ExplorerRepository,
    settings: Mapping[str, Any],
) -> ExplorerService:
    """Bind Franta configuration values to the portable Explorer service."""

    return ExplorerService(
        repository,
        limits=ExplorerLimits(
            max_abstract_bytes=int(settings["max_abstract_bytes"]),
            max_content_bytes=int(settings["max_content_bytes"]),
            max_scratch_per_attempt=int(settings["max_scratch_per_attempt"]),
            max_scratch_per_turn=int(settings["max_scratch_per_turn"]),
        ),
    )


def accept_explorer_handoff(
    runtime: Any,
    handoff: ExplorerHandoff,
    *,
    main_session_key: str,
) -> tuple[str, str, str]:
    """Accept one frozen Explorer offer into Franta's sort-task protocol."""

    if handoff.protocol_version != 1:
        raise RuntimeError("unsupported Explorer handoff protocol")
    repository = runtime.explorer_repository
    if repository is None:
        raise RuntimeError("Explorer handoff has no repository")
    frozen = repository.freeze_turn(handoff.turn_id)
    if (
        frozen.high_water_seq != handoff.source_high_water_seq
        or frozen.source_set_digest != handoff.source_set_digest
        or frozen.record_count != handoff.record_count
    ):
        raise RuntimeError("Explorer handoff no longer matches its frozen turn")
    snapshot = build_main_sort_explorer_snapshot_for_frozen_turn(
        repository,
        sort_run_id=handoff.handoff_id,
        frozen_turn=frozen,
    )
    snapshot_digest = explorer_snapshot_digest(snapshot)
    task_id = runtime.scheduler.prepare_main_sort_task(
        sort_run_id=handoff.handoff_id,
        turn_id=handoff.turn_id,
        source_high_water_seq=handoff.source_high_water_seq,
        source_set_digest=handoff.source_set_digest,
        snapshot_format_version=EXPLORER_SNAPSHOT_FORMAT_VERSION,
        snapshot_digest=snapshot_digest,
        snapshot_relative_path=EXPLORER_SNAPSHOT_RELATIVE_PATH.as_posix(),
    )
    repository.create_sort_run(handoff.handoff_id, frozen, task_id)
    call_id = runtime.scheduler.begin_main_sort_task_attempt(
        task_id,
        sort_run_id=handoff.handoff_id,
        session_key=main_session_key,
    )
    return handoff.handoff_id, task_id, call_id


def explorer_attempt_from_call(
    call_id: str, call: Mapping[str, Any]
) -> ExplorerAttempt:
    """Translate one persisted Franta call into a portable attempt identity."""

    payload = call.get("input") or {}
    return ExplorerAttempt(
        turn_id=str(payload.get("explorer_turn_id") or ""),
        worker_session_id=str(payload.get("worker_session_id") or ""),
        attempt_no=int(payload.get("attempt_number", 0)),
        call_id=call_id,
        lease_epoch=int(call.get("lease_epoch", 1)),
        launch_attempt=int(call.get("attempt", 1)),
    )


def explorer_attempt_from_runtime_call(call: Any) -> ExplorerAttempt:
    """Translate the immutable runtime launch DTO at the receipt boundary."""

    payload = call.payload
    return ExplorerAttempt(
        turn_id=str(payload["explorer_turn_id"]),
        worker_session_id=str(payload["worker_session_id"]),
        attempt_no=int(payload["attempt_number"]),
        call_id=str(call.call_id),
        lease_epoch=int(call.lease_epoch),
        launch_attempt=int(call.launch_attempt),
    )


def prepare_explorer_staged_result(
    runtime: Any,
    call: Any,
    *,
    skill: str,
    artifact: Mapping[str, Any],
    receipt: Mapping[str, Any],
    archived_artifact_relpath: str | None,
) -> None:
    """Pass one Franta-authenticated stage into Explorer's receipt gate."""

    from .runtime import InvalidAgentOutput

    service = runtime.explorer_service
    if service is None:
        return
    try:
        service.prepare_staged_result(
            explorer_attempt_from_runtime_call(call),
            skill=skill,
            artifact=artifact,
            receipt_sha256=str(receipt["receipt_sha256"]),
            execution_succeeded=bool(receipt.get("execution_succeeded")),
            archived_artifact_relpath=archived_artifact_relpath,
        )
    except ExplorerError as exc:
        raise InvalidAgentOutput(str(exc)) from exc


@dataclass(frozen=True)
class ExplorerAgentCallSpec:
    prompt: str
    schema_name: str
    model_config: ModelConfig


def is_explorer_agent_role(role: str) -> bool:
    return owns_agent_role(role)


def explorer_agent_call_spec(
    role: str,
    *,
    root_problem: str,
    mode: str | None,
    guidance_variant: str | None = None,
    human_guidance: str | None = None,
    policy: AccessPolicy,
    input_path: str,
    main_sort_block: MainSortBlock = DEFAULT_MAIN_SORT_BLOCK,
) -> ExplorerAgentCallSpec:
    """Validate the Franta policy and adapt one portable launch contract."""

    normalized = role.strip().lower().replace("_", "-")
    if policy.role != normalized:
        raise ValueError("Explorer agent role does not match its Franta access policy")
    if normalized == "explorer-worker" and policy.mode != mode:
        raise ValueError("Explorer worker mode does not match its Franta access policy")
    if (
        normalized == "explorer-worker"
        and mode in {"check-result", "portfolio", "full-memory"}
        and guidance_variant is None
    ):
        # Calls planned before guidance variants were added cannot have their
        # persisted input rewritten.  Derive the same stable choice from their
        # already-persisted grant digest on every recovery launch.
        digest = policy.explorer_access_grant_digest
        if not digest:
            raise ValueError("staged-access Explorer launch lacks guidance entropy")
        guidance_variant = select_guidance_variant(mode, entropy=digest)
    if normalized == "main-sort":
        if human_guidance is not None:
            raise ValueError("main-sort does not accept human guidance")
        if guidance_variant is not None:
            raise ValueError("main-sort does not accept guidance_variant")
        portable = main_sort_block.build_launch_spec(
            root_problem=root_problem,
            input_path=input_path,
            host_agent_name="Franta",
            host_tools=FRANTA_SORT_TOOLS,
        )
    else:
        portable = build_launch_spec(
            normalized,
            root_problem=root_problem,
            mode=mode,
            guidance_variant=guidance_variant,
            input_path=input_path,
            host_agent_name="Franta",
            host_sort_tools=FRANTA_SORT_TOOLS,
            **({"human_guidance": human_guidance} if human_guidance is not None else {}),
        )
    return ExplorerAgentCallSpec(
        prompt=portable.prompt,
        schema_name=portable.schema_name,
        model_config=ModelConfig(
            model=portable.model_route.model,
            reasoning_effort=portable.model_route.reasoning_effort,
        ),
    )


def write_explorer_schemas(directory: str | Path) -> dict[str, Path]:
    """Install the portable block's schemas into one Franta project."""

    return write_response_schemas(directory)


def explorer_skill_source() -> Path:
    """Return the portable block's self-contained skill asset directory."""

    return Path(__file__).resolve().parents[1] / "explorer_system" / "skills"


__all__ = [
    "AuditedExplorerAPI",
    "accept_explorer_handoff",
    "FrantaExplorerHost",
    "FrantaExplorerCollaborator",
    "FRANTA_SORT_TOOLS",
    "ExplorerAgentCallSpec",
    "EXPLORER_SKILLS",
    "ExplorerSettingsValidationError",
    "ExplorerToolValidationError",
    "MainSortContractError",
    "MainSortSnapshotError",
    "PortableExplorerSettings",
    "build_main_sort_explorer_snapshot",
    "build_main_sort_explorer_snapshot_for_frozen_turn",
    "build_explorer_program",
    "build_explorer_service",
    "explorer_agent_call_spec",
    "explorer_api_for_runtime_call",
    "explorer_attempt_from_call",
    "explorer_attempt_from_runtime_call",
    "explorer_skill_source",
    "explorer_tool_definitions",
    "explorer_worker_skills",
    "is_explorer_agent_role",
    "main_sort_snapshot_contract",
    "main_sort_snapshot_digest",
    "main_sort_computation_has_exact_provenance",
    "main_sort_operation_has_exact_provenance",
    "main_sort_operation_kinds",
    "normalize_main_sort_computation_promotions",
    "normalize_scratch_payload",
    "normalize_summary_payload",
    "normalize_check_result_payload",
    "normalize_portfolio_fetch_payload",
    "normalize_portfolio_search_payload",
    "prepare_explorer_staged_result",
    "render_main_sort_snapshot_files",
    "validate_main_sort_materialized_snapshot",
    "validate_main_sort_progress_operation",
    "validate_main_sort_submission",
    "write_explorer_schemas",
]
