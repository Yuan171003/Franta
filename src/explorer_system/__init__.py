"""Portable Explorer agent system.

This package is the copy boundary for Explorer.  It contains no imports from
the collaborator package and communicates with it exclusively through public
ports in :mod:`explorer_system.interfaces`.  A host-specific adapter is
responsible for capabilities, persistence of host control state, and export
into that host's authoritative memory.
"""

from . import access, agents, alternation, control, main_sort, settings, tools
from .contracts import *  # noqa: F401,F403
from .interfaces import (
    ExplorerCollaborator,
    ExplorerHandoffFactory,
    ExplorerHost,
    ExplorerMemorySnapshotPort,
    ExplorerTurnContext,
)
from .program import ExplorerProgram, ExplorerProgramError, ExplorerTurnAdvance
from .repository import ExplorerRepository
from .service import ExplorerAttempt, ExplorerLimits, ExplorerService

__all__ = [
    "ExplorerAttempt",
    "ExplorerLimits",
    "ExplorerCollaborator",
    "ExplorerHandoffFactory",
    "ExplorerHost",
    "ExplorerMemorySnapshotPort",
    "ExplorerProgram",
    "ExplorerProgramError",
    "ExplorerRepository",
    "ExplorerService",
    "ExplorerTurnAdvance",
    "ExplorerTurnContext",
    "access",
    "agents",
    "alternation",
    "control",
    "main_sort",
    "settings",
    "tools",
]
