"""Portable append-only SQLite authority for noncanonical Explorer records.

The repository is intentionally independent of collaborator memory. A
prepared record is invisible until a scheduler-private trusted receipt is
accepted, which lets the runtime bridge its filesystem receipt and this SQLite
store without treating an agent-writable outbox as authoritative.
"""

from __future__ import annotations

import copy
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import sqlite3
import stat
import threading
from typing import Any, Iterable, Iterator, Mapping, Sequence
import uuid

from .contracts import (
    CAS_EVIDENCE_ID_RE,
    EXPLORER_EXPORT_STATES,
    EXPLORER_RECORD_ID_RE,
    EXPLORER_RECORD_TYPES,
    EXPLORER_SCRATCH_KINDS,
    ExplorerAttemptAccessGrant,
    ExplorerCASEvidence,
    ExplorerExport,
    ExplorerHandoffRun,
    ExplorerIdempotencyConflict,
    ExplorerNotFoundError,
    ExplorerReadScope,
    ExplorerRecord,
    ExplorerSearchDocument,
    ExplorerValidationError,
    ExplorerWriteContext,
    FrozenExplorerTurn,
)


SCHEMA_VERSION = 1
MAX_ABSTRACT_CHARS = 4_096
MAX_CONTENT_CHARS = 262_144
MAX_SOURCE_SCRATCHES = 64
MAX_RELATED_MEMORY_IDS = 64
MAX_CAS_OPERATION_IDS = 64
MAX_DIRECTIONS_TRIED = 64

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_PATH_FREE_ID_RE = re.compile(r"^[^/\\]+$")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise ExplorerValidationError("Explorer payload must be JSON serializable") from exc


def _loads(value: str) -> Any:
    try:
        return json.loads(value)
    except json.JSONDecodeError as exc:  # database corruption is never guessed around
        raise ExplorerValidationError("stored Explorer JSON is invalid") from exc


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _text(value: Any, field: str, *, maximum: int | None = None) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ExplorerValidationError(
            f"{field} must be a nonempty exact string without surrounding whitespace"
        )
    if maximum is not None and len(value.encode("utf-8")) > maximum:
        raise ExplorerValidationError(f"{field} exceeds {maximum} UTF-8 bytes")
    return value


def _optional_text(value: Any, field: str) -> str | None:
    if value is None:
        return None
    return _text(value, field, maximum=256)


def _closed(payload: Mapping[str, Any], allowed: set[str], label: str) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        raise ExplorerValidationError(f"{label} payload must be an object")
    unknown = set(payload) - allowed
    if unknown:
        raise ExplorerValidationError(
            f"{label} payload has unknown fields: {sorted(unknown)}"
        )
    return copy.deepcopy(dict(payload))


def _string_ids(
    value: Any,
    field: str,
    *,
    maximum: int,
    pattern: re.Pattern[str] | None = None,
) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ExplorerValidationError(f"{field} must be a list")
    if len(value) > maximum:
        raise ExplorerValidationError(f"{field} exceeds its limit of {maximum}")
    result: list[str] = []
    for index, item in enumerate(value):
        exact = _text(item, f"{field}[{index}]", maximum=256)
        if pattern is not None and not pattern.fullmatch(exact):
            raise ExplorerValidationError(f"{field}[{index}] has an invalid ID")
        result.append(exact)
    if len(set(result)) != len(result):
        raise ExplorerValidationError(f"{field} must not contain duplicate IDs")
    return tuple(result)


def _receipt_hash(value: Any) -> str:
    if not isinstance(value, str):
        raise ExplorerValidationError("trusted receipt digest must be a SHA-256 string")
    digest = value.lower()
    if not _SHA256_RE.fullmatch(digest):
        raise ExplorerValidationError("trusted receipt digest must be lowercase SHA-256")
    return digest


def _content_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _scoped_operation_id(
    namespace: str,
    context: ExplorerWriteContext,
    client_operation_id: str,
) -> str:
    """Return a collision-free repository identity for a caller operation ID.

    Explorer workers are deliberately independent and often reuse simple
    operation labels.  The private store therefore scopes idempotency to one
    worker attempt while continuing to expose the caller's original ID in
    records and CAS citations.
    """

    digest = _digest({
        'turn_id': context.turn_id,
        'worker_session_id': context.worker_session_id,
        'attempt_no': context.attempt_no,
        'operation_id': client_operation_id,
    })
    return f"XOP-{namespace}-{digest[:40]}"


def _quota(value: int | None, field: str) -> int | None:
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ExplorerValidationError(f"{field} must be a nonnegative integer or None")
    return value


