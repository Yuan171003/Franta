from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path


SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from franta.explorer.contracts import (  # noqa: E402
    ExplorerIdempotencyConflict,
    ExplorerReadScope,
    ExplorerValidationError,
    ExplorerWriteContext,
)
from franta.explorer.repository import ExplorerRepository  # noqa: E402


def _receipt(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def _context(
    *,
    turn: str = "ETURN-1",
    session: str = "EWORK-1",
    attempt: int = 1,
    call: str = "CALL-1",
) -> ExplorerWriteContext:
    return ExplorerWriteContext(
        turn_id=turn,
        worker_session_id=session,
        attempt_no=attempt,
        call_id=call,
    )


def _scratch(
    operation: str,
    record_id: str,
    *,
    abstract: str = "Spectral sequence idea",
    content: str = "Try the filtration induced by the root problem.",
    kind: str = "idea",
    cas_operations: list[str] | None = None,
    related_memory_ids: list[str] | None = None,
) -> dict[str, object]:
    return {
        "skill": "record-scratch",
        "operation_id": operation,
        "record_id": record_id,
        "record_kind": kind,
        "abstract": abstract,
        "content": content,
        "related_memory_ids": list(
            ["F-1", "R-2"]
            if related_memory_ids is None
            else related_memory_ids
        ),
        "cas_operation_ids": list(cas_operations or []),
    }


def _summary(
    operation: str,
    record_id: str,
    sources: list[str],
) -> dict[str, object]:
    return {
        "skill": "record-summary",
        "operation_id": operation,
        "record_id": record_id,
        "abstract": "Attempt summary for the spectral route",
        "content": "The attempt developed a filtration and found a boundary obstacle.",
        "directions_tried": ["Filter first", "Dualize the obstruction"],
        "main_progress": "Reduced the root problem to one boundary map.",
        "main_obstacles": "The boundary map need not vanish without extra input.",
        "source_scratch_ids": sources,
    }


class ExplorerRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.database = Path(self.temporary.name) / "explorer.sqlite3"
        self.repository = ExplorerRepository(self.database)

    def tearDown(self) -> None:
        self.repository.close()
        self.temporary.cleanup()

    def test_prepare_trust_visibility_and_semantic_idempotency(self) -> None:
        context = _context()
        first_receipt = _receipt("scratch-first")
        prepared = self.repository.prepare_scratch(
            context,
            _scratch("OP-S1", "ES-runtime-1"),
            first_receipt,
        )
        scope = ExplorerReadScope(frozenset({context.turn_id}))

        self.assertEqual(prepared.seq, 0)
        self.assertIsNone(self.repository.fetch(scope, prepared.record_id))
        self.assertEqual(self.repository.visible_high_water(), 0)

        self.repository.trust_receipt(first_receipt)
        visible = self.repository.fetch(scope, prepared.record_id)
        self.assertIsNotNone(visible)
        assert visible is not None
        self.assertEqual(visible.seq, 1)
        self.assertEqual(self.repository.visible_high_water(), 1)

        # A transport retry may allocate a new runtime candidate record ID. The
        # stable operation still resolves to the first accepted ES-* record.
        replay_receipt = _receipt("scratch-replay")
        replay = self.repository.prepare_scratch(
            _context(call="CALL-2"),
            _scratch("OP-S1", "ES-runtime-retry"),
            replay_receipt,
        )
        self.assertEqual(replay.record_id, prepared.record_id)
        self.repository.trust_receipt(replay_receipt)
        self.assertEqual(self.repository.visible_high_water(), 1)

        with self.assertRaises(ExplorerIdempotencyConflict):
            self.repository.prepare_scratch(
                _context(call="CALL-STOLEN"),
                _scratch("OP-S1", "ES-runtime-stolen"),
                first_receipt,
            )

        changed = _scratch("OP-S1", "ES-runtime-3")
        changed["content"] = "Different mathematical content."
        with self.assertRaises(ExplorerIdempotencyConflict):
            self.repository.prepare_scratch(
                _context(call="CALL-3"), changed, _receipt("scratch-conflict")
            )

    def test_scratch_related_memory_ids_accept_explorer_scratch_ids(self) -> None:
        context = _context()
        receipt = _receipt("scratch-with-explorer-relation")
        prepared = self.repository.prepare_scratch(
            context,
            _scratch(
                "OP-ES-RELATION",
                "ES-runtime-related",
                related_memory_ids=["F-1", "ES-prior-scratch"],
            ),
            receipt,
        )
        self.assertEqual(
            prepared.related_memory_ids,
            ("F-1", "ES-prior-scratch"),
        )

        self.repository.trust_receipt(receipt)
        visible = self.repository.fetch(
            ExplorerReadScope(frozenset({context.turn_id})),
            prepared.record_id,
        )
        self.assertIsNotNone(visible)
        assert visible is not None
        self.assertEqual(
            visible.related_memory_ids,
            ("F-1", "ES-prior-scratch"),
        )

        with self.assertRaises(ExplorerValidationError):
            self.repository.prepare_scratch(
                context,
                _scratch(
                    "OP-ESUM-RELATION",
                    "ES-runtime-summary-related",
                    related_memory_ids=["ESUM-attempt-summary"],
                ),
                _receipt("scratch-with-summary-relation"),
            )

    def test_caller_operation_ids_are_scoped_to_worker_attempt(self) -> None:
        """Parallel owners may reuse labels; one owner's replay remains exact."""

        first_context = _context(session="EWORK-A", call="CALL-A")
        second_context = _context(session="EWORK-B", call="CALL-B")
        shared_scratch_operation = "OP-SHARED-SCRATCH"
        first_payload = _scratch(
            shared_scratch_operation,
            "ES-shared-a",
            content="Worker A's independently staged idea.",
        )
        second_payload = _scratch(
            shared_scratch_operation,
            "ES-shared-b",
            content="Worker B's genuinely different idea.",
        )
        first = self.repository.prepare_scratch(
            first_context, first_payload, _receipt("shared-scratch-a")
        )
        second = self.repository.prepare_scratch(
            second_context, second_payload, _receipt("shared-scratch-b")
        )
        self.assertNotEqual(first.record_id, second.record_id)

        replay = self.repository.prepare_scratch(
            _context(session="EWORK-A", call="CALL-A-RETRY"),
            _scratch(
                shared_scratch_operation,
                "ES-shared-a-retry",
                content="Worker A's independently staged idea.",
            ),
            _receipt("shared-scratch-a-retry"),
        )
        self.assertEqual(replay.record_id, first.record_id)
        with self.assertRaises(ExplorerIdempotencyConflict):
            self.repository.prepare_scratch(
                _context(session="EWORK-A", call="CALL-A-CONFLICT"),
                _scratch(
                    shared_scratch_operation,
                    "ES-shared-a-conflict",
                    content="Worker A replayed different mathematical bytes.",
                ),
                _receipt("shared-scratch-a-conflict"),
            )

        def cas_payload(marker: str) -> dict[str, object]:
            return {
                "skill": "CAS",
                "operation_id": "CAS-SHARED",
                "software": "python",
                "software_version": "3.14",
                "exact_input": f"print({marker!r})",
                "exact_output": marker + "\n",
                "exit_status": 0,
                "description": f"Compute the {marker} worker model.",
                "assumptions": "Exact string output.",
                "interpretation": f"The model returned {marker}.",
                "related_ids": {},
                "execution_succeeded": True,
            }

        first_cas = self.repository.prepare_cas_evidence(
            first_context,
            cas_payload("A"),
            _receipt("shared-cas-a"),
        )
        second_cas = self.repository.prepare_cas_evidence(
            second_context,
            cas_payload("B"),
            _receipt("shared-cas-b"),
        )
        self.assertNotEqual(first_cas.evidence_id, second_cas.evidence_id)
        replay_cas = self.repository.prepare_cas_evidence(
            _context(session="EWORK-A", call="CALL-A-CAS-RETRY"),
            cas_payload("A"),
            _receipt("shared-cas-a-retry"),
        )
        self.assertEqual(replay_cas.evidence_id, first_cas.evidence_id)
        with self.assertRaises(ExplorerIdempotencyConflict):
            self.repository.prepare_cas_evidence(
                _context(session="EWORK-A", call="CALL-A-CAS-CONFLICT"),
                cas_payload("changed-A"),
                _receipt("shared-cas-a-conflict"),
            )

    def test_scratch_cas_citation_is_pinned_when_the_scratch_is_created(
        self,
    ) -> None:
        """A later same-label CAS must not rebind immutable scratch provenance."""

        def cas_payload(marker: str) -> dict[str, object]:
            return {
                "skill": "CAS",
                "operation_id": "CAS-PINNED",
                "software": "python",
                "software_version": "3.14",
                "exact_input": f"print({marker!r})",
                "exact_output": marker + "\n",
                "exit_status": 0,
                "description": f"Compute the {marker} model.",
                "assumptions": "Exact string output.",
                "interpretation": f"The model returned {marker}.",
                "related_ids": {},
                "execution_succeeded": True,
            }

        first_context = _context(attempt=1, call="CALL-PINNED-1")
        first_receipt = _receipt("pinned-cas-first")
        first = self.repository.prepare_cas_evidence(
            first_context,
            cas_payload("first"),
            first_receipt,
        )
        self.repository.trust_receipt(first_receipt)

        scratch_context = _context(attempt=2, call="CALL-PINNED-SCRATCH")
        scratch_receipt = _receipt("pinned-scratch")
        scratch = self.repository.prepare_scratch(
            scratch_context,
            _scratch(
                "OP-PINNED-SCRATCH",
                "ES-pinned-scratch",
                kind="computation",
                cas_operations=["CAS-PINNED"],
            ),
            scratch_receipt,
        )
        self.repository.trust_receipt(scratch_receipt)
        self.assertEqual(
            self.repository.list_cas_evidence_for_record(scratch.record_id),
            (first,),
        )

        later_receipt = _receipt("pinned-cas-later")
        later = self.repository.prepare_cas_evidence(
            _context(attempt=2, call="CALL-PINNED-LATER"),
            cas_payload("later"),
            later_receipt,
        )
        self.repository.trust_receipt(later_receipt)
        self.assertNotEqual(later.evidence_id, first.evidence_id)

        self.assertEqual(
            self.repository.list_cas_evidence_for_record(scratch.record_id),
            (first,),
        )

    def test_summary_source_cannot_authorize_late_rebound_cas_evidence(
        self,
    ) -> None:
        """A summary inherits its source scratch's exact CAS binding."""

        def cas_payload(marker: str) -> dict[str, object]:
            return {
                "skill": "CAS",
                "operation_id": "CAS-SUMMARY-PINNED",
                "software": "python",
                "software_version": "3.14",
                "exact_input": f"print({marker!r})",
                "exact_output": marker + "\n",
                "exit_status": 0,
                "description": f"Compute the {marker} summary model.",
                "assumptions": "Exact string output.",
                "interpretation": f"The model returned {marker}.",
                "related_ids": {},
                "execution_succeeded": True,
            }

        first_receipt = _receipt("summary-pinned-cas-first")
        self.repository.prepare_cas_evidence(
            _context(attempt=1, call="CALL-SUMMARY-PINNED-1"),
            cas_payload("first"),
            first_receipt,
        )
        self.repository.trust_receipt(first_receipt)

        context = _context(attempt=2, call="CALL-SUMMARY-PINNED-2")
        scratch_receipt = _receipt("summary-pinned-scratch")
        scratch = self.repository.prepare_scratch(
            context,
            _scratch(
                "OP-SUMMARY-PINNED-SCRATCH",
                "ES-summary-pinned-scratch",
                kind="computation",
                cas_operations=["CAS-SUMMARY-PINNED"],
            ),
            scratch_receipt,
        )
        self.repository.trust_receipt(scratch_receipt)
        summary_receipt = _receipt("summary-pinned-summary")
        summary = self.repository.prepare_summary(
            context,
            _summary(
                "OP-SUMMARY-PINNED",
                "ESUM-summary-pinned",
                [scratch.record_id],
            ),
            summary_receipt,
        )
        self.repository.trust_receipt(summary_receipt)

        later_receipt = _receipt("summary-pinned-cas-later")
        later = self.repository.prepare_cas_evidence(
            _context(attempt=2, call="CALL-SUMMARY-PINNED-LATER"),
            cas_payload("later"),
            later_receipt,
        )
        self.repository.trust_receipt(later_receipt)

        frozen = self.repository.freeze_turn(context.turn_id)
        run = self.repository.create_sort_run(
            "SORT-SUMMARY-PINNED",
            frozen,
            "T-summary-pinned",
        )
        with self.assertRaises(ExplorerValidationError):
            self.repository.resolve_computation_for_sort(
                run.sort_run_id,
                later.evidence_id,
                [summary.record_id],
                run.sort_task_id,
            )

    def test_same_label_cross_session_cas_cannot_be_promoted_by_peer_source(
        self,
    ) -> None:
        """A selected source authorizes only its pinned XCAS evidence identity."""

        def cas_payload(marker: str) -> dict[str, object]:
            return {
                "skill": "CAS",
                "operation_id": "CAS-CROSS-SESSION",
                "software": "python",
                "software_version": "3.14",
                "exact_input": f"print({marker!r})",
                "exact_output": marker + "\n",
                "exit_status": 0,
                "description": f"Compute worker {marker}'s model.",
                "assumptions": "Exact string output.",
                "interpretation": f"The model returned {marker}.",
                "related_ids": {},
                "execution_succeeded": True,
            }

        context_a = _context(session="EWORK-PROMOTE-A", call="CALL-PROMOTE-A")
        context_b = _context(session="EWORK-PROMOTE-B", call="CALL-PROMOTE-B")
        cas_receipt_a = _receipt("promote-cross-session-cas-a")
        cas_receipt_b = _receipt("promote-cross-session-cas-b")
        evidence_a = self.repository.prepare_cas_evidence(
            context_a,
            cas_payload("A"),
            cas_receipt_a,
        )
        evidence_b = self.repository.prepare_cas_evidence(
            context_b,
            cas_payload("B"),
            cas_receipt_b,
        )
        self.repository.trust_receipt(cas_receipt_a)
        self.repository.trust_receipt(cas_receipt_b)

        scratch_receipt_a = _receipt("promote-cross-session-scratch-a")
        scratch_receipt_b = _receipt("promote-cross-session-scratch-b")
        scratch_a = self.repository.prepare_scratch(
            context_a,
            _scratch(
                "OP-PROMOTE-CROSS-SESSION-A",
                "ES-promote-cross-session-a",
                kind="computation",
                cas_operations=["CAS-CROSS-SESSION"],
            ),
            scratch_receipt_a,
        )
        scratch_b = self.repository.prepare_scratch(
            context_b,
            _scratch(
                "OP-PROMOTE-CROSS-SESSION-B",
                "ES-promote-cross-session-b",
                kind="computation",
                cas_operations=["CAS-CROSS-SESSION"],
            ),
            scratch_receipt_b,
        )
        self.repository.trust_receipt(scratch_receipt_a)
        self.repository.trust_receipt(scratch_receipt_b)

        frozen = self.repository.freeze_turn(context_a.turn_id)
        run = self.repository.create_sort_run(
            "SORT-CROSS-SESSION-CAS",
            frozen,
            "T-cross-session-cas",
        )
        canonical_a = self.repository.resolve_computation_for_sort(
            run.sort_run_id,
            evidence_a.evidence_id,
            [scratch_a.record_id],
            run.sort_task_id,
        )
        canonical_b = self.repository.resolve_computation_for_sort(
            run.sort_run_id,
            evidence_b.evidence_id,
            [scratch_b.record_id],
            run.sort_task_id,
        )
        self.assertEqual(canonical_a["output"], "A\n")
        self.assertEqual(canonical_b["output"], "B\n")

        with self.assertRaises(ExplorerValidationError):
            self.repository.resolve_computation_for_sort(
                run.sort_run_id,
                evidence_b.evidence_id,
                [scratch_a.record_id],
                run.sort_task_id,
            )
        with self.assertRaises(ExplorerValidationError):
            self.repository.resolve_computation_for_sort(
                run.sort_run_id,
                evidence_a.evidence_id,
                [scratch_b.record_id],
                run.sort_task_id,
            )

    def test_v1_database_backfills_pinned_cas_citation_idempotently(self) -> None:
        """Migration binds pre-record evidence, never a later same-label CAS."""

        legacy_database = Path(self.temporary.name) / "explorer-v1.sqlite3"
        legacy = ExplorerRepository(legacy_database)

        def cas_payload(marker: str) -> dict[str, object]:
            return {
                "skill": "CAS",
                "operation_id": "CAS-V1-PIN",
                "software": "python",
                "software_version": "3.14",
                "exact_input": f"print({marker!r})",
                "exact_output": marker + "\n",
                "exit_status": 0,
                "description": f"Compute the legacy {marker} model.",
                "assumptions": "Exact string output.",
                "interpretation": f"The model returned {marker}.",
                "related_ids": {},
                "execution_succeeded": True,
            }

        first_receipt = _receipt("v1-pin-first-cas")
        first = legacy.prepare_cas_evidence(
            _context(attempt=1, call="CALL-V1-PIN-FIRST"),
            cas_payload("first"),
            first_receipt,
        )
        legacy.trust_receipt(first_receipt)
        scratch_receipt = _receipt("v1-pin-scratch")
        scratch = legacy.prepare_scratch(
            _context(attempt=2, call="CALL-V1-PIN-SCRATCH"),
            _scratch(
                "OP-V1-PIN-SCRATCH",
                "ES-v1-pin-scratch",
                kind="computation",
                cas_operations=["CAS-V1-PIN"],
            ),
            scratch_receipt,
        )
        legacy.trust_receipt(scratch_receipt)
        later_receipt = _receipt("v1-pin-later-cas")
        later = legacy.prepare_cas_evidence(
            _context(attempt=2, call="CALL-V1-PIN-LATER"),
            cas_payload("later"),
            later_receipt,
        )
        legacy.trust_receipt(later_receipt)
        legacy.close()

        # Recreate the shape of a schema-v1 database.  Explicit timestamps
        # make the migration boundary deterministic even on coarse clocks.
        connection = sqlite3.connect(legacy_database)
        try:
            connection.execute("DROP TABLE explorer_record_cas_evidence")
            connection.execute("DROP TRIGGER preserve_explorer_cas_update")
            connection.execute(
                "UPDATE explorer_cas_evidence SET created_at=? WHERE evidence_id=?",
                ("2000-01-01T00:00:00+00:00", first.evidence_id),
            )
            connection.execute(
                "UPDATE explorer_cas_evidence SET created_at=? WHERE evidence_id=?",
                ("9999-01-01T00:00:00+00:00", later.evidence_id),
            )
            connection.commit()
        finally:
            connection.close()

        migrated = ExplorerRepository(legacy_database)
        try:
            self.assertEqual(
                tuple(
                    item.evidence_id
                    for item in migrated.list_cas_evidence_for_record(
                        scratch.record_id
                    )
                ),
                (first.evidence_id,),
            )
        finally:
            migrated.close()

        reopened = ExplorerRepository(legacy_database)
        try:
            self.assertEqual(
                tuple(
                    item.evidence_id
                    for item in reopened.list_cas_evidence_for_record(
                        scratch.record_id
                    )
                ),
                (first.evidence_id,),
            )
            connection = sqlite3.connect(legacy_database)
            try:
                count = connection.execute(
                    "SELECT COUNT(*) FROM explorer_record_cas_evidence "
                    "WHERE record_id=?",
                    (scratch.record_id,),
                ).fetchone()[0]
            finally:
                connection.close()
            self.assertEqual(count, 1)
        finally:
            reopened.close()

    def test_append_only_rows_and_atomic_per_type_quotas(self) -> None:
        context = _context()
        first = self.repository.prepare_scratch(
            context,
            _scratch("OP-Q1", "ES-quota-1"),
            _receipt("quota-1"),
            max_records_per_attempt=1,
            max_records_per_turn=1,
        )
        with self.assertRaisesRegex(ExplorerValidationError, "quota"):
            self.repository.prepare_scratch(
                _context(call="CALL-Q2"),
                _scratch("OP-Q2", "ES-quota-2"),
                _receipt("quota-2"),
                max_records_per_attempt=1,
                max_records_per_turn=1,
            )

        # Prepared-but-untrusted rows count, but exact operation retries bypass
        # admission rather than failing because their own row filled the quota.
        replay = self.repository.prepare_scratch(
            _context(call="CALL-Q3"),
            _scratch("OP-Q1", "ES-quota-retry"),
            _receipt("quota-retry"),
            max_records_per_attempt=1,
            max_records_per_turn=1,
        )
        self.assertEqual(replay.record_id, first.record_id)

        # Summary quotas are independent from scratch quotas.
        self.repository.trust_receipt(_receipt("quota-1"))
        self.repository.prepare_summary(
            context,
            _summary("OP-SUM-Q", "ESUM-quota-1", [first.record_id]),
            _receipt("summary-quota"),
            max_records_per_attempt=1,
            max_records_per_turn=1,
        )

        # sqlite3.Connection's context manager controls its transaction but
        # does not close the connection.  Wrap it in closing() so this
        # low-level tamper check cannot leak a database handle into later
        # tests (where cyclic GC would otherwise emit a ResourceWarning).
        with closing(sqlite3.connect(self.database)) as connection:
            with self.assertRaises(sqlite3.DatabaseError):
                connection.execute(
                    "UPDATE explorer_records SET abstract='forged' WHERE record_id=?",
                    (first.record_id,),
                )
            connection.rollback()
            with self.assertRaises(sqlite3.DatabaseError):
                connection.execute(
                    "DELETE FROM explorer_records WHERE record_id=?",
                    (first.record_id,),
                )

    def test_trust_order_defines_high_water_and_historical_turn_scope(self) -> None:
        old_context = _context(turn="ETURN-old", session="EWORK-old")
        new_context = _context(turn="ETURN-new", session="EWORK-new")
        late_receipt = _receipt("late-old")
        late = self.repository.prepare_scratch(
            old_context,
            _scratch("OP-LATE", "ES-late"),
            late_receipt,
        )
        early_receipt = _receipt("early-new")
        early = self.repository.prepare_scratch(
            new_context,
            _scratch("OP-EARLY", "ES-early"),
            early_receipt,
        )
        self.repository.trust_receipt(early_receipt)
        frozen_high_water = self.repository.visible_high_water()
        self.assertEqual(frozen_high_water, 1)
        self.assertEqual(self.repository.trusted_turn_ids(frozen_high_water), ("ETURN-new",))

        self.repository.trust_receipt(late_receipt)
        self.assertEqual(self.repository.visible_high_water(), 2)
        self.assertEqual(
            self.repository.trusted_turn_ids(), ("ETURN-new", "ETURN-old")
        )
        frozen_scope = ExplorerReadScope(
            frozenset({"ETURN-old", "ETURN-new"}), max_seq=frozen_high_water
        )
        self.assertIsNotNone(self.repository.fetch(frozen_scope, early.record_id))
        self.assertIsNone(self.repository.fetch(frozen_scope, late.record_id))

    def test_summary_provenance_session_boundary_and_retired_direction(self) -> None:
        context = _context()
        first_receipt = _receipt("progress-1")
        first = self.repository.prepare_scratch(
            context,
            _scratch("OP-P1", "ES-progress-1", kind="progress"),
            first_receipt,
        )
        self.repository.trust_receipt(first_receipt)
        second_receipt = _receipt("progress-2")
        second = self.repository.prepare_scratch(
            _context(attempt=2, call="CALL-D2"),
            _scratch("OP-P2", "ES-progress-2", kind="progress"),
            second_receipt,
        )
        self.repository.trust_receipt(second_receipt)

        summary_receipt = _receipt("summary")
        summary = self.repository.prepare_summary(
            _context(attempt=2, call="CALL-SUM"),
            _summary("OP-SUM", "ESUM-runtime-1", [first.record_id, second.record_id]),
            summary_receipt,
        )
        expected_source_set_digest = hashlib.sha256(
            json.dumps(
                [
                    {
                        "record_id": first.record_id,
                        "input_digest": first.input_digest,
                    },
                    {
                        "record_id": second.record_id,
                        "input_digest": second.input_digest,
                    },
                ],
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        self.assertEqual(summary.source_set_digest, expected_source_set_digest)
        self.repository.trust_receipt(summary_receipt)
        visible = self.repository.fetch(
            ExplorerReadScope(frozenset({context.turn_id})), summary.record_id
        )
        assert visible is not None
        self.assertEqual(visible.directions_tried, ("Filter first", "Dualize the obstruction"))
        self.assertEqual(visible.source_scratch_ids, (first.record_id, second.record_id))

        # The attempt-final summary seals new scratch writes, while an exact
        # operation replay remains idempotent for crash recovery.
        replay = self.repository.prepare_scratch(
            _context(attempt=2, call="CALL-D2-REPLAY"),
            _scratch("OP-P2", "ES-progress-2-retry", kind="progress"),
            _receipt("progress-2-replay"),
        )
        self.assertEqual(replay.record_id, second.record_id)
        with self.assertRaises(ExplorerValidationError):
            self.repository.prepare_scratch(
                _context(attempt=2, call="CALL-AFTER-SUMMARY"),
                _scratch("OP-AFTER-SUMMARY", "ES-after-summary"),
                _receipt("after-summary"),
            )

        with self.assertRaises(ExplorerValidationError):
            self.repository.prepare_summary(
                _context(session="EWORK-other", attempt=2, call="CALL-BAD"),
                _summary("OP-BAD-SUM", "ESUM-bad", [first.record_id]),
                _receipt("bad-summary"),
            )
        with self.assertRaisesRegex(
            ExplorerValidationError, "at least one trusted scratch"
        ):
            self.repository.prepare_summary(
                _context(attempt=3, call="CALL-EMPTY-SUMMARY"),
                _summary("OP-EMPTY-SUMMARY", "ESUM-empty-summary", []),
                _receipt("empty-summary"),
            )
        with self.assertRaisesRegex(
            ExplorerValidationError, "authorized Explorer scratch kind"
        ):
            self.repository.prepare_scratch(
                _context(attempt=2, call="CALL-BAD-D"),
                _scratch("OP-BAD-D", "ES-bad-direction", kind="direction"),
                _receipt("bad-direction"),
            )

    def test_sort_cas_and_promotion_are_bound_to_frozen_sources(self) -> None:
        context = _context()
        cas_receipt = _receipt("cas")
        evidence = self.repository.prepare_cas_evidence(
            context,
            {
                "skill": "CAS",
                "operation_id": "CAS-OP-1",
                "software": "python",
                "software_version": "3.14",
                "exact_input": "print(2 + 2)",
                "exact_output": "4\n",
                "exit_status": 0,
                "description": "Evaluate the model example.",
                "assumptions": "Integer arithmetic.",
                "interpretation": "The example has value four.",
                "related_ids": {"fact": ["F-1"]},
                "output_artifact": {
                    "path": "artifacts/agent-output.txt",
                    "sha256": "b" * 64,
                },
                "execution_succeeded": True,
            },
            cas_receipt,
            archived_artifact_relpath="private/explorer-cas/output.txt",
        )
        self.assertIsNone(
            self.repository.get_cas_evidence_by_operation("CAS-OP-1")
        )
        self.repository.trust_receipt(cas_receipt)
        self.assertEqual(
            self.repository.get_cas_evidence_by_operation("CAS-OP-1"), evidence
        )

        scratch_receipt = _receipt("computation-scratch")
        scratch = self.repository.prepare_scratch(
            context,
            _scratch(
                "OP-COMP-S",
                "ES-computation",
                kind="computation",
                cas_operations=["CAS-OP-1"],
            ),
            scratch_receipt,
        )
        self.repository.trust_receipt(scratch_receipt)
        summary_receipt = _receipt("computation-summary")
        summary = self.repository.prepare_summary(
            context,
            _summary("OP-COMP-SUM", "ESUM-computation", [scratch.record_id]),
            summary_receipt,
        )
        self.repository.trust_receipt(summary_receipt)

        frozen = self.repository.freeze_turn(context.turn_id)
        run = self.repository.create_sort_run("SORT-1", frozen, "T-sort-1")
        canonical = self.repository.resolve_computation_for_sort(
            run.sort_run_id,
            evidence.evidence_id,
            [summary.record_id],
            run.sort_task_id,
        )
        self.assertEqual(canonical["task_id"], "T-sort-1")
        self.assertEqual(canonical["operation_id"], "explorer-cas:SORT-1:" + evidence.evidence_id)
        self.assertEqual(
            canonical["software"], {"name": "python", "version": "3.14"}
        )
        self.assertEqual(canonical["output"], "4\n")
        self.assertEqual(canonical["related_memory_ids"], {"fact": ["F-1"]})
        self.assertEqual(
            canonical["output_artifact"],
            {
                "path": "private/explorer-cas/output.txt",
                "sha256": "b" * 64,
            },
        )
        for removed in (
            "software_version",
            "exact_output",
            "related_ids",
            "arguments",
            "version_arguments",
        ):
            self.assertNotIn(removed, canonical)
        self.assertNotIn("execution_succeeded", canonical)

        promotion = self.repository.stage_promotion(
            run.sort_run_id,
            "FRANTA-OP-1",
            "computation",
            [summary.record_id],
            "a" * 64,
        )
        self.assertEqual(
            self.repository.list_received_promotions(sort_run_id=run.sort_run_id),
            (promotion,),
        )
        committed = self.repository.resolve_promotion(
            promotion.franta_operation_id,
            "committed",
            canonical_id="C-1",
            resolution="published",
        )
        self.assertEqual(committed.state, "committed")
        self.assertEqual(
            self.repository.list_received_promotions(sort_run_id=run.sort_run_id),
            (),
        )
        self.assertEqual(
            self.repository.list_promotions(states={"committed"}), (committed,)
        )
        visible_scratch = self.repository.fetch(
            ExplorerReadScope(frozenset({context.turn_id})), scratch.record_id
        )
        assert visible_scratch is not None
        self.assertEqual(visible_scratch.promoted_canonical_ids, ("C-1",))


if __name__ == "__main__":
    unittest.main()
