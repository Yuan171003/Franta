"""Compatibility facade for cross-boundary failure contracts."""

from __future__ import annotations

from .contracts.failures import (
    AccessDenied,
    ConfigurationError,
    ConflictError,
    FrantaError,
    NeedsAttention,
    StaleLease,
    ValidationError,
)

__all__ = [
    "AccessDenied",
    "ConfigurationError",
    "ConflictError",
    "FrantaError",
    "NeedsAttention",
    "StaleLease",
    "ValidationError",
]
