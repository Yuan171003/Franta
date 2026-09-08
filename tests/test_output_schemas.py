from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any, Iterator

from franta.output_schemas import SCHEMAS, STRICT_SCHEMA_BLOCKERS, write_schemas


def _is_object_schema(schema: dict[str, Any]) -> bool:
    value = schema.get("type")
    return value == "object" or (
        isinstance(value, list) and "object" in value
    )


def _walk_schemas(
    schema: Any, path: str
) -> Iterator[tuple[str, dict[str, Any]]]:
    if not isinstance(schema, dict):
        return
    yield path, schema
    properties = schema.get("properties")
    if isinstance(properties, dict):
        for name, child in properties.items():
            yield from _walk_schemas(child, f"{path}.properties.{name}")
    if "items" in schema:
        yield from _walk_schemas(schema["items"], f"{path}.items")
    for keyword in ("anyOf", "oneOf", "allOf"):
        alternatives = schema.get(keyword)
        if isinstance(alternatives, list):
            for index, child in enumerate(alternatives):
                yield from _walk_schemas(child, f"{path}.{keyword}[{index}]")
    definitions = schema.get("$defs")
    if isinstance(definitions, dict):
        for name, child in definitions.items():
            yield from _walk_schemas(child, f"{path}.$defs.{name}")


def _strict_object_violations() -> dict[str, list[str]]:
    violations: dict[str, list[str]] = {}
    for name, root in SCHEMAS.items():
        for path, schema in _walk_schemas(root, name):
            if not _is_object_schema(schema):
                continue
            problems: list[str] = []
            properties = schema.get("properties")
            if not isinstance(properties, dict):
                problems.append("missing properties")
                properties = {}
            if schema.get("additionalProperties") is not False:
                problems.append("additionalProperties is not false")
            required = schema.get("required")
            if not isinstance(required, list) or len(required) != len(set(required)):
                problems.append("required is missing or contains duplicates")
            elif set(required) != set(properties):
                problems.append("required does not name every property exactly once")
            if problems:
                violations[path] = problems
    return violations


def _resolve(schema: dict[str, Any], root: dict[str, Any]) -> dict[str, Any]:
    reference = schema.get("$ref")
    if reference is None:
        return schema
    prefix = "#/$defs/"
    if not isinstance(reference, str) or not reference.startswith(prefix):
        raise AssertionError(f"unsupported test reference: {reference!r}")
    return root["$defs"][reference[len(prefix) :]]


def _matches(value: Any, schema: dict[str, Any], root: dict[str, Any]) -> bool:
    schema = _resolve(schema, root)
    alternatives = schema.get("anyOf")
    if isinstance(alternatives, list):
        return any(_matches(value, child, root) for child in alternatives)

    expected = schema.get("type")
    expected_types = set(expected if isinstance(expected, list) else [expected])
    if value is None:
        return "null" in expected_types
    if isinstance(value, bool):
        actual = "boolean"
    elif isinstance(value, int):
        actual = "integer"
    elif isinstance(value, str):
        actual = "string"
    elif isinstance(value, list):
        actual = "array"
    elif isinstance(value, dict):
        actual = "object"
    else:
        return False
    if actual not in expected_types:
        return False
    if "enum" in schema and value not in schema["enum"]:
        return False
    if actual == "integer" and "minimum" in schema and value < schema["minimum"]:
        return False
    if actual == "array":
        if len(value) < schema.get("minItems", 0):
            return False
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            return False
        return all(_matches(item, schema["items"], root) for item in value)
    if actual == "object":
        properties = schema.get("properties", {})
        required = set(schema.get("required", []))
        if not required <= set(value):
            return False
        if schema.get("additionalProperties") is False and not set(value) <= set(properties):
            return False
        return all(_matches(item, properties[key], root) for key, item in value.items())
    return True


