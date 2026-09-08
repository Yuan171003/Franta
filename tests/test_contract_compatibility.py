from __future__ import annotations

import hashlib
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest

import franta.access as legacy_access
import franta.config as legacy_config
import franta.errors as legacy_failures
import franta.models as legacy_canonical
import franta.output_schemas as legacy_responses
import franta.references as legacy_references
import franta.workflows as legacy_workflows
from franta import contracts
from franta.contracts import agent_access, canonical, configuration, failures, references
from franta.contracts import responses, workflows
from franta.scheduler import IdempotencyConflict as SchedulerIdempotencyConflict


SCHEMA_HASHES = {
    "main": "f3faa1882dc6d58225844727e905548b71d8f1815c6744e4b64ed615961c7097",
    "trimmer-review": "d40ca4118b3e0667ff6f8249de52e839896dcf490bb2d45033bacf8c15411b67",
    "trimmer": "b48cb0ef9b6734a8e090b40751530e3549dc8dc24dc32e3bc1bcf2515fb33bd8",
    "worker": "0c76619a72dfd7e15c6f2d903de5d763fc3cfeb3ee3b35ac9c998e79509a7483",
    "synthesizer": "2b05f7172eb627d79d46acbe37f14be9551131b5329c650b8768f0d3d8ea69fe",
    "verifier": "3b10c64313a757148ef143bab8f30d0bc7de52d4f09a0c79ac3fa13aff725f87",
    "challenge-verifier": "96bc641a3a8e9372beecdebab401e384c2c4b60e870aef88f44a7263a7128843",
    "main-closure-review": "f55cac2eb2a0ff42d01308677155e6bf51e1b5431b564faf0533b2cede0d861a",
    "summarizer": "a067a9d8f9f524739b923b231bed048e3c5be8a984f4e12da85f5d307ed31607",
}


