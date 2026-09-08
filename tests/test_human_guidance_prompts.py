from __future__ import annotations

import hashlib
from pathlib import Path
import sys
import unittest

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from franta.contracts.agent_access import policy_for
from franta.prompts import MODE_GUIDANCE, prompt_for, with_human_guidance
from explorer_system.agents import (
    EXPLORER_GUIDANCE_VARIANTS,
    build_launch_spec,
    explorer_worker_prompt,
    main_sort_prompt,
)


# Reviewed Franta release snapshots. The prompt builders retain the supplied
# research semantics; branding and previously stale snapshots are refreshed.
UNGUIDED_SHA256 = {
    "franta:main": "f274e169aa600e3dd743d3d9a6350186051ba162f6541db664d103f3845e6645",
    "franta:worker:research": "7963d8756c51d74af62dbcd23ff1cd678831cdb8c01dbf8929b01daf42830746",
    "franta:worker:brainstorm": "37cd40fe0caeef01f8704744fcfb48cd0ec90a027e9547f3aa68e9d6eb5ca399",
    "franta:worker:associate": "365dbfe26f822262c03804e92203a5d721d772f521b83a644c84557c66e082f6",
    "franta:worker:multi-discipline": "35a376d1e597b69d8cf65138dfdd6020e3f8a941950b2c7e651d464b161876f5",
    "franta:worker:reformulate": "3a163679181fb93913c94ac567cf9e054f1035359ed2c00c317a419020edfa43",
    "franta:worker:computation": "1cd25f7f407800323a33b074b4b827da8e48d73a1cc213dc6d30f8c827a64fde",
    "franta:worker:proof-writer": "3fa478013e0316883edeadae4df23a98e0bf9f6f15cfba47192a294e9c3b7f74",
    "franta:trimmer": "2938455e23f6e81e8447d9f875874bfb04ceb5fcbbe3502944f98d44ffdaa6fa",
    "franta:synthesizer": "589ff3ec5b67fca8620cc1d79c31e31d94fd2fe460ec94e89e203375c4f4a3d5",
    "franta:verifier": "260fec63f2ebb914c2ace9895b986d19d358d86b532da872defa951a1972ab25",
    "franta:challenge-verifier": "882a26911767b915001949362f6daa6e52c5ce7627211e7a7906c4286935429f",
    "franta:main-closure-review": "0504ea597ad7f31b570b401d7e11b6f3ca0d7ed2326ce0c741bc8e19c9338e33",
    "franta:summarizer": "b94b490b20fbafbdd98797332c505c75b0e27a1985d5d2e9a694190a11dba820",
    "explorer:check-result:check-result-promising": "dc1ab8f9f64c79f8072c0e724bfa422f86934a48ad3cbdde9671ec7fa7cde39e",
    "explorer:check-result:check-result-uncommon": "7d8f663a20220bebcba5355fc3f52115da9da262dc4a9059a79d9ce61ff32eec",
    "explorer:portfolio:portfolio-synthesize": "b3f60eedcc44ba36c042d8c81b3ebe02545fd0d2b9a620969466ccc0828b2509",
    "explorer:portfolio:portfolio-select-best": "fc54bdb63d52d9f37ca3b463b9e8d98d5642d26140af1c4130f78a1305d65422",
    "explorer:portfolio:portfolio-diversify": "24bc36014d1e0e90bcf3939344849a48c9ef79a6e575ae8e531f1623723afdbb",
    "explorer:portfolio:portfolio-unconstrained": "52793ba54455c4158c475336220e02c4673ea6a11d6be35e0ac057d9f4b360cc",
    "explorer:full-memory:full-memory-adaptive": "7218a28831060c6f915c5ed0e43d85274a9cae7bca9e6bb5e94cefdf7ffbdcbf",
    "explorer:clean-room": "1b3cf002611cbec77973a65b53f6ea961dd6d6d440d19ad090f4f6f0a0dbe1b2",
    "explorer:explore": "af060407264a38c5d5b5bf9f2c357258d2a514618de0756b2d3e362b30c00e5d",
    "explorer:main-sort": "fc5fe1901a32f437de62602297197be15f369e869a2ad113230aa0f29960204e",
}


