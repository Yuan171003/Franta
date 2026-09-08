from __future__ import annotations

import ast
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
import unittest

from franta.config import load_manifest
from franta.runtime import FrantaRuntime


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = REPOSITORY_ROOT / "src"
EXPLORER_SOURCE = SOURCE_ROOT / "explorer_system"

EXPECTED_SKILL_FILES = frozenset(
    {
        Path("explorer-search/SKILL.md"),
        Path("explorer-search/references/api.md"),
        Path("record-scratch/SKILL.md"),
        Path("record-scratch/references/payload.md"),
        Path("record-summary/SKILL.md"),
        Path("record-summary/references/payload.md"),
    }
)


ISOLATED_EXPLORER_PROBE = r"""
import importlib
import hashlib
from pathlib import Path
import sys
import tempfile


class RejectFrantaImports:
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "franta" or fullname.startswith("franta."):
            raise ModuleNotFoundError(
                "the copied Explorer block attempted to import its Franta host"
            )
        return None


copied_root = Path(sys.argv[1]).resolve()
sys.meta_path.insert(0, RejectFrantaImports())
sys.path.insert(0, str(copied_root))

package_root = copied_root / "explorer_system"
module_names = ["explorer_system"]
for source in sorted(package_root.rglob("*.py")):
    relative = source.relative_to(copied_root)
    if relative.name == "__init__.py":
        if relative.parent != Path("explorer_system"):
            module_names.append(".".join(relative.parent.parts))
        continue
    module_names.append(".".join(relative.with_suffix("").parts))
for module_name in dict.fromkeys(module_names):
    importlib.import_module(module_name)

from explorer_system.program import ExplorerProgram
from explorer_system.repository import ExplorerRepository
from explorer_system.service import ExplorerAttempt, ExplorerService
from explorer_system.interfaces import ExplorerTurnContext


class FakeHost:
    phase = "explorer_admission"
    max_workers = 1

    def __init__(self):
        self.state = {"lineages": {}, "admission_order": []}
        self.calls = {}
        self.admitted = False
        self.launches = []
        self.cancelled = []

    def tick(self):
        return None

    def expire_attempts(self):
        return ()

    def cancel_call(self, call_id, *, reason):
        self.cancelled.append((call_id, reason))

    def controller_snapshot(self):
        return self.state

    def visible_high_water(self):
        return 0

    def admit_lineage(self):
        if self.admitted:
            return False
        self.admitted = True
        lineage_id = "standalone-lineage"
        self.state["admission_order"].append(lineage_id)
        self.state["lineages"][lineage_id] = {
            "status": "ready",
            "current_call_id": None,
            "attempts_started": 0,
        }
        return True

    def start_attempt(self, lineage_id, *, source_high_water_seq):
        assert source_high_water_seq == 0
        lineage = self.state["lineages"][lineage_id]
        number = lineage["attempts_started"] + 1
        call_id = f"standalone-call-{number}"
        lineage["attempts_started"] = number
        lineage["status"] = "running"
        lineage["current_call_id"] = call_id
        self.calls[call_id] = {
            "status": "prepared",
            "input": {"explorer_turn_id": "standalone-turn"},
            "attempt_number": number,
        }
        return call_id

    def call_is_launchable(self, call_id):
        return self.calls[call_id]["status"] == "prepared"

    def prepare_launch(self, call_id):
        number = self.calls[call_id]["attempt_number"]
        return {
            "attempt_number": number,
            "mode": "clean-room" if number == 1 else "explore",
        }

    def run_launch(self, call_id, launch):
        self.launches.append((call_id, dict(launch)))
        return {
            "attempt_ended": True,
            "final_summary_id": f"summary-{launch['attempt_number']}",
            "root_candidate_scratch_id": None,
            "root_candidate_outcome": None,
            "stop_reason": "attempt_complete",
        }

    def call_snapshot(self, call_id):
        return self.calls[call_id]

    def validate_result(self, call_id, result):
        assert result["attempt_ended"] is True
        assert result["root_candidate_scratch_id"] is None

    def fail_attempt(self, call_id, *, reason):
        raise AssertionError(f"unexpected failure for {call_id}: {reason}")

    def commit_attempt(self, call_id, *, outcome, root_candidate):
        assert outcome == "progress"
        assert root_candidate is None
        call = self.calls[call_id]
        call["status"] = "completed"
        lineage = self.state["lineages"]["standalone-lineage"]
        lineage["current_call_id"] = None
        lineage["status"] = (
            "continuation_pending"
            if lineage["attempts_started"] < 3
            else "closed"
        )
        return ()


host = FakeHost()
program = ExplorerProgram(host, poll_seconds=0)
wave_results = [program.run_wave() for _ in range(4)]
assert wave_results == [True, True, True, False], wave_results
assert [launch[1]["attempt_number"] for launch in host.launches] == [1, 2, 3]
assert [launch[1]["mode"] for launch in host.launches] == [
    "clean-room",
    "explore",
    "explore",
]
assert host.state["lineages"]["standalone-lineage"]["status"] == "closed"
assert host.cancelled == []

with tempfile.TemporaryDirectory() as raw:
    repository = ExplorerRepository(
        Path(raw) / "explorer.sqlite3",
        export_kinds={"insight", "calculation"},
    )
    service = ExplorerService(repository)
    attempt = ExplorerAttempt(
        turn_id="portable-turn",
        worker_session_id="portable-session",
        attempt_no=1,
        call_id="portable-call",
    )
    scratch_receipt = hashlib.sha256(b"portable-scratch").hexdigest()
    scratch = service.prepare_staged_result(
        attempt,
        skill="record-scratch",
        receipt_sha256=scratch_receipt,
        artifact={
            "skill": "record-scratch",
            "operation_id": "portable-scratch-op",
            "record_id": "ES-portable-scratch",
            "record_kind": "idea",
            "abstract": "Portable idea",
            "content": "A host-independent research idea.",
            "related_memory_ids": ["OTHERHOST:record:1"],
            "cas_operation_ids": [],
        },
    )
    service.trust_receipt(scratch_receipt)
    handoff = service.create_handoff("portable-turn")
    assert handoff.protocol_version == 1
    assert handoff.record_count == 1
    frozen = repository.freeze_turn("portable-turn")
    assert scratch.record_id in frozen.record_ids
    run = repository.create_handoff_run(
        "otherhost-run-1", frozen, "OTHERHOST:context:1"
    )
    export = repository.stage_export(
        run.run_id,
        "OTHERHOST:item:1",
        "insight",
        [scratch.record_id],
        hashlib.sha256(b"otherhost-export").hexdigest(),
    )
    assert export.target_kind == "insight"
    assert export.host_item_id == "OTHERHOST:item:1"
    assert not hasattr(export, "franta_operation_id")
    assert not hasattr(export, "canonical_id")
    repository.close()

with tempfile.TemporaryDirectory() as raw:
    repository = ExplorerRepository(Path(raw) / "turn.sqlite3")
    service = ExplorerService(repository)

    class TurnHost(FakeHost):
        def __init__(self):
            super().__init__()
            self.phase = "explorer_admission"
            self.admitted = True

        def tick(self):
            if self.phase == "explorer_admission":
                self.phase = "explorer_drain"

        def explorer_is_drained(self):
            return True

        def current_turn_context(self):
            return ExplorerTurnContext(turn_id="portable-lifecycle-turn")

    class FakeCollaborator:
        def __init__(self, host):
            self.host = host
            self.accepted = []

        def handoff_id_for(self, frozen_turn):
            return "OTHER-HANDOFF-" + frozen_turn.source_set_digest[:16]

        def accept_explorer_handoff(self, handoff):
            self.accepted.append(handoff)
            self.host.phase = "host_sort"
            return {"accepted": handoff.handoff_id}

    turn_host = TurnHost()
    collaborator = FakeCollaborator(turn_host)
    advance = service.program(
        turn_host, collaborator, poll_seconds=0
    ).advance_turn()
    assert advance.handled is True
    assert advance.progressed is True
    assert advance.handoff is collaborator.accepted[0]
    assert advance.handoff.turn_id == "portable-lifecycle-turn"
    assert advance.handoff.record_count == 0
    assert advance.collaborator_receipt == {
        "accepted": advance.handoff.handoff_id
    }
    assert turn_host.phase == "host_sort"
    repository.close()

assert not any(
    name == "franta" or name.startswith("franta.") for name in sys.modules
)
"""


