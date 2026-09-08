"""High-level, host-neutral entry point for the Explorer block.

The service owns Explorer record preparation, attempt-result validation,
read-scope construction, and worker-program construction.  A collaborator
adapter supplies only launch authority, trusted-receipt persistence, and its
read-only published-memory port.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from typing import Any, Callable, Mapping, Sequence

from .access import (
    ExplorerAccessAuditPort,
    ExplorerAttemptAccessAPI,
    build_attempt_access_grant,
)
from .contracts import (
    ExplorerAttemptAccessGrant,
    ExplorerReadScope,
    ExplorerHandoff,
    ExplorerNotFoundError,
    ExplorerPublishedMemorySnapshot,
    ExplorerValidationError,
    ExplorerWriteContext,
)
from .interfaces import ExplorerCollaborator, ExplorerHost
from .program import ExplorerProgram
from .repository import ExplorerRepository


@dataclass(frozen=True)
class ExplorerLimits:
    """Intrinsic storage limits for one Explorer installation."""

    max_abstract_bytes: int = 4_096
    max_content_bytes: int = 262_144
    max_scratch_per_attempt: int = 512
    max_scratch_per_turn: int = 8_192

    def __post_init__(self) -> None:
        for name, value in vars(self).items():
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise ExplorerValidationError(f"{name} must be a positive integer")


@dataclass(frozen=True)
class ExplorerAttempt:
    """Host-authenticated identity of one logical Explorer attempt."""

    turn_id: str
    worker_session_id: str
    attempt_no: int
    call_id: str
    lease_epoch: int = 1
    launch_attempt: int = 1

    def write_context(self) -> ExplorerWriteContext:
        return ExplorerWriteContext(
            turn_id=self.turn_id,
            worker_session_id=self.worker_session_id,
            attempt_no=self.attempt_no,
            call_id=self.call_id,
            lease_epoch=self.lease_epoch,
            launch_attempt=self.launch_attempt,
        )

    def read_scope(
        self,
        *,
        allowed_turn_ids: Sequence[str],
        max_seq: int | None,
        include_peer_sessions: bool = True,
    ) -> ExplorerReadScope:
        return ExplorerReadScope(
            allowed_turn_ids=frozenset(allowed_turn_ids),
            allowed_worker_session_ids=(
                None
                if include_peer_sessions
                else frozenset({self.worker_session_id})
            ),
            max_seq=max_seq,
            label=f"explorer:{self.worker_session_id}:{self.attempt_no}",
        )


class ExplorerService:
    """One portable Explorer installation backed by an append-only store."""

    def __init__(
        self,
        repository: ExplorerRepository,
        *,
        limits: ExplorerLimits | None = None,
    ) -> None:
        self.repository = repository
        self.limits = limits or ExplorerLimits()

    def program(
        self,
        host: ExplorerHost,
        collaborator: ExplorerCollaborator | None = None,
        **options: Any,
    ) -> ExplorerProgram:
        """Bind the fixed worker controller to a collaborator adapter."""

        return ExplorerProgram(
            host,
            service=self,
            collaborator=collaborator,
            **options,
        )

    def prepare_staged_result(
        self,
        attempt: ExplorerAttempt,
        *,
        skill: str,
        artifact: Mapping[str, Any],
        receipt_sha256: str,
        execution_succeeded: bool | None = None,
        archived_artifact_relpath: str | None = None,
    ) -> Any:
        """Prepare one broker-authenticated result without making it visible.

        The host remains responsible for authenticating the broker callback and
        durably appending its private receipt.  Visibility begins only after it
        calls :meth:`trust_receipt`.
        """

        context = attempt.write_context()
        if skill in {"record-scratch", "record-summary"}:
            for field, maximum in (
                ("abstract", self.limits.max_abstract_bytes),
                ("content", self.limits.max_content_bytes),
            ):
                value = artifact.get(field)
                if not isinstance(value, str) or len(value.encode("utf-8")) > maximum:
                    raise ExplorerValidationError(
                        f"Explorer {field} exceeds the configured UTF-8 byte limit"
                    )
        if skill == "record-scratch":
            return self.repository.prepare_scratch(
                context,
                artifact,
                receipt_sha256,
                max_records_per_attempt=self.limits.max_scratch_per_attempt,
                max_records_per_turn=self.limits.max_scratch_per_turn,
            )
        if skill == "record-summary":
            return self.repository.prepare_summary(
                context,
                artifact,
                receipt_sha256,
                max_records_per_attempt=1,
                max_records_per_turn=self.limits.max_scratch_per_turn,
            )
        if skill == "CAS":
            normalized = dict(artifact)
            normalized["execution_succeeded"] = bool(execution_succeeded)
            return self.repository.prepare_cas_evidence(
                context,
                normalized,
                receipt_sha256,
                archived_artifact_relpath=archived_artifact_relpath,
            )
        raise ExplorerValidationError(f"unsupported Explorer staging skill: {skill}")

    def trust_receipt(self, receipt_sha256: str) -> None:
        self.repository.trust_receipt(receipt_sha256)

    def replay_trusted_receipts(self, receipt_sha256s: Sequence[str]) -> None:
        self.repository.replay_trusted_receipts(receipt_sha256s)

    def create_attempt_access_grant(
        self,
        attempt: ExplorerAttempt,
        *,
        host_snapshot: ExplorerPublishedMemorySnapshot,
        source_high_water_seq: int | None = None,
        experiment_seed: str = "default",
        fallback_query: str | None = None,
    ) -> ExplorerAttemptAccessGrant:
        """Freeze and persist the exact memory capability for one attempt."""

        if not isinstance(attempt, ExplorerAttempt):
            raise ExplorerValidationError(
                "attempt access grant requires an ExplorerAttempt identity"
            )
        if not isinstance(host_snapshot, ExplorerPublishedMemorySnapshot):
            raise ExplorerValidationError(
                "host_snapshot must be an Explorer published-memory snapshot"
            )
        current_high_water = self.repository.visible_high_water()
        high_water = (
            current_high_water
            if source_high_water_seq is None
            else source_high_water_seq
        )
        if (
            not isinstance(high_water, int)
            or isinstance(high_water, bool)
            or not 0 <= high_water <= current_high_water
        ):
            raise ExplorerValidationError(
                "source_high_water_seq exceeds trusted Explorer visibility"
            )
        grant = build_attempt_access_grant(
            self.repository,
            turn_id=attempt.turn_id,
            worker_session_id=attempt.worker_session_id,
            attempt_no=attempt.attempt_no,
            host_snapshot=host_snapshot,
            explorer_high_water_seq=high_water,
            experiment_seed=experiment_seed,
            fallback_query=fallback_query,
        )
        return self.repository.put_attempt_access_grant(grant)

    def attempt_access_grant(
        self, attempt: ExplorerAttempt
    ) -> ExplorerAttemptAccessGrant:
        """Load the server-bound grant for an authenticated attempt."""

        grant = self.repository.get_attempt_access_grant(
            attempt.turn_id,
            attempt.worker_session_id,
            attempt.attempt_no,
        )
        if grant is None:
            raise ExplorerNotFoundError(
                "no memory access grant exists for this Explorer attempt"
            )
        return grant

    def attempt_access_api(
        self,
        attempt: ExplorerAttempt,
        *,
        audit: ExplorerAccessAuditPort | None = None,
    ) -> ExplorerAttemptAccessAPI:
        """Bind broker operations to an already persisted immutable grant."""

        return ExplorerAttemptAccessAPI(
            self.attempt_access_grant(attempt),
            audit=audit,
        )

    def create_handoff(
        self,
        turn_id: str,
        *,
        root_candidate: Mapping[str, Any] | None = None,
        id_factory: Callable[[Any], str] | None = None,
    ) -> ExplorerHandoff:
        """Freeze exactly one turn and return its collaborator wire object."""

        frozen = self.repository.freeze_turn(turn_id)
        handoff_id = (
            id_factory(frozen)
            if id_factory is not None
            else "XHANDOFF-"
            + hashlib.sha256(
                f"{frozen.turn_id}:{frozen.source_set_digest}".encode("utf-8")
            ).hexdigest()[:24]
        )
        if (
            not isinstance(handoff_id, str)
            or not handoff_id
            or handoff_id != handoff_id.strip()
        ):
            raise ExplorerValidationError("handoff ID factory returned an invalid ID")
        return ExplorerHandoff(
            protocol_version=1,
            handoff_id=handoff_id,
            turn_id=frozen.turn_id,
            source_high_water_seq=frozen.high_water_seq,
            source_set_digest=frozen.source_set_digest,
            record_count=frozen.record_count,
            root_candidate=(
                None if root_candidate is None else dict(root_candidate)
            ),
        )

    def validate_attempt_result(
        self,
        attempt: ExplorerAttempt,
        value: Mapping[str, Any],
    ) -> None:
        """Ensure final IDs belong to the exact attempt that returned them."""

        if "ongoing_direction_scratch_id" in value:
            raise ExplorerValidationError(
                "Explorer final responses no longer accept direction scratch IDs"
            )

        scope = ExplorerReadScope(
            allowed_turn_ids=frozenset({attempt.turn_id}),
            allowed_worker_session_ids=frozenset({attempt.worker_session_id}),
            max_seq=None,
            label=f"explorer-final:{attempt.call_id}",
        )

        def owned(
            record_id: Any, *, record_type: str, kind: str | None = None
        ) -> Any:
            if not isinstance(record_id, str):
                raise ExplorerValidationError(
                    "Explorer final response contains a non-string ID"
                )
            record = self.repository.fetch(scope, record_id)
            if (
                record is None
                or record.record_type != record_type
                or int(record.attempt_no) != attempt.attempt_no
                or (kind is not None and record.record_kind != kind)
            ):
                raise ExplorerValidationError(
                    f"Explorer final response cites an ineligible "
                    f"{record_type}: {record_id}"
                )
            return record

        summary = owned(value.get("final_summary_id"), record_type="summary")
        candidate_id = value.get("root_candidate_scratch_id")
        if candidate_id is not None:
            candidate = owned(candidate_id, record_type="scratch", kind="proof")
            if candidate.record_id == summary.record_id:
                raise ExplorerValidationError(
                    "Explorer root candidate must be its own proof scratch"
                )


__all__ = [
    "ExplorerAttempt",
    "ExplorerLimits",
    "ExplorerService",
]
