from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from franta.access import (  # noqa: E402
    AccessError,
    AuditLog,
    AuditedMemoryAPI,
    BrokerBinding,
    BrokerClient,
    BrokerError,
    CodexPermissionProfile,
    InMemoryBackend,
    MemoryBroker,
    MemoryRecord,
    policy_for,
)
from franta.materialize import (  # noqa: E402
    MaterializationError,
    MemorySnapshot,
    WorkspaceMaterializer,
    validate_explorer_snapshot,
)
from franta.prompts import (  # noqa: E402
    ModelConfig,
    challenge_verifier_prompt,
    closure_review_prompt,
    main_prompt,
    model_config,
    sprint_summarizer_prompt,
    synthesizer_prompt,
    trimmer_prompt,
    verifier_prompt,
    worker_prompt,
)
from franta.skill_runtime import (  # noqa: E402
    SkillContext,
    SkillRuntime,
    SkillRuntimeError,
    allowed_skills,
    execute_cas,
)
from franta.runtime import FrantaRuntime, InvalidAgentOutput  # noqa: E402
from franta.transport import (  # noqa: E402
    CodexRequest,
    CodexTransport,
    CodexTransportError,
    ThreadLedger,
    parse_jsonl_events,
)


ROOT_PROBLEM = "Prove that every object X satisfying H has property P."


def records() -> list[MemoryRecord]:
    return [
        MemoryRecord(
            "F-1",
            "fact",
            "Active Hodge decomposition lemma for X",
            "Full proof of the Hodge decomposition lemma.",
        ),
        MemoryRecord(
            "F-2",
            "fact",
            "Inactive Hodge degeneration claim",
            "Historical proof.",
            active=False,
        ),
        MemoryRecord(
            "F-3",
            "fact",
            "Root solution using the Hodge lemma",
            "Proof of ROOT from F-1.",
            predecessor_ids=("F-1",),
        ),
        MemoryRecord(
            "F-4",
            "fact",
            "Unrelated active fact",
            "Unrelated proof.",
        ),
        MemoryRecord(
            "R-1",
            "route",
            "Hodge-theoretic route for X",
            "Try a period map.",
            revision=2,
        ),
        MemoryRecord(
            "CL-1",
            "claim",
            "Withdrawn Hodge claim",
            "Unverified content.",
            withdrawn=True,
        ),
        MemoryRecord(
            "O-1",
            "obligation",
            "Self-contained intermediate Hodge obligation",
            "Precise statement.",
        ),
    ]


class ModelPromptTests(unittest.TestCase):
    def test_fixed_model_configuration(self) -> None:
        self.assertEqual(model_config("main").model, "gpt-6-astra")
        self.assertEqual(model_config("main").reasoning_effort, "ultra")
        self.assertEqual(model_config("trimmer").reasoning_effort, "ultra")
        self.assertEqual(model_config("verifier").reasoning_effort, "ultra")
        self.assertEqual(model_config("challenge-verifier").reasoning_effort, "ultra")
        self.assertEqual(model_config("main-closure-review").reasoning_effort, "ultra")
        self.assertEqual(model_config("worker").reasoning_effort, "max")
        self.assertEqual(model_config("proof-writer").reasoning_effort, "max")
        self.assertEqual(model_config("summarizer").reasoning_effort, "max")
        self.assertEqual(
            model_config("discovery-sprint-summarizer").reasoning_effort,
            "max",
        )
        self.assertEqual(model_config("synthesizer").reasoning_effort, "xhigh")
        with self.assertRaises(ValueError):
            model_config("scheduler")

    def test_legacy_sol_routes_preserve_reasoning_effort(self) -> None:
        for effort in ("low", "medium", "high", "xhigh", "max", "ultra"):
            with self.subTest(effort=effort):
                config = ModelConfig("gpt-5.6-sol", effort)
                self.assertEqual(config.model, "gpt-6-astra")
                self.assertEqual(config.reasoning_effort, effort)
        custom = ModelConfig("operator-selected-model", "high")
        self.assertEqual(custom.model, "operator-selected-model")

    def test_coordinator_prompts_cover_the_design_contracts(self) -> None:
        main = main_prompt(ROOT_PROBLEM)
        trimmer = trimmer_prompt(ROOT_PROBLEM)
        main_text = " ".join(main.split())
        trimmer_text = " ".join(trimmer.split())

        # Coordinators need a fuller operating contract than workers, while
        # still retaining a guard against accidental unbounded prompt growth.
        self.assertLessEqual(len(main.split()), 1500)
        self.assertLessEqual(len(trimmer.split()), 1200)

        self.assertLess(
            main_text.index("new or recent task summaries"),
            main_text.index("read complete records"),
        )
        for text in (
            "every running task card",
            "version-pinned, read-only snapshot of all project memory",
            "snapshot ID and high-water mark",
            "local file reads, `rg`, and small local scripts",
            "focused context seed, not a limit",
            "self-contained objective",
            "complete task and progress history",
            "exactly one blocked main obligation",
            "genuinely different",
            "not significant by themselves",
            "six explicit cross-task resume",
            "using the supplied `reserved_batch_id`",
            "`wait_for_results`",
            "`terminal`",
        ):
            self.assertIn(text, main_text)
        for text in ("category", "internal-search", "`stuck`", "report.summary"):
            self.assertNotIn(text, main_text)

        for text in (
            "A category is a mutable, nonauthoritative organizational view",
            "Tasks and computations",
            "zero, one, or several categories",
            "neither owns nor deletes memory",
            "must never be treated as verified support",
            "A category portfolio is a revision-pinned selection",
            "not an access boundary",
            "In `review`",
            "One failed task",
            "return only `no_trim` or `trim`",
            "Maintenance must precede selection",
            "split and redraw a category",
            "exact union",
            "not numerical quotas",
            "confirm the proposal only through an event ID",
            "`discovery-sprint`",
            "`human-guidance`",
            "scheduler alone",
        ):
            self.assertIn(text, trimmer_text)

        self.assertLess(
            trimmer_text.index("Maintain First inspect"),
            trimmer_text.index("Select After maintenance"),
        )

    def test_non_coordinator_prompts_are_concise_and_preserve_boundaries(self) -> None:
        ordinary = worker_prompt("research", ROOT_PROBLEM, policy_for("worker", mode="research"))
        isolated = worker_prompt(
            "brainstorm", ROOT_PROBLEM, policy_for("worker", mode="brainstorm")
        )
        for prompt in (
            ordinary,
            isolated,
            verifier_prompt(),
            challenge_verifier_prompt(),
            closure_review_prompt(),
            sprint_summarizer_prompt(),
        ):
            # Includes the supplied worker goal guard and verification contracts.
            # Retain a bounded size check without truncating research instructions.
            self.assertLess(len(prompt.split()), 600)
        self.assertIn("internal-search", ordinary)
        self.assertNotIn("call `internal-search`", isolated)
        self.assertIn("sealed", isolated)
        self.assertIn("`final_progress_id`", ordinary)
        self.assertIn("`is_final: true`", ordinary)
        self.assertIn("exact immutable", verifier_prompt())
        self.assertIn("fact-only `internal-search`", verifier_prompt())
        self.assertIn("abstracts first", verifier_prompt())
        self.assertIn("only `correct` confirms", verifier_prompt())
        self.assertIn("operation digest", synthesizer_prompt())
        self.assertIn("only the sealed frozen input", sprint_summarizer_prompt())
        self.assertIn("Franta staging skills are unavailable", sprint_summarizer_prompt())
        self.assertIn("confirmed_invalid", challenge_verifier_prompt())
        self.assertIn("Do not plan assignments", closure_review_prompt())

    def test_franta_worker_prompts_forbid_codex_thread_goals(self) -> None:
        goal_guard = (
            "Do not use Codex thread goals or call `create_goal`, `get_goal`, or "
            "`update_goal`."
        )
        for mode in (
            "research",
            "brainstorm",
            "associate",
            "multi-discipline",
            "reformulate",
            "computation",
        ):
            with self.subTest(mode=mode):
                prompt = worker_prompt(
                    mode,
                    ROOT_PROBLEM,
                    policy_for("worker", mode=mode),
                )
                self.assertIn(goal_guard, prompt)

    def test_synthesizer_prompt_restricts_relied_on_to_canonical_memory(self) -> None:
        text = " ".join(synthesizer_prompt().split())
        self.assertIn("already-existing canonical memory record", text)
        self.assertIn("current revision", text)
        self.assertIn("never put a temporary/proposal ID", text)
        self.assertIn("typed relationship field", text)

    def test_trimmer_prompt_mentions_only_materialized_optional_skills(self) -> None:
        minimal = trimmer_prompt(
            ROOT_PROBLEM,
            available_skills={"internal-search", "task-search"},
        )
        sprint_only = trimmer_prompt(
            ROOT_PROBLEM,
            available_skills={"internal-search", "task-search", "discovery-sprint"},
        )
        self.assertNotIn("discovery-sprint", minimal)
        self.assertNotIn("human-guidance", minimal)
        self.assertIn("discovery-sprint", sprint_only)
        self.assertNotIn("human-guidance", sprint_only)

    def test_mode_prompts_name_only_available_memory_capabilities(self) -> None:
        proof_writer = worker_prompt(
            "proof-writer", ROOT_PROBLEM, policy_for("proof-writer")
        )
        self.assertIn("fact_dependency_closure", proof_writer)
        self.assertIn("no other project memory is available", proof_writer)
        self.assertNotIn("Codex thread goals", proof_writer)
        for mode in ("brainstorm", "multi-discipline"):
            prompt = worker_prompt(mode, ROOT_PROBLEM, policy_for("worker", mode=mode))
            self.assertIn("Project-memory tools are unavailable", prompt)
            self.assertNotIn("call `internal-search`", prompt)


