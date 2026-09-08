"""Typed failure contracts used across scheduler boundaries."""

from __future__ import annotations


class FrantaError(Exception):
    """Base class for an expected Franta failure."""


class ConfigurationError(FrantaError):
    """The bootstrap manifest is missing or violates a mechanical invariant."""


class ValidationError(FrantaError):
    """An agent-produced artifact failed mechanical validation."""


class AccessDenied(ValidationError):
    """A caller attempted to read or reference material outside its policy."""


class ConflictError(ValidationError):
    """An optimistic revision or idempotency key conflicts with committed state."""


class NeedsAttention(FrantaError):
    """Durable work cannot proceed automatically but has not been rejected."""


class StaleLease(FrantaError):
    """Output came from a superseded call epoch."""


__all__ = [
    "AccessDenied",
    "ConfigurationError",
    "ConflictError",
    "FrantaError",
    "NeedsAttention",
    "StaleLease",
    "ValidationError",
]
