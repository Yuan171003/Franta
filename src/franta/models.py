"""Compatibility facade for canonical contracts.

New production code imports from :mod:`franta.contracts.canonical`.  These
aliases preserve existing callers and, importantly, preserve object identity.
"""

from __future__ import annotations

from .contracts.canonical import (
    AccessDeniedError,
    AccessPolicy,
    CATEGORY_PREFIX,
    ConflictError,
    ControlState,
    IdempotencyConflict,
    ImmutableRecordError,
    MEMORY_PREFIXES,
    MemoryRecord,
    MemoryType,
    NotFoundError,
    OperationResult,
    OperationType,
    RevocationResult,
    SearchResult,
    StoreError,
    ValidationError,
)

__all__ = [
    "AccessDeniedError",
    "AccessPolicy",
    "CATEGORY_PREFIX",
    "ConflictError",
    "ControlState",
    "IdempotencyConflict",
    "ImmutableRecordError",
    "MEMORY_PREFIXES",
    "MemoryRecord",
    "MemoryType",
    "NotFoundError",
    "OperationResult",
    "OperationType",
    "RevocationResult",
    "SearchResult",
    "StoreError",
    "ValidationError",
]
