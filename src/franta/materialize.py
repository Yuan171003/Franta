"""Compatibility facade for Block 5 sealed-workspace materialization."""

from .read_access.materialization import (
    EXPLORER_SNAPSHOT_FORMAT_VERSION,
    EXPLORER_SNAPSHOT_RELATIVE_PATH,
    MaterializationError,
    MaterializedWorkspace,
    MemorySnapshot,
    WorkspaceMaterializer,
    explorer_snapshot_digest,
    validate_explorer_snapshot,
)

__all__ = [
    "EXPLORER_SNAPSHOT_FORMAT_VERSION",
    "EXPLORER_SNAPSHOT_RELATIVE_PATH",
    "MaterializationError",
    "MaterializedWorkspace",
    "MemorySnapshot",
    "WorkspaceMaterializer",
    "explorer_snapshot_digest",
    "validate_explorer_snapshot",
]