class ContractCompatibilityTests(unittest.TestCase):
    def assert_aliases(self, legacy: object, current: object, names: list[str]) -> None:
        for name in names:
            with self.subTest(name=name):
                self.assertIs(getattr(legacy, name), getattr(current, name))

    def test_legacy_facades_preserve_contract_object_identity(self) -> None:
        self.assert_aliases(legacy_canonical, canonical, legacy_canonical.__all__)
        self.assert_aliases(legacy_failures, failures, legacy_failures.__all__)
        self.assert_aliases(legacy_references, references, legacy_references.__all__)
        self.assert_aliases(legacy_workflows, workflows, legacy_workflows.__all__)
        self.assert_aliases(legacy_responses, responses, legacy_responses.__all__)

        config_names = [
            "BootstrapManifest",
            "DEFAULT_MODEL",
            "DEFAULT_REASONING",
            "InitialMaterial",
            "Limits",
            "ModelSettings",
            "RetrySettings",
            "SYNTH_REASONING",
            "TimeoutSettings",
            "ToolSettings",
        ]
        self.assert_aliases(legacy_config, configuration, config_names)

        access_names = [
            "AccessPolicy",
            "CAS_TOOL_ARGUMENTS",
            "ISOLATED_MODES",
            "MEMORY_TYPES",
            "MemoryRecord",
            "ORDINARY_SEARCH_MODES",
            "SEARCHABLE_MEMORY_TYPES",
            "SPRINT_LANES",
            "STAGING_SKILL_BY_TOOL",
            "STAGING_TOOL_BY_SKILL",
            "policy_for",
            "staging_skills_for_policy",
        ]
        self.assert_aliases(legacy_access, agent_access, access_names)

    def test_ambiguous_legacy_types_remain_distinct(self) -> None:
        self.assertIsNot(canonical.AccessPolicy, agent_access.AccessPolicy)
        self.assertIsNot(canonical.MemoryRecord, agent_access.MemoryRecord)
        self.assertIsNot(canonical.ValidationError, failures.ValidationError)
        self.assertIsNot(canonical.ConflictError, failures.ConflictError)
        self.assertIsNot(canonical.IdempotencyConflict, SchedulerIdempotencyConflict)

        self.assertIs(contracts.CanonicalAccessPolicy, canonical.AccessPolicy)
        self.assertIs(contracts.AgentAccessPolicy, agent_access.AccessPolicy)
        self.assertIs(contracts.CanonicalMemoryRecord, canonical.MemoryRecord)
        self.assertIs(contracts.AgentMemoryRecord, agent_access.MemoryRecord)
        self.assertIs(contracts.CanonicalValidationError, canonical.ValidationError)
        self.assertIs(contracts.ArtifactValidationError, failures.ValidationError)

    def test_fixed_memory_and_operation_terminology_is_unchanged(self) -> None:
        self.assertEqual(
            [item.value for item in canonical.MemoryType],
            ["fact", "route", "memo", "claim", "obligation", "task", "computation"],
        )
        self.assertEqual(
            [item.value for item in canonical.OperationType],
            [
                "fact",
                "route_update",
                "route_add",
                "memo",
                "claim_add",
                "claim_remove",
                "obligation_add",
                "obligation_update",
                "obligation_remove",
            ],
        )
        self.assertEqual(
            {kind.value: prefix for kind, prefix in canonical.MEMORY_PREFIXES.items()},
            {
                "fact": "F",
                "route": "R",
                "memo": "M",
                "claim": "CL",
                "obligation": "O",
                "task": "T",
                "computation": "C",
            },
        )
        self.assertEqual(canonical.CATEGORY_PREFIX, "CAT")
        self.assertEqual(
            agent_access.MEMORY_TYPES,
            frozenset(item.value for item in canonical.MemoryType),
        )
        self.assertEqual(
            workflows.MEMORY_OPERATION_KINDS,
            frozenset(item.value for item in canonical.OperationType),
        )
        self.assertEqual(agent_access.ISOLATED_MODES, workflows.ISOLATED_MODES)

    def test_response_wire_contracts_match_pre_extraction_hashes(self) -> None:
        self.assertEqual(set(responses.SCHEMAS), set(SCHEMA_HASHES))
        with tempfile.TemporaryDirectory() as raw:
            written = responses.write_schemas(raw)
            for name, expected in SCHEMA_HASHES.items():
                with self.subTest(schema=name):
                    path = written[name]
                    data = path.read_bytes()
                    self.assertTrue(data.endswith(b"\n"))
                    self.assertEqual(hashlib.sha256(data).hexdigest(), expected)
                    self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_manifest_digest_matches_release_contract(self) -> None:
        manifest = configuration.BootstrapManifest(
            source=Path("/tmp/bootstrap.toml"),
            project_name="p",
            project_dir=Path("/tmp/state"),
            root_problem="R",
            foundation_policy="F",
            context_budgets={"main": 100},
            default_model=configuration.ModelSettings(),
            synthesizer_model=configuration.ModelSettings(reasoning_effort="xhigh"),
            retries=configuration.RetrySettings(),
            timeouts=configuration.TimeoutSettings(),
            limits=configuration.Limits(),
            tools=configuration.ToolSettings(),
            initial=configuration.InitialMaterial(),
        )
        self.assertEqual(
            manifest.manifest_digest,
            "d992bd28b991b5c8556d68a9a5baeb842d767ce8e45ef66e96c939a5150aebe5",
        )

    def test_contract_package_is_a_leaf_of_stateful_blocks(self) -> None:
        source_root = Path(__file__).resolve().parents[1] / "src"
        environment = dict(os.environ)
        environment["PYTHONPATH"] = str(source_root)
        probe = (
            "import sys; import franta.contracts; "
            "blocked={'franta.store','franta.scheduler','franta.runtime','franta.access'}; "
            "present=sorted(blocked.intersection(sys.modules)); "
            "raise SystemExit('stateful imports: '+repr(present) if present else 0)"
        )
        result = subprocess.run(
            [sys.executable, "-c", probe],
            capture_output=True,
            check=False,
            env=environment,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)


if __name__ == "__main__":
    unittest.main()
