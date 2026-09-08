from __future__ import annotations

import ast
import shutil
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path
import unittest

from advisor_system.assets import advisor_assets_root, selection_report_skill_root
from advisor_system.prompts import (
    FINALIZE_RESPONSE_SCHEMA,
    PROPOSAL_RESPONSE_SCHEMA,
    build_launch_spec,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = REPOSITORY_ROOT / "src"
ADVISOR_SOURCE = SOURCE_ROOT / "advisor_system"

EXPECTED_SKILL_FILES = frozenset(
    {
        Path("selection-report/SKILL.md"),
        Path("selection-report/references/payload.md"),
    }
)


ISOLATED_ADVISOR_PROBE = r"""
import importlib
from pathlib import Path
import sys


class RejectHostImports:
    def find_spec(self, fullname, path=None, target=None):
        if (
            fullname == "franta"
            or fullname.startswith("franta.")
            or fullname == "explorer_system"
            or fullname.startswith("explorer_system.")
        ):
            raise ModuleNotFoundError(
                "the copied Advisor block attempted to import a peer or its Franta host"
            )
        return None


copied_root = Path(sys.argv[1]).resolve()
sys.meta_path.insert(0, RejectHostImports())
sys.path.insert(0, str(copied_root))

package_root = copied_root / "advisor_system"
module_names = ["advisor_system"]
for source in sorted(package_root.rglob("*.py")):
    relative = source.relative_to(copied_root)
    if relative.name == "__init__.py":
        if relative.parent != Path("advisor_system"):
            module_names.append(".".join(relative.parent.parts))
        continue
    module_names.append(".".join(relative.with_suffix("").parts))
for module_name in dict.fromkeys(module_names):
    importlib.import_module(module_name)

from advisor_system.assets import selection_report_skill_root
from advisor_system.prompts import build_launch_spec
from advisor_system.settings import must_resume_session

skill_root = selection_report_skill_root()
assert (skill_root / "SKILL.md").read_text(encoding="utf-8").strip()
assert (skill_root / "references" / "payload.md").read_text(encoding="utf-8").strip()

proposal_one = build_launch_spec(stage="proposal", advisor_index=1)
finalize_one = build_launch_spec(stage="finalize", advisor_index=1)
proposal_two = build_launch_spec(stage="proposal", advisor_index=2)
assert proposal_one.session_key == finalize_one.session_key == proposal_two.session_key
assert proposal_one.resume_required is False
assert finalize_one.resume_required is True
assert proposal_two.resume_required is True
assert must_resume_session(advisor_index=2, stage="finalize") is True
assert not any(
    name == "franta"
    or name.startswith("franta.")
    or name == "explorer_system"
    or name.startswith("explorer_system.")
    for name in sys.modules
)
"""


def _imported_modules(source: Path) -> tuple[tuple[str, int], ...]:
    tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
    imported: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend((alias.name, node.lineno) for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.append((node.module, node.lineno))
    return tuple(imported)


class AdvisorBlockPortabilityTests(unittest.TestCase):
    def test_portable_block_has_no_franta_or_explorer_back_edge(self) -> None:
        forbidden = ("franta", "explorer_system")
        violations: list[str] = []
        for source in sorted(ADVISOR_SOURCE.rglob("*.py")):
            for module_name, line in _imported_modules(source):
                if any(
                    module_name == package or module_name.startswith(f"{package}.")
                    for package in forbidden
                ):
                    relative = source.relative_to(REPOSITORY_ROOT)
                    violations.append(f"{relative}:{line}: {module_name}")
        self.assertEqual(violations, [])

    def test_explorer_has_no_direct_advisor_dependency(self) -> None:
        violations: list[str] = []
        for source in sorted((SOURCE_ROOT / "explorer_system").rglob("*.py")):
            for module_name, line in _imported_modules(source):
                if module_name == "advisor_system" or module_name.startswith(
                    "advisor_system."
                ):
                    relative = source.relative_to(REPOSITORY_ROOT)
                    violations.append(f"{relative}:{line}: {module_name}")
        self.assertEqual(violations, [])

    def test_franta_imports_advisor_only_through_the_adapter(self) -> None:
        importers: set[Path] = set()
        for source in sorted((SOURCE_ROOT / "franta").rglob("*.py")):
            if any(
                module_name == "advisor_system"
                or module_name.startswith("advisor_system.")
                for module_name, _line in _imported_modules(source)
            ):
                importers.add(source.relative_to(SOURCE_ROOT))
        self.assertEqual(importers, {Path("franta/advisor_adapter.py")})

    def test_copied_block_imports_and_runs_without_peer_or_host_packages(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            isolated = Path(raw)
            shutil.copytree(
                ADVISOR_SOURCE,
                isolated / "advisor_system",
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
            )
            probe = subprocess.run(
                [sys.executable, "-I", "-c", ISOLATED_ADVISOR_PROBE, str(isolated)],
                cwd=isolated,
                capture_output=True,
                check=False,
                text=True,
                timeout=30,
            )
        self.assertEqual(
            probe.returncode,
            0,
            f"isolated Advisor probe failed\nstdout:\n{probe.stdout}\nstderr:\n{probe.stderr}",
        )

    def test_selection_report_assets_are_inside_copyable_and_packaged(self) -> None:
        skill_root = advisor_assets_root()
        self.assertEqual(selection_report_skill_root(), skill_root / "selection-report")
        actual_files = frozenset(
            path.relative_to(skill_root)
            for path in skill_root.rglob("*.md")
            if path.is_file()
        )
        self.assertTrue(
            EXPECTED_SKILL_FILES <= actual_files,
            f"missing portable Advisor skill assets: "
            f"{sorted(EXPECTED_SKILL_FILES - actual_files)}",
        )
        for relative in sorted(actual_files):
            source = skill_root / relative
            self.assertFalse(source.is_symlink())
            self.assertTrue(source.read_text(encoding="utf-8").strip())

        configuration = tomllib.loads(
            (REPOSITORY_ROOT / "pyproject.toml").read_text(encoding="utf-8")
        )
        package_data = configuration["tool"]["setuptools"]["package-data"]
        advisor_patterns = set(package_data["advisor_system"])
        self.assertIn("skills/*/*.md", advisor_patterns)
        self.assertIn("skills/*/references/*.md", advisor_patterns)

        with tempfile.TemporaryDirectory() as raw:
            copied = Path(raw) / "advisor_system"
            shutil.copytree(ADVISOR_SOURCE, copied)
            for relative in actual_files:
                self.assertEqual(
                    (copied / "skills" / relative).read_bytes(),
                    (skill_root / relative).read_bytes(),
                )

    def test_launch_specs_encode_memory_report_pause_and_resume_invariants(self) -> None:
        proposal_one = build_launch_spec(stage="proposal", advisor_index=1)
        finalize_one = build_launch_spec(stage="finalize", advisor_index=1)
        proposal_two = build_launch_spec(stage="proposal", advisor_index=2)

        self.assertEqual(proposal_one.role, "advisor")
        self.assertEqual(proposal_one.memory_access_profile, "main-agent-equivalent")
        self.assertEqual(
            proposal_one.skill_names,
            ("task-search", "selection-report"),
        )
        self.assertEqual(finalize_one.skill_names, ("task-search",))
        self.assertEqual(
            proposal_one.session_key,
            finalize_one.session_key,
        )
        self.assertEqual(proposal_one.session_key, proposal_two.session_key)
        self.assertFalse(proposal_one.resume_required)
        self.assertTrue(finalize_one.resume_required)
        self.assertTrue(proposal_two.resume_required)

        proposal = " ".join(proposal_one.prompt.split())
        self.assertIn("Inspect every memory", proposal)
        self.assertIn("every previous problem assignment", proposal)
        self.assertIn("exactly five ranked obligations", proposal)
        self.assertIn("should not merely restate the original problem", proposal)
        self.assertIn("breakthrough", proposal)
        self.assertIn("selection_report", proposal)
        self.assertIn("end this call", proposal)
        self.assertIn("Do not choose subproblems", proposal)

        finalize = " ".join(finalize_one.prompt.split())
        self.assertIn("Resume the existing persistent Advisor session", finalize)
        self.assertIn("Follow the human guidance strictly", finalize)
        self.assertIn("custom choice is authoritative", finalize)
        self.assertIn("one or two choices", finalize)
        self.assertIn("reproduced exactly", finalize)

        self.assertFalse(PROPOSAL_RESPONSE_SCHEMA["additionalProperties"])
        self.assertIs(PROPOSAL_RESPONSE_SCHEMA["properties"]["call_ended"]["const"], True)
        selected = FINALIZE_RESPONSE_SCHEMA["properties"]["selected_subproblems"]
        self.assertEqual((selected["minItems"], selected["maxItems"]), (1, 2))
        self.assertIs(FINALIZE_RESPONSE_SCHEMA["properties"]["call_ended"]["const"], True)


if __name__ == "__main__":
    unittest.main()
