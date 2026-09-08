"""SQLite-backed canonical memory store.

SQLite is the sole durable authority.  Markdown files produced by this module
are deterministic projections and may always be rebuilt from the database.
The store owns ID allocation, validation, atomic publication, reciprocal link
maintenance, idempotency, status overlays, and revocation propagation.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import sqlite3
import tempfile
import threading
import uuid
from collections import deque
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Sequence

from .contracts.canonical import (
    CATEGORY_PREFIX,
    MEMORY_PREFIXES,
    AccessDeniedError,
    AccessPolicy,
    ConflictError,
    ControlState,
    IdempotencyConflict,
    MemoryRecord,
    MemoryType,
    NotFoundError,
    OperationResult,
    OperationType,
    RevocationResult,
    ValidationError,
)
from .render import projection_relative_path, render_record
from .contracts.references import contains_identifier_token, substitute_nonfact_typed_ids


SCHEMA_VERSION = 2
_ID_SUFFIX_RE = re.compile(r"^[0-9a-f]{32}$")
_VALUE_KEYS = ("confidence", "success_gain", "failure_gain", "relevance", "novelty")
_FACT_OR_OBLIGATION = "fact_or_obligation"
_UNPUBLISHED_SUFFIX = "(unpublished)"
_SEARCHABLE_TYPES = {
    MemoryType.FACT,
    MemoryType.ROUTE,
    MemoryType.MEMO,
    MemoryType.CLAIM,
    MemoryType.OBLIGATION,
    MemoryType.COMPUTATION,
}


def _reference_type_set(
    expected: MemoryType | Iterable[MemoryType] | str,
) -> frozenset[MemoryType]:
    """Return the concrete memory types admitted by one soft-reference slot."""

    if isinstance(expected, MemoryType):
        return frozenset({expected})
    if isinstance(expected, str):
        if expected == _FACT_OR_OBLIGATION:
            return frozenset({MemoryType.FACT, MemoryType.OBLIGATION})
        try:
            return frozenset({MemoryType(expected)})
        except ValueError as exc:
            raise ValidationError(f"unknown soft-reference type: {expected}") from exc
    result = frozenset(expected)
    if not result or not all(isinstance(item, MemoryType) for item in result):
        raise ValidationError("soft-reference type constraint is invalid")
    return result


def _reference_type_token(
    expected: MemoryType | Iterable[MemoryType] | str,
) -> str:
    concrete = _reference_type_set(expected)
    if len(concrete) == 1:
        return next(iter(concrete)).value
    if concrete == {MemoryType.FACT, MemoryType.OBLIGATION}:
        return _FACT_OR_OBLIGATION
    raise ValidationError("unsupported soft-reference type union")


def _reference_requires_active(kind: MemoryType) -> bool:
    return kind in {MemoryType.FACT, MemoryType.CLAIM, MemoryType.OBLIGATION}


def _reference_type_accepts(
    expected: MemoryType | Iterable[MemoryType] | str,
    actual: MemoryType,
) -> bool:
    return actual in _reference_type_set(expected)


def _strip_unpublished_suffix(reference_id: str) -> str:
    while reference_id.endswith(_UNPUBLISHED_SUFFIX):
        reference_id = reference_id[: -len(_UNPUBLISHED_SUFFIX)]
    return reference_id


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _json_default(value: Any) -> Any:
    if isinstance(value, (MemoryType, OperationType)):
        return value.value
    if isinstance(value, (set, frozenset, tuple)):
        return list(value)
    if hasattr(value, "to_dict"):
        return value.to_dict()
    raise TypeError(f"not JSON serializable: {type(value).__name__}")


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=_json_default,
    )


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _loads(value: str | None, default: Any = None) -> Any:
    if value is None:
        return default
    return json.loads(value)


def _require_mapping(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValidationError(f"{field} must be an object")
    return copy.deepcopy(dict(value))


def _require_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{field} must be a nonempty string")
    return value.strip()


def _optional_text(value: Any, field: str, default: str = "") -> str:
    if value is None:
        return default
    if not isinstance(value, str):
        raise ValidationError(f"{field} must be a string")
    return value.strip()


def _text_list(value: Any, field: str, *, allow_empty: bool = True) -> list[str]:
    if value is None:
        result: list[str] = []
    elif isinstance(value, str):
        result = [value.strip()] if value.strip() else []
    elif isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        result = []
        for index, item in enumerate(value):
            result.append(_require_text(item, f"{field}[{index}]"))
    else:
        raise ValidationError(f"{field} must be a string or list of strings")
    if not allow_empty and not result:
        raise ValidationError(f"{field} must not be empty")
    return result


def _id_list(value: Any, field: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise ValidationError(f"{field} must be a list of IDs")
    result: list[str] = []
    seen: set[str] = set()
    for index, item in enumerate(value):
        item = _require_text(item, f"{field}[{index}]")
        if item in seen:
            raise ValidationError(f"{field} contains duplicate ID {item}")
        seen.add(item)
        result.append(item)
    return result


class MemoryStore:
    """Canonical memory and scheduler-private durable control storage.

    ``projection_dir`` defaults to ``<database-stem>.projections`` beside a
    filesystem database.  Pass ``False`` or use ``:memory:`` to disable file
    projections.  Control state and control events are intentionally excluded
    from search, projections, and all agent-facing record methods.
    """

    def __init__(
        self,
        db_path: str | os.PathLike[str],
        projection_dir: str | os.PathLike[str] | None | bool = None,
    ) -> None:
        self.db_path = str(db_path)
        if projection_dir is False or self.db_path == ":memory:":
            self.projection_dir: Path | None = None
        elif projection_dir is None:
            path = Path(self.db_path)
            self.projection_dir = path.parent / f"{path.stem}.projections"
        else:
            self.projection_dir = Path(projection_dir)
        if self.db_path != ":memory:":
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._tx_depth = 0
        self._savepoint_counter = 0
        self._connection = sqlite3.connect(
            self.db_path,
            timeout=30.0,
            isolation_level=None,
            check_same_thread=False,
        )
        self._connection.row_factory = sqlite3.Row
        self._initialize()

    def _initialize(self) -> None:
        with self._lock:
            self._connection.execute("PRAGMA foreign_keys = ON")
            self._connection.execute("PRAGMA busy_timeout = 30000")
            self._connection.execute("PRAGMA synchronous = FULL")
            self._connection.execute("PRAGMA journal_mode = WAL")
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS schema_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS allocated_ids (
                    memory_id TEXT PRIMARY KEY,
                    memory_type TEXT NOT NULL,
                    allocated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS memories (
                    memory_id TEXT PRIMARY KEY,
                    memory_type TEXT NOT NULL CHECK (
                        memory_type IN ('fact','route','memo','claim','obligation','task','computation')
                    ),
                    revision INTEGER NOT NULL CHECK (revision >= 1),
                    metadata_version INTEGER NOT NULL DEFAULT 1 CHECK (metadata_version >= 1),
                    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0,1)),
                    abstract TEXT NOT NULL,
                    core_json TEXT NOT NULL,
                    metadata_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(memory_id) REFERENCES allocated_ids(memory_id)
                );
                CREATE INDEX IF NOT EXISTS memories_type_active
                    ON memories(memory_type, active);

                CREATE TABLE IF NOT EXISTS operations (
                    operation_id TEXT PRIMARY KEY,
                    operation_type TEXT NOT NULL,
                    input_hash TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (
                        status IN ('pending','committed','rejected','abandoned','needs_attention')
                    ),
                    canonical_ids_json TEXT NOT NULL DEFAULT '[]',
                    resolution TEXT,
                    result_json TEXT NOT NULL DEFAULT '{}',
                    error TEXT,
                    group_id TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS operation_groups (
                    group_id TEXT PRIMARY KEY,
                    input_hash TEXT NOT NULL,
                    status TEXT NOT NULL,
                    result_json TEXT NOT NULL,
                    error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS proposal_mappings (
                    proposal_id TEXT PRIMARY KEY,
                    operation_id TEXT NOT NULL UNIQUE,
                    canonical_id TEXT NOT NULL,
                    resolution TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(operation_id) REFERENCES operations(operation_id),
                    FOREIGN KEY(canonical_id) REFERENCES memories(memory_id)
                );

                CREATE TABLE IF NOT EXISTS temporary_memory_ids (
                    temporary_id TEXT PRIMARY KEY,
                    memory_type TEXT NOT NULL CHECK (
                        memory_type IN (
                            'fact','route','memo','claim','obligation','fact_or_obligation'
                        )
                    ),
                    state TEXT NOT NULL CHECK (
                        state IN ('pending','resolved','rejected','abandoned')
                    ),
                    canonical_id TEXT,
                    resolution TEXT,
                    target_operation_id TEXT,
                    reason TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(canonical_id) REFERENCES memories(memory_id)
                );

                CREATE TABLE IF NOT EXISTS deferred_typed_updates (
                    document_id TEXT PRIMARY KEY,
                    source_memory_id TEXT NOT NULL,
                    source_memory_type TEXT NOT NULL CHECK (
                        source_memory_type IN ('obligation')
                    ),
                    field_name TEXT NOT NULL CHECK (field_name = 'relations'),
                    mode TEXT NOT NULL CHECK (mode IN ('append')),
                    template_json TEXT NOT NULL,
                    state TEXT NOT NULL CHECK (
                        state IN ('pending','resolved','rejected','abandoned','removed')
                    ),
                    cause_operation_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(source_memory_id) REFERENCES memories(memory_id)
                );

                CREATE TABLE IF NOT EXISTS pending_memory_references (
                    reference_id TEXT PRIMARY KEY,
                    source_memory_id TEXT NOT NULL,
                    source_memory_type TEXT NOT NULL CHECK (
                        source_memory_type IN (
                            'fact','route','memo','claim','obligation','computation'
                        )
                    ),
                    field_path_json TEXT NOT NULL,
                    document_path_json TEXT,
                    expected_type TEXT NOT NULL CHECK (
                        expected_type IN (
                            'fact','route','memo','claim','obligation','fact_or_obligation'
                        )
                    ),
                    temporary_id TEXT NOT NULL,
                    state TEXT NOT NULL CHECK (
                        state IN ('pending','resolved','rejected','abandoned','removed')
                    ),
                    canonical_id TEXT,
                    document_id TEXT,
                    cause_operation_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(source_memory_id,field_path_json,temporary_id,cause_operation_id),
                    FOREIGN KEY(source_memory_id) REFERENCES memories(memory_id),
                    FOREIGN KEY(temporary_id) REFERENCES temporary_memory_ids(temporary_id),
                    FOREIGN KEY(canonical_id) REFERENCES memories(memory_id),
                    FOREIGN KEY(document_id) REFERENCES deferred_typed_updates(document_id)
                );
                CREATE INDEX IF NOT EXISTS pending_refs_by_target
                    ON pending_memory_references(temporary_id,state);
                CREATE INDEX IF NOT EXISTS pending_refs_by_source
                    ON pending_memory_references(source_memory_id,state);

                CREATE TABLE IF NOT EXISTS fact_dependencies (
                    predecessor_id TEXT NOT NULL,
                    fact_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(predecessor_id, fact_id),
                    FOREIGN KEY(predecessor_id) REFERENCES memories(memory_id),
                    FOREIGN KEY(fact_id) REFERENCES memories(memory_id),
                    CHECK(predecessor_id <> fact_id)
                );
                CREATE INDEX IF NOT EXISTS fact_dependencies_reverse
                    ON fact_dependencies(fact_id, predecessor_id);

                CREATE TABLE IF NOT EXISTS fact_external_references (
                    fact_id TEXT NOT NULL,
                    reference_key TEXT NOT NULL,
                    reference_index INTEGER NOT NULL,
                    reference_json TEXT NOT NULL,
                    PRIMARY KEY(fact_id, reference_index),
                    FOREIGN KEY(fact_id) REFERENCES memories(memory_id)
                );
                CREATE INDEX IF NOT EXISTS facts_by_external_reference
                    ON fact_external_references(reference_key, fact_id);

                CREATE TABLE IF NOT EXISTS fact_foundation_versions (
                    foundation_version TEXT NOT NULL,
                    fact_id TEXT NOT NULL UNIQUE,
                    PRIMARY KEY(foundation_version, fact_id),
                    FOREIGN KEY(fact_id) REFERENCES memories(memory_id)
                );

                CREATE TABLE IF NOT EXISTS obligation_predecessors (
                    obligation_id TEXT NOT NULL,
                    fact_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(obligation_id, fact_id),
                    FOREIGN KEY(obligation_id) REFERENCES memories(memory_id),
                    FOREIGN KEY(fact_id) REFERENCES memories(memory_id)
                );
                CREATE INDEX IF NOT EXISTS obligation_predecessors_by_fact
                    ON obligation_predecessors(fact_id, obligation_id);

                CREATE TABLE IF NOT EXISTS route_links (
                    link_id TEXT PRIMARY KEY,
                    route_id TEXT NOT NULL,
                    memory_id TEXT NOT NULL,
                    memory_type TEXT NOT NULL,
                    current INTEGER NOT NULL CHECK(current IN (0,1)),
                    created_at TEXT NOT NULL,
                    ended_at TEXT,
                    cause_id TEXT,
                    FOREIGN KEY(route_id) REFERENCES memories(memory_id),
                    FOREIGN KEY(memory_id) REFERENCES memories(memory_id)
                );
                CREATE UNIQUE INDEX IF NOT EXISTS one_current_route_link
                    ON route_links(route_id, memory_id, memory_type) WHERE current = 1;
                CREATE INDEX IF NOT EXISTS route_links_by_route
                    ON route_links(route_id, memory_type, current);
                CREATE INDEX IF NOT EXISTS route_links_by_memory
                    ON route_links(memory_id, memory_type, current);

                CREATE TABLE IF NOT EXISTS route_task_history (
                    route_id TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    recorded_at TEXT NOT NULL,
                    PRIMARY KEY(route_id, task_id),
                    FOREIGN KEY(route_id) REFERENCES memories(memory_id)
                );

                CREATE TABLE IF NOT EXISTS status_overlays (
                    overlay_seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    memory_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    root_cause_id TEXT,
                    dependency_path_json TEXT,
                    event_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(memory_id) REFERENCES memories(memory_id)
                );
                CREATE INDEX IF NOT EXISTS overlays_by_memory
                    ON status_overlays(memory_id, overlay_seq);

                CREATE TABLE IF NOT EXISTS revocations (
                    fact_id TEXT PRIMARY KEY,
                    root_fact_id TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    evidence_json TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    dependency_path_json TEXT NOT NULL,
                    event_id TEXT NOT NULL,
                    revoked_at TEXT NOT NULL,
                    FOREIGN KEY(fact_id) REFERENCES memories(memory_id)
                );

                CREATE TABLE IF NOT EXISTS root_resolution_state (
                    singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
                    root_obligation_id TEXT,
                    solution_fact_id TEXT,
                    outcome TEXT CHECK(outcome IS NULL OR outcome IN ('proved','disproved')),
                    status TEXT NOT NULL CHECK(status IN ('open','resolved','needs_attention')),
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS root_resolutions (
                    fact_id TEXT PRIMARY KEY,
                    outcome TEXT NOT NULL CHECK(outcome IN ('proved','disproved')),
                    is_primary INTEGER NOT NULL CHECK(is_primary IN (0,1)),
                    committed_at TEXT NOT NULL,
                    FOREIGN KEY(fact_id) REFERENCES memories(memory_id)
                );

                CREATE TABLE IF NOT EXISTS search_documents (
                    memory_id TEXT PRIMARY KEY,
                    memory_type TEXT NOT NULL,
                    title TEXT NOT NULL,
                    abstract TEXT NOT NULL,
                    search_text TEXT NOT NULL,
                    FOREIGN KEY(memory_id) REFERENCES memories(memory_id)
                );

                CREATE TABLE IF NOT EXISTS audit_events (
                    event_seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL UNIQUE,
                    event_type TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    subject_id TEXT,
                    details_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS read_audit (
                    audit_seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    action TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    policy_label TEXT NOT NULL,
                    subject_id TEXT,
                    query_json TEXT,
                    result_ids_json TEXT,
                    allowed INTEGER NOT NULL CHECK(allowed IN (0,1)),
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS control_state (
                    state_key TEXT PRIMARY KEY,
                    revision INTEGER NOT NULL CHECK(revision >= 1),
                    payload_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS control_events (
                    idempotency_key TEXT PRIMARY KEY,
                    input_hash TEXT NOT NULL,
                    event_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TRIGGER IF NOT EXISTS immutable_memory_identity
                BEFORE UPDATE OF memory_id, memory_type ON memories
                BEGIN
                    SELECT RAISE(ABORT, 'canonical memory identity is immutable');
                END;

                CREATE TRIGGER IF NOT EXISTS immutable_fact_core
                BEFORE UPDATE OF core_json ON memories
                WHEN OLD.memory_type = 'fact' AND NEW.core_json <> OLD.core_json
                BEGIN
                    SELECT RAISE(ABORT, 'published fact core is immutable');
                END;

                CREATE TRIGGER IF NOT EXISTS preserve_canonical_memory
                BEFORE DELETE ON memories
                BEGIN
                    SELECT RAISE(ABORT, 'canonical memory cannot be deleted');
                END;

                CREATE TRIGGER IF NOT EXISTS preserve_fact_edges
                BEFORE DELETE ON fact_dependencies
                BEGIN
                    SELECT RAISE(ABORT, 'historical fact edges cannot be deleted');
                END;
                """
            )
            pending_columns = {
                row["name"]
                for row in self._connection.execute(
                    "PRAGMA table_info(pending_memory_references)"
                ).fetchall()
            }
            if "document_path_json" not in pending_columns:
                self._connection.execute(
                    "ALTER TABLE pending_memory_references ADD COLUMN document_path_json TEXT"
                )
            current = self._connection.execute(
                "SELECT value FROM schema_meta WHERE key = 'schema_version'"
            ).fetchone()
            if current is None:
                self._connection.execute(
                    "INSERT INTO schema_meta(key, value) VALUES('schema_version', ?)",
                    (str(SCHEMA_VERSION),),
                )
            elif int(current["value"]) == 1 and SCHEMA_VERSION == 2:
                self._migrate_schema_v1_to_v2()
            elif int(current["value"]) != SCHEMA_VERSION:
                raise RuntimeError(
                    f"unsupported schema version {current['value']}; expected {SCHEMA_VERSION}"
                )

    def _migrate_schema_v1_to_v2(self) -> None:
        """Expand durable soft-reference tables without losing their history."""

        self._connection.execute("PRAGMA foreign_keys = OFF")
        try:
            self._connection.executescript(
                """
                BEGIN IMMEDIATE;

                CREATE TABLE temporary_memory_ids_v2 (
                    temporary_id TEXT PRIMARY KEY,
                    memory_type TEXT NOT NULL CHECK (
                        memory_type IN (
                            'fact','route','memo','claim','obligation','fact_or_obligation'
                        )
                    ),
                    state TEXT NOT NULL CHECK (
                        state IN ('pending','resolved','rejected','abandoned')
                    ),
                    canonical_id TEXT,
                    resolution TEXT,
                    target_operation_id TEXT,
                    reason TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(canonical_id) REFERENCES memories(memory_id)
                );
                INSERT INTO temporary_memory_ids_v2
                    SELECT * FROM temporary_memory_ids;

                CREATE TABLE deferred_typed_updates_v2 (
                    document_id TEXT PRIMARY KEY,
                    source_memory_id TEXT NOT NULL,
                    source_memory_type TEXT NOT NULL CHECK (
                        source_memory_type IN ('obligation')
                    ),
                    field_name TEXT NOT NULL CHECK (field_name = 'relations'),
                    mode TEXT NOT NULL CHECK (mode IN ('append')),
                    template_json TEXT NOT NULL,
                    state TEXT NOT NULL CHECK (
                        state IN ('pending','resolved','rejected','abandoned','removed')
                    ),
                    cause_operation_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(source_memory_id) REFERENCES memories(memory_id)
                );
                INSERT INTO deferred_typed_updates_v2
                    SELECT * FROM deferred_typed_updates;

                CREATE TABLE pending_memory_references_v2 (
                    reference_id TEXT PRIMARY KEY,
                    source_memory_id TEXT NOT NULL,
                    source_memory_type TEXT NOT NULL CHECK (
                        source_memory_type IN (
                            'fact','route','memo','claim','obligation','computation'
                        )
                    ),
                    field_path_json TEXT NOT NULL,
                    document_path_json TEXT,
                    expected_type TEXT NOT NULL CHECK (
                        expected_type IN (
                            'fact','route','memo','claim','obligation','fact_or_obligation'
                        )
                    ),
                    temporary_id TEXT NOT NULL,
                    state TEXT NOT NULL CHECK (
                        state IN ('pending','resolved','rejected','abandoned','removed')
                    ),
                    canonical_id TEXT,
                    document_id TEXT,
                    cause_operation_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(source_memory_id,field_path_json,temporary_id,cause_operation_id),
                    FOREIGN KEY(source_memory_id) REFERENCES memories(memory_id),
                    FOREIGN KEY(temporary_id) REFERENCES temporary_memory_ids(temporary_id),
                    FOREIGN KEY(canonical_id) REFERENCES memories(memory_id),
                    FOREIGN KEY(document_id) REFERENCES deferred_typed_updates(document_id)
                );
                INSERT INTO pending_memory_references_v2
                    SELECT reference_id,source_memory_id,source_memory_type,
                           field_path_json,document_path_json,expected_type,temporary_id,
                           state,canonical_id,document_id,cause_operation_id,created_at,updated_at
                    FROM pending_memory_references;

                DROP TABLE pending_memory_references;
                DROP TABLE deferred_typed_updates;
                DROP TABLE temporary_memory_ids;
                ALTER TABLE temporary_memory_ids_v2 RENAME TO temporary_memory_ids;
                ALTER TABLE deferred_typed_updates_v2 RENAME TO deferred_typed_updates;
                ALTER TABLE pending_memory_references_v2 RENAME TO pending_memory_references;

                CREATE INDEX pending_refs_by_target
                    ON pending_memory_references(temporary_id,state);
                CREATE INDEX pending_refs_by_source
                    ON pending_memory_references(source_memory_id,state);
                UPDATE schema_meta SET value='2' WHERE key='schema_version';
                COMMIT;
                """
            )
        finally:
            self._connection.execute("PRAGMA foreign_keys = ON")
        violation = self._connection.execute("PRAGMA foreign_key_check").fetchone()
        if violation is not None:
            raise RuntimeError("schema v2 migration produced a foreign-key violation")

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def __enter__(self) -> "MemoryStore":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()

    @contextmanager
    def transaction(self) -> Iterator["MemoryStore"]:
        """Open a write transaction, nesting through SQLite savepoints."""

        self._lock.acquire()
        savepoint: str | None = None
        try:
            if self._tx_depth == 0:
                self._connection.execute("BEGIN IMMEDIATE")
            else:
                self._savepoint_counter += 1
                savepoint = f"franta_sp_{self._savepoint_counter}"
                self._connection.execute(f"SAVEPOINT {savepoint}")
            self._tx_depth += 1
            try:
                yield self
            except BaseException:
                self._tx_depth -= 1
                if savepoint is None:
                    self._connection.execute("ROLLBACK")
                else:
                    self._connection.execute(f"ROLLBACK TO {savepoint}")
                    self._connection.execute(f"RELEASE {savepoint}")
                raise
            else:
                self._tx_depth -= 1
                if savepoint is None:
                    self._connection.execute("COMMIT")
                else:
                    self._connection.execute(f"RELEASE {savepoint}")
        finally:
            self._lock.release()

    @property
    def journal_mode(self) -> str:
        with self._lock:
            row = self._connection.execute("PRAGMA journal_mode").fetchone()
            return str(row[0]).lower()

    def _parse_memory_type(self, memory_type: MemoryType | str) -> MemoryType:
        try:
            return memory_type if isinstance(memory_type, MemoryType) else MemoryType(memory_type)
        except ValueError as exc:
            raise ValidationError(f"unknown canonical memory type: {memory_type}") from exc

    def _allocate_id_locked(self, memory_type: MemoryType | str) -> str:
        kind = self._parse_memory_type(memory_type)
        prefix = MEMORY_PREFIXES[kind]
        while True:
            memory_id = f"{prefix}-{uuid.uuid4().hex}"
            try:
                self._connection.execute(
                    "INSERT INTO allocated_ids(memory_id, memory_type, allocated_at) VALUES(?,?,?)",
                    (memory_id, kind.value, _utc_now()),
                )
            except sqlite3.IntegrityError:
                continue
            return memory_id

    def allocate_id(self, memory_type: MemoryType | str) -> str:
        if str(memory_type) == "category":
            return self.allocate_category_id()
        with self.transaction():
            return self._allocate_id_locked(memory_type)

    def allocate_category_id(self) -> str:
        with self.transaction():
            while True:
                memory_id = f"{CATEGORY_PREFIX}-{uuid.uuid4().hex}"
                try:
                    self._connection.execute(
                        "INSERT INTO allocated_ids(memory_id, memory_type, allocated_at) VALUES(?,?,?)",
                        (memory_id, "category", _utc_now()),
                    )
                except sqlite3.IntegrityError:
                    continue
                return memory_id

    def reserve_id(self, memory_type: MemoryType | str, memory_id: str) -> str:
        """Reserve a caller-supplied canonical ID after strict prefix checking."""

        kind = self._parse_memory_type(memory_type)
        prefix, separator, suffix = memory_id.partition("-")
        if separator != "-" or prefix != MEMORY_PREFIXES[kind] or not _ID_SUFFIX_RE.fullmatch(suffix):
            raise ValidationError(f"invalid {kind.value} ID: {memory_id}")
        with self.transaction():
            row = self._connection.execute(
                "SELECT memory_type FROM allocated_ids WHERE memory_id = ?", (memory_id,)
            ).fetchone()
            if row is not None:
                if row["memory_type"] != kind.value:
                    raise ValidationError(f"ID {memory_id} is reserved for {row['memory_type']}")
                return memory_id
            self._connection.execute(
                "INSERT INTO allocated_ids(memory_id, memory_type, allocated_at) VALUES(?,?,?)",
                (memory_id, kind.value, _utc_now()),
            )
        return memory_id

    def _canonical_id_for_add_locked(
        self, kind: MemoryType, payload: dict[str, Any]
    ) -> str:
        supplied = payload.pop("id", None)
        if supplied is None:
            return self._allocate_id_locked(kind)
        supplied = _require_text(supplied, "id")
        prefix, separator, suffix = supplied.partition("-")
        if separator != "-" or prefix != MEMORY_PREFIXES[kind] or not _ID_SUFFIX_RE.fullmatch(suffix):
            raise ValidationError(f"invalid {kind.value} ID: {supplied}")
        row = self._connection.execute(
            "SELECT memory_type FROM allocated_ids WHERE memory_id = ?", (supplied,)
        ).fetchone()
        if row is None:
            self._connection.execute(
                "INSERT INTO allocated_ids(memory_id, memory_type, allocated_at) VALUES(?,?,?)",
                (supplied, kind.value, _utc_now()),
            )
        elif row["memory_type"] != kind.value:
            raise ValidationError(f"ID {supplied} is reserved for {row['memory_type']}")
        if self._connection.execute(
            "SELECT 1 FROM memories WHERE memory_id = ?", (supplied,)
        ).fetchone():
            raise ValidationError(f"canonical ID already published: {supplied}")
        return supplied

    def _append_audit_locked(
        self,
        event_type: str,
        actor: str,
        *,
        subject_id: str | None = None,
        details: Mapping[str, Any] | None = None,
        event_id: str | None = None,
    ) -> str:
        event_id = event_id or f"EV-{uuid.uuid4().hex}"
        details_json = _canonical_json(dict(details or {}))
        existing = self._connection.execute(
            "SELECT event_type, actor, subject_id, details_json FROM audit_events WHERE event_id = ?",
            (event_id,),
        ).fetchone()
        if existing is not None:
            expected = (event_type, actor, subject_id, details_json)
            actual = (
                existing["event_type"],
                existing["actor"],
                existing["subject_id"],
                existing["details_json"],
            )
            if actual != expected:
                raise IdempotencyConflict(f"audit event {event_id} was replayed differently")
            return event_id
        self._connection.execute(
            """INSERT INTO audit_events(
                   event_id,event_type,actor,subject_id,details_json,created_at
               ) VALUES(?,?,?,?,?,?)""",
            (event_id, event_type, actor, subject_id, details_json, _utc_now()),
        )
        return event_id

    def audit_event(
        self,
        event_type: str,
        actor: str,
        *,
        subject_id: str | None = None,
        details: Mapping[str, Any] | None = None,
        event_id: str | None = None,
    ) -> str:
        with self.transaction():
            return self._append_audit_locked(
                _require_text(event_type, "event_type"),
                _require_text(actor, "actor"),
                subject_id=subject_id,
                details=details,
                event_id=event_id,
            )

    def list_audit_events(self, *, after_seq: int = 0) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM audit_events WHERE event_seq > ? ORDER BY event_seq", (after_seq,)
            ).fetchall()
        return [
            {
                "event_seq": row["event_seq"],
                "event_id": row["event_id"],
                "event_type": row["event_type"],
                "actor": row["actor"],
                "subject_id": row["subject_id"],
                "details": _loads(row["details_json"], {}),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    # ------------------------------------------------------------------
    # Validation and normalized record preparation

    def _allocated_type_locked(self, memory_id: str) -> str | None:
        row = self._connection.execute(
            "SELECT memory_type FROM allocated_ids WHERE memory_id = ?", (memory_id,)
        ).fetchone()
        return None if row is None else str(row["memory_type"])

    def _require_memory_locked(
        self,
        memory_id: str,
        expected: MemoryType | Iterable[MemoryType],
        *,
        active: bool = False,
        pending_types: Mapping[str, MemoryType] | None = None,
    ) -> MemoryType:
        expected_set = {expected} if isinstance(expected, MemoryType) else set(expected)
        if pending_types and memory_id in pending_types:
            kind = pending_types[memory_id]
            if kind not in expected_set:
                names = ", ".join(sorted(item.value for item in expected_set))
                raise ValidationError(f"{memory_id} must refer to one of: {names}")
            return kind
        row = self._connection.execute(
            "SELECT memory_type, active FROM memories WHERE memory_id = ?", (memory_id,)
        ).fetchone()
        if row is None:
            raise ValidationError(f"referenced canonical memory does not exist: {memory_id}")
        kind = MemoryType(row["memory_type"])
        if kind not in expected_set:
            names = ", ".join(sorted(item.value for item in expected_set))
            raise ValidationError(f"{memory_id} must refer to one of: {names}")
        if active and not bool(row["active"]):
            raise ValidationError(f"referenced {kind.value} is inactive: {memory_id}")
        return kind

    def _require_allocated_task_locked(self, task_id: str) -> None:
        if self._allocated_type_locked(task_id) != MemoryType.TASK.value:
            raise ValidationError(f"task ID was not scheduler-allocated: {task_id}")

    @staticmethod
    def _looks_canonical_id(memory_id: str) -> bool:
        return memory_id.startswith(("F-", "R-", "M-", "CL-", "O-", "T-", "C-"))

    def _register_temporary_target_locked(
        self,
        temporary_id: str,
        expected_type: MemoryType | Iterable[MemoryType] | str,
        *,
        operation_id: str | None = None,
    ) -> None:
        expected_token = _reference_type_token(expected_type)
        expected_types = _reference_type_set(expected_token)
        temporary_id = _strip_unpublished_suffix(
            _require_text(temporary_id, "temporary_id")
        )
        if not temporary_id:
            raise ValidationError("temporary_id must not be an unpublished marker alone")
        row = self._connection.execute(
            "SELECT * FROM temporary_memory_ids WHERE temporary_id=?", (temporary_id,)
        ).fetchone()
        now = _utc_now()
        if row is None:
            self._connection.execute(
                """INSERT INTO temporary_memory_ids(
                       temporary_id,memory_type,state,target_operation_id,created_at,updated_at
                   ) VALUES(?,?,'pending',?,?,?)""",
                (temporary_id, expected_token, operation_id, now, now),
            )
            return
        existing_types = _reference_type_set(str(row["memory_type"]))
        intersection = existing_types & expected_types
        if not intersection:
            raise ValidationError(
                f"temporary ID {temporary_id} is reserved for {row['memory_type']}, "
                f"not {expected_token}"
            )
        narrowed_token = _reference_type_token(intersection)
        if narrowed_token != row["memory_type"]:
            self._connection.execute(
                "UPDATE temporary_memory_ids SET memory_type=?,updated_at=? WHERE temporary_id=?",
                (narrowed_token, now, temporary_id),
            )
        if row["state"] == "resolved":
            return
        if row["state"] in {"rejected", "abandoned"}:
            # Exact proposal IDs are terminal.  A mathematical correction must
            # use a fresh proposal ID, so registering a new target operation
            # may never revive references attached to the old identity.
            if operation_id:
                raise ValidationError(
                    f"temporary ID is terminal ({row['state']}): {temporary_id}"
                )
            return
        if operation_id:
            self._connection.execute(
                """UPDATE temporary_memory_ids
                   SET target_operation_id=?,updated_at=? WHERE temporary_id=?""",
                (operation_id, now, temporary_id),
            )

    def _typed_reference_resolution_locked(
        self,
        reference_id: str,
        expected_type: MemoryType | Iterable[MemoryType] | str,
        *,
        pending_types: Mapping[str, MemoryType] | None = None,
        require_active: bool | None = None,
    ) -> tuple[str | None, str | None, str]:
        """Return ``(canonical_id, temporary_id, state)`` for one soft reference.

        A malformed, terminal, or type-incompatible soft target is retained as
        a durable rejected overlay.  It never makes publication of the source
        memory fail.
        """

        reference_id = _strip_unpublished_suffix(
            _require_text(reference_id, "typed reference")
        )
        if not reference_id:
            raise ValidationError("typed reference must not be an unpublished marker alone")
        expected_token = _reference_type_token(expected_type)
        expected_types = _reference_type_set(expected_token)

        def active_is_required(actual: MemoryType) -> bool:
            if require_active is not None:
                return require_active
            return _reference_requires_active(actual)

        def unresolved(state: str = "pending") -> tuple[None, str, str]:
            try:
                self._register_temporary_target_locked(reference_id, expected_token)
            except ValidationError:
                # A conflicting reservation belongs to the target identity,
                # not to the source.  The per-reference row records rejection.
                state = "rejected"
            return None, reference_id, state

        if pending_types and reference_id in pending_types:
            actual = pending_types[reference_id]
            if actual not in expected_types:
                return unresolved("rejected")
            return reference_id, None, "resolved"
        row = self._connection.execute(
            "SELECT memory_type,active FROM memories WHERE memory_id=?", (reference_id,)
        ).fetchone()
        if row is not None:
            actual = MemoryType(row["memory_type"])
            if actual not in expected_types or (
                active_is_required(actual) and not bool(row["active"])
            ):
                return unresolved("rejected")
            return reference_id, None, "resolved"
        mapping = self._connection.execute(
            "SELECT canonical_id FROM proposal_mappings WHERE proposal_id=?", (reference_id,)
        ).fetchone()
        if mapping is not None:
            canonical_id = str(mapping["canonical_id"])
            canonical = self._connection.execute(
                "SELECT memory_type,active FROM memories WHERE memory_id=?", (canonical_id,)
            ).fetchone()
            if canonical is None:
                return unresolved("rejected")
            actual = MemoryType(canonical["memory_type"])
            if actual not in expected_types or (
                active_is_required(actual) and not bool(canonical["active"])
            ):
                return unresolved("rejected")
            return canonical_id, None, "resolved"
        temporary = self._connection.execute(
            "SELECT * FROM temporary_memory_ids WHERE temporary_id=?", (reference_id,)
        ).fetchone()
        if temporary is not None:
            if not (_reference_type_set(str(temporary["memory_type"])) & expected_types):
                return None, reference_id, "rejected"
            if temporary["state"] == "resolved":
                canonical_id = str(temporary["canonical_id"])
                canonical = self._connection.execute(
                    "SELECT memory_type,active FROM memories WHERE memory_id=?", (canonical_id,)
                ).fetchone()
                if canonical is None:
                    return None, reference_id, "rejected"
                actual = MemoryType(canonical["memory_type"])
                if actual not in expected_types or (
                    active_is_required(actual) and not bool(canonical["active"])
                ):
                    return None, reference_id, "rejected"
                return canonical_id, None, "resolved"
            return None, reference_id, str(temporary["state"])
        self._register_temporary_target_locked(reference_id, expected_type)
        return None, reference_id, "pending"

    def _assert_no_temporary_ids_in_prose_locked(
        self,
        payload: Mapping[str, Any],
        temporary_ids: Iterable[str],
        *,
        typed_top_fields: set[str],
    ) -> None:
        identifiers = tuple(sorted(set(temporary_ids)))
        if not identifiers:
            return

        def inspect(value: Any, path: tuple[str, ...]) -> None:
            if isinstance(value, str):
                for temporary_id in identifiers:
                    if contains_identifier_token(value, temporary_id):
                        raise ValidationError(
                            "temporary IDs may occur only in typed relationship fields; "
                            f"found {temporary_id} in {'.'.join(path)}"
                        )
            elif isinstance(value, Mapping):
                for key, item in value.items():
                    inspect(item, (*path, str(key)))
            elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
                for index, item in enumerate(value):
                    inspect(item, (*path, str(index)))

        for key, value in payload.items():
            if key not in typed_top_fields:
                inspect(value, (str(key),))

    def _normalize_nonfact_add_references_locked(
        self,
        kind: MemoryType,
        payload: Mapping[str, Any],
        pending_types: Mapping[str, MemoryType] | None = None,
    ) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
        """Separate active canonical links from durable pending references."""

        normalized = copy.deepcopy(dict(payload))
        simple_fields: dict[str, MemoryType | Iterable[MemoryType] | str]
        if kind is MemoryType.FACT:
            simple_fields = {"related_route_ids": MemoryType.ROUTE}
        elif kind is MemoryType.ROUTE:
            simple_fields = {
                "related_obligation_ids": MemoryType.OBLIGATION,
                "active_fact_ids": MemoryType.FACT,
                "relevant_memo_ids": MemoryType.MEMO,
                "relevant_claim_ids": MemoryType.CLAIM,
            }
        elif kind in {MemoryType.MEMO, MemoryType.CLAIM}:
            simple_fields = {"related_route_ids": MemoryType.ROUTE}
        elif kind is MemoryType.OBLIGATION:
            simple_fields = {
                "predecessor_fact_ids": MemoryType.FACT,
                "related_route_ids": MemoryType.ROUTE,
            }
        elif kind is MemoryType.COMPUTATION:
            simple_fields = {}
        else:
            return normalized, [], []

        pending: list[dict[str, Any]] = []
        deferred: list[dict[str, Any]] = []
        observed_temporary_ids: set[str] = {
            str(item["temporary_id"])
            for item in self._connection.execute(
                "SELECT temporary_id FROM temporary_memory_ids"
            ).fetchall()
        }
        if kind is MemoryType.COMPUTATION:
            related_raw = _require_mapping(
                normalized.get("related_memory_ids", {}), "related_memory_ids"
            )
            expected_groups = {
                "fact",
                "route",
                "memo",
                "claim",
                "obligation",
            }
            unknown_groups = set(related_raw) - expected_groups
            if unknown_groups:
                raise ValidationError(
                    f"unknown related_memory_ids groups: {sorted(unknown_groups)}"
                )
            normalized_related: dict[str, list[str]] = {}
            for group in sorted(expected_groups):
                expected_type = MemoryType(group)
                ids = _id_list(
                    related_raw.get(group, []), f"related_memory_ids.{group}"
                )
                canonical_ids: list[str] = []
                for index, item in enumerate(ids):
                    canonical_id, temporary_id, reference_state = (
                        self._typed_reference_resolution_locked(
                            item,
                            expected_type,
                            pending_types=pending_types,
                            require_active=False,
                        )
                    )
                    if canonical_id is not None:
                        if canonical_id not in canonical_ids:
                            canonical_ids.append(canonical_id)
                    else:
                        assert temporary_id is not None
                        observed_temporary_ids.add(temporary_id)
                        pending.append(
                            {
                                "temporary_id": temporary_id,
                                "expected_type": expected_type,
                                "field_path": [
                                    "related_memory_ids",
                                    group,
                                    index,
                                ],
                                "document_path": None,
                                "document_key": None,
                                "state": reference_state,
                            }
                        )
                normalized_related[group] = canonical_ids
            normalized["related_memory_ids"] = normalized_related
            return normalized, pending, deferred

        for field, expected_type in simple_fields.items():
            ids = _id_list(normalized.get(field, []), field)
            active_ids: list[str] = []
            for index, item in enumerate(ids):
                canonical_id, temporary_id, reference_state = (
                    self._typed_reference_resolution_locked(
                    item, expected_type, pending_types=pending_types
                )
                )
                if canonical_id is not None:
                    if canonical_id not in active_ids:
                        active_ids.append(canonical_id)
                else:
                    assert temporary_id is not None
                    observed_temporary_ids.add(temporary_id)
                    pending.append(
                        {
                            "temporary_id": temporary_id,
                            "expected_type": expected_type,
                            "field_path": [field, index],
                            "document_path": None,
                            "document_key": None,
                            "state": reference_state,
                        }
                    )
            normalized[field] = active_ids

        if kind is MemoryType.OBLIGATION:
            raw_relations = normalized.get("relations", [])
            if not isinstance(raw_relations, Sequence) or isinstance(
                raw_relations, (str, bytes, bytearray)
            ):
                raise ValidationError("relations must be a list")
            active_relations: list[dict[str, Any]] = []
            for relation_index, raw_relation in enumerate(raw_relations):
                relation = _require_mapping(raw_relation, f"relations[{relation_index}]")
                allowed_relation_fields = {
                    "relation_type",
                    "premise_memory_ids",
                    "conclusion",
                    "explanation",
                    "supporting_fact_ids",
                }
                unknown_relation_fields = set(relation) - allowed_relation_fields
                if unknown_relation_fields:
                    raise ValidationError(
                        f"relations[{relation_index}] has unknown fields: "
                        f"{sorted(unknown_relation_fields)}"
                    )
                _require_text(
                    relation.get("relation_type"),
                    f"relations[{relation_index}].relation_type",
                )
                _require_text(
                    relation.get("explanation"),
                    f"relations[{relation_index}].explanation",
                )
                supporting = _id_list(
                    relation.get("supporting_fact_ids", []),
                    f"relations[{relation_index}].supporting_fact_ids",
                )
                relation_pending: list[dict[str, Any]] = []
                normalized_supporting: list[str] = []
                for index, fact_id in enumerate(supporting):
                    canonical_id, temporary_id, reference_state = (
                        self._typed_reference_resolution_locked(
                            fact_id,
                            MemoryType.FACT,
                            pending_types=pending_types,
                        )
                    )
                    if canonical_id is not None:
                        if canonical_id not in normalized_supporting:
                            normalized_supporting.append(canonical_id)
                    else:
                        assert temporary_id is not None
                        observed_temporary_ids.add(temporary_id)
                        relation_pending.append(
                            {
                                "temporary_id": temporary_id,
                                "expected_type": MemoryType.FACT,
                                "field_path": [
                                    "relations",
                                    relation_index,
                                    "supporting_fact_ids",
                                    index,
                                ],
                                "document_path": ["supporting_fact_ids", index],
                                "state": reference_state,
                            }
                        )
                        normalized_supporting.append(temporary_id)
                relation["supporting_fact_ids"] = normalized_supporting
                premises = _id_list(
                    relation.get("premise_memory_ids", []),
                    f"relations[{relation_index}].premise_memory_ids",
                )
                if not premises:
                    raise ValidationError(
                        f"relations[{relation_index}] must have at least one premise"
                    )
                normalized_premises: list[str] = []
                for index, item in enumerate(premises):
                    canonical_id, temporary_id, reference_state = (
                        self._typed_reference_resolution_locked(
                            item,
                            {MemoryType.FACT, MemoryType.OBLIGATION},
                            pending_types=pending_types,
                        )
                    )
                    if canonical_id is not None:
                        if canonical_id not in normalized_premises:
                            normalized_premises.append(canonical_id)
                    else:
                        assert temporary_id is not None
                        observed_temporary_ids.add(temporary_id)
                        relation_pending.append(
                            {
                                "temporary_id": temporary_id,
                                "expected_type": _FACT_OR_OBLIGATION,
                                "field_path": [
                                    "relations",
                                    relation_index,
                                    "premise_memory_ids",
                                    index,
                                ],
                                "document_path": ["premise_memory_ids", index],
                                "state": reference_state,
                            }
                        )
                        normalized_premises.append(temporary_id)
                relation["premise_memory_ids"] = normalized_premises
                conclusion = relation.get("conclusion")
                if conclusion != "ROOT":
                    conclusion = _require_text(
                        conclusion, f"relations[{relation_index}].conclusion"
                    )
                    canonical_id, temporary_id, reference_state = (
                        self._typed_reference_resolution_locked(
                            conclusion,
                            MemoryType.OBLIGATION,
                            pending_types=pending_types,
                        )
                    )
                    if canonical_id is not None:
                        relation["conclusion"] = canonical_id
                    else:
                        assert temporary_id is not None
                        observed_temporary_ids.add(temporary_id)
                        relation_pending.append(
                            {
                                "temporary_id": temporary_id,
                                "expected_type": MemoryType.OBLIGATION,
                                "field_path": ["relations", relation_index, "conclusion"],
                                "document_path": ["conclusion"],
                                "state": reference_state,
                            }
                        )
                self._assert_no_temporary_ids_in_prose_locked(
                    relation,
                    observed_temporary_ids,
                    typed_top_fields={
                        "premise_memory_ids",
                        "supporting_fact_ids",
                        "conclusion",
                    },
                )
                if relation_pending:
                    document_key = f"relation:{relation_index}"
                    for item in relation_pending:
                        item["document_key"] = document_key
                    pending.extend(relation_pending)
                    deferred.append(
                        {
                            "document_key": document_key,
                            "field_name": "relations",
                            "mode": "append",
                            "template": relation,
                        }
                    )
                else:
                    active_relations.append(relation)
            normalized["relations"] = active_relations

        typed_fields = set(simple_fields)
        if kind is MemoryType.OBLIGATION:
            typed_fields.add("relations")
        if kind is not MemoryType.FACT:
            self._assert_no_temporary_ids_in_prose_locked(
                normalized,
                observed_temporary_ids,
                typed_top_fields=typed_fields,
            )
        return normalized, pending, deferred

    def _validate_notation(self, value: Any) -> list[dict[str, str]]:
        if value is None:
            return []
        if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
            raise ValidationError("introduced_notation must be a list")
        result: list[dict[str, str]] = []
        for index, raw in enumerate(value):
            item = _require_mapping(raw, f"introduced_notation[{index}]")
            unknown = set(item) - {"symbol", "definition", "scope"}
            if unknown:
                raise ValidationError(
                    f"introduced_notation[{index}] has unknown fields: {sorted(unknown)}"
                )
            result.append(
                {
                    "symbol": _require_text(item.get("symbol"), f"introduced_notation[{index}].symbol"),
                    "definition": _require_text(
                        item.get("definition"), f"introduced_notation[{index}].definition"
                    ),
                    "scope": _require_text(item.get("scope"), f"introduced_notation[{index}].scope"),
                }
            )
        return result

    def _validate_external_references(self, value: Any) -> list[dict[str, Any]]:
        if value is None:
            return []
        if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
            raise ValidationError("external_references must be a list")
        required = {
            "source_type",
            "authors",
            "title",
            "stable_identifier_or_url",
            "locator",
            "exact_result",
            "role",
        }
        result: list[dict[str, Any]] = []
        for index, raw in enumerate(value):
            item = _require_mapping(raw, f"external_references[{index}]")
            missing = required - set(item)
            unknown = set(item) - required
            if missing or unknown:
                raise ValidationError(
                    f"external_references[{index}] fields: missing={sorted(missing)}, "
                    f"unknown={sorted(unknown)}"
                )
            authors_value = item["authors"]
            if isinstance(authors_value, str):
                authors: str | list[str] = _require_text(
                    authors_value, f"external_references[{index}].authors"
                )
            else:
                authors = _text_list(
                    authors_value,
                    f"external_references[{index}].authors",
                    allow_empty=False,
                )
            normalized: dict[str, Any] = {"authors": authors}
            for field in required - {"authors"}:
                normalized[field] = _require_text(
                    item[field], f"external_references[{index}].{field}"
                )
            result.append(normalized)
        return result

    def _validate_value_assessment(self, value: Any) -> dict[str, str]:
        item = _require_mapping(value, "value_assessment")
        if set(item) != set(_VALUE_KEYS):
            raise ValidationError(
                f"value_assessment must contain exactly {list(_VALUE_KEYS)}"
            )
        result: dict[str, str] = {}
        for key in _VALUE_KEYS:
            if isinstance(item[key], (int, float, bool)):
                raise ValidationError(f"value_assessment.{key} must be qualitative, not numeric")
            result[key] = _require_text(item[key], f"value_assessment.{key}")
        return result

    def _validate_relations_locked(
        self,
        value: Any,
        *,
        pending_types: Mapping[str, MemoryType] | None = None,
    ) -> list[dict[str, Any]]:
        if value is None:
            return []
        if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
            raise ValidationError("relations must be a list")
        result: list[dict[str, Any]] = []
        for index, raw in enumerate(value):
            item = _require_mapping(raw, f"relations[{index}]")
            allowed = {
                "relation_type",
                "premise_memory_ids",
                "conclusion",
                "explanation",
                "supporting_fact_ids",
            }
            unknown = set(item) - allowed
            if unknown:
                raise ValidationError(f"relations[{index}] has unknown fields: {sorted(unknown)}")
            relation_type = _require_text(
                item.get("relation_type"), f"relations[{index}].relation_type"
            )
            premises = _id_list(
                item.get("premise_memory_ids", []),
                f"relations[{index}].premise_memory_ids",
            )
            if not premises:
                raise ValidationError(f"relations[{index}] must have at least one premise")
            for premise_id in premises:
                premise_type = self._require_memory_locked(
                    premise_id,
                    {MemoryType.OBLIGATION, MemoryType.FACT},
                    pending_types=pending_types,
                )
                if premise_type is MemoryType.FACT:
                    self._require_memory_locked(
                        premise_id,
                        MemoryType.FACT,
                        active=True,
                        pending_types=pending_types,
                    )
            conclusion = _require_text(item.get("conclusion"), f"relations[{index}].conclusion")
            if conclusion != "ROOT":
                self._require_memory_locked(
                    conclusion,
                    MemoryType.OBLIGATION,
                    pending_types=pending_types,
                )
            supporting = _id_list(
                item.get("supporting_fact_ids", []),
                f"relations[{index}].supporting_fact_ids",
            )
            for fact_id in supporting:
                self._require_memory_locked(
                    fact_id,
                    MemoryType.FACT,
                    active=True,
                    pending_types=pending_types,
                )
            result.append(
                {
                    "relation_type": relation_type,
                    "premise_memory_ids": premises,
                    "conclusion": conclusion,
                    "explanation": _require_text(
                        item.get("explanation"), f"relations[{index}].explanation"
                    ),
                    "supporting_fact_ids": supporting,
                }
            )
        return result

    def _reject_unknown(self, payload: Mapping[str, Any], allowed: set[str], label: str) -> None:
        unknown = set(payload) - allowed
        if unknown:
            raise ValidationError(f"{label} has unknown fields: {sorted(unknown)}")

    def _prepare_fact_locked(
        self, memory_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        allowed = {
            "statement",
            "proof",
            "predecessor_fact_ids",
            "originating_task_id",
            "foundation_policy_version",
            "introduced_notation",
            "external_references",
            "root_resolution",
            "abstract",
            "keywords",
            "related_route_ids",
        }
        self._reject_unknown(payload, allowed, "fact")
        statement = _require_text(payload.get("statement"), "statement")
        proof = _require_text(payload.get("proof"), "proof")
        predecessors = _id_list(payload.get("predecessor_fact_ids", []), "predecessor_fact_ids")
        for predecessor_id in predecessors:
            self._require_memory_locked(predecessor_id, MemoryType.FACT, active=True)
            if predecessor_id == memory_id:
                raise ValidationError("a fact cannot depend on itself")
        task_id = _require_text(payload.get("originating_task_id"), "originating_task_id")
        self._require_allocated_task_locked(task_id)
        foundation_version = payload.get("foundation_policy_version")
        if not isinstance(foundation_version, (str, int)) or isinstance(foundation_version, bool):
            raise ValidationError("foundation_policy_version must be a string or integer")
        if isinstance(foundation_version, str) and not foundation_version.strip():
            raise ValidationError("foundation_policy_version must not be empty")
        root_resolution = payload.get("root_resolution")
        if root_resolution is not None:
            root_resolution = _require_mapping(root_resolution, "root_resolution")
            if set(root_resolution) != {"target", "outcome"}:
                raise ValidationError("root_resolution requires exactly target and outcome")
            if root_resolution["target"] != "ROOT":
                raise ValidationError("root_resolution.target must be ROOT")
            if root_resolution["outcome"] not in {"proved", "disproved"}:
                raise ValidationError("root_resolution.outcome must be proved or disproved")
            root_row = self._connection.execute(
                "SELECT root_obligation_id FROM root_resolution_state WHERE singleton = 1"
            ).fetchone()
            if root_row is None or root_row["root_obligation_id"] is None:
                raise ValidationError("root obligation must be configured before root resolution")
        related_routes = _id_list(payload.get("related_route_ids", []), "related_route_ids")
        for route_id in related_routes:
            self._require_memory_locked(route_id, MemoryType.ROUTE)
        keywords = _text_list(payload.get("keywords", []), "keywords")
        abstract = _require_text(payload.get("abstract"), "abstract")
        core = {
            "statement": statement,
            "proof": proof,
            "predecessor_fact_ids": predecessors,
            "originating_task_id": task_id,
            "foundation_policy_version": foundation_version,
            "introduced_notation": self._validate_notation(payload.get("introduced_notation", [])),
            "external_references": self._validate_external_references(
                payload.get("external_references", [])
            ),
            "root_resolution": root_resolution,
        }
        metadata = {"keywords": keywords}
        links = [(route_id, memory_id, MemoryType.FACT) for route_id in related_routes]
        title = statement.splitlines()[0][:160]
        return {
            "kind": MemoryType.FACT,
            "id": memory_id,
            "abstract": abstract,
            "core": core,
            "metadata": metadata,
            "links": links,
            "fact_dependencies": predecessors,
            "external_references": core["external_references"],
            "foundation_policy_version": foundation_version,
            "obligation_predecessors": [],
            "title": title,
            "search_text": " ".join([abstract, abstract, title, *keywords]),
        }

    def _prepare_route_locked(
        self,
        memory_id: str,
        payload: dict[str, Any],
        pending_types: Mapping[str, MemoryType] | None = None,
    ) -> dict[str, Any]:
        allowed = {
            "abstract",
            "strategy_description",
            "value_assessment",
            "progress",
            "related_obligation_ids",
            "next_steps",
            "obstacles",
            "active_fact_ids",
            "relevant_memo_ids",
            "relevant_claim_ids",
        }
        self._reject_unknown(payload, allowed, "route")
        abstract = _require_text(payload.get("abstract"), "abstract")
        strategy = _require_text(payload.get("strategy_description"), "strategy_description")
        metadata = {
            "value_assessment": self._validate_value_assessment(payload.get("value_assessment")),
            "progress": _text_list(payload.get("progress", []), "progress"),
            "next_steps": _text_list(payload.get("next_steps", []), "next_steps"),
            "obstacles": _text_list(payload.get("obstacles", []), "obstacles"),
        }
        typed = {
            "related_obligation_ids": (
                _id_list(payload.get("related_obligation_ids", []), "related_obligation_ids"),
                MemoryType.OBLIGATION,
                True,
            ),
            "active_fact_ids": (
                _id_list(payload.get("active_fact_ids", []), "active_fact_ids"),
                MemoryType.FACT,
                True,
            ),
            "relevant_memo_ids": (
                _id_list(payload.get("relevant_memo_ids", []), "relevant_memo_ids"),
                MemoryType.MEMO,
                False,
            ),
            "relevant_claim_ids": (
                _id_list(payload.get("relevant_claim_ids", []), "relevant_claim_ids"),
                MemoryType.CLAIM,
                True,
            ),
        }
        links: list[tuple[str, str, MemoryType]] = []
        for _, (ids, target_type, active) in typed.items():
            for target_id in ids:
                self._require_memory_locked(
                    target_id,
                    target_type,
                    active=active,
                    pending_types=pending_types,
                )
                links.append((memory_id, target_id, target_type))
        return {
            "kind": MemoryType.ROUTE,
            "id": memory_id,
            "abstract": abstract,
            "core": {"strategy_description": strategy},
            "metadata": metadata,
            "links": links,
            "fact_dependencies": [],
            "obligation_predecessors": [],
            "title": abstract.splitlines()[0][:160],
            "search_text": f"{abstract} {abstract}",
        }

    def _prepare_memo_or_claim_locked(
        self,
        kind: MemoryType,
        memory_id: str,
        payload: dict[str, Any],
        pending_types: Mapping[str, MemoryType] | None = None,
    ) -> dict[str, Any]:
        allowed = {"abstract", "content", "related_route_ids"}
        if kind is MemoryType.MEMO:
            allowed.add("genre")
        self._reject_unknown(payload, allowed, kind.value)
        abstract = _require_text(payload.get("abstract"), "abstract")
        core: dict[str, Any] = {"content": _require_text(payload.get("content"), "content")}
        if kind is MemoryType.MEMO:
            genre = payload.get("genre")
            if genre not in {"high-level", "normal"}:
                raise ValidationError("memo.genre must be high-level or normal")
            core["genre"] = genre
        routes = _id_list(payload.get("related_route_ids", []), "related_route_ids")
        links: list[tuple[str, str, MemoryType]] = []
        for route_id in routes:
            self._require_memory_locked(
                route_id,
                MemoryType.ROUTE,
                pending_types=pending_types,
            )
            links.append((route_id, memory_id, kind))
        return {
            "kind": kind,
            "id": memory_id,
            "abstract": abstract,
            "core": core,
            "metadata": {},
            "links": links,
            "fact_dependencies": [],
            "obligation_predecessors": [],
            "title": abstract.splitlines()[0][:160],
            "search_text": f"{abstract} {abstract}",
        }

    def _prepare_obligation_locked(
        self,
        memory_id: str,
        payload: dict[str, Any],
        pending_types: Mapping[str, MemoryType] | None = None,
    ) -> dict[str, Any]:
        allowed = {
            "abstract",
            "statement",
            "importance",
            "predecessor_fact_ids",
            "partial_progress",
            "related_route_ids",
            "relations",
        }
        self._reject_unknown(payload, allowed, "obligation")
        abstract = _require_text(payload.get("abstract"), "abstract")
        statement = _require_text(payload.get("statement"), "statement")
        predecessors = _id_list(payload.get("predecessor_fact_ids", []), "predecessor_fact_ids")
        for fact_id in predecessors:
            self._require_memory_locked(fact_id, MemoryType.FACT, active=True)
        routes = _id_list(payload.get("related_route_ids", []), "related_route_ids")
        links: list[tuple[str, str, MemoryType]] = []
        for route_id in routes:
            self._require_memory_locked(
                route_id,
                MemoryType.ROUTE,
                pending_types=pending_types,
            )
            links.append((route_id, memory_id, MemoryType.OBLIGATION))
        metadata = {
            "importance": _require_text(payload.get("importance"), "importance"),
            "partial_progress": _text_list(payload.get("partial_progress", []), "partial_progress"),
            "relations": self._validate_relations_locked(
                payload.get("relations", []), pending_types=pending_types
            ),
        }
        return {
            "kind": MemoryType.OBLIGATION,
            "id": memory_id,
            "abstract": abstract,
            "core": {"statement": statement, "predecessor_fact_ids": predecessors},
            "metadata": metadata,
            "links": links,
            "fact_dependencies": [],
            "obligation_predecessors": predecessors,
            "title": statement.splitlines()[0][:160],
            "search_text": f"{abstract} {abstract} {statement.splitlines()[0]}",
        }

    def _prepare_task_locked(self, memory_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        allowed = {
            "assign_record",
            "final_status",
            "final_summary",
            "artifact_references",
            "computation_ids",
        }
        self._reject_unknown(payload, allowed, "task")
        assign_record = _require_mapping(payload.get("assign_record"), "assign_record")
        assigned_id = assign_record.get("task_id")
        if assigned_id is not None and assigned_id != memory_id:
            raise ValidationError("assign_record.task_id must match the canonical task ID")
        if assigned_id is None:
            assign_record["task_id"] = memory_id
        final_status = payload.get("final_status")
        if final_status not in {"finished", "progress", "failed", "interrupted"}:
            raise ValidationError("invalid final_status")
        final_summary = _require_text(payload.get("final_summary"), "final_summary")
        artifact_refs_raw = payload.get("artifact_references", [])
        if not isinstance(artifact_refs_raw, Sequence) or isinstance(
            artifact_refs_raw, (str, bytes, bytearray)
        ):
            raise ValidationError("artifact_references must be a list")
        artifact_refs: list[dict[str, str]] = []
        for index, raw in enumerate(artifact_refs_raw):
            item = _require_mapping(raw, f"artifact_references[{index}]")
            if set(item) != {"kind", "path", "sha256"}:
                raise ValidationError(
                    f"artifact_references[{index}] requires exactly kind, path, sha256"
                )
            checksum = _require_text(item["sha256"], f"artifact_references[{index}].sha256")
            if not re.fullmatch(r"[0-9a-fA-F]{64}", checksum):
                raise ValidationError(f"artifact_references[{index}].sha256 is invalid")
            artifact_refs.append(
                {
                    "kind": _require_text(item["kind"], f"artifact_references[{index}].kind"),
                    "path": _require_text(item["path"], f"artifact_references[{index}].path"),
                    "sha256": checksum.lower(),
                }
            )
        computation_ids = _id_list(payload.get("computation_ids", []), "computation_ids")
        for computation_id in computation_ids:
            self._require_memory_locked(computation_id, MemoryType.COMPUTATION)
        core = {
            "assign_record": assign_record,
            "final_status": final_status,
            "final_summary": final_summary,
            "artifact_references": artifact_refs,
            "computation_ids": computation_ids,
        }
        return {
            "kind": MemoryType.TASK,
            "id": memory_id,
            "abstract": final_summary,
            "core": core,
            "metadata": {},
            "links": [],
            "fact_dependencies": [],
            "obligation_predecessors": [],
            "title": final_summary.splitlines()[0][:160],
            "search_text": "",
        }

    def _prepare_computation_locked(
        self, memory_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        allowed = {
            "task_id",
            "description",
            "assumptions",
            "exact_input",
            "software",
            "environment_versions",
            "random_seed",
            "output",
            "output_artifact",
            "exit_status",
            "error_output",
            "interpretation",
            "related_memory_ids",
            "fact_candidate_operation_ids",
        }
        self._reject_unknown(payload, allowed, "computation")
        task_id = _require_text(payload.get("task_id"), "task_id")
        self._require_allocated_task_locked(task_id)
        description = _require_text(payload.get("description"), "description")
        assumptions = _optional_text(payload.get("assumptions"), "assumptions")
        exact_input = _require_text(payload.get("exact_input"), "exact_input")
        software = _require_mapping(payload.get("software"), "software")
        if set(software) != {"name", "version"}:
            raise ValidationError("software requires exactly name and version")
        software = {
            "name": _require_text(software["name"], "software.name"),
            "version": _require_text(software["version"], "software.version"),
        }
        environment = _require_mapping(
            payload.get("environment_versions", {}), "environment_versions"
        )
        for key, value in environment.items():
            _require_text(key, "environment_versions key")
            if not isinstance(value, (str, int, float, bool)) and value is not None:
                raise ValidationError("environment_versions values must be scalar")
        if "output" not in payload and "output_artifact" not in payload:
            raise ValidationError("computation requires output or output_artifact")
        output_artifact = payload.get("output_artifact")
        if output_artifact is not None:
            output_artifact = _require_mapping(output_artifact, "output_artifact")
            if set(output_artifact) != {"path", "sha256"}:
                raise ValidationError("output_artifact requires exactly path and sha256")
            checksum = _require_text(output_artifact["sha256"], "output_artifact.sha256")
            if not re.fullmatch(r"[0-9a-fA-F]{64}", checksum):
                raise ValidationError("output_artifact.sha256 is invalid")
            output_artifact = {
                "path": _require_text(output_artifact["path"], "output_artifact.path"),
                "sha256": checksum.lower(),
            }
        exit_status = payload.get("exit_status")
        if not isinstance(exit_status, (str, int)) or isinstance(exit_status, bool):
            raise ValidationError("exit_status must be a string or integer")
        related_raw = _require_mapping(payload.get("related_memory_ids", {}), "related_memory_ids")
        expected_keys = {
            "fact",
            "route",
            "memo",
            "claim",
            "obligation",
        }
        unknown_related = set(related_raw) - expected_keys
        if unknown_related:
            raise ValidationError(f"unknown related_memory_ids groups: {sorted(unknown_related)}")
        related: dict[str, list[str]] = {}
        for name in sorted(expected_keys):
            ids = _id_list(related_raw.get(name, []), f"related_memory_ids.{name}")
            target_type = MemoryType(name)
            for target_id in ids:
                # Computations are historical evidence, so references to an
                # inactive fact/claim/obligation remain valid and are overlaid.
                self._require_memory_locked(target_id, target_type)
            related[name] = ids
        operation_ids = _id_list(
            payload.get("fact_candidate_operation_ids", []),
            "fact_candidate_operation_ids",
        )
        core = {
            "task_id": task_id,
            "description": description,
            "assumptions": assumptions,
            "exact_input": exact_input,
            "software": software,
            "environment_versions": environment,
            "random_seed": payload.get("random_seed"),
            "output": copy.deepcopy(payload.get("output")),
            "output_artifact": output_artifact,
            "exit_status": exit_status,
            "error_output": _optional_text(payload.get("error_output"), "error_output"),
            "interpretation": _require_text(payload.get("interpretation"), "interpretation"),
            "related_memory_ids": related,
            "fact_candidate_operation_ids": operation_ids,
        }
        summary_parts = [description]
        if assumptions:
            summary_parts.append(f"Assumptions: {assumptions}")
        summary_parts.append(f"{software['name']} {software['version']}; exit {exit_status}")
        summary_parts.append(core["interpretation"])
        abstract = " ".join(summary_parts)
        return {
            "kind": MemoryType.COMPUTATION,
            "id": memory_id,
            "abstract": abstract,
            "core": core,
            "metadata": {},
            "links": [],
            "fact_dependencies": [],
            "obligation_predecessors": [],
            "title": description.splitlines()[0][:160],
            "search_text": f"{abstract} {abstract}",
        }

    def _prepare_add_locked(
        self,
        kind: MemoryType,
        memory_id: str,
        payload: dict[str, Any],
        pending_types: Mapping[str, MemoryType] | None = None,
    ) -> dict[str, Any]:
        if kind is MemoryType.FACT:
            if pending_types:
                raise ValidationError("fact proposals are not allowed in non-fact atomic groups")
            return self._prepare_fact_locked(memory_id, payload)
        if kind is MemoryType.ROUTE:
            return self._prepare_route_locked(memory_id, payload, pending_types)
        if kind in {MemoryType.MEMO, MemoryType.CLAIM}:
            return self._prepare_memo_or_claim_locked(
                kind, memory_id, payload, pending_types
            )
        if kind is MemoryType.OBLIGATION:
            return self._prepare_obligation_locked(memory_id, payload, pending_types)
        if kind is MemoryType.TASK:
            return self._prepare_task_locked(memory_id, payload)
        if kind is MemoryType.COMPUTATION:
            return self._prepare_computation_locked(memory_id, payload)
        raise ValidationError(f"unsupported add type: {kind.value}")

    def _insert_base_record_locked(self, prepared: Mapping[str, Any]) -> None:
        now = _utc_now()
        self._connection.execute(
            """INSERT INTO memories(
                   memory_id,memory_type,revision,metadata_version,active,abstract,
                   core_json,metadata_json,created_at,updated_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (
                prepared["id"],
                prepared["kind"].value,
                1,
                1,
                1,
                prepared["abstract"],
                _canonical_json(prepared["core"]),
                _canonical_json(prepared["metadata"]),
                now,
                now,
            ),
        )
        if prepared["kind"] in _SEARCHABLE_TYPES:
            self._connection.execute(
                """INSERT INTO search_documents(
                       memory_id,memory_type,title,abstract,search_text
                   ) VALUES(?,?,?,?,?)""",
                (
                    prepared["id"],
                    prepared["kind"].value,
                    prepared["title"],
                    prepared["abstract"],
                    prepared["search_text"],
                ),
            )

    def _insert_specialized_indexes_locked(self, prepared: Mapping[str, Any]) -> None:
        now = _utc_now()
        for predecessor_id in prepared.get("fact_dependencies", []):
            if self._fact_reaches_locked(prepared["id"], predecessor_id):
                raise ValidationError(
                    f"publishing {prepared['id']} would create a fact dependency cycle"
                )
            self._connection.execute(
                "INSERT INTO fact_dependencies(predecessor_id,fact_id,created_at) VALUES(?,?,?)",
                (predecessor_id, prepared["id"], now),
            )
        if prepared["kind"] is MemoryType.FACT:
            self._connection.execute(
                """INSERT INTO fact_foundation_versions(foundation_version,fact_id)
                   VALUES(?,?)""",
                (str(prepared["foundation_policy_version"]), prepared["id"]),
            )
            for index, reference in enumerate(prepared.get("external_references", [])):
                self._connection.execute(
                    """INSERT INTO fact_external_references(
                           fact_id,reference_key,reference_index,reference_json
                       ) VALUES(?,?,?,?)""",
                    (
                        prepared["id"],
                        reference["stable_identifier_or_url"],
                        index,
                        _canonical_json(reference),
                    ),
                )
        for fact_id in prepared.get("obligation_predecessors", []):
            self._connection.execute(
                """INSERT INTO obligation_predecessors(obligation_id,fact_id,created_at)
                   VALUES(?,?,?)""",
                (prepared["id"], fact_id, now),
            )

    def _fact_reaches_locked(self, start_id: str, target_id: str) -> bool:
        if start_id == target_id:
            return True
        queue = deque([start_id])
        seen = {start_id}
        while queue:
            current = queue.popleft()
            rows = self._connection.execute(
                "SELECT fact_id FROM fact_dependencies WHERE predecessor_id = ?",
                (current,),
            ).fetchall()
            for row in rows:
                child = row["fact_id"]
                if child == target_id:
                    return True
                if child not in seen:
                    seen.add(child)
                    queue.append(child)
        return False

    def _ensure_route_link_locked(
        self,
        route_id: str,
        memory_id: str,
        memory_type: MemoryType,
        *,
        cause_id: str | None,
    ) -> bool:
        existing = self._connection.execute(
            """SELECT 1 FROM route_links
               WHERE route_id=? AND memory_id=? AND memory_type=? AND current=1""",
            (route_id, memory_id, memory_type.value),
        ).fetchone()
        if existing is not None:
            return False
        self._connection.execute(
            """INSERT INTO route_links(
                   link_id,route_id,memory_id,memory_type,current,created_at,cause_id
               ) VALUES(?,?,?,?,1,?,?)""",
            (
                f"RL-{uuid.uuid4().hex}",
                route_id,
                memory_id,
                memory_type.value,
                _utc_now(),
                cause_id,
            ),
        )
        return True

    def _end_route_link_locked(
        self,
        route_id: str,
        memory_id: str,
        memory_type: MemoryType,
        *,
        cause_id: str | None,
    ) -> bool:
        cursor = self._connection.execute(
            """UPDATE route_links SET current=0, ended_at=?, cause_id=?
               WHERE route_id=? AND memory_id=? AND memory_type=? AND current=1""",
            (_utc_now(), cause_id, route_id, memory_id, memory_type.value),
        )
        return cursor.rowcount > 0

    def _insert_links_locked(
        self, prepared: Mapping[str, Any], *, cause_id: str | None
    ) -> set[str]:
        touched: set[str] = {prepared["id"]}
        for route_id, memory_id, memory_type in prepared.get("links", []):
            if self._ensure_route_link_locked(
                route_id, memory_id, memory_type, cause_id=cause_id
            ):
                touched.update({route_id, memory_id})
        return touched

    def _insert_pending_references_locked(
        self,
        prepared: Mapping[str, Any],
        *,
        cause_id: str,
    ) -> None:
        pending = list(prepared.get("pending_references", []))
        deferred = list(prepared.get("deferred_typed_updates", []))
        if not pending and not deferred:
            return
        now = _utc_now()
        document_ids: dict[str, str] = {}
        for item in deferred:
            document_key = str(item["document_key"])
            document_id = "DTR-" + _digest(
                {
                    "source": prepared["id"],
                    "cause": cause_id,
                    "key": document_key,
                    "template": item["template"],
                }
            )[:32]
            document_ids[document_key] = document_id
            self._connection.execute(
                """INSERT INTO deferred_typed_updates(
                       document_id,source_memory_id,source_memory_type,field_name,mode,
                       template_json,state,cause_operation_id,created_at,updated_at
                   ) VALUES(?,?,?,?,?,?,'pending',?,?,?)""",
                (
                    document_id,
                    prepared["id"],
                    prepared["kind"].value,
                    item["field_name"],
                    item["mode"],
                    _canonical_json(item["template"]),
                    cause_id,
                    now,
                    now,
                ),
            )

        for item in pending:
            temporary_id = str(item["temporary_id"])
            expected_type = item["expected_type"]
            reference_state = str(item.get("state") or "pending")
            if reference_state not in {"pending", "rejected", "abandoned"}:
                reference_state = "pending"
            try:
                self._register_temporary_target_locked(temporary_id, expected_type)
            except ValidationError:
                # A conflicting or terminal target is a soft-reference
                # failure.  Persist it on the source instead of rejecting the
                # source operation.
                reference_state = "rejected"
            target = self._connection.execute(
                "SELECT state FROM temporary_memory_ids WHERE temporary_id=?",
                (temporary_id,),
            ).fetchone()
            if target is None:
                # Registration can fail only against an existing constraint,
                # but keep this branch defensive for databases imported from
                # an older implementation.
                self._connection.execute(
                    """INSERT INTO temporary_memory_ids(
                           temporary_id,memory_type,state,created_at,updated_at
                       ) VALUES(?,?,'rejected',?,?)""",
                    (
                        temporary_id,
                        _reference_type_token(expected_type),
                        now,
                        now,
                    ),
                )
                reference_state = "rejected"
            elif target["state"] in {"rejected", "abandoned"}:
                reference_state = str(target["state"])
            document_key = item.get("document_key")
            document_id = document_ids.get(str(document_key)) if document_key else None
            field_path_json = _canonical_json(item["field_path"])
            reference_id = "PR-" + _digest(
                {
                    "source": prepared["id"],
                    "cause": cause_id,
                    "path": item["field_path"],
                    "temporary_id": temporary_id,
                }
            )[:32]
            self._connection.execute(
                """INSERT INTO pending_memory_references(
                       reference_id,source_memory_id,source_memory_type,field_path_json,
                       document_path_json,expected_type,temporary_id,state,document_id,
                       cause_operation_id,created_at,updated_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    reference_id,
                    prepared["id"],
                    prepared["kind"].value,
                    field_path_json,
                    (
                        None
                        if item.get("document_path") is None
                        else _canonical_json(item["document_path"])
                    ),
                    _reference_type_token(expected_type),
                    temporary_id,
                    reference_state,
                    document_id,
                    cause_id,
                    now,
                    now,
                ),
            )
        for document_id in document_ids.values():
            states = {
                str(row["state"])
                for row in self._connection.execute(
                    """SELECT state FROM pending_memory_references
                       WHERE document_id=? AND state<>'removed'""",
                    (document_id,),
                ).fetchall()
            }
            terminal_state = (
                "abandoned"
                if "abandoned" in states
                else "rejected" if "rejected" in states else None
            )
            if terminal_state is not None:
                self._connection.execute(
                    """UPDATE deferred_typed_updates SET state=?,updated_at=?
                       WHERE document_id=?""",
                    (terminal_state, now, document_id),
                )

    def _abandon_pending_source_field_locked(
        self,
        source_id: str,
        field_name: str,
        *,
        cause_id: str,
        temporary_id: str | None = None,
    ) -> bool:
        if temporary_id is not None:
            temporary_id = _strip_unpublished_suffix(temporary_id)
        rows = self._connection.execute(
            """SELECT reference_id,field_path_json,document_id,temporary_id,canonical_id
               FROM pending_memory_references
               WHERE source_memory_id=? AND state<>'removed'""",
            (source_id,),
        ).fetchall()
        selected = [
            row
            for row in rows
            if (_loads(row["field_path_json"], [None]) or [None])[0] == field_name
            and (
                temporary_id is None
                or row["temporary_id"] == temporary_id
                or row["canonical_id"] == temporary_id
            )
        ]
        if not selected:
            return False
        now = _utc_now()
        for row in selected:
            self._connection.execute(
                """UPDATE pending_memory_references SET state='removed',updated_at=?
                   WHERE reference_id=?""",
                (now, row["reference_id"]),
            )
            if row["document_id"]:
                self._connection.execute(
                    """UPDATE deferred_typed_updates SET state='removed',updated_at=?
                       WHERE document_id=? AND state<>'removed'""",
                    (now, row["document_id"]),
                )
        self._append_audit_locked(
            "pending_source_field_removed",
            "scheduler",
            subject_id=source_id,
            details={
                "field_name": field_name,
                "temporary_id": temporary_id,
                "cause_operation_id": cause_id,
            },
        )
        return True

    @staticmethod
    def _set_exact_path(value: Any, path: Sequence[Any], replacement: str) -> None:
        if not path:
            raise ValidationError("a deferred reference path cannot be empty")
        current = value
        for component in path[:-1]:
            if isinstance(component, int) and isinstance(current, list):
                if component < 0 or component >= len(current):
                    raise ValidationError("deferred reference path is out of range")
                current = current[component]
            elif isinstance(component, str) and isinstance(current, dict):
                if component not in current:
                    raise ValidationError("deferred reference path is missing")
                current = current[component]
            else:
                raise ValidationError("deferred reference path has the wrong container type")
        leaf = path[-1]
        if isinstance(leaf, int) and isinstance(current, list):
            if leaf < 0 or leaf >= len(current):
                raise ValidationError("deferred reference path is out of range")
            current[leaf] = replacement
        elif isinstance(leaf, str) and isinstance(current, dict):
            if leaf not in current:
                raise ValidationError("deferred reference path is missing")
            current[leaf] = replacement
        else:
            raise ValidationError("deferred reference path has the wrong container type")

    @staticmethod
    def _dedupe_relation_reference_lists(relation: dict[str, Any]) -> None:
        """Apply relation set semantics after temporary IDs merge."""

        for field in ("premise_memory_ids", "supporting_fact_ids"):
            values = relation.get(field)
            if not isinstance(values, list):
                continue
            seen: set[str] = set()
            relation[field] = [
                item
                for item in values
                if isinstance(item, str) and not (item in seen or seen.add(item))
            ]

    def _materialize_deferred_document_locked(self, document_id: str) -> str | None:
        document = self._connection.execute(
            "SELECT * FROM deferred_typed_updates WHERE document_id=?", (document_id,)
        ).fetchone()
        if document is None or document["state"] != "pending":
            return None
        references = self._connection.execute(
            """SELECT * FROM pending_memory_references
               WHERE document_id=? ORDER BY reference_id""",
            (document_id,),
        ).fetchall()
        if not references or any(item["state"] != "resolved" for item in references):
            return None
        template = _loads(document["template_json"], {})
        for reference in references:
            path = _loads(reference["document_path_json"], None)
            if not isinstance(path, list):
                raise ValidationError("deferred reference lacks an exact typed path")
            canonical_id = str(reference["canonical_id"])
            expected = _reference_type_set(str(reference["expected_type"]))
            actual = self._require_memory_locked(canonical_id, expected)
            if _reference_requires_active(actual):
                self._require_memory_locked(canonical_id, actual, active=True)
            self._set_exact_path(template, path, canonical_id)
        self._dedupe_relation_reference_lists(template)

        source_id = str(document["source_memory_id"])
        source = self._connection.execute(
            "SELECT * FROM memories WHERE memory_id=?", (source_id,)
        ).fetchone()
        if source is None or source["memory_type"] != MemoryType.OBLIGATION.value:
            raise ValidationError("deferred relation source is not an obligation")
        if document["field_name"] != "relations" or document["mode"] != "append":
            raise ValidationError("unsupported deferred typed update")
        relation = self._validate_relations_locked([template])[0]
        metadata = _loads(source["metadata_json"], {})
        metadata.setdefault("relations", [])
        if relation not in metadata["relations"]:
            metadata["relations"].append(relation)
            self._connection.execute(
                "UPDATE memories SET metadata_json=?,updated_at=? WHERE memory_id=?",
                (_canonical_json(metadata), _utc_now(), source_id),
            )
        self._connection.execute(
            """UPDATE deferred_typed_updates SET state='resolved',updated_at=?
               WHERE document_id=?""",
            (_utc_now(), document_id),
        )
        return source_id

    def _activate_resolved_link_locked(self, reference: sqlite3.Row, canonical_id: str) -> set[str]:
        source_id = str(reference["source_memory_id"])
        source_type = MemoryType(reference["source_memory_type"])
        expected_types = _reference_type_set(str(reference["expected_type"]))
        if len(expected_types) != 1:
            raise ValidationError("a non-document soft reference must have one target type")
        expected_type = next(iter(expected_types))
        path = _loads(reference["field_path_json"], [])
        if not isinstance(path, list) or not path:
            raise ValidationError("pending reference lacks a typed field path")
        field = path[0]
        if source_type is MemoryType.ROUTE:
            expected_fields = {
                "related_obligation_ids": MemoryType.OBLIGATION,
                "active_fact_ids": MemoryType.FACT,
                "relevant_memo_ids": MemoryType.MEMO,
                "relevant_claim_ids": MemoryType.CLAIM,
            }
            if expected_fields.get(field) is not expected_type:
                raise ValidationError("pending route reference has inconsistent type metadata")
            route_id, memory_id, link_type = source_id, canonical_id, expected_type
        elif source_type is MemoryType.FACT:
            if field != "related_route_ids" or expected_type is not MemoryType.ROUTE:
                raise ValidationError("pending fact reference has inconsistent type metadata")
            route_id, memory_id, link_type = canonical_id, source_id, MemoryType.FACT
        elif source_type in {MemoryType.MEMO, MemoryType.CLAIM}:
            if field != "related_route_ids" or expected_type is not MemoryType.ROUTE:
                raise ValidationError("pending memo/claim reference has inconsistent type metadata")
            route_id, memory_id, link_type = canonical_id, source_id, source_type
        elif source_type is MemoryType.OBLIGATION:
            if field == "predecessor_fact_ids" and expected_type is MemoryType.FACT:
                source = self._connection.execute(
                    "SELECT core_json FROM memories WHERE memory_id=? AND memory_type='obligation'",
                    (source_id,),
                ).fetchone()
                if source is None:
                    raise ValidationError("pending predecessor source is not an obligation")
                core = _loads(source["core_json"], {})
                predecessors = list(core.get("predecessor_fact_ids", []))
                if canonical_id not in predecessors:
                    predecessors.append(canonical_id)
                    core["predecessor_fact_ids"] = predecessors
                    self._connection.execute(
                        "UPDATE memories SET core_json=?,updated_at=? WHERE memory_id=?",
                        (_canonical_json(core), _utc_now(), source_id),
                    )
                self._connection.execute(
                    """INSERT OR IGNORE INTO obligation_predecessors(
                           obligation_id,fact_id,created_at
                       ) VALUES(?,?,?)""",
                    (source_id, canonical_id, _utc_now()),
                )
                return {source_id, canonical_id}
            if field != "related_route_ids" or expected_type is not MemoryType.ROUTE:
                raise ValidationError("pending obligation reference has inconsistent type metadata")
            route_id, memory_id, link_type = canonical_id, source_id, MemoryType.OBLIGATION
        elif source_type is MemoryType.COMPUTATION:
            expected_groups = {
                "fact": MemoryType.FACT,
                "route": MemoryType.ROUTE,
                "memo": MemoryType.MEMO,
                "claim": MemoryType.CLAIM,
                "obligation": MemoryType.OBLIGATION,
            }
            if (
                field != "related_memory_ids"
                or len(path) < 3
                or expected_groups.get(path[1]) is not expected_type
            ):
                raise ValidationError(
                    "pending computation reference has inconsistent type metadata"
                )
            # Computations are immutable evidence records.  Their soft-link
            # ledger supplies the read overlay without changing core_json or
            # creating a reciprocal graph edge.
            return {source_id, canonical_id}
        else:
            raise ValidationError("unsupported pending-reference source type")
        self._ensure_route_link_locked(
            route_id,
            memory_id,
            link_type,
            cause_id=str(reference["cause_operation_id"]),
        )
        return {source_id, canonical_id}

    def _bump_reference_sources_locked(self, source_ids: Iterable[str]) -> None:
        now = _utc_now()
        for source_id in sorted(set(source_ids)):
            row = self._connection.execute(
                "SELECT memory_type FROM memories WHERE memory_id=?", (source_id,)
            ).fetchone()
            if row is None:
                continue
            kind = MemoryType(row["memory_type"])
            if kind in {MemoryType.ROUTE, MemoryType.OBLIGATION}:
                self._connection.execute(
                    """UPDATE memories SET revision=revision+1,updated_at=?
                       WHERE memory_id=?""",
                    (now, source_id),
                )
            elif kind in {
                MemoryType.FACT,
                MemoryType.MEMO,
                MemoryType.CLAIM,
                MemoryType.COMPUTATION,
            }:
                self._connection.execute(
                    """UPDATE memories SET metadata_version=metadata_version+1,updated_at=?
                       WHERE memory_id=?""",
                    (now, source_id),
                )

    def _resolve_temporary_target_locked(
        self,
        temporary_id: str,
        canonical_id: str,
        *,
        resolution: str,
        operation_id: str,
        actor: str,
    ) -> set[str]:
        temporary_id = _strip_unpublished_suffix(
            _require_text(temporary_id, "temporary_id")
        )
        if not temporary_id:
            raise ValidationError("temporary_id must not be an unpublished marker alone")
        target = self._connection.execute(
            "SELECT * FROM temporary_memory_ids WHERE temporary_id=?", (temporary_id,)
        ).fetchone()
        if target is None:
            return {canonical_id}
        if target["state"] == "resolved":
            if target["canonical_id"] != canonical_id:
                raise ConflictError(
                    f"temporary ID {temporary_id} already resolves to {target['canonical_id']}"
                )
        if target["state"] in {"rejected", "abandoned"}:
            raise ValidationError(
                f"temporary ID is terminal ({target['state']}): {temporary_id}"
            )
        expected_types = _reference_type_set(str(target["memory_type"]))
        actual_type = self._require_memory_locked(canonical_id, expected_types)
        canonical_row = self._connection.execute(
            "SELECT active FROM memories WHERE memory_id=?", (canonical_id,)
        ).fetchone()
        assert canonical_row is not None
        canonical_active = bool(canonical_row["active"])
        unresolved_count = int(
            self._connection.execute(
                """SELECT COUNT(*) AS count FROM pending_memory_references
                   WHERE temporary_id=? AND state='pending'""",
                (temporary_id,),
            ).fetchone()["count"]
        )
        if target["state"] == "resolved" and unresolved_count == 0:
            return {canonical_id}
        now = _utc_now()
        self._connection.execute(
            """UPDATE temporary_memory_ids
               SET memory_type=?,state='resolved',canonical_id=?,resolution=?,target_operation_id=?,
                   reason=NULL,updated_at=? WHERE temporary_id=?""",
            (
                actual_type.value,
                canonical_id,
                resolution,
                operation_id,
                now,
                temporary_id,
            ),
        )
        references = self._connection.execute(
            """SELECT * FROM pending_memory_references
               WHERE temporary_id=? AND state='pending' ORDER BY reference_id""",
            (temporary_id,),
        ).fetchall()
        touched: set[str] = {canonical_id}
        changed_sources: set[str] = set()
        document_ids: set[str] = set()
        for reference in references:
            source_id = str(reference["source_memory_id"])
            source_type = MemoryType(reference["source_memory_type"])
            inactive_for_source = (
                source_type is not MemoryType.COMPUTATION
                and _reference_requires_active(actual_type)
                and not canonical_active
            )
            if (
                not _reference_type_accepts(
                    str(reference["expected_type"]), actual_type
                )
                or inactive_for_source
            ):
                self._connection.execute(
                    """UPDATE pending_memory_references
                       SET state='rejected',canonical_id=NULL,updated_at=?
                       WHERE reference_id=?""",
                    (now, reference["reference_id"]),
                )
                if reference["document_id"] is not None:
                    self._connection.execute(
                        """UPDATE deferred_typed_updates
                           SET state='rejected',updated_at=?
                           WHERE document_id=? AND state='pending'""",
                        (now, reference["document_id"]),
                    )
                changed_sources.add(source_id)
                continue
            if reference["document_id"] is None:
                touched.update(self._activate_resolved_link_locked(reference, canonical_id))
            else:
                document_ids.add(str(reference["document_id"]))
            self._connection.execute(
                """UPDATE pending_memory_references
                   SET state='resolved',canonical_id=?,updated_at=? WHERE reference_id=?""",
                (canonical_id, now, reference["reference_id"]),
            )
            changed_sources.add(source_id)
        for document_id in sorted(document_ids):
            source_id = self._materialize_deferred_document_locked(document_id)
            if source_id:
                changed_sources.add(source_id)
                touched.add(source_id)
        self._bump_reference_sources_locked(changed_sources)
        self._append_audit_locked(
            "temporary_reference_resolved",
            actor,
            subject_id=canonical_id,
            details={
                "temporary_id": temporary_id,
                "resolution": resolution,
                "operation_id": operation_id,
                "affected_source_ids": sorted(changed_sources),
            },
        )
        return touched

    def _reject_temporary_target_locked(
        self,
        temporary_id: str,
        expected_type: MemoryType,
        *,
        reason: str,
        operation_id: str,
        actor: str,
    ) -> set[str]:
        temporary_id = _strip_unpublished_suffix(
            _require_text(temporary_id, "temporary_id")
        )
        if not temporary_id:
            raise ValidationError("temporary_id must not be an unpublished marker alone")
        self._register_temporary_target_locked(temporary_id, expected_type)
        row = self._connection.execute(
            "SELECT * FROM temporary_memory_ids WHERE temporary_id=?", (temporary_id,)
        ).fetchone()
        assert row is not None
        if row["state"] == "resolved":
            return set()
        if row["state"] in {"rejected", "abandoned"}:
            return set()
        now = _utc_now()
        self._connection.execute(
            """UPDATE temporary_memory_ids
               SET state='rejected',reason=?,target_operation_id=?,updated_at=?
               WHERE temporary_id=?""",
            (reason, operation_id, now, temporary_id),
        )
        affected_rows = self._connection.execute(
            """SELECT DISTINCT source_memory_id,document_id
               FROM pending_memory_references
               WHERE temporary_id=? AND state='pending'""",
            (temporary_id,),
        ).fetchall()
        self._connection.execute(
            """UPDATE pending_memory_references SET state='rejected',updated_at=?
               WHERE temporary_id=? AND state='pending'""",
            (now, temporary_id),
        )
        document_ids = {
            str(item["document_id"]) for item in affected_rows if item["document_id"]
        }
        for document_id in document_ids:
            self._connection.execute(
                """UPDATE deferred_typed_updates SET state='rejected',updated_at=?
                   WHERE document_id=? AND state='pending'""",
                (now, document_id),
            )
        source_ids = {str(item["source_memory_id"]) for item in affected_rows}
        self._bump_reference_sources_locked(source_ids)
        self._append_audit_locked(
            "temporary_reference_rejected",
            actor,
            subject_id=temporary_id,
            details={
                "reason": reason,
                "operation_id": operation_id,
                "affected_source_ids": sorted(source_ids),
            },
        )
        return source_ids

    def _record_root_resolution_locked(self, fact_id: str, outcome: str) -> dict[str, Any]:
        row = self._connection.execute(
            "SELECT * FROM root_resolution_state WHERE singleton=1"
        ).fetchone()
        if row is None or row["root_obligation_id"] is None:
            raise ValidationError("root obligation has not been configured")
        now = _utc_now()
        if row["solution_fact_id"] is None:
            self._connection.execute(
                """UPDATE root_resolution_state
                   SET solution_fact_id=?, outcome=?, status='resolved', updated_at=?
                   WHERE singleton=1""",
                (fact_id, outcome, now),
            )
            is_primary = 1
            disposition = "primary"
        elif row["outcome"] == outcome:
            is_primary = 0
            disposition = "alternate_same_outcome"
        else:
            self._connection.execute(
                """UPDATE root_resolution_state
                   SET status='needs_attention', updated_at=? WHERE singleton=1""",
                (now,),
            )
            is_primary = 0
            disposition = "opposite_outcome_conflict"
        self._connection.execute(
            """INSERT INTO root_resolutions(fact_id,outcome,is_primary,committed_at)
               VALUES(?,?,?,?)""",
            (fact_id, outcome, is_primary, now),
        )
        return {
            "root_resolution_disposition": disposition,
            "root_obligation_id": row["root_obligation_id"],
        }

    def _publish_prepared_locked(
        self, prepared: Mapping[str, Any], *, cause_id: str, actor: str
    ) -> tuple[set[str], dict[str, Any]]:
        self._insert_base_record_locked(prepared)
        self._insert_specialized_indexes_locked(prepared)
        touched = self._insert_links_locked(prepared, cause_id=cause_id)
        self._insert_pending_references_locked(prepared, cause_id=cause_id)
        result: dict[str, Any] = {"canonical_id": prepared["id"], "revision": 1}
        if prepared["kind"] is MemoryType.FACT:
            root_resolution = prepared["core"].get("root_resolution")
            if root_resolution:
                root_result = self._record_root_resolution_locked(
                    prepared["id"], root_resolution["outcome"]
                )
                result.update(root_result)
                touched.add(root_result["root_obligation_id"])
        self._append_audit_locked(
            "memory_published",
            actor,
            subject_id=prepared["id"],
            details={"memory_type": prepared["kind"].value, "operation_id": cause_id},
        )
        return touched, result

    # ------------------------------------------------------------------
    # Idempotent memory operations

    def _operation_from_row(self, row: sqlite3.Row, *, replayed: bool) -> OperationResult:
        return OperationResult(
            operation_id=row["operation_id"],
            operation_type=row["operation_type"],
            status=row["status"],
            canonical_ids=tuple(_loads(row["canonical_ids_json"], [])),
            resolution=row["resolution"],
            result=_loads(row["result_json"], {}),
            error=row["error"],
            replayed=replayed,
        )

    def operation_status(self, operation_id: str) -> OperationResult | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM operations WHERE operation_id = ?", (operation_id,)
            ).fetchone()
        return None if row is None else self._operation_from_row(row, replayed=False)

    def _start_operation_locked(
        self,
        operation_id: str,
        operation_type: str,
        input_hash: str,
        *,
        group_id: str | None = None,
    ) -> OperationResult | None:
        row = self._connection.execute(
            "SELECT * FROM operations WHERE operation_id = ?", (operation_id,)
        ).fetchone()
        if row is not None:
            if row["input_hash"] != input_hash or row["operation_type"] != operation_type:
                raise IdempotencyConflict(
                    f"operation ID {operation_id} was replayed with different input"
                )
            return self._operation_from_row(row, replayed=True)
        now = _utc_now()
        self._connection.execute(
            """INSERT INTO operations(
                   operation_id,operation_type,input_hash,status,group_id,created_at,updated_at
               ) VALUES(?,?,?,'pending',?,?,?)""",
            (operation_id, operation_type, input_hash, group_id, now, now),
        )
        return None

    def _finish_operation_locked(
        self,
        operation_id: str,
        *,
        status: str,
        canonical_ids: Sequence[str] = (),
        resolution: str | None = None,
        result: Mapping[str, Any] | None = None,
        error: str | None = None,
    ) -> OperationResult:
        self._connection.execute(
            """UPDATE operations
               SET status=?,canonical_ids_json=?,resolution=?,result_json=?,error=?,updated_at=?
               WHERE operation_id=?""",
            (
                status,
                _canonical_json(list(canonical_ids)),
                resolution,
                _canonical_json(dict(result or {})),
                error,
                _utc_now(),
                operation_id,
            ),
        )
        row = self._connection.execute(
            "SELECT * FROM operations WHERE operation_id = ?", (operation_id,)
        ).fetchone()
        assert row is not None
        return self._operation_from_row(row, replayed=False)

    def _insert_proposal_mapping_locked(
        self,
        proposal_id: str | None,
        operation_id: str,
        canonical_id: str,
        resolution: str,
    ) -> None:
        if proposal_id is None:
            return
        proposal_id = _require_text(proposal_id, "proposal_id")
        existing = self._connection.execute(
            "SELECT * FROM proposal_mappings WHERE proposal_id=? OR operation_id=?",
            (proposal_id, operation_id),
        ).fetchone()
        expected = (proposal_id, operation_id, canonical_id, resolution)
        if existing is not None:
            actual = (
                existing["proposal_id"],
                existing["operation_id"],
                existing["canonical_id"],
                existing["resolution"],
            )
            if actual != expected:
                raise IdempotencyConflict("proposal mapping was replayed differently")
            return
        self._connection.execute(
            """INSERT INTO proposal_mappings(
                   proposal_id,operation_id,canonical_id,resolution,created_at
               ) VALUES(?,?,?,?,?)""",
            (proposal_id, operation_id, canonical_id, resolution, _utc_now()),
        )

    def proposal_mapping(self, proposal_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM proposal_mappings WHERE proposal_id=?", (proposal_id,)
            ).fetchone()
        if row is None:
            return None
        return {
            "proposal_id": row["proposal_id"],
            "operation_id": row["operation_id"],
            "canonical_id": row["canonical_id"],
            "resolution": row["resolution"],
            "created_at": row["created_at"],
        }

    def temporary_reference_status(self, temporary_id: str) -> dict[str, Any] | None:
        """Return scheduler-visible state for one durable temporary ID."""

        temporary_id = _strip_unpublished_suffix(
            _require_text(temporary_id, "temporary_id")
        )
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM temporary_memory_ids WHERE temporary_id=?", (temporary_id,)
            ).fetchone()
            if row is None:
                return None
            counts = self._connection.execute(
                """SELECT state,COUNT(*) AS count FROM pending_memory_references
                   WHERE temporary_id=? GROUP BY state""",
                (temporary_id,),
            ).fetchall()
        return {
            "temporary_id": row["temporary_id"],
            "memory_type": row["memory_type"],
            "state": row["state"],
            "canonical_id": row["canonical_id"],
            "resolution": row["resolution"],
            "target_operation_id": row["target_operation_id"],
            "reason": row["reason"],
            "reference_counts": {item["state"]: int(item["count"]) for item in counts},
        }

    def list_temporary_references(
        self, *, state: str | None = None
    ) -> list[dict[str, Any]]:
        """List durable typed temporary targets and their affected sources."""

        allowed_states = {"pending", "resolved", "rejected", "abandoned"}
        if state is not None and state not in allowed_states:
            raise ValidationError(
                "temporary-reference state must be pending, resolved, rejected, or abandoned"
            )
        with self._lock:
            if state is None:
                rows = self._connection.execute(
                    "SELECT * FROM temporary_memory_ids ORDER BY created_at,temporary_id"
                ).fetchall()
            else:
                rows = self._connection.execute(
                    """SELECT * FROM temporary_memory_ids
                       WHERE state=? ORDER BY created_at,temporary_id""",
                    (state,),
                ).fetchall()
            result: list[dict[str, Any]] = []
            for row in rows:
                references = self._connection.execute(
                    """SELECT reference_id,source_memory_id,source_memory_type,
                              field_path_json,expected_type,state,canonical_id,
                              cause_operation_id
                       FROM pending_memory_references
                       WHERE temporary_id=? ORDER BY created_at,reference_id""",
                    (row["temporary_id"],),
                ).fetchall()
                result.append(
                    {
                        "temporary_id": row["temporary_id"],
                        "memory_type": row["memory_type"],
                        "state": row["state"],
                        "canonical_id": row["canonical_id"],
                        "resolution": row["resolution"],
                        "target_operation_id": row["target_operation_id"],
                        "reason": row["reason"],
                        "sources": [
                            {
                                "reference_id": reference["reference_id"],
                                "source_memory_id": reference["source_memory_id"],
                                "source_memory_type": reference["source_memory_type"],
                                "field_path": _loads(
                                    reference["field_path_json"], []
                                ),
                                "expected_type": reference["expected_type"],
                                "state": reference["state"],
                                "canonical_id": reference["canonical_id"],
                                "cause_operation_id": reference[
                                    "cause_operation_id"
                                ],
                            }
                            for reference in references
                        ],
                    }
                )
        return result

    def pending_references_for(self, source_memory_id: str) -> list[dict[str, Any]]:
        """List typed pending-reference history for one canonical source."""

        source_memory_id = _require_text(source_memory_id, "source_memory_id")
        with self._lock:
            self._require_memory_locked(
                source_memory_id,
                {
                    MemoryType.FACT,
                    MemoryType.ROUTE,
                    MemoryType.MEMO,
                    MemoryType.CLAIM,
                    MemoryType.OBLIGATION,
                    MemoryType.COMPUTATION,
                },
            )
            rows = self._connection.execute(
                """SELECT * FROM pending_memory_references
                   WHERE source_memory_id=? ORDER BY created_at,reference_id""",
                (source_memory_id,),
            ).fetchall()
        return [
            {
                "reference_id": row["reference_id"],
                "field_path": _loads(row["field_path_json"], []),
                "expected_type": row["expected_type"],
                "temporary_id": row["temporary_id"],
                "state": row["state"],
                "canonical_id": row["canonical_id"],
                "cause_operation_id": row["cause_operation_id"],
            }
            for row in rows
        ]

    def reject_temporary_reference(
        self,
        temporary_id: str,
        expected_type: MemoryType | str,
        *,
        reason: str,
        operation_id: str,
        actor: str = "scheduler",
    ) -> tuple[str, ...]:
        """Reject an unpublished target and every still-pending dependent slot."""

        kind = self._parse_memory_type(expected_type)
        reason = _require_text(reason, "reason")
        operation_id = _require_text(operation_id, "operation_id")
        actor = _require_text(actor, "actor")
        with self.transaction():
            touched = self._reject_temporary_target_locked(
                temporary_id,
                kind,
                reason=reason,
                operation_id=operation_id,
                actor=actor,
            )
        self._refresh_projections(touched)
        return tuple(sorted(touched))

    def resolve_temporary_reference(
        self,
        temporary_id: str,
        canonical_id: str,
        *,
        resolution: str,
        operation_id: str,
        actor: str = "scheduler",
    ) -> tuple[str, ...]:
        """Atomically resolve all typed slots, including after deduplication."""

        temporary_id = _strip_unpublished_suffix(
            _require_text(temporary_id, "temporary_id")
        )
        canonical_id = _require_text(canonical_id, "canonical_id")
        resolution = _require_text(resolution, "resolution")
        operation_id = _require_text(operation_id, "operation_id")
        actor = _require_text(actor, "actor")
        with self.transaction():
            touched = self._resolve_temporary_target_locked(
                temporary_id,
                canonical_id,
                resolution=resolution,
                operation_id=operation_id,
                actor=actor,
            )
        self._refresh_projections(touched)
        return tuple(sorted(touched))

    def reconcile_update_resolution(
        self,
        operation_id: str,
        proposal_id: str,
        canonical_id: str,
        *,
        actor: str = "scheduler-recovery",
    ) -> tuple[str, ...]:
        """Finish only the mapping tail of an already committed update.

        Older runtimes could commit a route/obligation update without resolving
        typed relationships waiting on the original add proposal.  This method
        validates the immutable committed operation before adding that missing
        mapping; it never reapplies the patch.
        """

        operation_id = _require_text(operation_id, "operation_id")
        proposal_id = _require_text(proposal_id, "proposal_id")
        canonical_id = _require_text(canonical_id, "canonical_id")
        actor = _require_text(actor, "actor")
        with self.transaction():
            operation = self._connection.execute(
                "SELECT * FROM operations WHERE operation_id=?", (operation_id,)
            ).fetchone()
            if operation is None or operation["status"] != "committed":
                raise ValidationError(
                    "update-reference reconciliation requires a committed operation"
                )
            try:
                operation_type = OperationType(operation["operation_type"])
            except ValueError as exc:
                raise ValidationError(
                    "update-reference reconciliation requires a route/obligation update"
                ) from exc
            expected_type = {
                OperationType.ROUTE_UPDATE: MemoryType.ROUTE,
                OperationType.OBLIGATION_UPDATE: MemoryType.OBLIGATION,
            }.get(operation_type)
            if expected_type is None or operation["resolution"] != "updated":
                raise ValidationError(
                    "update-reference reconciliation requires a committed updated result"
                )
            if tuple(_loads(operation["canonical_ids_json"], [])) != (canonical_id,):
                raise ConflictError(
                    "update-reference reconciliation canonical target does not match"
                )
            temporary = self._connection.execute(
                "SELECT state FROM temporary_memory_ids WHERE temporary_id=?",
                (proposal_id,),
            ).fetchone()
            if temporary is not None and temporary["state"] in {"rejected", "abandoned"}:
                raise ValidationError(
                    f"cannot reconcile a {temporary['state']} temporary target"
                )
            self._register_temporary_target_locked(
                proposal_id, expected_type, operation_id=operation_id
            )
            self._insert_proposal_mapping_locked(
                proposal_id, operation_id, canonical_id, "updated"
            )
            touched = self._resolve_temporary_target_locked(
                proposal_id,
                canonical_id,
                resolution="updated",
                operation_id=operation_id,
                actor=actor,
            )
        self._refresh_projections(touched)
        return tuple(sorted(touched))

    def abandon_temporary_reference(
        self,
        temporary_id: str,
        *,
        reason: str,
        operation_id: str,
        actor: str = "scheduler",
    ) -> tuple[str, ...]:
        """Explicitly abandon unresolved/rejected typed slots; no timeout is used."""

        temporary_id = _strip_unpublished_suffix(
            _require_text(temporary_id, "temporary_id")
        )
        reason = _require_text(reason, "reason")
        operation_id = _require_text(operation_id, "operation_id")
        actor = _require_text(actor, "actor")
        with self.transaction():
            row = self._connection.execute(
                "SELECT * FROM temporary_memory_ids WHERE temporary_id=?", (temporary_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError(f"temporary ID not found: {temporary_id}")
            if row["state"] == "resolved":
                raise ConflictError(f"resolved temporary ID cannot be abandoned: {temporary_id}")
            now = _utc_now()
            affected = self._connection.execute(
                """SELECT DISTINCT source_memory_id,document_id
                   FROM pending_memory_references
                   WHERE temporary_id=? AND state IN ('pending','rejected')""",
                (temporary_id,),
            ).fetchall()
            self._connection.execute(
                """UPDATE temporary_memory_ids
                   SET state='abandoned',reason=?,target_operation_id=?,updated_at=?
                   WHERE temporary_id=?""",
                (reason, operation_id, now, temporary_id),
            )
            self._connection.execute(
                """UPDATE pending_memory_references SET state='abandoned',updated_at=?
                   WHERE temporary_id=? AND state IN ('pending','rejected')""",
                (now, temporary_id),
            )
            document_ids = {str(item["document_id"]) for item in affected if item["document_id"]}
            for document_id in document_ids:
                self._connection.execute(
                    """UPDATE deferred_typed_updates SET state='abandoned',updated_at=?
                       WHERE document_id=? AND state IN ('pending','rejected')""",
                    (now, document_id),
                )
            source_ids = {str(item["source_memory_id"]) for item in affected}
            self._bump_reference_sources_locked(source_ids)
            self._append_audit_locked(
                "temporary_reference_abandoned",
                actor,
                subject_id=temporary_id,
                details={
                    "reason": reason,
                    "operation_id": operation_id,
                    "affected_source_ids": sorted(source_ids),
                },
            )
        self._refresh_projections(source_ids)
        return tuple(sorted(source_ids))

    def apply_operation(
        self,
        operation_id: str,
        operation_type: OperationType | str,
        payload: Mapping[str, Any],
        *,
        proposal_id: str | None = None,
        actor: str = "scheduler",
    ) -> OperationResult:
        """Validate and atomically apply one of the nine proposal operations.

        Schema/revision failures are durably returned as ``status='rejected'``
        so a worker can repair them.  Replaying the same operation ID and input
        returns the stored result; replaying it with different input raises
        :class:`IdempotencyConflict`.
        """

        operation_id = _require_text(operation_id, "operation_id")
        actor = _require_text(actor, "actor")
        try:
            op_type = operation_type if isinstance(operation_type, OperationType) else OperationType(operation_type)
        except ValueError as exc:
            raise ValidationError(f"unknown operation type: {operation_type}") from exc
        payload_copy = _require_mapping(payload, "payload")
        input_hash = _digest(
            {
                "operation_type": op_type.value,
                "payload": payload_copy,
                "proposal_id": proposal_id,
            }
        )
        touched: set[str] = set()
        with self.transaction():
            replay = self._start_operation_locked(
                operation_id, op_type.value, input_hash
            )
            if replay is not None:
                return replay
            try:
                with self.transaction():
                    canonical_ids, resolution, details, touched = self._dispatch_operation_locked(
                        operation_id,
                        op_type,
                        payload_copy,
                        actor,
                        proposal_id=proposal_id,
                    )
                    if canonical_ids:
                        self._insert_proposal_mapping_locked(
                            proposal_id,
                            operation_id,
                            canonical_ids[0],
                            resolution,
                        )
            except (ValidationError, ConflictError) as exc:
                rejected_kind = {
                    OperationType.FACT: MemoryType.FACT,
                    OperationType.ROUTE_ADD: MemoryType.ROUTE,
                    OperationType.ROUTE_UPDATE: MemoryType.ROUTE,
                    OperationType.MEMO: MemoryType.MEMO,
                    OperationType.CLAIM_ADD: MemoryType.CLAIM,
                    OperationType.OBLIGATION_ADD: MemoryType.OBLIGATION,
                    OperationType.OBLIGATION_UPDATE: MemoryType.OBLIGATION,
                }.get(op_type)
                reference_rejection_error: str | None = None
                if proposal_id and rejected_kind is not None:
                    try:
                        touched.update(
                            self._reject_temporary_target_locked(
                                proposal_id,
                                rejected_kind,
                                reason=str(exc),
                                operation_id=operation_id,
                                actor=actor,
                            )
                        )
                    except (ValidationError, ConflictError) as reference_exc:
                        if op_type not in {
                            OperationType.ROUTE_UPDATE,
                            OperationType.OBLIGATION_UPDATE,
                        }:
                            raise
                        # Preserve the operation rejection even when a corrupt
                        # or conflicting temporary-ID reservation cannot be
                        # changed safely.  Recovery must surface that conflict;
                        # it must not guess a replacement type or target.
                        reference_rejection_error = str(reference_exc)
                result = self._finish_operation_locked(
                    operation_id,
                    status="rejected",
                    error=str(exc),
                    result={
                        "validation_error": str(exc),
                        **(
                            {}
                            if reference_rejection_error is None
                            else {
                                "temporary_reference_error": reference_rejection_error
                            }
                        ),
                    },
                )
                self._append_audit_locked(
                    "operation_rejected",
                    actor,
                    subject_id=operation_id,
                    details={
                        "operation_type": op_type.value,
                        "error": str(exc),
                        **(
                            {}
                            if reference_rejection_error is None
                            else {
                                "temporary_reference_error": reference_rejection_error
                            }
                        ),
                    },
                )
            else:
                result = self._finish_operation_locked(
                    operation_id,
                    status="committed",
                    canonical_ids=canonical_ids,
                    resolution=resolution,
                    result=details,
                )
        self._refresh_projections(touched)
        return result

    def _dispatch_operation_locked(
        self,
        operation_id: str,
        op_type: OperationType,
        payload: dict[str, Any],
        actor: str,
        *,
        proposal_id: str | None = None,
    ) -> tuple[tuple[str, ...], str, dict[str, Any], set[str]]:
        add_types = {
            OperationType.FACT: MemoryType.FACT,
            OperationType.ROUTE_ADD: MemoryType.ROUTE,
            OperationType.MEMO: MemoryType.MEMO,
            OperationType.CLAIM_ADD: MemoryType.CLAIM,
            OperationType.OBLIGATION_ADD: MemoryType.OBLIGATION,
        }
        if op_type in add_types:
            kind = add_types[op_type]
            if proposal_id:
                self._register_temporary_target_locked(
                    proposal_id, kind, operation_id=operation_id
                )
            payload, pending, deferred = self._normalize_nonfact_add_references_locked(
                kind, payload
            )
            memory_id = self._canonical_id_for_add_locked(kind, payload)
            prepared = self._prepare_add_locked(kind, memory_id, payload)
            prepared["pending_references"] = pending
            prepared["deferred_typed_updates"] = deferred
            touched, details = self._publish_prepared_locked(
                prepared, cause_id=operation_id, actor=actor
            )
            if proposal_id:
                touched.update(
                    self._resolve_temporary_target_locked(
                        proposal_id,
                        memory_id,
                        resolution="published",
                        operation_id=operation_id,
                        actor=actor,
                    )
                )
            return (memory_id,), "published", details, touched
        if op_type is OperationType.ROUTE_UPDATE:
            if proposal_id:
                self._register_temporary_target_locked(
                    proposal_id, MemoryType.ROUTE, operation_id=operation_id
                )
            target_id, details, touched = self._apply_patch_locked(
                MemoryType.ROUTE, operation_id, payload, actor
            )
            if proposal_id:
                touched.update(
                    self._resolve_temporary_target_locked(
                        proposal_id,
                        target_id,
                        resolution="updated",
                        operation_id=operation_id,
                        actor=actor,
                    )
                )
            return (target_id,), "updated", details, touched
        if op_type is OperationType.OBLIGATION_UPDATE:
            if proposal_id:
                self._register_temporary_target_locked(
                    proposal_id, MemoryType.OBLIGATION, operation_id=operation_id
                )
            target_id, details, touched = self._apply_patch_locked(
                MemoryType.OBLIGATION, operation_id, payload, actor
            )
            if proposal_id:
                touched.update(
                    self._resolve_temporary_target_locked(
                        proposal_id,
                        target_id,
                        resolution="updated",
                        operation_id=operation_id,
                        actor=actor,
                    )
                )
            return (target_id,), "updated", details, touched
        if op_type is OperationType.CLAIM_REMOVE:
            target_id, details, touched = self._remove_claim_locked(
                operation_id, payload, actor
            )
            return (target_id,), "withdrawn", details, touched
        if op_type is OperationType.OBLIGATION_REMOVE:
            target_id, details, touched = self._remove_obligation_locked(
                operation_id, payload, actor
            )
            return (target_id,), "removed", details, touched
        raise ValidationError(f"unsupported operation type: {op_type.value}")

    def add_fact(
        self, operation_id: str, payload: Mapping[str, Any], **kwargs: Any
    ) -> OperationResult:
        return self.apply_operation(operation_id, OperationType.FACT, payload, **kwargs)

    def add_route(
        self, operation_id: str, payload: Mapping[str, Any], **kwargs: Any
    ) -> OperationResult:
        return self.apply_operation(operation_id, OperationType.ROUTE_ADD, payload, **kwargs)

    def update_route(
        self, operation_id: str, payload: Mapping[str, Any], **kwargs: Any
    ) -> OperationResult:
        return self.apply_operation(operation_id, OperationType.ROUTE_UPDATE, payload, **kwargs)

    def add_memo(
        self, operation_id: str, payload: Mapping[str, Any], **kwargs: Any
    ) -> OperationResult:
        return self.apply_operation(operation_id, OperationType.MEMO, payload, **kwargs)

    def add_claim(
        self, operation_id: str, payload: Mapping[str, Any], **kwargs: Any
    ) -> OperationResult:
        return self.apply_operation(operation_id, OperationType.CLAIM_ADD, payload, **kwargs)

    def remove_claim(
        self, operation_id: str, payload: Mapping[str, Any], **kwargs: Any
    ) -> OperationResult:
        return self.apply_operation(operation_id, OperationType.CLAIM_REMOVE, payload, **kwargs)

    def add_obligation(
        self, operation_id: str, payload: Mapping[str, Any], **kwargs: Any
    ) -> OperationResult:
        return self.apply_operation(operation_id, OperationType.OBLIGATION_ADD, payload, **kwargs)

    def update_obligation(
        self, operation_id: str, payload: Mapping[str, Any], **kwargs: Any
    ) -> OperationResult:
        return self.apply_operation(operation_id, OperationType.OBLIGATION_UPDATE, payload, **kwargs)

    def remove_obligation(
        self, operation_id: str, payload: Mapping[str, Any], **kwargs: Any
    ) -> OperationResult:
        return self.apply_operation(operation_id, OperationType.OBLIGATION_REMOVE, payload, **kwargs)

    def _validate_supporting_memories_locked(self, value: Any) -> list[str]:
        ids = _id_list(value, "supporting_memory_ids")
        for memory_id in ids:
            row = self._connection.execute(
                "SELECT memory_type,active FROM memories WHERE memory_id=?", (memory_id,)
            ).fetchone()
            if row is None:
                raise ValidationError(f"supporting memory does not exist: {memory_id}")
            if row["memory_type"] == MemoryType.FACT.value and not bool(row["active"]):
                raise ValidationError(f"revoked fact cannot support an update: {memory_id}")
        return ids

    def _apply_patch_locked(
        self,
        kind: MemoryType,
        operation_id: str,
        payload: dict[str, Any],
        actor: str,
    ) -> tuple[str, dict[str, Any], set[str]]:
        allowed_top = {
            "target_id",
            "expected_base_revision",
            "set",
            "append",
            "add_ids",
            "remove_ids",
            "explanation",
            "supporting_memory_ids",
        }
        self._reject_unknown(payload, allowed_top, f"{kind.value}_update")
        target_id = _require_text(payload.get("target_id"), "target_id")
        row = self._connection.execute(
            "SELECT * FROM memories WHERE memory_id=?", (target_id,)
        ).fetchone()
        if row is None or row["memory_type"] != kind.value:
            raise ValidationError(f"update target is not a {kind.value}: {target_id}")
        if not bool(row["active"]):
            raise ValidationError(f"cannot update inactive {kind.value}: {target_id}")
        expected_revision = payload.get("expected_base_revision")
        if not isinstance(expected_revision, int) or isinstance(expected_revision, bool):
            raise ValidationError("expected_base_revision must be an integer")
        if expected_revision != row["revision"]:
            raise ConflictError(
                f"stale {kind.value} revision: expected {expected_revision}, "
                f"current {row['revision']}"
            )
        _require_text(payload.get("explanation"), "explanation")
        self._validate_supporting_memories_locked(payload.get("supporting_memory_ids", []))
        set_patch = _require_mapping(payload.get("set", {}), "set")
        append_patch = _require_mapping(payload.get("append", {}), "append")
        add_ids = _require_mapping(payload.get("add_ids", {}), "add_ids")
        remove_ids = _require_mapping(payload.get("remove_ids", {}), "remove_ids")
        metadata = _loads(row["metadata_json"], {})
        abstract = row["abstract"]
        changed = False
        touched: set[str] = {target_id}
        pending_references: list[dict[str, Any]] = []
        deferred_updates: list[dict[str, Any]] = []
        existing_view = self._get_locked(target_id)
        visible_relation_count = len(existing_view.data.get("relations", []))

        if kind is MemoryType.ROUTE:
            scalar_fields = {"abstract", "value_assessment", "progress", "next_steps", "obstacles"}
            append_fields = {"progress", "next_steps", "obstacles"}
            relationship_fields: dict[str, tuple[MemoryType, bool]] = {
                "related_obligation_ids": (MemoryType.OBLIGATION, True),
                "active_fact_ids": (MemoryType.FACT, True),
                "relevant_memo_ids": (MemoryType.MEMO, False),
                "relevant_claim_ids": (MemoryType.CLAIM, True),
            }
        else:
            scalar_fields = {"abstract", "importance", "partial_progress", "relations"}
            append_fields = {"partial_progress", "relations"}
            relationship_fields = {"related_route_ids": (MemoryType.ROUTE, False)}

        unknown_set = set(set_patch) - scalar_fields
        unknown_append = set(append_patch) - append_fields
        unknown_add = set(add_ids) - set(relationship_fields)
        unknown_remove = set(remove_ids) - set(relationship_fields)
        if unknown_set or unknown_append or unknown_add or unknown_remove:
            raise ValidationError(
                "patch contains non-whitelisted fields: "
                f"set={sorted(unknown_set)}, append={sorted(unknown_append)}, "
                f"add_ids={sorted(unknown_add)}, remove_ids={sorted(unknown_remove)}"
            )

        for field, value in set_patch.items():
            if field == "abstract":
                normalized: Any = _require_text(value, "set.abstract")
                if normalized != abstract:
                    abstract = normalized
                    changed = True
            elif field == "value_assessment":
                normalized = self._validate_value_assessment(value)
                if normalized != metadata.get(field):
                    metadata[field] = normalized
                    changed = True
            elif field == "relations":
                relation_payload, relation_pending, relation_deferred = (
                    self._normalize_nonfact_add_references_locked(
                        MemoryType.OBLIGATION,
                        {"related_route_ids": [], "relations": value},
                    )
                )
                for item in relation_pending:
                    if item.get("document_key"):
                        item["document_key"] = f"set:{item['document_key']}"
                for item in relation_deferred:
                    item["document_key"] = f"set:{item['document_key']}"
                normalized = self._validate_relations_locked(
                    relation_payload.get("relations", [])
                )
                if self._abandon_pending_source_field_locked(
                    target_id, "relations", cause_id=operation_id
                ):
                    changed = True
                pending_references.extend(relation_pending)
                deferred_updates.extend(relation_deferred)
                if normalized != metadata.get(field):
                    metadata[field] = normalized
                    changed = True
                if relation_pending:
                    changed = True
                visible_relation_count = len(normalized) + len(relation_deferred)
            else:
                normalized = _text_list(value, f"set.{field}")
                if normalized != metadata.get(field):
                    metadata[field] = normalized
                    changed = True

        for field, value in append_patch.items():
            if field == "relations":
                relation_payload, relation_pending, relation_deferred = (
                    self._normalize_nonfact_add_references_locked(
                        MemoryType.OBLIGATION,
                        {"related_route_ids": [], "relations": value},
                    )
                )
                for item in relation_pending:
                    if item.get("document_key"):
                        item["document_key"] = f"append:{item['document_key']}"
                    field_path = item.get("field_path")
                    if (
                        isinstance(field_path, list)
                        and len(field_path) > 1
                        and isinstance(field_path[1], int)
                    ):
                        field_path[1] += visible_relation_count
                for item in relation_deferred:
                    item["document_key"] = f"append:{item['document_key']}"
                additions = self._validate_relations_locked(
                    relation_payload.get("relations", [])
                )
                pending_references.extend(relation_pending)
                deferred_updates.extend(relation_deferred)
            else:
                additions = _text_list(value, f"append.{field}", allow_empty=False)
                relation_pending = []
            if not additions and not relation_pending:
                raise ValidationError(f"append.{field} must not be empty")
            if additions:
                metadata.setdefault(field, [])
                metadata[field].extend(additions)
            changed = True

        for field, (target_type, require_active) in relationship_fields.items():
            raw_additions = _id_list(add_ids.get(field, []), f"add_ids.{field}")
            removals = _id_list(remove_ids.get(field, []), f"remove_ids.{field}")
            overlap = set(raw_additions) & set(removals)
            if overlap:
                raise ValidationError(f"cannot add and remove the same IDs: {sorted(overlap)}")
            additions: list[str] = []
            field_offset = len(existing_view.data.get(field, []))
            for index, memory_id in enumerate(raw_additions):
                canonical_id, temporary_id, reference_state = (
                    self._typed_reference_resolution_locked(memory_id, target_type)
                )
                if canonical_id is not None:
                    additions.append(canonical_id)
                else:
                    assert temporary_id is not None
                    pending_references.append(
                        {
                            "temporary_id": temporary_id,
                            "expected_type": target_type,
                            "field_path": [field, field_offset + index],
                            "document_path": None,
                            "document_key": None,
                            "state": reference_state,
                        }
                    )
                    changed = True
            for memory_id in additions:
                if kind is MemoryType.ROUTE:
                    route_id, linked_id, linked_type = target_id, memory_id, target_type
                else:
                    route_id, linked_id, linked_type = memory_id, target_id, MemoryType.OBLIGATION
                if self._ensure_route_link_locked(
                    route_id, linked_id, linked_type, cause_id=operation_id
                ):
                    changed = True
                    touched.update({route_id, linked_id})
            for memory_id in removals:
                removed_overlay = self._abandon_pending_source_field_locked(
                    target_id,
                    field,
                    cause_id=operation_id,
                    temporary_id=memory_id,
                )
                if removed_overlay:
                    changed = True
                if not self._looks_canonical_id(memory_id):
                    if removed_overlay:
                        continue
                self._require_memory_locked(memory_id, target_type)
                if kind is MemoryType.ROUTE:
                    route_id, linked_id, linked_type = target_id, memory_id, target_type
                else:
                    route_id, linked_id, linked_type = memory_id, target_id, MemoryType.OBLIGATION
                if self._end_route_link_locked(
                    route_id, linked_id, linked_type, cause_id=operation_id
                ):
                    changed = True
                    touched.update({route_id, linked_id})

        if not changed:
            raise ValidationError("patch makes no effective change")
        pending_ids = {str(item["temporary_id"]) for item in pending_references}
        pending_ids.update(
            str(item["temporary_id"])
            for item in self._connection.execute(
                "SELECT temporary_id FROM temporary_memory_ids"
            ).fetchall()
        )
        self._assert_no_temporary_ids_in_prose_locked(
            {
                "explanation": payload.get("explanation"),
                "supporting_memory_ids": payload.get("supporting_memory_ids", []),
                "set": {key: value for key, value in set_patch.items() if key != "relations"},
                "append": {
                    key: value for key, value in append_patch.items() if key != "relations"
                },
            },
            pending_ids,
            typed_top_fields=set(),
        )
        self._insert_pending_references_locked(
            {
                "id": target_id,
                "kind": kind,
                "pending_references": pending_references,
                "deferred_typed_updates": deferred_updates,
            },
            cause_id=operation_id,
        )
        new_revision = int(row["revision"]) + 1
        now = _utc_now()
        self._connection.execute(
            """UPDATE memories
               SET revision=?,abstract=?,metadata_json=?,updated_at=?
               WHERE memory_id=?""",
            (new_revision, abstract, _canonical_json(metadata), now, target_id),
        )
        if kind in _SEARCHABLE_TYPES:
            title = abstract.splitlines()[0][:160]
            if kind is MemoryType.OBLIGATION:
                core = _loads(row["core_json"], {})
                title = core["statement"].splitlines()[0][:160]
                search_text = f"{abstract} {abstract} {title}"
            else:
                search_text = f"{abstract} {abstract}"
            self._connection.execute(
                """UPDATE search_documents
                   SET title=?,abstract=?,search_text=? WHERE memory_id=?""",
                (title, abstract, search_text, target_id),
            )
        self._append_audit_locked(
            f"{kind.value}_updated",
            actor,
            subject_id=target_id,
            details={
                "operation_id": operation_id,
                "base_revision": expected_revision,
                "new_revision": new_revision,
                "explanation": payload["explanation"],
                "supporting_memory_ids": payload.get("supporting_memory_ids", []),
            },
        )
        return target_id, {"canonical_id": target_id, "revision": new_revision}, touched

    def _remove_claim_locked(
        self, operation_id: str, payload: dict[str, Any], actor: str
    ) -> tuple[str, dict[str, Any], set[str]]:
        self._reject_unknown(payload, {"target_id", "replacement_id", "reason"}, "claim_remove")
        target_id = _require_text(payload.get("target_id"), "target_id")
        row = self._connection.execute(
            "SELECT memory_type,active FROM memories WHERE memory_id=?", (target_id,)
        ).fetchone()
        if row is None or row["memory_type"] != MemoryType.CLAIM.value:
            raise ValidationError(f"claim_remove target is not a claim: {target_id}")
        if not bool(row["active"]):
            raise ValidationError(f"claim is already withdrawn: {target_id}")
        replacement_id = payload.get("replacement_id")
        if replacement_id is not None:
            replacement_id = _require_text(replacement_id, "replacement_id")
            self._require_memory_locked(
                replacement_id,
                {MemoryType.CLAIM, MemoryType.FACT},
                active=True,
            )
        reason = _optional_text(payload.get("reason"), "reason", "withdrawn")
        route_rows = self._connection.execute(
            """SELECT route_id FROM route_links
               WHERE memory_id=? AND memory_type='claim' AND current=1""",
            (target_id,),
        ).fetchall()
        touched = {target_id, *(row["route_id"] for row in route_rows)}
        self._connection.execute(
            "UPDATE memories SET active=0,updated_at=? WHERE memory_id=?",
            (_utc_now(), target_id),
        )
        for route_row in route_rows:
            self._end_route_link_locked(
                route_row["route_id"],
                target_id,
                MemoryType.CLAIM,
                cause_id=operation_id,
            )
        event_id = self._append_audit_locked(
            "claim_withdrawn",
            actor,
            subject_id=target_id,
            details={
                "operation_id": operation_id,
                "reason": reason,
                "replacement_id": replacement_id,
            },
        )
        self._connection.execute(
            """INSERT INTO status_overlays(
                   memory_id,status,reason,root_cause_id,event_id,created_at
               ) VALUES(?,'withdrawn',?,?,?,?)""",
            (target_id, reason, replacement_id, event_id, _utc_now()),
        )
        return (
            target_id,
            {"canonical_id": target_id, "replacement_id": replacement_id, "event_id": event_id},
            touched,
        )

    def _remove_obligation_locked(
        self, operation_id: str, payload: dict[str, Any], actor: str
    ) -> tuple[str, dict[str, Any], set[str]]:
        allowed = {
            "target_id",
            "resolving_fact_ids",
            "refuting_fact_ids",
            "replacement_obligation_id",
            "reason",
        }
        self._reject_unknown(payload, allowed, "obligation_remove")
        target_id = _require_text(payload.get("target_id"), "target_id")
        row = self._connection.execute(
            "SELECT memory_type,active FROM memories WHERE memory_id=?", (target_id,)
        ).fetchone()
        if row is None or row["memory_type"] != MemoryType.OBLIGATION.value:
            raise ValidationError(f"obligation_remove target is not an obligation: {target_id}")
        if not bool(row["active"]):
            raise ValidationError(f"obligation is already removed: {target_id}")
        root_row = self._connection.execute(
            "SELECT root_obligation_id FROM root_resolution_state WHERE singleton=1"
        ).fetchone()
        if root_row is not None and root_row["root_obligation_id"] == target_id:
            raise ValidationError("the root obligation retains its ID and cannot be removed")
        resolving = _id_list(payload.get("resolving_fact_ids", []), "resolving_fact_ids")
        refuting = _id_list(payload.get("refuting_fact_ids", []), "refuting_fact_ids")
        for fact_id in [*resolving, *refuting]:
            self._require_memory_locked(fact_id, MemoryType.FACT, active=True)
        replacement = payload.get("replacement_obligation_id")
        if replacement is not None:
            replacement = _require_text(replacement, "replacement_obligation_id")
            self._require_memory_locked(replacement, MemoryType.OBLIGATION, active=True)
        reason = _optional_text(payload.get("reason"), "reason", "archivally removed")
        route_rows = self._connection.execute(
            """SELECT route_id FROM route_links
               WHERE memory_id=? AND memory_type='obligation' AND current=1""",
            (target_id,),
        ).fetchall()
        touched = {target_id, *(item["route_id"] for item in route_rows)}
        self._connection.execute(
            "UPDATE memories SET active=0,updated_at=? WHERE memory_id=?",
            (_utc_now(), target_id),
        )
        for route_row in route_rows:
            self._end_route_link_locked(
                route_row["route_id"],
                target_id,
                MemoryType.OBLIGATION,
                cause_id=operation_id,
            )
        event_id = self._append_audit_locked(
            "obligation_removed",
            actor,
            subject_id=target_id,
            details={
                "operation_id": operation_id,
                "reason": reason,
                "resolving_fact_ids": resolving,
                "refuting_fact_ids": refuting,
                "replacement_obligation_id": replacement,
            },
        )
        self._connection.execute(
            """INSERT INTO status_overlays(
                   memory_id,status,reason,root_cause_id,event_id,created_at
               ) VALUES(?,'removed',?,?,?,?)""",
            (target_id, reason, replacement, event_id, _utc_now()),
        )
        return (
            target_id,
            {
                "canonical_id": target_id,
                "resolving_fact_ids": resolving,
                "refuting_fact_ids": refuting,
                "replacement_obligation_id": replacement,
                "event_id": event_id,
            },
            touched,
        )

    def _substitute_temporary_ids(
        self,
        kind: MemoryType,
        payload: Mapping[str, Any],
        mapping: Mapping[str, str],
    ) -> dict[str, Any]:
        """Compatibility helper restricted to declared relationship fields."""

        try:
            return substitute_nonfact_typed_ids(kind.value, payload, mapping)
        except ValueError as exc:
            raise ValidationError(str(exc)) from exc

    def apply_operation_group(
        self,
        group_id: str,
        operations: Sequence[Mapping[str, Any]],
        *,
        actor: str = "scheduler",
    ) -> tuple[OperationResult, ...]:
        """Atomically publish a referentially closed group of non-fact adds.

        Each entry requires ``operation_id``, ``operation_type``, ``proposal_id``
        (also used as its temporary ID), and ``payload``.  Route/memo/claim/
        obligation cycles are valid; all IDs are reserved and substituted
        before validation, then all base rows are inserted before reciprocal
        links are materialized.
        """

        group_id = _require_text(group_id, "group_id")
        actor = _require_text(actor, "actor")
        if not isinstance(operations, Sequence) or isinstance(
            operations, (str, bytes, bytearray)
        ) or not operations:
            raise ValidationError("operations must be a nonempty list")
        normalized: list[dict[str, Any]] = []
        seen_operations: set[str] = set()
        seen_proposals: set[str] = set()
        allowed_ops = {
            OperationType.ROUTE_ADD: MemoryType.ROUTE,
            OperationType.MEMO: MemoryType.MEMO,
            OperationType.CLAIM_ADD: MemoryType.CLAIM,
            OperationType.OBLIGATION_ADD: MemoryType.OBLIGATION,
        }
        for index, raw in enumerate(operations):
            item = _require_mapping(raw, f"operations[{index}]")
            self._reject_unknown(
                item,
                {"operation_id", "operation_type", "proposal_id", "payload"},
                f"operations[{index}]",
            )
            operation_id = _require_text(item.get("operation_id"), f"operations[{index}].operation_id")
            proposal_id = _require_text(item.get("proposal_id"), f"operations[{index}].proposal_id")
            if operation_id in seen_operations:
                raise ValidationError(f"duplicate operation ID in group: {operation_id}")
            if proposal_id in seen_proposals:
                raise ValidationError(f"duplicate proposal ID in group: {proposal_id}")
            seen_operations.add(operation_id)
            seen_proposals.add(proposal_id)
            try:
                op_type = OperationType(item.get("operation_type"))
            except ValueError as exc:
                raise ValidationError(
                    f"unknown operation type in operations[{index}]"
                ) from exc
            if op_type not in allowed_ops:
                raise ValidationError(
                    "atomic temporary-reference groups permit only route_add, memo, "
                    "claim_add, and obligation_add"
                )
            payload = _require_mapping(item.get("payload"), f"operations[{index}].payload")
            if "id" in payload:
                raise ValidationError("group payloads must use proposal_id, not a canonical id")
            normalized.append(
                {
                    "operation_id": operation_id,
                    "operation_type": op_type,
                    "proposal_id": proposal_id,
                    "payload": payload,
                    "kind": allowed_ops[op_type],
                }
            )
        group_hash = _digest(
            [
                {
                    "operation_id": item["operation_id"],
                    "operation_type": item["operation_type"].value,
                    "proposal_id": item["proposal_id"],
                    "payload": item["payload"],
                }
                for item in normalized
            ]
        )
        touched: set[str] = set()
        results: tuple[OperationResult, ...]
        with self.transaction():
            existing_group = self._connection.execute(
                "SELECT * FROM operation_groups WHERE group_id=?", (group_id,)
            ).fetchone()
            if existing_group is not None:
                if existing_group["input_hash"] != group_hash:
                    raise IdempotencyConflict(
                        f"operation group {group_id} was replayed differently"
                    )
                rows = [
                    self._connection.execute(
                        "SELECT * FROM operations WHERE operation_id=?", (item["operation_id"],)
                    ).fetchone()
                    for item in normalized
                ]
                if any(row is None for row in rows):
                    raise ConflictError(f"operation group {group_id} is incomplete")
                return tuple(self._operation_from_row(row, replayed=True) for row in rows if row)
            now = _utc_now()
            self._connection.execute(
                """INSERT INTO operation_groups(
                       group_id,input_hash,status,result_json,created_at,updated_at
                   ) VALUES(?,?,'pending','{}',?,?)""",
                (group_id, group_hash, now, now),
            )
            for item in normalized:
                item_hash = _digest(
                    {
                        "operation_type": item["operation_type"].value,
                        "payload": item["payload"],
                        "proposal_id": item["proposal_id"],
                    }
                )
                replay = self._start_operation_locked(
                    item["operation_id"],
                    item["operation_type"].value,
                    item_hash,
                    group_id=group_id,
                )
                if replay is not None:
                    raise ConflictError(
                        f"operation {item['operation_id']} already exists outside this new group"
                    )
            try:
                with self.transaction():
                    temp_to_canonical: dict[str, str] = {}
                    pending_types: dict[str, MemoryType] = {}
                    for item in normalized:
                        canonical_id = self._allocate_id_locked(item["kind"])
                        item["canonical_id"] = canonical_id
                        temp_to_canonical[item["proposal_id"]] = canonical_id
                        pending_types[canonical_id] = item["kind"]
                        self._register_temporary_target_locked(
                            item["proposal_id"],
                            item["kind"],
                            operation_id=item["operation_id"],
                        )
                    prepared_records: list[tuple[dict[str, Any], dict[str, Any]]] = []
                    for item in normalized:
                        substituted = self._substitute_temporary_ids(
                            item["kind"], item["payload"], temp_to_canonical
                        )
                        substituted, pending, deferred = (
                            self._normalize_nonfact_add_references_locked(
                                item["kind"], substituted, pending_types
                            )
                        )
                        prepared = self._prepare_add_locked(
                            item["kind"],
                            item["canonical_id"],
                            substituted,
                            pending_types,
                        )
                        prepared["pending_references"] = pending
                        prepared["deferred_typed_updates"] = deferred
                        prepared_records.append((item, prepared))
                    for _, prepared in prepared_records:
                        self._insert_base_record_locked(prepared)
                    for _, prepared in prepared_records:
                        self._insert_specialized_indexes_locked(prepared)
                    for item, prepared in prepared_records:
                        self._insert_pending_references_locked(
                            prepared, cause_id=item["operation_id"]
                        )
                    group_results: list[dict[str, Any]] = []
                    for item, prepared in prepared_records:
                        touched.update(
                            self._insert_links_locked(
                                prepared, cause_id=item["operation_id"]
                            )
                        )
                        details = {
                            "canonical_id": prepared["id"],
                            "revision": 1,
                            "group_id": group_id,
                        }
                        self._append_audit_locked(
                            "memory_published",
                            actor,
                            subject_id=prepared["id"],
                            details={
                                "memory_type": prepared["kind"].value,
                                "operation_id": item["operation_id"],
                                "group_id": group_id,
                            },
                        )
                        self._insert_proposal_mapping_locked(
                            item["proposal_id"],
                            item["operation_id"],
                            prepared["id"],
                            "published",
                        )
                        touched.update(
                            self._resolve_temporary_target_locked(
                                item["proposal_id"],
                                prepared["id"],
                                resolution="published",
                                operation_id=item["operation_id"],
                                actor=actor,
                            )
                        )
                        self._finish_operation_locked(
                            item["operation_id"],
                            status="committed",
                            canonical_ids=(prepared["id"],),
                            resolution="published",
                            result=details,
                        )
                        group_results.append(details)
            except (ValidationError, ConflictError) as exc:
                for item in normalized:
                    touched.update(
                        self._reject_temporary_target_locked(
                            item["proposal_id"],
                            item["kind"],
                            reason=str(exc),
                            operation_id=item["operation_id"],
                            actor=actor,
                        )
                    )
                    self._finish_operation_locked(
                        item["operation_id"],
                        status="rejected",
                        result={"validation_error": str(exc), "group_id": group_id},
                        error=str(exc),
                    )
                self._connection.execute(
                    """UPDATE operation_groups SET status='rejected',error=?,result_json=?,updated_at=?
                       WHERE group_id=?""",
                    (str(exc), _canonical_json({"validation_error": str(exc)}), _utc_now(), group_id),
                )
                self._append_audit_locked(
                    "operation_group_rejected",
                    actor,
                    subject_id=group_id,
                    details={"error": str(exc)},
                )
            else:
                self._connection.execute(
                    """UPDATE operation_groups SET status='committed',result_json=?,updated_at=?
                       WHERE group_id=?""",
                    (_canonical_json(group_results), _utc_now(), group_id),
                )
                self._append_audit_locked(
                    "operation_group_committed",
                    actor,
                    subject_id=group_id,
                    details={"operation_ids": [item["operation_id"] for item in normalized]},
                )
            rows = [
                self._connection.execute(
                    "SELECT * FROM operations WHERE operation_id=?", (item["operation_id"],)
                ).fetchone()
                for item in normalized
            ]
            results = tuple(self._operation_from_row(row, replayed=False) for row in rows if row)
        self._refresh_projections(touched)
        return results

    def record_duplicate_resolution(
        self,
        operation_id: str,
        operation_type: OperationType | str,
        proposal_id: str,
        canonical_id: str,
        proposal_payload: Mapping[str, Any],
        *,
        actor: str = "scheduler",
    ) -> OperationResult:
        """Durably resolve an add proposal to existing canonical memory."""

        operation_id = _require_text(operation_id, "operation_id")
        proposal_id = _require_text(proposal_id, "proposal_id")
        try:
            op_type = operation_type if isinstance(operation_type, OperationType) else OperationType(operation_type)
        except ValueError as exc:
            raise ValidationError(f"unknown operation type: {operation_type}") from exc
        expected_type = {
            OperationType.FACT: MemoryType.FACT,
            OperationType.ROUTE_ADD: MemoryType.ROUTE,
            OperationType.OBLIGATION_ADD: MemoryType.OBLIGATION,
        }.get(op_type)
        if expected_type is None:
            raise ValidationError("duplicate synthesis resolution applies only to fact/route/obligation adds")
        payload = _require_mapping(proposal_payload, "proposal_payload")
        input_hash = _digest(
            {
                "operation_type": op_type.value,
                "payload": payload,
                "proposal_id": proposal_id,
                "duplicate_of": canonical_id,
            }
        )
        touched: set[str] = set()
        with self.transaction():
            replay = self._start_operation_locked(operation_id, op_type.value, input_hash)
            if replay is not None:
                return replay
            self._require_memory_locked(
                canonical_id,
                expected_type,
                active=expected_type in {MemoryType.FACT, MemoryType.OBLIGATION},
            )
            self._insert_proposal_mapping_locked(
                proposal_id, operation_id, canonical_id, "duplicate"
            )
            self._register_temporary_target_locked(
                proposal_id, expected_type, operation_id=operation_id
            )
            touched.update(
                self._resolve_temporary_target_locked(
                    proposal_id,
                    canonical_id,
                    resolution="duplicate",
                    operation_id=operation_id,
                    actor=actor,
                )
            )
            self._append_audit_locked(
                "proposal_deduplicated",
                actor,
                subject_id=canonical_id,
                details={"operation_id": operation_id, "proposal_id": proposal_id},
            )
            result = self._finish_operation_locked(
                operation_id,
                status="committed",
                canonical_ids=(canonical_id,),
                resolution="duplicate",
                result={"canonical_id": canonical_id},
            )
        self._refresh_projections(touched)
        return result

    def _publish_internal_record(
        self,
        idempotency_key: str,
        kind: MemoryType,
        payload: Mapping[str, Any],
        *,
        actor: str,
    ) -> OperationResult:
        operation_type = f"{kind.value}_publish"
        payload_copy = _require_mapping(payload, "payload")
        input_hash = _digest({"operation_type": operation_type, "payload": payload_copy})
        touched: set[str] = set()
        with self.transaction():
            replay = self._start_operation_locked(
                _require_text(idempotency_key, "idempotency_key"),
                operation_type,
                input_hash,
            )
            if replay is not None:
                return replay
            try:
                with self.transaction():
                    prepared_payload = payload_copy
                    pending: list[dict[str, Any]] = []
                    deferred: list[dict[str, Any]] = []
                    if kind is MemoryType.COMPUTATION:
                        prepared_payload, pending, deferred = (
                            self._normalize_nonfact_add_references_locked(
                                kind, prepared_payload
                            )
                        )
                    memory_id = self._canonical_id_for_add_locked(
                        kind, prepared_payload
                    )
                    prepared = self._prepare_add_locked(
                        kind, memory_id, prepared_payload
                    )
                    prepared["pending_references"] = pending
                    prepared["deferred_typed_updates"] = deferred
                    touched, details = self._publish_prepared_locked(
                        prepared, cause_id=idempotency_key, actor=actor
                    )
            except (ValidationError, ConflictError) as exc:
                result = self._finish_operation_locked(
                    idempotency_key,
                    status="rejected",
                    result={"validation_error": str(exc)},
                    error=str(exc),
                )
            else:
                result = self._finish_operation_locked(
                    idempotency_key,
                    status="committed",
                    canonical_ids=(memory_id,),
                    resolution="published",
                    result=details,
                )
        self._refresh_projections(touched)
        return result

    def publish_task(
        self,
        idempotency_key: str,
        payload: Mapping[str, Any],
        *,
        actor: str = "scheduler",
    ) -> OperationResult:
        return self._publish_internal_record(
            idempotency_key, MemoryType.TASK, payload, actor=actor
        )

    add_task = publish_task

    def publish_computation(
        self,
        idempotency_key: str,
        payload: Mapping[str, Any],
        *,
        actor: str = "scheduler",
    ) -> OperationResult:
        # No update API exists by design; a correction is another C-* record.
        return self._publish_internal_record(
            idempotency_key, MemoryType.COMPUTATION, payload, actor=actor
        )

    add_computation = publish_computation

    def update_fact_metadata(
        self,
        operation_id: str,
        fact_id: str,
        *,
        expected_metadata_version: int,
        abstract: str | None = None,
        keywords: Sequence[str] | None = None,
        actor: str = "scheduler",
    ) -> OperationResult:
        """Update only the scheduler-owned mutable fact metadata.

        Route attachment changes are made through reciprocal route operations;
        this method cannot alter the immutable fact core or graph edges.
        """

        payload = {
            "fact_id": fact_id,
            "expected_metadata_version": expected_metadata_version,
            "abstract": abstract,
            "keywords": list(keywords) if keywords is not None else None,
        }
        input_hash = _digest(payload)
        touched: set[str] = set()
        with self.transaction():
            replay = self._start_operation_locked(
                operation_id, "fact_metadata_update", input_hash
            )
            if replay is not None:
                return replay
            try:
                with self.transaction():
                    row = self._connection.execute(
                        "SELECT * FROM memories WHERE memory_id=?", (fact_id,)
                    ).fetchone()
                    if row is None or row["memory_type"] != MemoryType.FACT.value:
                        raise ValidationError(f"not a fact: {fact_id}")
                    if row["metadata_version"] != expected_metadata_version:
                        raise ConflictError(
                            f"stale fact metadata version: expected {expected_metadata_version}, "
                            f"current {row['metadata_version']}"
                        )
                    metadata = _loads(row["metadata_json"], {})
                    new_abstract = row["abstract"]
                    changed = False
                    if abstract is not None:
                        normalized_abstract = _require_text(abstract, "abstract")
                        if normalized_abstract != new_abstract:
                            new_abstract = normalized_abstract
                            changed = True
                    if keywords is not None:
                        normalized_keywords = _text_list(keywords, "keywords")
                        if normalized_keywords != metadata.get("keywords", []):
                            metadata["keywords"] = normalized_keywords
                            changed = True
                    if not changed:
                        raise ValidationError("fact metadata update makes no effective change")
                    new_version = int(row["metadata_version"]) + 1
                    self._connection.execute(
                        """UPDATE memories SET abstract=?,metadata_json=?,metadata_version=?,updated_at=?
                           WHERE memory_id=?""",
                        (
                            new_abstract,
                            _canonical_json(metadata),
                            new_version,
                            _utc_now(),
                            fact_id,
                        ),
                    )
                    core = _loads(row["core_json"], {})
                    title = core["statement"].splitlines()[0][:160]
                    search_text = " ".join(
                        [new_abstract, new_abstract, title, *metadata.get("keywords", [])]
                    )
                    self._connection.execute(
                        """UPDATE search_documents SET abstract=?,search_text=?
                           WHERE memory_id=?""",
                        (new_abstract, search_text, fact_id),
                    )
                    self._append_audit_locked(
                        "fact_metadata_updated",
                        actor,
                        subject_id=fact_id,
                        details={
                            "operation_id": operation_id,
                            "base_metadata_version": expected_metadata_version,
                            "new_metadata_version": new_version,
                        },
                    )
                    details = {
                        "canonical_id": fact_id,
                        "metadata_version": new_version,
                    }
                    touched = {fact_id}
            except (ValidationError, ConflictError) as exc:
                result = self._finish_operation_locked(
                    operation_id,
                    status="rejected",
                    result={"validation_error": str(exc)},
                    error=str(exc),
                )
            else:
                result = self._finish_operation_locked(
                    operation_id,
                    status="committed",
                    canonical_ids=(fact_id,),
                    resolution="metadata_updated",
                    result=details,
                )
        self._refresh_projections(touched)
        return result

    def record_route_task_attempt(
        self,
        route_id: str,
        task_id: str,
        *,
        actor: str = "scheduler",
    ) -> bool:
        """Add one scheduler-derived route/task-history edge idempotently."""

        with self.transaction():
            self._require_memory_locked(route_id, MemoryType.ROUTE)
            self._require_allocated_task_locked(task_id)
            cursor = self._connection.execute(
                """INSERT OR IGNORE INTO route_task_history(route_id,task_id,recorded_at)
                   VALUES(?,?,?)""",
                (route_id, task_id, _utc_now()),
            )
            if cursor.rowcount:
                self._append_audit_locked(
                    "route_task_attempt_recorded",
                    actor,
                    subject_id=route_id,
                    details={"task_id": task_id},
                )
                changed = True
            else:
                changed = False
        if changed:
            self._refresh_projections({route_id})
        return changed

    # ------------------------------------------------------------------
    # Composed reads, access filtering, and read audits

    def _current_route_ids_for_memory_locked(
        self, memory_id: str, memory_type: MemoryType
    ) -> list[str]:
        rows = self._connection.execute(
            """SELECT route_id FROM route_links
               WHERE memory_id=? AND memory_type=? AND current=1 ORDER BY route_id""",
            (memory_id, memory_type.value),
        ).fetchall()
        return [row["route_id"] for row in rows]

    def _current_route_targets_locked(
        self, route_id: str, memory_type: MemoryType
    ) -> list[str]:
        rows = self._connection.execute(
            """SELECT memory_id FROM route_links
               WHERE route_id=? AND memory_type=? AND current=1 ORDER BY memory_id""",
            (route_id, memory_type.value),
        ).fetchall()
        return [row["memory_id"] for row in rows]

    def _status_locked(
        self,
        row: sqlite3.Row,
        core: Mapping[str, Any],
    ) -> str:
        kind = MemoryType(row["memory_type"])
        if kind is MemoryType.FACT:
            return "active" if bool(row["active"]) else "revoked"
        if kind is MemoryType.CLAIM:
            return "active" if bool(row["active"]) else "withdrawn"
        if kind is MemoryType.OBLIGATION:
            if not bool(row["active"]):
                return "removed"
            root = self._connection.execute(
                "SELECT * FROM root_resolution_state WHERE singleton=1"
            ).fetchone()
            if root is not None and root["root_obligation_id"] == row["memory_id"]:
                if root["status"] == "resolved":
                    return "resolved"
                if root["status"] == "needs_attention":
                    return "resolution_conflict"
            predecessors = core.get("predecessor_fact_ids", [])
            if predecessors:
                placeholders = ",".join("?" for _ in predecessors)
                inactive = self._connection.execute(
                    f"""SELECT 1 FROM memories
                        WHERE memory_id IN ({placeholders}) AND active=0 LIMIT 1""",
                    tuple(predecessors),
                ).fetchone()
                if inactive is not None:
                    return "unsupported"
            return "active"
        return "active" if bool(row["active"]) else "inactive"

    @staticmethod
    def _soft_reference_display_value(reference: sqlite3.Row) -> str:
        state = str(reference["state"])
        if state == "resolved" and reference["canonical_id"]:
            return str(reference["canonical_id"])
        temporary_id = str(reference["temporary_id"])
        if state in {"rejected", "abandoned"}:
            return f"{_strip_unpublished_suffix(temporary_id)}{_UNPUBLISHED_SUFFIX}"
        return temporary_id

    def _overlay_simple_soft_references_locked(
        self,
        data: dict[str, Any],
        reference_rows: Sequence[sqlite3.Row],
    ) -> None:
        """Project simple list slots at their durable original indexes."""

        grouped: dict[tuple[str, ...], list[tuple[int, sqlite3.Row]]] = {}
        for reference in reference_rows:
            if reference["document_id"] is not None:
                continue
            path = _loads(reference["field_path_json"], [])
            if (
                not isinstance(path, list)
                or len(path) < 2
                or not all(isinstance(component, str) for component in path[:-1])
                or not isinstance(path[-1], int)
            ):
                continue
            grouped.setdefault(tuple(path[:-1]), []).append(
                (max(0, path[-1]), reference)
            )

        for parent_path, indexed_rows in grouped.items():
            container: dict[str, Any] = data
            valid_container = True
            for component in parent_path[:-1]:
                nested = container.get(component)
                if not isinstance(nested, dict):
                    valid_container = False
                    break
                container = nested
            if not valid_container:
                continue
            field = parent_path[-1]
            current = container.get(field, [])
            if not isinstance(current, list):
                continue
            resolved_ids = {
                str(reference["canonical_id"])
                for _, reference in indexed_rows
                if reference["state"] == "resolved" and reference["canonical_id"]
            }
            ledger_only = parent_path[0] == "related_memory_ids"
            projected = (
                list(current)
                if ledger_only
                else [item for item in current if item not in resolved_ids]
            )
            ordered = sorted(
                indexed_rows,
                key=lambda item: (
                    item[0],
                    str(item[1]["created_at"]),
                    str(item[1]["reference_id"]),
                ),
            )
            unique: list[tuple[int, str]] = []
            seen: set[tuple[str, str]] = set()
            for index, reference in ordered:
                if reference["state"] == "resolved" and reference["canonical_id"]:
                    identity = ("canonical", str(reference["canonical_id"]))
                    if ledger_only and str(reference["canonical_id"]) in projected:
                        continue
                else:
                    identity = ("temporary", str(reference["temporary_id"]))
                if identity in seen:
                    continue
                seen.add(identity)
                unique.append((index, self._soft_reference_display_value(reference)))

            same_index_counts: dict[int, int] = {}
            for index, display in unique:
                offset = same_index_counts.get(index, 0)
                projected.insert(min(index + offset, len(projected)), display)
                same_index_counts[index] = offset + 1
            container[field] = projected

    def _overlay_deferred_relations_locked(
        self,
        source_id: str,
        relations: Sequence[Mapping[str, Any]],
    ) -> list[dict[str, Any]]:
        """Reconstruct pending, terminal, and resolved relation documents."""

        projected = [copy.deepcopy(dict(item)) for item in relations]
        documents = self._connection.execute(
            """SELECT * FROM deferred_typed_updates
               WHERE source_memory_id=? AND state<>'removed'
               ORDER BY created_at,document_id""",
            (source_id,),
        ).fetchall()
        candidates: list[tuple[int, str, str, dict[str, Any]]] = []
        for document in documents:
            references = self._connection.execute(
                """SELECT * FROM pending_memory_references
                   WHERE document_id=? AND state<>'removed'
                   ORDER BY created_at,reference_id""",
                (document["document_id"],),
            ).fetchall()
            if not references:
                continue
            template = _loads(document["template_json"], {})
            if not isinstance(template, dict):
                continue
            relation_indexes: list[int] = []
            for reference in references:
                document_path = _loads(reference["document_path_json"], None)
                field_path = _loads(reference["field_path_json"], [])
                if isinstance(document_path, list):
                    self._set_exact_path(
                        template,
                        document_path,
                        self._soft_reference_display_value(reference),
                    )
                if (
                    isinstance(field_path, list)
                    and len(field_path) > 1
                    and field_path[0] == "relations"
                    and isinstance(field_path[1], int)
                ):
                    relation_indexes.append(max(0, field_path[1]))
            self._dedupe_relation_reference_lists(template)
            if document["state"] == "resolved":
                for index, existing in enumerate(projected):
                    if existing == template:
                        projected.pop(index)
                        break
            relation_index = min(relation_indexes) if relation_indexes else len(projected)
            candidates.append(
                (
                    relation_index,
                    str(document["created_at"]),
                    str(document["document_id"]),
                    template,
                )
            )

        seen_candidates: set[str] = set()
        same_index_counts: dict[int, int] = {}
        for index, _, _, relation in sorted(candidates, key=lambda item: item[:3]):
            identity = _canonical_json(relation)
            if identity in seen_candidates:
                continue
            seen_candidates.add(identity)
            offset = same_index_counts.get(index, 0)
            projected.insert(min(index + offset, len(projected)), relation)
            same_index_counts[index] = offset + 1
        return projected

    def _relation_view_locked(self, relation: Mapping[str, Any]) -> dict[str, Any]:
        result = copy.deepcopy(dict(relation))
        supporting = result.get("supporting_fact_ids", [])
        inactive_support: list[str] = []
        for fact_id in supporting:
            row = self._connection.execute(
                "SELECT active FROM memories WHERE memory_id=? AND memory_type='fact'",
                (fact_id,),
            ).fetchone()
            if row is None or not bool(row["active"]):
                inactive_support.append(fact_id)
        inactive_targets: list[str] = []
        for memory_id in [*result.get("premise_memory_ids", [])]:
            row = self._connection.execute(
                "SELECT active FROM memories WHERE memory_id=?", (memory_id,)
            ).fetchone()
            if row is None or not bool(row["active"]):
                inactive_targets.append(memory_id)
        conclusion = result.get("conclusion")
        if conclusion != "ROOT":
            row = self._connection.execute(
                "SELECT active FROM memories WHERE memory_id=?", (conclusion,)
            ).fetchone()
            if row is None or not bool(row["active"]):
                inactive_targets.append(conclusion)
        result["evidence_status"] = (
            "established" if supporting and not inactive_support else "conjectural"
        )
        result["inactive_supporting_fact_ids"] = sorted(inactive_support)
        result["inactive_endpoint_ids"] = sorted(set(inactive_targets))
        return result

    def _get_locked(self, memory_id: str) -> MemoryRecord:
        row = self._connection.execute(
            "SELECT * FROM memories WHERE memory_id=?", (memory_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"canonical memory not found: {memory_id}")
        kind = MemoryType(row["memory_type"])
        core = _loads(row["core_json"], {})
        metadata = _loads(row["metadata_json"], {})
        status = self._status_locked(row, core)
        data: dict[str, Any] = {**core, **metadata}
        if kind in _SEARCHABLE_TYPES:
            data["abstract"] = row["abstract"]
        if kind is MemoryType.FACT:
            data["related_route_ids"] = self._current_route_ids_for_memory_locked(
                memory_id, MemoryType.FACT
            )
        elif kind is MemoryType.ROUTE:
            data["related_obligation_ids"] = self._current_route_targets_locked(
                memory_id, MemoryType.OBLIGATION
            )
            data["active_fact_ids"] = self._current_route_targets_locked(
                memory_id, MemoryType.FACT
            )
            data["relevant_memo_ids"] = self._current_route_targets_locked(
                memory_id, MemoryType.MEMO
            )
            data["relevant_claim_ids"] = self._current_route_targets_locked(
                memory_id, MemoryType.CLAIM
            )
            task_rows = self._connection.execute(
                """SELECT task_id FROM route_task_history
                   WHERE route_id=? ORDER BY recorded_at,task_id""",
                (memory_id,),
            ).fetchall()
            data["task_history_ids"] = [item["task_id"] for item in task_rows]
        elif kind in {MemoryType.MEMO, MemoryType.CLAIM}:
            data["related_route_ids"] = self._current_route_ids_for_memory_locked(
                memory_id, kind
            )
        elif kind is MemoryType.OBLIGATION:
            data["related_route_ids"] = self._current_route_ids_for_memory_locked(
                memory_id, MemoryType.OBLIGATION
            )
            data["relations"] = copy.deepcopy(metadata.get("relations", []))
        if kind in {
            MemoryType.FACT,
            MemoryType.ROUTE,
            MemoryType.MEMO,
            MemoryType.CLAIM,
            MemoryType.OBLIGATION,
            MemoryType.COMPUTATION,
        }:
            reference_rows = self._connection.execute(
                """SELECT * FROM pending_memory_references
                   WHERE source_memory_id=? AND state<>'removed'
                   ORDER BY created_at,reference_id""",
                (memory_id,),
            ).fetchall()
            self._overlay_simple_soft_references_locked(data, reference_rows)
            if kind is MemoryType.OBLIGATION:
                projected_relations = self._overlay_deferred_relations_locked(
                    memory_id, data.get("relations", [])
                )
                data["relations"] = [
                    self._relation_view_locked(item) for item in projected_relations
                ]
            if kind is MemoryType.COMPUTATION:
                statuses: dict[str, str] = {}
                for ids in data.get("related_memory_ids", {}).values():
                    for related_id in ids:
                        related_row = self._connection.execute(
                            "SELECT * FROM memories WHERE memory_id=?",
                            (related_id,),
                        ).fetchone()
                        if related_row is not None:
                            related_core = _loads(related_row["core_json"], {})
                            statuses[related_id] = self._status_locked(
                                related_row, related_core
                            )
                data["related_memory_statuses"] = dict(sorted(statuses.items()))
            data["pending_references"] = [
                {
                    "field_path": _loads(item["field_path_json"], []),
                    "expected_type": item["expected_type"],
                    "temporary_id": item["temporary_id"],
                    "state": item["state"],
                }
                for item in reference_rows
                if item["state"] in {"pending", "rejected", "abandoned"}
            ]
        return MemoryRecord(
            memory_id=memory_id,
            memory_type=kind,
            revision=int(row["revision"]),
            metadata_version=int(row["metadata_version"]),
            active=bool(row["active"]),
            status=status,
            data=data,
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def get(self, memory_id: str) -> MemoryRecord:
        """Internal canonical read without an agent read-audit entry."""

        with self._lock:
            return self._get_locked(memory_id)

    def _record_read_audit_locked(
        self,
        *,
        action: str,
        actor: str,
        policy_label: str,
        subject_id: str | None = None,
        query: Mapping[str, Any] | None = None,
        result_ids: Sequence[str] | None = None,
        allowed: bool,
    ) -> None:
        self._connection.execute(
            """INSERT INTO read_audit(
                   action,actor,policy_label,subject_id,query_json,result_ids_json,allowed,created_at
               ) VALUES(?,?,?,?,?,?,?,?)""",
            (
                action,
                actor,
                policy_label,
                subject_id,
                None if query is None else _canonical_json(dict(query)),
                None if result_ids is None else _canonical_json(list(result_ids)),
                int(allowed),
                _utc_now(),
            ),
        )

    def fetch(
        self,
        memory_id: str,
        *,
        actor: str,
        access_policy: AccessPolicy | None = None,
    ) -> MemoryRecord:
        """Audited full-record fetch subject to a mechanical access policy."""

        policy = access_policy or AccessPolicy(label="unrestricted-active")
        missing = False
        denied = False
        record: MemoryRecord | None = None
        with self.transaction():
            try:
                record = self._get_locked(memory_id)
            except NotFoundError:
                self._record_read_audit_locked(
                    action="fetch",
                    actor=actor,
                    policy_label=policy.label,
                    subject_id=memory_id,
                    allowed=False,
                )
                missing = True
            else:
                allowed = policy.permits(record.memory_id, record.memory_type, record.status)
                self._record_read_audit_locked(
                    action="fetch",
                    actor=actor,
                    policy_label=policy.label,
                    subject_id=memory_id,
                    allowed=allowed,
                )
                denied = not allowed
        if missing:
            raise NotFoundError(f"canonical memory not found: {memory_id}")
        if denied:
            raise AccessDeniedError(f"access denied to canonical memory {memory_id}")
        assert record is not None
        return record

    def list_records(
        self,
        *,
        types: Iterable[MemoryType | str] | None = None,
        include_inactive: bool = True,
    ) -> list[MemoryRecord]:
        parsed = None if types is None else {self._parse_memory_type(item) for item in types}
        with self._lock:
            if parsed:
                placeholders = ",".join("?" for _ in parsed)
                rows = self._connection.execute(
                    f"SELECT memory_id FROM memories WHERE memory_type IN ({placeholders}) "
                    "ORDER BY memory_type,memory_id",
                    tuple(sorted(item.value for item in parsed)),
                ).fetchall()
            else:
                rows = self._connection.execute(
                    "SELECT memory_id FROM memories ORDER BY memory_type,memory_id"
                ).fetchall()
            records = [self._get_locked(row["memory_id"]) for row in rows]
        if include_inactive:
            return records
        return [record for record in records if record.active]

    def _search_candidates(
        self, types: set[MemoryType] | None = None
    ) -> list[dict[str, Any]]:
        with self._lock:
            if types:
                placeholders = ",".join("?" for _ in types)
                rows = self._connection.execute(
                    f"SELECT * FROM search_documents WHERE memory_type IN ({placeholders}) "
                    "ORDER BY memory_id",
                    tuple(sorted(item.value for item in types)),
                ).fetchall()
            else:
                rows = self._connection.execute(
                    "SELECT * FROM search_documents ORDER BY memory_id"
                ).fetchall()
            result: list[dict[str, Any]] = []
            for row in rows:
                record = self._get_locked(row["memory_id"])
                result.append(
                    {
                        "memory_id": row["memory_id"],
                        "memory_type": MemoryType(row["memory_type"]),
                        "title": row["title"],
                        "abstract": row["abstract"],
                        "search_text": row["search_text"],
                        "status": record.status,
                    }
                )
            return result

    def _audit_search(
        self,
        *,
        actor: str,
        policy: AccessPolicy,
        query: Mapping[str, Any],
        result_ids: Sequence[str],
    ) -> None:
        with self.transaction():
            self._record_read_audit_locked(
                action="search",
                actor=actor,
                policy_label=policy.label,
                query=query,
                result_ids=result_ids,
                allowed=True,
            )

    def list_read_audit(self, *, after_seq: int = 0) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM read_audit WHERE audit_seq>? ORDER BY audit_seq", (after_seq,)
            ).fetchall()
        return [
            {
                "audit_seq": row["audit_seq"],
                "action": row["action"],
                "actor": row["actor"],
                "policy_label": row["policy_label"],
                "subject_id": row["subject_id"],
                "query": _loads(row["query_json"]),
                "result_ids": _loads(row["result_ids_json"]),
                "allowed": bool(row["allowed"]),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def status_history(self, memory_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute(
                """SELECT * FROM status_overlays
                   WHERE memory_id=? ORDER BY overlay_seq""",
                (memory_id,),
            ).fetchall()
        return [
            {
                "status": row["status"],
                "reason": row["reason"],
                "root_cause_id": row["root_cause_id"],
                "dependency_path": _loads(row["dependency_path_json"]),
                "event_id": row["event_id"],
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def projection_path(self, memory_id: str) -> Path | None:
        if self.projection_dir is None:
            return None
        record = self.get(memory_id)
        return self.projection_dir / projection_relative_path(record)

    def _refresh_projections(self, memory_ids: Iterable[str]) -> None:
        if self.projection_dir is None:
            return
        for memory_id in sorted(set(memory_ids)):
            try:
                record = self.get(memory_id)
            except NotFoundError:
                continue
            target = self.projection_dir / projection_relative_path(record)
            target.parent.mkdir(parents=True, exist_ok=True)
            content = render_record(record)
            descriptor, temp_name = tempfile.mkstemp(
                prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
            )
            try:
                with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
                    handle.write(content)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temp_name, target)
            finally:
                if os.path.exists(temp_name):
                    os.unlink(temp_name)

    def rebuild_projections(self) -> list[Path]:
        if self.projection_dir is None:
            return []
        records = self.list_records()
        self._refresh_projections(record.memory_id for record in records)
        return [
            self.projection_dir / projection_relative_path(record)
            for record in records
        ]

    # ------------------------------------------------------------------
    # Root state and transitive fact revocation

    def set_root_obligation(
        self, obligation_id: str, *, actor: str = "scheduler"
    ) -> None:
        """Set the immutable root-obligation identity after bootstrap."""

        with self.transaction():
            self._require_memory_locked(obligation_id, MemoryType.OBLIGATION, active=True)
            row = self._connection.execute(
                "SELECT * FROM root_resolution_state WHERE singleton=1"
            ).fetchone()
            if row is not None:
                if row["root_obligation_id"] != obligation_id:
                    raise ConflictError(
                        f"root obligation is already {row['root_obligation_id']}"
                    )
                return
            self._connection.execute(
                """INSERT INTO root_resolution_state(
                       singleton,root_obligation_id,status,updated_at
                   ) VALUES(1,?,'open',?)""",
                (obligation_id, _utc_now()),
            )
            self._append_audit_locked(
                "root_obligation_configured",
                actor,
                subject_id=obligation_id,
                details={},
            )
        self._refresh_projections({obligation_id})

    def root_resolution_state(self) -> dict[str, Any] | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM root_resolution_state WHERE singleton=1"
            ).fetchone()
            if row is None:
                return None
            resolution_rows = self._connection.execute(
                """SELECT rr.fact_id,rr.outcome,rr.is_primary,m.active
                   FROM root_resolutions rr JOIN memories m ON m.memory_id=rr.fact_id
                   ORDER BY rr.committed_at,rr.fact_id"""
            ).fetchall()
        return {
            "root_obligation_id": row["root_obligation_id"],
            "root_solution_fact_id": row["solution_fact_id"],
            "root_resolution_outcome": row["outcome"],
            "status": row["status"],
            "resolutions": [
                {
                    "fact_id": item["fact_id"],
                    "outcome": item["outcome"],
                    "is_primary": bool(item["is_primary"]),
                    "active": bool(item["active"]),
                }
                for item in resolution_rows
            ],
            "updated_at": row["updated_at"],
        }

    def _descendant_paths_locked(self, root_fact_id: str) -> dict[str, list[str]]:
        paths: dict[str, list[str]] = {root_fact_id: [root_fact_id]}
        queue = deque([root_fact_id])
        while queue:
            predecessor = queue.popleft()
            children = self._connection.execute(
                """SELECT fact_id FROM fact_dependencies
                   WHERE predecessor_id=? ORDER BY fact_id""",
                (predecessor,),
            ).fetchall()
            for child_row in children:
                child = child_row["fact_id"]
                if child not in paths:
                    paths[child] = [*paths[predecessor], child]
                    queue.append(child)
        return paths

    def _refresh_root_state_after_revocation_locked(
        self, revoked_ids: set[str]
    ) -> tuple[bool, str | None]:
        state = self._connection.execute(
            "SELECT * FROM root_resolution_state WHERE singleton=1"
        ).fetchone()
        if state is None:
            return False, None
        primary_id = state["solution_fact_id"]
        if primary_id is None:
            return False, state["root_obligation_id"]
        if primary_id in revoked_ids:
            # A separately verified proof of the same outcome keeps ROOT
            # resolved.  Promote the earliest surviving alternate rather than
            # coupling project truth to one particular proof record.
            replacement = self._connection.execute(
                """SELECT rr.fact_id FROM root_resolutions rr
                   JOIN memories m ON m.memory_id=rr.fact_id
                   WHERE m.active=1 AND rr.outcome=?
                   ORDER BY rr.committed_at,rr.fact_id LIMIT 1""",
                (state["outcome"],),
            ).fetchone()
            if replacement is not None:
                replacement_id = replacement["fact_id"]
                opposite = self._connection.execute(
                    """SELECT 1 FROM root_resolutions rr
                       JOIN memories m ON m.memory_id=rr.fact_id
                       WHERE m.active=1 AND rr.outcome<>? LIMIT 1""",
                    (state["outcome"],),
                ).fetchone()
                desired = "needs_attention" if opposite is not None else "resolved"
                self._connection.execute(
                    "UPDATE root_resolutions SET is_primary=0 WHERE is_primary=1"
                )
                self._connection.execute(
                    "UPDATE root_resolutions SET is_primary=1 WHERE fact_id=?",
                    (replacement_id,),
                )
                self._connection.execute(
                    """UPDATE root_resolution_state
                       SET solution_fact_id=?,status=?,updated_at=? WHERE singleton=1""",
                    (replacement_id, desired, _utc_now()),
                )
                return False, state["root_obligation_id"]
            active_opposite = self._connection.execute(
                """SELECT 1 FROM root_resolutions rr
                   JOIN memories m ON m.memory_id=rr.fact_id
                   WHERE m.active=1 LIMIT 1"""
            ).fetchone()
            desired = "needs_attention" if active_opposite is not None else "open"
            self._connection.execute(
                """UPDATE root_resolution_state
                   SET solution_fact_id=NULL,outcome=NULL,status=?,updated_at=?
                   WHERE singleton=1""",
                (desired, _utc_now()),
            )
            return True, state["root_obligation_id"]
        opposite = self._connection.execute(
            """SELECT 1 FROM root_resolutions rr
               JOIN memories m ON m.memory_id=rr.fact_id
               WHERE m.active=1 AND rr.outcome<>? LIMIT 1""",
            (state["outcome"],),
        ).fetchone()
        desired = "needs_attention" if opposite is not None else "resolved"
        if desired != state["status"]:
            self._connection.execute(
                "UPDATE root_resolution_state SET status=?,updated_at=? WHERE singleton=1",
                (desired, _utc_now()),
            )
        return False, state["root_obligation_id"]

    def revoke_fact(
        self,
        fact_id: str,
        *,
        reason: str,
        actor: str,
        evidence: Mapping[str, Any] | None = None,
        operation_id: str | None = None,
    ) -> RevocationResult:
        """Atomically revoke a fact and all active dependency descendants."""

        fact_id = _require_text(fact_id, "fact_id")
        reason = _require_text(reason, "reason")
        actor = _require_text(actor, "actor")
        evidence_dict = _require_mapping(evidence or {}, "evidence")
        operation_id = operation_id or f"REVREQ-{uuid.uuid4().hex}"
        input_hash = _digest(
            {
                "fact_id": fact_id,
                "reason": reason,
                "actor": actor,
                "evidence": evidence_dict,
            }
        )
        touched: set[str] = set()
        with self.transaction():
            replay = self._start_operation_locked(
                operation_id, "fact_revoke", input_hash
            )
            if replay is not None:
                details = dict(replay.result)
                return RevocationResult(
                    challenged_fact_id=fact_id,
                    revoked_fact_ids=tuple(details.get("revoked_fact_ids", [])),
                    affected_obligation_ids=tuple(
                        details.get("affected_obligation_ids", [])
                    ),
                    affected_route_ids=tuple(details.get("affected_route_ids", [])),
                    root_resolution_cleared=bool(
                        details.get("root_resolution_cleared", False)
                    ),
                    event_id=details.get("event_id", ""),
                    replayed=True,
                )
            row = self._connection.execute(
                "SELECT memory_type,active FROM memories WHERE memory_id=?", (fact_id,)
            ).fetchone()
            if row is None or row["memory_type"] != MemoryType.FACT.value:
                raise ValidationError(f"revocation target is not a fact: {fact_id}")
            if not bool(row["active"]):
                prior = self._connection.execute(
                    "SELECT event_id FROM revocations WHERE fact_id=?", (fact_id,)
                ).fetchone()
                details = {
                    "revoked_fact_ids": [],
                    "affected_obligation_ids": [],
                    "affected_route_ids": [],
                    "root_resolution_cleared": False,
                    "event_id": "" if prior is None else prior["event_id"],
                    "already_revoked": True,
                }
                self._finish_operation_locked(
                    operation_id,
                    status="committed",
                    canonical_ids=(fact_id,),
                    resolution="already_revoked",
                    result=details,
                )
                return RevocationResult(
                    challenged_fact_id=fact_id,
                    revoked_fact_ids=(),
                    affected_obligation_ids=(),
                    affected_route_ids=(),
                    root_resolution_cleared=False,
                    event_id=details["event_id"],
                )
            paths = self._descendant_paths_locked(fact_id)
            active_revoked: list[str] = []
            for candidate_id in paths:
                active_row = self._connection.execute(
                    "SELECT active FROM memories WHERE memory_id=?", (candidate_id,)
                ).fetchone()
                if active_row is not None and bool(active_row["active"]):
                    active_revoked.append(candidate_id)
            active_revoked.sort(key=lambda item: (len(paths[item]), item))
            revoked_set = set(active_revoked)
            placeholders = ",".join("?" for _ in active_revoked)
            route_rows = self._connection.execute(
                f"""SELECT DISTINCT route_id FROM route_links
                    WHERE memory_type='fact' AND current=1
                      AND memory_id IN ({placeholders}) ORDER BY route_id""",
                tuple(active_revoked),
            ).fetchall()
            affected_routes = [item["route_id"] for item in route_rows]
            obligation_rows = self._connection.execute(
                f"""SELECT DISTINCT op.obligation_id
                    FROM obligation_predecessors op
                    JOIN memories o ON o.memory_id=op.obligation_id
                    WHERE o.active=1 AND op.fact_id IN ({placeholders})
                    ORDER BY op.obligation_id""",
                tuple(active_revoked),
            ).fetchall()
            affected_obligations = [item["obligation_id"] for item in obligation_rows]
            event_id = f"REV-{uuid.uuid4().hex}"
            self._append_audit_locked(
                "fact_revoked",
                actor,
                subject_id=fact_id,
                details={
                    "reason": reason,
                    "evidence": evidence_dict,
                    "revoked_fact_ids": active_revoked,
                    "operation_id": operation_id,
                },
                event_id=event_id,
            )
            now = _utc_now()
            for revoked_id in active_revoked:
                path = paths[revoked_id]
                self._connection.execute(
                    "UPDATE memories SET active=0,updated_at=? WHERE memory_id=?",
                    (now, revoked_id),
                )
                self._connection.execute(
                    """INSERT INTO revocations(
                           fact_id,root_fact_id,reason,evidence_json,actor,
                           dependency_path_json,event_id,revoked_at
                       ) VALUES(?,?,?,?,?,?,?,?)""",
                    (
                        revoked_id,
                        fact_id,
                        reason,
                        _canonical_json(evidence_dict),
                        actor,
                        _canonical_json(path),
                        event_id,
                        now,
                    ),
                )
                self._connection.execute(
                    """INSERT INTO status_overlays(
                           memory_id,status,reason,root_cause_id,dependency_path_json,
                           event_id,created_at
                       ) VALUES(?,'revoked',?,?,?,?,?)""",
                    (revoked_id, reason, fact_id, _canonical_json(path), event_id, now),
                )
                link_rows = self._connection.execute(
                    """SELECT route_id FROM route_links
                       WHERE memory_id=? AND memory_type='fact' AND current=1""",
                    (revoked_id,),
                ).fetchall()
                for link_row in link_rows:
                    self._end_route_link_locked(
                        link_row["route_id"],
                        revoked_id,
                        MemoryType.FACT,
                        cause_id=event_id,
                    )
            for obligation_id in affected_obligations:
                cause_fact = self._connection.execute(
                    f"""SELECT fact_id FROM obligation_predecessors
                        WHERE obligation_id=? AND fact_id IN ({placeholders})
                        ORDER BY fact_id LIMIT 1""",
                    (obligation_id, *active_revoked),
                ).fetchone()["fact_id"]
                self._connection.execute(
                    """INSERT INTO status_overlays(
                           memory_id,status,reason,root_cause_id,dependency_path_json,
                           event_id,created_at
                       ) VALUES(?,'unsupported',?,?,?,?,?)""",
                    (
                        obligation_id,
                        f"predecessor {cause_fact} was revoked",
                        fact_id,
                        _canonical_json(paths[cause_fact]),
                        event_id,
                        now,
                    ),
                )
            root_cleared, root_obligation_id = self._refresh_root_state_after_revocation_locked(
                revoked_set
            )
            touched = {
                *active_revoked,
                *affected_routes,
                *affected_obligations,
            }
            if root_obligation_id:
                touched.add(root_obligation_id)
            details = {
                "revoked_fact_ids": active_revoked,
                "affected_obligation_ids": affected_obligations,
                "affected_route_ids": affected_routes,
                "root_resolution_cleared": root_cleared,
                "event_id": event_id,
            }
            self._finish_operation_locked(
                operation_id,
                status="committed",
                canonical_ids=tuple(active_revoked),
                resolution="revoked",
                result=details,
            )
        self._refresh_projections(touched)
        return RevocationResult(
            challenged_fact_id=fact_id,
            revoked_fact_ids=tuple(active_revoked),
            affected_obligation_ids=tuple(affected_obligations),
            affected_route_ids=tuple(affected_routes),
            root_resolution_cleared=root_cleared,
            event_id=event_id,
        )

    def fact_predecessors(self, fact_id: str) -> tuple[str, ...]:
        with self._lock:
            self._require_memory_locked(fact_id, MemoryType.FACT)
            rows = self._connection.execute(
                """SELECT predecessor_id FROM fact_dependencies
                   WHERE fact_id=? ORDER BY predecessor_id""",
                (fact_id,),
            ).fetchall()
        return tuple(row["predecessor_id"] for row in rows)

    def facts_using_external_reference(
        self, stable_identifier_or_url: str, *, active_only: bool = True
    ) -> tuple[str, ...]:
        key = _require_text(stable_identifier_or_url, "stable_identifier_or_url")
        with self._lock:
            if active_only:
                rows = self._connection.execute(
                    """SELECT DISTINCT er.fact_id FROM fact_external_references er
                       JOIN memories m ON m.memory_id=er.fact_id
                       WHERE er.reference_key=? AND m.active=1 ORDER BY er.fact_id""",
                    (key,),
                ).fetchall()
            else:
                rows = self._connection.execute(
                    """SELECT DISTINCT fact_id FROM fact_external_references
                       WHERE reference_key=? ORDER BY fact_id""",
                    (key,),
                ).fetchall()
        return tuple(row["fact_id"] for row in rows)

    def facts_using_foundation_version(
        self, foundation_policy_version: str | int, *, active_only: bool = True
    ) -> tuple[str, ...]:
        version = str(foundation_policy_version)
        with self._lock:
            if active_only:
                rows = self._connection.execute(
                    """SELECT fv.fact_id FROM fact_foundation_versions fv
                       JOIN memories m ON m.memory_id=fv.fact_id
                       WHERE fv.foundation_version=? AND m.active=1 ORDER BY fv.fact_id""",
                    (version,),
                ).fetchall()
            else:
                rows = self._connection.execute(
                    """SELECT fact_id FROM fact_foundation_versions
                       WHERE foundation_version=? ORDER BY fact_id""",
                    (version,),
                ).fetchall()
        return tuple(row["fact_id"] for row in rows)

    def fact_descendants(self, fact_id: str, *, active_only: bool = False) -> tuple[str, ...]:
        with self._lock:
            self._require_memory_locked(fact_id, MemoryType.FACT)
            paths = self._descendant_paths_locked(fact_id)
            ids = [item for item in paths if item != fact_id]
            if active_only:
                ids = [
                    item
                    for item in ids
                    if bool(
                        self._connection.execute(
                            "SELECT active FROM memories WHERE memory_id=?", (item,)
                        ).fetchone()["active"]
                    )
                ]
        return tuple(sorted(ids, key=lambda item: (len(paths[item]), item)))

    # ------------------------------------------------------------------
    # Scheduler-private durable state (never exposed as project memory)

    def load_control_state(self, key: str) -> tuple[int, dict[str, Any]] | None:
        key = _require_text(key, "key")
        with self._lock:
            row = self._connection.execute(
                "SELECT revision,payload_json FROM control_state WHERE state_key=?", (key,)
            ).fetchone()
        if row is None:
            return None
        return int(row["revision"]), _loads(row["payload_json"], {})

    def load_control_state_object(self, key: str) -> ControlState | None:
        loaded = self.load_control_state(key)
        if loaded is None:
            return None
        revision, payload = loaded
        return ControlState(revision=revision, payload=payload)

    def compare_and_swap_control_state(
        self,
        key: str,
        expected_revision: int | None,
        payload: Mapping[str, Any],
    ) -> int:
        """CAS a scheduler-private JSON value and return its new revision.

        Creation requires ``expected_revision`` to be ``None`` or ``0``.  An
        existing value requires its exact positive revision; using ``None`` is
        never an unconditional overwrite.
        """

        key = _require_text(key, "key")
        payload_dict = _require_mapping(payload, "payload")
        payload_json = _canonical_json(payload_dict)
        with self.transaction():
            row = self._connection.execute(
                "SELECT revision FROM control_state WHERE state_key=?", (key,)
            ).fetchone()
            if row is None:
                if expected_revision not in (None, 0):
                    raise ConflictError(
                        f"control state {key} does not exist; expected revision must be None or 0"
                    )
                new_revision = 1
                self._connection.execute(
                    """INSERT INTO control_state(state_key,revision,payload_json,updated_at)
                       VALUES(?,?,?,?)""",
                    (key, new_revision, payload_json, _utc_now()),
                )
            else:
                current = int(row["revision"])
                if expected_revision != current:
                    raise ConflictError(
                        f"control state {key} revision conflict: expected "
                        f"{expected_revision}, current {current}"
                    )
                new_revision = current + 1
                cursor = self._connection.execute(
                    """UPDATE control_state SET revision=?,payload_json=?,updated_at=?
                       WHERE state_key=? AND revision=?""",
                    (new_revision, payload_json, _utc_now(), key, current),
                )
                if cursor.rowcount != 1:
                    raise ConflictError(f"control state {key} changed concurrently")
        return new_revision

    def save_control_state(self, key: str, payload: Mapping[str, Any]) -> int:
        """Convenience single-process upsert; CAS is preferred for coordination."""

        with self._lock:
            row = self._connection.execute(
                "SELECT revision FROM control_state WHERE state_key=?", (key,)
            ).fetchone()
            expected = None if row is None else int(row["revision"])
        return self.compare_and_swap_control_state(key, expected, payload)

    def append_control_event(
        self, idempotency_key: str, event: Mapping[str, Any]
    ) -> bool:
        """Append an opaque scheduler event; return False on identical replay."""

        idempotency_key = _require_text(idempotency_key, "idempotency_key")
        event_dict = _require_mapping(event, "event")
        event_json = _canonical_json(event_dict)
        input_hash = hashlib.sha256(event_json.encode("utf-8")).hexdigest()
        with self.transaction():
            row = self._connection.execute(
                "SELECT input_hash FROM control_events WHERE idempotency_key=?",
                (idempotency_key,),
            ).fetchone()
            if row is not None:
                if row["input_hash"] != input_hash:
                    raise IdempotencyConflict(
                        f"control event {idempotency_key} was replayed differently"
                    )
                return False
            self._connection.execute(
                """INSERT INTO control_events(idempotency_key,input_hash,event_json,created_at)
                   VALUES(?,?,?,?)""",
                (idempotency_key, input_hash, event_json, _utc_now()),
            )
        return True

    def list_control_events(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM control_events ORDER BY created_at,idempotency_key"
            ).fetchall()
        return [
            {
                "idempotency_key": row["idempotency_key"],
                "event": _loads(row["event_json"], {}),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def search(self, query: str, **kwargs: Any) -> list[Any]:
        """Convenience wrapper around Block 5's read-access search engine."""

        from .read_access.search import SearchEngine

        return SearchEngine(self).search(query, **kwargs)

    def as_memory_backend(self) -> "StoreMemoryBackend":
        """Return a zero-copy adapter for Block 5's audited memory API."""

        return StoreMemoryBackend(self)

    def iter_records(self, memory_types: frozenset[str]) -> Iterable[Any]:
        """MemoryBackend-compatible convenience method.

        Values are generated from canonical rows on every call; no second copy
        of project memory is maintained.
        """

        return self.as_memory_backend().iter_records(memory_types)

    def get_record(self, memory_id: str) -> Any | None:
        """MemoryBackend-compatible canonical lookup."""

        return self.as_memory_backend().get_record(memory_id)


class StoreMemoryBackend:
    """Adapt :class:`MemoryStore` to Block 5's ``MemoryBackend`` protocol.

    The import of the agent-facing record occurs only while adapting a result,
    avoiding an import cycle and keeping SQLite as the sole data authority.
    """

    def __init__(self, store: MemoryStore) -> None:
        self.store = store

    def _adapt(self, record: MemoryRecord) -> Any:
        from .contracts.agent_access import MemoryRecord as AccessMemoryRecord

        data = record.to_dict()
        abstract = str(
            data.get("abstract")
            or data.get("final_summary")
            or data.get("description")
            or ""
        )
        if record.memory_type is MemoryType.FACT:
            title = str(data.get("statement", abstract)).splitlines()[0][:160]
            predecessor_ids = tuple(data.get("predecessor_fact_ids", []))
        elif record.memory_type is MemoryType.OBLIGATION:
            title = str(data.get("statement", abstract)).splitlines()[0][:160]
            predecessor_ids = ()
        else:
            title = abstract.splitlines()[0][:160]
            predecessor_ids = ()
        return AccessMemoryRecord(
            memory_id=record.memory_id,
            memory_type=record.memory_type.value,
            abstract=abstract,
            content=render_record(record),
            title=title,
            active=record.active,
            withdrawn=record.status == "withdrawn",
            revision=record.revision,
            predecessor_ids=predecessor_ids,
            metadata=data,
        )

    def iter_records(self, memory_types: frozenset[str]) -> Iterable[Any]:
        try:
            parsed = {MemoryType(item) for item in memory_types}
        except ValueError as exc:
            raise ValidationError("MemoryBackend requested an unknown memory type") from exc
        return (self._adapt(record) for record in self.store.list_records(types=parsed))

    def get_record(self, memory_id: str) -> Any | None:
        try:
            return self._adapt(self.store.get(memory_id))
        except NotFoundError:
            return None
