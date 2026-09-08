from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path

from franta.cli import main


class CliTests(unittest.TestCase):
    def test_init_status_and_projection_rebuild(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = root / "bootstrap.toml"
            project = root / "project"
            manifest.write_text(
                "\n".join(
                    [
                        "[project]",
                        'name = "cli-test"',
                        'directory = "project"',
                        'root_problem = "Prove that 1 = 1."',
                        'foundation_policy = "Use ordinary equality axioms."',
                        "",
                        "[context_budgets]",
                        "main = 1000",
                        "",
                        "[initial]",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            output = io.StringIO()
            self.assertEqual(main(["init", str(manifest)], output=output), 0)
            initialized = json.loads(output.getvalue())
            self.assertEqual(initialized["gate"], "trimming")
            self.assertEqual(initialized["memories"]["obligation"], 1)

            output = io.StringIO()
            self.assertEqual(main(["status", str(project)], output=output), 0)
            self.assertEqual(json.loads(output.getvalue())["root"]["obligation_status"], "active")

            output = io.StringIO()
            self.assertEqual(
                main(["rebuild-projections", str(project)], output=output), 0
            )
            rebuilt = json.loads(output.getvalue())
            self.assertGreaterEqual(rebuilt["memory_projections"], 1)

            output = io.StringIO()
            self.assertEqual(main(["refs", str(project)], output=output), 0)
            self.assertEqual(json.loads(output.getvalue()), {"references": []})

    def test_start_zero_cycles_is_a_nonsemantic_process_bound(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = root / "bootstrap.toml"
            manifest.write_text(
                "\n".join(
                    [
                        "[project]",
                        'name = "bounded-test"',
                        'directory = "project"',
                        'root_problem = "Decide P."',
                        'foundation_policy = "No extra axioms."',
                        "",
                        "[context_budgets]",
                        "main = 1000",
                        "",
                        "[initial]",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            output = io.StringIO()
            self.assertEqual(
                main(["start", str(manifest), "--max-cycles", "0"], output=output),
                0,
            )
            self.assertEqual(json.loads(output.getvalue())["gate"], "trimming")


if __name__ == "__main__":
    unittest.main()
