"""Canonical-memory terminology and value-object contracts.

The database is intentionally the source of truth.  These classes are small,
immutable views used at the boundary of :mod:`franta.store`; they are not an
alternative object database and do not acquire write methods.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Any, Iterable, Iterator, Mapping


class MemoryType(str, Enum):
    """The seven and only seven canonical memory types."""

    FACT = "fact"
    ROUTE = "route"
    MEMO = "memo"
    CLAIM = "claim"
    OBLIGATION = "obligation"
    TASK = "task"
    COMPUTATION = "computation"


class OperationType(str, Enum):
    """The nine worker-proposable memory operations."""

    FACT = "fact"
    ROUTE_UPDATE = "route_update"
    ROUTE_ADD = "route_add"
    MEMO = "memo"
    CLAIM_ADD = "claim_add"
    CLAIM_REMOVE = "claim_remove"
    OBLIGATION_ADD = "obligation_add"
    OBLIGATION_UPDATE = "obligation_update"
    OBLIGATION_REMOVE = "obligation_remove"


MEMORY_PREFIXES: Mapping[MemoryType, str] = MappingProxyType(
    {
        MemoryType.FACT: "F",
        MemoryType.ROUTE: "R",
        MemoryType.MEMO: "M",
        MemoryType.CLAIM: "CL",
        MemoryType.OBLIGATION: "O",
        MemoryType.TASK: "T",
        MemoryType.COMPUTATION: "C",
    }
)
CATEGORY_PREFIX = "CAT"


class StoreError(RuntimeError):
    """Base class for canonical-store errors."""


class ValidationError(StoreError):
    """The proposed data violates a schema or a canonical invariant."""


class NotFoundError(StoreError):
    """A requested canonical object does not exist."""


class ConflictError(StoreError):
    """A revision or compare-and-swap precondition failed."""


class IdempotencyConflict(ConflictError):
    """An idempotency key was replayed with different input."""


class AccessDeniedError(StoreError):
    """The requested record is outside the supplied access policy."""


class ImmutableRecordError(StoreError):
    """An operation attempted to change immutable canonical data."""


@dataclass(frozen=True)
class AccessPolicy:
    """A mechanical read filter.

    ``None`` means unrestricted for the corresponding dimension.  An empty
    set means no access.  Status flags are deliberately independent of the ID
    and type filters so callers cannot accidentally expose historical facts or
    withdrawn claims merely by naming their type.
    """

    allowed_ids: frozenset[str] | None = None
    allowed_types: frozenset[MemoryType] | None = None
    allow_inactive_facts: bool = False
    allow_withdrawn_claims: bool = False
    allow_removed_obligations: bool = False
    label: str = "default"

    @classmethod
    def sealed(
        cls,
        ids: Iterable[str],
        *,
        types: Iterable[MemoryType | str] | None = None,
        label: str = "sealed",
    ) -> "AccessPolicy":
        parsed_types = None
        if types is not None:
            parsed_types = frozenset(MemoryType(item) for item in types)
        return cls(
            allowed_ids=frozenset(ids),
            allowed_types=parsed_types,
            label=label,
        )

    def permits(
        self,
        memory_id: str,
        memory_type: MemoryType | str,
        status: str,
    ) -> bool:
        kind = MemoryType(memory_type)
        if self.allowed_ids is not None and memory_id not in self.allowed_ids:
            return False
        if self.allowed_types is not None and kind not in self.allowed_types:
            return False
        if kind is MemoryType.FACT and status == "revoked":
            return self.allow_inactive_facts
        if kind is MemoryType.CLAIM and status == "withdrawn":
            return self.allow_withdrawn_claims
        if kind is MemoryType.OBLIGATION and status == "removed":
            return self.allow_removed_obligations
        return True


@dataclass(frozen=True)
class MemoryRecord(Mapping[str, Any]):
    """An immutable, fully composed canonical-memory view."""

    memory_id: str
    memory_type: MemoryType
    revision: int
    active: bool
    status: str
    data: Mapping[str, Any]
    created_at: str
    updated_at: str
    metadata_version: int = 1
    _view: Mapping[str, Any] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        composed = {
            "id": self.memory_id,
            "type": self.memory_type.value,
            "revision": self.revision,
            "metadata_version": self.metadata_version,
            "active": self.active,
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            **dict(self.data),
        }
        object.__setattr__(self, "_view", MappingProxyType(composed))
        object.__setattr__(self, "data", MappingProxyType(dict(self.data)))

    def __getitem__(self, key: str) -> Any:
        return self._view[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._view)

    def __len__(self) -> int:
        return len(self._view)

    def to_dict(self) -> dict[str, Any]:
        return dict(self._view)


@dataclass(frozen=True)
class OperationResult:
    operation_id: str
    operation_type: str
    status: str
    canonical_ids: tuple[str, ...] = ()
    resolution: str | None = None
    result: Mapping[str, Any] = field(default_factory=dict)
    error: str | None = None
    replayed: bool = False

    @property
    def canonical_id(self) -> str | None:
        return self.canonical_ids[0] if len(self.canonical_ids) == 1 else None


@dataclass(frozen=True)
class RevocationResult:
    challenged_fact_id: str
    revoked_fact_ids: tuple[str, ...]
    affected_obligation_ids: tuple[str, ...]
    affected_route_ids: tuple[str, ...]
    root_resolution_cleared: bool
    event_id: str
    replayed: bool = False


@dataclass(frozen=True)
class SearchResult:
    """Abstract-first search result; intentionally contains no full record."""

    memory_id: str
    memory_type: MemoryType
    display_title: str
    abstract: str
    status: str
    score: float
    rank: int


@dataclass(frozen=True)
class ControlState:
    revision: int
    payload: Mapping[str, Any]

    def __iter__(self) -> Iterator[Any]:
        # Allows ``revision, payload = load_control_state(...)`` while keeping
        # named attributes for callers that prefer them.
        yield self.revision
        yield dict(self.payload)


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
