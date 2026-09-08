from __future__ import annotations

import copy
import hashlib
import json
import random
import tempfile
import unittest
from pathlib import Path

from franta.access import policy_for
from franta.config import load_manifest
from franta.runtime import AgentCall, FrantaRuntime
from franta.scheduler import Scheduler, SchedulerError
from franta.skill_runtime import SkillContext, SkillRuntime
from franta.store import MemoryStore
from franta.testing import FakeControlStore, SimulatedCrash
from franta.workflows import OperationState, TaskState


PROJECT_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_PATH = PROJECT_ROOT / "tests/fixtures/r4_k_equivalence_regressions.json"
ROOT_ID = "O-c9d516224f994856a2ded4f16e0f006c"


def _portfolio(**entries: list[str]) -> dict[str, list[str]]:
    result = {
        kind: []
        for kind in ("fact", "route", "memo", "claim", "obligation", "computation")
    }
    result.update(entries)
    return result


def _assignment(portfolio: dict[str, list[str]] | None = None) -> dict[str, object]:
    return {
        "report_id": "AR-CONTRACT",
        "objective": "Exercise one durable progress contract.",
        "if_resume": None,
        "mode": "associate",
        "main_route_ids": [],
        "main_obligation_ids": [],
        "perspective": None,
        "portfolio": portfolio or _portfolio(),
        "reason": "Regression coverage.",
    }


def _final(task_id: str, attempt: int, sequence: int, evidence: list[str]) -> dict:
    return {
        "progress_id": f"PRG-FINAL-{task_id}-{sequence}",
        "task_id": task_id,
        "attempt": attempt,
        "sequence": sequence,
        "is_final": True,
        "outcome_status": "finished",
        "progress_since_previous": "The assigned check is complete.",
        "operations": [],
        "computation_operation_ids": [],
        "fact_challenges": [],
        "completion_evidence_ids": list(evidence),
        "attempt_summary": {
            "work_mode": "associate",
            "task": "Exercise one durable progress contract.",
            "proposed_outcome": "finished",
            "cumulative_important_progress": "The evidence was recorded.",
            "completion_evidence_operation_ids": list(evidence),
            "most_promising_next_steps": "None.",
        },
    }


def _computation(staging_id: str) -> dict[str, object]:
    return {
        "operation_id": staging_id,
        "staging_id": staging_id,
        "software": {"name": "test-cas", "version": "1"},
        "exact_input": "1 + 1",
        "output": "2",
        "exit_status": 0,
        "description": "Compute a toy value.",
        "assumptions": "Integer arithmetic.",
        "environment_versions": {},
        "random_seed": None,
        "error_output": "",
        "interpretation": "The toy value is two.",
        "related_memory_ids": {},
        "fact_candidate_operation_ids": [],
    }


class PinnedTaskMemoryStore(MemoryStore):
    def __init__(self, database: Path, projection: Path, task_ids: list[str] | None = None):
        super().__init__(database, projection)
        self._pinned_task_ids = list(task_ids or [])

    def allocate_id(self, memory_type):  # type: ignore[no-untyped-def]
        value = getattr(memory_type, "value", str(memory_type))
        if value == "task" and self._pinned_task_ids:
            return self._pinned_task_ids.pop(0)
        return super().allocate_id(memory_type)


class PinnedTaskFakeStore(FakeControlStore):
    def __init__(self, task_ids: list[str]):
        super().__init__()
        self._pinned_task_ids = list(task_ids)

    def allocate_id(self, memory_type: str) -> str:
        if str(memory_type) == "task" and self._pinned_task_ids:
            return self._pinned_task_ids.pop(0)
        return super().allocate_id(memory_type)


