from __future__ import annotations

import tempfile
from pathlib import Path
import unittest

from franta.errors import AccessDenied
from franta.project import ProjectLayout, require_beneath


class ProjectLayoutTests(unittest.TestCase):
    def test_layout_and_safe_workspace_name(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            layout = ProjectLayout.at(raw)
            layout.create()
            workspace = layout.workspace("T-000001", create=True)
            self.assertTrue(workspace.is_dir())
            with self.assertRaises(AccessDenied):
                layout.workspace("../private")

    def test_symlink_escape_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            allowed = root / "allowed"
            outside = root / "outside"
            allowed.mkdir()
            outside.mkdir()
            (allowed / "escape").symlink_to(outside, target_is_directory=True)
            with self.assertRaises(AccessDenied):
                require_beneath(allowed / "escape", allowed)


if __name__ == "__main__":
    unittest.main()

