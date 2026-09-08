from __future__ import annotations

import json
import re
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
ROOT = PROJECT_ROOT / "src" / "explorer_system" / "skills"
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from franta.contracts.agent_access import policy_for  # noqa: E402
from franta.execution_gateway.skills import (  # noqa: E402
    SkillContext,
    SkillRuntime,
    _tool_definitions,
)


SKILLS = (
    "record-scratch",
    "record-summary",
    "explorer-search",
    "check-result",
    "portfolio-search",
)


def _frontmatter(text: str) -> dict[str, str]:
    lines = text.splitlines()
    if not lines or lines[0] != "---":
        raise AssertionError("skill is missing YAML frontmatter")
    try:
        end = lines.index("---", 1)
    except ValueError as exc:
        raise AssertionError("skill has unterminated YAML frontmatter") from exc
    values: dict[str, str] = {}
    for line in lines[1:end]:
        key, separator, value = line.partition(":")
        if separator:
            values[key.strip()] = value.strip()
    return values


def _json_examples(skill: str) -> list[dict[str, object]]:
    examples: list[dict[str, object]] = []
    for path in (ROOT / skill / "references").glob("*.md"):
        for raw in re.findall(
            r"```json\s*\n(.*?)\n```",
            path.read_text(encoding="utf-8"),
            flags=re.DOTALL,
        ):
            value = json.loads(raw)
            if not isinstance(value, dict):
                raise AssertionError(f"{path} contains a non-object JSON example")
            examples.append(value)
    return examples


class ExplorerSkillDocumentationTests(unittest.TestCase):
    def test_skill_entrypoints_are_named_and_all_local_references_resolve(self) -> None:
        for name in SKILLS:
            with self.subTest(skill=name):
                entrypoint = ROOT / name / "SKILL.md"
                text = entrypoint.read_text(encoding="utf-8")
                metadata = _frontmatter(text)
                self.assertEqual(metadata.get("name"), name)
                self.assertTrue(metadata.get("description"))
                references = re.findall(r"\]\((references/[^)]+)\)", text)
                self.assertTrue(references)
                for relative in references:
                    target = (entrypoint.parent / relative).resolve()
                    self.assertTrue(target.is_relative_to(entrypoint.parent.resolve()))
                    self.assertTrue(target.is_file(), relative)

    def test_contract_examples_are_machine_readable_json(self) -> None:
        for name in SKILLS:
            with self.subTest(skill=name):
                self.assertTrue(_json_examples(name))

    def test_staging_examples_match_the_live_skill_contracts(self) -> None:
        scratch_examples = _json_examples("record-scratch")
        summary_example = _json_examples("record-summary")[0]
        with tempfile.TemporaryDirectory() as raw:
            runtime = SkillRuntime(
                SkillContext(
                    Path(raw),
                    policy_for("explorer-worker", mode="check-result"),
                )
            )
            scratch = runtime.invoke("record-scratch", scratch_examples[0])
            summary = runtime.invoke("record-summary", summary_example)

        self.assertRegex(str(scratch["record_id"]), r"^ES-")
        self.assertEqual(scratch["artifact"]["record_kind"], "idea")
        self.assertNotIn("ongoing_direction", scratch["artifact"])
        self.assertNotIn("previous_direction_id", scratch["artifact"])
        self.assertRegex(str(summary["record_id"]), r"^ESUM-")
        self.assertEqual(
            summary["artifact"]["source_scratch_ids"],
            summary_example["source_scratch_ids"],
        )

    def test_search_example_matches_live_mcp_schema(self) -> None:
        definitions = {
            item["name"]: item
            for item in _tool_definitions({"explorer_search", "explorer_fetch"})
        }
        search = _json_examples("explorer-search")[0]
        search_schema = definitions["explorer_search"]["inputSchema"]
        self.assertLessEqual(set(search_schema["required"]), set(search))
        self.assertLessEqual(set(search), set(search_schema["properties"]))
        allowed = set(search_schema["properties"]["record_types"]["items"]["enum"])
        self.assertLessEqual(set(search["record_types"]), allowed)
        self.assertEqual(
            definitions["explorer_fetch"]["inputSchema"]["required"],
            ["record_id"],
        )


if __name__ == "__main__":
    unittest.main()