class HumanGuidancePromptTests(unittest.TestCase):
    def test_all_unguided_prompts_match_release_hashes(self) -> None:
        prompts = {}
        for role in (
            "main", "trimmer", "synthesizer", "verifier",
            "challenge-verifier", "main-closure-review", "summarizer",
        ):
            prompts[f"franta:{role}"] = prompt_for(role, root_problem="ROOT TEST")
        for mode in MODE_GUIDANCE:
            prompts[f"franta:worker:{mode}"] = prompt_for(
                "worker", root_problem="ROOT TEST", mode=mode,
                policy=policy_for("worker", mode=mode),
            )
        for mode, variants in EXPLORER_GUIDANCE_VARIANTS.items():
            for variant in variants:
                prompts[f"explorer:{mode}:{variant}"] = explorer_worker_prompt(
                    "ROOT TEST", mode=mode, guidance_variant=variant,
                    host_agent_name="Franta",
                )
        for mode in ("clean-room", "explore"):
            prompts[f"explorer:{mode}"] = explorer_worker_prompt(
                "ROOT TEST", mode=mode, host_agent_name="Franta",
            )
        prompts["explorer:main-sort"] = main_sort_prompt(
            "ROOT TEST", host_agent_name="Franta",
        )
        self.assertEqual(set(prompts), set(UNGUIDED_SHA256))
        for key, prompt in prompts.items():
            with self.subTest(prompt=key):
                self.assertEqual(
                    hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
                    UNGUIDED_SHA256[key],
                )

    def test_explorer_guidance_replaces_independent_direction_only_when_supplied(self) -> None:
        text = "  先研究特殊纤维。\n\nTry the degeneration {X_t} -> X_0.\n"
        for mode, variant in (
            ("check-result", "check-result-promising"),
            ("check-result", "check-result-uncommon"),
            ("clean-room", None),
        ):
            with self.subTest(mode=mode, variant=variant):
                args = dict(root_problem="ROOT TEST", mode=mode, guidance_variant=variant)
                original = build_launch_spec("explorer-worker", **args)
                explicit_none = build_launch_spec(
                    "explorer-worker", **args, human_guidance=None,
                )
                self.assertEqual(original, explicit_none)
                guided = build_launch_spec(
                    "explorer-worker", **args, human_guidance=text,
                )
                self.assertEqual(guided.model_route, original.model_route)
                self.assertEqual(guided.schema_name, original.schema_name)
                self.assertNotIn("Think freely", guided.prompt)
                self.assertNotIn("receive only ROOT", guided.prompt)
                self.assertNotIn("attack it independently", guided.prompt)
                self.assertIn("strictly follow human guidance", guided.prompt)
                self.assertIn("binding research direction", guided.prompt)
                self.assertIn("Mathematical claims in the guidance remain unproved", guided.prompt)
                self.assertIn("do not silently pivot", guided.prompt)
                self.assertIn("concrete obstacles", guided.prompt)
                self.assertIn("normal attempt-final summary", guided.prompt)
                self.assertIn("`record-scratch`", guided.prompt)
                if mode == "check-result":
                    self.assertIn("`check-result`. Use it *only*", guided.prompt)
                self.assertTrue(guided.prompt.endswith(
                    "BEGIN HUMAN GUIDANCE\n" + text + "\nEND HUMAN GUIDANCE"
                ))
                self.assertEqual(original, build_launch_spec("explorer-worker", **args))

    def test_explorer_later_attempts_and_main_sort_reject_guidance(self) -> None:
        for mode in ("portfolio", "full-memory", "explore"):
            variants = EXPLORER_GUIDANCE_VARIANTS.get(mode, (None,))
            for variant in variants:
                with self.subTest(mode=mode, variant=variant):
                    with self.assertRaisesRegex(ValueError, "only for Explorer attempt 1"):
                        build_launch_spec(
                            "explorer-worker", root_problem="ROOT TEST", mode=mode,
                            guidance_variant=variant, human_guidance="Try induction.",
                        )
        with self.assertRaisesRegex(ValueError, "main-sort does not accept"):
            build_launch_spec(
                "main-sort", root_problem="ROOT TEST", human_guidance="Try induction.",
            )

    def test_explorer_empty_guidance_is_rejected(self) -> None:
        for text in ("", " \n\t", 1, {}):
            with self.subTest(text=text):
                with self.assertRaisesRegex(ValueError, "nonempty text"):
                    explorer_worker_prompt(
                        "ROOT", mode="check-result",
                        guidance_variant="check-result-promising", human_guidance=text,
                    )

    def test_main_guidance_requires_first_self_contained_assignment(self) -> None:
        original = prompt_for("main", root_problem="ROOT TEST")
        text = "\n  用退化方法。\nStudy X_{t=0} first.  \n"
        guided = with_human_guidance(
            original, role="main", guidance_id="HG-0001", text=text,
        )
        self.assertTrue(guided.startswith(original + "\n\n"))
        self.assertIn("return an `assignments` decision", guided)
        self.assertIn("FIRST assignment", guided)
        self.assertIn("strictly follow human guidance", guided)
        self.assertIn("verbatim", guided)
        self.assertIn("`objective`", guided)
        self.assertIn("self-contained mathematical task", guided)
        self.assertIn("only to this batch", guided)
        self.assertIn("do not repeat this guidance in future batches", guided)
        self.assertTrue(guided.endswith(
            "BEGIN HUMAN GUIDANCE HG-0001\n" + text + "\nEND HUMAN GUIDANCE HG-0001"
        ))
        self.assertEqual(original, prompt_for("main", root_problem="ROOT TEST"))

    def test_worker_guidance_scopes_binding_direction_and_keeps_evidence_rules(self) -> None:
        for mode in MODE_GUIDANCE:
            with self.subTest(mode=mode):
                original = prompt_for(
                    "worker", root_problem="ROOT TEST", mode=mode,
                    policy=policy_for("worker", mode=mode),
                )
                guided = with_human_guidance(
                    original, role="worker", guidance_id="HG-0002", text="Try induction.\n",
                )
                self.assertTrue(guided.startswith(original + "\n\n"))
                self.assertIn("strictly follow human guidance", guided)
                self.assertIn("overrides earlier instructions", guided)
                self.assertIn("do not silently pivot", guided)
                self.assertIn("do not carry it into a later assignment", guided)
                self.assertIn("Mathematical claims in the guidance remain unproved", guided)
                self.assertIn("requirements remain unchanged", guided)
                self.assertIn("normal final summary", guided)

    def test_franta_helper_rejects_unsupported_roles_and_invalid_guidance(self) -> None:
        for role in ("advisor", "trimmer", "verifier", "main-sort", "main-agent"):
            with self.subTest(role=role):
                with self.assertRaisesRegex(ValueError, "only main and worker"):
                    with_human_guidance("prompt", role=role, guidance_id="HG-1", text="Try induction.")
        for text in ("", " \n\t", None, 3):
            with self.subTest(text=text):
                with self.assertRaisesRegex(ValueError, "nonempty text"):
                    with_human_guidance("prompt", role="main", guidance_id="HG-1", text=text)
        for guidance_id in ("", " HG-1", "HG-1\nInjected", "HG-1\rInjected", None):
            with self.subTest(guidance_id=guidance_id):
                with self.assertRaisesRegex(ValueError, "guidance_id"):
                    with_human_guidance("prompt", role="worker", guidance_id=guidance_id, text="Try induction.")


if __name__ == "__main__":
    unittest.main()
