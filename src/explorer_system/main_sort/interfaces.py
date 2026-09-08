"""Public host boundary for the isolated main-sort block."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence, runtime_checkable

from .contracts import HostSortTools, MainSortLaunchSpec, MainSortModelRoute
from .validation import ValidatedMainSortSubmission


@runtime_checkable
class MainSortBlock(Protocol):
    """Pure launch-spec service required by a collaborator adapter."""

    def model_route(self) -> MainSortModelRoute:
        """Return the model selection owned by this block."""

    def response_schema(self) -> dict[str, Any]:
        """Return a detached strict schema for the sorter response."""

    def build_launch_spec(
        self,
        *,
        root_problem: str,
        input_path: str = "input/task_card.json",
        host_agent_name: str = "host collaborator",
        host_tools: HostSortTools | None = None,
    ) -> MainSortLaunchSpec:
        """Return one portable main-sort launch description."""


@runtime_checkable
class MainSortSnapshotBlock(Protocol):
    """Frozen-source portion of the isolated block contract."""

    def snapshot_contract(self) -> tuple[int, Path]:
        """Return the version and workspace-relative snapshot root."""

    def render_snapshot_files(
        self, snapshot: Mapping[str, Any]
    ) -> dict[Path, str]:
        """Validate and render one immutable source snapshot."""

    def build_snapshot(
        self,
        *,
        sort_run_id: str,
        turn_id: str,
        source_high_water_seq: int,
        source_set_digest: str,
        records: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any]:
        """Build an immutable snapshot from adapter-projected records."""

    def snapshot_digest(self, snapshot: Mapping[str, Any]) -> str:
        """Return the versioned byte identity of one source snapshot."""

    def validate_materialized_snapshot(
        self,
        workspace: str | os.PathLike[str],
        snapshot: Mapping[str, Any],
    ) -> Path:
        """Validate a host-materialized read-only snapshot."""


@runtime_checkable
class MainSortValidationBlock(Protocol):
    """Side-effect-free result-validation portion of the block contract."""

    def operation_kinds(self) -> frozenset[str]:
        """Return the proposal vocabulary accepted from main-sort."""

    def validate_progress_operation(
        self, operation: Mapping[str, Any], *, sort_run_id: str
    ) -> None:
        """Validate one proposed operation and its frozen provenance."""

    def normalize_computation_promotions(
        self, value: Any
    ) -> list[dict[str, Any]]:
        """Validate and detach trusted-computation declarations."""

    def operation_has_exact_provenance(
        self, operation: Mapping[str, Any], *, sort_run_id: str
    ) -> bool:
        """Return whether an operation has exact frozen provenance."""

    def computation_has_exact_provenance(
        self, computation: Mapping[str, Any], *, sort_run_id: str
    ) -> bool:
        """Return whether a computation has exact trusted provenance."""

    def validate_submission(
        self,
        result: Mapping[str, Any],
        progress: Sequence[Mapping[str, Any]],
        *,
        task_id: str,
        attempt: int,
        sort_run_id: str,
        source_is_allowed: Callable[[str], bool],
    ) -> ValidatedMainSortSubmission:
        """Validate one authenticated final result without side effects."""


@runtime_checkable
class MainSortProgram(
    MainSortBlock, MainSortSnapshotBlock, MainSortValidationBlock, Protocol
):
    """Complete interface implemented by the default isolated block."""


__all__ = [
    "MainSortBlock",
    "MainSortProgram",
    "MainSortSnapshotBlock",
    "MainSortValidationBlock",
]
