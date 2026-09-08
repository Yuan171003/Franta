from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

from explorer_system.agents import (
    EXPLORER_ACCESS_MODES,
    explorer_worker_prompt,
)
from explorer_system.tools import (
    ExplorerToolValidationError,
    explorer_tool_definitions,
    explorer_worker_skills,
    normalize_check_result_payload,
    normalize_portfolio_fetch_payload,
    normalize_portfolio_search_payload,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SKILL_ROOT = PROJECT_ROOT / "src" / "explorer_system" / "skills"


def _json_examples(skill: str) -> list[dict[str, object]]:
    values: list[dict[str, object]] = []
    for path in (SKILL_ROOT / skill / "references").glob("*.md"):
        for raw in re.findall(
            r"```json\s*\n(.*?)\n```",
            path.read_text(encoding="utf-8"),
            flags=re.DOTALL,
        ):
            value = json.loads(raw)
            if isinstance(value, dict):
                values.append(value)
    return values


class ExplorerStagedAccessToolTests(unittest.TestCase):
    def test_worker_skill_sets_are_mechanically_disjoint(self) -> None:
        self.assertEqual(
            EXPLORER_ACCESS_MODES,
            {"check-result", "portfolio", "full-memory"},
        )
        staging = {"record-scratch", "record-summary"}
        self.assertEqual(
            explorer_worker_skills(access_mode="check-result"),
            staging | {"check-result"},
        )
        self.assertEqual(
            explorer_worker_skills(access_mode="portfolio"),
            staging | {"check-result", "portfolio-search"},
        )
        self.assertEqual(
            explorer_worker_skills(access_mode="full-memory"),
            staging | {"explorer-search"},
        )
        with self.assertRaises(ExplorerToolValidationError):
            explorer_worker_skills(access_mode="portfolio", search_enabled=True)

    def test_attempt_prompts_match_the_three_capability_tiers(self) -> None:
        attempt_1 = explorer_worker_prompt(
            "ROOT",
            mode="check-result",
            guidance_variant="check-result-promising",
        )
        self.assertIn("Initially you receive only ROOT", attempt_1)
        self.assertIn("sole memory-reading skill is `check-result`", attempt_1)
        self.assertIn("one strict, single mathematical proposition", attempt_1)
        self.assertIn("whichever research direction appears most promising", attempt_1)
        self.assertNotIn("`portfolio-search`", attempt_1)
        self.assertNotIn("`explorer-search`", attempt_1)

        attempt_2 = explorer_worker_prompt(
            "ROOT",
            mode="portfolio",
            guidance_variant="portfolio-synthesize",
        )
        self.assertIn("`portfolio-search` and `check-result`", attempt_2)
        self.assertIn("up to three routes, six high-level memos", attempt_2)
        self.assertIn("actively combine your ideas and findings", attempt_2)
        self.assertNotIn("`explorer-search`", attempt_2)

        attempt_3 = explorer_worker_prompt(
            "ROOT",
            mode="full-memory",
            guidance_variant="full-memory-adaptive",
        )
        self.assertIn("Use `explorer-search` freely", attempt_3)
        self.assertIn("genuinely different from existing ones", attempt_3)
        self.assertIn("combine genuinely different directions", attempt_3)
        self.assertNotIn("`check-result`", attempt_3)
        self.assertNotIn("`portfolio-search`", attempt_3)

    def test_portfolio_guidance_variants_preserve_all_four_arms(self) -> None:
        expected = {
            "portfolio-synthesize": "actively combine your ideas and findings",
            "portfolio-select-best": "choose the most promising direction",
            "portfolio-diversify": "genuinely different from existing ones",
        }
        for variant, marker in expected.items():
            with self.subTest(variant=variant):
                prompt = explorer_worker_prompt(
                    "ROOT", mode="portfolio", guidance_variant=variant
                )
                self.assertIn(marker, prompt)
                self.assertNotIn("{think_guidance}", prompt)

        unconstrained = explorer_worker_prompt(
            "ROOT",
            mode="portfolio",
            guidance_variant="portfolio-unconstrained",
        )
        for marker in expected.values():
            self.assertNotIn(marker, unconstrained)
        self.assertNotIn("()", unconstrained)

    def test_check_result_payload_is_closed_and_proposition_scoped(self) -> None:
        self.assertEqual(
            normalize_check_result_payload(
                {
                    "kind": "proved",
                    "statement": "Every finite field has prime-power cardinality.",
                }
            ),
            {
                "kind": "proved",
                "statement": "Every finite field has prime-power cardinality.",
            },
        )
        for payload in (
            {"statement": "P."},
            {"kind": "unknown", "statement": "P."},
            {"kind": "proved", "statement": "Is P true?"},
            {
                "kind": "proved",
                "statement": " Every finite field has prime-power cardinality. ",
            },
            {"kind": "proved", "statement": "P.", "limit": 2},
        ):
            with self.subTest(payload=payload):
                with self.assertRaises(ExplorerToolValidationError):
                    normalize_check_result_payload(payload)

    def test_portfolio_payloads_are_closed_and_bounded(self) -> None:
        self.assertEqual(
            normalize_portfolio_search_payload({"query": " boundary term "}),
            {"query": "boundary term", "limit": 10},
        )
        self.assertEqual(
            normalize_portfolio_fetch_payload(
                {"portfolio_item_id": "EPI-OPAQUE-1"}
            ),
            {"portfolio_item_id": "EPI-OPAQUE-1"},
        )
        with self.assertRaises(ExplorerToolValidationError):
            normalize_portfolio_search_payload({"query": "x", "limit": 11})
        with self.assertRaises(ExplorerToolValidationError):
            normalize_portfolio_fetch_payload({"portfolio_item_id": "../F-1"})

    def test_mcp_inputs_are_closed_and_have_no_paging_escape(self) -> None:
        definitions = explorer_tool_definitions({"fact", "route", "memo"})
        check = definitions["check_result"]["inputSchema"]
        self.assertEqual(check["required"], ["kind", "statement"])
        self.assertEqual(set(check["properties"]), {"kind", "statement"})
        self.assertFalse(check["additionalProperties"])
        self.assertNotIn("limit", check["properties"])
        self.assertNotIn("record_id", check["properties"])

        search = definitions["portfolio_search"]["inputSchema"]
        self.assertEqual(search["required"], ["query"])
        self.assertEqual(set(search["properties"]), {"query", "limit"})
        self.assertFalse(search["additionalProperties"])
        fetch = definitions["portfolio_fetch"]["inputSchema"]
        self.assertEqual(fetch["required"], ["portfolio_item_id"])
        self.assertFalse(fetch["additionalProperties"])

    def test_documented_results_are_uniform_and_deidentified(self) -> None:
        check_examples = _json_examples("check-result")
        response = next(value for value in check_examples if "results" in value)
        results = response["results"]
        self.assertIsInstance(results, list)
        self.assertLessEqual(len(results), 3)
        for result in results:
            self.assertEqual(
                set(result),
                {"result_id", "status", "abstract", "main_content", "relevance"},
            )
            self.assertEqual(result["status"], "established")
            self.assertFalse(
                {"type", "record_type", "memory_type", "fact", "claim"}
                & set(result)
            )

        portfolio_examples = _json_examples("portfolio-search")
        response = next(value for value in portfolio_examples if "results" in value)
        for item in response["results"]:
            self.assertEqual(
                set(item),
                {"portfolio_item_id", "abstract", "main_content", "relevance"},
            )
            self.assertFalse(
                {
                    "record_type",
                    "canonical_id",
                    "relations",
                    "dependencies",
                    "selection_reason",
                }
                & set(item)
            )


if __name__ == "__main__":
    unittest.main()