class R4FixtureReplayTests(unittest.TestCase):
    @staticmethod
    def _fixture() -> dict:
        return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))

    def _payload(self, name: str) -> dict:
        entry = self._fixture()["payloads"][name]
        path = PROJECT_ROOT / entry["path"]
        data = path.read_bytes()
        self.assertEqual(hashlib.sha256(data).hexdigest(), entry["sha256"])
        return json.loads(data)

    def test_complete_payload_hashes_and_exact_event_order_are_pinned(self) -> None:
        fixture = self._fixture()
        for name in fixture["payloads"]:
            with self.subTest(payload=name):
                self._payload(name)

        actual = fixture["event_sequence"]
        encoded_events = json.dumps(
            actual, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
        self.assertEqual(
            hashlib.sha256(encoded_events).hexdigest(),
            fixture["event_sequence_sha256"],
        )
        self.assertEqual(
            [item["event_id"] for item in actual],
            sorted(item["event_id"] for item in actual),
        )
        self.assertEqual(
            [item["event_id"] for item in actual],
            [24, 28, 35, 41, 44, 45, 46, 47, 62, 95, 96, 102, 103, 109, 110],
        )
        self.assertEqual(
            fixture["persisted_bug_observations"][
                "OP-OBL-1b418cfd9d20468d9a9ea2e11d4cb2fe"
            ]["error"],
            "relations[0] must have at least one premise",
        )

    def test_real_root_and_empty_premise_payload_sequence_replays_without_root_capture(self) -> None:
        source_id = "T-6319fbf653d044cd81beb63c46d286d9"
        victim_id = "T-8f64d69ca31b4eba8173e691f5df2949"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = PinnedTaskMemoryStore(
                root / "memory.sqlite3", root / "projection", [source_id, victim_id]
            )
            try:
                scheduler = Scheduler(store)
                scheduler.bootstrap(
                    root_problem="K-equivalence implies D-equivalence?",
                    root_obligation_id=ROOT_ID,
                )
                store.add_obligation(
                    "r4-root-replay",
                    {
                        "id": ROOT_ID,
                        "abstract": "The root problem.",
                        "statement": "K-equivalence implies D-equivalence.",
                        "importance": "This is the project target.",
                        "predecessor_fact_ids": [],
                        "partial_progress": [],
                        "related_route_ids": [],
                        "relations": [],
                    },
                )
                scheduler.bind_canonical_root_obligation()
                scheduler.commit_initial_trim({"category_ids": []})
                assigned = scheduler.submit_batch(
                    "B-R4-ROOT-REPLAY", [_assignment(), _assignment()]
                )
                self.assertEqual(assigned, [source_id, victim_id])
                scheduler.start_task_attempt(source_id)
                scheduler.start_task_attempt(victim_id)

                scheduler.ingest_progress(self._payload("root_reservation_prior_progress"))
                scheduler.ingest_progress(self._payload("root_reservation_source"))
                scheduler.ingest_progress(self._payload("root_reservation_victim"))
                scheduler.ingest_progress(
                    self._payload("root_reservation_victim_followup")
                )

                for operation_id in (
                    "OP-OBL-EQUIVARIANT-FLOP-001",
                    "OP-OBL-REDUCED-GERBE-001",
                ):
                    operation = scheduler.state["operations"][operation_id]
                    self.assertNotIn("temporary ID ROOT", str(operation.get("error") or ""))

                empty_id = "OP-OBL-1b418cfd9d20468d9a9ea2e11d4cb2fe"
                dependent_id = "OP-OBL-ccefe11e756d41d09686918845312b8b"
                route_id = "OP-ROUTE-0f96dc2408204852adbf15f837c31edf"
                for operation_id in (empty_id, dependent_id, route_id):
                    scheduler.apply_synthesizer_result(
                        operation_id,
                        {
                            "resolution": "new",
                            "operation_digest": scheduler.state["operations"][operation_id][
                                "input_digest"
                            ],
                            "relied_on": [],
                        },
                    )
                self.assertEqual(
                    scheduler.state["operations"][empty_id]["error"],
                    "relations[0] must have at least one premise",
                )
                for operation_id in (dependent_id, route_id):
                    operation = scheduler.state["operations"][operation_id]
                    self.assertEqual(
                        operation["state"], OperationState.COMMITTED.value
                    )
                    self.assertIsNone(operation["error"])

                failed_proposal = "PROP-OBL-86003642632d4bc083c6cbb68cd8a7f9"
                marker = f"{failed_proposal}(unpublished)"
                dependent = store.get(
                    scheduler.state["proposal_mappings"][
                        "PROP-OBL-acde30243ac34a0f93f99b209b160f65"
                    ]
                )
                route = store.get(
                    scheduler.state["proposal_mappings"][
                        "PROP-ROUTE-40d55581b07c4fbf8d42a0d5f191a363"
                    ]
                )
                self.assertIn(
                    marker,
                    dependent["relations"][0]["premise_memory_ids"],
                )
                self.assertIn(marker, route["related_obligation_ids"])
            finally:
                store.close()

    def test_real_cas_payload_sequence_now_closes_the_task(self) -> None:
        task_id = "T-9485410981974bfb85a5894fac35a9c1"
        store = PinnedTaskFakeStore([task_id])
        store.records[ROOT_ID] = {
            "id": ROOT_ID,
            "type": "obligation",
            "revision": 1,
            "active": True,
            "status": "active",
        }
        scheduler = Scheduler(store)
        scheduler.bootstrap(
            root_problem="K-equivalence implies D-equivalence?",
            root_obligation_id=ROOT_ID,
        )
        scheduler.commit_initial_trim({"category_ids": []})
        report = _assignment(_portfolio(obligation=[ROOT_ID]))
        report.update(
            {"mode": "computation", "main_obligation_ids": [ROOT_ID]}
        )
        self.assertEqual(scheduler.submit_batch("B-R4-CAS", [report]), [task_id])
        scheduler.start_task_attempt(task_id)

        progress = self._payload("cas_progress")
        computation = FrantaRuntime._computation_record(self._payload("cas_computation"))
        progress.pop("skill", None)
        progress.pop("computation_operation_ids")
        progress["computations"] = [computation]
        scheduler.ingest_progress(progress, authenticated_computations=True)

        final = self._payload("cas_final")
        final.pop("skill", None)
        final.pop("computation_operation_ids")
        final["computations"] = []
        scheduler.ingest_progress(final, authenticated_computations=True)
        self.assertEqual(
            scheduler.state["computations"]["OP-05ea4b89-9825-49cd-af46-2e8b4be330e5"][
                "state"
            ],
            OperationState.COMMITTED.value,
        )
        self.assertEqual(scheduler.state["tasks"][task_id]["state"], TaskState.CLOSED.value)


class RecordProgressContractClosureTests(unittest.TestCase):
    @staticmethod
    def _route(**changes: object) -> dict[str, object]:
        result: dict[str, object] = {
            "abstract": "A route through the test obstruction.",
            "strategy_description": "Reduce the obstruction to a test lemma.",
            "value_assessment": {
                "confidence": "plausible",
                "success_gain": "settles the test target",
                "failure_gain": "isolates the obstruction",
                "relevance": "direct",
                "novelty": "new test reduction",
            },
            "progress": ["Set up the reduction."],
            "related_obligation_ids": [],
            "next_steps": ["Prove the test lemma."],
            "obstacles": ["One boundary case."],
            "active_fact_ids": [],
            "relevant_memo_ids": [],
            "relevant_claim_ids": [],
        }
        result.update(changes)
        return result

    @staticmethod
    def _obligation(**changes: object) -> dict[str, object]:
        result: dict[str, object] = {
            "abstract": "The test obstruction vanishes.",
            "statement": "The test obstruction vanishes in every test object.",
            "importance": "This completes the test reduction.",
            "predecessor_fact_ids": [],
            "partial_progress": ["The base case is known."],
            "related_route_ids": [],
            "relations": [],
        }
        result.update(changes)
        return result

    def test_every_documented_operation_kind_stages_and_reaches_real_store(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = MemoryStore(root / "memory.sqlite3", root / "projection")
            try:
                seed_task = store.allocate_id("task")
                fact = store.add_fact(
                    "seed-fact",
                    {
                        "statement": "The test base case holds.",
                        "proof": "It is one of the test definitions.",
                        "predecessor_fact_ids": [],
                        "originating_task_id": seed_task,
                        "foundation_policy_version": 1,
                        "introduced_notation": [],
                        "external_references": [],
                        "root_resolution": None,
                        "abstract": "The test base case holds.",
                        "keywords": ["test"],
                        "related_route_ids": [],
                    },
                ).canonical_id
                route = store.add_route("seed-route", self._route()).canonical_id
                claim = store.add_claim(
                    "seed-claim",
                    {
                        "abstract": "A disposable test claim.",
                        "content": "This claim exists for removal coverage.",
                        "related_route_ids": [],
                    },
                ).canonical_id
                obligation_update = store.add_obligation(
                    "seed-obligation-update", self._obligation()
                ).canonical_id
                obligation_remove = store.add_obligation(
                    "seed-obligation-remove",
                    self._obligation(statement="A disposable test obligation."),
                ).canonical_id

                scheduler = Scheduler(store)
                scheduler.bootstrap(root_problem="Prove the test target.")
                scheduler.commit_initial_trim({"category_ids": []})
                portfolio = _portfolio(
                    fact=[fact],
                    route=[route],
                    claim=[claim],
                    obligation=[obligation_update, obligation_remove],
                )
                task_id = scheduler.submit_batch("B-CONTRACT-MATRIX", [_assignment(portfolio)])[0]
                attempt = scheduler.start_task_attempt(task_id)
                workspace = root / "skill-workspace"
                workspace.mkdir()
                skill = SkillRuntime(
                    SkillContext(
                        workspace=workspace,
                        policy=policy_for("worker", mode="associate"),
                        task_id=task_id,
                        attempt=attempt,
                        task_card={"task_id": task_id, "attempt": attempt, "mode": "associate"},
                    )
                )

                operations = [
                    {
                        "operation_id": "OP-CONTRACT-FACT",
                        "kind": "fact",
                        "proposal_id": "TMP-CONTRACT-FACT",
                        "candidate_id": "FC-CONTRACT-FACT",
                        "candidate_version": 1,
                        "statement": "Every contract test has a payload.",
                        "proof": "This follows by inspection of the finite matrix.",
                        "predecessor_fact_ids": [],
                        "originating_task_id": task_id,
                        "foundation_policy_version": 1,
                        "introduced_notation": [],
                        "external_references": [],
                        "root_resolution": None,
                        "abstract": "Every contract test has a payload.",
                        "keywords": ["contract"],
                        "related_route_ids": [],
                    },
                    {"operation_id": "OP-CONTRACT-ROUTE-ADD", "kind": "route_add", "proposal_id": "TMP-CONTRACT-ROUTE", **self._route()},
                    {
                        "operation_id": "OP-CONTRACT-ROUTE-UPDATE",
                        "kind": "route_update",
                        "target_id": route,
                        "expected_base_revision": 1,
                        "set": {"abstract": "The updated test route."},
                        "append": {},
                        "add_ids": {},
                        "remove_ids": {},
                        "explanation": "Sharpen the test route.",
                        "supporting_memory_ids": [fact],
                    },
                    {
                        "operation_id": "OP-CONTRACT-MEMO",
                        "kind": "memo",
                        "proposal_id": "TMP-CONTRACT-MEMO",
                        "abstract": "A contract test memo.",
                        "genre": "normal",
                        "content": "The nine payload kinds share one durable interpretation.",
                        "related_route_ids": [],
                    },
                    {
                        "operation_id": "OP-CONTRACT-CLAIM-ADD",
                        "kind": "claim_add",
                        "proposal_id": "TMP-CONTRACT-CLAIM",
                        "abstract": "A contract test claim.",
                        "content": "The claim-add payload is accepted.",
                        "related_route_ids": [],
                    },
                    {
                        "operation_id": "OP-CONTRACT-CLAIM-REMOVE",
                        "kind": "claim_remove",
                        "target_id": claim,
                        "replacement_id": fact,
                        "reason": "Replace the provisional claim with the seed fact.",
                    },
                    {
                        "operation_id": "OP-CONTRACT-OBLIGATION-ADD",
                        "kind": "obligation_add",
                        "proposal_id": "TMP-CONTRACT-OBLIGATION",
                        **self._obligation(
                            relations=[
                                {
                                    "relation_type": "test_implication",
                                    "premise_memory_ids": [fact],
                                    "conclusion": "ROOT",
                                    "explanation": "The seed fact is the explicit premise.",
                                    "supporting_fact_ids": [fact],
                                }
                            ]
                        ),
                    },
                    {
                        "operation_id": "OP-CONTRACT-OBLIGATION-UPDATE",
                        "kind": "obligation_update",
                        "target_id": obligation_update,
                        "expected_base_revision": 1,
                        "set": {"abstract": "The updated test obligation."},
                        "append": {},
                        "add_ids": {},
                        "remove_ids": {},
                        "explanation": "Sharpen the obligation abstract.",
                        "supporting_memory_ids": [fact],
                    },
                    {
                        "operation_id": "OP-CONTRACT-OBLIGATION-REMOVE",
                        "kind": "obligation_remove",
                        "target_id": obligation_remove,
                        "resolving_fact_ids": [fact],
                        "refuting_fact_ids": [],
                        "reason": "The seed fact resolves the disposable obligation.",
                    },
                ]
                for sequence, operation in enumerate(operations, start=1):
                    receipt = skill.invoke(
                        "record-progress",
                        {
                            "operation_id": f"RP-CONTRACT-{sequence}",
                            "progress_id": f"PRG-CONTRACT-{sequence}",
                            "sequence": sequence,
                            "is_final": False,
                            "outcome_status": "progress",
                            "progress_since_previous": f"Exercise {operation['kind']}.",
                            "operations": [operation],
                            "computation_operation_ids": [],
                            "fact_challenges": [],
                        },
                    )["artifact"]
                    scheduler.ingest_progress(receipt)
                    operation_id = operation["operation_id"]
                    if operation["kind"] in {"fact", "route_add", "obligation_add"}:
                        scheduler.apply_synthesizer_result(
                            operation_id,
                            {
                                "resolution": "new",
                                "operation_digest": scheduler.state["operations"][operation_id][
                                    "input_digest"
                                ],
                                "relied_on": [],
                            },
                        )

                scheduler.ingest_progress(
                    _final(task_id, attempt, len(operations) + 1, [])
                )
                operation_id = "OP-CONTRACT-FACT"
                bundle = scheduler.verification_bundle(operation_id)
                scheduler.apply_verifier_report(
                    operation_id,
                    {
                        "verdict": "correct",
                        "candidate_id": bundle["candidate_id"],
                        "candidate_version": bundle["candidate_version"],
                        "operation_id": bundle["operation_id"],
                        "bundle_digest": bundle["bundle_digest"],
                        "verifier_attempt_id": bundle["verifier_attempt_id"],
                        "predecessor_ids": bundle["predecessor_ids"],
                        "introduced_notation": bundle["introduced_notation"],
                        "external_references": bundle["external_references"],
                        "root_resolution": bundle["root_resolution"],
                        "errors": [],
                    },
                )

                self.assertEqual(
                    {scheduler.state["operations"][item["operation_id"]]["state"] for item in operations},
                    {OperationState.COMMITTED.value},
                )
                self.assertEqual(store.get(route)["abstract"], "The updated test route.")
                self.assertEqual(store.get(claim).status, "withdrawn")
                self.assertEqual(store.get(obligation_update)["abstract"], "The updated test obligation.")
                self.assertEqual(store.get(obligation_remove).status, "removed")
                added_obligation_id = scheduler.state["operations"][
                    "OP-CONTRACT-OBLIGATION-ADD"
                ]["canonical_id"]
                relation = store.get(added_obligation_id)["relations"][0]
                self.assertEqual(relation["premise_memory_ids"], [fact])
                self.assertEqual(relation["conclusion"], "ROOT")
            finally:
                store.close()


class SchedulerModelPropertyTests(unittest.TestCase):
    def test_seeded_namespace_and_terminal_state_model(self) -> None:
        cases = [
            (owned_kind, owned_state, foreign_collision, evidence_phase)
            for owned_kind in ("operation", "computation")
            for owned_state in ("committed", "rejected")
            for foreign_collision in (False, True)
            for evidence_phase in ("same_progress", "prior_progress")
        ]
        random.Random(20260821).shuffle(cases)
        for index, (
            owned_kind,
            owned_state,
            foreign_collision,
            evidence_phase,
        ) in enumerate(cases):
            with self.subTest(
                owned_kind=owned_kind,
                owned_state=owned_state,
                foreign_collision=foreign_collision,
                evidence_phase=evidence_phase,
            ):
                store = FakeControlStore()
                scheduler = Scheduler(store, control_key=f"property-{index}")
                scheduler.bootstrap(root_problem="Prove the model invariant.")
                scheduler.commit_initial_trim({"category_ids": []})
                task_ids = scheduler.submit_batch(
                    f"B-PROPERTY-{index}", [_assignment(), _assignment()]
                )
                foreign_task, owned_task = task_ids
                foreign_attempt = scheduler.start_task_attempt(foreign_task)
                owned_attempt = scheduler.start_task_attempt(owned_task)
                evidence_id = f"EVIDENCE-{index}"

                if foreign_collision:
                    if owned_kind == "computation":
                        foreign_operation = {
                            "operation_id": evidence_id,
                            "kind": "memo" if owned_state == "rejected" else "invalid-kind",
                            "proposal_id": f"TMP-FOREIGN-{index}",
                            "abstract": "Foreign evidence.",
                            "genre": "normal",
                            "content": "Foreign namespace collision.",
                            "related_route_ids": [],
                        }
                        scheduler.ingest_progress(
                            {
                                **_final(foreign_task, foreign_attempt, 1, []),
                                "is_final": False,
                                "operations": [foreign_operation],
                            }
                        )
                    else:
                        scheduler.ingest_progress(
                            {
                                **_final(foreign_task, foreign_attempt, 1, []),
                                "is_final": False,
                                "computations": [_computation(evidence_id)],
                            },
                            authenticated_computations=True,
                        )

                if owned_kind == "operation":
                    operation = {
                        "operation_id": evidence_id,
                        "kind": "memo" if owned_state == "committed" else "invalid-kind",
                        "proposal_id": f"TMP-OWNED-{index}",
                        "abstract": "Owned evidence.",
                        "genre": "normal",
                        "content": "Owned operation evidence.",
                        "related_route_ids": [],
                    }
                    owned_fields = {"operations": [operation]}
                else:
                    if owned_state == "rejected":
                        computation = _computation(evidence_id)
                        computation["related_memory_ids"] = {"fact": ["F-NOT-AUTHORIZED"]}
                    else:
                        computation = _computation(evidence_id)
                    owned_fields = {"computations": [computation]}

                if evidence_phase == "same_progress":
                    scheduler.ingest_progress(
                        {
                            **_final(owned_task, owned_attempt, 1, [evidence_id]),
                            **owned_fields,
                        },
                        authenticated_computations=owned_kind == "computation",
                    )
                else:
                    scheduler.ingest_progress(
                        {
                            **_final(owned_task, owned_attempt, 1, []),
                            "is_final": False,
                            **owned_fields,
                        },
                        authenticated_computations=owned_kind == "computation",
                    )
                    scheduler.ingest_progress(
                        _final(owned_task, owned_attempt, 2, [evidence_id])
                    )
                expected = (
                    TaskState.CLOSED.value
                    if owned_state == "committed"
                    else TaskState.REVISION_PENDING.value
                )
                self.assertEqual(scheduler.state["tasks"][owned_task]["state"], expected)

    def test_computation_crash_points_reconcile_to_the_same_closed_state(self) -> None:
        class CrashStore(FakeControlStore):
            def __init__(self, phase: str):
                super().__init__()
                self.phase = phase
                self.crashed = False

            def apply_operation(self, operation_id, operation_type, payload, **kwargs):
                if operation_type == "computation" and not self.crashed:
                    self.crashed = True
                    if self.phase == "before_store":
                        raise SimulatedCrash("before computation store commit")
                    result = super().apply_operation(
                        operation_id, operation_type, payload, **kwargs
                    )
                    raise SimulatedCrash("after computation store commit")
                return super().apply_operation(operation_id, operation_type, payload, **kwargs)

        phases = ["before_store", "after_store"]
        random.Random(20260821).shuffle(phases)
        for phase in phases:
            with self.subTest(crash_point=phase):
                store = CrashStore(phase)
                scheduler = Scheduler(store)
                scheduler.bootstrap(root_problem="Prove crash recovery.")
                scheduler.commit_initial_trim({"category_ids": []})
                task_id = scheduler.submit_batch("B-CRASH", [_assignment()])[0]
                attempt = scheduler.start_task_attempt(task_id)
                with self.assertRaises(SimulatedCrash):
                    scheduler.ingest_progress(
                        {
                            **_final(task_id, attempt, 1, []),
                            "is_final": False,
                            "computations": [_computation("CAS-CRASH")],
                        },
                        authenticated_computations=True,
                    )
                resumed = Scheduler(store)
                resumed.ingest_progress(_final(task_id, attempt, 2, ["CAS-CRASH"]))
                self.assertEqual(
                    resumed.state["tasks"][task_id]["state"],
                    TaskState.POSTPROCESSING.value,
                )
                resumed.reconcile_pending_ingestion()
                self.assertEqual(
                    resumed.state["tasks"][task_id]["state"], TaskState.CLOSED.value
                )


class CrossBlockWorkerOutboxTests(unittest.TestCase):
    def test_worker_skill_outbox_flows_through_runtime_store_and_task_closure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = root / "bootstrap.toml"
            manifest.write_text(
                "\n".join(
                    (
                        "[project]",
                        'name = "cross-block-progress"',
                        'directory = "project"',
                        'root_problem = "Prove the test property."',
                        'foundation_policy = "Use the test definition."',
                        "",
                        "[context_budgets]",
                        "main = 1000",
                        "",
                        "[initial]",
                    )
                )
                + "\n",
                encoding="utf-8",
            )

            def executor(call: AgentCall) -> dict[str, object]:
                card = json.loads(call.workspace.task_card_path.read_text(encoding="utf-8"))
                operation_id = "OP-CROSS-BLOCK-MEMO"
                progress_id = "PRG-CROSS-BLOCK-FINAL"
                SkillRuntime(SkillContext.load(call.workspace.path)).invoke(
                    "record-progress",
                    {
                        "operation_id": "RP-CROSS-BLOCK-FINAL",
                        "progress_id": progress_id,
                        "sequence": 1,
                        "is_final": True,
                        "outcome_status": "finished",
                        "progress_since_previous": "Recorded the end-to-end result.",
                        "operations": [
                            {
                                "operation_id": operation_id,
                                "kind": "memo",
                                "proposal_id": "TMP-CROSS-BLOCK-MEMO",
                                "abstract": "End-to-end worker outbox regression.",
                                "genre": "normal",
                                "content": "The staged payload reached canonical memory.",
                                "related_route_ids": [],
                            }
                        ],
                        "computation_operation_ids": [],
                        "fact_challenges": [],
                        "completion_evidence_ids": [operation_id],
                        "attempt_summary": {
                            "work_mode": card["mode"],
                            "task": card["objective"],
                            "proposed_outcome": "finished",
                            "cumulative_important_progress": "Published one durable memo.",
                            "completion_evidence_operation_ids": [operation_id],
                            "most_promising_next_steps": "None.",
                        },
                    },
                )
                return {"attempt_ended": True, "final_progress_id": progress_id}

            runtime = FrantaRuntime.initialize(load_manifest(manifest), executor=executor)
            try:
                runtime.scheduler.commit_initial_trim({"category_ids": []})
                task_id = runtime.scheduler.submit_batch("B-CROSS-BLOCK", [_assignment()])[0]
                runtime._main_should_run = lambda: False
                runtime.run(max_cycles=1)
                task = runtime.scheduler.state["tasks"][task_id]
                operation = runtime.scheduler.state["operations"]["OP-CROSS-BLOCK-MEMO"]
                self.assertEqual(task["state"], TaskState.CLOSED.value)
                self.assertEqual(operation["state"], OperationState.COMMITTED.value)
                self.assertEqual(
                    runtime.store.get(operation["canonical_id"])["content"],
                    "The staged payload reached canonical memory.",
                )
                self.assertTrue(
                    any(
                        "outbox/record-progress/RP-CROSS-BLOCK-FINAL.json"
                        in artifact["relative_path"]
                        for artifact in task["artifact_references"]
                    )
                )
            finally:
                runtime.close()


if __name__ == "__main__":
    unittest.main()
