from __future__ import annotations

import ast
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any
import unittest

from franta.contracts.agent_access import policy_for
from franta.explorer_adapter import (
    FRANTA_SORT_TOOLS,
    explorer_agent_call_spec,
    main_sort_computation_has_exact_provenance,
    main_sort_operation_has_exact_provenance,
    main_sort_operation_kinds,
    main_sort_snapshot_contract,
    main_sort_snapshot_digest,
    normalize_main_sort_computation_promotions,
    render_main_sort_snapshot_files,
    validate_main_sort_materialized_snapshot,
    validate_main_sort_progress_operation,
    validate_main_sort_submission,
)
from franta.read_access.materialization import (
    MaterializationError,
    explorer_snapshot_digest,
)
from explorer_system.agents import (
    RESPONSE_SCHEMAS,
    build_launch_spec as build_legacy_launch_spec,
    main_sort_prompt as legacy_main_sort_prompt,
)
from explorer_system.main_sort import (
    MAIN_SORT_MODEL_NAME,
    MAIN_SORT_REASONING_EFFORT,
    MAIN_SORT_RESPONSE_SCHEMA,
    SNAPSHOT_FORMAT_VERSION,
    DEFAULT_MAIN_SORT_BLOCK,
    HostSortTools,
    MainSortBlock,
    MainSortLaunchSpec,
    MainSortModelRoute,
    MainSortProgram,
    MainSortValidationError,
    SnapshotError,
    ValidatedMainSortSubmission,
    build_main_sort_launch_spec,
    main_sort_prompt,
    render_snapshot_files,
    snapshot_digest,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
MAIN_SORT_SOURCE = REPOSITORY_ROOT / "src" / "explorer_system" / "main_sort"


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _snapshot_fixture() -> dict[str, Any]:
    turn_id = "XTURN-main-sort-isolation"
    scratch_content = "A complete proof-carrying scratch."
    summary_content = "The attempt summary indexes the proof scratch."
    scratch_input_digest = "a" * 64
    summary_input_digest = "b" * 64
    source_identity = [
        {
            "record_id": "ES-main-sort-proof",
            "input_digest": scratch_input_digest,
        },
        {
            "record_id": "ESUM-main-sort-summary",
            "input_digest": summary_input_digest,
        },
    ]
    source_set_digest = _sha256_text(_canonical_json(source_identity))
    common = {
        "record_space": "explorer",
        "status": "provisional",
        "turn_id": turn_id,
        "worker_session_id": "EWORK-main-sort-isolation",
        "attempt_no": 1,
        "related_memory_ids": [],
        "cas_operation_ids": [],
        "directions_tried": [],
        "main_progress": None,
        "main_obstacles": None,
        "created_at": "2026-08-31T00:00:00Z",
    }
    scratch = {
        **common,
        "id": "ES-main-sort-proof",
        "record_type": "scratch",
        "record_kind": "proof",
        "title": "Proof scratch",
        "seq": 1,
        "operation_id": "OP-main-sort-proof",
        "input_digest": scratch_input_digest,
        "content_digest": _sha256_text(scratch_content),
        "abstract": "Proof scratch",
        "content": scratch_content,
        "cas_operation_ids": ["CAS-main-sort-proof"],
        "cas_evidence": [
            {
                "evidence_id": "XCAS-main-sort-proof",
                "operation_id": "CAS-main-sort-proof",
                "execution_succeeded": True,
                "turn_id": turn_id,
                "worker_session_id": "EWORK-main-sort-isolation",
                "attempt_no": 1,
                "output_artifact_sha256": "c" * 64,
            }
        ],
        "source_scratch_ids": [],
        "source_set_digest": None,
    }
    summary = {
        **common,
        "id": "ESUM-main-sort-summary",
        "record_type": "summary",
        "record_kind": None,
        "title": "Attempt summary",
        "seq": 2,
        "operation_id": "OP-main-sort-summary",
        "input_digest": summary_input_digest,
        "content_digest": _sha256_text(summary_content),
        "abstract": "Attempt summary",
        "content": summary_content,
        "cas_evidence": [],
        "source_scratch_ids": ["ES-main-sort-proof"],
        "source_set_digest": source_set_digest,
        "directions_tried": ["Prove the isolated test statement."],
        "main_progress": "Produced a proof scratch.",
        "main_obstacles": "None in this fixture.",
    }
    return {
        "format_version": SNAPSHOT_FORMAT_VERSION,
        "sort_run_id": "XSORT-main-sort-isolation",
        "turn_id": turn_id,
        "source_high_water_seq": 2,
        "source_set_digest": source_set_digest,
        "records": [scratch, summary],
    }


class _FakeMainSortBlock:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def model_route(self) -> MainSortModelRoute:
        return MainSortModelRoute(
            model="fake-main-sort-model",
            reasoning_effort="fake-main-sort-effort",
        )

    def response_schema(self) -> dict[str, Any]:
        return {"type": "object", "fake": True}

    def build_launch_spec(
        self,
        *,
        root_problem: str,
        input_path: str = "input/task_card.json",
        host_agent_name: str = "host collaborator",
        host_tools: HostSortTools | None = None,
    ) -> MainSortLaunchSpec:
        self.calls.append(
            {
                "root_problem": root_problem,
                "input_path": input_path,
                "host_agent_name": host_agent_name,
                "host_tools": host_tools,
            }
        )
        return MainSortLaunchSpec(
            role="main-sort",
            prompt="FAKE MAIN-SORT PROMPT",
            schema_name="fake-main-sort-schema",
            model_route=self.model_route(),
        )

    def render_snapshot_files(self, snapshot: Any) -> Any:
        raise AssertionError("launch adaptation must not render a snapshot")

    def snapshot_digest(self, snapshot: Any) -> str:
        raise AssertionError("launch adaptation must not digest a snapshot")

    def validate_materialized_snapshot(self, workspace: Any, snapshot: Any) -> Any:
        raise AssertionError("launch adaptation must not validate a workspace")

    def validate_submission(self, result: Any, progress: Any, **kwargs: Any) -> Any:
        raise AssertionError("launch adaptation must not validate a submission")


class _FakeSnapshotAndValidationBlock(_FakeMainSortBlock):
    def __init__(self) -> None:
        super().__init__()
        self.boundary_calls: list[tuple[str, Any]] = []

    def snapshot_contract(self) -> tuple[int, Path]:
        self.boundary_calls.append(("contract", None))
        return 7, Path("input/fake-main-sort-snapshot")

    def build_snapshot(self, **kwargs: Any) -> dict[str, Any]:
        self.boundary_calls.append(("build", kwargs))
        return {"fake": "snapshot"}

    def render_snapshot_files(self, snapshot: Any) -> Any:
        self.boundary_calls.append(("render", snapshot))
        return {Path("fake-snapshot.json"): "fake snapshot\n"}

    def snapshot_digest(self, snapshot: Any) -> str:
        self.boundary_calls.append(("digest", snapshot))
        return "d" * 64

    def validate_materialized_snapshot(self, workspace: Any, snapshot: Any) -> Any:
        self.boundary_calls.append(("materialized", (workspace, snapshot)))
        return Path(workspace) / "input" / "explorer_snapshot"

    def validate_submission(self, result: Any, progress: Any, **kwargs: Any) -> Any:
        self.boundary_calls.append(
            ("submission", (result, progress, dict(kwargs)))
        )
        return ValidatedMainSortSubmission(
            final_progress_id="PRG-from-fake-port",
            selected_source_ids=("ES-from-fake-port",),
            deferred_computation_record_ids=(),
        )

    def operation_kinds(self) -> frozenset[str]:
        self.boundary_calls.append(("operation-kinds", None))
        return frozenset({"fake_add"})

    def validate_progress_operation(
        self, operation: Any, *, sort_run_id: str
    ) -> None:
        self.boundary_calls.append(
            ("progress-operation", (operation, sort_run_id))
        )

    def normalize_computation_promotions(self, value: Any) -> Any:
        self.boundary_calls.append(("normalize-computations", value))
        return [{"normalized": value}]

    def operation_has_exact_provenance(
        self, operation: Any, *, sort_run_id: str
    ) -> bool:
        self.boundary_calls.append(
            ("operation-provenance", (operation, sort_run_id))
        )
        return True

    def computation_has_exact_provenance(
        self, computation: Any, *, sort_run_id: str
    ) -> bool:
        self.boundary_calls.append(
            ("computation-provenance", (computation, sort_run_id))
        )
        return True


class MainSortBlockIsolationTests(unittest.TestCase):
    def test_block_has_no_host_or_explorer_sibling_imports(self) -> None:
        violations: list[str] = []
        sources = sorted(MAIN_SORT_SOURCE.rglob("*.py"))
        self.assertTrue(sources)
        for source in sources:
            tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        if alias.name == "franta" or alias.name.startswith("franta."):
                            violations.append(
                                f"{source.name}:{node.lineno}: {alias.name}"
                            )
                        if (
                            alias.name == "explorer_system"
                            or alias.name.startswith("explorer_system.")
                        ) and not alias.name.startswith("explorer_system.main_sort"):
                            violations.append(
                                f"{source.name}:{node.lineno}: {alias.name}"
                            )
                elif isinstance(node, ast.ImportFrom):
                    if node.level > 1:
                        violations.append(
                            f"{source.name}:{node.lineno}: relative level {node.level}"
                        )
                    module = node.module or ""
                    if module == "franta" or module.startswith("franta."):
                        violations.append(f"{source.name}:{node.lineno}: {module}")
                    if (
                        module == "explorer_system"
                        or module.startswith("explorer_system.")
                    ) and not module.startswith("explorer_system.main_sort"):
                        violations.append(f"{source.name}:{node.lineno}: {module}")
        self.assertEqual(violations, [])

    def test_block_copies_and_imports_without_franta_or_explorer_siblings(self) -> None:
        probe = r"""
import hashlib
import sys
from pathlib import Path


class RejectHostOrExplorerImports:
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "franta" or fullname.startswith("franta."):
            raise ModuleNotFoundError("isolated main-sort imported Franta")
        if fullname == "explorer_system" or fullname.startswith("explorer_system."):
            raise ModuleNotFoundError("isolated main-sort imported Explorer siblings")
        return None


copied_root = Path(sys.argv[1]).resolve()
sys.meta_path.insert(0, RejectHostOrExplorerImports())
sys.path.insert(0, str(copied_root))
import main_sort

tools = main_sort.HostSortTools(
    published_search="internal-search",
    progress_writer="record-progress",
    provenance_field="explorer_provenance",
    computation_exports_field="explorer_computation_promotions",
)
prompt = main_sort.main_sort_prompt(
    "ROOT TEST", host_agent_name="Franta", host_tools=tools
)
assert hashlib.sha256(prompt.encode("utf-8")).hexdigest() == (
    "37fa2e3a96a09291621fb8f084479c5def4d52f5e4ea3ec8d23dd9b4f6f25bd2"
)
assert main_sort.DEFAULT_MAIN_SORT_BLOCK.build_launch_spec(
    root_problem="ROOT"
).model_route.reasoning_effort == "ultra"
assert not any(
    name == "franta" or name.startswith("franta.")
    or name == "explorer_system" or name.startswith("explorer_system.")
    for name in sys.modules
)
"""
        with tempfile.TemporaryDirectory() as raw:
            copied_root = Path(raw)
            shutil.copytree(
                MAIN_SORT_SOURCE,
                copied_root / "main_sort",
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
            )
            result = subprocess.run(
                [sys.executable, "-I", "-c", probe, str(copied_root)],
                cwd=copied_root,
                capture_output=True,
                check=False,
                text=True,
                timeout=30,
            )
        self.assertEqual(
            result.returncode,
            0,
            f"isolated main-sort probe failed\nstdout:\n{result.stdout}"
            f"\nstderr:\n{result.stderr}",
        )

    def test_prompt_model_schema_and_legacy_facade_are_byte_compatible(self) -> None:
        direct_prompt = main_sort_prompt(
            "ROOT TEST",
            host_agent_name="Franta",
            host_tools=FRANTA_SORT_TOOLS,
        )
        self.assertEqual(
            _sha256_text(direct_prompt),
            "37fa2e3a96a09291621fb8f084479c5def4d52f5e4ea3ec8d23dd9b4f6f25bd2",
        )
        self.assertEqual(
            direct_prompt,
            legacy_main_sort_prompt(
                "ROOT TEST",
                host_agent_name="Franta",
                host_tools=FRANTA_SORT_TOOLS,
            ),
        )
        direct = build_main_sort_launch_spec(
            root_problem="ROOT TEST",
            host_agent_name="Franta",
            host_tools=FRANTA_SORT_TOOLS,
        )
        legacy = build_legacy_launch_spec(
            "main_sort",
            root_problem="ROOT TEST",
            host_agent_name="Franta",
            host_sort_tools=FRANTA_SORT_TOOLS,
        )
        self.assertEqual(direct.role, legacy.role)
        self.assertEqual(direct.prompt, legacy.prompt)
        self.assertEqual(direct.schema_name, legacy.schema_name)
        self.assertEqual(direct.model_route.model, legacy.model_route.model)
        self.assertEqual(
            direct.model_route.reasoning_effort,
            legacy.model_route.reasoning_effort,
        )
        self.assertEqual(direct.model_route.model, MAIN_SORT_MODEL_NAME)
        self.assertEqual(
            direct.model_route.reasoning_effort,
            MAIN_SORT_REASONING_EFFORT,
        )
        self.assertEqual(MAIN_SORT_RESPONSE_SCHEMA, RESPONSE_SCHEMAS["main-sort"])
        self.assertIsInstance(DEFAULT_MAIN_SORT_BLOCK, MainSortBlock)
        self.assertIsInstance(DEFAULT_MAIN_SORT_BLOCK, MainSortProgram)

    def test_franta_adapter_consumes_only_the_injected_main_sort_port(self) -> None:
        fake = _FakeMainSortBlock()
        self.assertIsInstance(fake, MainSortBlock)
        adapted = explorer_agent_call_spec(
            "main-sort",
            root_problem="ROOT VIA FAKE PORT",
            mode="main-sort",
            policy=policy_for("main-sort"),
            input_path="input/fake-task-card.json",
            main_sort_block=fake,
        )
        self.assertEqual(adapted.prompt, "FAKE MAIN-SORT PROMPT")
        self.assertEqual(adapted.schema_name, "fake-main-sort-schema")
        self.assertEqual(adapted.model_config.model, "fake-main-sort-model")
        self.assertEqual(
            adapted.model_config.reasoning_effort,
            "fake-main-sort-effort",
        )
        self.assertEqual(
            fake.calls,
            [
                {
                    "root_problem": "ROOT VIA FAKE PORT",
                    "input_path": "input/fake-task-card.json",
                    "host_agent_name": "Franta",
                    "host_tools": FRANTA_SORT_TOOLS,
                }
            ],
        )

    def test_franta_snapshot_and_validation_adapters_use_the_injected_port(
        self,
    ) -> None:
        fake = _FakeSnapshotAndValidationBlock()
        opaque_snapshot = {"owned": "by fake port"}
        self.assertEqual(
            main_sort_snapshot_contract(main_sort_block=fake),
            (7, Path("input/fake-main-sort-snapshot")),
        )
        self.assertEqual(
            render_main_sort_snapshot_files(
                opaque_snapshot, main_sort_block=fake
            ),
            {Path("fake-snapshot.json"): "fake snapshot\n"},
        )
        self.assertEqual(
            main_sort_snapshot_digest(opaque_snapshot, main_sort_block=fake),
            "d" * 64,
        )
        self.assertEqual(
            validate_main_sort_materialized_snapshot(
                "/tmp/fake-main-sort-workspace",
                opaque_snapshot,
                main_sort_block=fake,
            ),
            Path("/tmp/fake-main-sort-workspace/input/explorer_snapshot"),
        )
        source_port = lambda record_id: record_id == "ES-from-fake-port"
        validated = validate_main_sort_submission(
            {"opaque": "result"},
            [{"opaque": "progress"}],
            task_id="T-fake",
            attempt=7,
            sort_run_id="XSORT-fake",
            source_is_allowed=source_port,
            main_sort_block=fake,
        )
        self.assertEqual(validated.final_progress_id, "PRG-from-fake-port")
        validate_main_sort_progress_operation(
            {"opaque": "operation"},
            sort_run_id="XSORT-fake",
            main_sort_block=fake,
        )
        self.assertEqual(
            normalize_main_sort_computation_promotions(
                ["opaque"], main_sort_block=fake
            ),
            [{"normalized": ["opaque"]}],
        )
        self.assertEqual(
            main_sort_operation_kinds(main_sort_block=fake),
            frozenset({"fake_add"}),
        )
        self.assertTrue(
            main_sort_operation_has_exact_provenance(
                {"opaque": "operation"},
                sort_run_id="XSORT-fake",
                main_sort_block=fake,
            )
        )
        self.assertTrue(
            main_sort_computation_has_exact_provenance(
                {"opaque": "computation"},
                sort_run_id="XSORT-fake",
                main_sort_block=fake,
            )
        )
        self.assertEqual(
            fake.boundary_calls,
            [
                ("contract", None),
                ("render", opaque_snapshot),
                ("digest", opaque_snapshot),
                (
                    "materialized",
                    ("/tmp/fake-main-sort-workspace", opaque_snapshot),
                ),
                (
                    "submission",
                    (
                        {"opaque": "result"},
                        [{"opaque": "progress"}],
                        {
                            "task_id": "T-fake",
                            "attempt": 7,
                            "sort_run_id": "XSORT-fake",
                            "source_is_allowed": source_port,
                        },
                    ),
                ),
                (
                    "progress-operation",
                    ({"opaque": "operation"}, "XSORT-fake"),
                ),
                ("normalize-computations", ["opaque"]),
                ("operation-kinds", None),
                (
                    "operation-provenance",
                    ({"opaque": "operation"}, "XSORT-fake"),
                ),
                (
                    "computation-provenance",
                    ({"opaque": "computation"}, "XSORT-fake"),
                ),
            ],
        )

    def test_snapshot_format_and_digest_are_frozen_and_legacy_compatible(self) -> None:
        snapshot = _snapshot_fixture()
        files = render_snapshot_files(snapshot)
        self.assertEqual(
            {path.as_posix() for path in files},
            {
                "README.md",
                "manifest.json",
                "catalog.jsonl",
                "records/scratches/00000001.json",
                "records/summaries/00000002.json",
            },
        )
        manifest = json.loads(files[Path("manifest.json")])
        self.assertEqual(manifest["record_count"], 2)
        self.assertEqual(manifest["scratch_count"], 1)
        self.assertEqual(manifest["summary_count"], 1)
        self.assertEqual(manifest["snapshot_digest"], snapshot_digest(snapshot))
        self.assertEqual(snapshot_digest(snapshot), explorer_snapshot_digest(snapshot))
        rendered_digest = _sha256_text(
            _canonical_json(
                [
                    {
                        "path": path.as_posix(),
                        "content": content,
                    }
                    for path, content in sorted(
                        files.items(), key=lambda item: item[0].as_posix()
                    )
                ]
            )
        )
        # This pins every rendered byte independently of the compatibility wrapper.
        self.assertEqual(
            rendered_digest,
            "acdd473129f3103120a14066b4c12c99f21c6347fe3eee9cfa5d6f6e7ec5f67a",
        )

    def test_snapshot_errors_keep_the_legacy_exception_boundary(self) -> None:
        malformed = _snapshot_fixture()
        malformed["source_high_water_seq"] = 3
        with self.assertRaisesRegex(SnapshotError, "high-water"):
            snapshot_digest(malformed)
        with self.assertRaisesRegex(MaterializationError, "high-water"):
            explorer_snapshot_digest(malformed)

    def test_submission_validation_uses_only_the_read_only_source_port(self) -> None:
        sort_run_id = "XSORT-validation-port"
        progress = [
            {
                "progress_id": "PRG-final",
                "task_id": "T-main-sort",
                "attempt": 1,
                "is_final": True,
                "operations": [
                    {
                        "operation_id": "OP-route",
                        "explorer_provenance": {
                            "sort_run_id": sort_run_id,
                            "source_record_ids": ["ES-route-source"],
                        },
                    }
                ],
                "computations": [
                    {
                        "operation_id": "OP-computation",
                        "explorer_provenance": {
                            "sort_run_id": sort_run_id,
                            "source_record_ids": ["ES-computation-source"],
                        },
                    }
                ],
            }
        ]
        result = {
            "sort_ended": True,
            "final_progress_id": "PRG-final",
            "selected_explorer_record_ids": [
                "ES-route-source",
                "ES-computation-source",
            ],
            "deferred_computation_record_ids": ["ES-deferred-source"],
        }
        reads: list[str] = []

        def source_is_allowed(record_id: str) -> bool:
            reads.append(record_id)
            return record_id in {
                "ES-route-source",
                "ES-computation-source",
                "ES-deferred-source",
            }

        validated = DEFAULT_MAIN_SORT_BLOCK.validate_submission(
            result,
            progress,
            task_id="T-main-sort",
            attempt=1,
            sort_run_id=sort_run_id,
            source_is_allowed=source_is_allowed,
        )
        self.assertEqual(validated.final_progress_id, "PRG-final")
        self.assertEqual(
            validated.selected_source_ids,
            ("ES-route-source", "ES-computation-source"),
        )
        self.assertEqual(
            validated.deferred_computation_record_ids,
            ("ES-deferred-source",),
        )
        self.assertEqual(
            reads,
            [
                "ES-route-source",
                "ES-computation-source",
                "ES-deferred-source",
            ],
        )

        with self.assertRaisesRegex(MainSortValidationError, "outside"):
            DEFAULT_MAIN_SORT_BLOCK.validate_submission(
                result,
                progress,
                task_id="T-main-sort",
                attempt=1,
                sort_run_id=sort_run_id,
                source_is_allowed=lambda record_id: record_id
                != "ES-deferred-source",
            )


if __name__ == "__main__":
    unittest.main()
