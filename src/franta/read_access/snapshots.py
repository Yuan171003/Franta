"""Pure validation for read-side memory and portfolio snapshots.

The state-owning blocks decide what belongs in a category or assignment and
commit their choices.  This module only checks already selected, read-only
views.  Optional exact revision, status, and cursor arguments are deliberately
caller supplied: Read Access does not invent a consistency point or advance
any durable cursor.
"""

from __future__ import annotations

import copy
import os
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

from ..contracts.agent_access import MEMORY_TYPES
from ..contracts.canonical import CATEGORY_PREFIX, MEMORY_PREFIXES


ASSIGNMENT_MEMORY_TYPES = (
    "fact",
    "route",
    "memo",
    "claim",
    "obligation",
    "computation",
)
CATEGORY_MEMBER_TYPES = ASSIGNMENT_MEMORY_TYPES[:-1]

_MEMORY_PREFIXES = {memory_type.value: prefix for memory_type, prefix in MEMORY_PREFIXES.items()}
_MEMORY_ID_RE = re.compile(r"^(?:F|R|M|CL|O|T|C)-[A-Za-z0-9][A-Za-z0-9_.:-]*$")
_CATEGORY_ID_RE = re.compile(
    rf"^{re.escape(CATEGORY_PREFIX)}-[A-Za-z0-9][A-Za-z0-9_.:-]*$"
)
_UNSET = object()


class SnapshotValidationError(ValueError):
    """A selected read snapshot is malformed, stale, or internally inconsistent."""


@dataclass(frozen=True)
class MemorySnapshot:
    """An immutable copied view suitable for sealed workspace materialization."""

    memory_id: str
    memory_type: str
    abstract: str
    content: str
    revision: int | None = None
    status: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # Preserve the original materialization boundary: exact type/revision
        # checks are opt-in through ``validate_memory_snapshot`` below.
        if not _MEMORY_ID_RE.fullmatch(self.memory_id):
            raise ValueError(f"invalid canonical memory ID: {self.memory_id!r}")
        if self.memory_type not in MEMORY_TYPES:
            raise ValueError(f"unknown memory type: {self.memory_type!r}")
        if isinstance(self.content, os.PathLike):
            raise TypeError("materialization accepts copied content, never canonical paths")

    def summary(self) -> dict[str, Any]:
        return {
            "id": self.memory_id,
            "memory_type": self.memory_type,
            "abstract": self.abstract,
            "revision": self.revision,
            "status": self.status,
        }


def _as_mapping(value: Any, label: str) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return copy.deepcopy(dict(value))
    if hasattr(value, "to_dict"):
        return copy.deepcopy(dict(value.to_dict()))
    if hasattr(value, "__dict__"):
        return copy.deepcopy(vars(value))
    raise SnapshotValidationError(
        f"{label} must be mapping-like, got {type(value).__name__}"
    )


def _memory_type(value: Any) -> str:
    result = str(value)
    if result.startswith("MemoryType."):
        result = result.rsplit(".", 1)[-1].lower()
    return result


