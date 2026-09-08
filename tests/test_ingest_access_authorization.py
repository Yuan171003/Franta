from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any

from franta.scheduler import Scheduler, SchedulerError
from franta.store import MemoryStore
from franta.testing import FakeControlStore, SimulatedCrash
from franta.workflows import OperationState


MEMORY_TYPES = ("fact", "route", "memo", "claim", "obligation", "computation")


def _portfolio(**updates: list[str]) -> dict[str, list[str]]:
    value = {kind: [] for kind in MEMORY_TYPES}
    value.update(updates)
    return value


def _brainstorm_report(index: int, *, facts: list[str] | None = None) -> dict[str, Any]:
    return {
        "report_id": f"AR-SEALED-{index}",
        "objective": "Attack the supplied target independently.",
        "if_resume": None,
        "mode": "brainstorm",
        "main_route_ids": [],
        "main_obligation_ids": ["O-target"],
        "perspective": None,
        "portfolio": _portfolio(fact=list(facts or [])),
        "reason": "Keep this task sealed.",
    }


def _research_report(index: int) -> dict[str, Any]:
    return {
        "report_id": f"AR-WIDE-{index}",
        "objective": "Investigate the public route.",
        "if_resume": None,
        "mode": "research",
        "main_route_ids": ["R-public"],
        "main_obligation_ids": [],
        "perspective": None,
        "portfolio": _portfolio(),
        "reason": "Use ordinary project-wide research access.",
    }


def _progress(
    task_id: str,
    attempt: int,
    *,
    progress_id: str,
    sequence: int = 1,
    operations: list[dict[str, Any]] | None = None,
    challenges: list[dict[str, Any]] | None = None,
    computations: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "progress_id": progress_id,
        "task_id": task_id,
        "attempt": attempt,
        "sequence": sequence,
        "is_final": False,
        "operations": list(operations or []),
        "fact_challenges": list(challenges or []),
        "computations": list(computations or []),
    }


def _computation(staging_id: str, **changes: Any) -> dict[str, Any]:
    value: dict[str, Any] = {
        "staging_id": staging_id,
        "description": "Check one exact toy case.",
        "assumptions": "None.",
        "exact_input": "1 + 1",
        "software": {"name": "SageMath", "version": "1"},
        "environment_versions": {},
        "random_seed": None,
        "output": "2",
        "exit_status": 0,
        "error_output": "",
        "interpretation": "The exact value is two.",
        "related_memory_ids": {},
        "fact_candidate_operation_ids": [],
    }
    value.update(changes)
    return value


class IngestAccessAuthorizationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = FakeControlStore()
        records = {
            "O-target": "obligation",
            "O-secret": "obligation",
            "R-public": "route",
            "R-secret": "route",
            "F-allowed": "fact",
            "F-secret": "fact",
            "F-inactive": "fact",
            "M-secret": "memo",
            "CL-secret": "claim",
        }
        for memory_id, memory_type in records.items():
            self.store.records[memory_id] = {
                "id": memory_id,
                "type": memory_type,
                "revision": 1,
                "statement": "The supplied target." if memory_type == "obligation" else None,
                "active": True,
                "status": "active",
            }
        self.store.records["F-inactive"]["active"] = False
        self.store.records["F-inactive"]["status"] = "revoked"
        self.scheduler = Scheduler(self.store)
        self.scheduler.bootstrap(root_problem="Prove ROOT.")
        self.scheduler.commit_initial_trim({"category_ids": []})

    def _start_sealed(self, index: int, *, facts: list[str] | None = None) -> tuple[str, int]:
        task_id = self.scheduler.submit_batch(
            f"B-SEALED-{index}", [_brainstorm_report(index, facts=facts)]
        )[0]
        return task_id, self.scheduler.start_task_attempt(task_id)

    def test_all_nine_operation_kinds_and_challenge_deny_unsupplied_ids(self) -> None:
        task_id, attempt = self._start_sealed(1, facts=["F-allowed"])
        operations = [
            {
                "operation_id": "OP-DENY-FACT",
                "kind": "fact",
                "candidate_id": "FC-DENY",
                "candidate_version": 1,
                "predecessor_fact_ids": ["F-secret"],
            },
            {
                "operation_id": "OP-DENY-ROUTE-UPDATE",
                "kind": "route_update",
                "target_id": "R-secret",
            },
            {
                "operation_id": "OP-DENY-ROUTE-ADD",
                "kind": "route_add",
                "active_fact_ids": ["F-secret"],
            },
            {
                "operation_id": "OP-DENY-MEMO",
                "kind": "memo",
                "related_route_ids": ["R-secret"],
            },
            {
                "operation_id": "OP-DENY-CLAIM-ADD",
                "kind": "claim_add",
                "related_route_ids": ["R-secret"],
            },
            {
                "operation_id": "OP-DENY-CLAIM-REMOVE",
                "kind": "claim_remove",
                "target_id": "CL-secret",
            },
            {
                "operation_id": "OP-DENY-OBLIGATION-ADD",
                "kind": "obligation_add",
                "predecessor_fact_ids": ["F-secret"],
            },
            {
                "operation_id": "OP-DENY-OBLIGATION-UPDATE",
                "kind": "obligation_update",
                "target_id": "O-target",
                "supporting_memory_ids": ["M-secret"],
            },
            {
                "operation_id": "OP-DENY-OBLIGATION-REMOVE",
                "kind": "obligation_remove",
                "target_id": "O-secret",
            },
            {
                "operation_id": "OP-DENY-RELATION",
                "kind": "obligation_add",
                "predecessor_fact_ids": ["F-allowed"],
                "relations": [
                    {
                        "premise_memory_ids": ["F-allowed"],
                        "supporting_fact_ids": ["F-allowed"],
                        "conclusion": "O-secret",
                    }
                ],
            },
        ]
        self.scheduler.ingest_progress(
            _progress(
                task_id,
                attempt,
                progress_id="P-DENY-ALL",
                operations=operations,
                challenges=[
                    {
                        "challenge_id": "CH-DENY",
                        "fact_id": "F-secret",
                        "alleged_failure": "A hidden proof gap.",
                    }
                ],
            )
        )

        state = self.scheduler.state
        for operation in operations:
            record = state["operations"][operation["operation_id"]]
            self.assertEqual(record["state"], OperationState.REJECTED.value)
            self.assertIn("outside_task_access", record["error"])
            self.assertNotIn(operation["operation_id"], self.store.operation_results)
        challenge = state["challenges"]["CH-DENY"]
        self.assertEqual(challenge["state"], OperationState.REJECTED.value)
        self.assertFalse(challenge["access_authorized"])
        self.assertFalse(
            any(
                call.get("kind") == "challenge-verifier"
                for call in state["calls"].values()
            )
        )
        frozen_lane = self.scheduler._sprint_lane_result_locked(
            self.scheduler._state, task_id
        )
        self.assertIsNone(frozen_lane["challenges"][0]["challenged_fact_snapshot"])

    def test_malformed_challenges_reject_independently_of_valid_siblings(self) -> None:
        task_id, attempt = self._start_sealed(13, facts=["F-allowed"])
        invalid_container = _progress(
            task_id,
            attempt,
            progress_id="P-CHALLENGE-CONTAINER",
        )
        invalid_container["fact_challenges"] = {"challenge_id": "CH-NOT-A-LIST"}
        with self.assertRaisesRegex(
            SchedulerError, "progress fact_challenges must be a list"
        ):
            self.scheduler.ingest_progress(invalid_container)

        self.scheduler.ingest_progress(
            _progress(
                task_id,
                attempt,
                progress_id="P-CHALLENGE-SCHEMA",
                challenges=[
                    None,
                    {"challenge_id": "CH-MISSING"},
                    {"challenge_id": "CH-BLANK", "fact_id": "   "},
                    {"challenge_id": "CH-NUMERIC", "fact_id": 42},
                    {
                        "challenge_id": "CH-LEADING-SPACE",
                        "fact_id": " F-allowed",
                    },
                    {
                        "challenge_id": "CH-TRAILING-SPACE",
                        "fact_id": "F-allowed ",
                    },
                    {
                        "challenge_id": "CH-WRONG-TYPE-SEALED",
                        "fact_id": "O-target",
                    },
                    {
                        "challenge_id": "CH-VALID-SIBLING",
                        "fact_id": "F-allowed",
                        "alleged_failure": "A concrete alleged proof gap.",
                    },
                ],
            )
        )
        state = self.scheduler.state
        rejected_ids = (
            "P-CHALLENGE-SCHEMA:invalid-challenge:0",
            "CH-MISSING",
            "CH-BLANK",
            "CH-NUMERIC",
            "CH-LEADING-SPACE",
            "CH-TRAILING-SPACE",
            "CH-WRONG-TYPE-SEALED",
        )
        for challenge_id in rejected_ids:
            challenge = state["challenges"][challenge_id]
            self.assertEqual(challenge["state"], OperationState.REJECTED.value)
            self.assertFalse(challenge["access_authorized"])
        self.assertEqual(
            state["challenges"]["CH-VALID-SIBLING"]["state"],
            OperationState.VERIFYING.value,
        )

    def test_supplied_ids_are_authorized_in_nested_fields(self) -> None:
        task_id, attempt = self._start_sealed(2, facts=["F-allowed"])
        self.scheduler.ingest_progress(
            _progress(
                task_id,
                attempt,
                progress_id="P-ALLOW-SUPPLIED",
                operations=[
                    {
                        "operation_id": "OP-ALLOW-FACT",
                        "kind": "fact",
                        "proposal_id": "TMP-ALLOW-FACT",
                        "candidate_id": "FC-ALLOW",
                        "candidate_version": 1,
                        "statement": "A supplied consequence holds.",
                        "proof": "By F-allowed.",
                        "predecessor_fact_ids": ["F-allowed"],
                        "abstract": "A supplied consequence.",
                    },
                    {
                        "operation_id": "OP-ALLOW-OBLIGATION",
                        "kind": "obligation_add",
                        "proposal_id": "TMP-ALLOW-O",
                        "predecessor_fact_ids": ["F-allowed"],
                        "related_route_ids": [],
                        "relations": [
                            {
                                "premise_memory_ids": ["F-allowed", "O-target"],
                                "supporting_fact_ids": ["F-allowed"],
                                "conclusion": "ROOT",
                            }
                        ],
                    },
                ],
                challenges=[
                    {
                        "challenge_id": "CH-ALLOW",
                        "fact_id": "F-allowed",
                        "alleged_failure": "Check the supplied proof.",
                    }
                ],
            )
        )
        state = self.scheduler.state
        self.assertEqual(
            state["operations"]["OP-ALLOW-FACT"]["state"],
            OperationState.SYNTHESIZING.value,
        )
        self.assertEqual(
            state["operations"]["OP-ALLOW-OBLIGATION"]["state"],
            OperationState.SYNTHESIZING.value,
        )
        self.assertEqual(
            state["challenges"]["CH-ALLOW"]["state"],
            OperationState.VERIFYING.value,
        )

    def test_typed_canonical_references_enforce_type_and_active_after_restart(self) -> None:
        task_id = self.scheduler.submit_batch(
            "B-TYPED-CANONICAL", [_research_report(35)]
        )[0]
        attempt = self.scheduler.start_task_attempt(task_id)
        progress = _progress(
            task_id,
            attempt,
            progress_id="P-TYPED-CANONICAL",
            operations=[
                {
                    "operation_id": "OP-WRONG-PREDECESSOR-TYPE",
                    "kind": "fact",
                    "candidate_id": "FC-WRONG-PREDECESSOR-TYPE",
                    "candidate_version": 1,
                    "predecessor_fact_ids": ["R-public"],
                },
                {
                    "operation_id": "OP-INACTIVE-PREDECESSOR",
                    "kind": "fact",
                    "candidate_id": "FC-INACTIVE-PREDECESSOR",
                    "candidate_version": 1,
                    "predecessor_fact_ids": ["F-inactive"],
                },
                {
                    "operation_id": "OP-WRONG-ACTIVE-FACT-TYPE",
                    "kind": "route_add",
                    "active_fact_ids": ["R-public"],
                },
                {
                    "operation_id": "OP-WRONG-RELATED-ROUTE-TYPE",
                    "kind": "obligation_add",
                    "related_route_ids": ["O-target"],
                },
            ],
            computations=[
                _computation(
                    "CAS-WRONG-FACT-TYPE",
                    related_memory_ids={"fact": ["R-public"]},
                )
            ],
        )
        self.scheduler.ingest_progress(progress, authenticated_computations=True)
        rejected_operation_ids = (
            "OP-WRONG-PREDECESSOR-TYPE",
            "OP-INACTIVE-PREDECESSOR",
            "OP-WRONG-ACTIVE-FACT-TYPE",
            "OP-WRONG-RELATED-ROUTE-TYPE",
        )
        for operation_id in rejected_operation_ids:
            operation = self.scheduler.state["operations"][operation_id]
            self.assertEqual(operation["state"], OperationState.REJECTED.value)
            self.assertIn("outside_task_access", operation["error"])
            self.assertNotIn(operation_id, self.store.operation_results)
        computation = self.scheduler.state["computations"]["CAS-WRONG-FACT-TYPE"]
        self.assertEqual(computation["state"], OperationState.REJECTED.value)
        self.assertIn("outside_task_access", computation["error"])

        self.scheduler = Scheduler(self.store)
        self.scheduler.reconcile_pending_ingestion()
        for operation_id in rejected_operation_ids:
            self.assertEqual(
                self.scheduler.state["operations"][operation_id]["state"],
                OperationState.REJECTED.value,
            )
        replay = self.scheduler.ingest_progress(
            progress, authenticated_computations=True
        )
        self.assertTrue(
            all(replay[item] == OperationState.REJECTED.value for item in replay)
        )

    def test_malformed_fact_predecessor_shapes_reject_without_routing(self) -> None:
        task_id = self.scheduler.submit_batch(
            "B-MALFORMED-PREDECESSORS", [_research_report(36)]
        )[0]
        attempt = self.scheduler.start_task_attempt(task_id)
        operation_ids = (
            "OP-PREDECESSOR-SCALAR",
            "OP-PREDECESSOR-NUMERIC",
            "OP-PREDECESSOR-MAPPING",
            "OP-PREDECESSOR-LEADING-SPACE",
            "OP-PREDECESSOR-TRAILING-SPACE",
        )
        self.scheduler.ingest_progress(
            _progress(
                task_id,
                attempt,
                progress_id="P-MALFORMED-PREDECESSORS",
                operations=[
                    {
                        "operation_id": operation_ids[0],
                        "kind": "fact",
                        "proposal_id": "TMP-PREDECESSOR-SCALAR",
                        "candidate_id": "FC-PREDECESSOR-SCALAR",
                        "candidate_version": 1,
                        "predecessor_fact_ids": "F-allowed",
                    },
                    {
                        "operation_id": operation_ids[1],
                        "kind": "fact",
                        "proposal_id": "TMP-PREDECESSOR-NUMERIC",
                        "candidate_id": "FC-PREDECESSOR-NUMERIC",
                        "candidate_version": 1,
                        "predecessor_fact_ids": [42],
                    },
                    {
                        "operation_id": operation_ids[2],
                        "kind": "fact",
                        "proposal_id": "TMP-PREDECESSOR-MAPPING",
                        "candidate_id": "FC-PREDECESSOR-MAPPING",
                        "candidate_version": 1,
                        "predecessor_fact_ids": [{"id": "F-allowed"}],
                    },
                    {
                        "operation_id": operation_ids[3],
                        "kind": "fact",
                        "proposal_id": "TMP-PREDECESSOR-LEADING-SPACE",
                        "candidate_id": "FC-PREDECESSOR-LEADING-SPACE",
                        "candidate_version": 1,
                        "predecessor_fact_ids": [" F-allowed"],
                    },
                    {
                        "operation_id": operation_ids[4],
                        "kind": "fact",
                        "proposal_id": "TMP-PREDECESSOR-TRAILING-SPACE",
                        "candidate_id": "FC-PREDECESSOR-TRAILING-SPACE",
                        "candidate_version": 1,
                        "predecessor_fact_ids": ["F-allowed "],
                    },
                    {
                        "operation_id": "OP-PREDECESSOR-VALID-SIBLING",
                        "kind": "memo",
                        "abstract": "A valid sibling survives malformed facts.",
                        "genre": "normal",
                        "content": "Sibling artifact independence is preserved.",
                        "related_route_ids": [],
                    },
                ],
            )
        )
        for operation_id in operation_ids:
            operation = self.scheduler.state["operations"][operation_id]
            self.assertEqual(operation["state"], OperationState.REJECTED.value)
            self.assertIn("expected a list of nonempty string IDs", operation["error"])
            self.assertNotIn(operation_id, self.store.operation_results)
        self.assertEqual(
            self.scheduler.state["operations"]["OP-PREDECESSOR-VALID-SIBLING"][
                "state"
            ],
            OperationState.COMMITTED.value,
        )

        self.scheduler = Scheduler(self.store)
        self.scheduler.reconcile_pending_ingestion()
        for operation_id in operation_ids:
            self.assertEqual(
                self.scheduler.state["operations"][operation_id]["state"],
                OperationState.REJECTED.value,
            )

    def test_access_denied_declarations_cascade_to_dependents_in_any_order(self) -> None:
        task_id, attempt = self._start_sealed(14)
        self.scheduler.ingest_progress(
            _progress(
                task_id,
                attempt,
                progress_id="P-ACCESS-CASCADE",
                operations=[
                    {
                        "operation_id": "OP-CASCADE-MEMO",
                        "kind": "memo",
                        "proposal_id": "TMP-CASCADE-MEMO",
                        "abstract": "A transitive dependent memo.",
                        "genre": "normal",
                        "content": "This must not retain a stranded route reference.",
                        "related_route_ids": ["TMP-CASCADE-ROUTE"],
                    },
                    {
                        "operation_id": "OP-CASCADE-ROUTE",
                        "kind": "route_add",
                        "proposal_id": "TMP-CASCADE-ROUTE",
                        "related_obligation_ids": ["TMP-CASCADE-OBLIGATION"],
                    },
                    {
                        "operation_id": "OP-CASCADE-OBLIGATION",
                        "kind": "obligation_add",
                        "proposal_id": "TMP-CASCADE-OBLIGATION",
                        "predecessor_fact_ids": ["F-secret"],
                    },
                    {
                        "operation_id": "OP-CASCADE-VALID-SIBLING",
                        "kind": "memo",
                        "abstract": "An unrelated valid sibling.",
                        "genre": "normal",
                        "content": "Independent progress remains publishable.",
                        "related_route_ids": [],
                    },
                ],
            )
        )
        for operation_id in (
            "OP-CASCADE-MEMO",
            "OP-CASCADE-ROUTE",
            "OP-CASCADE-OBLIGATION",
        ):
            operation = self.scheduler.state["operations"][operation_id]
            self.assertEqual(operation["state"], OperationState.REJECTED.value)
            self.assertIn("outside_task_access", operation["error"])
            self.assertNotIn(operation_id, self.store.operation_results)
        self.assertEqual(
            self.scheduler.state["operations"]["OP-CASCADE-VALID-SIBLING"]["state"],
            OperationState.COMMITTED.value,
        )

    def test_valid_forward_cycle_survives_access_fixpoint(self) -> None:
        task_id, attempt = self._start_sealed(15)
        self.scheduler.ingest_progress(
            _progress(
                task_id,
                attempt,
                progress_id="P-ACCESS-CYCLE",
                operations=[
                    {
                        "operation_id": "OP-CYCLE-OBLIGATION",
                        "kind": "obligation_add",
                        "proposal_id": "TMP-CYCLE-OBLIGATION",
                        "predecessor_fact_ids": [],
                        "related_route_ids": ["TMP-CYCLE-ROUTE"],
                    },
                    {
                        "operation_id": "OP-CYCLE-ROUTE",
                        "kind": "route_add",
                        "proposal_id": "TMP-CYCLE-ROUTE",
                        "related_obligation_ids": ["TMP-CYCLE-OBLIGATION"],
                    },
                ],
            )
        )
        for operation_id in ("OP-CYCLE-OBLIGATION", "OP-CYCLE-ROUTE"):
            operation = self.scheduler.state["operations"][operation_id]
            self.assertEqual(operation["state"], OperationState.SYNTHESIZING.value)
            self.assertTrue(operation["access_authorized"])
            self.assertNotIn("outside_task_access", str(operation.get("error") or ""))

    def test_non_access_rejected_fact_requires_fresh_correction_ids(self) -> None:
        task_id, attempt = self._start_sealed(16)
        self.scheduler.ingest_progress(
            _progress(
                task_id,
                attempt,
                progress_id="P-LATE-SCHEMA-REJECTION",
                operations=[
                    {
                        "operation_id": "OP-LATE-SCHEMA-PROPOSAL",
                        "kind": "fact",
                        "proposal_id": "TMP-LATE-SCHEMA-FACT",
                        "candidate_id": "FC-LATE-SCHEMA",
                        "candidate_version": 0,
                        "predecessor_fact_ids": [],
                    },
                    {
                        "operation_id": "OP-LATE-SCHEMA-DEPENDENT",
                        "kind": "fact",
                        "proposal_id": "TMP-LATE-SCHEMA-DEPENDENT",
                        "candidate_id": "FC-LATE-SCHEMA-DEPENDENT",
                        "candidate_version": 1,
                        "predecessor_fact_ids": ["TMP-LATE-SCHEMA-FACT"],
                    },
                ],
            )
        )
        for operation_id in (
            "OP-LATE-SCHEMA-PROPOSAL",
            "OP-LATE-SCHEMA-DEPENDENT",
        ):
            operation = self.scheduler.state["operations"][operation_id]
            self.assertEqual(operation["state"], OperationState.REJECTED.value)
            self.assertTrue(operation["access_authorized"])
            self.assertNotIn("outside_task_access", str(operation.get("error") or ""))

        self.scheduler.ingest_progress(
            _progress(
                task_id,
                attempt,
                progress_id="P-LATE-SCHEMA-CORRECTION",
                sequence=2,
                operations=[
                    {
                        "operation_id": "OP-LATE-SCHEMA-CORRECTION",
                        "kind": "fact",
                        "proposal_id": "TMP-LATE-SCHEMA-FACT-CORRECTED",
                        "candidate_id": "FC-LATE-SCHEMA-CORRECTED",
                        "candidate_version": 1,
                        "predecessor_fact_ids": [],
                    }
                ],
            )
        )
        corrected = self.scheduler.state["operations"]["OP-LATE-SCHEMA-CORRECTION"]
        self.assertEqual(corrected["state"], OperationState.SYNTHESIZING.value)
        self.assertTrue(corrected["access_authorized"])

    def test_identity_invalid_declarations_never_grant_temporary_ownership(self) -> None:
        foreign_task, foreign_attempt = self._start_sealed(17)
        self.scheduler.ingest_progress(
            _progress(
                foreign_task,
                foreign_attempt,
                progress_id="P-IDENTITY-FOREIGN-OWNER",
                operations=[
                    {
                        "operation_id": "OP-IDENTITY-FOREIGN",
                        "kind": "route_add",
                        "proposal_id": "TMP-IDENTITY-FOREIGN-OWNER",
                        "related_obligation_ids": ["O-target"],
                    }
                ],
            )
        )

        task_id, attempt = self._start_sealed(18)
        self.scheduler.ingest_progress(
            _progress(
                task_id,
                attempt,
                progress_id="P-IDENTITY-INVALID",
                operations=[
                    {
                        "operation_id": 42,
                        "kind": "route_add",
                        "proposal_id": "TMP-IDENTITY-NUMERIC",
                        "related_obligation_ids": ["O-target"],
                    },
                    {
                        "operation_id": "OP-IDENTITY-NUMERIC-DEPENDENT",
                        "kind": "memo",
                        "related_route_ids": ["TMP-IDENTITY-NUMERIC"],
                    },
                    {
                        "operation_id": "   ",
                        "kind": "route_add",
                        "proposal_id": "TMP-IDENTITY-BLANK",
                        "related_obligation_ids": ["O-target"],
                    },
                    {
                        "operation_id": "OP-IDENTITY-BLANK-DEPENDENT",
                        "kind": "memo",
                        "related_route_ids": ["TMP-IDENTITY-BLANK"],
                    },
                    {
                        "operation_id": "OP-IDENTITY-NUMERIC-PROPOSAL",
                        "kind": "route_add",
                        "proposal_id": 99,
                        "related_obligation_ids": ["O-target"],
                    },
                    {
                        "operation_id": "OP-IDENTITY-NUMERIC-PROPOSAL-DEPENDENT",
                        "kind": "memo",
                        "related_route_ids": ["99"],
                    },
                    {
                        "operation_id": "OP-IDENTITY-DUPLICATE",
                        "kind": "route_add",
                        "proposal_id": "TMP-IDENTITY-DUPLICATE-VALID",
                        "related_obligation_ids": ["O-target"],
                    },
                    {
                        "operation_id": "OP-IDENTITY-DUPLICATE",
                        "kind": "route_add",
                        "proposal_id": "TMP-IDENTITY-DUPLICATE-INVALID",
                        "related_obligation_ids": ["O-target"],
                    },
                    {
                        "operation_id": "OP-IDENTITY-DUPLICATE-DEPENDENT",
                        "kind": "memo",
                        "related_route_ids": ["TMP-IDENTITY-DUPLICATE-INVALID"],
                    },
                    {
                        "operation_id": "OP-IDENTITY-FOREIGN",
                        "kind": "route_add",
                        "proposal_id": "TMP-IDENTITY-FOREIGN-REPLAY",
                        "related_obligation_ids": ["O-target"],
                    },
                    {
                        "operation_id": "OP-IDENTITY-FOREIGN-DEPENDENT",
                        "kind": "memo",
                        "related_route_ids": ["TMP-IDENTITY-FOREIGN-REPLAY"],
                    },
                    {
                        "operation_id": "OP-IDENTITY-VALID-SIBLING",
                        "kind": "memo",
                        "abstract": "A valid independent identity sibling.",
                        "genre": "normal",
                        "content": "This operation remains publishable.",
                        "related_route_ids": [],
                    },
                ],
            )
        )
        for operation_id in (
            "OP-IDENTITY-NUMERIC-DEPENDENT",
            "OP-IDENTITY-BLANK-DEPENDENT",
            "OP-IDENTITY-NUMERIC-PROPOSAL-DEPENDENT",
            "OP-IDENTITY-DUPLICATE-DEPENDENT",
            "OP-IDENTITY-FOREIGN-DEPENDENT",
        ):
            operation = self.scheduler.state["operations"][operation_id]
            self.assertEqual(operation["state"], OperationState.REJECTED.value)
            self.assertIn("outside_task_access", operation["error"])
        self.assertEqual(
            self.scheduler.state["operations"]["OP-IDENTITY-DUPLICATE"]["state"],
            OperationState.SYNTHESIZING.value,
        )
        self.assertEqual(
            self.scheduler.state["operations"]["OP-IDENTITY-VALID-SIBLING"]["state"],
            OperationState.COMMITTED.value,
        )

        self.scheduler.ingest_progress(
            _progress(
                task_id,
                attempt,
                progress_id="P-IDENTITY-REPLAY-CONFLICT",
                sequence=2,
                operations=[
                    {
                        "operation_id": "OP-IDENTITY-DUPLICATE",
                        "kind": "route_add",
                        "proposal_id": "TMP-IDENTITY-REPLAY-CONFLICT",
                        "related_obligation_ids": [],
                    },
                    {
                        "operation_id": "OP-IDENTITY-REPLAY-DEPENDENT",
                        "kind": "memo",
                        "related_route_ids": ["TMP-IDENTITY-REPLAY-CONFLICT"],
                    },
                ],
            )
        )
        replay_dependent = self.scheduler.state["operations"][
            "OP-IDENTITY-REPLAY-DEPENDENT"
        ]
        self.assertEqual(replay_dependent["state"], OperationState.REJECTED.value)
        self.assertIn("outside_task_access", replay_dependent["error"])

    def test_sealed_cas_checks_memory_and_fact_operation_ownership(self) -> None:
        wide_task = self.scheduler.submit_batch("B-WIDE-FACT", [_research_report(1)])[0]
        wide_attempt = self.scheduler.start_task_attempt(wide_task)
        self.scheduler.ingest_progress(
            _progress(
                wide_task,
                wide_attempt,
                progress_id="P-WIDE-FACT",
                operations=[
                    {
                        "operation_id": "OP-FOREIGN-FACT",
                        "kind": "fact",
                        "candidate_id": "FC-FOREIGN",
                        "candidate_version": 1,
                        "statement": "A foreign candidate.",
                        "proof": "Directly.",
                        "predecessor_fact_ids": [],
                        "abstract": "A foreign candidate.",
                    }
                ],
            )
        )
        task_id, attempt = self._start_sealed(3, facts=["F-allowed"])
        self.scheduler.ingest_progress(
            _progress(
                task_id,
                attempt,
                progress_id="P-SEALED-CAS",
                computations=[
                    _computation(
                        "CAS-ALLOW",
                        related_memory_ids={
                            "fact": ["F-allowed"],
                            "obligation": ["O-target"],
                        },
                    ),
                    _computation(
                        "CAS-DENY-MEMORY",
                        related_memory_ids={"fact": ["F-secret"]},
                    ),
                    _computation(
                        "CAS-DENY-OPERATION",
                        fact_candidate_operation_ids=["OP-FOREIGN-FACT"],
                    ),
                ],
            ),
            authenticated_computations=True,
        )
        computations = self.scheduler.state["computations"]
        self.assertEqual(computations["CAS-ALLOW"]["state"], OperationState.COMMITTED.value)
        for staging_id in ("CAS-DENY-MEMORY", "CAS-DENY-OPERATION"):
            self.assertEqual(
                computations[staging_id]["state"], OperationState.REJECTED.value
            )
            self.assertIn("outside_task_access", computations[staging_id]["error"])
            self.assertNotIn(f"computation:{staging_id}", self.store.operation_results)

    def test_mixed_siblings_replay_and_full_stop_recovery(self) -> None:
        task_id, attempt = self._start_sealed(4)
        progress = _progress(
            task_id,
            attempt,
            progress_id="P-MIXED-CRASH",
            operations=[
                {
                    "operation_id": "OP-MIXED-VALID",
                    "kind": "memo",
                    "abstract": "A self-contained idea.",
                    "genre": "normal",
                    "content": "No canonical premise is used.",
                    "related_route_ids": [],
                },
                {
                    "operation_id": "OP-MIXED-DENIED",
                    "kind": "claim_remove",
                    "target_id": "CL-secret",
                },
            ],
        )
        self.scheduler._route_operation = lambda _operation_id: (_ for _ in ()).throw(
            SimulatedCrash("after durable receipt")
        )
        with self.assertRaises(SimulatedCrash):
            self.scheduler.ingest_progress(progress)

        resumed = Scheduler(self.store)
        self.assertEqual(
            resumed.state["operations"]["OP-MIXED-VALID"]["state"],
            OperationState.RECEIVED.value,
        )
        self.assertEqual(
            resumed.state["operations"]["OP-MIXED-DENIED"]["state"],
            OperationState.REJECTED.value,
        )
        resumed.recover(live_task_ids=[task_id])
        resumed.reconcile_pending_ingestion()
        self.assertEqual(
            resumed.state["operations"]["OP-MIXED-VALID"]["state"],
            OperationState.COMMITTED.value,
        )
        self.assertNotIn("OP-MIXED-DENIED", self.store.operation_results)
        operation_count = len(self.store.operation_results)
        replay = resumed.ingest_progress(progress)
        self.assertEqual(replay["OP-MIXED-DENIED"], OperationState.REJECTED.value)
        self.assertEqual(len(self.store.operation_results), operation_count)

    def test_project_wide_worker_keeps_unsupplied_canonical_access(self) -> None:
        task_id = self.scheduler.submit_batch("B-WIDE", [_research_report(2)])[0]
        attempt = self.scheduler.start_task_attempt(task_id)
        self.scheduler.ingest_progress(
            _progress(
                task_id,
                attempt,
                progress_id="P-WIDE",
                operations=[
                    {
                        "operation_id": "OP-WIDE",
                        "kind": "claim_remove",
                        "target_id": "CL-secret",
                    }
                ],
                challenges=[
                    {
                        "challenge_id": "CH-WIDE",
                        "fact_id": "F-secret",
                        "alleged_failure": "Check this project fact.",
                    }
                ],
                computations=[
                    _computation(
                        "CAS-WIDE",
                        related_memory_ids={
                            "fact": ["F-secret"],
                            "route": ["R-secret"],
                            "memo": ["M-secret"],
                            "claim": ["CL-secret"],
                            "obligation": ["O-secret"],
                        },
                    )
                ],
            ),
            authenticated_computations=True,
        )
        state = self.scheduler.state
        self.assertEqual(state["operations"]["OP-WIDE"]["state"], OperationState.COMMITTED.value)
        self.assertEqual(state["challenges"]["CH-WIDE"]["state"], OperationState.VERIFYING.value)
        self.assertEqual(state["computations"]["CAS-WIDE"]["state"], OperationState.COMMITTED.value)

    def test_foreign_operation_and_challenge_ids_are_not_attached(self) -> None:
        first_task = self.scheduler.submit_batch("B-OWNER-1", [_research_report(3)])[0]
        first_attempt = self.scheduler.start_task_attempt(first_task)
        shared_operation = {
            "operation_id": "OP-SHARED",
            "kind": "memo",
            "abstract": "Shared identifier test.",
            "genre": "normal",
            "content": "First task owns this operation.",
            "related_route_ids": [],
        }
        shared_challenge = {
            "challenge_id": "CH-SHARED",
            "fact_id": "F-allowed",
            "alleged_failure": "Check the proof.",
        }
        self.scheduler.ingest_progress(
            _progress(
                first_task,
                first_attempt,
                progress_id="P-OWNER-1",
                operations=[shared_operation],
                challenges=[shared_challenge],
            )
        )

        second_task, second_attempt = self._start_sealed(5, facts=["F-allowed"])
        self.scheduler.ingest_progress(
            _progress(
                second_task,
                second_attempt,
                progress_id="P-OWNER-2",
                operations=[shared_operation],
                challenges=[shared_challenge],
            )
        )
        state = self.scheduler.state
        self.assertEqual(state["operations"]["OP-SHARED"]["task_id"], first_task)
        invalid_operations = [
            item
            for item in state["operations"].values()
            if item.get("requested_operation_id") == "OP-SHARED"
        ]
        self.assertEqual(len(invalid_operations), 1)
        self.assertEqual(invalid_operations[0]["state"], OperationState.REJECTED.value)
        self.assertEqual(state["challenges"]["CH-SHARED"]["task_id"], first_task)
        invalid_challenges = [
            item
            for item in state["challenges"].values()
            if item.get("requested_challenge_id") == "CH-SHARED"
        ]
        self.assertEqual(len(invalid_challenges), 1)
        self.assertEqual(invalid_challenges[0]["state"], OperationState.REJECTED.value)

    def test_proposal_and_candidate_control_ids_keep_one_task_owner(self) -> None:
        first_task = self.scheduler.submit_batch("B-CONTROL-1", [_research_report(30)])[0]
        first_attempt = self.scheduler.start_task_attempt(first_task)
        self.scheduler.ingest_progress(
            _progress(
                first_task,
                first_attempt,
                progress_id="P-CONTROL-1",
                operations=[
                    {
                        "operation_id": "OP-PROPOSAL-OWNER",
                        "kind": "memo",
                        "proposal_id": "TMP-SHARED-PROPOSAL",
                        "abstract": "The first task owns this proposal.",
                        "genre": "normal",
                        "content": "Ownership is scheduler control data.",
                        "related_route_ids": [],
                    },
                    {
                        "operation_id": "OP-CANDIDATE-OWNER",
                        "kind": "fact",
                        "proposal_id": "TMP-CANDIDATE-OWNER",
                        "candidate_id": "FC-SHARED-CANDIDATE",
                        "candidate_version": 1,
                        "statement": "The first task's candidate.",
                        "proof": "Directly.",
                        "predecessor_fact_ids": [],
                        "abstract": "The first task's candidate.",
                    },
                ],
            )
        )

        second_task = self.scheduler.submit_batch("B-CONTROL-2", [_research_report(31)])[0]
        second_attempt = self.scheduler.start_task_attempt(second_task)
        self.scheduler.ingest_progress(
            _progress(
                second_task,
                second_attempt,
                progress_id="P-CONTROL-2",
                operations=[
                    {
                        "operation_id": "OP-FOREIGN-PROPOSAL",
                        "kind": "memo",
                        "proposal_id": "TMP-SHARED-PROPOSAL",
                        "abstract": "A foreign proposal reuse.",
                        "genre": "normal",
                        "content": "This must be rejected before publication.",
                        "related_route_ids": [],
                    },
                    {
                        "operation_id": "OP-FOREIGN-CANDIDATE",
                        "kind": "fact",
                        "proposal_id": "TMP-FOREIGN-CANDIDATE",
                        "candidate_id": "FC-SHARED-CANDIDATE",
                        "candidate_version": 1,
                        "statement": "A foreign candidate reuse.",
                        "proof": "Directly.",
                        "predecessor_fact_ids": [],
                        "abstract": "A foreign candidate reuse.",
                    },
                    {
                        "operation_id": "OP-FOREIGN-FACT-PROPOSAL",
                        "kind": "fact",
                        "proposal_id": "TMP-CANDIDATE-OWNER",
                        "candidate_id": "FC-FRESH-FOREIGN-PROPOSAL",
                        "candidate_version": 1,
                        "statement": "A foreign Fact proposal reuse.",
                        "proof": "Directly.",
                        "predecessor_fact_ids": [],
                        "abstract": "A foreign Fact proposal reuse.",
                    },
                ],
            )
        )
        state = self.scheduler.state
        self.assertEqual(
            state["operations"]["OP-FOREIGN-PROPOSAL"]["state"],
            OperationState.REJECTED.value,
        )
        self.assertIn(
            "belongs to another task",
            state["operations"]["OP-FOREIGN-PROPOSAL"]["error"],
        )
        self.assertEqual(
            state["operations"]["OP-FOREIGN-CANDIDATE"]["state"],
            OperationState.REJECTED.value,
        )
        self.assertIn(
            "fresh candidate_id",
            state["operations"]["OP-FOREIGN-CANDIDATE"]["error"],
        )
        self.assertEqual(
            state["operations"]["OP-FOREIGN-FACT-PROPOSAL"]["state"],
            OperationState.REJECTED.value,
        )
        self.assertIn(
            "belongs to another task",
            state["operations"]["OP-FOREIGN-FACT-PROPOSAL"]["error"],
        )
        self.assertEqual(
            state["operations"]["OP-CANDIDATE-OWNER"]["state"],
            OperationState.SYNTHESIZING.value,
        )
        self.assertEqual(
            state["fact_proposals"]["TMP-CANDIDATE-OWNER"]["state"],
            "pending",
        )

        restarted = Scheduler(self.store)
        restarted.recover()
        restarted.reconcile_pending_ingestion()
        replayed = restarted.state
        self.assertEqual(
            replayed["operations"]["OP-CANDIDATE-OWNER"]["state"],
            OperationState.SYNTHESIZING.value,
        )
        self.assertEqual(
            replayed["fact_proposals"]["TMP-CANDIDATE-OWNER"]["state"],
            "pending",
        )

    def test_project_wide_access_does_not_grant_foreign_proposal_staging(self) -> None:
        first_task = self.scheduler.submit_batch("B-TEMP-OWNER", [_research_report(32)])[0]
        first_attempt = self.scheduler.start_task_attempt(first_task)
        self.scheduler.ingest_progress(
            _progress(
                first_task,
                first_attempt,
                progress_id="P-TEMP-OWNER",
                operations=[
                    {
                        "operation_id": "OP-TEMP-OWNER-PLAIN",
                        "kind": "route_add",
                        "proposal_id": "TMP-FOREIGN-ROUTE",
                        "related_obligation_ids": [],
                    },
                    {
                        "operation_id": "OP-TEMP-OWNER-CANONICAL-LOOKING",
                        "kind": "route_add",
                        "proposal_id": "R-FOREIGN-ROUTE",
                        "related_obligation_ids": [],
                    },
                ],
            )
        )

        second_task = self.scheduler.submit_batch("B-TEMP-BORROW", [_research_report(33)])[0]
        second_attempt = self.scheduler.start_task_attempt(second_task)
        borrow_progress = _progress(
            second_task,
            second_attempt,
            progress_id="P-TEMP-BORROW",
            operations=[
                {
                    "operation_id": "OP-TEMP-BORROW-PLAIN",
                    "kind": "memo",
                    "abstract": "Attempt to borrow another task's staging ID.",
                    "genre": "normal",
                    "content": "Project-wide canonical access is not proposal access.",
                    "related_route_ids": ["TMP-FOREIGN-ROUTE"],
                },
                {
                    "operation_id": "OP-TEMP-BORROW-CANONICAL-LOOKING",
                    "kind": "memo",
                    "abstract": "Attempt to borrow a canonical-looking staging ID.",
                    "genre": "normal",
                    "content": "A prefix does not turn another task's proposal into canonical access.",
                    "related_route_ids": ["R-FOREIGN-ROUTE"],
                },
            ],
        )
        self.scheduler.ingest_progress(borrow_progress)
        for operation_id in (
            "OP-TEMP-BORROW-PLAIN",
            "OP-TEMP-BORROW-CANONICAL-LOOKING",
        ):
            operation = self.scheduler.state["operations"][operation_id]
            self.assertEqual(operation["state"], OperationState.REJECTED.value)
            self.assertIn("outside_task_access", operation["error"])

        self.scheduler = Scheduler(self.store)
        replay = self.scheduler.ingest_progress(borrow_progress)
        self.assertEqual(
            replay["OP-TEMP-BORROW-PLAIN"], OperationState.REJECTED.value
        )
        self.scheduler.ingest_progress(
            _progress(
                first_task,
                first_attempt,
                progress_id="P-TEMP-OWNER-AFTER-SQUAT",
                sequence=2,
                operations=[
                    {
                        "operation_id": "OP-TEMP-OWNER-MEMO-AFTER-SQUAT",
                        "kind": "memo",
                        "abstract": "The true owner still uses its own staging ID.",
                        "genre": "normal",
                        "content": "A rejected foreign declaration cannot poison ownership.",
                        "related_route_ids": ["TMP-FOREIGN-ROUTE"],
                    }
                ],
            )
        )
        owner_operation = self.scheduler.state["operations"][
            "OP-TEMP-OWNER-MEMO-AFTER-SQUAT"
        ]
        self.assertNotEqual(owner_operation["state"], OperationState.REJECTED.value)
        self.assertNotIn(
            "outside_task_access", str(owner_operation.get("error") or "")
        )

    def test_project_wide_access_does_not_authorize_nonexistent_canonical_ids(self) -> None:
        task_id = self.scheduler.submit_batch(
            "B-WIDE-GHOST", [_research_report(34)]
        )[0]
        attempt = self.scheduler.start_task_attempt(task_id)
        self.scheduler.ingest_progress(
            _progress(
                task_id,
                attempt,
                progress_id="P-WIDE-GHOST",
                operations=[
                    {
                        "operation_id": "OP-WIDE-GHOST",
                        "kind": "route_update",
                        "target_id": "R-GHOST",
                    }
                ],
                challenges=[
                    {
                        "challenge_id": "CH-WIDE-GHOST",
                        "fact_id": "F-GHOST",
                        "alleged_failure": "This nonexistent fact cannot be challenged.",
                    },
                    {
                        "challenge_id": "CH-WIDE-WRONG-TYPE",
                        "fact_id": "R-public",
                        "alleged_failure": "A route is not a fact.",
                    },
                ],
            )
        )
        state = self.scheduler.state
        operation = state["operations"]["OP-WIDE-GHOST"]
        self.assertEqual(operation["state"], OperationState.REJECTED.value)
        self.assertIn("outside_task_access", operation["error"])
        for challenge_id in ("CH-WIDE-GHOST", "CH-WIDE-WRONG-TYPE"):
            challenge = state["challenges"][challenge_id]
            self.assertEqual(challenge["state"], OperationState.REJECTED.value)
            self.assertFalse(challenge["access_authorized"])
        self.assertFalse(
            any(
                call.get("kind") == "challenge-verifier"
                for call in state["calls"].values()
            )
        )

    def test_duplicate_proposal_declarations_are_independently_rejected(self) -> None:
        task_id, attempt = self._start_sealed(6)
        self.scheduler.ingest_progress(
            _progress(
                task_id,
                attempt,
                progress_id="P-DUPLICATE-PROPOSAL",
                operations=[
                    {
                        "operation_id": "OP-DUPLICATE-PROPOSAL-1",
                        "kind": "memo",
                        "proposal_id": "TMP-DUPLICATE",
                        "related_route_ids": [],
                    },
                    {
                        "operation_id": "OP-DUPLICATE-PROPOSAL-2",
                        "kind": "memo",
                        "proposal_id": "TMP-DUPLICATE",
                        "related_route_ids": [],
                    },
                ],
            )
        )
        for operation_id in (
            "OP-DUPLICATE-PROPOSAL-1",
            "OP-DUPLICATE-PROPOSAL-2",
        ):
            operation = self.scheduler.state["operations"][operation_id]
            self.assertEqual(operation["state"], OperationState.REJECTED.value)
            self.assertIn("duplicated within this progress file", operation["error"])

    def test_canonical_looking_owned_temp_is_not_misclassified_or_allowed_to_shadow(self) -> None:
        task_id, attempt = self._start_sealed(7)
        self.scheduler.ingest_progress(
            _progress(
                task_id,
                attempt,
                progress_id="P-PREFIX-TEMP",
                operations=[
                    {
                        "operation_id": "OP-PREFIX-ROUTE",
                        "kind": "route_add",
                        "proposal_id": "R-TEMP-OWNED",
                        "related_obligation_ids": ["O-target"],
                    },
                    {
                        "operation_id": "OP-PREFIX-MEMO",
                        "kind": "memo",
                        "proposal_id": "TMP-PREFIX-MEMO",
                        "related_route_ids": ["R-TEMP-OWNED"],
                    },
                    {
                        "operation_id": "OP-SHADOW-ROUTE",
                        "kind": "route_add",
                        "proposal_id": "R-secret",
                        "related_obligation_ids": ["O-target"],
                    },
                    {
                        "operation_id": "OP-SHADOW-MEMO",
                        "kind": "memo",
                        "proposal_id": "TMP-SHADOW-MEMO",
                        "related_route_ids": ["R-secret"],
                    },
                ],
            )
        )
        state = self.scheduler.state
        self.assertNotIn(
            "outside_task_access",
            str(state["operations"]["OP-PREFIX-MEMO"].get("error") or ""),
        )
        for operation_id in ("OP-SHADOW-ROUTE", "OP-SHADOW-MEMO"):
            self.assertEqual(
                state["operations"][operation_id]["state"],
                OperationState.REJECTED.value,
            )
            self.assertIn(
                "outside_task_access", state["operations"][operation_id]["error"]
            )