class ExplorerRepository:
    """Durable, append-only repository for one project's Explorer data."""

    def __init__(
        self,
        path: str | os.PathLike[str],
        *,
        published_record_id_pattern: re.Pattern[str] = _PATH_FREE_ID_RE,
        host_context_id_pattern: re.Pattern[str] = _PATH_FREE_ID_RE,
        host_record_id_pattern: re.Pattern[str] = _PATH_FREE_ID_RE,
        export_kinds: Iterable[str] = (),
        handoff_digest_fields: tuple[str, str] = ("run_id", "host_context_id"),
    ) -> None:
        """Open one Explorer-private store.

        Identifier syntax and export vocabulary are collaborator interface
        parameters.  They are never inferred from a particular host package.
        """

        self._published_record_id_pattern = published_record_id_pattern
        self._host_context_id_pattern = host_context_id_pattern
        self._host_record_id_pattern = host_record_id_pattern
        self._export_kinds = frozenset(export_kinds)
        if any(
            not isinstance(item, str)
            or not item
            or item != item.strip()
            or not _PATH_FREE_ID_RE.fullmatch(item)
            for item in self._export_kinds
        ):
            raise ExplorerValidationError(
                "export_kinds must contain exact path-free collaborator kind names"
            )
        if (
            not isinstance(handoff_digest_fields, tuple)
            or len(handoff_digest_fields) != 2
            or any(
                not isinstance(item, str)
                or not item
                or item != item.strip()
                or not _PATH_FREE_ID_RE.fullmatch(item)
                for item in handoff_digest_fields
            )
        ):
            raise ExplorerValidationError(
                "handoff_digest_fields must name the run and host-context fields"
            )
        self._handoff_digest_fields = handoff_digest_fields
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.is_symlink():
            raise ExplorerValidationError("Explorer database may not be a symlink")
        if self.path.exists():
            if not stat.S_ISREG(self.path.stat().st_mode):
                raise ExplorerValidationError(
                    "Explorer database must be a regular file"
                )
        else:
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            try:
                descriptor = os.open(self.path, flags, 0o600)
            except FileExistsError as exc:
                raise ExplorerValidationError(
                    "Explorer database path changed during secure creation"
                ) from exc
            else:
                os.close(descriptor)
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(
            self.path,
            isolation_level=None,
            check_same_thread=False,
        )
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys=ON")
        self._connection.execute("PRAGMA synchronous=FULL")
        self.journal_mode = str(
            self._connection.execute("PRAGMA journal_mode=WAL").fetchone()[0]
        ).lower()
        self._initialize_schema()

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def __enter__(self) -> "ExplorerRepository":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    @contextmanager
    def transaction(self) -> Iterator[None]:
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                yield
            except Exception:
                self._connection.execute("ROLLBACK")
                raise
            else:
                self._connection.execute("COMMIT")

    def _initialize_schema(self) -> None:
        with self._lock:
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS explorer_schema_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS explorer_records (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    record_id TEXT NOT NULL UNIQUE,
                    record_type TEXT NOT NULL CHECK(record_type IN ('scratch','summary')),
                    operation_id TEXT NOT NULL UNIQUE,
                    client_operation_id TEXT,
                    input_digest TEXT NOT NULL,
                    content_digest TEXT NOT NULL,
                    turn_id TEXT NOT NULL,
                    worker_session_id TEXT NOT NULL,
                    attempt_no INTEGER NOT NULL CHECK(attempt_no >= 1),
                    record_kind TEXT CHECK(record_kind IS NULL OR record_kind IN (
                        'idea','thought','intuition','claim','proof','route','obligation',
                        'computation','discovery','example','counterexample','obstacle',
                        'progress','other'
                    )),
                    previous_direction_id TEXT,
                    ongoing_direction INTEGER NOT NULL CHECK(ongoing_direction IN (0,1)),
                    abstract TEXT NOT NULL,
                    content TEXT NOT NULL,
                    related_memory_ids_json TEXT NOT NULL DEFAULT '[]',
                    cas_operation_ids_json TEXT NOT NULL DEFAULT '[]',
                    directions_tried_json TEXT,
                    main_progress TEXT,
                    main_obstacles TEXT,
                    source_set_digest TEXT,
                    created_at TEXT NOT NULL,
                    CHECK(
                        (
                          record_type='scratch' AND record_kind IS NOT NULL
                          AND directions_tried_json IS NULL
                          AND main_progress IS NULL AND main_obstacles IS NULL
                          AND source_set_digest IS NULL
                        )
                        OR
                        (
                          record_type='summary' AND record_kind IS NULL
                          AND previous_direction_id IS NULL AND ongoing_direction=0
                          AND directions_tried_json IS NOT NULL
                          AND main_progress IS NOT NULL AND main_obstacles IS NOT NULL
                          AND source_set_digest IS NOT NULL
                        )
                    ),
                    CHECK(
                        previous_direction_id IS NULL AND ongoing_direction=0
                    )
                );
                CREATE INDEX IF NOT EXISTS explorer_records_turn_seq
                    ON explorer_records(turn_id,seq);
                CREATE INDEX IF NOT EXISTS explorer_records_session_seq
                    ON explorer_records(worker_session_id,seq);
                CREATE INDEX IF NOT EXISTS explorer_records_type_seq
                    ON explorer_records(record_type,seq);

                CREATE TABLE IF NOT EXISTS explorer_summary_sources (
                    summary_id TEXT NOT NULL,
                    ordinal INTEGER NOT NULL CHECK(ordinal >= 0),
                    scratch_id TEXT NOT NULL,
                    scratch_digest TEXT NOT NULL,
                    PRIMARY KEY(summary_id,scratch_id),
                    UNIQUE(summary_id,ordinal),
                    FOREIGN KEY(summary_id) REFERENCES explorer_records(record_id),
                    FOREIGN KEY(scratch_id) REFERENCES explorer_records(record_id)
                );

                CREATE TABLE IF NOT EXISTS explorer_record_receipts (
                    receipt_sha256 TEXT PRIMARY KEY,
                    record_id TEXT NOT NULL,
                    call_id TEXT NOT NULL,
                    lease_epoch INTEGER NOT NULL CHECK(lease_epoch >= 1),
                    launch_attempt INTEGER NOT NULL CHECK(launch_attempt >= 1),
                    FOREIGN KEY(record_id) REFERENCES explorer_records(record_id)
                );

                CREATE TABLE IF NOT EXISTS explorer_trusted_receipts (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    receipt_sha256 TEXT NOT NULL UNIQUE,
                    trusted_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS explorer_record_visibility (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    record_id TEXT NOT NULL UNIQUE,
                    receipt_sha256 TEXT NOT NULL,
                    visible_at TEXT NOT NULL,
                    FOREIGN KEY(record_id) REFERENCES explorer_records(record_id),
                    FOREIGN KEY(receipt_sha256)
                        REFERENCES explorer_trusted_receipts(receipt_sha256)
                );

                CREATE TABLE IF NOT EXISTS explorer_cas_evidence (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    evidence_id TEXT NOT NULL UNIQUE,
                    operation_id TEXT NOT NULL UNIQUE,
                    client_operation_id TEXT,
                    input_digest TEXT NOT NULL,
                    turn_id TEXT NOT NULL,
                    worker_session_id TEXT NOT NULL,
                    attempt_no INTEGER NOT NULL CHECK(attempt_no >= 1),
                    normalized_payload_json TEXT NOT NULL,
                    execution_succeeded INTEGER NOT NULL CHECK(execution_succeeded IN (0,1)),
                    output_artifact_sha256 TEXT,
                    archived_artifact_relpath TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS explorer_cas_turn_session
                    ON explorer_cas_evidence(turn_id,worker_session_id,attempt_no);

                CREATE TABLE IF NOT EXISTS explorer_cas_receipts (
                    receipt_sha256 TEXT PRIMARY KEY,
                    evidence_id TEXT NOT NULL,
                    call_id TEXT NOT NULL,
                    lease_epoch INTEGER NOT NULL CHECK(lease_epoch >= 1),
                    launch_attempt INTEGER NOT NULL CHECK(launch_attempt >= 1),
                    FOREIGN KEY(evidence_id) REFERENCES explorer_cas_evidence(evidence_id)
                );

                CREATE TABLE IF NOT EXISTS explorer_record_cas_evidence (
                    record_id TEXT NOT NULL,
                    ordinal INTEGER NOT NULL CHECK(ordinal >= 0),
                    operation_id TEXT NOT NULL,
                    evidence_id TEXT NOT NULL,
                    evidence_digest TEXT NOT NULL,
                    PRIMARY KEY(record_id,evidence_id),
                    UNIQUE(record_id,ordinal),
                    FOREIGN KEY(record_id) REFERENCES explorer_records(record_id),
                    FOREIGN KEY(evidence_id)
                        REFERENCES explorer_cas_evidence(evidence_id)
                );

                /* These physical v1 table/column names are intentionally
                   retained so databases created by the original adapter
                   remain readable. Public objects and methods are neutral. */
                CREATE TABLE IF NOT EXISTS explorer_sort_runs (
                    sort_run_id TEXT PRIMARY KEY,
                    turn_id TEXT NOT NULL,
                    source_high_water_seq INTEGER NOT NULL CHECK(source_high_water_seq >= 0),
                    source_set_digest TEXT NOT NULL,
                    sort_task_id TEXT NOT NULL UNIQUE,
                    input_digest TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS explorer_promotions (
                    promotion_id TEXT PRIMARY KEY,
                    sort_run_id TEXT NOT NULL,
                    franta_operation_id TEXT NOT NULL UNIQUE,
                    target_kind TEXT NOT NULL,
                    operation_digest TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(sort_run_id) REFERENCES explorer_sort_runs(sort_run_id)
                );

                CREATE TABLE IF NOT EXISTS explorer_promotion_sources (
                    promotion_id TEXT NOT NULL,
                    record_id TEXT NOT NULL,
                    record_digest TEXT NOT NULL,
                    PRIMARY KEY(promotion_id,record_id),
                    FOREIGN KEY(promotion_id) REFERENCES explorer_promotions(promotion_id),
                    FOREIGN KEY(record_id) REFERENCES explorer_records(record_id)
                );

                CREATE TABLE IF NOT EXISTS explorer_promotion_events (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    promotion_id TEXT NOT NULL,
                    state TEXT NOT NULL CHECK(state IN (
                        'received','committed','rejected','abandoned','needs_attention'
                    )),
                    canonical_id TEXT,
                    resolution TEXT,
                    error TEXT,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(promotion_id) REFERENCES explorer_promotions(promotion_id)
                );
                CREATE INDEX IF NOT EXISTS explorer_promotion_events_by_promotion
                    ON explorer_promotion_events(promotion_id,seq);

                CREATE TABLE IF NOT EXISTS explorer_attempt_access_grants (
                    grant_id TEXT PRIMARY KEY,
                    turn_id TEXT NOT NULL,
                    worker_session_id TEXT NOT NULL,
                    attempt_no INTEGER NOT NULL CHECK(attempt_no IN (1,2,3)),
                    access_mode TEXT NOT NULL CHECK(access_mode IN (
                        'check-result','portfolio','full-memory'
                    )),
                    host_snapshot_revision TEXT NOT NULL,
                    host_snapshot_digest TEXT NOT NULL,
                    explorer_high_water_seq INTEGER NOT NULL
                        CHECK(explorer_high_water_seq >= 0),
                    portfolio_digest TEXT,
                    input_digest TEXT NOT NULL,
                    private_payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(turn_id,worker_session_id,attempt_no),
                    CHECK(
                        (attempt_no=2 AND access_mode='portfolio'
                         AND portfolio_digest IS NOT NULL)
                        OR
                        (attempt_no<>2 AND portfolio_digest IS NULL)
                    )
                );
                CREATE INDEX IF NOT EXISTS explorer_access_grants_turn
                    ON explorer_attempt_access_grants(turn_id,worker_session_id);

                CREATE TRIGGER IF NOT EXISTS preserve_explorer_records_update
                    BEFORE UPDATE ON explorer_records
                    BEGIN SELECT RAISE(ABORT, 'Explorer records are append-only'); END;
                CREATE TRIGGER IF NOT EXISTS preserve_explorer_records_delete
                    BEFORE DELETE ON explorer_records
                    BEGIN SELECT RAISE(ABORT, 'Explorer records are append-only'); END;
                CREATE TRIGGER IF NOT EXISTS preserve_explorer_sources_update
                    BEFORE UPDATE ON explorer_summary_sources
                    BEGIN SELECT RAISE(ABORT, 'Explorer summary sources are append-only'); END;
                CREATE TRIGGER IF NOT EXISTS preserve_explorer_sources_delete
                    BEFORE DELETE ON explorer_summary_sources
                    BEGIN SELECT RAISE(ABORT, 'Explorer summary sources are append-only'); END;
                CREATE TRIGGER IF NOT EXISTS preserve_explorer_receipts_update
                    BEFORE UPDATE ON explorer_record_receipts
                    BEGIN SELECT RAISE(ABORT, 'Explorer receipt links are append-only'); END;
                CREATE TRIGGER IF NOT EXISTS preserve_explorer_receipts_delete
                    BEFORE DELETE ON explorer_record_receipts
                    BEGIN SELECT RAISE(ABORT, 'Explorer receipt links are append-only'); END;
                CREATE TRIGGER IF NOT EXISTS preserve_explorer_trust_update
                    BEFORE UPDATE ON explorer_trusted_receipts
                    BEGIN SELECT RAISE(ABORT, 'Trusted receipts are append-only'); END;
                CREATE TRIGGER IF NOT EXISTS preserve_explorer_trust_delete
                    BEFORE DELETE ON explorer_trusted_receipts
                    BEGIN SELECT RAISE(ABORT, 'Trusted receipts are append-only'); END;
                CREATE TRIGGER IF NOT EXISTS preserve_explorer_visibility_update
                    BEFORE UPDATE ON explorer_record_visibility
                    BEGIN SELECT RAISE(ABORT, 'Explorer visibility is append-only'); END;
                CREATE TRIGGER IF NOT EXISTS preserve_explorer_visibility_delete
                    BEFORE DELETE ON explorer_record_visibility
                    BEGIN SELECT RAISE(ABORT, 'Explorer visibility is append-only'); END;
                CREATE TRIGGER IF NOT EXISTS preserve_explorer_cas_update
                    BEFORE UPDATE ON explorer_cas_evidence
                    BEGIN SELECT RAISE(ABORT, 'Explorer CAS evidence is append-only'); END;
                CREATE TRIGGER IF NOT EXISTS preserve_explorer_cas_delete
                    BEFORE DELETE ON explorer_cas_evidence
                    BEGIN SELECT RAISE(ABORT, 'Explorer CAS evidence is append-only'); END;
                CREATE TRIGGER IF NOT EXISTS preserve_explorer_cas_receipts_update
                    BEFORE UPDATE ON explorer_cas_receipts
                    BEGIN SELECT RAISE(ABORT, 'Explorer CAS receipts are append-only'); END;
                CREATE TRIGGER IF NOT EXISTS preserve_explorer_cas_receipts_delete
                    BEFORE DELETE ON explorer_cas_receipts
                    BEGIN SELECT RAISE(ABORT, 'Explorer CAS receipts are append-only'); END;
                CREATE TRIGGER IF NOT EXISTS preserve_explorer_record_cas_update
                    BEFORE UPDATE ON explorer_record_cas_evidence
                    BEGIN SELECT RAISE(ABORT, 'Explorer CAS citations are append-only'); END;
                CREATE TRIGGER IF NOT EXISTS preserve_explorer_record_cas_delete
                    BEFORE DELETE ON explorer_record_cas_evidence
                    BEGIN SELECT RAISE(ABORT, 'Explorer CAS citations are append-only'); END;
                CREATE TRIGGER IF NOT EXISTS preserve_explorer_sort_runs_update
                    BEFORE UPDATE ON explorer_sort_runs
                    BEGIN SELECT RAISE(ABORT, 'Explorer sort runs are append-only'); END;
                CREATE TRIGGER IF NOT EXISTS preserve_explorer_sort_runs_delete
                    BEFORE DELETE ON explorer_sort_runs
                    BEGIN SELECT RAISE(ABORT, 'Explorer sort runs are append-only'); END;
                CREATE TRIGGER IF NOT EXISTS preserve_explorer_promotions_update
                    BEFORE UPDATE ON explorer_promotions
                    BEGIN SELECT RAISE(ABORT, 'Explorer promotions are append-only'); END;
                CREATE TRIGGER IF NOT EXISTS preserve_explorer_promotions_delete
                    BEFORE DELETE ON explorer_promotions
                    BEGIN SELECT RAISE(ABORT, 'Explorer promotions are append-only'); END;
                CREATE TRIGGER IF NOT EXISTS preserve_explorer_promotion_sources_update
                    BEFORE UPDATE ON explorer_promotion_sources
                    BEGIN SELECT RAISE(ABORT, 'Explorer promotion sources are append-only'); END;
                CREATE TRIGGER IF NOT EXISTS preserve_explorer_promotion_sources_delete
                    BEFORE DELETE ON explorer_promotion_sources
                    BEGIN SELECT RAISE(ABORT, 'Explorer promotion sources are append-only'); END;
                CREATE TRIGGER IF NOT EXISTS preserve_explorer_promotion_events_update
                    BEFORE UPDATE ON explorer_promotion_events
                    BEGIN SELECT RAISE(ABORT, 'Explorer promotion events are append-only'); END;
                CREATE TRIGGER IF NOT EXISTS preserve_explorer_promotion_events_delete
                    BEFORE DELETE ON explorer_promotion_events
                    BEGIN SELECT RAISE(ABORT, 'Explorer promotion events are append-only'); END;
                CREATE TRIGGER IF NOT EXISTS preserve_explorer_access_grants_update
                    BEFORE UPDATE ON explorer_attempt_access_grants
                    BEGIN SELECT RAISE(ABORT, 'Explorer access grants are append-only'); END;
                CREATE TRIGGER IF NOT EXISTS preserve_explorer_access_grants_delete
                    BEFORE DELETE ON explorer_attempt_access_grants
                    BEGIN SELECT RAISE(ABORT, 'Explorer access grants are append-only'); END;
                """
            )
            # Version-one Explorer databases used the globally unique
            # operation_id directly.  Add a nullable display/citation column
            # in place so new writes can use a server-scoped internal ID while
            # old rows remain readable and replayable.
            for table in ("explorer_records", "explorer_cas_evidence"):
                columns = {
                    str(row["name"])
                    for row in self._connection.execute(
                        f"PRAGMA table_info({table})"
                    ).fetchall()
                }
                if "client_operation_id" not in columns:
                    self._connection.execute(
                        f"ALTER TABLE {table} ADD COLUMN client_operation_id TEXT"
                    )
            self._connection.execute(
                """CREATE INDEX IF NOT EXISTS explorer_records_client_operation
                   ON explorer_records(
                       turn_id,worker_session_id,attempt_no,client_operation_id
                   )"""
            )
            self._connection.execute(
                """CREATE INDEX IF NOT EXISTS explorer_cas_client_operation
                   ON explorer_cas_evidence(
                       turn_id,worker_session_id,attempt_no,client_operation_id
                   )"""
            )
            row = self._connection.execute(
                "SELECT value FROM explorer_schema_meta WHERE key='schema_version'"
            ).fetchone()
            if row is None:
                self._connection.execute(
                    "INSERT INTO explorer_schema_meta(key,value) VALUES('schema_version',?)",
                    (str(SCHEMA_VERSION),),
                )
            elif int(row["value"]) != SCHEMA_VERSION:
                raise ExplorerValidationError(
                    f"unsupported Explorer schema version {row['value']}"
                )
            self._backfill_record_cas_evidence_locked()

    def _normalize_record_payload(
        self,
        record_type: str,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        common = {"operation_id", "record_id", "skill", "abstract", "content"}
        if record_type == "scratch":
            allowed = common | {
                "record_kind",
                "related_memory_ids",
                "cas_operation_ids",
            }
        else:
            allowed = common | {
                "directions_tried",
                "main_progress",
                "main_obstacles",
                "source_scratch_ids",
            }
        value = _closed(payload, allowed, f"Explorer {record_type}")
        operation_id = _text(value.get("operation_id"), "operation_id", maximum=256)
        record_id = _text(value.get("record_id"), "record_id", maximum=256)
        expected_id = (
            re.compile(r"^ES-[A-Za-z0-9][A-Za-z0-9_.:-]*$")
            if record_type == "scratch"
            else re.compile(r"^ESUM-[A-Za-z0-9][A-Za-z0-9_.:-]*$")
        )
        if not expected_id.fullmatch(record_id):
            raise ExplorerValidationError(
                f"runtime-owned record_id has the wrong {record_type} namespace"
            )
        skill = value.get("skill")
        expected_skill = "record-scratch" if record_type == "scratch" else "record-summary"
        if skill is not None and skill != expected_skill:
            raise ExplorerValidationError(
                f"staged {record_type} artifact has the wrong skill label"
            )
        abstract = _text(
            value.get("abstract"), "abstract", maximum=MAX_ABSTRACT_CHARS
        )
        content = _text(value.get("content"), "content", maximum=MAX_CONTENT_CHARS)
        record_kind: str | None = None
        previous_direction_id: str | None = None
        ongoing_direction = False
        related: tuple[str, ...] = ()
        cas_operations: tuple[str, ...] = ()
        sources: tuple[str, ...] = ()
        directions: tuple[str, ...] = ()
        main_progress: str | None = None
        main_obstacles: str | None = None
        if record_type == "scratch":
            record_kind = value.get("record_kind")
            if record_kind not in EXPLORER_SCRATCH_KINDS:
                raise ExplorerValidationError(
                    "record_kind is not an authorized Explorer scratch kind"
                )
            related = _string_ids(
                value.get("related_memory_ids", []),
                "related_memory_ids",
                maximum=MAX_RELATED_MEMORY_IDS,
                pattern=self._published_record_id_pattern,
            )
            cas_operations = _string_ids(
                value.get("cas_operation_ids", []),
                "cas_operation_ids",
                maximum=MAX_CAS_OPERATION_IDS,
                pattern=_PATH_FREE_ID_RE,
            )
        else:
            raw_directions = value.get("directions_tried")
            if (
                not isinstance(raw_directions, list)
                or not raw_directions
                or len(raw_directions) > MAX_DIRECTIONS_TRIED
            ):
                raise ExplorerValidationError(
                    "directions_tried must contain 1-64 texts"
                )
            normalized_directions: list[str] = []
            for index, item in enumerate(raw_directions):
                normalized_directions.append(
                    _text(
                        item,
                        f"directions_tried[{index}]",
                        maximum=32_768,
                    )
                )
            directions = tuple(normalized_directions)
            main_progress = _text(
                value.get("main_progress"), "main_progress", maximum=32_768
            )
            main_obstacles = _text(
                value.get("main_obstacles"), "main_obstacles", maximum=32_768
            )
            sources = _string_ids(
                value.get("source_scratch_ids", []),
                "source_scratch_ids",
                maximum=MAX_SOURCE_SCRATCHES,
                pattern=re.compile(r"^ES-[A-Za-z0-9][A-Za-z0-9_.:-]*$"),
            )
            if not sources:
                raise ExplorerValidationError(
                    "source_scratch_ids must cite at least one trusted scratch"
                )
        return {
            "operation_id": operation_id,
            "record_id": record_id,
            "abstract": abstract,
            "content": content,
            "record_kind": record_kind,
            "previous_direction_id": previous_direction_id,
            "ongoing_direction": ongoing_direction,
            "related_memory_ids": related,
            "cas_operation_ids": cas_operations,
            "source_scratch_ids": sources,
            "directions_tried": directions,
            "main_progress": main_progress,
            "main_obstacles": main_obstacles,
        }

    def _record_is_trusted_locked(self, record_id: str) -> bool:
        row = self._connection.execute(
            """SELECT 1 FROM explorer_record_visibility
               WHERE record_id=? LIMIT 1""",
            (record_id,),
        ).fetchone()
        return row is not None

    def _evidence_is_trusted_locked(self, evidence_id: str) -> bool:
        row = self._connection.execute(
            """SELECT 1
               FROM explorer_cas_receipts cr
               JOIN explorer_trusted_receipts tr
                 ON tr.receipt_sha256=cr.receipt_sha256
               WHERE cr.evidence_id=? LIMIT 1""",
            (evidence_id,),
        ).fetchone()
        return row is not None

    def _backfill_record_cas_evidence_locked(self) -> None:
        """Pin citations written by databases created before citation links.

        Explorer records were already required to cite trusted evidence that
        existed when the record was prepared.  ``created_at`` therefore gives
        the migration a strict upper bound which prevents a same-label CAS
        operation created later from being selected during backfill.
        """

        records = self._connection.execute(
            """SELECT record_id,turn_id,worker_session_id,attempt_no,
                      cas_operation_ids_json,created_at
               FROM explorer_records
               WHERE cas_operation_ids_json<>'[]'
               ORDER BY seq"""
        ).fetchall()
        for record in records:
            operation_ids = tuple(_loads(record["cas_operation_ids_json"]))
            existing_rows = self._connection.execute(
                """SELECT ordinal,operation_id,evidence_id,evidence_digest
                   FROM explorer_record_cas_evidence
                   WHERE record_id=? ORDER BY ordinal""",
                (record["record_id"],),
            ).fetchall()
            existing = {int(row["ordinal"]): row for row in existing_rows}
            for ordinal, operation_id in enumerate(operation_ids):
                linked = existing.get(ordinal)
                if linked is not None:
                    if str(linked["operation_id"]) != operation_id:
                        raise ExplorerValidationError(
                            "stored Explorer CAS citation does not match its record"
                        )
                    continue
                evidence = self._connection.execute(
                    """SELECT ce.* FROM explorer_cas_evidence ce
                       JOIN explorer_cas_receipts cr
                         ON cr.evidence_id=ce.evidence_id
                       JOIN explorer_trusted_receipts tr
                         ON tr.receipt_sha256=cr.receipt_sha256
                       WHERE ce.turn_id=? AND ce.worker_session_id=?
                         AND ce.attempt_no<=? AND ce.created_at<=?
                         AND (
                           ce.client_operation_id=?
                           OR (ce.client_operation_id IS NULL AND ce.operation_id=?)
                         )
                       ORDER BY ce.attempt_no DESC,ce.created_at DESC,ce.seq DESC
                       LIMIT 1""",
                    (
                        record["turn_id"],
                        record["worker_session_id"],
                        int(record["attempt_no"]),
                        record["created_at"],
                        operation_id,
                        operation_id,
                    ),
                ).fetchone()
                if evidence is None:
                    raise ExplorerValidationError(
                        "stored Explorer record cites unavailable CAS evidence"
                    )
                self._connection.execute(
                    """INSERT INTO explorer_record_cas_evidence(
                           record_id,ordinal,operation_id,evidence_id,evidence_digest
                       ) VALUES(?,?,?,?,?)""",
                    (
                        record["record_id"],
                        ordinal,
                        operation_id,
                        evidence["evidence_id"],
                        evidence["input_digest"],
                    ),
                )

    def _resolve_cas_operations_locked(
        self,
        operation_ids: Sequence[str],
        context: ExplorerWriteContext,
    ) -> list[sqlite3.Row]:
        rows: list[sqlite3.Row] = []
        for operation_id in operation_ids:
            row = self._connection.execute(
                """SELECT * FROM explorer_cas_evidence
                   WHERE turn_id=? AND worker_session_id=? AND attempt_no<=?
                     AND (
                       client_operation_id=?
                       OR (client_operation_id IS NULL AND operation_id=?)
                     )
                   ORDER BY attempt_no DESC,seq DESC LIMIT 1""",
                (
                    context.turn_id,
                    context.worker_session_id,
                    context.attempt_no,
                    operation_id,
                    operation_id,
                ),
            ).fetchone()
            if row is None or not self._evidence_is_trusted_locked(
                str(row["evidence_id"])
            ):
                raise ExplorerNotFoundError(
                    f"trusted Explorer CAS operation not found: {operation_id}"
                )
            rows.append(row)
        return rows

    def _source_rows_locked(
        self,
        source_ids: Sequence[str],
        context: ExplorerWriteContext,
    ) -> list[sqlite3.Row]:
        rows: list[sqlite3.Row] = []
        for source_id in source_ids:
            row = self._connection.execute(
                "SELECT * FROM explorer_records WHERE record_id=?",
                (source_id,),
            ).fetchone()
            if (
                row is None
                or row["record_type"] != "scratch"
                or not self._record_is_trusted_locked(source_id)
            ):
                raise ExplorerNotFoundError(
                    f"trusted source scratch not found: {source_id}"
                )
            if (
                row["turn_id"] != context.turn_id
                or row["worker_session_id"] != context.worker_session_id
                or int(row["attempt_no"]) > context.attempt_no
            ):
                raise ExplorerValidationError(
                    f"source scratch {source_id} is outside this Explorer lineage"
                )
            rows.append(row)
        return rows

    def _link_record_receipt_locked(
        self,
        record_id: str,
        receipt_sha256: str,
        context: ExplorerWriteContext,
    ) -> None:
        cas_owner = self._connection.execute(
            "SELECT evidence_id FROM explorer_cas_receipts WHERE receipt_sha256=?",
            (receipt_sha256,),
        ).fetchone()
        if cas_owner is not None:
            raise ExplorerIdempotencyConflict(
                "trusted receipt was already allocated to Explorer CAS evidence"
            )
        existing = self._connection.execute(
            "SELECT * FROM explorer_record_receipts WHERE receipt_sha256=?",
            (receipt_sha256,),
        ).fetchone()
        if existing is not None:
            if (
                existing["record_id"] != record_id
                or existing["call_id"] != context.call_id
                or int(existing["lease_epoch"]) != context.lease_epoch
                or int(existing["launch_attempt"]) != context.launch_attempt
            ):
                raise ExplorerIdempotencyConflict(
                    "trusted receipt was replayed outside its Explorer call provenance"
                )
            return
        self._connection.execute(
            """INSERT INTO explorer_record_receipts(
                   receipt_sha256,record_id,call_id,lease_epoch,launch_attempt
               ) VALUES(?,?,?,?,?)""",
            (
                receipt_sha256,
                record_id,
                context.call_id,
                context.lease_epoch,
                context.launch_attempt,
            ),
        )

    def _enforce_record_quota_locked(
        self,
        record_type: str,
        context: ExplorerWriteContext,
        *,
        max_records_per_attempt: int | None,
        max_records_per_turn: int | None,
    ) -> None:
        """Check per-type quotas while holding the repository write lock.

        Prepared rows count even before their receipts become trusted. Scratch
        and summary quotas are deliberately independent, so runtime scratch
        limits cannot be bypassed or consumed by attempt-final summaries.
        """

        if max_records_per_attempt is not None:
            row = self._connection.execute(
                """SELECT COUNT(*) AS total FROM explorer_records
                   WHERE record_type=? AND turn_id=? AND worker_session_id=?
                     AND attempt_no=?""",
                (
                    record_type,
                    context.turn_id,
                    context.worker_session_id,
                    context.attempt_no,
                ),
            ).fetchone()
            assert row is not None
            if int(row["total"]) >= max_records_per_attempt:
                raise ExplorerValidationError(
                    f"Explorer {record_type} per-attempt quota is exhausted"
                )
        if max_records_per_turn is not None:
            row = self._connection.execute(
                """SELECT COUNT(*) AS total FROM explorer_records
                   WHERE record_type=? AND turn_id=?""",
                (record_type, context.turn_id),
            ).fetchone()
            assert row is not None
            if int(row["total"]) >= max_records_per_turn:
                raise ExplorerValidationError(
                    f"Explorer {record_type} per-turn quota is exhausted"
                )

    def _prepare_record(
        self,
        record_type: str,
        context: ExplorerWriteContext,
        payload: Mapping[str, Any],
        receipt_sha256: str,
        *,
        max_records_per_attempt: int | None = None,
        max_records_per_turn: int | None = None,
    ) -> ExplorerRecord:
        if record_type not in EXPLORER_RECORD_TYPES:
            raise ExplorerValidationError(f"unknown Explorer record type: {record_type}")
        receipt = _receipt_hash(receipt_sha256)
        attempt_quota = _quota(
            max_records_per_attempt, "max_records_per_attempt"
        )
        turn_quota = _quota(max_records_per_turn, "max_records_per_turn")
        normalized = self._normalize_record_payload(record_type, payload)
        client_operation_id = str(normalized["operation_id"])
        scoped_operation_id = _scoped_operation_id(
            "REC", context, client_operation_id
        )
        semantic = dict(normalized)
        # The staged runtime allocates ES-/ESUM- IDs.  A transport retry may
        # allocate a fresh candidate ID for the same operation; semantic
        # idempotency returns the first accepted record rather than treating
        # that runtime-owned envelope value as new mathematical content.
        semantic.pop("record_id", None)
        immutable_input = {
            "record_type": record_type,
            "turn_id": context.turn_id,
            "worker_session_id": context.worker_session_id,
            "attempt_no": context.attempt_no,
            **semantic,
            "related_memory_ids": list(normalized["related_memory_ids"]),
            "cas_operation_ids": list(normalized["cas_operation_ids"]),
            "source_scratch_ids": list(normalized["source_scratch_ids"]),
            "directions_tried": list(normalized["directions_tried"]),
        }
        input_digest = _digest(immutable_input)
        with self.transaction():
            cas_rows = self._resolve_cas_operations_locked(
                normalized["cas_operation_ids"], context
            )
            source_rows = self._source_rows_locked(
                normalized["source_scratch_ids"], context
            )
            source_set_digest = (
                _digest(
                    [
                        {
                            "record_id": row["record_id"],
                            "input_digest": row["input_digest"],
                        }
                        for row in source_rows
                    ]
                )
                if record_type == "summary"
                else None
            )
            existing = self._connection.execute(
                """SELECT * FROM explorer_records
                   WHERE turn_id=? AND worker_session_id=? AND attempt_no=?
                     AND (
                       operation_id=?
                       OR (client_operation_id IS NULL AND operation_id=?)
                     )
                   LIMIT 1""",
                (
                    context.turn_id,
                    context.worker_session_id,
                    context.attempt_no,
                    scoped_operation_id,
                    client_operation_id,
                ),
            ).fetchone()
            if existing is not None:
                if (
                    existing["record_type"] != record_type
                    or existing["input_digest"] != input_digest
                    or existing["source_set_digest"] != source_set_digest
                ):
                    raise ExplorerIdempotencyConflict(
                        f"operation {normalized['operation_id']} was replayed differently"
                    )
                record_id = str(existing["record_id"])
            else:
                if record_type == "scratch":
                    closed = self._connection.execute(
                        """SELECT 1 FROM explorer_records
                           WHERE record_type='summary' AND turn_id=?
                             AND worker_session_id=? AND attempt_no=? LIMIT 1""",
                        (
                            context.turn_id,
                            context.worker_session_id,
                            context.attempt_no,
                        ),
                    ).fetchone()
                    if closed is not None:
                        raise ExplorerValidationError(
                            "Explorer attempt is closed by its summary"
                        )
                self._enforce_record_quota_locked(
                    record_type,
                    context,
                    max_records_per_attempt=attempt_quota,
                    max_records_per_turn=turn_quota,
                )
                record_id = normalized["record_id"]
                id_owner = self._connection.execute(
                    "SELECT operation_id FROM explorer_records WHERE record_id=?",
                    (record_id,),
                ).fetchone()
                if id_owner is not None:
                    raise ExplorerIdempotencyConflict(
                        "runtime-owned Explorer record ID is already allocated"
                    )
                self._connection.execute(
                    """INSERT INTO explorer_records(
                           record_id,record_type,operation_id,client_operation_id,
                           input_digest,content_digest,
                           turn_id,worker_session_id,attempt_no,record_kind,
                           previous_direction_id,ongoing_direction,abstract,content,
                           related_memory_ids_json,cas_operation_ids_json,
                           directions_tried_json,main_progress,main_obstacles,
                           source_set_digest,created_at
                       ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        record_id,
                        record_type,
                        scoped_operation_id,
                        client_operation_id,
                        input_digest,
                        _content_hash(normalized["content"]),
                        context.turn_id,
                        context.worker_session_id,
                        context.attempt_no,
                        normalized["record_kind"],
                        normalized["previous_direction_id"],
                        int(normalized["ongoing_direction"]),
                        normalized["abstract"],
                        normalized["content"],
                        _canonical_json(list(normalized["related_memory_ids"])),
                        _canonical_json(list(normalized["cas_operation_ids"])),
                        (
                            _canonical_json(list(normalized["directions_tried"]))
                            if record_type == "summary"
                            else None
                        ),
                        normalized["main_progress"],
                        normalized["main_obstacles"],
                        source_set_digest,
                        _utc_now(),
                    ),
                )
                for ordinal, row in enumerate(source_rows):
                    self._connection.execute(
                        """INSERT INTO explorer_summary_sources(
                               summary_id,ordinal,scratch_id,scratch_digest
                           ) VALUES(?,?,?,?)""",
                        (
                            record_id,
                            ordinal,
                            row["record_id"],
                            row["input_digest"],
                        ),
                    )
                for ordinal, (operation_id, evidence) in enumerate(
                    zip(normalized["cas_operation_ids"], cas_rows, strict=True)
                ):
                    self._connection.execute(
                        """INSERT INTO explorer_record_cas_evidence(
                               record_id,ordinal,operation_id,evidence_id,evidence_digest
                           ) VALUES(?,?,?,?,?)""",
                        (
                            record_id,
                            ordinal,
                            operation_id,
                            evidence["evidence_id"],
                            evidence["input_digest"],
                        ),
                    )
            self._link_record_receipt_locked(record_id, receipt, context)
            row = self._connection.execute(
                "SELECT * FROM explorer_records WHERE record_id=?", (record_id,)
            ).fetchone()
            assert row is not None
            return self._record_from_row_locked(row)

    def prepare_scratch(
        self,
        context: ExplorerWriteContext,
        payload: Mapping[str, Any],
        receipt_sha256: str,
        *,
        max_records_per_attempt: int | None = None,
        max_records_per_turn: int | None = None,
    ) -> ExplorerRecord:
        """Prepare an immutable scratch; it remains invisible until trust."""

        return self._prepare_record(
            "scratch",
            context,
            payload,
            receipt_sha256,
            max_records_per_attempt=max_records_per_attempt,
            max_records_per_turn=max_records_per_turn,
        )

    def prepare_summary(
        self,
        context: ExplorerWriteContext,
        payload: Mapping[str, Any],
        receipt_sha256: str,
        *,
        max_records_per_attempt: int | None = None,
        max_records_per_turn: int | None = None,
    ) -> ExplorerRecord:
        """Prepare a provisional summary under independent summary quotas."""

        return self._prepare_record(
            "summary",
            context,
            payload,
            receipt_sha256,
            max_records_per_attempt=max_records_per_attempt,
            max_records_per_turn=max_records_per_turn,
        )

    def trust_receipt(self, receipt_sha256: str) -> None:
        """Make every prepared object linked to one private receipt visible."""

        receipt = _receipt_hash(receipt_sha256)
        with self.transaction():
            existing = self._connection.execute(
                "SELECT 1 FROM explorer_trusted_receipts WHERE receipt_sha256=?",
                (receipt,),
            ).fetchone()
            if existing is not None:
                return
            record_link = self._connection.execute(
                "SELECT 1 FROM explorer_record_receipts WHERE receipt_sha256=?",
                (receipt,),
            ).fetchone()
            cas_link = self._connection.execute(
                "SELECT 1 FROM explorer_cas_receipts WHERE receipt_sha256=?",
                (receipt,),
            ).fetchone()
            if record_link is None and cas_link is None:
                raise ExplorerNotFoundError(
                    "trusted receipt has no prepared Explorer object"
                )
            self._connection.execute(
                "INSERT INTO explorer_trusted_receipts(receipt_sha256,trusted_at) VALUES(?,?)",
                (receipt, _utc_now()),
            )
            if record_link is not None:
                record_row = self._connection.execute(
                    """SELECT record_id FROM explorer_record_receipts
                       WHERE receipt_sha256=?""",
                    (receipt,),
                ).fetchone()
                assert record_row is not None
                self._connection.execute(
                    """INSERT OR IGNORE INTO explorer_record_visibility(
                           record_id,receipt_sha256,visible_at
                       ) VALUES(?,?,?)""",
                    (record_row["record_id"], receipt, _utc_now()),
                )

    def replay_trusted_receipts(self, receipt_hashes: Iterable[str]) -> None:
        """Idempotently complete receipt tails after a runtime restart."""

        for receipt_hash in receipt_hashes:
            receipt = _receipt_hash(receipt_hash)
            with self._lock:
                known = self._connection.execute(
                    """SELECT 1 FROM explorer_record_receipts WHERE receipt_sha256=?
                       UNION ALL
                       SELECT 1 FROM explorer_cas_receipts WHERE receipt_sha256=?
                       LIMIT 1""",
                    (receipt, receipt),
                ).fetchone()
            if known is not None:
                self.trust_receipt(receipt)

    @staticmethod
    def _access_grant_from_row(row: sqlite3.Row) -> ExplorerAttemptAccessGrant:
        payload = _loads(str(row["private_payload_json"]))
        if not isinstance(payload, Mapping):
            raise ExplorerValidationError(
                "stored Explorer access grant payload is invalid"
            )
        grant = ExplorerAttemptAccessGrant.from_private_payload(payload)
        if (
            grant.grant_id != str(row["grant_id"])
            or grant.turn_id != str(row["turn_id"])
            or grant.worker_session_id != str(row["worker_session_id"])
            or grant.attempt_no != int(row["attempt_no"])
            or grant.access_mode != str(row["access_mode"])
            or grant.host_snapshot_revision
            != str(row["host_snapshot_revision"])
            or grant.host_snapshot_digest != str(row["host_snapshot_digest"])
            or grant.explorer_high_water_seq
            != int(row["explorer_high_water_seq"])
            or grant.input_digest != str(row["input_digest"])
            or (
                None if grant.portfolio is None else grant.portfolio.portfolio_digest
            )
            != row["portfolio_digest"]
        ):
            raise ExplorerValidationError(
                "stored Explorer access grant metadata does not match its payload"
            )
        return grant

    def put_attempt_access_grant(
        self, grant: ExplorerAttemptAccessGrant
    ) -> ExplorerAttemptAccessGrant:
        """Idempotently append one immutable logical-attempt access grant."""

        if not isinstance(grant, ExplorerAttemptAccessGrant):
            raise ExplorerValidationError(
                "put_attempt_access_grant requires an Explorer access grant"
            )
        assert grant.input_digest is not None
        portfolio_digest = (
            None if grant.portfolio is None else grant.portfolio.portfolio_digest
        )
        private_payload = _canonical_json(grant.private_payload())
        with self.transaction():
            existing = self._connection.execute(
                """SELECT * FROM explorer_attempt_access_grants
                   WHERE grant_id=? OR (
                       turn_id=? AND worker_session_id=? AND attempt_no=?
                   )""",
                (
                    grant.grant_id,
                    grant.turn_id,
                    grant.worker_session_id,
                    grant.attempt_no,
                ),
            ).fetchone()
            if existing is not None:
                restored = self._access_grant_from_row(existing)
                if restored != grant:
                    raise ExplorerIdempotencyConflict(
                        "attempt access grant was replayed with different input"
                    )
                return restored
            self._connection.execute(
                """INSERT INTO explorer_attempt_access_grants(
                       grant_id,turn_id,worker_session_id,attempt_no,access_mode,
                       host_snapshot_revision,host_snapshot_digest,
                       explorer_high_water_seq,portfolio_digest,input_digest,
                       private_payload_json,created_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    grant.grant_id,
                    grant.turn_id,
                    grant.worker_session_id,
                    grant.attempt_no,
                    grant.access_mode,
                    grant.host_snapshot_revision,
                    grant.host_snapshot_digest,
                    grant.explorer_high_water_seq,
                    portfolio_digest,
                    grant.input_digest,
                    private_payload,
                    _utc_now(),
                ),
            )
            row = self._connection.execute(
                "SELECT * FROM explorer_attempt_access_grants WHERE grant_id=?",
                (grant.grant_id,),
            ).fetchone()
            assert row is not None
            return self._access_grant_from_row(row)

    def get_attempt_access_grant(
        self,
        turn_id: str,
        worker_session_id: str,
        attempt_no: int,
    ) -> ExplorerAttemptAccessGrant | None:
        """Load the immutable grant bound to one logical attempt identity."""

        turn = _text(turn_id, "turn_id", maximum=256)
        session = _text(worker_session_id, "worker_session_id", maximum=256)
        if (
            not isinstance(attempt_no, int)
            or isinstance(attempt_no, bool)
            or attempt_no not in {1, 2, 3}
        ):
            raise ExplorerValidationError("attempt_no must be 1, 2, or 3")
        with self._lock:
            row = self._connection.execute(
                """SELECT * FROM explorer_attempt_access_grants
                   WHERE turn_id=? AND worker_session_id=? AND attempt_no=?""",
                (turn, session, attempt_no),
            ).fetchone()
            return None if row is None else self._access_grant_from_row(row)

    def get_attempt_access_grant_by_id(
        self, grant_id: str
    ) -> ExplorerAttemptAccessGrant | None:
        exact = _text(grant_id, "grant_id", maximum=256)
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM explorer_attempt_access_grants WHERE grant_id=?",
                (exact,),
            ).fetchone()
            return None if row is None else self._access_grant_from_row(row)

    def _exported_ids_locked(self, record_id: str) -> tuple[str, ...]:
        rows = self._connection.execute(
            """SELECT DISTINCT pe.canonical_id
               FROM explorer_promotion_sources ps
               JOIN explorer_promotion_events pe
                 ON pe.promotion_id=ps.promotion_id
               WHERE ps.record_id=? AND pe.state='committed'
                 AND pe.canonical_id IS NOT NULL
               ORDER BY pe.canonical_id""",
            (record_id,),
        ).fetchall()
        return tuple(str(row["canonical_id"]) for row in rows)

    def _record_from_row_locked(self, row: sqlite3.Row) -> ExplorerRecord:
        source_rows = self._connection.execute(
            """SELECT scratch_id FROM explorer_summary_sources
               WHERE summary_id=? ORDER BY ordinal""",
            (row["record_id"],),
        ).fetchall()
        visibility = self._connection.execute(
            """SELECT seq FROM explorer_record_visibility
               WHERE record_id=?""",
            (row["record_id"],),
        ).fetchone()
        return ExplorerRecord(
            seq=0 if visibility is None else int(visibility["seq"]),
            record_id=str(row["record_id"]),
            record_type=str(row["record_type"]),
            operation_id=str(row["client_operation_id"] or row["operation_id"]),
            input_digest=str(row["input_digest"]),
            content_digest=str(row["content_digest"]),
            turn_id=str(row["turn_id"]),
            worker_session_id=str(row["worker_session_id"]),
            attempt_no=int(row["attempt_no"]),
            record_kind=row["record_kind"],
            abstract=str(row["abstract"]),
            content=str(row["content"]),
            related_memory_ids=tuple(_loads(row["related_memory_ids_json"])),
            cas_operation_ids=tuple(_loads(row["cas_operation_ids_json"])),
            source_scratch_ids=tuple(
                str(source["scratch_id"]) for source in source_rows
            ),
            source_set_digest=row["source_set_digest"],
            directions_tried=tuple(
                _loads(row["directions_tried_json"])
                if row["directions_tried_json"] is not None
                else ()
            ),
            main_progress=row["main_progress"],
            main_obstacles=row["main_obstacles"],
            created_at=str(row["created_at"]),
            exported_record_ids=self._exported_ids_locked(str(row["record_id"])),
        )

    def fetch(
        self,
        scope: ExplorerReadScope,
        record_id: str,
    ) -> ExplorerRecord | None:
        """Return one trusted record if and only if the scope permits it."""

        if not isinstance(record_id, str) or not EXPLORER_RECORD_ID_RE.fullmatch(record_id):
            raise ExplorerValidationError("Explorer fetch requires an ES-* or ESUM-* ID")
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM explorer_records WHERE record_id=?", (record_id,)
            ).fetchone()
            if row is None or not self._record_is_trusted_locked(record_id):
                return None
            if not scope.permits(
                turn_id=str(row["turn_id"]),
                worker_session_id=str(row["worker_session_id"]),
                seq=int(
                    self._connection.execute(
                        "SELECT seq FROM explorer_record_visibility WHERE record_id=?",
                        (record_id,),
                    ).fetchone()["seq"]
                ),
            ):
                return None
            return self._record_from_row_locked(row)

    def search_documents(
        self,
        scope: ExplorerReadScope,
        record_types: Iterable[str] = EXPLORER_RECORD_TYPES,
    ) -> list[ExplorerSearchDocument]:
        """Return trusted abstract-first candidates inside a frozen scope."""

        requested = frozenset(record_types)
        if not requested or not requested <= EXPLORER_RECORD_TYPES:
            raise ExplorerValidationError(
                "Explorer record types must be a nonempty scratch/summary subset"
            )
        if not scope.allowed_turn_ids:
            return []
        conditions = [
            "r.record_type IN (" + ",".join("?" for _ in requested) + ")",
            "r.turn_id IN (" + ",".join("?" for _ in scope.allowed_turn_ids) + ")",
        ]
        parameters: list[Any] = [*sorted(requested), *sorted(scope.allowed_turn_ids)]
        if scope.allowed_worker_session_ids is not None:
            if not scope.allowed_worker_session_ids:
                return []
            conditions.append(
                "r.worker_session_id IN ("
                + ",".join("?" for _ in scope.allowed_worker_session_ids)
                + ")"
            )
            parameters.extend(sorted(scope.allowed_worker_session_ids))
        if scope.max_seq is not None:
            conditions.append("v.seq<=?")
            parameters.append(scope.max_seq)
        query = (
            "SELECT r.* FROM explorer_records r "
            "JOIN explorer_record_visibility v ON v.record_id=r.record_id WHERE "
            + " AND ".join(conditions)
            + " ORDER BY v.seq"
        )
        with self._lock:
            rows = self._connection.execute(query, tuple(parameters)).fetchall()
            return [
                ExplorerSearchDocument(self._record_from_row_locked(row)) for row in rows
            ]

    def visible_high_water(self) -> int:
        """Return the latest trusted record-visibility sequence, or zero."""

        with self._lock:
            row = self._connection.execute(
                "SELECT COALESCE(MAX(seq),0) AS high_water FROM explorer_record_visibility"
            ).fetchone()
            assert row is not None
            return int(row["high_water"])

    def trusted_turn_ids(self, max_seq: int | None = None) -> tuple[str, ...]:
        """List trusted turns by first visibility, optionally at a high-water mark."""

        if max_seq is not None and (
            not isinstance(max_seq, int)
            or isinstance(max_seq, bool)
            or max_seq < 0
        ):
            raise ExplorerValidationError(
                "max_seq must be a nonnegative integer or None"
            )
        condition = "" if max_seq is None else "WHERE v.seq<=?"
        parameters: tuple[Any, ...] = () if max_seq is None else (max_seq,)
        with self._lock:
            rows = self._connection.execute(
                "SELECT r.turn_id,MIN(v.seq) AS first_visibility "
                "FROM explorer_records r "
                "JOIN explorer_record_visibility v ON v.record_id=r.record_id "
                f"{condition} GROUP BY r.turn_id "
                "ORDER BY first_visibility,r.turn_id",
                parameters,
            ).fetchall()
            return tuple(str(row["turn_id"]) for row in rows)

    def _freeze_turn_locked(
        self,
        turn_id: str,
        *,
        max_seq: int | None = None,
    ) -> FrozenExplorerTurn:
        conditions = [
            "r.turn_id=?",
        ]
        parameters: list[Any] = [turn_id]
        if max_seq is not None:
            conditions.append("v.seq<=?")
            parameters.append(max_seq)
        rows = self._connection.execute(
            "SELECT r.*,v.seq AS visibility_seq FROM explorer_records r "
            "JOIN explorer_record_visibility v ON v.record_id=r.record_id WHERE "
            + " AND ".join(conditions)
            + " ORDER BY v.seq",
            tuple(parameters),
        ).fetchall()
        snapshot = [
            {
                "record_id": str(row["record_id"]),
                "input_digest": str(row["input_digest"]),
            }
            for row in rows
        ]
        return FrozenExplorerTurn(
            turn_id=turn_id,
            high_water_seq=max(
                (int(row["visibility_seq"]) for row in rows), default=0
            ),
            record_count=len(rows),
            source_set_digest=_digest(snapshot),
            record_ids=tuple(item["record_id"] for item in snapshot),
        )

    def freeze_turn(self, turn_id: str) -> FrozenExplorerTurn:
        turn = _text(turn_id, "turn_id", maximum=256)
        with self._lock:
            return self._freeze_turn_locked(turn)

    def create_handoff_run(
        self,
        run_id: str,
        frozen_turn: FrozenExplorerTurn,
        host_context_id: str,
    ) -> ExplorerHandoffRun:
        run_id = _text(run_id, "run_id", maximum=256)
        task_id = _text(host_context_id, "host_context_id", maximum=256)
        if not self._host_context_id_pattern.fullmatch(task_id):
            raise ExplorerValidationError(
                "host_context_id does not match the collaborator contract"
            )
        turn_id = _text(frozen_turn.turn_id, "turn_id", maximum=256)
        run_field, context_field = self._handoff_digest_fields
        immutable = {
            run_field: run_id,
            "turn_id": turn_id,
            "source_high_water_seq": frozen_turn.high_water_seq,
            "source_set_digest": frozen_turn.source_set_digest,
            context_field: task_id,
        }
        input_digest = _digest(immutable)
        with self.transaction():
            actual = self._freeze_turn_locked(
                turn_id, max_seq=frozen_turn.high_water_seq
            )
            if actual != frozen_turn:
                raise ExplorerIdempotencyConflict(
                    "frozen Explorer turn does not match trusted repository state"
                )
            existing = self._connection.execute(
                """SELECT * FROM explorer_sort_runs
                   WHERE sort_run_id=? OR sort_task_id=?""",
                (run_id, task_id),
            ).fetchone()
            if existing is not None:
                if (
                    existing["sort_run_id"] != run_id
                    or existing["sort_task_id"] != task_id
                    or existing["input_digest"] != input_digest
                ):
                    raise ExplorerIdempotencyConflict("handoff run was replayed differently")
                return self._sort_run_from_row(existing)
            self._connection.execute(
                """INSERT INTO explorer_sort_runs(
                       sort_run_id,turn_id,source_high_water_seq,source_set_digest,
                       sort_task_id,input_digest,created_at
                   ) VALUES(?,?,?,?,?,?,?)""",
                (
                    run_id,
                    turn_id,
                    frozen_turn.high_water_seq,
                    frozen_turn.source_set_digest,
                    task_id,
                    input_digest,
                    _utc_now(),
                ),
            )
            row = self._connection.execute(
                "SELECT * FROM explorer_sort_runs WHERE sort_run_id=?", (run_id,)
            ).fetchone()
            assert row is not None
            return self._sort_run_from_row(row)

    @staticmethod
    def _sort_run_from_row(row: sqlite3.Row) -> ExplorerHandoffRun:
        return ExplorerHandoffRun(
            run_id=str(row["sort_run_id"]),
            turn_id=str(row["turn_id"]),
            source_high_water_seq=int(row["source_high_water_seq"]),
            source_set_digest=str(row["source_set_digest"]),
            host_context_id=str(row["sort_task_id"]),
            input_digest=str(row["input_digest"]),
            created_at=str(row["created_at"]),
        )

    def get_handoff_run(self, run_id: str) -> ExplorerHandoffRun | None:
        exact = _text(run_id, "run_id", maximum=256)
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM explorer_sort_runs WHERE sort_run_id=?", (exact,)
            ).fetchone()
            return None if row is None else self._sort_run_from_row(row)

    def _records_for_frozen_turn_locked(
        self,
        frozen: FrozenExplorerTurn,
    ) -> tuple[ExplorerRecord, ...]:
        current = self._freeze_turn_locked(
            frozen.turn_id,
            max_seq=frozen.high_water_seq,
        )
        if current != frozen:
            raise ExplorerIdempotencyConflict(
                "frozen Explorer turn no longer matches trusted records"
            )
        rows = self._connection.execute(
            """SELECT r.* FROM explorer_records r
               JOIN explorer_record_visibility v ON v.record_id=r.record_id
               WHERE r.turn_id=? AND v.seq<=?
               ORDER BY v.seq""",
            (frozen.turn_id, frozen.high_water_seq),
        ).fetchall()
        records = tuple(
            replace(
                self._record_from_row_locked(row),
                exported_record_ids=(),
            )
            for row in rows
        )
        if tuple(record.record_id for record in records) != frozen.record_ids:
            raise ExplorerIdempotencyConflict(
                "frozen Explorer turn record order drifted"
            )
        return records

    def records_for_frozen_turn(
        self,
        frozen: FrozenExplorerTurn,
    ) -> tuple[ExplorerRecord, ...]:
        """Return one explicitly frozen trusted record set without mutable exports."""

        if not isinstance(frozen, FrozenExplorerTurn):
            raise ExplorerValidationError("frozen Explorer turn is required")
        with self._lock:
            return self._records_for_frozen_turn_locked(frozen)

    def records_for_handoff(self, run_id: str) -> tuple[ExplorerRecord, ...]:
        """Return the exact trusted records pinned by one immutable handoff.

        This is an export boundary, not a search API.  Revalidating the stored
        high-water mark and ordered source digest makes a recreated sorter
        workspace byte-stable and excludes later, untrusted, or retry-only
        physical records.
        """

        exact = _text(run_id, "run_id", maximum=256)
        with self._lock:
            run = self._connection.execute(
                "SELECT * FROM explorer_sort_runs WHERE sort_run_id=?", (exact,)
            ).fetchone()
            if run is None:
                raise ExplorerNotFoundError(f"unknown Explorer handoff run: {exact}")
            frozen = self._freeze_turn_locked(
                str(run["turn_id"]),
                max_seq=int(run["source_high_water_seq"]),
            )
            if (
                frozen.high_water_seq != int(run["source_high_water_seq"])
                or frozen.source_set_digest != str(run["source_set_digest"])
            ):
                raise ExplorerIdempotencyConflict(
                    "stored Explorer handoff no longer matches trusted records"
                )
            return self._records_for_frozen_turn_locked(frozen)

    def _export_source_rows_locked(
        self,
        run: sqlite3.Row,
        source_record_ids: Sequence[str],
    ) -> list[sqlite3.Row]:
        expanded: list[str] = []
        seen: set[str] = set()
        for record_id in source_record_ids:
            if record_id not in seen:
                expanded.append(record_id)
                seen.add(record_id)
            source_rows = self._connection.execute(
                """SELECT scratch_id FROM explorer_summary_sources
                   WHERE summary_id=? ORDER BY ordinal""",
                (record_id,),
            ).fetchall()
            for source in source_rows:
                scratch_id = str(source["scratch_id"])
                if scratch_id not in seen:
                    expanded.append(scratch_id)
                    seen.add(scratch_id)
        rows: list[sqlite3.Row] = []
        for record_id in expanded:
            row = self._connection.execute(
                "SELECT * FROM explorer_records WHERE record_id=?", (record_id,)
            ).fetchone()
            if row is None or not self._record_is_trusted_locked(record_id):
                raise ExplorerNotFoundError(
                    f"trusted export source not found: {record_id}"
                )
            visibility = self._connection.execute(
                "SELECT seq FROM explorer_record_visibility WHERE record_id=?",
                (record_id,),
            ).fetchone()
            assert visibility is not None
            if (
                row["turn_id"] != run["turn_id"]
                or int(visibility["seq"]) > int(run["source_high_water_seq"])
            ):
                raise ExplorerValidationError(
                    f"export source {record_id} is outside the frozen turn"
                )
            rows.append(row)
        return rows

    def stage_export(
        self,
        run_id: str,
        host_item_id: str,
        target_kind: str,
        source_record_ids: Sequence[str],
        operation_digest: str,
    ) -> ExplorerExport:
        run_id = _text(run_id, "run_id", maximum=256)
        operation_id = _text(
            host_item_id, "host_item_id", maximum=256
        )
        if target_kind not in self._export_kinds:
            raise ExplorerValidationError("unsupported Explorer export kind")
        if not isinstance(operation_digest, str) or not _SHA256_RE.fullmatch(
            operation_digest.lower()
        ):
            raise ExplorerValidationError("operation_digest must be SHA-256")
        sources = tuple(source_record_ids)
        if not sources or len(sources) > MAX_SOURCE_SCRATCHES:
            raise ExplorerValidationError("export requires 1-64 source record IDs")
        if len(set(sources)) != len(sources) or any(
            not isinstance(item, str) or not EXPLORER_RECORD_ID_RE.fullmatch(item)
            for item in sources
        ):
            raise ExplorerValidationError("export source IDs are invalid or duplicated")
        with self.transaction():
            run = self._connection.execute(
                "SELECT * FROM explorer_sort_runs WHERE sort_run_id=?", (run_id,)
            ).fetchone()
            if run is None:
                raise ExplorerNotFoundError(f"unknown Explorer handoff run: {run_id}")
            source_rows = self._export_source_rows_locked(run, sources)
            declaration = {
                "run_id": run_id,
                "host_item_id": operation_id,
                "target_kind": target_kind,
                "operation_digest": operation_digest.lower(),
                "sources": [
                    {
                        "record_id": row["record_id"],
                        "record_digest": row["input_digest"],
                    }
                    for row in source_rows
                ],
            }
            promotion_id = "XP-" + _digest(declaration)[:32]
            existing = self._connection.execute(
                """SELECT * FROM explorer_promotions
                   WHERE promotion_id=? OR franta_operation_id=?""",
                (promotion_id, operation_id),
            ).fetchone()
            if existing is not None:
                existing_sources = self._connection.execute(
                    """SELECT record_id,record_digest FROM explorer_promotion_sources
                       WHERE promotion_id=? ORDER BY rowid""",
                    (existing["promotion_id"],),
                ).fetchall()
                actual = {
                    "run_id": existing["sort_run_id"],
                    "host_item_id": existing["franta_operation_id"],
                    "target_kind": existing["target_kind"],
                    "operation_digest": existing["operation_digest"],
                    "sources": [
                        {
                            "record_id": row["record_id"],
                            "record_digest": row["record_digest"],
                        }
                        for row in existing_sources
                    ],
                }
                if actual != declaration:
                    raise ExplorerIdempotencyConflict(
                        "host item was replayed with different Explorer provenance"
                    )
                return self._export_from_row_locked(existing)
            self._connection.execute(
                """INSERT INTO explorer_promotions(
                       promotion_id,sort_run_id,franta_operation_id,target_kind,
                       operation_digest,created_at
                   ) VALUES(?,?,?,?,?,?)""",
                (
                    promotion_id,
                    run_id,
                    operation_id,
                    target_kind,
                    operation_digest.lower(),
                    _utc_now(),
                ),
            )
            for row in source_rows:
                self._connection.execute(
                    """INSERT INTO explorer_promotion_sources(
                           promotion_id,record_id,record_digest
                       ) VALUES(?,?,?)""",
                    (promotion_id, row["record_id"], row["input_digest"]),
                )
            self._connection.execute(
                """INSERT INTO explorer_promotion_events(
                       promotion_id,state,canonical_id,resolution,error,created_at
                   ) VALUES(?,'received',NULL,NULL,NULL,?)""",
                (promotion_id, _utc_now()),
            )
            row = self._connection.execute(
                "SELECT * FROM explorer_promotions WHERE promotion_id=?",
                (promotion_id,),
            ).fetchone()
            assert row is not None
            return self._export_from_row_locked(row)

    def _export_from_row_locked(self, row: sqlite3.Row) -> ExplorerExport:
        source_rows = self._connection.execute(
            """SELECT record_id FROM explorer_promotion_sources
               WHERE promotion_id=? ORDER BY rowid""",
            (row["promotion_id"],),
        ).fetchall()
        event = self._connection.execute(
            """SELECT * FROM explorer_promotion_events
               WHERE promotion_id=? ORDER BY seq DESC LIMIT 1""",
            (row["promotion_id"],),
        ).fetchone()
        assert event is not None
        return ExplorerExport(
            export_id=str(row["promotion_id"]),
            run_id=str(row["sort_run_id"]),
            host_item_id=str(row["franta_operation_id"]),
            target_kind=str(row["target_kind"]),
            operation_digest=str(row["operation_digest"]),
            source_record_ids=tuple(
                str(source["record_id"]) for source in source_rows
            ),
            state=str(event["state"]),
            host_record_id=event["canonical_id"],
            resolution=event["resolution"],
            error=event["error"],
            created_at=str(row["created_at"]),
        )

    def get_export(self, host_item_id: str) -> ExplorerExport | None:
        operation_id = _text(
            host_item_id, "host_item_id", maximum=256
        )
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM explorer_promotions WHERE franta_operation_id=?",
                (operation_id,),
            ).fetchone()
            return None if row is None else self._export_from_row_locked(row)

    def list_exports(
        self,
        *,
        run_id: str | None = None,
        states: Iterable[str] | None = None,
    ) -> tuple[ExplorerExport, ...]:
        """List export declarations with their latest append-only state."""

        run_id = (
            None
            if run_id is None
            else _text(run_id, "run_id", maximum=256)
        )
        requested_states = (
            None if states is None else frozenset(states)
        )
        if requested_states is not None and (
            not requested_states or not requested_states <= EXPLORER_EXPORT_STATES
        ):
            raise ExplorerValidationError(
                "export states must be a nonempty authorized subset"
            )
        with self._lock:
            if run_id is None:
                rows = self._connection.execute(
                    "SELECT * FROM explorer_promotions ORDER BY created_at,promotion_id"
                ).fetchall()
            else:
                rows = self._connection.execute(
                    """SELECT * FROM explorer_promotions
                       WHERE sort_run_id=? ORDER BY created_at,promotion_id""",
                    (run_id,),
                ).fetchall()
            exports = tuple(
                self._export_from_row_locked(row) for row in rows
            )
            if requested_states is None:
                return exports
            return tuple(
                export
                for export in exports
                if export.state in requested_states
            )

    def list_pending_exports(
        self,
        *,
        run_id: str | None = None,
    ) -> tuple[ExplorerExport, ...]:
        """Return exports awaiting collaborator reconciliation."""

        return self.list_exports(
            run_id=run_id,
            states={"received"},
        )

    def resolve_export(
        self,
        host_item_id: str,
        state: str,
        *,
        host_record_id: str | None = None,
        resolution: str | None = None,
        error: str | None = None,
    ) -> ExplorerExport:
        operation_id = _text(
            host_item_id, "host_item_id", maximum=256
        )
        if state not in EXPLORER_EXPORT_STATES - {"received"}:
            raise ExplorerValidationError("export resolution requires a terminal state")
        if state == "committed":
            if (
                not isinstance(host_record_id, str)
                or not self._host_record_id_pattern.fullmatch(host_record_id)
            ):
                raise ExplorerValidationError(
                    "committed export requires a host record ID"
                )
        elif host_record_id is not None:
            raise ExplorerValidationError(
                "only a committed export may carry a host record ID"
            )
        resolution_value = (
            None if resolution is None else _text(resolution, "resolution", maximum=256)
        )
        error_value = None if error is None else _text(error, "error", maximum=16_384)
        with self.transaction():
            row = self._connection.execute(
                "SELECT * FROM explorer_promotions WHERE franta_operation_id=?",
                (operation_id,),
            ).fetchone()
            if row is None:
                raise ExplorerNotFoundError(
                    f"unknown exported host item: {operation_id}"
                )
            latest = self._connection.execute(
                """SELECT * FROM explorer_promotion_events
                   WHERE promotion_id=? ORDER BY seq DESC LIMIT 1""",
                (row["promotion_id"],),
            ).fetchone()
            assert latest is not None
            expected = (state, host_record_id, resolution_value, error_value)
            actual = (
                latest["state"],
                latest["canonical_id"],
                latest["resolution"],
                latest["error"],
            )
            if actual == expected:
                return self._export_from_row_locked(row)
            if latest["state"] != "received":
                raise ExplorerIdempotencyConflict(
                    "terminal export was replayed with a different result"
                )
            self._connection.execute(
                """INSERT INTO explorer_promotion_events(
                       promotion_id,state,canonical_id,resolution,error,created_at
                   ) VALUES(?,?,?,?,?,?)""",
                (
                    row["promotion_id"],
                    state,
                    host_record_id,
                    resolution_value,
                    error_value,
                    _utc_now(),
                ),
            )
            return self._export_from_row_locked(row)

    def prepare_cas_evidence(
        self,
        context: ExplorerWriteContext,
        artifact: Mapping[str, Any],
        receipt_sha256: str,
        *,
        archived_artifact_relpath: str | None = None,
    ) -> ExplorerCASEvidence:
        """Prepare normalized CAS evidence under the same receipt trust boundary."""

        if not isinstance(artifact, Mapping):
            raise ExplorerValidationError("CAS artifact must be an object")
        normalized = copy.deepcopy(dict(artifact))
        client_operation_id = _text(
            normalized.get("operation_id"), "operation_id", maximum=256
        )
        scoped_operation_id = _scoped_operation_id(
            "CAS", context, client_operation_id
        )
        receipt = _receipt_hash(receipt_sha256)
        if archived_artifact_relpath is not None:
            relative = PurePosixPath(
                _text(
                    archived_artifact_relpath,
                    "archived_artifact_relpath",
                    maximum=1_024,
                )
            )
            if relative.is_absolute() or ".." in relative.parts:
                raise ExplorerValidationError("archived CAS artifact path is unsafe")
            archived_artifact_relpath = relative.as_posix()
        output_artifact = normalized.get("output_artifact")
        output_sha: str | None = None
        if output_artifact is not None:
            if not isinstance(output_artifact, Mapping):
                raise ExplorerValidationError("CAS output_artifact must be an object")
            raw_sha = output_artifact.get("sha256")
            if not isinstance(raw_sha, str) or not _SHA256_RE.fullmatch(raw_sha.lower()):
                raise ExplorerValidationError("CAS output artifact requires SHA-256")
            output_sha = raw_sha.lower()
            if archived_artifact_relpath is None:
                raise ExplorerValidationError(
                    "CAS output artifact must be archived before registration"
                )
        raw_success = normalized.get("execution_succeeded")
        if isinstance(raw_success, bool):
            succeeded = raw_success
        else:
            raw_exit = normalized.get("exit_status")
            succeeded = not isinstance(raw_exit, bool) and str(raw_exit).strip() == "0"
        immutable = {
            "turn_id": context.turn_id,
            "worker_session_id": context.worker_session_id,
            "attempt_no": context.attempt_no,
            "artifact": normalized,
            "archived_artifact_relpath": archived_artifact_relpath,
        }
        input_digest = _digest(immutable)
        with self.transaction():
            existing = self._connection.execute(
                """SELECT * FROM explorer_cas_evidence
                   WHERE turn_id=? AND worker_session_id=? AND attempt_no=?
                     AND (
                       operation_id=?
                       OR (client_operation_id IS NULL AND operation_id=?)
                     )
                   LIMIT 1""",
                (
                    context.turn_id,
                    context.worker_session_id,
                    context.attempt_no,
                    scoped_operation_id,
                    client_operation_id,
                ),
            ).fetchone()
            if existing is not None:
                if existing["input_digest"] != input_digest:
                    raise ExplorerIdempotencyConflict(
                        f"CAS operation {client_operation_id} was replayed differently"
                    )
                evidence_id = str(existing["evidence_id"])
            else:
                evidence_id = f"XCAS-{uuid.uuid4().hex}"
                self._connection.execute(
                    """INSERT INTO explorer_cas_evidence(
                           evidence_id,operation_id,client_operation_id,input_digest,
                           turn_id,worker_session_id,
                           attempt_no,normalized_payload_json,execution_succeeded,
                           output_artifact_sha256,archived_artifact_relpath,created_at
                       ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        evidence_id,
                        scoped_operation_id,
                        client_operation_id,
                        input_digest,
                        context.turn_id,
                        context.worker_session_id,
                        context.attempt_no,
                        _canonical_json(normalized),
                        int(succeeded),
                        output_sha,
                        archived_artifact_relpath,
                        _utc_now(),
                    ),
                )
            link = self._connection.execute(
                "SELECT * FROM explorer_cas_receipts WHERE receipt_sha256=?",
                (receipt,),
            ).fetchone()
            record_link = self._connection.execute(
                "SELECT record_id FROM explorer_record_receipts WHERE receipt_sha256=?",
                (receipt,),
            ).fetchone()
            if record_link is not None:
                raise ExplorerIdempotencyConflict(
                    "trusted receipt was already allocated to an Explorer record"
                )
            if link is not None:
                if (
                    link["evidence_id"] != evidence_id
                    or link["call_id"] != context.call_id
                    or int(link["lease_epoch"]) != context.lease_epoch
                    or int(link["launch_attempt"]) != context.launch_attempt
                ):
                    raise ExplorerIdempotencyConflict(
                        "trusted receipt was replayed outside its CAS call provenance"
                    )
            if link is None:
                self._connection.execute(
                    """INSERT INTO explorer_cas_receipts(
                           receipt_sha256,evidence_id,call_id,lease_epoch,launch_attempt
                       ) VALUES(?,?,?,?,?)""",
                    (
                        receipt,
                        evidence_id,
                        context.call_id,
                        context.lease_epoch,
                        context.launch_attempt,
                    ),
                )
            row = self._connection.execute(
                "SELECT * FROM explorer_cas_evidence WHERE evidence_id=?",
                (evidence_id,),
            ).fetchone()
            assert row is not None
            return self._cas_from_row(row)

    @staticmethod
    def _cas_from_row(row: sqlite3.Row) -> ExplorerCASEvidence:
        return ExplorerCASEvidence(
            seq=int(row["seq"]),
            evidence_id=str(row["evidence_id"]),
            operation_id=str(row["client_operation_id"] or row["operation_id"]),
            input_digest=str(row["input_digest"]),
            turn_id=str(row["turn_id"]),
            worker_session_id=str(row["worker_session_id"]),
            attempt_no=int(row["attempt_no"]),
            normalized_payload=copy.deepcopy(_loads(row["normalized_payload_json"])),
            execution_succeeded=bool(row["execution_succeeded"]),
            output_artifact_sha256=row["output_artifact_sha256"],
            archived_artifact_relpath=row["archived_artifact_relpath"],
            created_at=str(row["created_at"]),
        )

    def get_cas_evidence(
        self,
        evidence_id: str,
        *,
        require_success: bool = False,
    ) -> ExplorerCASEvidence | None:
        if not isinstance(evidence_id, str) or not CAS_EVIDENCE_ID_RE.fullmatch(
            evidence_id
        ):
            raise ExplorerValidationError("invalid Explorer CAS evidence ID")
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM explorer_cas_evidence WHERE evidence_id=?",
                (evidence_id,),
            ).fetchone()
            if row is None or not self._evidence_is_trusted_locked(evidence_id):
                return None
            evidence = self._cas_from_row(row)
            if require_success and not evidence.execution_succeeded:
                return None
            return evidence

    def get_cas_evidence_by_operation(
        self,
        operation_id: str,
        *,
        require_success: bool = False,
        turn_id: str | None = None,
        worker_session_id: str | None = None,
        max_attempt_no: int | None = None,
    ) -> ExplorerCASEvidence | None:
        """Fetch trusted CAS evidence by the operation ID cited in scratch."""

        operation = _text(operation_id, "operation_id", maximum=256)
        if not _PATH_FREE_ID_RE.fullmatch(operation):
            raise ExplorerValidationError("invalid Explorer CAS operation ID")
        conditions = [
            "(client_operation_id=? OR "
            "(client_operation_id IS NULL AND operation_id=?))"
        ]
        parameters: list[Any] = [operation, operation]
        if turn_id is not None:
            conditions.append("turn_id=?")
            parameters.append(_text(turn_id, "turn_id", maximum=256))
        if worker_session_id is not None:
            conditions.append("worker_session_id=?")
            parameters.append(
                _text(worker_session_id, "worker_session_id", maximum=256)
            )
        if max_attempt_no is not None:
            if (
                not isinstance(max_attempt_no, int)
                or isinstance(max_attempt_no, bool)
                or max_attempt_no < 1
            ):
                raise ExplorerValidationError(
                    "max_attempt_no must be a positive integer"
                )
            conditions.append("attempt_no<=?")
            parameters.append(max_attempt_no)
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM explorer_cas_evidence WHERE "
                + " AND ".join(conditions)
                + " ORDER BY attempt_no DESC,seq DESC LIMIT 1",
                tuple(parameters),
            ).fetchone()
            if row is None or not self._evidence_is_trusted_locked(
                str(row["evidence_id"])
            ):
                return None
            evidence = self._cas_from_row(row)
            if require_success and not evidence.execution_succeeded:
                return None
            return evidence

    def list_cas_evidence_for_record(
        self,
        record_id: str,
        *,
        require_success: bool = False,
    ) -> tuple[ExplorerCASEvidence, ...]:
        """Return the immutable evidence identities pinned by one record."""

        if not isinstance(record_id, str) or not EXPLORER_RECORD_ID_RE.fullmatch(
            record_id
        ):
            raise ExplorerValidationError("invalid Explorer record ID")
        with self._lock:
            if not self._record_is_trusted_locked(record_id):
                return ()
            rows = self._connection.execute(
                """SELECT ce.*
                   FROM explorer_record_cas_evidence rc
                   JOIN explorer_cas_evidence ce
                     ON ce.evidence_id=rc.evidence_id
                   WHERE rc.record_id=?
                   ORDER BY rc.ordinal""",
                (record_id,),
            ).fetchall()
            result: list[ExplorerCASEvidence] = []
            for row in rows:
                evidence_id = str(row["evidence_id"])
                if not self._evidence_is_trusted_locked(evidence_id):
                    raise ExplorerValidationError(
                        "Explorer record cites evidence outside the trust ledger"
                    )
                evidence = self._cas_from_row(row)
                if require_success and not evidence.execution_succeeded:
                    continue
                result.append(evidence)
            return tuple(result)

    def resolve_computation_evidence(
        self,
        run_id: str,
        evidence_id: str,
        source_record_ids: Sequence[str],
    ) -> dict[str, Any]:
        """Return authenticated computation bytes for one frozen handoff.

        The model supplies only evidence/source IDs.  Software, input, output,
        success, and artifact hashes come from the trusted CAS registry, while
        host execution/publishing provenance is deliberately not added here.
        """

        run_id = _text(run_id, "run_id", maximum=256)
        if not isinstance(evidence_id, str) or not CAS_EVIDENCE_ID_RE.fullmatch(
            evidence_id
        ):
            raise ExplorerValidationError("invalid Explorer CAS evidence ID")
        with self._lock:
            run = self._connection.execute(
                "SELECT * FROM explorer_sort_runs WHERE sort_run_id=?", (run_id,)
            ).fetchone()
            if run is None:
                raise ExplorerNotFoundError(f"unknown Explorer handoff run: {run_id}")
            evidence_row = self._connection.execute(
                "SELECT * FROM explorer_cas_evidence WHERE evidence_id=?",
                (evidence_id,),
            ).fetchone()
            if (
                evidence_row is None
                or not self._evidence_is_trusted_locked(evidence_id)
                or not bool(evidence_row["execution_succeeded"])
                or evidence_row["turn_id"] != run["turn_id"]
            ):
                raise ExplorerNotFoundError(
                    "successful trusted CAS evidence is unavailable to this handoff run"
                )
            source_rows = self._export_source_rows_locked(run, source_record_ids)
            source_ids = tuple(str(row["record_id"]) for row in source_rows)
            cited = any(
                self._connection.execute(
                    """SELECT 1 FROM explorer_record_cas_evidence
                       WHERE record_id=? AND evidence_id=? LIMIT 1""",
                    (record_id, evidence_id),
                ).fetchone()
                is not None
                for record_id in source_ids
            )
            if not cited:
                raise ExplorerValidationError(
                    "CAS evidence is not cited by the selected Explorer source"
                )
            value = copy.deepcopy(_loads(evidence_row["normalized_payload_json"]))
        value.pop("skill", None)
        value.pop("arguments", None)
        value.pop("version_arguments", None)
        value.pop("execution_succeeded", None)
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
        if evidence_row["archived_artifact_relpath"] is not None:
            value["output_artifact"] = {
                "path": str(evidence_row["archived_artifact_relpath"]),
                "sha256": str(evidence_row["output_artifact_sha256"]),
            }
        return value


__all__ = [
    "ExplorerRepository",
    "MAX_ABSTRACT_CHARS",
    "MAX_CAS_OPERATION_IDS",
    "MAX_CONTENT_CHARS",
    "MAX_DIRECTIONS_TRIED",
    "MAX_RELATED_MEMORY_IDS",
    "MAX_SOURCE_SCRATCHES",
    "SCHEMA_VERSION",
]
