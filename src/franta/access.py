"""Compatibility facade for Blocks 1, 5, and 6 access surfaces.

New code imports read semantics from :mod:`franta.read_access`, launch-bound
broker and permission behavior from :mod:`franta.execution_gateway`, and fixed
policy values from :mod:`franta.contracts.agent_access`.  These aliases preserve
the legacy ``franta.access`` API without duplicating any implementation.
"""

from __future__ import annotations

from .contracts.agent_access import (
    AccessPolicy,
    CAS_TOOL_ARGUMENTS,
    ISOLATED_MODES,
    MEMORY_TYPES,
    MemoryRecord,
    ORDINARY_SEARCH_MODES,
    SEARCHABLE_MEMORY_TYPES,
    SPRINT_LANES,
    STAGING_SKILL_BY_TOOL,
    STAGING_TOOL_BY_SKILL,
    _ID_RE,
    policy_for,
    staging_skills_for_policy,
)
from .execution_gateway.broker import (
    BrokerBinding,
    BrokerClient,
    BrokerError,
    MemoryBroker,
)
from .execution_gateway.permissions import CodexPermissionProfile
from .read_access.audit import AuditLog
from .read_access.memory import (
    AccessError,
    AuditedMemoryAPI,
    InMemoryBackend,
    MemoryBackend,
)
from .read_access.tasks import AuditedTaskAPI, TaskBackend


__all__ = [
    "AccessError",
    "AccessPolicy",
    "AuditLog",
    "AuditedMemoryAPI",
    "AuditedTaskAPI",
    "BrokerBinding",
    "BrokerClient",
    "BrokerError",
    "CodexPermissionProfile",
    "InMemoryBackend",
    "MEMORY_TYPES",
    "MemoryBackend",
    "MemoryBroker",
    "MemoryRecord",
    "ORDINARY_SEARCH_MODES",
    "SEARCHABLE_MEMORY_TYPES",
    "STAGING_SKILL_BY_TOOL",
    "STAGING_TOOL_BY_SKILL",
    "TaskBackend",
    "policy_for",
    "staging_skills_for_policy",
]
