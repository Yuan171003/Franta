"""Host ports used by the portable Explorer program.

The Explorer block deliberately knows nothing about collaborator tasks,
schedulers, workspaces, brokers, or published-memory schemas. A host adapter implements
this protocol and translates its own control plane into these operations.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping, Protocol, Sequence, runtime_checkable

from .contracts import (
    ExplorerHandoff,
    ExplorerPublishedMemorySnapshot,
    FrozenExplorerTurn,
)


@dataclass(frozen=True)
class ExplorerTurnContext:
    """Host-authenticated identity of the Explorer turn being drained."""

    turn_id: str
    root_candidate: Mapping[str, Any] | None = None


@runtime_checkable
class ExplorerHost(Protocol):
    """Narrow integration surface required by :class:`ExplorerProgram`."""

    def tick(self) -> None:
        """Advance the host's persisted admission/drain clock."""

    def expire_attempts(self) -> Sequence[str]:
        """Fence overdue logical attempts and return their call IDs."""

    def cancel_call(self, call_id: str, *, reason: str) -> None:
        """Best-effort cancellation of one host-owned transport call."""

    @property
    def phase(self) -> str | None:
        """Current Explorer/host alternation phase."""

    @property
    def max_workers(self) -> int:
        """Maximum concurrent Explorer lineages."""

    def controller_snapshot(self) -> Mapping[str, Any]:
        """Return the persisted Explorer controller value."""

    def visible_high_water(self) -> int:
        """Return the current trusted Explorer-record high-water mark."""

    def admit_lineage(self) -> bool:
        """Persist one block-requested lineage; false means admission closed."""

    def start_attempt(
        self, lineage_id: str, *, source_high_water_seq: int
    ) -> str:
        """Persist one block-requested attempt and return its call ID."""

    def call_is_launchable(self, call_id: str) -> bool:
        """Return whether a planned call may enter the worker pool."""

    def prepare_launch(self, call_id: str) -> Any:
        """Return an opaque, host-owned launch description."""

    def run_launch(self, call_id: str, launch: Any) -> Mapping[str, Any]:
        """Execute one Explorer attempt and return its validated final object."""

    def call_snapshot(self, call_id: str) -> Mapping[str, Any]:
        """Return a read-only current view of one logical call."""

    def validate_result(self, call_id: str, result: Mapping[str, Any]) -> None:
        """Validate final IDs against host-bound repository visibility."""

    def fail_attempt(self, call_id: str, *, reason: str) -> None:
        """End one planned attempt after an execution/infrastructure failure."""

    def commit_attempt(
        self,
        call_id: str,
        *,
        outcome: str,
        root_candidate: Mapping[str, str] | None,
    ) -> Sequence[str]:
        """Commit an attempt and return peer calls fenced by a root candidate."""

    def explorer_is_drained(self) -> bool:
        """Return whether every lineage admitted into the current turn stopped."""

    def current_turn_context(self) -> ExplorerTurnContext:
        """Return the immutable identity and candidate for the current turn."""


@runtime_checkable
class ExplorerCollaborator(Protocol):
    """Single handoff seam from Explorer into a host's integration turn."""

    def handoff_id_for(self, frozen_turn: FrozenExplorerTurn) -> str:
        """Return the host-facing idempotency ID for one frozen turn."""

    def accept_explorer_handoff(self, handoff: ExplorerHandoff) -> Any:
        """Idempotently accept a frozen turn and return an opaque receipt."""


@runtime_checkable
class ExplorerMemorySnapshotPort(Protocol):
    """Read-only host port used to freeze a worker's published-memory view."""

    def explorer_memory_snapshot(self) -> ExplorerPublishedMemorySnapshot:
        """Return one revisioned projection with host-decided eligibility."""


@runtime_checkable
class ExplorerHandoffFactory(Protocol):
    """Portable service operation needed by the complete turn program."""

    def create_handoff(
        self,
        turn_id: str,
        *,
        root_candidate: Mapping[str, Any] | None = None,
        id_factory: Callable[[FrozenExplorerTurn], str] | None = None,
    ) -> ExplorerHandoff:
        """Freeze a turn and construct its immutable collaborator offer."""


__all__ = [
    "ExplorerCollaborator",
    "ExplorerHandoffFactory",
    "ExplorerHost",
    "ExplorerMemorySnapshotPort",
    "ExplorerTurnContext",
]
