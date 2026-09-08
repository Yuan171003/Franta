"""Portable noncanonical Explorer record and access contracts.

Explorer records deliberately use a namespace and value objects distinct from
the collaborator's published memory. Nothing in this module makes an Explorer
record acceptable where a host-authoritative record ID is required.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
from typing import Any, Mapping


EXPLORER_RECORD_TYPES = frozenset({"scratch", "summary"})
EXPLORER_SCRATCH_KINDS = frozenset(
    {
        "idea",
        "thought",
        "intuition",
        "claim",
        "proof",
        "route",
        "obligation",
        "computation",
        "discovery",
        "example",
        "counterexample",
        "obstacle",
        "progress",
        "other",
    }
)
EXPLORER_SEARCH_TYPES = frozenset(EXPLORER_RECORD_TYPES)
EXPLORER_EXPORT_STATES = frozenset(
    {"received", "committed", "rejected", "abandoned", "needs_attention"}
)
EXPLORER_ACCESS_MODES = frozenset(
    {"check-result", "portfolio", "full-memory"}
)
EXPLORER_PUBLISHED_MEMORY_KINDS = frozenset(
    {"fact", "claim", "computation", "route", "obligation", "memo"}
)
EXPLORER_PORTFOLIO_ITEM_KINDS = frozenset(
    {"route", "memo", "peer-summary", "peer-scratch"}
)

SCRATCH_ID_RE = re.compile(r"^ES-[A-Za-z0-9][A-Za-z0-9_.:-]*$")
SUMMARY_ID_RE = re.compile(r"^ESUM-[A-Za-z0-9][A-Za-z0-9_.:-]*$")
CAS_EVIDENCE_ID_RE = re.compile(r"^XCAS-[A-Za-z0-9][A-Za-z0-9_.:-]*$")
EXPLORER_RECORD_ID_RE = re.compile(
    r"^(?:ES|ESUM)-[A-Za-z0-9][A-Za-z0-9_.:-]*$"
)


class ExplorerError(RuntimeError):
    """Base class for the noncanonical Explorer data layer."""


class ExplorerValidationError(ExplorerError):
    """An Explorer payload or identifier violates its closed contract."""


class ExplorerAccessError(ExplorerError):
    """A record is outside the server-bound Explorer read scope."""


class ExplorerNotFoundError(ExplorerError):
    """A requested trusted Explorer object does not exist."""


class ExplorerIdempotencyConflict(ExplorerError):
    """An operation ID was replayed with different immutable input."""


def _exact_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ExplorerValidationError(
            f"{field} must be a nonempty exact string without surrounding whitespace"
        )
    return value


def _private_digest(value: Any) -> str:
    """Digest one closed private access-policy value deterministically."""

    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ExplorerValidationError(
            "Explorer access-policy value must be JSON serializable"
        ) from exc
    return hashlib.sha256(encoded).hexdigest()


def _path_free_text(value: Any, field: str) -> str:
    exact = _exact_text(value, field)
    if "/" in exact or "\\" in exact:
        raise ExplorerValidationError(f"{field} must be path-free")
    return exact


@dataclass(frozen=True)
class ExplorerPublishedMemoryDocument:
    """Private, host-supplied projection used to freeze attempt access.

    ``source_id`` and ``memory_kind`` are audit-private. Neither is ever
    included in a check-result or portfolio response visible to a worker.
    Host adapters should mark inactive Facts, withdrawn Claims, and failed
    computations ineligible; the portable block defensively filters them.
    ``full_record_json`` is an optional canonical JSON object kept only in the
    private grant.  Hosts that provide full-memory Attempt 3 access use it to
    replay the exact launch snapshot instead of reopening a mutable live
    memory store.
    """

    source_id: str
    memory_kind: str
    abstract: str
    main_content: str
    eligible: bool = True
    full_record_json: str | None = None

    def __post_init__(self) -> None:
        _path_free_text(self.source_id, "source_id")
        if self.memory_kind not in EXPLORER_PUBLISHED_MEMORY_KINDS:
            raise ExplorerValidationError("unsupported published-memory kind")
        _exact_text(self.abstract, "abstract")
        _exact_text(self.main_content, "main_content")
        if not isinstance(self.eligible, bool):
            raise ExplorerValidationError("eligible must be boolean")
        if self.full_record_json is not None:
            if not isinstance(self.full_record_json, str):
                raise ExplorerValidationError("full_record_json must be text")
            try:
                full_record = json.loads(self.full_record_json)
            except json.JSONDecodeError as exc:
                raise ExplorerValidationError(
                    "full_record_json must contain canonical JSON"
                ) from exc
            if not isinstance(full_record, Mapping):
                raise ExplorerValidationError(
                    "full_record_json must contain a JSON object"
                )
            canonical = json.dumps(
                full_record,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            if canonical != self.full_record_json:
                raise ExplorerValidationError(
                    "full_record_json must use canonical JSON encoding"
                )

    @property
    def source_digest(self) -> str:
        return _private_digest(self.private_payload())

    def private_payload(self) -> dict[str, Any]:
        value = {
            "source_id": self.source_id,
            "memory_kind": self.memory_kind,
            "abstract": self.abstract,
            "main_content": self.main_content,
            "eligible": self.eligible,
        }
        # Omitting the absent optional field preserves digests of grants made
        # before frozen full-record payloads were introduced.
        if self.full_record_json is not None:
            value["full_record_json"] = self.full_record_json
        return value

    @classmethod
    def from_private_payload(
        cls, value: Mapping[str, Any]
    ) -> "ExplorerPublishedMemoryDocument":
        return cls(
            source_id=value.get("source_id"),
            memory_kind=value.get("memory_kind"),
            abstract=value.get("abstract"),
            main_content=value.get("main_content"),
            eligible=value.get("eligible"),
            full_record_json=value.get("full_record_json"),
        )


@dataclass(frozen=True)
class ExplorerPublishedMemorySnapshot:
    """Immutable host-memory snapshot supplied through the adapter port."""

    revision: str
    documents: tuple[ExplorerPublishedMemoryDocument, ...]
    source_set_digest: str | None = None

    def __post_init__(self) -> None:
        _path_free_text(self.revision, "revision")
        documents = tuple(self.documents)
        if any(
            not isinstance(item, ExplorerPublishedMemoryDocument)
            for item in documents
        ):
            raise ExplorerValidationError(
                "documents must contain published-memory document projections"
            )
        identities = [(item.memory_kind, item.source_id) for item in documents]
        if len(set(identities)) != len(identities):
            raise ExplorerValidationError(
                "published-memory snapshot contains duplicate source identities"
            )
        documents = tuple(
            sorted(documents, key=lambda item: (item.memory_kind, item.source_id))
        )
        object.__setattr__(self, "documents", documents)
        actual = _private_digest(
            [item.private_payload() for item in documents]
        )
        if self.source_set_digest is None:
            object.__setattr__(self, "source_set_digest", actual)
        elif self.source_set_digest != actual:
            raise ExplorerValidationError(
                "published-memory snapshot digest does not match its documents"
            )


@dataclass(frozen=True)
class ExplorerPortfolioItem:
    """One frozen portfolio item with an intentionally narrow public view."""

    portfolio_item_id: str
    item_kind: str
    source_id: str
    source_digest: str
    abstract: str
    main_content: str
    selection_reason: str
    source_worker_session_id: str | None = None
    source_attempt_no: int | None = None

    def __post_init__(self) -> None:
        _path_free_text(self.portfolio_item_id, "portfolio_item_id")
        if self.item_kind not in EXPLORER_PORTFOLIO_ITEM_KINDS:
            raise ExplorerValidationError("unsupported portfolio item kind")
        _path_free_text(self.source_id, "source_id")
        if not re.fullmatch(r"[0-9a-f]{64}", self.source_digest):
            raise ExplorerValidationError("source_digest must be lowercase SHA-256")
        _exact_text(self.abstract, "abstract")
        _exact_text(self.main_content, "main_content")
        if self.selection_reason not in {"closest", "random", "peer-member"}:
            raise ExplorerValidationError("invalid private portfolio selection reason")
        if self.source_worker_session_id is not None:
            _path_free_text(
                self.source_worker_session_id, "source_worker_session_id"
            )
        if self.source_attempt_no is not None and (
            not isinstance(self.source_attempt_no, int)
            or isinstance(self.source_attempt_no, bool)
            or self.source_attempt_no < 1
        ):
            raise ExplorerValidationError("source_attempt_no must be positive")

    def agent_view(self) -> dict[str, str]:
        """Return the complete worker-visible projection."""

        return {
            "portfolio_item_id": self.portfolio_item_id,
            "abstract": self.abstract,
            "main_content": self.main_content,
        }

    def private_payload(self) -> dict[str, Any]:
        return {
            "portfolio_item_id": self.portfolio_item_id,
            "item_kind": self.item_kind,
            "source_id": self.source_id,
            "source_digest": self.source_digest,
            "abstract": self.abstract,
            "main_content": self.main_content,
            "selection_reason": self.selection_reason,
            "source_worker_session_id": self.source_worker_session_id,
            "source_attempt_no": self.source_attempt_no,
        }

    @classmethod
    def from_private_payload(cls, value: Mapping[str, Any]) -> "ExplorerPortfolioItem":
        return cls(**dict(value))


@dataclass(frozen=True)
class ExplorerPortfolioSnapshot:
    """Deterministically selected, immutable Attempt-2 portfolio."""

    portfolio_id: str
    query_source: str
    query_source_record_ids: tuple[str, ...]
    query_source_record_digests: tuple[str, ...]
    source_summary_id: str | None
    source_summary_digest: str | None
    query_text: str
    selection_seed: str
    host_snapshot_revision: str
    host_snapshot_digest: str
    explorer_high_water_seq: int
    items: tuple[ExplorerPortfolioItem, ...]
    portfolio_digest: str | None = None

    def __post_init__(self) -> None:
        for field in (
            "portfolio_id",
            "selection_seed",
            "host_snapshot_revision",
        ):
            _path_free_text(getattr(self, field), field)
        if self.query_source not in {
            "attempt-1-summary",
            "attempt-1-scratch-fallback",
            "host-fallback",
        }:
            raise ExplorerValidationError("invalid portfolio query source")
        source_ids = tuple(self.query_source_record_ids)
        source_digests = tuple(self.query_source_record_digests)
        if len(source_ids) != len(source_digests):
            raise ExplorerValidationError(
                "portfolio query source IDs and digests must align"
            )
        for source_id in source_ids:
            _path_free_text(source_id, "query_source_record_id")
        for source_digest in source_digests:
            if not re.fullmatch(r"[0-9a-f]{64}", source_digest):
                raise ExplorerValidationError(
                    "query source digest must be lowercase SHA-256"
                )
        object.__setattr__(self, "query_source_record_ids", source_ids)
        object.__setattr__(self, "query_source_record_digests", source_digests)
        if self.query_source == "attempt-1-summary":
            if (
                self.source_summary_id is None
                or self.source_summary_digest is None
                or source_ids != (self.source_summary_id,)
                or source_digests != (self.source_summary_digest,)
            ):
                raise ExplorerValidationError(
                    "normal portfolio queries must pin exactly their Attempt-1 summary"
                )
        elif self.source_summary_id is not None or self.source_summary_digest is not None:
            raise ExplorerValidationError(
                "fallback portfolio queries cannot claim a source summary"
            )
        if self.query_source == "attempt-1-scratch-fallback" and not source_ids:
            raise ExplorerValidationError(
                "scratch fallback must pin at least one trusted scratch"
            )
        if self.query_source == "host-fallback" and source_ids:
            raise ExplorerValidationError(
                "host fallback cannot cite Explorer source records"
            )
        if self.source_summary_id is not None:
            _path_free_text(self.source_summary_id, "source_summary_id")
        for field, value in (
            ("source_summary_digest", self.source_summary_digest),
            ("host_snapshot_digest", self.host_snapshot_digest),
        ):
            if (
                (field == "host_snapshot_digest" and value is None)
                or (value is not None and not re.fullmatch(r"[0-9a-f]{64}", value))
            ):
                raise ExplorerValidationError(f"{field} must be lowercase SHA-256")
        _exact_text(self.query_text, "query_text")
        if (
            not isinstance(self.explorer_high_water_seq, int)
            or isinstance(self.explorer_high_water_seq, bool)
            or self.explorer_high_water_seq < 0
        ):
            raise ExplorerValidationError(
                "explorer_high_water_seq must be nonnegative"
            )
        items = tuple(self.items)
        if len({item.portfolio_item_id for item in items}) != len(items):
            raise ExplorerValidationError("portfolio item IDs must be unique")
        object.__setattr__(self, "items", items)
        actual = _private_digest(self._digest_payload())
        if self.portfolio_digest is None:
            object.__setattr__(self, "portfolio_digest", actual)
        elif self.portfolio_digest != actual:
            raise ExplorerValidationError(
                "portfolio digest does not match its frozen contents"
            )

    def _digest_payload(self) -> dict[str, Any]:
        return {
            "portfolio_id": self.portfolio_id,
            "query_source": self.query_source,
            "query_source_record_ids": list(self.query_source_record_ids),
            "query_source_record_digests": list(self.query_source_record_digests),
            "source_summary_id": self.source_summary_id,
            "source_summary_digest": self.source_summary_digest,
            "query_text": self.query_text,
            "selection_seed": self.selection_seed,
            "host_snapshot_revision": self.host_snapshot_revision,
            "host_snapshot_digest": self.host_snapshot_digest,
            "explorer_high_water_seq": self.explorer_high_water_seq,
            "items": [item.private_payload() for item in self.items],
        }

    def private_payload(self) -> dict[str, Any]:
        return {**self._digest_payload(), "portfolio_digest": self.portfolio_digest}

    @classmethod
    def from_private_payload(
        cls, value: Mapping[str, Any]
    ) -> "ExplorerPortfolioSnapshot":
        payload = dict(value)
        payload["items"] = tuple(
            ExplorerPortfolioItem.from_private_payload(item)
            for item in payload.get("items", ())
        )
        return cls(**payload)


@dataclass(frozen=True)
class ExplorerAttemptAccessGrant:
    """Server-bound, immutable access capability for one logical attempt."""

    grant_id: str
    turn_id: str
    worker_session_id: str
    attempt_no: int
    access_mode: str
    host_snapshot_revision: str
    host_snapshot_digest: str
    explorer_high_water_seq: int
    published_documents: tuple[ExplorerPublishedMemoryDocument, ...]
    portfolio: ExplorerPortfolioSnapshot | None = None
    input_digest: str | None = None

    def __post_init__(self) -> None:
        for field in (
            "grant_id",
            "turn_id",
            "worker_session_id",
            "host_snapshot_revision",
        ):
            _path_free_text(getattr(self, field), field)
        if (
            not isinstance(self.attempt_no, int)
            or isinstance(self.attempt_no, bool)
            or self.attempt_no not in {1, 2, 3}
        ):
            raise ExplorerValidationError("access grant attempt_no must be 1, 2, or 3")
        expected_mode = {1: "check-result", 2: "portfolio", 3: "full-memory"}[
            self.attempt_no
        ]
        if self.access_mode != expected_mode:
            raise ExplorerValidationError(
                "access mode does not match the Explorer attempt number"
            )
        if not re.fullmatch(r"[0-9a-f]{64}", self.host_snapshot_digest):
            raise ExplorerValidationError(
                "host_snapshot_digest must be lowercase SHA-256"
            )
        if (
            not isinstance(self.explorer_high_water_seq, int)
            or isinstance(self.explorer_high_water_seq, bool)
            or self.explorer_high_water_seq < 0
        ):
            raise ExplorerValidationError(
                "explorer_high_water_seq must be nonnegative"
            )
        documents = tuple(self.published_documents)
        if self.attempt_no in {1, 2} and any(
            not item.eligible for item in documents
        ):
            raise ExplorerValidationError(
                "check-result grants may contain only host-eligible documents"
            )
        object.__setattr__(self, "published_documents", documents)
        if (self.attempt_no == 2) != (self.portfolio is not None):
            raise ExplorerValidationError(
                "exactly Attempt 2 must have a frozen portfolio"
            )
        actual = _private_digest(self._digest_payload())
        if self.input_digest is None:
            object.__setattr__(self, "input_digest", actual)
        elif self.input_digest != actual:
            raise ExplorerValidationError(
                "attempt access grant digest does not match its contents"
            )

    def _digest_payload(self) -> dict[str, Any]:
        return {
            "grant_id": self.grant_id,
            "turn_id": self.turn_id,
            "worker_session_id": self.worker_session_id,
            "attempt_no": self.attempt_no,
            "access_mode": self.access_mode,
            "host_snapshot_revision": self.host_snapshot_revision,
            "host_snapshot_digest": self.host_snapshot_digest,
            "explorer_high_water_seq": self.explorer_high_water_seq,
            "published_documents": [
                item.private_payload() for item in self.published_documents
            ],
            "portfolio": (
                None if self.portfolio is None else self.portfolio.private_payload()
            ),
        }

    def private_payload(self) -> dict[str, Any]:
        return {**self._digest_payload(), "input_digest": self.input_digest}

    @classmethod
    def from_private_payload(
        cls, value: Mapping[str, Any]
    ) -> "ExplorerAttemptAccessGrant":
        payload = dict(value)
        payload["published_documents"] = tuple(
            ExplorerPublishedMemoryDocument.from_private_payload(item)
            for item in payload.get("published_documents", ())
        )
        if payload.get("portfolio") is not None:
            payload["portfolio"] = ExplorerPortfolioSnapshot.from_private_payload(
                payload["portfolio"]
            )
        return cls(**payload)


@dataclass(frozen=True)
class ExplorerWriteContext:
    """Scheduler-owned provenance for one Explorer write capability."""

    turn_id: str
    worker_session_id: str
    attempt_no: int
    call_id: str
    lease_epoch: int = 1
    launch_attempt: int = 1

    def __post_init__(self) -> None:
        for field in ("turn_id", "worker_session_id", "call_id"):
            _exact_text(getattr(self, field), field)
        for field in ("attempt_no", "lease_epoch", "launch_attempt"):
            value = getattr(self, field)
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise ExplorerValidationError(f"{field} must be a positive integer")


@dataclass(frozen=True)
class ExplorerReadScope:
    """Mechanical turn/session/high-water filter bound to a broker capability.

    ``allowed_worker_session_ids=None`` grants every worker session in the
    named turns.  An empty set grants none.  ``max_seq`` freezes visibility at
    a repository high-water mark and prevents later writes from leaking into a
    resumed attempt or a collaborator handoff call.
    """

    allowed_turn_ids: frozenset[str]
    allowed_worker_session_ids: frozenset[str] | None = None
    max_seq: int | None = None
    label: str = "explorer"

    def __post_init__(self) -> None:
        turns = frozenset(self.allowed_turn_ids)
        if any(not isinstance(item, str) or not item or item != item.strip() for item in turns):
            raise ExplorerValidationError("allowed_turn_ids contains an invalid turn ID")
        object.__setattr__(self, "allowed_turn_ids", turns)
        if self.allowed_worker_session_ids is not None:
            sessions = frozenset(self.allowed_worker_session_ids)
            if any(
                not isinstance(item, str) or not item or item != item.strip()
                for item in sessions
            ):
                raise ExplorerValidationError(
                    "allowed_worker_session_ids contains an invalid session ID"
                )
            object.__setattr__(self, "allowed_worker_session_ids", sessions)
        if self.max_seq is not None and (
            not isinstance(self.max_seq, int)
            or isinstance(self.max_seq, bool)
            or self.max_seq < 0
        ):
            raise ExplorerValidationError("max_seq must be a nonnegative integer or None")
        _exact_text(self.label, "label")

    def permits(self, *, turn_id: str, worker_session_id: str, seq: int) -> bool:
        if turn_id not in self.allowed_turn_ids:
            return False
        if (
            self.allowed_worker_session_ids is not None
            and worker_session_id not in self.allowed_worker_session_ids
        ):
            return False
        return self.max_seq is None or seq <= self.max_seq


@dataclass(frozen=True)
class ExplorerRecord:
    """Immutable, trusted scratch or summary view."""

    seq: int
    record_id: str
    record_type: str
    operation_id: str
    input_digest: str
    content_digest: str
    turn_id: str
    worker_session_id: str
    attempt_no: int
    record_kind: str | None
    abstract: str
    content: str
    related_memory_ids: tuple[str, ...]
    cas_operation_ids: tuple[str, ...]
    source_scratch_ids: tuple[str, ...]
    source_set_digest: str | None
    directions_tried: tuple[str, ...]
    main_progress: str | None
    main_obstacles: str | None
    created_at: str
    exported_record_ids: tuple[str, ...] = ()

    @property
    def title(self) -> str:
        return self.abstract.splitlines()[0][:160]

    @property
    def payload(self) -> dict[str, Any]:
        """Return the normalized staged payload without repository metadata."""

        common: dict[str, Any] = {
            "operation_id": self.operation_id,
            "record_id": self.record_id,
            "abstract": self.abstract,
            "content": self.content,
        }
        if self.record_type == "scratch":
            return {
                **common,
                "record_kind": self.record_kind,
                "related_memory_ids": list(self.related_memory_ids),
                "cas_operation_ids": list(self.cas_operation_ids),
            }
        return {
            **common,
            "directions_tried": list(self.directions_tried),
            "main_progress": self.main_progress,
            "main_obstacles": self.main_obstacles,
            "source_scratch_ids": list(self.source_scratch_ids),
        }

    def summary(self, *, score: float | None = None) -> dict[str, Any]:
        value: dict[str, Any] = {
            "id": self.record_id,
            "record_space": "explorer",
            "record_type": self.record_type,
            "title": self.title,
            "abstract": self.abstract,
            "status": "provisional",
            "turn_id": self.turn_id,
            "worker_session_id": self.worker_session_id,
            "attempt_no": self.attempt_no,
            "seq": self.seq,
            "exported_record_ids": list(self.exported_record_ids),
        }
        if self.record_kind is not None:
            value["record_kind"] = self.record_kind
        if self.record_type == "summary":
            value["directions_tried"] = list(self.directions_tried)
            value["main_progress"] = self.main_progress
            value["main_obstacles"] = self.main_obstacles
        if score is not None:
            value["relevance"] = score
        return value

    def full(self) -> dict[str, Any]:
        value = {
            **self.summary(),
            "operation_id": self.operation_id,
            "input_digest": self.input_digest,
            "content_digest": self.content_digest,
            "content": self.content,
            "related_memory_ids": list(self.related_memory_ids),
            "cas_operation_ids": list(self.cas_operation_ids),
            "source_scratch_ids": list(self.source_scratch_ids),
            "source_set_digest": self.source_set_digest,
            "directions_tried": list(self.directions_tried),
            "main_progress": self.main_progress,
            "main_obstacles": self.main_obstacles,
            "created_at": self.created_at,
        }
        return value


@dataclass(frozen=True)
class ExplorerSearchDocument:
    """Abstract-first Explorer search candidate."""

    record: ExplorerRecord

    @property
    def record_id(self) -> str:
        return self.record.record_id

    @property
    def title(self) -> str:
        return self.record.title

    @property
    def abstract(self) -> str:
        return self.record.abstract


@dataclass(frozen=True)
class FrozenExplorerTurn:
    turn_id: str
    high_water_seq: int
    record_count: int
    source_set_digest: str
    record_ids: tuple[str, ...]


@dataclass(frozen=True)
class ExplorerHandoff:
    """Immutable offer from a drained Explorer turn to a collaborator."""

    protocol_version: int
    handoff_id: str
    turn_id: str
    source_high_water_seq: int
    source_set_digest: str
    record_count: int
    root_candidate: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class ExplorerHandoffRun:
    run_id: str
    turn_id: str
    source_high_water_seq: int
    source_set_digest: str
    host_context_id: str
    input_digest: str
    created_at: str

@dataclass(frozen=True)
class ExplorerExport:
    export_id: str
    run_id: str
    host_item_id: str
    target_kind: str
    operation_digest: str
    source_record_ids: tuple[str, ...]
    state: str
    host_record_id: str | None
    resolution: str | None
    error: str | None
    created_at: str

@dataclass(frozen=True)
class ExplorerCASEvidence:
    seq: int
    evidence_id: str
    operation_id: str
    input_digest: str
    turn_id: str
    worker_session_id: str
    attempt_no: int
    normalized_payload: Mapping[str, Any]
    execution_succeeded: bool
    output_artifact_sha256: str | None
    archived_artifact_relpath: str | None
    created_at: str


__all__ = [
    "CAS_EVIDENCE_ID_RE",
    "EXPLORER_ACCESS_MODES",
    "EXPLORER_EXPORT_STATES",
    "EXPLORER_PORTFOLIO_ITEM_KINDS",
    "EXPLORER_PUBLISHED_MEMORY_KINDS",
    "EXPLORER_RECORD_ID_RE",
    "EXPLORER_RECORD_TYPES",
    "EXPLORER_SEARCH_TYPES",
    "EXPLORER_SCRATCH_KINDS",
    "ExplorerAccessError",
    "ExplorerAttemptAccessGrant",
    "ExplorerCASEvidence",
    "ExplorerError",
    "ExplorerExport",
    "ExplorerHandoffRun",
    "ExplorerHandoff",
    "ExplorerIdempotencyConflict",
    "ExplorerNotFoundError",
    "ExplorerPortfolioItem",
    "ExplorerPortfolioSnapshot",
    "ExplorerPublishedMemoryDocument",
    "ExplorerPublishedMemorySnapshot",
    "ExplorerReadScope",
    "ExplorerRecord",
    "ExplorerSearchDocument",
    "ExplorerValidationError",
    "ExplorerWriteContext",
    "FrozenExplorerTurn",
    "SCRATCH_ID_RE",
    "SUMMARY_ID_RE",
]
