"""Compatibility facade for typed-reference contracts."""

from __future__ import annotations

from .contracts.references import (
    FactNormalization,
    ReferenceValidationError,
    contains_identifier_token,
    normalize_fact_candidate,
    replace_identifier_token,
    substitute_nonfact_typed_ids,
)

__all__ = [
    "FactNormalization",
    "ReferenceValidationError",
    "contains_identifier_token",
    "normalize_fact_candidate",
    "replace_identifier_token",
    "substitute_nonfact_typed_ids",
]
