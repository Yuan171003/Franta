from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from franta.models import IdempotencyConflict, MemoryType, ValidationError
from franta.references import normalize_fact_candidate
from franta.store import MemoryStore


class _CrashAfterUpdateResolutionStore(MemoryStore):
    crash_next_update_resolution = False

    def _resolve_temporary_target_locked(self, *args, **kwargs):
        touched = super()._resolve_temporary_target_locked(*args, **kwargs)
        if (
            self.crash_next_update_resolution
            and kwargs.get("resolution") == "updated"
        ):
            self.crash_next_update_resolution = False
            raise KeyboardInterrupt("simulated stop before update transaction commit")
        return touched


def route_payload(**changes: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "abstract": "A route with a durable pending relationship",
        "strategy_description": "Develop the geometric reduction directly.",
        "value_assessment": {
            "confidence": "plausible",
            "success_gain": "would remove the obstruction",
            "failure_gain": "would locate a boundary failure",
            "relevance": "central",
            "novelty": "new reduction",
        },
        "progress": [],
        "related_obligation_ids": [],
        "next_steps": [],
        "obstacles": [],
        "active_fact_ids": [],
        "relevant_memo_ids": [],
        "relevant_claim_ids": [],
    }
    payload.update(changes)
    return payload


def memo_payload(**changes: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "abstract": "A later memo",
        "genre": "normal",
        "content": "The reduction exposes a useful invariant.",
        "related_route_ids": [],
    }
    payload.update(changes)
    return payload


def obligation_payload(**changes: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "abstract": "A typed pending obligation",
        "statement": "Establish the remaining local assertion.",
        "importance": "It closes the local step.",
        "predecessor_fact_ids": [],
        "partial_progress": [],
        "related_route_ids": [],
        "relations": [],
    }
    payload.update(changes)
    return payload


class FactReferenceNormalizationTests(unittest.TestCase):
    def test_only_predecessor_fact_ids_change_in_new_payload_copy(self) -> None:
        original = {
            "candidate_id": "FC-1",
            "candidate_version": 1,
            "statement": "TMP-LEMMA implies the result.",
            "proof": "Apply TMP-LEMMA. TMP-LEMMA-extra is unrelated text.",
            "predecessor_fact_ids": ["TMP-LEMMA"],
            "abstract": "No substitution here: TMP-LEMMA.",
        }
        normalized = normalize_fact_candidate(original, {"TMP-LEMMA": "F-canonical"})
        self.assertTrue(normalized.changed)
        self.assertEqual(
            normalized.payload["predecessor_fact_ids"], ["F-canonical"]
        )
        self.assertEqual(normalized.payload["statement"], original["statement"])
        self.assertEqual(normalized.payload["proof"], original["proof"])
        self.assertEqual(normalized.payload["abstract"], original["abstract"])
        self.assertEqual(original["predecessor_fact_ids"], ["TMP-LEMMA"])

    def test_declared_temporary_predecessor_need_not_be_cited(self) -> None:
        original = {
            "statement": "The result follows.",
            "proof": "A direct argument with no identifier token.",
            "predecessor_fact_ids": ["TMP-MISSING"],
        }
        normalized = normalize_fact_candidate(
            original,
            {"TMP-MISSING": "F-canonical"},
        )
        self.assertTrue(normalized.changed)
        self.assertEqual(
            normalized.payload["predecessor_fact_ids"], ["F-canonical"]
        )
        self.assertEqual(normalized.payload["statement"], original["statement"])
        self.assertEqual(normalized.payload["proof"], original["proof"])

    def test_legacy_predecessor_ids_is_not_authoritative(self) -> None:
        original = {
            "statement": "TMP-LEGACY appears only as mathematical prose.",
            "proof": "Use TMP-LEGACY as a label without declaring a dependency.",
            "predecessor_ids": ["TMP-LEGACY"],
        }
        normalized = normalize_fact_candidate(
            original,
            {"TMP-LEGACY": "F-canonical"},
        )
        self.assertFalse(normalized.changed)
        self.assertEqual(normalized.payload["predecessor_ids"], ["TMP-LEGACY"])
        self.assertEqual(normalized.payload["predecessor_fact_ids"], [])
        self.assertEqual(normalized.payload["statement"], original["statement"])
        self.assertEqual(normalized.payload["proof"], original["proof"])
        self.assertEqual(normalized.unresolved_predecessors, ())

    def test_distinct_temporary_predecessors_may_collapse_to_one_fact(self) -> None:
        normalized = normalize_fact_candidate(
            {
                "statement": "A consequence.",
                "proof": "Complete argument.",
                "predecessor_fact_ids": ["TMP-A", "F-existing", "TMP-B"],
            },
            {"TMP-A": "F-shared", "TMP-B": "F-shared"},
        )
        self.assertEqual(
            normalized.payload["predecessor_fact_ids"],
            ["F-shared", "F-existing"],
        )
        self.assertTrue(normalized.changed)


class DurableNonFactReferenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temporary.name) / "memory.sqlite3"
        self.store = MemoryStore(self.db_path, projection_dir=False)

    def tearDown(self) -> None:
        self.store.close()
        self.temporary.cleanup()

    def test_cross_progress_reference_survives_restart_and_resolves_atomically(self) -> None:
        route = self.store.add_route(
            "route-first",
            route_payload(relevant_memo_ids=["TMP-LATER-MEMO"]),
            proposal_id="TMP-ROUTE",
        )
        self.assertEqual(route.status, "committed")
        route_id = route.canonical_id
        assert route_id is not None
        before = self.store.get(route_id)
        self.assertEqual(before["relevant_memo_ids"], ["TMP-LATER-MEMO"])
        self.assertEqual(before["pending_references"][0]["temporary_id"], "TMP-LATER-MEMO")
        self.assertEqual(before.revision, 1)

        self.store.close()
        self.store = MemoryStore(self.db_path, projection_dir=False)
        self.assertEqual(
            self.store.temporary_reference_status("TMP-LATER-MEMO")["state"], "pending"
        )
        memo = self.store.add_memo(
            "memo-later",
            memo_payload(related_route_ids=["TMP-ROUTE"]),
            proposal_id="TMP-LATER-MEMO",
        )
        self.assertEqual(memo.status, "committed")
        memo_id = memo.canonical_id
        assert memo_id is not None
        after = self.store.get(route_id)
        self.assertEqual(after["relevant_memo_ids"], [memo_id])
        self.assertEqual(after["pending_references"], [])
        self.assertEqual(after.revision, 2)
        self.assertEqual(self.store.get(memo_id)["related_route_ids"], [route_id])
        self.assertEqual(
            self.store.temporary_reference_status("TMP-LATER-MEMO")["canonical_id"],
            memo_id,
        )
        replay = self.store.add_memo(
            "memo-later",
            memo_payload(related_route_ids=["TMP-ROUTE"]),
            proposal_id="TMP-LATER-MEMO",
        )
        self.assertTrue(replay.replayed)
        self.assertEqual(self.store.get(route_id).revision, 2)

    def test_duplicate_resolution_updates_prior_sources(self) -> None:
        memo = self.store.add_memo(
            "memo-first",
            memo_payload(related_route_ids=["TMP-DUPLICATE-ROUTE"]),
            proposal_id="TMP-MEMO",
        )
        existing = self.store.add_route("existing-route", route_payload())
        memo_id = memo.canonical_id
        route_id = existing.canonical_id
        assert memo_id is not None and route_id is not None
        result = self.store.record_duplicate_resolution(
            "route-dedup",
            "route_add",
            "TMP-DUPLICATE-ROUTE",
            route_id,
            route_payload(),
        )
        self.assertEqual(result.status, "committed")
        resolved_memo = self.store.get(memo_id)
        self.assertEqual(resolved_memo["related_route_ids"], [route_id])
        self.assertEqual(resolved_memo.metadata_version, 2)
        self.assertEqual(self.store.get(route_id)["relevant_memo_ids"], [memo_id])

    def test_rejection_is_terminal_and_fresh_correction_does_not_retarget_source(self) -> None:
        route = self.store.add_route(
            "route-waits",
            route_payload(relevant_memo_ids=["TMP-CORRECTED-MEMO"]),
        )
        route_id = route.canonical_id
        assert route_id is not None
        rejected = self.store.add_memo(
            "bad-memo",
            memo_payload(genre="unsupported"),
            proposal_id="TMP-CORRECTED-MEMO",
        )
        self.assertEqual(rejected.status, "rejected")
        self.assertEqual(
            self.store.temporary_reference_status("TMP-CORRECTED-MEMO")["state"],
            "rejected",
        )
        self.assertEqual(self.store.get(route_id)["pending_references"][0]["state"], "rejected")
        self.assertEqual(
            self.store.get(route_id)["relevant_memo_ids"],
            ["TMP-CORRECTED-MEMO(unpublished)"],
        )

        corrected = self.store.add_memo(
            "corrected-memo",
            memo_payload(),
            proposal_id="TMP-FRESH-CORRECTED-MEMO",
        )
        self.assertEqual(corrected.status, "committed")
        corrected_id = corrected.canonical_id
        assert corrected_id is not None
        route_record = self.store.get(route_id)
        self.assertEqual(
            route_record["relevant_memo_ids"],
            ["TMP-CORRECTED-MEMO(unpublished)"],
        )
        self.assertEqual(route_record["pending_references"][0]["state"], "rejected")
        self.assertNotIn(route_id, self.store.get(corrected_id)["related_route_ids"])

    def test_explicit_resolution_cannot_revive_rejected_link(self) -> None:
        source = self.store.add_memo(
            "memo-waits-for-explicit-route",
            memo_payload(related_route_ids=["TMP-EXPLICIT-ROUTE"]),
        )
        source_id = source.canonical_id
        assert source_id is not None
        rejected = self.store.add_route(
            "bad-explicit-route",
            route_payload(unexpected_field="invalid"),
            proposal_id="TMP-EXPLICIT-ROUTE",
        )
        self.assertEqual(rejected.status, "rejected")
        self.assertEqual(
            self.store.temporary_reference_status("TMP-EXPLICIT-ROUTE")["state"],
            "rejected",
        )

        target = self.store.add_route("explicit-existing-route", route_payload())
        target_id = target.canonical_id
        assert target_id is not None
        with self.assertRaises(ValidationError):
            self.store.resolve_temporary_reference(
                "TMP-EXPLICIT-ROUTE",
                target_id,
                resolution="operator_correction",
                operation_id="explicit-route-resolution",
                actor="operator",
            )
        status = self.store.temporary_reference_status("TMP-EXPLICIT-ROUTE")
        self.assertEqual(status["state"], "rejected")
        self.assertEqual(status["reference_counts"], {"rejected": 1})
        self.assertEqual(
            self.store.get(source_id)["related_route_ids"],
            ["TMP-EXPLICIT-ROUTE(unpublished)"],
        )
        self.assertEqual(self.store.get(target_id)["relevant_memo_ids"], [])

    def test_explicit_resolution_cannot_revive_rejected_deferred_relation(self) -> None:
        source = self.store.add_obligation(
            "relation-waits-for-explicit-obligation",
            obligation_payload(
                relations=[
                    {
                        "relation_type": "suffices",
                        "premise_memory_ids": ["TMP-EXPLICIT-OBLIGATION"],
                        "conclusion": "ROOT",
                        "explanation": "The corrected target supplies the premise.",
                        "supporting_fact_ids": [],
                    }
                ]
            ),
        )
        source_id = source.canonical_id
        assert source_id is not None
        rejected = self.store.add_obligation(
            "bad-explicit-obligation",
            obligation_payload(unexpected_field="invalid"),
            proposal_id="TMP-EXPLICIT-OBLIGATION",
        )
        self.assertEqual(rejected.status, "rejected")
        target = self.store.add_obligation(
            "explicit-existing-obligation", obligation_payload()
        )
        target_id = target.canonical_id
        assert target_id is not None

        with self.assertRaises(ValidationError):
            self.store.resolve_temporary_reference(
                "TMP-EXPLICIT-OBLIGATION",
                target_id,
                resolution="operator_correction",
                operation_id="explicit-obligation-resolution",
                actor="operator",
            )
        status = self.store.temporary_reference_status("TMP-EXPLICIT-OBLIGATION")
        self.assertEqual(status["reference_counts"], {"rejected": 1})
        source_record = self.store.get(source_id)
        self.assertEqual(
            source_record["relations"][0]["premise_memory_ids"],
            ["TMP-EXPLICIT-OBLIGATION(unpublished)"],
        )

    def test_deferred_relation_is_not_a_premise_until_target_resolves(self) -> None:
        source = self.store.add_obligation(
            "source-obligation",
            obligation_payload(
                relations=[
                    {
                        "relation_type": "suffices",
                        "premise_memory_ids": ["TMP-PREMISE-OBLIGATION"],
                        "conclusion": "ROOT",
                        "explanation": "The local assertion is sufficient.",
                        "supporting_fact_ids": [],
                    }
                ]
            ),
        )
        source_id = source.canonical_id
        assert source_id is not None
        pending_relation = self.store.get(source_id)["relations"][0]
        self.assertEqual(
            pending_relation["premise_memory_ids"],
            ["TMP-PREMISE-OBLIGATION"],
        )
        self.assertEqual(
            pending_relation["inactive_endpoint_ids"],
            ["TMP-PREMISE-OBLIGATION"],
        )

        target = self.store.add_obligation(
            "target-obligation",
            obligation_payload(statement="Prove the local assertion."),
            proposal_id="TMP-PREMISE-OBLIGATION",
        )
        target_id = target.canonical_id
        assert target_id is not None
        source_record = self.store.get(source_id)
        self.assertEqual(source_record["relations"][0]["premise_memory_ids"], [target_id])
        self.assertEqual(source_record.revision, 2)

    def test_update_can_stage_a_cross_progress_typed_reference(self) -> None:
        route = self.store.add_route("route-base", route_payload())
        route_id = route.canonical_id
        assert route_id is not None
        update = self.store.update_route(
            "route-pending-update",
            {
                "target_id": route_id,
                "expected_base_revision": 1,
                "set": {},
                "append": {},
                "add_ids": {"relevant_memo_ids": ["TMP-UPDATE-MEMO"]},
                "remove_ids": {},
                "explanation": "Attach the future memo when it is published.",
                "supporting_memory_ids": [],
            },
        )
        self.assertEqual(update.status, "committed")
        self.assertEqual(self.store.get(route_id).revision, 2)
        memo = self.store.add_memo(
            "update-memo",
            memo_payload(),
            proposal_id="TMP-UPDATE-MEMO",
        )
        self.assertEqual(self.store.get(route_id)["relevant_memo_ids"], [memo.canonical_id])
        self.assertEqual(self.store.get(route_id).revision, 3)

    def test_route_update_resolves_original_add_proposal_atomically(self) -> None:
        source_obligation = self.store.add_obligation(
            "obligation-waits-for-updated-route",
            obligation_payload(related_route_ids=["TMP-UPDATED-ROUTE"]),
        )
        source_memo = self.store.add_memo(
            "memo-waits-for-updated-route",
            memo_payload(related_route_ids=["TMP-UPDATED-ROUTE"]),
        )
        existing = self.store.add_route("existing-route-for-update", route_payload())
        route_id = existing.canonical_id
        obligation_id = source_obligation.canonical_id
        memo_id = source_memo.canonical_id
        assert route_id and obligation_id and memo_id

        patch = {
            "target_id": route_id,
            "expected_base_revision": 1,
            "set": {},
            "append": {"progress": ["The proposed route sharpens this mechanism."]},
            "add_ids": {},
            "remove_ids": {},
            "explanation": "Absorb the proposed route's new progress.",
            "supporting_memory_ids": [],
        }
        result = self.store.update_route(
            "synthesized-route-update",
            patch,
            proposal_id="TMP-UPDATED-ROUTE",
        )
        self.assertEqual(result.status, "committed")
        self.assertEqual(result.resolution, "updated")
        self.assertEqual(result.canonical_id, route_id)
        self.assertEqual(
            self.store.proposal_mapping("TMP-UPDATED-ROUTE")["resolution"],
            "updated",
        )
        temporary = self.store.temporary_reference_status("TMP-UPDATED-ROUTE")
        self.assertEqual(temporary["state"], "resolved")
        self.assertEqual(temporary["canonical_id"], route_id)
        self.assertEqual(
            self.store.get(obligation_id)["related_route_ids"], [route_id]
        )
        self.assertEqual(self.store.get(obligation_id).revision, 2)
        self.assertEqual(self.store.get(memo_id)["related_route_ids"], [route_id])
        self.assertEqual(self.store.get(memo_id).metadata_version, 2)
        self.assertEqual(self.store.get(route_id)["relevant_memo_ids"], [memo_id])
        self.assertEqual(
            self.store.get(route_id)["related_obligation_ids"], [obligation_id]
        )
        self.assertEqual(self.store.get(route_id).revision, 2)

        replay = self.store.update_route(
            "synthesized-route-update",
            patch,
            proposal_id="TMP-UPDATED-ROUTE",
        )
        self.assertTrue(replay.replayed)
        self.assertEqual(self.store.get(route_id).revision, 2)
        self.assertEqual(self.store.get(obligation_id).revision, 2)
        self.assertEqual(self.store.get(memo_id).metadata_version, 2)
        with self.assertRaises(IdempotencyConflict):
            self.store.update_route(
                "synthesized-route-update",
                {**patch, "explanation": "A different replay."},
                proposal_id="TMP-UPDATED-ROUTE",
            )

    def test_obligation_update_resolves_simple_and_deferred_references(self) -> None:
        source_route = self.store.add_route(
            "route-waits-for-updated-obligation",
            route_payload(related_obligation_ids=["TMP-UPDATED-OBLIGATION"]),
        )
        source_obligation = self.store.add_obligation(
            "relation-waits-for-updated-obligation",
            obligation_payload(
                relations=[
                    {
                        "relation_type": "suffices",
                        "premise_memory_ids": ["TMP-UPDATED-OBLIGATION"],
                        "conclusion": "ROOT",
                        "explanation": "The updated target supplies the local premise.",
                        "supporting_fact_ids": [],
                    }
                ]
            ),
        )
        existing = self.store.add_obligation(
            "existing-obligation-for-update", obligation_payload()
        )
        route_id = source_route.canonical_id
        source_id = source_obligation.canonical_id
        target_id = existing.canonical_id
        assert route_id and source_id and target_id

        result = self.store.update_obligation(
            "synthesized-obligation-update",
            {
                "target_id": target_id,
                "expected_base_revision": 1,
                "set": {},
                "append": {"partial_progress": ["The proposed reduction is available."]},
                "add_ids": {},
                "remove_ids": {},
                "explanation": "Absorb the proposal into the existing obligation.",
                "supporting_memory_ids": [],
            },
            proposal_id="TMP-UPDATED-OBLIGATION",
        )
        self.assertEqual(result.status, "committed")
        self.assertEqual(
            self.store.get(route_id)["related_obligation_ids"], [target_id]
        )
        self.assertEqual(self.store.get(route_id).revision, 2)
        relation = self.store.get(source_id)["relations"][0]
        self.assertEqual(relation["premise_memory_ids"], [target_id])
        self.assertEqual(self.store.get(source_id).revision, 2)
        self.assertEqual(self.store.get(target_id).revision, 2)
        self.assertEqual(
            self.store.temporary_reference_status("TMP-UPDATED-OBLIGATION")[
                "state"
            ],
            "resolved",
        )

    def test_invalid_synthesized_update_is_terminal_for_its_proposal_id(self) -> None:
        source = self.store.add_memo(
            "memo-waits-for-corrected-update",
            memo_payload(related_route_ids=["TMP-CORRECTED-UPDATE"]),
        )
        existing = self.store.add_route("existing-corrected-update", route_payload())
        source_id = source.canonical_id
        route_id = existing.canonical_id
        assert source_id and route_id
        invalid = self.store.update_route(
            "invalid-synthesized-update",
            {
                "target_id": route_id,
                "expected_base_revision": 99,
                "set": {},
                "append": {"progress": ["This stale patch must not land."]},
                "add_ids": {},
                "remove_ids": {},
                "explanation": "A stale update.",
                "supporting_memory_ids": [],
            },
            proposal_id="TMP-CORRECTED-UPDATE",
        )
        self.assertEqual(invalid.status, "rejected")
        self.assertEqual(self.store.get(route_id).revision, 1)
        self.assertIsNone(self.store.proposal_mapping("TMP-CORRECTED-UPDATE"))
        self.assertEqual(
            self.store.temporary_reference_status("TMP-CORRECTED-UPDATE")["state"],
            "rejected",
        )
        self.assertEqual(
            self.store.get(source_id)["related_route_ids"],
            ["TMP-CORRECTED-UPDATE(unpublished)"],
        )

        corrected = self.store.update_route(
            "corrected-synthesized-update",
            {
                "target_id": route_id,
                "expected_base_revision": 1,
                "set": {},
                "append": {"progress": ["The corrected patch lands once."]},
                "add_ids": {},
                "remove_ids": {},
                "explanation": "Correct the rejected update.",
                "supporting_memory_ids": [],
            },
            proposal_id="TMP-FRESH-CORRECTED-UPDATE",
        )
        self.assertEqual(corrected.status, "committed")
        self.assertEqual(
            self.store.get(source_id)["related_route_ids"],
            ["TMP-CORRECTED-UPDATE(unpublished)"],
        )
        self.assertEqual(
            self.store.temporary_reference_status("TMP-CORRECTED-UPDATE")["state"],
            "rejected",
        )

    def test_update_resolution_rolls_back_as_one_transaction_on_process_stop(self) -> None:
        self.store.close()
        self.store = _CrashAfterUpdateResolutionStore(
            self.db_path, projection_dir=False
        )
        source = self.store.add_memo(
            "memo-waits-across-update-stop",
            memo_payload(related_route_ids=["TMP-CRASHED-UPDATE"]),
        )
        existing = self.store.add_route("existing-crashed-update", route_payload())
        source_id = source.canonical_id
        route_id = existing.canonical_id
        assert source_id and route_id
        patch = {
            "target_id": route_id,
            "expected_base_revision": 1,
            "set": {},
            "append": {"progress": ["Commit this update atomically."]},
            "add_ids": {},
            "remove_ids": {},
            "explanation": "Crash-boundary update.",
            "supporting_memory_ids": [],
        }
        self.store.crash_next_update_resolution = True
        with self.assertRaisesRegex(
            KeyboardInterrupt, "before update transaction commit"
        ):
            self.store.update_route(
                "crashed-synthesized-update",
                patch,
                proposal_id="TMP-CRASHED-UPDATE",
            )
        self.store.close()
        self.store = MemoryStore(self.db_path, projection_dir=False)
        self.assertIsNone(self.store.operation_status("crashed-synthesized-update"))
        self.assertEqual(self.store.get(route_id).revision, 1)
        self.assertEqual(
            self.store.get(source_id)["related_route_ids"],
            ["TMP-CRASHED-UPDATE"],
        )
        self.assertEqual(
            self.store.temporary_reference_status("TMP-CRASHED-UPDATE")["state"],
            "pending",
        )

        committed = self.store.update_route(
            "crashed-synthesized-update",
            patch,
            proposal_id="TMP-CRASHED-UPDATE",
        )
        self.assertEqual(committed.status, "committed")
        self.assertEqual(self.store.get(route_id).revision, 2)
        self.assertEqual(self.store.get(source_id)["related_route_ids"], [route_id])

    def test_update_type_conflict_rejects_without_retyping_temporary_target(self) -> None:
        source = self.store.add_route(
            "route-reserves-obligation-temp",
            route_payload(related_obligation_ids=["TMP-TYPE-CONFLICT"]),
        )
        existing = self.store.add_route("route-target-type-conflict", route_payload())
        source_id = source.canonical_id
        route_id = existing.canonical_id
        assert source_id and route_id
        result = self.store.update_route(
            "route-update-type-conflict",
            {
                "target_id": route_id,
                "expected_base_revision": 1,
                "set": {},
                "append": {"progress": ["This update must roll back."]},
                "add_ids": {},
                "remove_ids": {},
                "explanation": "The proposal ID has the wrong reserved type.",
                "supporting_memory_ids": [],
            },
            proposal_id="TMP-TYPE-CONFLICT",
        )
        self.assertEqual(result.status, "rejected")
        self.assertIn("reserved for obligation", result.error)
        self.assertIn("temporary_reference_error", result.result)
        self.assertEqual(self.store.get(route_id).revision, 1)
        self.assertEqual(self.store.get(source_id).revision, 1)
        temporary = self.store.temporary_reference_status("TMP-TYPE-CONFLICT")
        self.assertEqual(temporary["memory_type"], "obligation")
        self.assertEqual(temporary["state"], "pending")
        self.assertIsNone(self.store.proposal_mapping("TMP-TYPE-CONFLICT"))

    def test_temporary_id_in_prose_is_rejected_without_raw_replacement(self) -> None:
        result = self.store.add_route(
            "unsafe-route",
            route_payload(
                strategy_description="Assume TMP-PROSE-MEMO without publishing it.",
                relevant_memo_ids=["TMP-PROSE-MEMO"],
            ),
            proposal_id="TMP-UNSAFE-ROUTE",
        )
        self.assertEqual(result.status, "rejected")
        self.assertIn("typed relationship fields", result.error)


if __name__ == "__main__":
    unittest.main()
