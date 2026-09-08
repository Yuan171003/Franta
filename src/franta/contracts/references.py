"""Typed temporary-reference contracts and normalization.

Temporary IDs are scheduler control data.  They may occur only in declared
relationship slots.  Fact dependencies are declared exclusively by
``predecessor_fact_ids``; mathematical prose is copied without inspection or
substitution.  The helpers here deliberately never perform recursive or
substring replacement.
"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass
from typing import Any, Mapping, Sequence


class ReferenceValidationError(ValueError):
    """A temporary-reference request is malformed or unsafe."""


@dataclass(frozen=True)
class FactNormalization:
    """Result of resolving the declared predecessor list in one candidate."""

    payload: dict[str, Any]
    changed: bool
    unresolved_predecessors: tuple[str, ...]
    resolved_predecessors: Mapping[str, str]


_ID_CHARACTER = r"A-Za-z0-9_.:-"


def _token_pattern(identifier: str) -> re.Pattern[str]:
    if not isinstance(identifier, str) or not identifier:
        raise ReferenceValidationError("temporary identifiers must be nonempty strings")
    # A final period followed by whitespace/end is sentence punctuation.  A
    # period followed by another ID character remains part of a larger token.
    return re.compile(
        rf"(?<![{_ID_CHARACTER}]){re.escape(identifier)}"
        rf"(?![A-Za-z0-9_:-])(?!\.[{_ID_CHARACTER}])"
    )


def contains_identifier_token(text: str, identifier: str) -> bool:
    """Return whether ``identifier`` occurs as one complete ID token."""

    return bool(_token_pattern(identifier).search(text))


def replace_identifier_token(text: str, identifier: str, canonical_id: str) -> str:
    """Replace one complete identifier token, never a substring of another ID."""

    if not isinstance(text, str):
        raise ReferenceValidationError("citation text must be a string")
    if not isinstance(canonical_id, str) or not canonical_id.startswith("F-"):
        raise ReferenceValidationError("a fact predecessor must resolve to a canonical F- ID")
    return _token_pattern(identifier).sub(lambda _match: canonical_id, text)


def normalize_fact_candidate(
    payload: Mapping[str, Any],
    resolutions: Mapping[str, str],
) -> FactNormalization:
    """Resolve IDs declared in ``predecessor_fact_ids`` only.

    The predecessor list is the sole authority for the fact dependency graph.
    A resolution changes that list only; statement, proof, metadata, external
    references, legacy similarly named fields, and all other content are copied
    byte-for-byte as values without inspection or substitution.  The caller
    creates a new immutable candidate version whenever ``changed`` is true.
    """

    if not isinstance(payload, Mapping):
        raise ReferenceValidationError("fact candidate payload must be an object")
    result = copy.deepcopy(dict(payload))
    field = "predecessor_fact_ids"
    raw = result.get(field, [])
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes, bytearray)):
        raise ReferenceValidationError(f"{field} must be a list")

    changed = False
    normalized: list[str] = []
    unresolved: list[str] = []
    used: dict[str, str] = {}
    seen: set[str] = set()
    for index, raw_id in enumerate(raw):
        if not isinstance(raw_id, str) or not raw_id.strip():
            raise ReferenceValidationError(f"{field}[{index}] must be a nonempty string")
        predecessor_id = raw_id.strip()
        if predecessor_id in seen:
            raise ReferenceValidationError(f"{field} contains duplicate ID {predecessor_id}")
        seen.add(predecessor_id)
        canonical_id = resolutions.get(predecessor_id)
        if canonical_id is None:
            normalized.append(predecessor_id)
            if not predecessor_id.startswith("F-"):
                unresolved.append(predecessor_id)
            continue
        if not isinstance(canonical_id, str) or not canonical_id.startswith("F-"):
            raise ReferenceValidationError(
                "a fact predecessor must resolve to a canonical F- ID"
            )
        if predecessor_id.startswith("F-") and predecessor_id != canonical_id:
            raise ReferenceValidationError("canonical fact IDs cannot be remapped")
        # Distinct temporary proposals may synthesize or deduplicate to the
        # same canonical fact.  The authoritative dependency field is a set-
        # like ID list, so keep the first position instead of rejecting an
        # otherwise valid candidate merely because two aliases collapsed.
        if canonical_id not in normalized:
            normalized.append(canonical_id)
        used[predecessor_id] = canonical_id
        changed = changed or predecessor_id != canonical_id

    result[field] = normalized
    return FactNormalization(
        payload=result,
        changed=changed,
        unresolved_predecessors=tuple(unresolved),
        resolved_predecessors=used,
    )


def substitute_nonfact_typed_ids(
    memory_type: str,
    payload: Mapping[str, Any],
    resolutions: Mapping[str, str],
) -> dict[str, Any]:
    """Substitute only schema-declared non-fact relationship slots.

    This helper is used by the legacy atomic-group compatibility path.  It is
    intentionally blind to abstracts, strategy descriptions, statements,
    memo/claim content, explanations, and all other prose or mathematical
    fields.
    """

    result = copy.deepcopy(dict(payload))
    list_fields = {
        "route": ("related_obligation_ids", "relevant_memo_ids", "relevant_claim_ids"),
        "memo": ("related_route_ids",),
        "claim": ("related_route_ids",),
        "obligation": ("related_route_ids",),
    }.get(memory_type)
    if list_fields is None:
        raise ReferenceValidationError(f"unsupported non-fact memory type: {memory_type}")
    for field in list_fields:
        value = result.get(field)
        if isinstance(value, list):
            result[field] = [resolutions.get(item, item) if isinstance(item, str) else item for item in value]

    if memory_type == "obligation" and isinstance(result.get("relations"), list):
        for relation in result["relations"]:
            if not isinstance(relation, dict):
                continue
            for field in ("premise_memory_ids",):
                value = relation.get(field)
                if isinstance(value, list):
                    relation[field] = [
                        resolutions.get(item, item) if isinstance(item, str) else item
                        for item in value
                    ]
            conclusion = relation.get("conclusion")
            if isinstance(conclusion, str) and conclusion != "ROOT":
                relation["conclusion"] = resolutions.get(conclusion, conclusion)
    return result


__all__ = [
    "FactNormalization",
    "ReferenceValidationError",
    "contains_identifier_token",
    "normalize_fact_candidate",
    "replace_identifier_token",
    "substitute_nonfact_typed_ids",
]
