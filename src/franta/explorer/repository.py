"""Franta identifier-policy adapter for the standalone Explorer store."""

import re

from explorer_system.repository import *  # noqa: F401,F403
from explorer_system.repository import ExplorerRepository as _PortableRepository
from explorer_system.repository import __all__ as _portable_all

from .contracts import (
    EXPLORER_PROMOTION_KINDS,
    ExplorerPromotion,
    ExplorerRecord,
    ExplorerSortRun,
)


_FRANTA_MEMORY_ID_RE = re.compile(
    r"^(?:F|R|M|CL|O|C|ES)-[A-Za-z0-9][A-Za-z0-9_.:-]*$"
)
_FRANTA_ANY_ID_RE = re.compile(
    r"^(?:F|R|M|CL|O|T|C)-[A-Za-z0-9][A-Za-z0-9_.:-]*$"
)
_FRANTA_TASK_ID_RE = re.compile(
    r"^T-[A-Za-z0-9][A-Za-z0-9_.:-]*$"
)


class ExplorerRepository(_PortableRepository):
    """Bind portable Explorer IDs and exports to Franta canonical contracts."""

    def __init__(self, path):
        super().__init__(
            path,
            published_record_id_pattern=_FRANTA_MEMORY_ID_RE,
            host_context_id_pattern=_FRANTA_TASK_ID_RE,
            host_record_id_pattern=_FRANTA_ANY_ID_RE,
            export_kinds=EXPLORER_PROMOTION_KINDS,
            handoff_digest_fields=("sort_run_id", "sort_task_id"),
        )

    def _record_from_row_locked(self, row):
        record = super()._record_from_row_locked(row)
        return ExplorerRecord(**record.__dict__)

    @staticmethod
    def _sort_run_from_row(row):
        return ExplorerSortRun(
            run_id=str(row["sort_run_id"]),
            turn_id=str(row["turn_id"]),
            source_high_water_seq=int(row["source_high_water_seq"]),
            source_set_digest=str(row["source_set_digest"]),
            host_context_id=str(row["sort_task_id"]),
            input_digest=str(row["input_digest"]),
            created_at=str(row["created_at"]),
        )

    def _export_from_row_locked(self, row):
        export = super()._export_from_row_locked(row)
        return ExplorerPromotion(**export.__dict__)

    # The following methods are the complete Franta handoff vocabulary. The
    # portable repository itself exposes only host-neutral run/export names.
    def create_sort_run(self, sort_run_id, frozen_turn, sort_task_id):
        return self.create_handoff_run(sort_run_id, frozen_turn, sort_task_id)

    def stage_promotion(
        self,
        sort_run_id,
        franta_operation_id,
        target_kind,
        source_record_ids,
        operation_digest,
    ):
        return self.stage_export(
            sort_run_id,
            franta_operation_id,
            target_kind,
            source_record_ids,
            operation_digest,
        )

    def get_promotion(self, franta_operation_id):
        return self.get_export(franta_operation_id)

    def list_promotions(self, *, sort_run_id=None, states=None):
        return self.list_exports(run_id=sort_run_id, states=states)

    def list_received_promotions(self, *, sort_run_id=None):
        return self.list_pending_exports(run_id=sort_run_id)

    def resolve_promotion(
        self,
        franta_operation_id,
        state,
        *,
        canonical_id=None,
        resolution=None,
        error=None,
    ):
        return self.resolve_export(
            franta_operation_id,
            state,
            host_record_id=canonical_id,
            resolution=resolution,
            error=error,
        )

    def resolve_computation_for_sort(
        self,
        sort_run_id,
        evidence_id,
        source_record_ids,
        sort_task_id,
    ):
        run = self.get_handoff_run(sort_run_id)
        if run is None:
            from .contracts import ExplorerNotFoundError

            raise ExplorerNotFoundError(
                f"unknown Explorer sort run: {sort_run_id}"
            )
        if run.host_context_id != sort_task_id:
            from .contracts import ExplorerValidationError

            raise ExplorerValidationError("sort task does not own this sort run")
        value = self.resolve_computation_evidence(
            sort_run_id, evidence_id, source_record_ids
        )
        value["task_id"] = sort_task_id
        value["staging_id"] = f"explorer-cas:{sort_run_id}:{evidence_id}"
        value["operation_id"] = value["staging_id"]
        value["fact_candidate_operation_ids"] = []
        return value


__all__ = [name for name in _portable_all if name != "ExplorerRepository"] + [
    "ExplorerRepository"
]
