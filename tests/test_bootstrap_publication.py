from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any

from franta.config import load_manifest
from franta.models import MemoryType
from franta.runtime import AgentCall, BOOTSTRAP_STATE_KEY, FrantaRuntime


def _manifest(root: Path) -> Path:
    path = root / "bootstrap.toml"
    path.write_text(
        """
[project]
name = "bootstrap-publication"
directory = "project"
root_problem = "Prove the bootstrap test property."
foundation_policy = "Use only the bootstrap test axiom."

[context_budgets]
main = 1000

[initial]

[[initial.routes]]
proposal_id = "BOOT-ROUTE"
abstract = "Bootstrap route"
strategy_description = "Apply the bootstrap axiom directly."
value_assessment = { confidence = "plausible", success_gain = "settles the target", failure_gain = "isolates the obstruction", relevance = "central", novelty = "initial mechanism" }
progress = []
related_obligation_ids = []
next_steps = ["Check the axiom."]
obstacles = []
active_fact_ids = []
relevant_memo_ids = []
relevant_claim_ids = []

[[initial.memos]]
proposal_id = "BOOT-MEMO"
abstract = "Bootstrap memo"
genre = "normal"
content = "The initial route is the natural baseline."
related_route_ids = ["BOOT-ROUTE"]

[[initial.claims]]
abstract = "Bootstrap claim"
content = "The initial route may settle the target."
related_route_ids = ["BOOT-ROUTE"]
""".lstrip(),
        encoding="utf-8",
    )
    return path


def _update_manifest(root: Path) -> Path:
    path = root / "bootstrap-update.toml"
    path.write_text(
        """
[project]
name = "bootstrap-update-publication"
directory = "p"
root_problem = "Prove the bootstrap update test property."
foundation_policy = "Use only the bootstrap update test axiom."

[context_budgets]
main = 1000

[initial]

[[initial.obligations]]
abstract = "Obligation waiting for the bootstrap route"
statement = "Establish the bootstrap route's local consequence."
importance = "It checks update-reference resolution."
predecessor_fact_ids = []
partial_progress = []
related_route_ids = ["BOOT-UPDATE-ROUTE"]
relations = []

[[initial.routes]]
proposal_id = "BOOT-UPDATE-ROUTE"
abstract = "Bootstrap update route"
strategy_description = "Refine the existing bootstrap mechanism."
value_assessment = { confidence = "plausible", success_gain = "settles the target", failure_gain = "isolates the obstruction", relevance = "central", novelty = "refinement" }
progress = ["A sharper bootstrap estimate is available."]
related_obligation_ids = []
next_steps = ["Apply the sharper estimate."]
obstacles = []
active_fact_ids = []
relevant_memo_ids = []
relevant_claim_ids = []
""".lstrip(),
        encoding="utf-8",
    )
    return path


