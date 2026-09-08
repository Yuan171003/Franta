from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import franta.access as legacy_access
import franta.materialize as legacy_materialization
import franta.search as legacy_search
import franta.skill_runtime as legacy_skills
import franta.transport as legacy_transport
from franta.contracts import agent_access
from franta.execution_gateway import broker, permissions, skills, transport
from franta.prompts import model_config, prompt_for
from franta.read_access import audit, materialization, memory, search, tasks


def _digest(value: object) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


class Block56CompatibilityTests(unittest.TestCase):
    def test_legacy_access_surface_preserves_object_identity(self) -> None:
        expected = {
            "AccessError": memory.AccessError,
            "AccessPolicy": agent_access.AccessPolicy,
            "AuditLog": audit.AuditLog,
            "AuditedMemoryAPI": memory.AuditedMemoryAPI,
            "AuditedTaskAPI": tasks.AuditedTaskAPI,
            "BrokerBinding": broker.BrokerBinding,
            "BrokerClient": broker.BrokerClient,
            "BrokerError": broker.BrokerError,
            "CodexPermissionProfile": permissions.CodexPermissionProfile,
            "InMemoryBackend": memory.InMemoryBackend,
            "MemoryBackend": memory.MemoryBackend,
            "MemoryBroker": broker.MemoryBroker,
            "MemoryRecord": agent_access.MemoryRecord,
            "TaskBackend": tasks.TaskBackend,
            "policy_for": agent_access.policy_for,
            "staging_skills_for_policy": agent_access.staging_skills_for_policy,
        }
        for name, current in expected.items():
            with self.subTest(name=name):
                self.assertIs(getattr(legacy_access, name), current)

    def test_legacy_module_surfaces_preserve_identity_and_patch_seams(self) -> None:
        self.assertIs(legacy_transport, transport)
        self.assertIs(legacy_skills, skills)
        for name in legacy_materialization.__all__:
            with self.subTest(module="materialize", name=name):
                self.assertIs(
                    getattr(legacy_materialization, name),
                    getattr(materialization, name),
                )
        for name in legacy_search.__all__:
            with self.subTest(module="search", name=name):
                self.assertIs(getattr(legacy_search, name), getattr(search, name))

        marker = object()
        with mock.patch("franta.skill_runtime._run_constrained", marker):
            self.assertIs(skills._run_constrained, marker)

    def test_release_policy_prompt_tool_and_permission_goldens(self) -> None:
        policy_specs = [
            ("main", None, None, None),
            ("trimmer", None, None, None),
            ("worker", "research", None, None),
            ("worker", "associate", None, None),
            ("worker", "reformulate", None, None),
            ("worker", "computation", None, None),
            ("worker", "brainstorm", None, None),
            ("worker", "multi-discipline", None, None),
            ("worker", "associate", "D", None),
            ("verifier", None, None, None),
            ("proof-writer", None, None, None),
            ("summarizer", None, None, None),
            ("synthesizer", None, None, "fact"),
            ("synthesizer", None, None, "route"),
            ("synthesizer", None, None, "obligation"),
        ]
        policies = []
        for spec in policy_specs:
            role, mode, lane, review_type = spec
            policy = agent_access.policy_for(
                role,
                mode=mode,
                sprint_lane=lane,
                review_memory_type=review_type,
            )
            policies.append({"spec": spec, "value": policy.as_public_dict()})
        self.assertEqual(
            _digest(policies),
            "cfa69b22cd03ccba1d2a84606f79a6af62c75ab1d7cb9d0d0ef6f0f0d058c76a",
        )

        available = {
            "internal-search",
            "task-search",
            "record-progress",
            "task-writing",
            "CAS",
            "human-guidance",
            "discovery-sprint",
        }
        prompt_specs = [
            ("main", None, None),
            ("trimmer", None, None),
            ("worker", "research", agent_access.policy_for("worker", mode="research")),
            ("worker", "brainstorm", agent_access.policy_for("worker", mode="brainstorm")),
            (
                "worker",
                "associate",
                agent_access.policy_for("worker", mode="associate", sprint_lane="D"),
            ),
            ("proof-writer", None, agent_access.policy_for("proof-writer")),
            ("verifier", None, None),
            ("challenge-verifier", None, None),
            ("main-closure-review", None, None),
            ("synthesizer", None, None),
            ("summarizer", None, None),
        ]
        prompts = [
            {
                "role": role,
                "mode": mode,
                "text": prompt_for(
                    role,
                    root_problem="ROOT TEST",
                    mode=mode,
                    policy=policy,
                    available_skills=available,
                ),
            }
            for role, mode, policy in prompt_specs
        ]
        self.assertEqual(
            _digest(prompts),
            "7533669015070d96cd981bdee4d8a9209d53613491d826e3e61607b3dadde22f",
        )

        tool_names = [
            "internal_search",
            "memory_fetch",
            "fact_dependency_closure",
            "task_summary",
            "task_artifact_fetch",
            "task_writing",
            "record_progress",
            "human_guidance",
            "discovery_sprint",
            "execute_cas",
        ]
        self.assertEqual(
            _digest(
                skills._tool_definitions(
                    tool_names,
                    configured_cas_software=("sage", "M2"),
                )
            ),
            "1ea492ad8d9ddf3d96c91c5b51544d46abe4f118ca8c3e96102f15a7b4b85fcd",
        )

        profile = permissions.CodexPermissionProfile.for_policy(
            agent_access.policy_for("worker", mode="research"),
            canonical_path="/project/canonical.sqlite",
            private_paths=("/project/private", "/project/archive"),
        )
        self.assertEqual(
            _digest(profile.config_overrides()),
            "0a9fc1f043183988d4bbbabda478c23501cb91e0a18839822892c6ef8108582f",
        )

    def test_model_defaults_and_move_sensitive_paths_are_unchanged(self) -> None:
        self.assertEqual(
            (model_config("main").model, model_config("main").reasoning_effort),
            ("gpt-6-astra", "ultra"),
        )
        self.assertEqual(
            (model_config("worker").model, model_config("worker").reasoning_effort),
            ("gpt-6-astra", "max"),
        )
        self.assertEqual(
            (
                model_config("synthesizer").model,
                model_config("synthesizer").reasoning_effort,
            ),
            ("gpt-6-astra", "xhigh"),
        )

        project_root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            materializer = materialization.WorkspaceMaterializer(root / "workspaces")
            gateway = transport.CodexTransport(
                root / "transport",
                host_codex_home=root / "host-codex-home",
            )
            self.assertEqual(materializer.skill_source, project_root / ".agents" / "skills")
            self.assertEqual(gateway.source_root, project_root / "src")

    def test_legacy_skill_runtime_cli_remains_available(self) -> None:
        source_root = Path(__file__).resolve().parents[1] / "src"
        environment = dict(os.environ)
        environment["PYTHONPATH"] = str(source_root)
        result = subprocess.run(
            [sys.executable, "-m", "franta.skill_runtime", "--help"],
            capture_output=True,
            check=False,
            env=environment,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("python3 -m franta.skill_runtime", result.stdout)

    def test_block_dependency_direction_has_no_runtime_back_edge(self) -> None:
        source_root = Path(__file__).resolve().parents[1] / "src"
        environment = dict(os.environ)
        environment["PYTHONPATH"] = str(source_root)
        probes = [
            (
                "import sys; import franta.read_access; "
                "blocked={'franta.execution_gateway','franta.runtime','franta.scheduler'}; "
                "present=sorted(blocked.intersection(sys.modules)); "
                "raise SystemExit('back edge: '+repr(present) if present else 0)"
            ),
            (
                "import sys; import franta.execution_gateway; "
                "blocked={'franta.runtime','franta.scheduler'}; "
                "present=sorted(blocked.intersection(sys.modules)); "
                "raise SystemExit('runtime imports: '+repr(present) if present else 0)"
            ),
        ]
        for probe in probes:
            with self.subTest(probe=probe):
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