def _legacy_manifest(directory: Path) -> Path:
    path = directory / "bootstrap.toml"
    path.write_text(
        "\n".join(
            [
                "[project]",
                'name = "explorer-disabled-boundary"',
                'directory = "project"',
                'root_problem = "Prove that every test object has property P."',
                'foundation_policy = "Use the declared definition only."',
                "",
                "[initial]",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return path


class ExplorerBlockPortabilityTests(unittest.TestCase):
    def test_block_has_no_static_franta_imports(self) -> None:
        violations: list[str] = []
        python_sources = sorted(EXPLORER_SOURCE.rglob("*.py"))
        self.assertTrue(python_sources, "Explorer copy boundary has no Python modules")
        for source in python_sources:
            tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
            for node in ast.walk(tree):
                imported: tuple[str, ...] = ()
                if isinstance(node, ast.Import):
                    imported = tuple(alias.name for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imported = (node.module,)
                for module_name in imported:
                    if module_name == "franta" or module_name.startswith("franta."):
                        relative = source.relative_to(REPOSITORY_ROOT)
                        violations.append(f"{relative}:{node.lineno}: {module_name}")
        self.assertEqual(violations, [])

    def test_franta_reaches_the_block_only_through_adapter_or_compatibility_shims(
        self,
    ) -> None:
        allowed = {
            Path("franta/explorer_adapter.py"),
            Path("franta/explorer/contracts.py"),
            Path("franta/explorer/repository.py"),
            Path("franta/explorer_control/state.py"),
            Path("franta/phase_control/state.py"),
        }
        violations: list[str] = []
        franta_root = SOURCE_ROOT / "franta"
        for source in sorted(franta_root.rglob("*.py")):
            tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
            imports_block = False
            for node in ast.walk(tree):
                names: tuple[str, ...] = ()
                if isinstance(node, ast.Import):
                    names = tuple(alias.name for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    names = (node.module,)
                if any(
                    name == "explorer_system" or name.startswith("explorer_system.")
                    for name in names
                ):
                    imports_block = True
                    break
            relative = source.relative_to(SOURCE_ROOT)
            if imports_block and relative not in allowed:
                violations.append(str(relative))
        self.assertEqual(violations, [])

    def test_copied_block_runs_three_attempts_and_single_entry_handoff_without_franta(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            isolated = Path(raw)
            shutil.copytree(
                EXPLORER_SOURCE,
                isolated / "explorer_system",
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
            )
            probe = subprocess.run(
                [
                    sys.executable,
                    "-I",
                    "-c",
                    ISOLATED_EXPLORER_PROBE,
                    str(isolated),
                ],
                cwd=isolated,
                capture_output=True,
                check=False,
                text=True,
                timeout=30,
            )
        self.assertEqual(
            probe.returncode,
            0,
            f"isolated Explorer probe failed\nstdout:\n{probe.stdout}\nstderr:\n{probe.stderr}",
        )

    def test_all_explorer_skill_assets_are_inside_and_copy_with_the_block(
        self,
    ) -> None:
        skill_root = EXPLORER_SOURCE / "skills"
        actual_files = frozenset(
            path.relative_to(skill_root)
            for path in skill_root.rglob("*.md")
            if path.is_file()
        )
        self.assertTrue(
            EXPECTED_SKILL_FILES <= actual_files,
            f"missing portable Explorer skill assets: "
            f"{sorted(EXPECTED_SKILL_FILES - actual_files)}",
        )
        for relative in sorted(actual_files):
            source = skill_root / relative
            self.assertFalse(source.is_symlink())
            self.assertTrue(source.read_text(encoding="utf-8").strip())

        with tempfile.TemporaryDirectory() as raw:
            copied = Path(raw) / "explorer_system"
            shutil.copytree(EXPLORER_SOURCE, copied)
            for relative in actual_files:
                self.assertEqual(
                    (copied / "skills" / relative).read_bytes(),
                    (skill_root / relative).read_bytes(),
                )

    def test_legacy_runtime_does_not_activate_or_attach_explorer(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            manifest = load_manifest(_legacy_manifest(Path(raw)))
            self.assertIsNone(manifest.explorer)
            runtime = FrantaRuntime.initialize(manifest)
            try:
                self.assertIsNone(runtime.explorer_repository)
                self.assertIsNone(runtime.explorer_program)
                self.assertNotIn("phase_control", runtime.scheduler.state)
                self.assertNotIn("explorer_control", runtime.scheduler.state)
                self.assertFalse(runtime.layout.explorer_database.exists())
                self.assertFalse(runtime.layout.explorer_cas_archive.exists())
                self.assertFalse(
                    (runtime.layout.schemas / "explorer-worker.schema.json").exists()
                )
                self.assertFalse(
                    (runtime.layout.schemas / "main-sort.schema.json").exists()
                )
                self.assertEqual(
                    runtime.materializer.skill_sources,
                    (runtime.materializer.skill_source,),
                )
            finally:
                runtime.close()


if __name__ == "__main__":
    unittest.main()