class BootstrapPublicationTests(unittest.TestCase):
    @staticmethod
    def _route_body() -> dict[str, Any]:
        return {
            "abstract": "Existing route",
            "strategy_description": "Use the existing mechanism.",
            "value_assessment": {
                "confidence": "plausible",
                "success_gain": "settles the target",
                "failure_gain": "isolates the obstruction",
                "relevance": "central",
                "novelty": "baseline mechanism",
            },
            "progress": [],
            "related_obligation_ids": [],
            "next_steps": ["Check the mechanism."],
            "obstacles": [],
            "active_fact_ids": [],
            "relevant_memo_ids": [],
            "relevant_claim_ids": [],
        }

    def test_workflow_proposal_ids_do_not_enter_canonical_payloads(self) -> None:
        original = {
            "proposal_id": "BOOT-OUTER",
            "content": {"proposal_id": "mathematical text is not rewritten"},
            "unrelated_field": "still present for normal schema validation",
        }
        self.assertEqual(
            FrantaRuntime._bootstrap_canonical_body(original),
            {
                "content": {"proposal_id": "mathematical text is not rewritten"},
                "unrelated_field": "still present for normal schema validation",
            },
        )
        self.assertEqual(original["proposal_id"], "BOOT-OUTER")

        with tempfile.TemporaryDirectory() as raw:
            synthesizer_proposals: list[dict[str, Any]] = []

            def executor(call: AgentCall) -> dict[str, Any]:
                self.assertEqual(call.kind, "synthesizer")
                synthesizer_proposals.append(dict(call.payload["proposal"]))
                return {
                    "resolution": "new",
                    "operation_digest": call.payload["operation_digest"],
                    "relied_on": [],
                }

            runtime = FrantaRuntime.initialize(
                load_manifest(_manifest(Path(raw))), executor=executor
            )
            try:
                runtime.start_services()
                self.assertTrue(runtime._advance_bootstrap_proposals())
                revision, state = runtime.store.load_control_state(
                    BOOTSTRAP_STATE_KEY
                )
                self.assertGreaterEqual(revision, 1)
                self.assertTrue(state["stable"])
                self.assertEqual(
                    [item["status"] for item in state["proposals"]],
                    ["committed", "committed", "committed"],
                )
                self.assertEqual(
                    synthesizer_proposals[0]["proposal_id"], "BOOT-ROUTE"
                )

                route_id = state["mappings"]["BOOT-ROUTE"]
                memo_id = state["mappings"]["BOOT-MEMO"]
                claim_id = state["mappings"]["BOOT-CLAIMS-1"]
                self.assertEqual(runtime.store.get(route_id).memory_type, MemoryType.ROUTE)
                self.assertEqual(runtime.store.get(memo_id)["related_route_ids"], [route_id])
                self.assertEqual(runtime.store.get(claim_id)["related_route_ids"], [route_id])
                for proposal_id, canonical_id in state["mappings"].items():
                    self.assertNotIn(
                        "proposal_id", runtime.store.get(canonical_id).to_dict()
                    )
                    mapping = runtime.store.proposal_mapping(proposal_id)
                    self.assertIsNotNone(mapping)
                    self.assertEqual(mapping["canonical_id"], canonical_id)

                project_dir = runtime.layout.root
                canonical_ids = dict(state["mappings"])
                self.assertFalse(runtime._advance_bootstrap_proposals())
            finally:
                runtime.close()

            resumed = FrantaRuntime.open(
                project_dir,
                executor=lambda call: (_ for _ in ()).throw(
                    AssertionError(f"unexpected replayed call {call.kind}")
                ),
            )
            try:
                resumed.recover()
                self.assertFalse(resumed._advance_bootstrap_proposals())
                _revision, state = resumed.store.load_control_state(
                    BOOTSTRAP_STATE_KEY
                )
                self.assertEqual(state["mappings"], canonical_ids)
                self.assertEqual(
                    len(resumed.store.list_records(types=[MemoryType.ROUTE])), 1
                )
                self.assertEqual(
                    len(resumed.store.list_records(types=[MemoryType.MEMO])), 1
                )
                self.assertEqual(
                    len(resumed.store.list_records(types=[MemoryType.CLAIM])), 1
                )
            finally:
                resumed.close()

    def test_duplicate_and_update_resolution_paths_are_unchanged(self) -> None:
        for resolution in ("duplicate", "update"):
            with self.subTest(resolution=resolution), tempfile.TemporaryDirectory() as raw:
                existing_id: str | None = None

                def executor(call: AgentCall) -> dict[str, Any]:
                    assert existing_id is not None
                    result: dict[str, Any] = {
                        "resolution": resolution,
                        "operation_digest": call.payload["operation_digest"],
                        "canonical_id": existing_id,
                        "relied_on": [],
                    }
                    if resolution == "update":
                        result["patch"] = {
                            "target_id": existing_id,
                            "expected_base_revision": 1,
                            "append": {
                                "progress": ["The bootstrap candidate sharpened this route."]
                            },
                            "add_ids": {},
                            "remove_ids": {},
                            "explanation": "Record the new bootstrap progress.",
                            "supporting_memory_ids": [],
                        }
                    return result

                runtime = FrantaRuntime.initialize(
                    load_manifest(_manifest(Path(raw))), executor=executor
                )
                try:
                    seeded = runtime.store.add_route(
                        f"seed-{resolution}", self._route_body(), actor="test"
                    )
                    existing_id = seeded.canonical_id
                    runtime.start_services()
                    self.assertTrue(runtime._advance_bootstrap_proposals())
                    _revision, state = runtime.store.load_control_state(
                        BOOTSTRAP_STATE_KEY
                    )
                    self.assertEqual(state["mappings"]["BOOT-ROUTE"], existing_id)
                    self.assertEqual(
                        len(runtime.store.list_records(types=[MemoryType.ROUTE])), 1
                    )
                    expected_revision = 1 if resolution == "duplicate" else 2
                    self.assertEqual(
                        runtime.store.get(existing_id).revision, expected_revision
                    )
                    self.assertFalse(runtime._advance_bootstrap_proposals())
                    self.assertEqual(
                        runtime.store.get(existing_id).revision, expected_revision
                    )
                finally:
                    runtime.close()

    def test_bootstrap_update_resolves_waiting_initial_obligation(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            existing_id: str | None = None

            def executor(call: AgentCall) -> dict[str, Any]:
                assert existing_id is not None
                return {
                    "resolution": "update",
                    "operation_digest": call.payload["operation_digest"],
                    "canonical_id": existing_id,
                    "relied_on": [],
                    "patch": {
                        "target_id": existing_id,
                        "expected_base_revision": 1,
                        "set": {},
                        "append": {
                            "progress": ["A sharper bootstrap estimate is available."]
                        },
                        "add_ids": {},
                        "remove_ids": {},
                        "explanation": "Absorb the initial proposal's progress.",
                        "supporting_memory_ids": [],
                    },
                }

            runtime = FrantaRuntime.initialize(
                load_manifest(_update_manifest(Path(raw))), executor=executor
            )
            try:
                existing = runtime.store.add_route(
                    "existing-bootstrap-update-route",
                    self._route_body(),
                    actor="test",
                )
                existing_id = existing.canonical_id
                assert existing_id
                runtime.start_services()
                self.assertTrue(runtime._advance_bootstrap_proposals())
                _revision, state = runtime.store.load_control_state(
                    BOOTSTRAP_STATE_KEY
                )
                self.assertTrue(state["stable"])
                self.assertEqual(state["mappings"]["BOOT-UPDATE-ROUTE"], existing_id)
                proposal = state["proposals"][0]
                self.assertTrue(proposal["update_reference_resolution_applied"])
                mapping = runtime.store.proposal_mapping("BOOT-UPDATE-ROUTE")
                self.assertEqual(mapping["canonical_id"], existing_id)
                self.assertEqual(mapping["resolution"], "updated")
                obligation = next(
                    record
                    for record in runtime.store.list_records(
                        types=[MemoryType.OBLIGATION]
                    )
                    if record["abstract"]
                    == "Obligation waiting for the bootstrap route"
                )
                self.assertEqual(obligation["related_route_ids"], [existing_id])
                self.assertEqual(obligation.revision, 2)
                self.assertEqual(runtime.store.get(existing_id).revision, 2)
                project_dir = runtime.layout.root
            finally:
                runtime.close()

            resumed = FrantaRuntime.open(
                project_dir,
                executor=lambda call: (_ for _ in ()).throw(
                    AssertionError(f"unexpected replayed call {call.kind}")
                ),
            )
            try:
                resumed.recover()
                self.assertFalse(resumed._advance_bootstrap_proposals())
                self.assertEqual(resumed.store.get(existing_id).revision, 2)
            finally:
                resumed.close()

    def test_recovery_repairs_stable_legacy_bootstrap_update_without_repatching(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            runtime = FrantaRuntime.initialize(
                load_manifest(_update_manifest(Path(raw))),
                executor=lambda call: (_ for _ in ()).throw(
                    AssertionError(f"unexpected model call {call.kind}")
                ),
            )
            try:
                existing = runtime.store.add_route(
                    "existing-legacy-bootstrap-update-route",
                    self._route_body(),
                    actor="test",
                )
                existing_id = existing.canonical_id
                assert existing_id
                revision, state = runtime.store.load_control_state(
                    BOOTSTRAP_STATE_KEY
                )
                proposal = state["proposals"][0]
                operation_id = proposal["operation_id"]
                runtime.store.update_route(
                    operation_id,
                    {
                        "target_id": existing_id,
                        "expected_base_revision": 1,
                        "set": {},
                        "append": {
                            "progress": ["Legacy bootstrap progress already committed."]
                        },
                        "add_ids": {},
                        "remove_ids": {},
                        "explanation": "Simulate the pre-fix bootstrap update.",
                        "supporting_memory_ids": [],
                    },
                    actor="legacy-bootstrap",
                )
                proposal["status"] = "committed"
                proposal["canonical_id"] = existing_id
                state["mappings"]["BOOT-UPDATE-ROUTE"] = existing_id
                state["stable"] = True
                runtime.store.compare_and_swap_control_state(
                    BOOTSTRAP_STATE_KEY, revision, state
                )
                project_dir = runtime.layout.root
            finally:
                runtime.close()

            resumed = FrantaRuntime.open(
                project_dir,
                executor=lambda call: (_ for _ in ()).throw(
                    AssertionError(f"recovery relaunched {call.kind}")
                ),
            )
            try:
                resumed.recover()
                _revision, state = resumed.store.load_control_state(
                    BOOTSTRAP_STATE_KEY
                )
                proposal = state["proposals"][0]
                self.assertTrue(proposal["update_reference_resolution_applied"])
                mapping = resumed.store.proposal_mapping("BOOT-UPDATE-ROUTE")
                self.assertEqual(mapping["canonical_id"], existing_id)
                self.assertEqual(mapping["resolution"], "updated")
                obligation = next(
                    record
                    for record in resumed.store.list_records(
                        types=[MemoryType.OBLIGATION]
                    )
                    if record["abstract"]
                    == "Obligation waiting for the bootstrap route"
                )
                self.assertEqual(obligation["related_route_ids"], [existing_id])
                self.assertEqual(obligation.revision, 2)
                self.assertEqual(resumed.store.get(existing_id).revision, 2)
            finally:
                resumed.close()

            second = FrantaRuntime.open(
                project_dir,
                executor=lambda call: (_ for _ in ()).throw(
                    AssertionError(f"second recovery relaunched {call.kind}")
                ),
            )
            try:
                second.recover()
                self.assertEqual(second.store.get(existing_id).revision, 2)
                obligation = next(
                    record
                    for record in second.store.list_records(
                        types=[MemoryType.OBLIGATION]
                    )
                    if record["abstract"]
                    == "Obligation waiting for the bootstrap route"
                )
                self.assertEqual(obligation.revision, 2)
            finally:
                second.close()


if __name__ == "__main__":
    unittest.main()
