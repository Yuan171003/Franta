from __future__ import annotations

from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from check_distribution import audit_entries
from export_release import export, public_files


class DistributionTests(unittest.TestCase):
    def test_export_rejects_output_inside_packaged_source_directories(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for relative in ("src/generated", "tests", "docs/releases", ".agents/skills"):
                with self.subTest(relative=relative), self.assertRaisesRegex(ValueError, "outside public source"):
                    export(root, root / relative, force=True)

    def test_export_excludes_private_data_caches_and_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            allowed = {"README.md", "src/franta/__init__.py", ".agents/skills/CAS/SKILL.md", "tests/fixtures/regression.json"}
            forbidden = {"long_tests/run.json", "src/franta/__pycache__/module.pyc", ".env", "src/franta/.env.json", "src/private/history.json", "src/anything.egg-info/PKG-INFO", "tests/.DS_Store", "src/secret.key", "src/auth.json", "examples/workspaces/session.json", "examples/task-archive/response.json"}
            for name in allowed | forbidden:
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("fixture", encoding="utf-8")
            self.assertEqual({path.relative_to(root).as_posix() for path in public_files(root)}, allowed)

    def test_export_rejects_links_to_files_outside_the_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "src").mkdir()
            (root / "private.txt").write_text("secret", encoding="utf-8")
            (root / "src/link.txt").symlink_to(root / "private.txt")
            with self.assertRaisesRegex(ValueError, "symlink"):
                public_files(root)

    def test_archive_audit_rejects_private_paths_and_home_directories(self) -> None:
        for name, data in (
            ("long_tests/run.json", b"{}"),
            ("src/.env", b"secret"),
            ("src/config.py", b"/" + b"Users" + b"/somebody/tool"),
            ("src/config.py", b"Da" + b"NuS"),
            ("src/config.py", b"sk-" + b"X" * 32),
            ("src/auth.json", b"{}"),
            ("examples/private/state.json", b"{}"),
            ("../outside.txt", b"escape"),
        ):
            with self.subTest(name=name), self.assertRaises(ValueError):
                audit_entries({name: data})
        audit_entries({"src/config.py": b"sage = 'sage'"})

    def test_export_rejects_symlinked_parents_of_public_directories(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "checkout"
            root.mkdir()
            outside = Path(directory) / "outside"
            (outside / "skills/CAS").mkdir(parents=True)
            (outside / "skills/CAS/private-notes.md").write_text("private", encoding="utf-8")
            (root / ".agents").symlink_to(outside, target_is_directory=True)
            with self.assertRaisesRegex(ValueError, "symlink"):
                public_files(root)

    def test_materializer_uses_installed_skill_resources_without_a_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            installed = root / "site-packages"
            shutil.copytree(ROOT / "src/franta", installed / "franta", ignore=shutil.ignore_patterns("__pycache__"))
            shutil.copytree(ROOT / ".agents/skills", installed / "franta/skills", ignore=shutil.ignore_patterns(".DS_Store"))
            (installed / "franta/skills/CAS/.DS_Store").write_bytes(b"\x00\xff")
            program = '''
from pathlib import Path
import sys
sys.path.insert(0, sys.argv[1])
from franta.contracts.agent_access import policy_for
from franta.materialize import WorkspaceMaterializer
materializer = WorkspaceMaterializer(Path.cwd() / "workspaces")
assert materializer.skill_source == Path(sys.argv[1]).resolve() / "franta/skills"
workspace = materializer.create("installed", root_problem="A fixture problem", policy=policy_for("worker", mode="research"), skills=("CAS", "record-progress"))
assert (workspace.path / ".agents/skills/CAS/SKILL.md").read_text()
assert (workspace.path / ".agents/skills/record-progress/references/payload.md").read_text()
assert not (workspace.path / ".agents/skills/CAS/.DS_Store").exists()
'''
            result = subprocess.run([sys.executable, "-I", "-S", "-c", program, str(installed)], cwd=root, text=True, capture_output=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