class OutputSchemaTests(unittest.TestCase):
    def test_only_documented_strict_compatibility_blockers_remain(self) -> None:
        self.assertEqual({}, STRICT_SCHEMA_BLOCKERS)
        self.assertEqual(set(_strict_object_violations()), set(STRICT_SCHEMA_BLOCKERS))

    def test_every_object_is_closed_and_requires_all_properties(self) -> None:
        self.assertEqual({}, _strict_object_violations())

    def test_main_has_no_trim_or_stuck_response_branch(self) -> None:
        schema = SCHEMAS["main"]
        self.assertEqual(
            schema["properties"]["decision"]["enum"],
            ["assignments", "wait_for_results", "terminal"],
        )
        self.assertNotIn("report", schema["properties"])
        result = {
            "decision": "wait_for_results",
            "batch_id": None,
            "assignment_report_ids": [],
            "wait_for_task_ids": ["T-1"],
            "decline_proof_writer": False,
        }
        self.assertTrue(_matches(result, schema, schema))
        self.assertFalse(_matches({**result, "decision": "stuck"}, schema, schema))

    def test_trimmer_uses_closed_category_revision_lists(self) -> None:
        schema = SCHEMAS["trimmer"]
        members = {
            "fact": [],
            "route": ["R-1"],
            "memo": [],
            "claim": [],
            "obligation": ["O-1"],
        }
        result = {
            "decision": "commit",
            "proposal": {
                "expected_state_revision": 3,
                "category_changes": [
                    {
                        "kind": "merge",
                        "source_category_ids": ["CAT-1", "CAT-2"],
                        "expected_revisions": [
                            {"category_id": "CAT-1", "category_revision": 2},
                            {"category_id": "CAT-2", "category_revision": 4},
                        ],
                        "result": {
                            "proposal_id": "CAT-MERGED",
                            "name": "Merged route family",
                            "description": "Two presentations of one direction.",
                            "main_progress": "The common reduction is isolated.",
                            "current_obstacles": "The final estimate remains open.",
                            "members": members,
                        },
                    }
                ],
                "portfolio": {
                    "expected_portfolio_revision": 2,
                    "categories": [
                        {"category_id": "CAT-MERGED", "category_revision": 1}
                    ],
                    "flattened_members": members,
                    "base_event_id": 10,
                    "confirmed_through_event_id": 12,
                    "selection_rationale": "Keep the unified direction visible.",
                    "human_guidance_reference": None,
                },
            },
            "expected_portfolio_revision": 2,
            "confirmed_through_event_id": 12,
            "artifact_operation_id": None,
        }
        self.assertTrue(_matches(result, schema, schema))
        result["proposal"]["portfolio"]["categories"] = {"CAT-MERGED": 1}
        self.assertFalse(_matches(result, schema, schema))

    def test_synthesizer_partial_patches_remain_exactly_representable(self) -> None:
        schema = SCHEMAS["synthesizer"]
        route_result = {
            "resolution": "update",
            "operation_digest": "digest",
            "explanation": "The existing route can absorb the progress.",
            "canonical_id": "R-1",
            "relied_on": [{"id": "R-1", "revision": 2}],
            "patch": {
                "target_id": "R-1",
                "expected_base_revision": 2,
                "set": {"progress": ["A new reduction is available."]},
                "add_ids": {"relevant_memo_ids": ["M-1"]},
                "explanation": "Record only the new reduction and its memo.",
            },
        }
        obligation_result = {
            "resolution": "update",
            "operation_digest": "digest-2",
            "explanation": "The relation is new progress on the same obligation.",
            "canonical_id": "O-1",
            "relied_on": [{"id": "O-1", "revision": 4}],
            "patch": {
                "target_id": "O-1",
                "expected_base_revision": 4,
                "append": {
                    "relations": [
                        {
                            "relation_type": "reduction",
                            "premise_memory_ids": ["O-2"],
                            "conclusion": "O-1",
                            "explanation": "O-2 implies the target obligation.",
                        }
                    ]
                },
                "remove_ids": {"related_route_ids": []},
                "explanation": "Append the reduction relation.",
                "supporting_memory_ids": ["F-1"],
            },
        }
        self.assertTrue(_matches(route_result, schema, schema))
        self.assertTrue(_matches(obligation_result, schema, schema))
        route_result["patch"]["unknown"] = "not permitted"
        self.assertFalse(_matches(route_result, schema, schema))

    def test_verifier_exact_subrecords_are_closed(self) -> None:
        schema = SCHEMAS["verifier"]["properties"]
        for field in ("introduced_notation", "external_references"):
            violations = {
                path: problem
                for path, problem in _strict_object_violations().items()
                if path.startswith(f"verifier.properties.{field}")
            }
            self.assertEqual({}, violations)
        root_resolution = schema["root_resolution"]
        self.assertTrue(
            _matches(
                {"target": "ROOT", "outcome": "proved"},
                root_resolution,
                SCHEMAS["verifier"],
            )
        )
        self.assertFalse(
            _matches(
                {"target": "ROOT", "outcome": "unknown"},
                root_resolution,
                SCHEMAS["verifier"],
            )
        )
        error = {
            "location": "Proof, paragraph 3",
            "error": "The boundary case is not justified.",
            "suggested_repair": None,
        }
        self.assertTrue(_matches(error, schema["errors"]["items"], SCHEMAS["verifier"]))
        self.assertFalse(
            _matches(
                {**error, "unstructured_extra": "not allowed"},
                schema["errors"]["items"],
                SCHEMAS["verifier"],
            )
        )

    def test_closure_and_sprint_reports_have_closed_complete_records(self) -> None:
        closure = {
            "outcome": "progress",
            "summary": {"summary": "The reduction is valid but the final estimate is open."},
        }
        self.assertTrue(
            _matches(
                closure,
                SCHEMAS["main-closure-review"],
                SCHEMAS["main-closure-review"],
            )
        )

        sprint = {
            "mechanism_fingerprints": [
                {
                    "lane_id": "A",
                    "lane_status": "progress",
                    "principal_objects": "Boundary strata",
                    "representation": "Incidence complex",
                    "central_move": "Filter by codimension",
                    "required_bridge": "Compare the filtration to O-1",
                    "main_obstacle": "The comparison map is not known to be injective",
                    "evidence_status": "Unverified claim supported by examples",
                }
            ],
            "differences": [
                {
                    "lane_ids": ["A", "B"],
                    "classification": "genuinely_different",
                    "explanation": "The lanes use distinct representations.",
                }
            ],
            "shared_bottlenecks": [
                {
                    "lane_ids": ["A", "B"],
                    "finding": "Both require control of the comparison map.",
                    "evidence_status": "Unverified synthesis observation",
                }
            ],
            "contradictions": [],
            "bridges": [],
            "negative_results": [],
            "follow_up_tasks": [
                {
                    "objective": "Test injectivity in the first singular case.",
                    "suitable_mode": "computation",
                    "source_lane_ids": ["A"],
                    "reason": "A small case distinguishes the mechanisms.",
                }
            ],
        }
        first_fingerprint = sprint["mechanism_fingerprints"][0]
        sprint["mechanism_fingerprints"].extend(
            [{**first_fingerprint, "lane_id": lane_id} for lane_id in ("B", "C", "D")]
        )
        self.assertTrue(_matches(sprint, SCHEMAS["summarizer"], SCHEMAS["summarizer"]))
        sprint["mechanism_fingerprints"][0]["extra"] = "not allowed"
        self.assertFalse(_matches(sprint, SCHEMAS["summarizer"], SCHEMAS["summarizer"]))

    def test_written_schemas_round_trip_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            written = write_schemas(directory)
            self.assertEqual(set(written), set(SCHEMAS))
            for name, path in written.items():
                self.assertEqual(json.loads(Path(path).read_text(encoding="utf-8")), SCHEMAS[name])


if __name__ == "__main__":
    unittest.main()
