"""Stable contract definitions shared by all Franta blocks.

Names that are ambiguous in the legacy modules are exposed here with explicit
qualifiers.  Existing imports from ``franta.models``, ``franta.errors``,
``franta.workflows``, ``franta.references``, and ``franta.output_schemas`` remain
supported by identity-preserving compatibility facades.
"""

from __future__ import annotations

from .agent_access import AccessPolicy as AgentAccessPolicy
from .agent_access import MemoryRecord as AgentMemoryRecord
from .canonical import AccessPolicy as CanonicalAccessPolicy
from .canonical import MemoryRecord as CanonicalMemoryRecord
from .canonical import MemoryType, OperationType
from .failures import ValidationError as ArtifactValidationError
from .canonical import ValidationError as CanonicalValidationError


__all__ = [
    "AgentAccessPolicy",
    "AgentMemoryRecord",
    "ArtifactValidationError",
    "CanonicalAccessPolicy",
    "CanonicalMemoryRecord",
    "CanonicalValidationError",
    "MemoryType",
    "OperationType",
]