class PolicyTests(unittest.TestCase):
    def test_access_matrix(self) -> None:
        main = policy_for("main")
        trimmer = policy_for("trimmer")
        research = policy_for("worker", mode="research")
        brainstorm = policy_for("worker", mode="brainstorm")
        multi = policy_for("worker", mode="multi-discipline")
        sprint_d = policy_for("worker", mode="associate", sprint_lane="D")
        verifier = policy_for("verifier")
        writer = policy_for("proof-writer")
        summarizer = policy_for("summarizer")

        self.assertTrue(main.project_memory_snapshot)
        self.assertFalse(main.project_memory_api)
        self.assertFalse(main.category_api)
        self.assertFalse(main.isolated_from_project_memory)
        self.assertEqual(
            main.allowed_memory_types,
            frozenset({"fact", "route", "memo", "claim", "obligation", "task", "computation"}),
        )
        self.assertTrue(main.as_public_dict()["project_memory_snapshot"])
        for policy in (trimmer, research):
            self.assertTrue(policy.project_memory_api)
            self.assertTrue(policy.native_web_search)
        self.assertTrue(main.native_web_search)
        for policy in (brainstorm, multi, sprint_d, summarizer):
            self.assertFalse(policy.project_memory_api)
            self.assertTrue(policy.isolated_from_project_memory)
        self.assertEqual(verifier.allowed_memory_types, frozenset({"fact"}))
        self.assertTrue(writer.dependency_closure_only)
        self.assertFalse(writer.project_memory_api)
        for policy in (main, trimmer, research, brainstorm, multi, sprint_d, verifier, writer):
            self.assertFalse(policy.direct_canonical_mount)
            self.assertFalse(policy.command_network)

    def test_skill_allowlist_tracks_access(self) -> None:
        self.assertEqual(
            allowed_skills(policy_for("main")),
            frozenset({"task-writing", "task-search"}),
        )
        self.assertNotIn("internal-search", allowed_skills(policy_for("main")))
        self.assertIn("internal-search", allowed_skills(policy_for("worker", mode="research")))
        self.assertNotIn(
            "internal-search", allowed_skills(policy_for("worker", mode="brainstorm"))
        )
        self.assertEqual(
            allowed_skills(policy_for("summarizer")),
            frozenset(),
        )
        main_sort = policy_for("main-sort")
        self.assertFalse(main_sort.explorer_memory_api)
        self.assertNotIn("explorer-search", allowed_skills(main_sort))
        self.assertIn("record-progress", allowed_skills(main_sort))

    def test_permission_profile_denies_canonical_private_and_command_network(self) -> None:
        policy = policy_for("worker", mode="research")
        profile = CodexPermissionProfile.for_policy(
            policy,
            canonical_path="/project/canonical",
            private_paths=("/project/private",),
        )
        overrides = dict(profile.config_overrides())
        filesystem = overrides[f"permissions.{profile.name}.filesystem"]
        self.assertIn('"/project/canonical" = "deny"', filesystem)
        self.assertIn('"/project/private" = "deny"', filesystem)
        self.assertIn('"." = "read"', filesystem)
        self.assertIn('"outbox" = "write"', filesystem)
        self.assertEqual(overrides[f"permissions.{profile.name}.network.enabled"], "false")
        self.assertFalse(any("sandbox_mode" in key for key in overrides))


class MemoryApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.backend = InMemoryBackend(records())

    def test_status_filter_precedes_ranking_and_full_reads_are_audited(self) -> None:
        audit = AuditLog()
        api = AuditedMemoryAPI(
            self.backend,
            policy_for("worker", mode="research"),
            audit=audit,
            caller_id="T-1",
        )
        result = api.search("Hodge", ["fact", "claim"], limit=10)
        ids = {item["id"] for item in result}
        self.assertIn("F-1", ids)
        self.assertNotIn("F-2", ids)
        self.assertNotIn("CL-1", ids)
        self.assertNotIn("content", result[0])
        full = api.fetch("F-1")
        self.assertIn("content", full)
        self.assertEqual([event["action"] for event in audit.events], ["internal_search", "memory_fetch"])

    def test_isolation_and_verifier_fact_only(self) -> None:
        isolated = AuditedMemoryAPI(self.backend, policy_for("worker", mode="brainstorm"))
        with self.assertRaises(AccessError):
            isolated.search("Hodge", ["fact"])
        verifier = AuditedMemoryAPI(self.backend, policy_for("verifier"))
        with self.assertRaises(AccessError):
            verifier.search("Hodge", ["route"])
        with self.assertRaises(AccessError):
            verifier.fetch("R-1")

    def test_proof_writer_gets_only_on_demand_active_closure(self) -> None:
        api = AuditedMemoryAPI(
            self.backend,
            policy_for("proof-writer"),
            root_fact_id="F-3",
        )
        with self.assertRaises(AccessError):
            api.fetch("F-1")
        closure = api.dependency_closure()
        self.assertEqual({entry["id"] for entry in closure}, {"F-1", "F-3"})
        self.assertEqual(api.fetch("F-1")["id"], "F-1")
        with self.assertRaises(AccessError):
            api.fetch("F-4")

    def test_broker_capability_cannot_widen_policy_or_accept_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            audit = AuditLog()
            broker = MemoryBroker(self.backend, audit=audit)
            broker.start(Path(directory) / "private" / "memory.sock")
            try:
                binding = broker.issue(policy_for("verifier"), caller_id="verify-1")
                client = BrokerClient(binding.socket_path, binding.token)
                result = client.call(
                    "internal_search", {"query": "Hodge", "memory_types": ["fact"]}
                )
                self.assertIn("F-1", {entry["id"] for entry in result})
                with self.assertRaises(BrokerError):
                    client.call(
                        "internal_search", {"query": "Hodge", "memory_types": ["route"]}
                    )
                with self.assertRaises(BrokerError):
                    client.call("memory_fetch", {"memory_id": "../../canonical.sqlite"})
                with self.assertRaises(BrokerError):
                    BrokerClient(binding.socket_path, "wrong-token").call("describe", {})
            finally:
                broker.stop()

    def test_mcp_stdio_adapter_uses_private_capability(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            broker = MemoryBroker(self.backend)
            broker.start(root / "private" / "memory.sock")
            try:
                binding = broker.issue(
                    policy_for("worker", mode="research"), caller_id="T-stdio"
                )
                capability = root / "private" / "capability.json"
                capability.write_text(
                    json.dumps(
                        {"socket_path": binding.socket_path, "token": binding.token}
                    ),
                    encoding="utf-8",
                )
                capability.chmod(0o600)
                environment = os.environ.copy()
                environment.update(
                    {
                        "PYTHONPATH": str(SRC),
                        "FRANTA_BROKER_CAPABILITY_FILE": str(capability),
                    }
                )
                requests = [
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "initialize",
                        "params": {},
                    },
                    {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                    {
                        "jsonrpc": "2.0",
                        "id": 3,
                        "method": "tools/call",
                        "params": {
                            "name": "internal_search",
                            "arguments": {
                                "query": "Hodge",
                                "memory_types": ["fact"],
                            },
                        },
                    },
                ]
                completed = subprocess.run(
                    [sys.executable, "-m", "franta.skill_runtime", "mcp-server"],
                    input="\n".join(json.dumps(value) for value in requests) + "\n",
                    text=True,
                    capture_output=True,
                    env=environment,
                    check=False,
                    timeout=10,
                )
                self.assertEqual(completed.returncode, 0, completed.stderr)
                responses = [json.loads(line) for line in completed.stdout.splitlines()]
                self.assertEqual(len(responses), 3)
                tool_names = {
                    tool["name"] for tool in responses[1]["result"]["tools"]
                }
                self.assertEqual(tool_names, {"internal_search", "memory_fetch"})
                content = responses[2]["result"]["content"][0]["text"]
                self.assertIn("F-1", content)
            finally:
                broker.stop()


class MaterializerTests(unittest.TestCase):
    def test_copy_only_layers_root_problem_and_skills(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            materializer = WorkspaceMaterializer(Path(directory) / "workspaces")
            snapshot = MemorySnapshot(
                "F-1", "fact", "Hodge abstract", "Complete copied fact proof", status="active"
            )
            workspace = materializer.create(
                "call-1",
                root_problem=ROOT_PROBLEM,
                policy=policy_for("worker", mode="research"),
                task_card={"task_id": "T-1", "attempt": 1, "work_mode": "research"},
                portfolio=[snapshot],
                skills=("internal-search", "record-progress"),
            )
            self.assertEqual(workspace.root_problem_path.read_text().strip(), ROOT_PROBLEM)
            card = json.loads(workspace.task_card_path.read_text())
            self.assertEqual(card["root_problem"], ROOT_PROBLEM)
            index = json.loads(workspace.portfolio_index_path.read_text())
            self.assertEqual(index["records"][0]["abstract"], "Hodge abstract")
            full = workspace.path / "input" / "portfolio" / "records" / "F-1.md"
            self.assertIn("Complete copied fact proof", full.read_text())
            self.assertTrue((workspace.path / ".agents/skills/internal-search/SKILL.md").is_file())
            self.assertTrue(
                (
                    workspace.path
                    / ".agents/skills/record-progress/references/payload.md"
                ).is_file()
            )
            self.assertFalse(card["access_policy"]["direct_canonical_mount"])
            self.assertNotIn("canonical.sqlite", json.dumps(card).lower())

    def test_frozen_explorer_snapshot_is_complete_read_only_and_revalidated(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            identity = [{"record_id": "ES-source", "input_digest": "a" * 64}]
            source_digest = hashlib.sha256(
                json.dumps(
                    identity,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            content = "The complete frozen source body."
            record = {
                "id": "ES-source",
                "record_space": "explorer",
                "record_type": "scratch",
                "record_kind": "idea",
                "status": "provisional",
                "title": "Frozen source abstract",
                "seq": 1,
                "operation_id": "scratch-source",
                "turn_id": "XTURN-1",
                "worker_session_id": "worker-1",
                "attempt_no": 1,
                "input_digest": "a" * 64,
                "content_digest": hashlib.sha256(content.encode("utf-8")).hexdigest(),
                "abstract": "Frozen source abstract",
                "content": content,
                "related_memory_ids": [],
                "cas_operation_ids": [],
                "source_scratch_ids": [],
                "source_set_digest": None,
                "directions_tried": [],
                "main_progress": None,
                "main_obstacles": None,
                "created_at": "2026-08-30T00:00:00+00:00",
                "cas_evidence": [],
            }
            snapshot = {
                "format_version": 1,
                "sort_run_id": "SORT-1",
                "turn_id": "XTURN-1",
                "source_high_water_seq": 1,
                "source_set_digest": source_digest,
                "records": [record],
            }
            workspace = WorkspaceMaterializer(
                Path(directory) / "workspaces"
            ).create(
                "sort-call",
                root_problem=ROOT_PROBLEM,
                policy=policy_for("main-sort"),
                task_card={"task_id": "T-sort", "attempt": 1},
                explorer_snapshot=snapshot,
            )
            root = workspace.input_path / "explorer_snapshot"
            manifest = json.loads((root / "manifest.json").read_text())
            self.assertEqual(manifest["record_count"], 1)
            self.assertEqual(manifest["scratch_count"], 1)
            self.assertEqual(manifest["summary_count"], 0)
            self.assertEqual(manifest["source_set_digest"], source_digest)
            record_path = root / manifest["records"][0]["path"]
            self.assertEqual(json.loads(record_path.read_text()), record)
            self.assertEqual(record_path.stat().st_mode & 0o777, 0o444)
            self.assertEqual(root.stat().st_mode & 0o777, 0o555)
            validate_explorer_snapshot(workspace.path, snapshot)

            root.chmod(0o755)
            extra = root / "unexpected-empty-directory"
            extra.mkdir()
            extra.chmod(0o555)
            root.chmod(0o555)
            with self.assertRaisesRegex(MaterializationError, "directory set drifted"):
                validate_explorer_snapshot(workspace.path, snapshot)
            root.chmod(0o755)
            extra.rmdir()
            root.chmod(0o555)

            record_path.chmod(0o644)
            record_path.write_text("{}\n", encoding="utf-8")
            record_path.chmod(0o444)
            with self.assertRaisesRegex(MaterializationError, "content drifted"):
                validate_explorer_snapshot(workspace.path, snapshot)

            invalid = json.loads(json.dumps(snapshot))
            invalid["records"][0]["unexpected"] = "mutable extension"
            with self.assertRaisesRegex(MaterializationError, "malformed"):
                WorkspaceMaterializer(Path(directory) / "invalid-workspaces").create(
                    "sort-invalid-contract",
                    root_problem=ROOT_PROBLEM,
                    policy=policy_for("main-sort"),
                    task_card={"task_id": "T-sort", "attempt": 1},
                    explorer_snapshot=invalid,
                )

            symlink_workspace = WorkspaceMaterializer(
                Path(directory) / "symlink-workspaces"
            ).create(
                "sort-symlink",
                root_problem=ROOT_PROBLEM,
                policy=policy_for("main-sort"),
                task_card={"task_id": "T-sort", "attempt": 1},
                explorer_snapshot=snapshot,
            )
            outside_input = Path(directory).resolve() / "outside-input"
            symlink_workspace.input_path.chmod(0o755)
            symlink_workspace.input_path.rename(outside_input)
            symlink_workspace.input_path.symlink_to(
                outside_input,
                target_is_directory=True,
            )
            with self.assertRaisesRegex(MaterializationError, "missing or unsafe"):
                validate_explorer_snapshot(symlink_workspace.path, snapshot)

            if hasattr(os, "mkfifo"):
                fifo_workspace = WorkspaceMaterializer(
                    Path(directory) / "fifo-workspaces"
                ).create(
                    "sort-fifo",
                    root_problem=ROOT_PROBLEM,
                    policy=policy_for("main-sort"),
                    task_card={"task_id": "T-sort", "attempt": 1},
                    explorer_snapshot=snapshot,
                )
                fifo_root = fifo_workspace.input_path / "explorer_snapshot"
                fifo_root.chmod(0o755)
                os.mkfifo(fifo_root / "unexpected.fifo", 0o444)
                fifo_root.chmod(0o555)
                with self.assertRaisesRegex(MaterializationError, "special file"):
                    validate_explorer_snapshot(fifo_workspace.path, snapshot)

    def test_traversal_and_symlink_workspace_escape_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            MemorySnapshot("../../db", "fact", "a", "b")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "workspaces"
            materializer = WorkspaceMaterializer(root)
            outside = Path(directory) / "outside"
            outside.mkdir()
            (root / "escape").symlink_to(outside, target_is_directory=True)
            with self.assertRaises(MaterializationError):
                materializer.create(
                    "escape",
                    root_problem=ROOT_PROBLEM,
                    policy=policy_for("worker", mode="brainstorm"),
                )
            with self.assertRaises(MaterializationError):
                materializer.create(
                    "../escape",
                    root_problem=ROOT_PROBLEM,
                    policy=policy_for("worker", mode="brainstorm"),
                )


class SkillRuntimeTests(unittest.TestCase):
    def _workspace(self, directory: str, role: str, mode: str | None, task: bool = True):
        materializer = WorkspaceMaterializer(Path(directory) / "workspaces")
        policy = policy_for(role, mode=mode) if mode else policy_for(role)
        card = None
        if task:
            card = {"task_id": "T-1", "attempt": 1, "work_mode": mode}
        return materializer.create(
            f"{role}-{mode or 'call'}",
            root_problem=ROOT_PROBLEM,
            policy=policy,
            task_card=card,
        )

    @staticmethod
    def _portfolio() -> dict[str, list[str]]:
        return {kind: [] for kind in ("fact", "route", "memo", "claim", "obligation", "computation")}

    def test_trimmer_review_uses_review_validation_contract(self) -> None:
        self.assertEqual(
            FrantaRuntime._validation_kind(
                {"kind": "trimmer", "continuation": {"phase": "review"}}
            ),
            "trimmer-review",
        )

    def test_task_writing_does_not_allow_obligation_inside_brainstorm_portfolio(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = self._workspace(directory, "main", None, task=False)
            runtime = SkillRuntime(SkillContext.load(workspace.path))
            portfolio = self._portfolio()
            portfolio["obligation"] = ["O-1"]
            payload = {
                "batch_finalized": True,
                "work_mode": "brainstorm",
                "main_obligation_ids": ["O-1"],
                "assignment_portfolio": portfolio,
            }
            with self.assertRaises(SkillRuntimeError):
                runtime.invoke("task-writing", payload)
            portfolio["obligation"] = []
            portfolio["fact"] = ["F-1"]
            result = runtime.invoke("task-writing", payload)
            self.assertTrue(Path(result["staged_path"]).is_file())

    def test_final_progress_requires_complete_attempt_summary(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = self._workspace(directory, "worker", "research")
            runtime = SkillRuntime(SkillContext.load(workspace.path))
            payload = {
                "sequence": 1,
                "is_final": True,
                "outcome_status": "progress",
            }
            with self.assertRaises(SkillRuntimeError):
                runtime.invoke("record-progress", payload)
            payload["attempt_summary"] = {
                "work_mode": "research",
                "task": "Attack the route",
                "proposed_outcome": "progress",
                "cumulative_important_progress": "Located the obstruction",
                "completion_evidence_operation_ids": [],
                "most_promising_next_steps": "Test the boundary case",
            }
            payload["completion_evidence_ids"] = []
            result = runtime.invoke("record-progress", payload)
            self.assertTrue(result["artifact"]["is_final"])

    def test_record_progress_completion_evidence_requires_matching_unique_string_ids(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = self._workspace(directory, "worker", "research")
            runtime = SkillRuntime(SkillContext.load(workspace.path))

            def payload(evidence: object, summary_evidence: object) -> dict[str, object]:
                return {
                    "sequence": 1,
                    "is_final": True,
                    "outcome_status": "progress",
                    "completion_evidence_ids": evidence,
                    "attempt_summary": {
                        "work_mode": "research",
                        "task": "Attack the route",
                        "proposed_outcome": "progress",
                        "cumulative_important_progress": "Located the obstruction",
                        "completion_evidence_operation_ids": summary_evidence,
                        "most_promising_next_steps": "Test the boundary case",
                    },
                }

            invalid_pairs = (
                (None, []),
                ([], None),
                (["OP-A", "OP-A"], ["OP-A"]),
                (["OP-A"], ["OP-A", "OP-A"]),
                (["OP-A"], ["OP-B"]),
                ([1], [1]),
            )
            for evidence, summary_evidence in invalid_pairs:
                with self.subTest(evidence=evidence, summary_evidence=summary_evidence):
                    with self.assertRaises(SkillRuntimeError):
                        runtime.invoke(
                            "record-progress", payload(evidence, summary_evidence)
                        )

            result = runtime.invoke(
                "record-progress", payload(["OP-A", "OP-B"], ["OP-B", "OP-A"])
            )
            self.assertEqual(
                result["artifact"]["completion_evidence_ids"], ["OP-A", "OP-B"]
            )

    def test_record_progress_rejects_invalid_operation_identities_before_staging(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = self._workspace(directory, "worker", "research")
            runtime = SkillRuntime(SkillContext.load(workspace.path))
            base = {
                "sequence": 1,
                "is_final": False,
                "outcome_status": "progress",
            }
            invalid_operations = (
                [{"kind": "memo"}],
                [
                    {"operation_id": "OP-DUPLICATE", "kind": "memo"},
                    {"operation_id": "OP-DUPLICATE", "kind": "claim_add"},
                ],
                [{"operation_id": 42, "kind": "memo"}],
                [{"operation_id": "", "kind": "memo"}],
                [{"operation_id": "   ", "kind": "memo"}],
                [{"operation_id": " OP-PADDED", "kind": "memo"}],
            )
            for operations in invalid_operations:
                with self.subTest(operations=operations):
                    with self.assertRaises(SkillRuntimeError):
                        runtime.invoke(
                            "record-progress", {**base, "operations": operations}
                        )

            staged_directory = workspace.outbox_path / "record-progress"
            self.assertFalse(staged_directory.exists())
            result = runtime.invoke(
                "record-progress",
                {
                    **base,
                    "operations": [
                        {"operation_id": "OP-A", "kind": "memo"},
                        {"operation_id": "OP-B", "kind": "claim_add"},
                    ],
                },
            )
            self.assertEqual(
                [
                    operation["operation_id"]
                    for operation in result["artifact"]["operations"]
                ],
                ["OP-A", "OP-B"],
            )

    def test_record_progress_accepts_only_cas_operation_id_references(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = self._workspace(directory, "worker", "research")
            runtime = SkillRuntime(SkillContext.load(workspace.path))
            base = {
                "sequence": 1,
                "is_final": False,
                "outcome_status": "progress",
            }
            with self.assertRaises(SkillRuntimeError):
                runtime.invoke(
                    "record-progress",
                    {**base, "computations": [{"operation_id": "FORGED-CAS"}]},
                )
            for references in ("CAS-1", ["CAS-1", "CAS-1"], [1]):
                with self.subTest(references=references):
                    with self.assertRaises(SkillRuntimeError):
                        runtime.invoke(
                            "record-progress",
                            {**base, "computation_operation_ids": references},
                        )

            result = runtime.invoke(
                "record-progress",
                {**base, "computation_operation_ids": ["CAS-1"]},
            )
            self.assertEqual(
                result["artifact"]["computation_operation_ids"], ["CAS-1"]
            )

    def test_runtime_never_accepts_inline_progress_computations(self) -> None:
        runtime = FrantaRuntime.__new__(FrantaRuntime)
        with self.assertRaises(InvalidAgentOutput):
            runtime._progress_artifacts(
                None,  # type: ignore[arg-type]
                verified_progress=[
                    {
                        "attempt": 1,
                        "sequence": 1,
                        "computations": [{"operation_id": "FORGED-CAS"}],
                    }
                ],
                verified_computations=[],
            )

        progress = runtime._progress_artifacts(
            None,  # type: ignore[arg-type]
            verified_progress=[
                {
                    "attempt": 1,
                    "sequence": 1,
                    "computation_operation_ids": ["CAS-1"],
                }
            ],
            verified_computations=[
                {
                    "operation_id": "CAS-1",
                    "software": "SageMath",
                    "software_version": "1",
                    "exit_status": 0,
                }
            ],
        )
        self.assertEqual(progress[0]["computations"][0]["operation_id"], "CAS-1")

        unreferenced = runtime._progress_artifacts(
            None,  # type: ignore[arg-type]
            verified_progress=[
                {
                    "attempt": 1,
                    "sequence": 1,
                    "is_final": True,
                    "computation_operation_ids": [],
                }
            ],
            verified_computations=[
                {
                    "operation_id": "CAS-UNREFERENCED",
                    "software": "SageMath",
                    "software_version": "1",
                    "exit_status": 0,
                }
            ],
        )
        self.assertEqual(unreferenced[0]["computations"], [])

        with self.assertRaisesRegex(InvalidAgentOutput, "failed CAS artifact"):
            runtime._progress_artifacts(
                None,  # type: ignore[arg-type]
                verified_progress=[
                    {
                        "attempt": 1,
                        "sequence": 1,
                        "computation_operation_ids": ["CAS-FAILED"],
                    }
                ],
                verified_computations=[
                    {
                        "operation_id": "CAS-FAILED",
                        "software": "SageMath",
                        "software_version": "1",
                        "exit_status": 1,
                    }
                ],
            )

    def test_skill_receipt_hashes_artifact_and_runtime_rejects_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = self._workspace(directory, "main", None, task=False)
            skill = SkillRuntime(SkillContext.load(workspace.path))
            payload = {
                "operation_id": "AR-1",
                "batch_finalized": True,
                "batch_id": "BATCH-1",
                "objective": "Investigate the route",
                "work_mode": "research",
                "if_resume": None,
                "main_route_ids": ["R-1"],
                "main_obligation_ids": [],
                "selected_new_perspective": None,
                "assignment_portfolio": self._portfolio(),
                "reason": "Focused test",
            }
            staged = skill.invoke("task-writing", payload)
            receipt = json.loads(
                (workspace.outbox_path / "skill_activity.jsonl").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(receipt["status"], "succeeded")
            self.assertEqual(receipt["call_id"], workspace.path.name)
            self.assertEqual(len(receipt["artifact_sha256"]), 64)

            driver = object.__new__(FrantaRuntime)
            driver.executor = lambda call: {}
            response = {
                "decision": "assignments",
                "assignment_report_ids": [staged["operation_id"]],
            }
            driver._validate_main_task_writing(
                response,
                workspace=workspace,
                call_id=workspace.path.name,
                reserved_batch_id="BATCH-1",
                terminal=False,
            )
            driver.executor = None
            driver.transport = types.SimpleNamespace(
                state_dir=Path(directory) / "transport-state"
            )
            with self.assertRaises(InvalidAgentOutput):
                driver._validate_main_task_writing(
                    response,
                    workspace=workspace,
                    call_id=workspace.path.name,
                    reserved_batch_id="BATCH-1",
                    terminal=False,
                )
            artifact_path = Path(staged["staged_path"])
            artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
            artifact["reason"] = "hand-written mutation"
            artifact_path.write_text(json.dumps(artifact), encoding="utf-8")
            with self.assertRaises(InvalidAgentOutput):
                driver._validate_main_task_writing(
                    response,
                    workspace=workspace,
                    call_id=workspace.path.name,
                    reserved_batch_id="BATCH-1",
                    terminal=False,
                )

    def test_worker_normal_exit_requires_matching_final_record_progress(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = self._workspace(directory, "worker", "research")
            skill = SkillRuntime(SkillContext.load(workspace.path))
            staged = skill.invoke(
                "record-progress",
                {
                    "operation_id": "RP-1",
                    "progress_id": "PRG-final",
                    "sequence": 1,
                    "is_final": True,
                    "outcome_status": "failed",
                    "completion_evidence_ids": [],
                    "attempt_summary": {
                        "work_mode": "research",
                        "task": "Investigate the route",
                        "proposed_outcome": "failed",
                        "cumulative_important_progress": "No important progress",
                        "completion_evidence_operation_ids": [],
                        "most_promising_next_steps": "Try another route",
                    },
                },
            )
            self.assertEqual(staged["artifact"]["progress_id"], "PRG-final")
            driver = object.__new__(FrantaRuntime)
            driver.executor = lambda call: {}
            self.assertEqual(
                driver._verified_worker_final_progress_id(
                    "T-1",
                    workspace.path.name,
                    1,
                    workspace,
                    {"attempt_ended": True, "final_progress_id": "PRG-final"},
                    expected_lease_epoch=1,
                    expected_launch_attempt=1,
                ),
                "PRG-final",
            )
            with self.assertRaises(InvalidAgentOutput):
                driver._verified_worker_final_progress_id(
                    "T-1",
                    workspace.path.name,
                    1,
                    workspace,
                    {"attempt_ended": True, "final_progress_id": "PRG-other"},
                    expected_lease_epoch=1,
                    expected_launch_attempt=1,
                )

    def test_execute_cas_stages_only_canonical_computation_fields(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            materializer = WorkspaceMaterializer(Path(directory) / "workspaces")
            workspace = materializer.create(
                "cas-worker",
                root_problem=ROOT_PROBLEM,
                policy=policy_for("worker", mode="computation"),
                task_card={
                    "task_id": "T-1",
                    "attempt": 1,
                    "work_mode": "computation",
                },
            )
            def direct_test_run(context, executable, arguments, **kwargs):
                return subprocess.run(
                    [str(executable), *arguments],
                    input=kwargs.get("input_text"),
                    text=True,
                    capture_output=True,
                    check=False,
                    cwd=context.workspace,
                    shell=False,
                )

            with mock.patch(
                "franta.skill_runtime._run_constrained", side_effect=direct_test_run
            ):
                result = execute_cas(
                    SkillContext.load(workspace.path),
                    {
                        "software": "python",
                        "arguments": [
                            "-c",
                            "import sys; print(sys.stdin.read().strip().upper())",
                        ],
                        "version_arguments": ["--version"],
                        "exact_input": "franta\n",
                        "description": "Upper-case a deterministic test string",
                        "assumptions": "None",
                        "environment_versions": {},
                        "random_seed": None,
                        "interpretation": "The configured executable accepted standard input.",
                        "related_ids": {},
                        "fact_candidate_operation_ids": [],
                    },
                    configured_executables={"python": sys.executable},
                )
            artifact = result["artifact"]
            self.assertEqual(artifact["exact_output"].strip(), "FRANTA")
            self.assertNotIn("arguments", artifact)
            self.assertNotIn("version_arguments", artifact)
            self.assertIn("invocation_arguments", artifact["environment_versions"])


class _FakeRunner:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def __call__(self, command, **kwargs):
        self.calls.append({"command": command, **kwargs})
        events = [
            {"type": "thread.started", "thread_id": "thread-123"},
            {
                "type": "item.started",
                "item": {
                    "id": "tool-2",
                    "type": "command_execution",
                    "command": (
                        "python3 -m franta.skill_runtime stage "
                        "record-progress progress.json"
                    ),
                    # Event phase wins over a contradictory status: this row
                    # must not count as a successful staged invocation.
                    "status": "completed",
                },
            },
            {
                "type": "item.completed",
                "item": {
                    "id": "tool-1",
                    "type": "mcp_tool_call",
                    "server": "franta",
                    "tool": "internal_search",
                    "status": "completed",
                    "arguments": {
                        "query": "Euler trail",
                        "memory_types": ["fact"],
                        "limit": 3,
                    },
                    "result": {
                        "structured_content": {
                            "result": [{"id": "F-1", "abstract": "A fact."}]
                        }
                    },
                },
            },
            {
                "type": "item.completed",
                "item": {
                    "id": "tool-2",
                    "type": "command_execution",
                    "command": (
                        "python3 -m franta.skill_runtime stage "
                        "record-progress progress.json"
                    ),
                    "status": "completed",
                    "exit_code": 0,
                },
            },
            {
                "type": "item.completed",
                "item": {
                    "id": "tool-3",
                    "type": "command_execution",
                    "command": (
                        "python3 -m franta.skill_runtime stage "
                        "task-writing assignment.json"
                    ),
                    "status": "completed",
                    "exit_code": 2,
                },
            },
            {
                "type": "item.completed",
                "item": {"id": "msg-1", "type": "agent_message", "text": "done"},
            },
            {"type": "turn.completed"},
        ]
        return _FakeProcess("\n".join(json.dumps(event) for event in events) + "\n")


class _FakeProcess:
    def __init__(self, stdout: str, stderr: str = "", returncode: int = 0) -> None:
        self.pid = None
        self.stdin = io.StringIO()
        self.stdout = io.StringIO(stdout)
        self.stderr = io.StringIO(stderr)
        self.returncode = returncode

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        del timeout
        return self.returncode

    def terminate(self):
        self.returncode = -15

    def kill(self):
        self.returncode = -9


class TransportTests(unittest.TestCase):
    def test_jsonl_thread_persistence_resume_models_permissions_and_tool_audit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            workspace = base / "workspace"
            for relative in ("outbox", "artifacts", "tmp"):
                (workspace / relative).mkdir(parents=True, exist_ok=True)
            policy = policy_for("worker", mode="research")
            profile = CodexPermissionProfile.for_policy(
                policy,
                canonical_path=base / "canonical.sqlite",
                private_paths=(base / "scheduler-private",),
            )
            runner = _FakeRunner()
            host_codex_home = base / "host-codex-home"
            host_codex_home.mkdir()
            (host_codex_home / "auth.json").write_text('{"token":"test"}\n')
            (host_codex_home / "skills").mkdir()
            (host_codex_home / "plugins").mkdir()
            transport = CodexTransport(
                base / "state",
                runner=runner,
                source_root=SRC,
                host_codex_home=host_codex_home,
            )
            binding = BrokerBinding(
                str(base / "private.sock"), "secret-not-on-command-line", ("internal_search",)
            )
            request = CodexRequest(
                call_id="call-1",
                role="worker",
                prompt="work",
                workspace=workspace,
                policy=policy,
                permission_profile=profile,
                session_key="worker-lineage-1",
                broker_binding=binding,
            )
            with mock.patch.dict(
                os.environ,
                {
                    "CODEX_THREAD_ID": "parent-thread-must-not-leak",
                    "CODEX_INTERNAL_ORIGINATOR_OVERRIDE": "parent-originator",
                    "UNRELATED_SECRET": "must-not-leak",
                },
            ):
                result = transport.invoke(request)
            self.assertEqual(result.thread_id, "thread-123")
            self.assertEqual(result.final_message, "done")
            command = runner.calls[0]["command"]
            joined = " ".join(command)
            self.assertIn("gpt-6-astra", joined)
            self.assertIn('model_reasoning_effort="max"', joined)
            self.assertIn("model_context_window=872000", command)
            self.assertIn("model_auto_compact_token_limit=780000", command)
            self.assertIn('web_search="live"', joined)
            self.assertIn("network.enabled=false", joined)
            self.assertIn("default_permissions", joined)
            self.assertIn('features.multi_agent=false', joined)
            self.assertIn("features.goals=false", command)
            self.assertIn('agents.enabled=false', joined)
            self.assertIn('features.plugins=false', joined)
            self.assertIn('include_collaboration_mode_instructions=false', joined)
            self.assertIn('project_root_markers=[".franta-root"]', joined)
            self.assertIn('mcp_servers.franta.default_tools_approval_mode="approve"', joined)
            self.assertNotIn("--sandbox", command)
            self.assertNotIn("secret-not-on-command-line", joined)
            self.assertIn("FRANTA_BROKER_CAPABILITY_FILE", joined)
            launched_environment = runner.calls[0]["env"]
            self.assertNotIn("CODEX_THREAD_ID", launched_environment)
            self.assertNotIn("CODEX_INTERNAL_ORIGINATOR_OVERRIDE", launched_environment)
            self.assertNotIn("UNRELATED_SECRET", launched_environment)
            self.assertEqual(
                launched_environment["CODEX_HOME"],
                str((base / "state/codex-home").resolve()),
            )
            self.assertTrue((base / "state/codex-home/auth.json").is_file())
            self.assertFalse((base / "state/codex-home/skills").exists())
            self.assertFalse((base / "state/codex-home/plugins").exists())
            self.assertTrue((workspace / ".franta-root").is_file())
            self.assertFalse((base / "state/broker-capabilities/call-1.json").exists())
            self.assertEqual(transport.ledger.resolve("worker-lineage-1"), "thread-123")
            activity = (base / "state/tool_activity.jsonl").read_text()
            self.assertIn("internal_search", activity)
            activity_rows = [json.loads(line) for line in activity.splitlines()]
            search_row = next(
                row for row in activity_rows if row.get("tool") == "internal_search"
            )
            self.assertEqual(search_row["query"], "Euler trail")
            self.assertEqual(search_row["memory_types"], ["fact"])
            self.assertEqual(search_row["result_ids"], ["F-1"])
            self.assertEqual(
                transport.successful_skill_invocation_count(
                    "call-1", "record-progress"
                ),
                1,
            )
            self.assertEqual(
                transport.successful_skill_invocation_count("call-1", "task-writing"),
                0,
            )
            self.assertEqual(transport.completed_final_message("call-1"), "done")

            resumed = CodexRequest(
                call_id="call-2",
                role="worker",
                prompt="continue",
                workspace=workspace,
                policy=policy,
                permission_profile=profile,
                session_key="worker-lineage-1",
                resume=True,
            )
            self.assertTrue(transport.invoke(resumed).resumed)
            resume_command = runner.calls[1]["command"]
            self.assertEqual(resume_command[1:3], ["exec", "resume"])
            self.assertIn("thread-123", resume_command)
            self.assertIn("features.goals=false", resume_command)
            self.assertIn("model_context_window=872000", resume_command)
            self.assertIn("model_auto_compact_token_limit=780000", resume_command)

            synth_request = CodexRequest(
                call_id="synth-1",
                role="synthesizer",
                prompt="review",
                workspace=workspace,
                policy=policy_for("synthesizer", review_memory_type="fact"),
                permission_profile=profile,
            )
            synth_command = transport.build_command(synth_request, thread_id=None)
            self.assertIn('model_reasoning_effort="xhigh"', " ".join(synth_command))
            self.assertIn("model_context_window=872000", synth_command)
            self.assertIn("model_auto_compact_token_limit=780000", synth_command)
            self.assertNotIn("features.goals=false", synth_command)

            configured_request = CodexRequest(
                call_id="configured-1",
                role="worker",
                prompt="work",
                workspace=workspace,
                policy=policy,
                permission_profile=profile,
                model_config=ModelConfig("operator-selected-model", "high"),
            )
            configured_command = transport.build_command(
                configured_request, thread_id=None
            )
            configured_joined = " ".join(configured_command)
            self.assertIn("operator-selected-model", configured_command)
            self.assertIn('model_reasoning_effort="high"', configured_joined)

    def test_single_agent_preflight_fails_closed_on_collaboration_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            workspace = base / "workspace"
            for relative in ("outbox", "artifacts", "tmp"):
                (workspace / relative).mkdir(parents=True, exist_ok=True)
            policy = policy_for("worker", mode="research")
            profile = CodexPermissionProfile.for_policy(
                policy,
                canonical_path=base / "canonical.sqlite",
                private_paths=(base / "private",),
            )
            request = CodexRequest(
                call_id="preflight-1",
                role="worker",
                prompt="work",
                workspace=workspace,
                policy=policy,
                permission_profile=profile,
            )
            transport = CodexTransport(
                base / "state",
                source_root=SRC,
                host_codex_home=base / "host-codex-home",
            )
            environment = {"PATH": os.environ.get("PATH", "")}
            accepted = subprocess.CompletedProcess([], 0, "[]", "")
            with mock.patch("franta.transport.subprocess.run", return_value=accepted) as run:
                transport._preflight_single_agent_surface(
                    request, workspace=workspace, environment=environment
                )
            self.assertIn("agents.enabled=false", " ".join(run.call_args.args[0]))
            self.assertIn("model_context_window=872000", run.call_args.args[0])
            self.assertIn("model_auto_compact_token_limit=780000", run.call_args.args[0])
            self.assertTrue((base / "state/single-agent-preflight.jsonl").is_file())

            second = CodexTransport(
                base / "other-state",
                source_root=SRC,
                host_codex_home=base / "host-codex-home",
            )
            leaked = subprocess.CompletedProcess(
                [], 0, '"You can call spawn_agent"', ""
            )
            with mock.patch("franta.transport.subprocess.run", return_value=leaked):
                with self.assertRaises(CodexTransportError):
                    second._preflight_single_agent_surface(
                        request, workspace=workspace, environment=environment
                    )

    def test_parse_jsonl_rejects_non_json_noise(self) -> None:
        self.assertEqual(parse_jsonl_events('{"type":"turn.completed"}\n')[0]["type"], "turn.completed")
        with self.assertRaises(Exception):
            parse_jsonl_events("not-json\n")


if __name__ == "__main__":
    unittest.main()