def _revision(value: Any, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise SnapshotValidationError(f"{label} must be a positive integer")
    return value


def validate_event_id(value: Any, field_name: str) -> str | int:
    """Validate one persisted event ID without interpreting scheduler state."""

    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise SnapshotValidationError(
            f"{field_name} must be a string or integer event ID"
        )
    if isinstance(value, str):
        if not value.strip():
            raise SnapshotValidationError(f"{field_name} must not be empty")
        return value.strip()
    if value < 0:
        raise SnapshotValidationError(f"{field_name} must be nonnegative")
    return value


def validate_event_window(
    base_event_id: Any,
    confirmed_through_event_id: Any,
    *,
    expected_base_event_id: str | int | object = _UNSET,
    expected_confirmed_through_event_id: str | int | object = _UNSET,
) -> tuple[str | int, str | int]:
    """Validate a snapshot's event window and optional exact cursor bindings."""

    base = validate_event_id(base_event_id, "portfolio.base_event_id")
    confirmed = validate_event_id(
        confirmed_through_event_id,
        "portfolio.confirmed_through_event_id",
    )
    if isinstance(base, int) and isinstance(confirmed, int) and confirmed < base:
        raise SnapshotValidationError(
            "confirmed_through_event_id cannot precede base_event_id"
        )
    if expected_base_event_id is not _UNSET and base != expected_base_event_id:
        raise SnapshotValidationError(
            f"portfolio base event mismatch: expected {expected_base_event_id}, got {base}"
        )
    if (
        expected_confirmed_through_event_id is not _UNSET
        and confirmed != expected_confirmed_through_event_id
    ):
        raise SnapshotValidationError(
            "portfolio confirmed-through event mismatch: "
            f"expected {expected_confirmed_through_event_id}, got {confirmed}"
        )
    return base, confirmed


def validate_memory_snapshot(
    snapshot: MemorySnapshot,
    *,
    expected_id: str | None = None,
    expected_type: str | None = None,
    expected_revision: int | None | object = _UNSET,
    expected_status: str | None | object = _UNSET,
) -> MemorySnapshot:
    """Validate the exact identity and any caller-bound snapshot fields."""

    if not isinstance(snapshot, MemorySnapshot):
        raise SnapshotValidationError("memory snapshot has the wrong object type")
    expected_prefix = _MEMORY_PREFIXES[snapshot.memory_type]
    if not snapshot.memory_id.startswith(f"{expected_prefix}-"):
        raise SnapshotValidationError(
            f"snapshot ID {snapshot.memory_id} does not match type {snapshot.memory_type}"
        )
    if snapshot.revision is not None:
        _revision(snapshot.revision, f"snapshot {snapshot.memory_id} revision")
    if snapshot.status is not None and (
        not isinstance(snapshot.status, str) or not snapshot.status.strip()
    ):
        raise SnapshotValidationError(
            f"snapshot {snapshot.memory_id} status must be a nonempty string"
        )
    if expected_id is not None and snapshot.memory_id != expected_id:
        raise SnapshotValidationError(
            f"snapshot ID mismatch: expected {expected_id}, got {snapshot.memory_id}"
        )
    if expected_type is not None and snapshot.memory_type != expected_type:
        raise SnapshotValidationError(
            f"snapshot {snapshot.memory_id} is {snapshot.memory_type}, expected {expected_type}"
        )
    if expected_revision is not _UNSET and snapshot.revision != expected_revision:
        raise SnapshotValidationError(
            f"snapshot {snapshot.memory_id} revision mismatch: "
            f"expected {expected_revision}, got {snapshot.revision}"
        )
    if expected_status is not _UNSET and snapshot.status != expected_status:
        raise SnapshotValidationError(
            f"snapshot {snapshot.memory_id} status mismatch: "
            f"expected {expected_status}, got {snapshot.status}"
        )
    return snapshot


def snapshot_from_record(
    record: Any,
    *,
    content: str,
    requested_id: str | None = None,
    exact: bool = False,
) -> MemorySnapshot:
    """Build a copied snapshot from one current record.

    ``exact=False`` preserves the current ID-only/latest-record launch
    behavior.  Revision-pinned callers can opt into binding every record field
    with ``exact=True``.
    """

    data = _as_mapping(record, "memory record")
    record_id = str(data.get("id", ""))
    if exact and requested_id is not None and record_id != requested_id:
        raise SnapshotValidationError(
            f"snapshot ID mismatch: expected {requested_id}, got {record_id}"
        )
    memory_id = requested_id if requested_id is not None else record_id
    memory_type = str(data["type"])
    abstract = str(
        data.get("abstract")
        or data.get("final_summary")
        or data.get("description")
        or ""
    )
    snapshot = MemorySnapshot(
        memory_id=memory_id,
        memory_type=memory_type,
        abstract=abstract,
        content=content,
        revision=int(data["revision"]),
        status=str(data["status"]),
    )
    if exact:
        return validate_memory_snapshot(
            snapshot,
            expected_id=requested_id,
            expected_type=memory_type,
            expected_revision=int(data["revision"]),
            expected_status=str(data["status"]),
        )
    return snapshot


def validate_record_reference(
    record: Any,
    *,
    memory_id: str,
    expected_type: str,
    active_fact: bool,
    exact_id: bool = False,
    expected_revision: int | object = _UNSET,
    expected_status: str | object = _UNSET,
) -> dict[str, Any]:
    """Validate a fetched record against a selected portfolio reference.

    Revision and status bindings are optional because the current assignment
    wire format persists IDs only.  Callers with a revision-pinned artifact can
    bind those fields explicitly without changing the ID-only behavior.
    """

    data = _as_mapping(record, f"portfolio record {memory_id}")
    if exact_id and str(data.get("id", "")) != memory_id:
        raise SnapshotValidationError(
            f"portfolio record ID mismatch: expected {memory_id}, got {data.get('id')}"
        )
    if exact_id and not memory_id.startswith(f"{_MEMORY_PREFIXES[expected_type]}-"):
        raise SnapshotValidationError(
            f"portfolio ID {memory_id} does not match type {expected_type}"
        )
    actual = _memory_type(data.get("type", data.get("memory_type", expected_type)))
    if actual != expected_type:
        raise SnapshotValidationError(
            f"portfolio ID {memory_id} is {actual}, expected {expected_type}"
        )
    if active_fact:
        status = data.get("status")
        active = data.get("active", status not in {"revoked", "inactive"})
        if not active or status in {"revoked", "inactive"}:
            raise SnapshotValidationError(
                f"assignment may not materialize inactive fact {memory_id}"
            )
    if expected_revision is not _UNSET:
        current_revision = data.get("revision")
        if current_revision != expected_revision:
            raise SnapshotValidationError(
                f"portfolio ID {memory_id} revision mismatch: "
                f"expected {expected_revision}, got {current_revision}"
            )
    if expected_status is not _UNSET and data.get("status") != expected_status:
        raise SnapshotValidationError(
            f"portfolio ID {memory_id} status mismatch: "
            f"expected {expected_status}, got {data.get('status')}"
        )
    return data


def validate_assignment_portfolio(
    portfolio: Mapping[str, Any],
    *,
    record_lookup: Callable[[str], Any] | None,
) -> None:
    """Validate the current ID-only assignment portfolio against fetched records."""

    expected_groups = set(ASSIGNMENT_MEMORY_TYPES)
    for group, ids in portfolio.items():
        if group not in expected_groups:
            raise SnapshotValidationError(f"unknown portfolio memory group {group!r}")
        if not isinstance(ids, list):
            raise SnapshotValidationError(f"portfolio group {group!r} must be a list")
        for raw_id in ids:
            memory_id = str(raw_id)
            if record_lookup is None:
                raise SnapshotValidationError(
                    "Store must provide get() for portfolio validation"
                )
            try:
                record = record_lookup(memory_id)
            except Exception as exc:
                raise SnapshotValidationError(
                    f"invalid {group} ID {memory_id}: {exc}"
                ) from exc
            if record is None:
                raise SnapshotValidationError(f"unknown {group} ID {memory_id}")
            validate_record_reference(
                record,
                memory_id=memory_id,
                expected_type=group,
                active_fact=group == "fact",
            )


def unique_memory_snapshots(
    snapshots: Sequence[MemorySnapshot],
) -> tuple[MemorySnapshot, ...]:
    """Return a snapshot sequence after the legacy duplicate-ID check."""

    seen: set[str] = set()
    result: list[MemorySnapshot] = []
    for snapshot in snapshots:
        if snapshot.memory_id in seen:
            raise SnapshotValidationError(
                f"duplicate portfolio ID: {snapshot.memory_id}"
            )
        seen.add(snapshot.memory_id)
        result.append(snapshot)
    return tuple(result)


def assignment_snapshots_from_task_card(
    task_card: Mapping[str, Any],
    mode: str,
    *,
    snapshot_lookup: Callable[[str], MemorySnapshot],
) -> list[MemorySnapshot]:
    """Resolve the current ID-only task portfolio for materialization.

    This intentionally retains the existing latest-record behavior.  It does
    not reinterpret the task card as a revision-pinned artifact.
    """

    values: list[MemorySnapshot] = []
    seen: set[str] = set()
    for ids in (task_card.get("portfolio") or {}).values():
        for memory_id in ids or []:
            if memory_id not in seen:
                values.append(snapshot_lookup(str(memory_id)))
                seen.add(str(memory_id))
    if mode == "proof-writer":
        root_fact_id = task_card.get("root_solution_fact_id")
        if root_fact_id and root_fact_id not in seen:
            values.append(snapshot_lookup(str(root_fact_id)))
    return values


def _typed_members(value: Any, label: str) -> dict[str, list[str]]:
    if not isinstance(value, Mapping) or set(value) != set(CATEGORY_MEMBER_TYPES):
        raise SnapshotValidationError(
            f"{label} must contain exactly fact/route/memo/claim/obligation"
        )
    result: dict[str, list[str]] = {}
    for memory_type in CATEGORY_MEMBER_TYPES:
        ids = value[memory_type]
        if not isinstance(ids, list) or not all(isinstance(item, str) for item in ids):
            raise SnapshotValidationError(f"{label}.{memory_type} must be an ID list")
        if len(set(ids)) != len(ids):
            raise SnapshotValidationError(f"{label}.{memory_type} contains duplicate IDs")
        prefix = _MEMORY_PREFIXES[memory_type]
        for memory_id in ids:
            if not _MEMORY_ID_RE.fullmatch(memory_id) or not memory_id.startswith(
                f"{prefix}-"
            ):
                raise SnapshotValidationError(
                    f"{label}.{memory_type} contains mistyped ID {memory_id}"
                )
        result[memory_type] = list(ids)
    return result


def _category_at_revision(
    category_id: str,
    revision: int,
    current_categories: Mapping[str, Mapping[str, Any]],
    category_history: Mapping[str, Sequence[Mapping[str, Any]]],
) -> Mapping[str, Any] | None:
    current = current_categories.get(category_id)
    if current is not None and current.get("revision") == revision:
        return current
    for historical in category_history.get(category_id, ()):
        if historical.get("revision") == revision:
            return historical
    return None


def validate_category_portfolio_snapshot(
    snapshot: Mapping[str, Any],
    *,
    current_categories: Mapping[str, Mapping[str, Any]] | None = None,
    category_history: Mapping[str, Sequence[Mapping[str, Any]]] | None = None,
    expected_portfolio_revision: int | None = None,
    expected_base_event_id: str | int | object = _UNSET,
    expected_confirmed_through_event_id: str | int | object = _UNSET,
    allow_missing_categories: bool = False,
) -> None:
    """Validate a committed category portfolio as an exact read snapshot."""

    raw = _as_mapping(snapshot, "category portfolio snapshot")
    snapshot_id = raw.get("snapshot_id")
    if not isinstance(snapshot_id, str) or not snapshot_id.startswith("CP-"):
        raise SnapshotValidationError("category portfolio has an invalid snapshot ID")
    revision = _revision(raw.get("revision"), "category portfolio revision")
    if expected_portfolio_revision is not None and revision != expected_portfolio_revision:
        raise SnapshotValidationError(
            f"category portfolio revision mismatch: expected {expected_portfolio_revision}, "
            f"got {revision}"
        )
    categories = raw.get("categories")
    if not isinstance(categories, list) or not categories:
        raise SnapshotValidationError(
            "category portfolio must contain at least one category revision"
        )
    seen: set[str] = set()
    selected: list[tuple[str, int]] = []
    entries_by_id: dict[str, Mapping[str, Any]] = {}
    for index, entry in enumerate(categories):
        if not isinstance(entry, Mapping):
            raise SnapshotValidationError(
                f"portfolio.categories[{index}] must be an object"
            )
        category_id = entry.get("category_id")
        if not isinstance(category_id, str) or not _CATEGORY_ID_RE.fullmatch(category_id):
            raise SnapshotValidationError(
                f"portfolio.categories[{index}] has an invalid category ID"
            )
        if category_id in seen:
            raise SnapshotValidationError(f"portfolio repeats category {category_id}")
        seen.add(category_id)
        entries_by_id[category_id] = entry
        category_revision = _revision(
            entry.get("category_revision"),
            f"portfolio category {category_id} revision",
        )
        selected.append((category_id, category_revision))

    flattened = _typed_members(raw.get("flattened_members"), "portfolio.flattened_members")
    validate_event_window(
        raw.get("base_event_id"),
        raw.get("confirmed_through_event_id"),
        expected_base_event_id=expected_base_event_id,
        expected_confirmed_through_event_id=expected_confirmed_through_event_id,
    )

    if current_categories is None:
        return
    history = category_history or {}
    derived: dict[str, set[str]] = {key: set() for key in CATEGORY_MEMBER_TYPES}
    incomplete_revision_sources = False
    for category_id, category_revision in selected:
        category = _category_at_revision(
            category_id,
            category_revision,
            current_categories,
            history,
        )
        if category is None:
            if not allow_missing_categories:
                raise SnapshotValidationError(
                    f"portfolio references missing category revision "
                    f"{category_id}@{category_revision}"
                )
            incomplete_revision_sources = True
        else:
            members = _typed_members(
                category.get("members"),
                f"category {category_id}@{category_revision} members",
            )
            for memory_type in CATEGORY_MEMBER_TYPES:
                derived[memory_type].update(members[memory_type])
        current = current_categories.get(category_id)
        if current is None:
            if not allow_missing_categories:
                raise SnapshotValidationError(
                    f"portfolio category is missing: {category_id}"
                )
            composed_entry = entries_by_id[category_id]
            if "status" in composed_entry and composed_entry["status"] != "missing":
                raise SnapshotValidationError(
                    f"portfolio category {category_id} status overlay is stale"
                )
            if (
                "current_revision" in composed_entry
                and composed_entry["current_revision"] is not None
            ):
                raise SnapshotValidationError(
                    f"portfolio category {category_id} revision overlay is stale"
                )
            continue
        status = current.get("status")
        if not isinstance(status, str) or not status:
            raise SnapshotValidationError(
                f"portfolio category {category_id} has an invalid status overlay"
            )
        current_revision = _revision(
            current.get("revision"), f"current category {category_id} revision"
        )
        composed_entry = entries_by_id[category_id]
        if "status" in composed_entry and composed_entry["status"] != status:
            raise SnapshotValidationError(
                f"portfolio category {category_id} status overlay is stale"
            )
        if (
            "current_revision" in composed_entry
            and composed_entry["current_revision"] != current_revision
        ):
            raise SnapshotValidationError(
                f"portfolio category {category_id} revision overlay is stale"
            )

    exact = {key: sorted(value) for key, value in derived.items()}
    if not incomplete_revision_sources and flattened != exact:
        raise SnapshotValidationError(
            "portfolio.flattened_members does not exactly equal the selected category union"
        )


__all__ = [
    "ASSIGNMENT_MEMORY_TYPES",
    "CATEGORY_MEMBER_TYPES",
    "MemorySnapshot",
    "SnapshotValidationError",
    "assignment_snapshots_from_task_card",
    "snapshot_from_record",
    "unique_memory_snapshots",
    "validate_assignment_portfolio",
    "validate_category_portfolio_snapshot",
    "validate_event_id",
    "validate_event_window",
    "validate_memory_snapshot",
    "validate_record_reference",
]
