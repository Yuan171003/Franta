from __future__ import annotations

from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from franta.contracts import responses as franta_responses
from franta.contracts.agent_access import policy_for
from franta.explorer_adapter import FRANTA_SORT_TOOLS, explorer_agent_call_spec
import franta.prompts as franta_prompts
from explorer_system.agents import (
    EXPLORER_GUIDANCE_VARIANTS,
    RESPONSE_SCHEMAS,
    build_launch_spec,
    explorer_worker_prompt,
    main_sort_prompt,
    select_guidance_variant,
    write_response_schemas,
)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class PortableExplorerAgentBoundaryTests(unittest.TestCase):
    def test_portable_prompt_contract_has_no_required_direction_record(self) -> None:
        self.assertEqual(
            _sha256(
                explorer_worker_prompt(
                    "ROOT TEST", mode="clean-room", host_agent_name="Franta"
                )
            ),
            "1b3cf002611cbec77973a65b53f6ea961dd6d6d440d19ad090f4f6f0a0dbe1b2",
        )
        self.assertEqual(
            _sha256(
                explorer_worker_prompt(
                    "ROOT TEST", mode="explore", host_agent_name="Franta"
                )
            ),
            "af060407264a38c5d5b5bf9f2c357258d2a514618de0756b2d3e362b30c00e5d",
        )
        self.assertEqual(
            _sha256(
                main_sort_prompt(
                    "ROOT TEST",
                    host_agent_name="Franta",
                    host_tools=FRANTA_SORT_TOOLS,
                )
            ),
            "37fa2e3a96a09291621fb8f084479c5def4d52f5e4ea3ec8d23dd9b4f6f25bd2",
        )

    def test_launch_provider_owns_prompt_model_and_schema_selection(self) -> None:
        goal_guard = (
            'Here, "Your single goal" describes only this Explorer assignment. '
            "Do not use Codex thread goals or call `create_goal`, `get_goal`, or "
            "`update_goal`."
        )
        clean = build_launch_spec(
            "explorer_worker",
            root_problem="ROOT",
            mode="clean_room",
        )
        self.assertEqual(clean.role, "explorer-worker")
        self.assertEqual(clean.schema_name, "explorer-worker")
        self.assertEqual(clean.model_route.model, "gpt-6-astra")
        self.assertEqual(clean.model_route.reasoning_effort, "max")
        self.assertIn("strict clean-room attack", clean.prompt)
        self.assertIn("Your single goal is to attack ROOT:", clean.prompt)
        self.assertIn(goal_guard, " ".join(clean.prompt.split()))

        variants = {
            "check_result": "check-result-promising",
            "portfolio": "portfolio-synthesize",
            "full_memory": "full-memory-adaptive",
        }
        for mode, variant in variants.items():
            with self.subTest(mode=mode):
                current = build_launch_spec(
                    "explorer_worker",
                    root_problem="ROOT",
                    mode=mode,
                    guidance_variant=variant,
                )
                self.assertEqual(current.schema_name, "explorer-worker-v2")
                self.assertEqual(current.model_route.reasoning_effort, "max")
                self.assertNotIn("record_kind=direction", current.prompt)
                self.assertNotIn("ongoing_direction", current.prompt)
                self.assertIn("directions tried", current.prompt)
                self.assertIn("Your single goal is to attack ROOT:", current.prompt)
                self.assertIn(goal_guard, " ".join(current.prompt.split()))

        sort = build_launch_spec("main_sort", root_problem="ROOT")
        self.assertEqual(sort.schema_name, "main-sort")
        self.assertEqual(sort.model_route.model, "gpt-6-astra")
        self.assertEqual(sort.model_route.reasoning_effort, "ultra")
        self.assertIn("host collaborator main-sort agent", sort.prompt)
        self.assertIn("input/explorer_snapshot/", sort.prompt)
        self.assertIn("`rg`", sort.prompt)
        self.assertNotIn("explorer-search", sort.prompt)
        self.assertNotIn("Franta", sort.prompt)
        self.assertNotIn("Codex thread goals", sort.prompt)
        with self.assertRaisesRegex(ValueError, "requires a mode"):
            build_launch_spec("explorer-worker", root_problem="ROOT")
        with self.assertRaisesRegex(ValueError, "not owned"):
            build_launch_spec("worker", root_problem="ROOT", mode="research")

    def test_guidance_variants_are_explicit_deterministic_and_interpolated(self) -> None:
        for mode, variants in EXPLORER_GUIDANCE_VARIANTS.items():
            selected = select_guidance_variant(mode, entropy="a" * 64)
            self.assertIn(selected, variants)
            self.assertEqual(
                select_guidance_variant(mode, entropy="a" * 64),
                selected,
            )

        prompt = explorer_worker_prompt(
            "ROOT",
            mode="check-result",
            guidance_variant="check-result-uncommon",
            host_agent_name="AnotherHost",
        )
        self.assertIn("No other AnotherHost or Explorer memory", prompt)
        self.assertIn("deliberately choose a research direction that is uncommon", prompt)
        self.assertNotIn("{host}", prompt)
        self.assertNotIn("{think_guidance}", prompt)

        unconstrained = explorer_worker_prompt(
            "ROOT",
            mode="portfolio",
            guidance_variant="portfolio-unconstrained",
        )
        self.assertNotIn("()", unconstrained)
        self.assertNotIn("{think_guidance}", unconstrained)
        self.assertIn("fetch.\n\nThink freely", unconstrained)

        with self.assertRaisesRegex(ValueError, "guidance_variant"):
            explorer_worker_prompt(
                "ROOT",
                mode="portfolio",
                guidance_variant="check-result-promising",
            )
        with self.assertRaisesRegex(ValueError, "do not accept"):
            explorer_worker_prompt(
                "ROOT",
                mode="clean-room",
                guidance_variant="check-result-promising",
            )

    def test_host_name_is_an_explicit_portability_parameter(self) -> None:
        prompt = main_sort_prompt("ROOT", host_agent_name="AnotherHost")
        normalized = " ".join(prompt.split())
        self.assertIn("AnotherHost main-sort agent", normalized)
        self.assertIn("existing AnotherHost memory", normalized)
        self.assertIn("ordinary AnotherHost assignment planning", normalized)
        self.assertNotIn("Franta", prompt)
        worker = explorer_worker_prompt(
            "ROOT",
            mode="explore",
            host_agent_name="AnotherHost",
        )
        self.assertIn("among AnotherHost records", worker)
        self.assertIn("delete AnotherHost memory", worker)
        self.assertNotIn("Franta", worker)

    def test_portable_schema_writer_is_self_contained_and_private(self) -> None:
        self.assertEqual(
            set(RESPONSE_SCHEMAS),
            {
                "explorer-worker",
                "explorer-worker-v2",
                "main-sort",
            },
        )
        self.assertNotIn(
            "ongoing_direction_scratch_id",
            RESPONSE_SCHEMAS["explorer-worker-v2"]["properties"],
        )
        self.assertNotIn(
            "ongoing_direction_scratch_id",
            RESPONSE_SCHEMAS["explorer-worker"]["properties"],
        )
        self.assertEqual(
            _sha256(
                json.dumps(
                    RESPONSE_SCHEMAS,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
            ),
            "1595c467e760b18020790a560d53ce8edc3d28ec551dfe77d0feaad6b5c438b8",
        )
        with tempfile.TemporaryDirectory() as raw:
            written = write_response_schemas(raw)
            self.assertEqual(set(written), set(RESPONSE_SCHEMAS))
            for name, path in written.items():
                self.assertEqual(json.loads(path.read_text()), RESPONSE_SCHEMAS[name])
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_franta_prompt_and_schema_modules_have_no_explorer_dispatch(self) -> None:
        prompt_source = Path(franta_prompts.__file__).read_text(encoding="utf-8")
        response_source = Path(franta_responses.__file__).read_text(encoding="utf-8")
        for marker in ("Explorer", "explorer-worker", "main-sort"):
            self.assertNotIn(marker, prompt_source)
        self.assertNotIn("EXPLORER_SCHEMAS", response_source)
        self.assertNotIn("write_explorer_schemas", response_source)
        self.assertFalse({"explorer-worker", "main-sort"} & set(franta_responses.SCHEMAS))
        with self.assertRaisesRegex(ValueError, "unknown prompt role"):
            franta_prompts.prompt_for(
                "explorer-worker",
                root_problem="ROOT",
                mode="clean-room",
                policy=policy_for("explorer-worker", mode="clean-room"),
            )

    def test_franta_adapter_only_converts_the_portable_spec(self) -> None:
        policy = policy_for("explorer-worker", mode="check-result")
        adapted = explorer_agent_call_spec(
            "explorer-worker",
            root_problem="ROOT",
            mode="check-result",
            guidance_variant="check-result-uncommon",
            policy=policy,
            input_path="input/context.json",
        )
        portable = build_launch_spec(
            "explorer-worker",
            root_problem="ROOT",
            mode="check-result",
            guidance_variant="check-result-uncommon",
            input_path="input/context.json",
            host_agent_name="Franta",
            host_sort_tools=FRANTA_SORT_TOOLS,
        )
        self.assertEqual(adapted.prompt, portable.prompt)
        self.assertEqual(adapted.schema_name, portable.schema_name)
        self.assertEqual(adapted.model_config.model, portable.model_route.model)
        self.assertEqual(
            adapted.model_config.reasoning_effort,
            portable.model_route.reasoning_effort,
        )

    def test_pre_variant_v2_call_uses_stable_grant_digest_fallback(self) -> None:
        policy = replace(
            policy_for("explorer-worker", mode="check-result"),
            explorer_access_grant_id="XAG-legacy-v2",
            explorer_access_grant_digest="c" * 64,
        )
        expected_variant = select_guidance_variant(
            "check-result", entropy="c" * 64
        )
        first = explorer_agent_call_spec(
            "explorer-worker",
            root_problem="ROOT",
            mode="check-result",
            policy=policy,
            input_path="input/context.json",
        )
        second = explorer_agent_call_spec(
            "explorer-worker",
            root_problem="ROOT",
            mode="check-result",
            policy=policy,
            input_path="input/context.json",
        )
        self.assertEqual(first.prompt, second.prompt)
        self.assertEqual(
            first.prompt,
            explorer_worker_prompt(
                "ROOT",
                mode="check-result",
                guidance_variant=expected_variant,
                input_path="input/context.json",
                host_agent_name="Franta",
            ),
        )

    def test_portable_agent_module_imports_no_franta_code(self) -> None:
        source_root = Path(__file__).resolve().parents[1] / "src"
        environment = dict(os.environ)
        environment["PYTHONPATH"] = str(source_root)
        probe = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "import sys; import explorer_system.agents; "
                    "present=sorted(name for name in sys.modules "
                    "if name == 'franta' or name.startswith('franta.')); "
                    "raise SystemExit('host imports: '+repr(present) if present else 0)"
                ),
            ],
            capture_output=True,
            check=False,
            env=environment,
            text=True,
        )
        self.assertEqual(probe.returncode, 0, probe.stderr)


if __name__ == "__main__":
    unittest.main()
