"""Block 5: audited, policy-filtered read access and sealed materialization."""

from .audit import AuditLog
from .materialization import (
    EXPLORER_SNAPSHOT_FORMAT_VERSION,
    EXPLORER_SNAPSHOT_RELATIVE_PATH,
    MaterializationError,
    MaterializedWorkspace,
    MemorySnapshot,
    WorkspaceMaterializer,
    explorer_snapshot_digest,
    validate_explorer_snapshot,
)
from .memory import AccessError, AuditedMemoryAPI, InMemoryBackend, MemoryBackend
from .search import SearchEngine, search_memory, tokenize
from .snapshots import (
    ASSIGNMENT_MEMORY_TYPES,
    CATEGORY_MEMBER_TYPES,
    SnapshotValidationError,
    assignment_snapshots_from_task_card,
    snapshot_from_record,
    unique_memory_snapshots,
    validate_assignment_portfolio,
    validate_category_portfolio_snapshot,
    validate_event_id,
    validate_event_window,
    validate_memory_snapshot,
    validate_record_reference,
)
from .tasks import AuditedTaskAPI, TaskBackend

__all__ = [
    "AccessError",
    "ASSIGNMENT_MEMORY_TYPES",
    "AuditLog",
    "AuditedMemoryAPI",
    "AuditedTaskAPI",
    "CATEGORY_MEMBER_TYPES",
    "EXPLORER_SNAPSHOT_FORMAT_VERSION",
    "EXPLORER_SNAPSHOT_RELATIVE_PATH",
    "InMemoryBackend",
    "MaterializationError",
    "MaterializedWorkspace",
    "MemoryBackend",
    "MemorySnapshot",
    "SearchEngine",
    "SnapshotValidationError",
    "TaskBackend",
    "WorkspaceMaterializer",
    "assignment_snapshots_from_task_card",
    "explorer_snapshot_digest",
    "search_memory",
    "snapshot_from_record",
    "tokenize",
    "unique_memory_snapshots",
    "validate_assignment_portfolio",
    "validate_category_portfolio_snapshot",
    "validate_event_id",
    "validate_event_window",
    "validate_explorer_snapshot",
    "validate_memory_snapshot",
    "validate_record_reference",
]
