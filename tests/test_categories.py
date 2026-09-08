from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from franta.categories import CategoryStore, render_category, render_portfolio  # noqa: E402
from franta.models import IdempotencyConflict, MemoryType  # noqa: E402
from franta.store import MemoryStore  # noqa: E402


class CategoryStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.memory = MemoryStore(root / "memory.sqlite3", False)
        self.categories = CategoryStore(self.memory, root / "category-artifacts")
        self.task_id = self.memory.allocate_id("task")

        fact_result = self.memory.add_fact(
            "fact-op",
            {
                "statement": "The base object is smooth.",
                "proof": "This follows directly from the defining Jacobian criterion.",
                "predecessor_fact_ids": [],
                "originating_task_id": self.task_id,
                "foundation_policy_version": 1,
                "introduced_notation": [],
                "external_references": [],
                "root_resolution": None,
                "abstract": "Smoothness of the base object by the Jacobian criterion",
                "keywords": ["smoothness", "Jacobian"],
                "related_route_ids": [],
            },
        )
        self.fact_id = fact_result.canonical_id
        route_result = self.memory.add_route(
            "route-op",
            {
                "abstract": "Degenerate the base object to a normal-crossings model",
                "strategy_description": "Use semistable degeneration and compare monodromy.",
                "value_assessment": {
                    "confidence": "plausible",
                    "success_gain": "central",
                    "failure_gain": "isolates an obstruction",
                    "relevance": "direct",
                    "novelty": "new degeneration",
                },
                "progress": [],
                "related_obligation_ids": [],
                "next_steps": ["Construct the family."],
                "obstacles": ["Control specialization."],
                "active_fact_ids": [self.fact_id],
                "relevant_memo_ids": [],
                "relevant_claim_ids": [],
            },
        )
        self.route_id = route_result.canonical_id
        memo_result = self.memory.add_memo(
            "memo-op",
            {
                "abstract": "Weight filtration may measure the specialization defect",
                "genre": "high-level",
                "content": "Compare the limiting filtration to the generic fiber.",
                "related_route_ids": [self.route_id],
            },
        )
        self.memo_id = memo_result.canonical_id
        claim_result = self.memory.add_claim(
            "claim-op",
            {
                "abstract": "The two-dimensional toy defect vanishes",
                "content": "A direct unverified calculation gives zero.",
                "related_route_ids": [self.route_id],
            },
        )
        self.claim_id = claim_result.canonical_id
        obligation_result = self.memory.add_obligation(
            "obligation-op",
            {
                "abstract": "Vanishing of the specialization defect",
                "statement": "The specialization defect vanishes for every admissible family.",
                "importance": "This removes the main obstacle.",
                "predecessor_fact_ids": [self.fact_id],
                "partial_progress": [],
                "related_route_ids": [self.route_id],
                "relations": [],
            },
        )
        self.obligation_id = obligation_result.canonical_id

    def tearDown(self) -> None:
        self.memory.close()
        self.temporary.cleanup()

    def definition(self, proposal_id: str, name: str = "Degeneration") -> dict[str, object]:
        return {
            "proposal_id": proposal_id,
            "name": name,
            "description": "Semistable degeneration and limiting structures.",
            "main_progress": "A smooth base case is verified.",
            "current_obstacles": "Specialization remains uncontrolled.",
            "members": {
                "fact": [self.fact_id],
                "route": [self.route_id],
                "memo": [self.memo_id],
                "claim": [self.claim_id],
                "obligation": [self.obligation_id],
            },
        }

    def create(self, operation_id: str, proposal_id: str, name: str = "Degeneration") -> str:
        result = self.categories.create_category(
            operation_id, self.definition(proposal_id, name)
        )
        self.assertEqual(result.status, "committed")
        return result.proposal_mappings[proposal_id]

    def test_create_idempotency_typed_members_projection_and_recovery(self) -> None:
        category_id = self.create("category-create", "CAT-PROP-1")
        self.assertTrue(category_id.startswith("CAT-"))
        category = self.categories.get_category(category_id)
        self.assertEqual(category["revision"], 1)
        self.assertEqual(category["members"]["fact"], [self.fact_id])
        self.assertEqual(category["member_entries"]["fact"][0]["status"], "active")
        projection = (
            Path(self.temporary.name)
            / "category-artifacts"
            / "categories"
            / f"{category_id}.md"
        )
        self.assertEqual(projection.read_text(encoding="utf-8"), render_category(category))

        replay = self.categories.create_category(
            "category-create", self.definition("CAT-PROP-1")
        )
        self.assertTrue(replay.replayed)
        self.assertEqual(replay.category_ids, (category_id,))
        changed = self.definition("CAT-PROP-1", "Different")
        with self.assertRaises(IdempotencyConflict):
            self.categories.create_category("category-create", changed)

        recovered = CategoryStore(
            self.memory, Path(self.temporary.name) / "category-artifacts"
        )
        self.assertEqual(recovered.get_category(category_id)["name"], "Degeneration")

        invalid = self.categories.create_category(
            "invalid-member",
            {
                **self.definition("CAT-PROP-BAD"),
                "members": {"task": [self.task_id]},
            },
        )
        self.assertEqual(invalid.status, "rejected")
        self.assertNotIn(
            "CAT-PROP-BAD",
            self.categories._load()[1]["proposal_mappings"],
        )

    def test_optimistic_rename_revise_and_membership(self) -> None:
        category_id = self.create("create-update", "CAT-UP")
        renamed = self.categories.rename_category(
            "rename", category_id, 1, "Degeneration and monodromy"
        )
        self.assertEqual(renamed.status, "committed")
        self.assertEqual(self.categories.get_category(category_id)["revision"], 2)
        stale = self.categories.rename_category(
            "stale-rename", category_id, 1, "Stale name"
        )
        self.assertEqual(stale.status, "rejected")
        self.assertIn("revision conflict", stale.error)

        revised = self.categories.revise_category(
            "revise",
            category_id,
            2,
            set_fields={"main_progress": "The limiting family is now constructed."},
            remove_members={"memo": [self.memo_id]},
        )
        self.assertEqual(revised.status, "committed")
        category = self.categories.get_category(category_id)
        self.assertEqual(category["revision"], 3)
        self.assertEqual(category["members"]["memo"], [])
        self.assertEqual(len(self.categories.category_history(category_id)), 2)

        membership = self.categories.change_membership(
            "membership",
            category_id,
            3,
            add_members={"memo": [self.memo_id]},
            remove_members={"claim": [self.claim_id]},
        )
        self.assertEqual(membership.status, "committed")
        category = self.categories.get_category(category_id)
        self.assertEqual(category["members"]["memo"], [self.memo_id])
        self.assertEqual(category["members"]["claim"], [])

    def test_atomic_create_and_exact_portfolio_snapshot(self) -> None:
        second_definition = {
            "proposal_id": "CAT-TWO",
            "name": "Boundary calculation",
            "description": "A separate combinatorial boundary direction.",
            "main_progress": "A toy claim is available.",
            "current_obstacles": "No general formula.",
            "members": {
                "fact": [],
                "route": [],
                "memo": [],
                "claim": [self.claim_id],
                "obligation": [self.obligation_id],
            },
        }
        proposal = {
            "category_changes": [
                {"kind": "create", **self.definition("CAT-ONE")},
                {"kind": "create", **second_definition},
            ],
            "portfolio": {
                "expected_portfolio_revision": 0,
                "categories": {
                    "CAT-ONE": 1,
                    "CAT-TWO": 1,
                },
                "flattened_members": {
                    "fact": [self.fact_id],
                    "route": [self.route_id],
                    "memo": [self.memo_id],
                    "claim": [self.claim_id],
                    "obligation": [self.obligation_id],
                },
                "base_event_id": 10,
                "confirmed_through_event_id": 12,
                "selection_rationale": "Keep two mathematically distinct directions visible.",
                "human_guidance_reference": None,
            },
        }
        result = self.categories.apply_trim_proposal("trim-atomic", proposal)
        self.assertEqual(result.status, "committed")
        self.assertEqual(result.portfolio_revision, 1)
        snapshot = self.categories.active_portfolio()
        self.assertEqual(snapshot["revision"], 1)
        self.assertEqual(
            {entry["category_id"] for entry in snapshot["categories"]},
            set(result.proposal_mappings.values()),
        )
        self.assertEqual(snapshot["flattened_members"]["fact"], [self.fact_id])
        portfolio_path = next(
            (
                Path(self.temporary.name) / "category-artifacts" / "portfolios"
            ).glob("portfolio-000001-*.md")
        )
        self.assertEqual(
            portfolio_path.read_text(encoding="utf-8"), render_portfolio(snapshot)
        )

        wrong = self.categories.commit_portfolio(
            "portfolio-wrong",
            {
                "expected_portfolio_revision": 1,
                "categories": {
                    result.proposal_mappings["CAT-ONE"]: 1,
                },
                "flattened_members": {
                    "fact": [],
                    "route": [],
                    "memo": [],
                    "claim": [],
                    "obligation": [],
                },
                "base_event_id": 12,
                "confirmed_through_event_id": 12,
                "selection_rationale": "Intentionally malformed flattening.",
            },
        )
        self.assertEqual(wrong.status, "rejected")
        self.assertEqual(self.categories.active_portfolio()["revision"], 1)

    def test_merge_and_split_are_explicit_without_balance_logic(self) -> None:
        first = self.create("create-first", "CAT-FIRST", "First")
        second = self.create("create-second", "CAT-SECOND", "Second")
        third = self.create("create-third", "CAT-THIRD", "Third")
        merged_definition = self.definition("CAT-MERGED", "Merged direction")
        merged_definition["members"] = {
            "fact": [self.fact_id],
            "route": [],
            "memo": [],
            "claim": [],
            "obligation": [self.obligation_id],
        }
        merged = self.categories.merge_categories(
            "merge",
            [first, second],
            {first: 1, second: 1},
            merged_definition,
        )
        self.assertEqual(merged.status, "committed")
        merged_id = merged.proposal_mappings["CAT-MERGED"]
        self.assertEqual(self.categories.get_category(first)["status"], "merged")
        self.assertEqual(self.categories.get_category(second)["superseded_by"], [merged_id])
        self.assertEqual(self.categories.get_category(merged_id)["status"], "active")

        split = self.categories.split_category(
            "split",
            third,
            1,
            [
                {
                    "proposal_id": "CAT-SPLIT-A",
                    "name": "Empty exploratory branch",
                    "description": "An explicitly empty category is permitted.",
                    "main_progress": "",
                    "current_obstacles": "No material yet.",
                    "members": {},
                },
                {
                    "proposal_id": "CAT-SPLIT-B",
                    "name": "Fact-only branch",
                    "description": "A focused base-case category.",
                    "main_progress": "The base case is verified.",
                    "current_obstacles": "Generalization.",
                    "members": {"fact": [self.fact_id]},
                },
            ],
        )
        self.assertEqual(split.status, "committed")
        self.assertEqual(self.categories.get_category(third)["status"], "split")
        self.assertEqual(len(split.proposal_mappings), 2)
        empty_id = split.proposal_mappings["CAT-SPLIT-A"]
        self.assertEqual(
            sum(len(items) for items in self.categories.get_category(empty_id)["members"].values()),
            0,
        )

    def test_merge_accepts_closed_wire_revision_entries(self) -> None:
        first = self.create("create-wire-first", "CAT-WIRE-FIRST", "First wire category")
        second = self.create("create-wire-second", "CAT-WIRE-SECOND", "Second wire category")
        proposal = {
            "category_changes": [
                {
                    "kind": "merge",
                    "source_category_ids": [first, second],
                    "expected_revisions": [
                        {"category_id": first, "category_revision": 1},
                        {"category_id": second, "category_revision": 1},
                    ],
                    "result": self.definition("CAT-WIRE-MERGED", "Merged wire category"),
                }
            ]
        }
        merged = self.categories.apply_trim_proposal("merge-wire-list", proposal)
        self.assertEqual(merged.status, "committed")
        self.assertIn("CAT-WIRE-MERGED", merged.proposal_mappings)

        duplicate = self.categories.apply_trim_proposal(
            "merge-wire-duplicate",
            {
                "category_changes": [
                    {
                        "kind": "merge",
                        "source_category_ids": [
                            merged.proposal_mappings["CAT-WIRE-MERGED"],
                            merged.proposal_mappings["CAT-WIRE-MERGED"],
                        ],
                        "expected_revisions": [
                            {
                                "category_id": merged.proposal_mappings[
                                    "CAT-WIRE-MERGED"
                                ],
                                "category_revision": 1,
                            },
                            {
                                "category_id": merged.proposal_mappings[
                                    "CAT-WIRE-MERGED"
                                ],
                                "category_revision": 1,
                            },
                        ],
                        "result": self.definition(
                            "CAT-WIRE-INVALID", "Invalid duplicate merge"
                        ),
                    }
                ]
            },
        )
        self.assertEqual(duplicate.status, "rejected")

    def test_revoked_and_removed_members_remain_with_visible_overlays(self) -> None:
        category_id = self.create("create-overlay", "CAT-OVERLAY")
        self.memory.revoke_fact(
            self.fact_id,
            reason="The proof omitted a singular case.",
            actor="verifier",
            operation_id="revoke-category-fact",
        )
        removed = self.memory.remove_obligation(
            "remove-category-obligation",
            {
                "target_id": self.obligation_id,
                "reason": "Reformulated after support was revoked.",
            },
        )
        self.assertEqual(removed.status, "committed")
        category = self.categories.get_category(category_id)
        self.assertEqual(category["members"]["fact"], [self.fact_id])
        self.assertEqual(category["members"]["obligation"], [self.obligation_id])
        self.assertEqual(category["member_entries"]["fact"][0]["status"], "revoked")
        self.assertEqual(category["member_entries"]["obligation"][0]["status"], "removed")
        self.categories.rebuild_projections()
        path = (
            Path(self.temporary.name)
            / "category-artifacts"
            / "categories"
            / f"{category_id}.md"
        )
        markdown = path.read_text(encoding="utf-8")
        self.assertIn("**revoked**", markdown)
        self.assertIn("**removed**", markdown)


if __name__ == "__main__":
    unittest.main()
