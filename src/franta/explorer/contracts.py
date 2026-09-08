"""Franta vocabulary adapter for the standalone Explorer contracts."""

from explorer_system.contracts import *  # noqa: F401,F403
from explorer_system.contracts import (
    EXPLORER_EXPORT_STATES,
    ExplorerExport as _PortableExplorerExport,
    ExplorerHandoffRun as _PortableExplorerHandoffRun,
    ExplorerRecord as _PortableExplorerRecord,
    __all__ as _portable_all,
)


EXPLORER_PROMOTION_KINDS = frozenset(
    {
        "fact",
        "route_add",
        "route_update",
        "memo",
        "claim_add",
        "obligation_add",
        "obligation_update",
        "computation",
    }
)
EXPLORER_PROMOTION_STATES = EXPLORER_EXPORT_STATES


class ExplorerRecord(_PortableExplorerRecord):
    @property
    def promoted_canonical_ids(self) -> tuple[str, ...]:
        return self.exported_record_ids


class ExplorerSortRun(_PortableExplorerHandoffRun):
    @property
    def sort_run_id(self) -> str:
        return self.run_id

    @property
    def sort_task_id(self) -> str:
        return self.host_context_id


class ExplorerPromotion(_PortableExplorerExport):
    @property
    def promotion_id(self) -> str:
        return self.export_id

    @property
    def sort_run_id(self) -> str:
        return self.run_id

    @property
    def franta_operation_id(self) -> str:
        return self.host_item_id

    @property
    def canonical_id(self) -> str | None:
        return self.host_record_id


__all__ = [
    name
    for name in _portable_all
    if name not in {"ExplorerRecord", "ExplorerHandoffRun", "ExplorerExport"}
] + [
    "EXPLORER_PROMOTION_KINDS",
    "EXPLORER_PROMOTION_STATES",
    "ExplorerRecord",
    "ExplorerSortRun",
    "ExplorerPromotion",
]
