"""Pure validation for the portable main-sort block.

This module deliberately knows nothing about Explorer repositories, Franta
tasks, workspaces, receipts, or canonical memory.  Collaborators authenticate
those resources and pass the resulting plain values through this contract.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Callable, Mapping, Sequence


MAIN_SORT_OPERATION_KINDS = frozenset(
    {
        "route_add",
        "route_update",
        "memo",
        "claim_add",
        "obligation_add",
        "obligation_update",
    }
)
_EXPLORER_RECORD_ID_RE = re.compile(
    r"^(?:ES|ESUM)-[A-Za-z0-9][A-Za-z0-9_.:-]*$"
)
_EXPLORER_CAS_ID_RE = re.compile(
    r"^XCAS-[A-Za-z0-9][A-Za-z0-9_.:-]*$"
)


class MainSortValidationError(ValueError):
    """One portable main-sort value violates the versioned contract."""


@dataclass(frozen=True)
class ValidatedMainSortSubmission:
    """Host-neutral result of authenticating one completed sorter output."""

    final_progress_id: str
    selected_source_ids: tuple[str, ...]
    deferred_computation_record_ids: tuple[str, ...]


def validate_progress_operation(
    operation: Mapping[str, Any], *, sort_run_id: str
) -> None:
    """Validate the main-sort extension on one record-progress operation."""

    provenance = operation.get("explorer_provenance")
    if not isinstance(provenance, Mapping) or set(provenance) != {
        "sort_run_id",
        "source_record_ids",
    }:
        raise MainSortValidationError(
            "each main-sort operation requires exact Explorer provenance"
        )
    source_ids = provenance.get("source_record_ids")
    if (
        provenance.get("sort_run_id") != sort_run_id
        or not isinstance(source_ids, list)
        or not source_ids
        or len(source_ids) > 64
        or not all(
            isinstance(item, str) and _EXPLORER_RECORD_ID_RE.fullmatch(item)
            for item in source_ids
        )
        or len(set(source_ids)) != len(source_ids)
    ):
        raise MainSortValidationError(
            "main-sort Explorer provenance is outside its frozen source contract"
        )


def normalize_computation_promotions(value: Any) -> list[dict[str, Any]]:
    """Validate main-sort XCAS declarations and return a detached value."""

    if not isinstance(value, list) or len(value) > 64:
        raise MainSortValidationError(
            "explorer_computation_promotions must be a list of at most 64 entries"
        )
    seen_evidence: set[str] = set()
    normalized: list[dict[str, Any]] = []
    for promotion in value:
        if not isinstance(promotion, Mapping) or set(promotion) != {
            "evidence_id",
            "source_record_ids",
        }:
            raise MainSortValidationError(
                "Explorer computation promotion requires evidence_id and source_record_ids"
            )
        evidence_id = promotion.get("evidence_id")
        source_ids = promotion.get("source_record_ids")
        if (
            not isinstance(evidence_id, str)
            or not _EXPLORER_CAS_ID_RE.fullmatch(evidence_id)
            or evidence_id in seen_evidence
            or not isinstance(source_ids, list)
            or not source_ids
            or len(source_ids) > 64
            or not all(
                isinstance(item, str) and _EXPLORER_RECORD_ID_RE.fullmatch(item)
                for item in source_ids
            )
            or len(set(source_ids)) != len(source_ids)
        ):
            raise MainSortValidationError(
                "invalid Explorer computation evidence or source IDs"
            )
        seen_evidence.add(evidence_id)
        normalized.append(
            {
                "evidence_id": evidence_id,
                "source_record_ids": list(source_ids),
            }
        )
    return normalized


def operation_source_ids(
    operation: Mapping[str, Any],
    *,
    sort_run_id: str | None = None,
) -> tuple[str, ...]:
    """Return exact Explorer sources for one proposal or reject its provenance."""

    provenance = operation.get("explorer_provenance")
    if not isinstance(provenance, Mapping):
        raise MainSortValidationError(
            "main-sort operation lacks Explorer provenance"
        )
    if sort_run_id is not None and provenance.get("sort_run_id") != sort_run_id:
        raise MainSortValidationError(
            "main-sort operation lacks exact frozen Explorer provenance"
        )
    source_ids = provenance.get("source_record_ids")
    if not isinstance(source_ids, list) or not all(
        isinstance(item, str) for item in source_ids
    ):
        raise MainSortValidationError(
            "main-sort operation lacks exact frozen Explorer provenance"
        )
    return tuple(source_ids)


def computation_source_ids(
    computation: Mapping[str, Any],
    *,
    sort_run_id: str | None = None,
) -> tuple[str, ...]:
    """Return exact Explorer sources for one trusted computation promotion."""

    provenance = computation.get("explorer_provenance")
    if not isinstance(provenance, Mapping):
        raise MainSortValidationError(
            "main-sort computation lacks Explorer provenance"
        )
    if sort_run_id is not None and provenance.get("sort_run_id") != sort_run_id:
        raise MainSortValidationError(
            "main-sort computation lacks trusted Explorer provenance"
        )
    source_ids = provenance.get("source_record_ids")
    if not isinstance(source_ids, list) or not all(
        isinstance(item, str) for item in source_ids
    ):
        raise MainSortValidationError(
            "main-sort computation lacks trusted Explorer provenance"
        )
    return tuple(source_ids)


def validate_computation_provenance(
    computation: Mapping[str, Any], *, sort_run_id: str
) -> None:
    """Validate the trusted-computation provenance stored by the host."""

    provenance = computation.get("explorer_provenance")
    if (
        not isinstance(provenance, Mapping)
        or set(provenance)
        != {"sort_run_id", "source_record_ids", "evidence_id"}
        or provenance.get("sort_run_id") != sort_run_id
        or not isinstance(provenance.get("source_record_ids"), list)
        or not provenance.get("source_record_ids")
    ):
        raise MainSortValidationError(
            "main-sort computation lacks trusted Explorer provenance"
        )


def validate_submission(
    result: Mapping[str, Any],
    progress: Sequence[Mapping[str, Any]],
    *,
    task_id: str,
    attempt: int,
    sort_run_id: str,
    source_is_allowed: Callable[[str], bool],
) -> ValidatedMainSortSubmission:
    """Validate the final response against authenticated progress artifacts.

    The host authenticates the artifacts and supplies a frozen-scope predicate;
    the block owns all selection/provenance semantics.  No persistence occurs
    here, so retries are side-effect free.
    """

    finals = [item for item in progress if item.get("is_final")]
    final_progress_id = str(result.get("final_progress_id") or "")
    if (
        len(finals) != 1
        or str(finals[0].get("progress_id") or "") != final_progress_id
        or str(finals[0].get("task_id") or "") != task_id
        or int(finals[0].get("attempt", 0)) != int(attempt)
    ):
        raise MainSortValidationError(
            "main-sort response must match exactly one final record-progress artifact"
        )

    promoted_sources: list[str] = []
    for record in progress:
        for operation in record.get("operations", []):
            if not isinstance(operation, Mapping):
                raise MainSortValidationError(
                    "main-sort operation must be an object"
                )
            promoted_sources.extend(
                operation_source_ids(operation, sort_run_id=sort_run_id)
            )
        for computation in record.get("computations", []):
            if not isinstance(computation, Mapping):
                raise MainSortValidationError(
                    "main-sort computation must be an object"
                )
            promoted_sources.extend(
                computation_source_ids(computation, sort_run_id=sort_run_id)
            )

    selected = result.get("selected_explorer_record_ids")
    if (
        not isinstance(selected, list)
        or not all(isinstance(item, str) for item in selected)
        or len(set(selected)) != len(selected)
        or set(selected) != set(promoted_sources)
    ):
        raise MainSortValidationError(
            "main-sort selected IDs must exactly match promoted provenance"
        )

    deferred = result.get("deferred_computation_record_ids")
    if (
        not isinstance(deferred, list)
        or not all(isinstance(item, str) for item in deferred)
        or len(set(deferred)) != len(deferred)
    ):
        raise MainSortValidationError(
            "deferred computation record IDs must be duplicate-free strings"
        )
    for record_id in [*selected, *deferred]:
        if not source_is_allowed(record_id):
            raise MainSortValidationError(
                f"main-sort record is outside the frozen turn: {record_id}"
            )

    return ValidatedMainSortSubmission(
        final_progress_id=final_progress_id,
        selected_source_ids=tuple(selected),
        deferred_computation_record_ids=tuple(deferred),
    )


__all__ = [
    "MAIN_SORT_OPERATION_KINDS",
    "MainSortValidationError",
    "ValidatedMainSortSubmission",
    "computation_source_ids",
    "normalize_computation_promotions",
    "operation_source_ids",
    "validate_progress_operation",
    "validate_computation_provenance",
    "validate_submission",
]