class TaskOwnedMemoryAuthorizationTests(unittest.TestCase):
    def test_final_receipt_rejects_access_denied_proposal_and_dependent_before_store(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            store = MemoryStore(Path(raw) / "memory.sqlite3", projection_dir=False)
            try:
                scheduler = Scheduler(store)
                root_id = scheduler.bootstrap(root_problem="Prove ROOT.")
                store.add_obligation(
                    "bootstrap-root-fixpoint",
                    {
                        "id": root_id,
                        "abstract": "The root target.",
                        "statement": "ROOT holds.",
                        "importance": "This is the project target.",
                        "predecessor_fact_ids": [],
                        "partial_progress": [],
                        "related_route_ids": [],
                        "relations": [],
                    },
                )
                scheduler.bind_canonical_root_obligation()
                hidden = store.add_obligation(
                    "hidden-obligation-fixpoint",
                    {
                        "abstract": "A hidden auxiliary target.",
                        "statement": "The hidden auxiliary target holds.",
                        "importance": "It must not enter the sealed task.",
                        "predecessor_fact_ids": [],
                        "partial_progress": [],
                        "related_route_ids": [],
                        "relations": [],
                    },
                )
                scheduler.commit_initial_trim({"category_ids": []})
                report = _brainstorm_report(21)
                report["main_obligation_ids"] = [root_id]
                task_id = scheduler.submit_batch("B-FIXPOINT-FINAL", [report])[0]
                attempt = scheduler.start_task_attempt(task_id)
                progress = _progress(
                    task_id,
                    attempt,
                    progress_id="P-FIXPOINT-FINAL",
                    operations=[
                        {
                            "operation_id": "OP-FIXPOINT-DENIED-ROUTE",
                            "kind": "route_add",
                            "proposal_id": "TMP-FIXPOINT-DENIED-ROUTE",
                            "related_obligation_ids": [hidden.canonical_id],
                        },
                        {
                            "operation_id": "OP-FIXPOINT-DEPENDENT-MEMO",
                            "kind": "memo",
                            "proposal_id": "TMP-FIXPOINT-DEPENDENT-MEMO",
                            "abstract": "A memo depending on a denied declaration.",
                            "genre": "normal",
                            "content": "It must reject before creating a pending reference.",
                            "related_route_ids": ["TMP-FIXPOINT-DENIED-ROUTE"],
                        },
                        {
                            "operation_id": "OP-FIXPOINT-VALID-MEMO",
                            "kind": "memo",
                            "proposal_id": "TMP-FIXPOINT-VALID-MEMO",
                            "abstract": "An independent valid memo.",
                            "genre": "normal",
                            "content": "This useful independent observation remains.",
                            "related_route_ids": [],
                        },
                        {
                            "kind": "route_add",
                            "proposal_id": "TMP-FIXPOINT-MISSING-ID-ROUTE",
                            "related_obligation_ids": [root_id],
                        },
                        {
                            "operation_id": "OP-FIXPOINT-MISSING-ID-DEPENDENT",
                            "kind": "memo",
                            "proposal_id": "TMP-FIXPOINT-MISSING-ID-MEMO",
                            "abstract": "A memo depending on an identity-invalid declaration.",
                            "genre": "normal",
                            "content": "It must reject before creating a pending reference.",
                            "related_route_ids": ["TMP-FIXPOINT-MISSING-ID-ROUTE"],
                        },
                    ],
                )
                progress.update(
                    {
                        "is_final": True,
                        "outcome_status": "progress",
                        "completion_evidence_ids": ["OP-FIXPOINT-VALID-MEMO"],
                        "attempt_summary": {
                            "work_mode": "brainstorm",
                            "task": "Attack the root target independently.",
                            "proposed_outcome": "progress",
                            "cumulative_important_progress": "Recorded one independent memo.",
                            "completion_evidence_operation_ids": [
                                "OP-FIXPOINT-VALID-MEMO"
                            ],
                            "most_promising_next_steps": "Try another sealed approach.",
                        },
                    }
                )
                scheduler.ingest_progress(progress)
                for operation_id in (
                    "OP-FIXPOINT-DENIED-ROUTE",
                    "OP-FIXPOINT-DEPENDENT-MEMO",
                    "OP-FIXPOINT-MISSING-ID-DEPENDENT",
                ):
                    operation = scheduler.state["operations"][operation_id]
                    self.assertEqual(operation["state"], OperationState.REJECTED.value)
                    self.assertIn("outside_task_access", operation["error"])
                self.assertEqual(
                    scheduler.state["operations"]["OP-FIXPOINT-VALID-MEMO"]["state"],
                    OperationState.COMMITTED.value,
                )
                self.assertEqual(scheduler.state["tasks"][task_id]["state"], "closed")
                self.assertNotIn(
                    "TMP-FIXPOINT-DENIED-ROUTE",
                    {
                        item["temporary_id"]
                        for item in store.list_temporary_references()
                    },
                )
                self.assertNotIn(
                    "TMP-FIXPOINT-MISSING-ID-ROUTE",
                    {
                        item["temporary_id"]
                        for item in store.list_temporary_references()
                    },
                )

                resumed = Scheduler(store)
                resumed.recover()
                resumed.reconcile_pending_ingestion()
                self.assertEqual(resumed.state["tasks"][task_id]["state"], "closed")
                replay = resumed.ingest_progress(progress)
                self.assertEqual(
                    replay["OP-FIXPOINT-DENIED-ROUTE"],
                    OperationState.REJECTED.value,
                )
                self.assertEqual(
                    replay["OP-FIXPOINT-DEPENDENT-MEMO"],
                    OperationState.REJECTED.value,
                )
                self.assertEqual(
                    replay["OP-FIXPOINT-MISSING-ID-DEPENDENT"],
                    OperationState.REJECTED.value,
                )
                self.assertNotIn(
                    "TMP-FIXPOINT-DENIED-ROUTE",
                    {
                        item["temporary_id"]
                        for item in store.list_temporary_references()
                    },
                )
            finally:
                store.close()

    def test_sealed_task_can_use_its_temporary_and_new_canonical_records(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            store = MemoryStore(Path(raw) / "memory.sqlite3", projection_dir=False)
            try:
                scheduler = Scheduler(store)
                root_id = scheduler.bootstrap(root_problem="Prove ROOT.")
                store.add_obligation(
                    "bootstrap-root-access",
                    {
                        "id": root_id,
                        "abstract": "The root target.",
                        "statement": "ROOT holds.",
                        "importance": "This is the project target.",
                        "predecessor_fact_ids": [],
                        "partial_progress": [],
                        "related_route_ids": [],
                        "relations": [],
                    },
                )
                scheduler.bind_canonical_root_obligation()
                scheduler.commit_initial_trim({"category_ids": []})
                report = _brainstorm_report(20)
                report["main_obligation_ids"] = [root_id]
                task_id = scheduler.submit_batch("B-TASK-OWNED", [report])[0]
                attempt = scheduler.start_task_attempt(task_id)
                scheduler.ingest_progress(
                    _progress(
                        task_id,
                        attempt,
                        progress_id="P-TASK-OWNED-1",
                        operations=[
                            {
                                "operation_id": "OP-OWNED-ROUTE",
                                "kind": "route_add",
                                "proposal_id": "TMP-OWNED-ROUTE",
                                "abstract": "A new route from the sealed task.",
                                "strategy_description": "Attack ROOT through a new invariant.",
                                "value_assessment": {
                                    "confidence": "plausible",
                                    "success_gain": "could resolve ROOT",
                                    "failure_gain": "would isolate the obstruction",
                                    "relevance": "central",
                                    "novelty": "independent",
                                },
                                "progress": [],
                                "related_obligation_ids": [root_id],
                                "next_steps": [],
                                "obstacles": [],
                                "active_fact_ids": [],
                                "relevant_memo_ids": [],
                                "relevant_claim_ids": [],
                            },
                            {
                                "operation_id": "OP-OWNED-MEMO",
                                "kind": "memo",
                                "proposal_id": "TMP-OWNED-MEMO",
                                "abstract": "A memo attached through a temporary route ID.",
                                "genre": "normal",
                                "content": "The new invariant may control the boundary.",
                                "related_route_ids": ["TMP-OWNED-ROUTE"],
                            },
                        ],
                    )
                )
                self.assertNotEqual(
                    scheduler.state["operations"]["OP-OWNED-MEMO"]["state"],
                    OperationState.REJECTED.value,
                )
                route_digest = scheduler.state["operations"]["OP-OWNED-ROUTE"][
                    "input_digest"
                ]
                scheduler.apply_synthesizer_result(
                    "OP-OWNED-ROUTE",
                    {
                        "resolution": "new",
                        "operation_digest": route_digest,
                        "relied_on": [],
                    },
                )
                state = scheduler.state
                self.assertEqual(
                    state["operations"]["OP-OWNED-ROUTE"]["state"],
                    OperationState.COMMITTED.value,
                )
                self.assertEqual(
                    state["operations"]["OP-OWNED-MEMO"]["state"],
                    OperationState.COMMITTED.value,
                )
                memo_id = state["operations"]["OP-OWNED-MEMO"]["canonical_id"]
                route_id = state["operations"]["OP-OWNED-ROUTE"]["canonical_id"]
                self.assertIn(route_id, store.get(memo_id)["related_route_ids"])

                scheduler.ingest_progress(
                    _progress(
                        task_id,
                        attempt,
                        progress_id="P-TASK-OWNED-2",
                        sequence=2,
                        operations=[
                            {
                                "operation_id": "OP-USE-NEW-CANONICAL",
                                "kind": "route_add",
                                "proposal_id": "TMP-SECOND-ROUTE",
                                "abstract": "A second route uses the task's new memo.",
                                "strategy_description": "Combine the memo with ROOT.",
                                "value_assessment": {
                                    "confidence": "plausible",
                                    "success_gain": "could resolve ROOT",
                                    "failure_gain": "would test the memo",
                                    "relevance": "central",
                                    "novelty": "combines task output",
                                },
                                "progress": [],
                                "related_obligation_ids": [root_id],
                                "next_steps": [],
                                "obstacles": [],
                                "active_fact_ids": [],
                                "relevant_memo_ids": [memo_id],
                                "relevant_claim_ids": [],
                            }
                        ],
                    )
                )
                self.assertEqual(
                    scheduler.state["operations"]["OP-USE-NEW-CANONICAL"]["state"],
                    OperationState.SYNTHESIZING.value,
                )
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
