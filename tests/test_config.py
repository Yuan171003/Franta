from __future__ import annotations

import tempfile
from pathlib import Path
import unittest

from franta.config import ConfigurationError, load_manifest


MANIFEST = """
[project]
name = "test-project"
directory = "state"
root_problem = "Prove that 1 + 1 = 2."
foundation_policy = "Ordinary Peano arithmetic."

[models.default]
model = "gpt-6-astra"
reasoning_effort = "ultra"

[models.synthesizer]
model = "gpt-6-astra"
reasoning_effort = "xhigh"

[retries]
worker_interruptions = 2
verifier_transport = 2
synthesizer_transport = 2
summarizer_transport = 2
main_transport = 3
trimmer_transport = 3

[limits]
max_non_verifier_workers = 4
max_parallel_verifiers = 2
max_portfolio_memories = 20
max_search_results = 10
worker_resume_launches = 6
verifier_revision_requests = 2
assignments_per_trim_review = 8
trimmer_rounds_per_session = 3

[tools]
codex = "codex"

[agents]
native_web_search = true
"""


class ManifestTests(unittest.TestCase):
    def write(self, directory: Path, text: str = MANIFEST) -> Path:
        path = directory / "bootstrap.toml"
        path.write_text(text, encoding="utf-8")
        return path

    def test_loads_approved_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            cfg = load_manifest(self.write(Path(raw)))
        self.assertEqual(cfg.default_model.reasoning_effort, "ultra")
        self.assertEqual(cfg.synthesizer_model.reasoning_effort, "xhigh")
        self.assertTrue(cfg.native_web_search)
        self.assertIsNone(cfg.timeouts.agent_call_seconds)
        self.assertEqual(cfg.limits.max_non_verifier_workers, 4)
        self.assertIsNone(cfg.explorer)
        self.assertEqual(len(cfg.manifest_digest), 64)

    def test_explorer_is_opt_in_and_an_empty_table_uses_fixed_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            legacy = load_manifest(self.write(root))
            enabled = load_manifest(
                self.write(root, MANIFEST + "\n[explorer]\n")
            )

        self.assertIsNone(legacy.explorer)
        self.assertIsNotNone(enabled.explorer)
        assert enabled.explorer is not None
        self.assertTrue(enabled.explorer.enabled)
        self.assertEqual(enabled.explorer.max_workers, 4)
        self.assertEqual(enabled.explorer.attempts_per_worker, 3)
        self.assertEqual(enabled.explorer.attempt_seconds, 3 * 60 * 60)
        self.assertEqual(
            enabled.explorer.explorer_admission_seconds, 2 * 60 * 60
        )
        self.assertEqual(enabled.explorer.franta_admission_seconds, 8 * 60 * 60)
        self.assertEqual(enabled.explorer.max_scratch_per_attempt, 256)
        self.assertEqual(enabled.explorer.max_scratch_per_turn, 4096)
        self.assertEqual(enabled.explorer.max_abstract_bytes, 4096)
        self.assertEqual(enabled.explorer.max_content_bytes, 262_144)
        self.assertNotEqual(legacy.manifest_digest, enabled.manifest_digest)

    def test_explorer_defaults_are_digest_stable_and_explicitly_identified(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            empty_table = load_manifest(
                self.write(root, MANIFEST + "\n[explorer]\n")
            )
            explicit_defaults = load_manifest(
                self.write(
                    root,
                    MANIFEST
                    + """

[explorer]
enabled = true
max_workers = 4
attempts_per_worker = 3
attempt_seconds = 10800
explorer_admission_seconds = 7200
franta_admission_seconds = 28800
max_scratch_per_attempt = 256
max_scratch_per_turn = 4096
max_abstract_bytes = 4096
max_content_bytes = 262144
""",
                )
            )

        self.assertEqual(empty_table.explorer, explicit_defaults.explorer)
        self.assertEqual(
            empty_table.manifest_digest, explicit_defaults.manifest_digest
        )

    def test_explorer_byte_limits_may_tighten_but_not_expand_skill_contract(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            configured = load_manifest(
                self.write(
                    root,
                    MANIFEST
                    + """

[explorer]
max_abstract_bytes = 17
max_content_bytes = 257
""",
                )
            )
            assert configured.explorer is not None
            self.assertEqual(configured.explorer.max_abstract_bytes, 17)
            self.assertEqual(configured.explorer.max_content_bytes, 257)

            for field, value, message in (
                (
                    "max_abstract_bytes",
                    4097,
                    "cannot exceed the fixed 4096-byte skill contract",
                ),
                (
                    "max_content_bytes",
                    262145,
                    "cannot exceed the fixed 262144-byte skill contract",
                ),
            ):
                with self.subTest(field=field):
                    invalid = MANIFEST + f"\n[explorer]\n{field} = {value}\n"
                    with self.assertRaisesRegex(ConfigurationError, message):
                        load_manifest(self.write(root, invalid))

    def test_explorer_rejects_values_that_break_scheduler_invariants(self) -> None:
        cases = (
            ("enabled = false", "explorer.enabled must be true"),
            ("max_workers = 0", "max_workers must be between 1 and 4"),
            ("max_workers = true", "max_workers must be between 1 and 4"),
            ("attempts_per_worker = 2", "attempts_per_worker must remain 3"),
            ("attempt_seconds = 0", "attempt_seconds must be a positive integer"),
            (
                "max_scratch_per_attempt = 3\nmax_scratch_per_turn = 2",
                "max_scratch_per_turn must be at least max_scratch_per_attempt",
            ),
            ("unknown_setting = 1", "unknown ExplorerSettings keys"),
        )
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            for body, message in cases:
                with self.subTest(body=body):
                    with self.assertRaisesRegex(ConfigurationError, message):
                        load_manifest(
                            self.write(root, MANIFEST + f"\n[explorer]\n{body}\n")
                        )

    def test_explorer_worker_limit_cannot_exceed_the_legacy_worker_limit(self) -> None:
        text = MANIFEST.replace(
            "max_non_verifier_workers = 4", "max_non_verifier_workers = 2"
        )
        with tempfile.TemporaryDirectory() as raw:
            with self.assertRaisesRegex(
                ConfigurationError,
                "explorer.max_workers cannot exceed limits.max_non_verifier_workers",
            ):
                load_manifest(
                    self.write(Path(raw), text + "\n[explorer]\nmax_workers = 3\n")
                )

    def test_rejects_more_than_four_workers(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            text = MANIFEST.replace("max_non_verifier_workers = 4", "max_non_verifier_workers = 5")
            with self.assertRaises(ConfigurationError):
                load_manifest(self.write(Path(raw), text))

    def test_web_search_cannot_be_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            text = MANIFEST.replace("native_web_search = true", "native_web_search = false")
            with self.assertRaises(ConfigurationError):
                load_manifest(self.write(Path(raw), text))

    def test_optional_agent_watchdog_must_be_positive(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            configured = MANIFEST + "\n[timeouts]\nagent_call_seconds = 900\n"
            cfg = load_manifest(self.write(Path(raw), configured))
            self.assertEqual(cfg.timeouts.agent_call_seconds, 900)

            invalid = MANIFEST + "\n[timeouts]\nagent_call_seconds = 0\n"
            with self.assertRaises(ConfigurationError):
                load_manifest(self.write(Path(raw), invalid))


if __name__ == "__main__":
    unittest.main()
