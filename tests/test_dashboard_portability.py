from __future__ import annotations

import ast
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest


class DashboardPortabilityTests(unittest.TestCase):
    package = Path(__file__).resolve().parents[1] / "src" / "dashboard_system"

    def test_dashboard_has_no_research_host_imports(self) -> None:
        forbidden = {"franta", "explorer_system", "advisor_system"}
        for path in self.package.rglob("*.py"):
            with self.subTest(path=path.relative_to(self.package)):
                tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
                for node in ast.walk(tree):
                    if isinstance(node, ast.Import):
                        modules = [alias.name for alias in node.names]
                    elif isinstance(node, ast.ImportFrom) and node.level == 0:
                        modules = [node.module or ""]
                    else:
                        continue
                    self.assertFalse(forbidden.intersection(name.split(".")[0] for name in modules))

    def test_dashboard_chrome_is_english_and_includes_requested_live_views(self) -> None:
        index = (self.package / "static/index.html").read_text(encoding="utf-8")
        app = (self.package / "static/app.js").read_text(encoding="utf-8")
        style = (self.package / "static/style.css").read_text(encoding="utf-8")
        self.assertEqual(re.findall(r"[\u4e00-\u9fff]", index + app), [])
        self.assertIn('lang="en"', index)
        self.assertIn('id="worker-roster"', index)
        self.assertIn('id="memory-map"', index)
        self.assertIn("renderMemoryGraph", app)
        self.assertIn("memoryNodeColor", app)
        self.assertIn("reheatMemoryGraph", app)
        self.assertIn("Lighter older", index)
        self.assertIn("memory-network-node.route", style)
        self.assertIn("#2f8a6f", style)
        self.assertIn("#4d7fa8", style)
        self.assertIn("#9a7441", style)
        self.assertIn("#80648b", style)

    def test_copied_dashboard_imports_and_loads_assets_without_other_project_packages(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            copied = root / "dashboard_system"
            shutil.copytree(self.package, copied, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            script = """
import importlib.util
import importlib.resources
import pathlib
import sys
sys.path.insert(0, sys.argv[1])
for name in ('franta', 'explorer_system', 'advisor_system'):
    assert importlib.util.find_spec(name) is None, name
import dashboard_system
from dashboard_system.interfaces import DashboardReadPort, OperatorCommandPort, MonitorPort
from dashboard_system.monitor import ResearchMonitor, validate_summary
from dashboard_system.server import DashboardServer
assert pathlib.Path(dashboard_system.__file__).resolve().parent == pathlib.Path(sys.argv[1]) / 'dashboard_system'
assets = importlib.resources.files('dashboard_system').joinpath('static')
for filename in ('index.html', 'app.js', 'style.css', 'vendor/katex.js', 'vendor/katex.min.css'):
    assert assets.joinpath(filename).read_bytes(), filename
assert any(path.name.endswith('.woff2') for path in assets.joinpath('vendor/fonts').iterdir())
assert validate_summary({'directions': []}, set()) == {'directions': []}
class Read:
    def monitor_snapshot(self):
        return {'source_ids': []}
class Model:
    def summarize(self, snapshot):
        raise AssertionError('Portable import must not start a model')
monitor = ResearchMonitor(Read(), Model(), pathlib.Path(sys.argv[1]) / 'cache', auto_start=False)
assert monitor.status()['status'] == 'idle'
monitor.stop()
print('DASHBOARD_PORTABLE')
"""
            result = subprocess.run(
                [sys.executable, "-I", "-S", "-c", script, str(root)],
                cwd=root, text=True, capture_output=True, timeout=20, check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), "DASHBOARD_PORTABLE")


if __name__ == "__main__":
    unittest.main()
