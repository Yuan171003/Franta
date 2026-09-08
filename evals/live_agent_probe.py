#!/usr/bin/env python3
"""Run focused, inspectable probes against real Franta Codex agent sessions.

The probe deliberately uses the production workspace materializer, prompts,
permission policies, memory broker, output schemas, skill runtime, and Codex
transport.  It never mutates the operator's Franta project; the repair-stop
probe creates its own disposable project below the fresh diagnostic directory.
Every run is summarized as JSON.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import shutil
import sys
import tempfile
import traceback
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = PROJECT_ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from franta.access import (  # noqa: E402
    AuditLog,
    CodexPermissionProfile,
    InMemoryBackend,
    MemoryBroker,
    MemoryRecord,
    policy_for,
)
from franta.categories import CategoryStore  # noqa: E402
from franta.config import load_manifest  # noqa: E402
from franta.materialize import (  # noqa: E402
    MaterializedWorkspace,
    WorkspaceMaterializer,
)
from franta.output_schemas import write_schemas  # noqa: E402
from franta.prompts import model_config, prompt_for  # noqa: E402
from franta.runtime import AgentExecutor, FrantaRuntime  # noqa: E402
from franta.skill_runtime import (  # noqa: E402
    SkillContext,
    SkillRuntime,
    allowed_skills,
    compile_human_guidance,
    execute_cas,
)
from franta.store import MemoryStore  # noqa: E402
from franta.transport import (  # noqa: E402
    CodexRequest,
    CodexResult,
    CodexTransport,
    CodexTransportError,
)
from franta.workflows import CallState, GateState, OperationState, TaskState  # noqa: E402


ROOT_PROBLEM = (
    "Prove that every finite graph in which every vertex has even degree has "
    "an Eulerian circuit in each connected component containing an edge."
)
FOUNDATION_POLICY = (
    "Use the standard definitions of a finite graph, degree, connected component, "
    "trail, and Eulerian circuit."
)
PORTFOLIO_TYPES = ("fact", "route", "memo", "claim", "obligation", "computation")
PROBE_NAMES = (
    "main",
    "research",
    "isolated",
    "trimmer",
    "discovery-sprint",
    "human-guidance",
    "cas",
    "repair-stop",
)

REPAIR_V1_CANDIDATE_ID = "FC-LIVE-ROOT-REPAIR-V1"
REPAIR_V1_PROPOSAL_ID = "TMP-LIVE-ROOT-REPAIR-V1"
REPAIR_V1_OPERATION_ID = "OP-LIVE-ROOT-REPAIR-V1"
REPAIR_V2_CANDIDATE_ID = "FC-LIVE-ROOT-REPAIR-V2"
REPAIR_V2_PROPOSAL_ID = "TMP-LIVE-ROOT-REPAIR-V2"
REPAIR_V2_OPERATION_ID = "OP-LIVE-ROOT-REPAIR-V2"
REPAIR_PREDECESSOR_CANDIDATE_ID = "FC-LIVE-REPAIR-PREDECESSOR"
REPAIR_PREDECESSOR_OPERATION_ID = "OP-LIVE-REPAIR-PREDECESSOR"
REPAIR_PREDECESSOR_PROPOSAL_ID = "TMP-LIVE-REPAIR-PREDECESSOR"


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _json_object(text: str) -> dict[str, Any]:
    value = json.loads(text)
    if not isinstance(value, dict):
        raise ValueError("structured final message is not a JSON object")
    return value


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    values: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            values.append({"_invalid_jsonl": line})
            continue
        if isinstance(value, dict):
            values.append(value)
    return values


def _read_artifacts(workspace: Path, skill: str) -> list[dict[str, Any]]:
    directory = workspace / "outbox" / skill
    values: list[dict[str, Any]] = []
    if not directory.is_dir():
        return values
    for path in sorted(directory.glob("*.json")):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            value = {"_invalid_json": True}
        if isinstance(value, dict):
            value = dict(value)
            value["_relative_path"] = str(path.relative_to(workspace))
            value["_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
            values.append(value)
    return values


def _classify_full_record_reads(
    actions: Sequence[Mapping[str, Any]],
    *,
    required_ids: Sequence[str],
    related_ids: Sequence[str],
) -> dict[str, Any]:
    """Separate relevant expansion from unrelated and clearly repeated reads.

    A repeated fetch is called clearly redundant only when the same record was
    fetched earlier, no new search returned it in between, and the later audit
    row carries no explicit need or justification.  The classification is
    diagnostic: the probe still treats progressive disclosure as a ``SHOULD``.
    """

    required = {str(item) for item in required_ids}
    related = {str(item) for item in related_ids} - required
    last_search: dict[str, int] = {}
    last_fetch: dict[str, int] = {}
    fetch_counts: dict[str, int] = {}
    required_reads: set[str] = set()
    related_reads: set[str] = set()
    unrelated_reads: set[str] = set()
    clearly_redundant: list[dict[str, Any]] = []

    for index, action in enumerate(actions):
        kind = action.get("action")
        if kind == "internal_search":
            for memory_id in action.get("result_ids", []):
                last_search[str(memory_id)] = index
            continue
        if kind != "memory_fetch":
            continue
        memory_id = str(action.get("memory_id") or "")
        if not memory_id:
            continue
        fetch_counts[memory_id] = fetch_counts.get(memory_id, 0) + 1
        if memory_id in required:
            required_reads.add(memory_id)
        elif memory_id in related:
            related_reads.add(memory_id)
        else:
            unrelated_reads.add(memory_id)

        previous_fetch = last_fetch.get(memory_id)
        explicitly_needed = action.get("needed") is True or bool(
            action.get("justification") or action.get("reason")
        )
        if (
            previous_fetch is not None
            and last_search.get(memory_id, -1) <= previous_fetch
            and not explicitly_needed
        ):
            clearly_redundant.append(
                {
                    "memory_id": memory_id,
                    "previous_action_index": previous_fetch,
                    "action_index": index,
                }
            )
        last_fetch[memory_id] = index

    return {
        "required_record_reads": sorted(required_reads),
        "defensible_related_record_reads": sorted(related_reads),
        "clearly_unrelated_record_reads": sorted(unrelated_reads),
        "repeated_full_record_reads": {
            memory_id: count
            for memory_id, count in sorted(fetch_counts.items())
            if count > 1
        },
        "clearly_redundant_full_record_reads": clearly_redundant,
    }


def _skill_receipt_checks(
    workspace: Path,
    *,
    skill: str,
    artifacts: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    receipts = [
        item
        for item in _read_jsonl(workspace / "outbox" / "skill_activity.jsonl")
        if item.get("skill") == skill
    ]
    matched: list[str] = []
    for artifact in artifacts:
        operation_id = artifact.get("operation_id")
        relative_path = artifact.get("_relative_path")
        digest = artifact.get("_sha256")
        if any(
            receipt.get("status") == "succeeded"
            and receipt.get("operation_id") == operation_id
            and receipt.get("relative_path") == relative_path
            and receipt.get("artifact_sha256") == digest
            for receipt in receipts
        ):
            matched.append(str(operation_id))
    return {
        "receipt_count": len(receipts),
        "matched_artifact_operation_ids": matched,
        "all_artifacts_have_hash_matched_receipts": len(matched) == len(artifacts),
        "receipts": receipts,
    }


def _empty_portfolio() -> dict[str, list[str]]:
    return {kind: [] for kind in PORTFOLIO_TYPES}


def _task_card(
    *,
    task_id: str,
    mode: str,
    objective: str,
    main_route_ids: Sequence[str] = (),
    main_obligation_ids: Sequence[str] = (),
) -> dict[str, Any]:
    return {
        "task_id": task_id,
        "attempt": 1,
        "batch_id": f"BATCH-{task_id}",
        "objective": objective,
        "mode": mode,
        "if_resume": None,
        "main_route_ids": list(main_route_ids),
        "main_obligation_ids": list(main_obligation_ids),
        "selected_new_perspective": None,
        "assignment_portfolio": _empty_portfolio(),
        "reason": "Exercise the selected access and terminal-skill behavior in isolation.",
        "root_solution_fact_id": None,
        "foundation_policy": FOUNDATION_POLICY,
        "attempt_supplement": None,
    }


def _broker_records() -> list[MemoryRecord]:
    return [
        MemoryRecord(
            "F-1",
            "fact",
            "Every vertex of a finite even-degree graph lies on a closed trail.",
            (
                "Starting from such a vertex and extending a trail without repeating an edge, "
                "parity prevents a first dead end away from the start. Finiteness then closes "
                "the trail."
            ),
            title="Closed-trail parity lemma",
        ),
        MemoryRecord(
            "R-1",
            "route",
            (
                "Chromatic-lantern splicing route: repeatedly extract a closed trail and splice "
                "it into the current circuit inside one connected even-degree component."
            ),
            (
                "Full strategy: begin with the parity lemma, remove the edges of the resulting "
                "closed trail, and note that all residual degrees remain even. If unused edges "
                "remain in the component, connectedness supplies a vertex of the current trail "
                "incident to a residual edge. Build another closed trail there and splice it at "
                "that vertex. Finiteness makes the iteration terminate."
            ),
            title="Chromatic-lantern trail splicing",
            metadata={
                "probe_marker": "needed-full-record",
                "related_memory_ids": ["F-1", "O-ROOT"],
            },
        ),
        MemoryRecord(
            "R-2",
            "route",
            "Degree-sum induction on trees, a tempting but inapplicable route for cyclic graphs.",
            (
                "This route deletes a leaf and inducts. It does not apply directly because an "
                "even-degree connected graph with edges has no leaf."
            ),
            title="Tree-leaf induction distractor",
        ),
        MemoryRecord(
            "M-1",
            "memo",
            "Terminology memo distinguishing a circuit from a general closed walk.",
            "A circuit is a closed trail and therefore repeats no edge.",
        ),
        MemoryRecord(
            "O-ROOT",
            "obligation",
            "Construct an Eulerian circuit in an arbitrary connected even-degree component.",
            ROOT_PROBLEM,
        ),
    ]


class _ProbeTaskBackend:
    """Small closed-task view for exercising the production task broker."""

    _summaries = {
        "T-PREVIOUS-SPLICE-CHECK": {
            "task_id": "T-PREVIOUS-SPLICE-CHECK",
            "state": "closed",
            "final_status": "progress",
            "final_summary": {
                "cumulative_important_progress": (
                    "The remaining gap is to justify that an unused edge can be reached "
                    "from the current trail before the next splice."
                )
            },
            "artifacts": [],
        },
        "T-ALGEBRAIC-SUMMARY": {
            "task_id": "T-ALGEBRAIC-SUMMARY",
            "state": "closed",
            "final_status": "progress",
            "final_summary": {
                "cumulative_important_progress": (
                    "Reduced the algebraic route to cycle splicing."
                )
            },
            "artifacts": [],
        },
        "T-INVOLUTION-SUMMARY": {
            "task_id": "T-INVOLUTION-SUMMARY",
            "state": "closed",
            "final_status": "progress",
            "final_summary": {
                "cumulative_important_progress": (
                    "Reduced the local route to selecting one successor orbit."
                )
            },
            "artifacts": [],
        },
    }

    def __init__(
        self, summaries: Sequence[Mapping[str, Any]] | None = None
    ) -> None:
        self._values = copy.deepcopy(self._summaries)
        for item in summaries or ():
            task_id = str(item.get("task_id") or "")
            if task_id:
                self._values[task_id] = copy.deepcopy(dict(item))

    def get_task_summary(self, task_id: str) -> Mapping[str, Any] | None:
        value = self._values.get(task_id)
        return dict(value) if value is not None else None

    def get_task_artifact(
        self, task_id: str, artifact_id: str
    ) -> Mapping[str, Any] | None:
        del task_id, artifact_id
        return None


def _issue_probe_binding(
    broker: MemoryBroker,
    *,
    policy: Any,
    call_id: str,
    workspace: MaterializedWorkspace,
    cas_executables: Mapping[str, str] | None = None,
    tectonic_executable: str | None = None,
) -> Any:
    configured_cas = dict(cas_executables or {})
    staging_skills = allowed_skills(policy) & {
        "task-writing",
        "record-progress",
        "CAS",
        "human-guidance",
        "discovery-sprint",
    }
    if not configured_cas:
        staging_skills -= {"CAS"}
    if tectonic_executable is None:
        staging_skills -= {"human-guidance"}
    skill_runtime = SkillRuntime(SkillContext.load(workspace.path))

    def stage(skill: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        if skill == "CAS":
            return execute_cas(
                skill_runtime.context,
                payload,
                configured_executables=configured_cas,
            )
        if skill == "human-guidance":
            return compile_human_guidance(
                skill_runtime.context,
                payload,
                tectonic_executable=tectonic_executable,
            )
        return skill_runtime.invoke(skill, payload)

    return broker.issue(
        policy,
        caller_id=call_id,
        staging_handler=stage,
        staging_skills=staging_skills,
        cas_software_names=sorted(configured_cas),
    )


@dataclass
class ProbeHarness:
    root: Path
    codex_binary: str
    timeout_seconds: float | None

    def __post_init__(self) -> None:
        self.root.mkdir(parents=True, exist_ok=False)
        self.workspaces = self.root / "workspaces"
        self.state = self.root / "transport-state"
        self.schemas = self.root / "schemas"
        self.canonical = self.root / "canonical-denied"
        self.private = self.root / "scheduler-private-denied"
        self.canonical.mkdir()
        self.private.mkdir()
        self.materializer = WorkspaceMaterializer(self.workspaces)
        self.transport = CodexTransport(
            self.state,
            codex_binary=self.codex_binary,
            source_root=SOURCE_ROOT,
            default_timeout_seconds=self.timeout_seconds,
        )
        self.schema_paths = write_schemas(self.schemas)

    def permission_profile(self, policy: Any) -> CodexPermissionProfile:
        return CodexPermissionProfile.for_policy(
            policy,
            canonical_path=self.canonical,
            private_paths=(self.private,),
        )

    def invoke(
        self,
        *,
        call_id: str,
        role: str,
        mode: str | None,
        policy: Any,
        workspace: MaterializedWorkspace,
        schema_name: str,
        broker_binding: Any = None,
    ) -> tuple[CodexResult, dict[str, Any]]:
        prompt = prompt_for(
            role,
            root_problem=ROOT_PROBLEM,
            mode=mode,
            policy=policy,
            input_path=(
                "input/task_card.json" if role == "worker" else "input/context.json"
            ),
        )
        request = CodexRequest(
            call_id=call_id,
            role=role,
            prompt=prompt,
            workspace=workspace.path,
            policy=policy,
            permission_profile=self.permission_profile(policy),
            broker_binding=broker_binding,
            timeout_seconds=self.timeout_seconds,
            output_schema=self.schema_paths[schema_name],
        )
        result = self.transport.invoke(request)
        return result, _json_object(result.final_message)

    def tool_activity(self, call_id: str) -> list[dict[str, Any]]:
        return [
            item
            for item in _read_jsonl(self.state / "tool_activity.jsonl")
            if item.get("call_id") == call_id
        ]


def _base_observation(
    harness: ProbeHarness,
    *,
    name: str,
    call_id: str,
    workspace: Path,
) -> dict[str, Any]:
    config = model_config("worker" if name in {"research", "isolated"} else name)
    return {
        "probe": name,
        "call_id": call_id,
        "model": config.model,
        "reasoning_effort": config.reasoning_effort,
        "workspace": str(workspace),
        "call_audit": str(harness.state / "calls" / f"{call_id}.jsonl"),
    }


def _materialized_isolation_checks(
    harness: ProbeHarness,
    *,
    workspace: MaterializedWorkspace,
    policy: Any,
) -> tuple[dict[str, bool], dict[str, Any]]:
    manifest = json.loads(workspace.access_manifest_path.read_text(encoding="utf-8"))
    skill_root = workspace.path / ".agents" / "skills"
    materialized_skills = (
        sorted(path.name for path in skill_root.iterdir())
        if skill_root.is_dir()
        else []
    )
    profile = harness.permission_profile(policy)
    overrides = dict(profile.config_overrides())
    filesystem = overrides[f"permissions.{profile.name}.filesystem"]
    checks = {
        "policy_isolated_from_project_memory": policy.isolated_from_project_memory,
        "policy_has_no_full_record_fetch": not policy.full_record_fetch,
        "policy_has_no_allowed_memory_types": not policy.allowed_memory_types,
        "policy_has_no_direct_canonical_mount": not policy.direct_canonical_mount,
        "policy_keeps_native_web_search": policy.native_web_search,
        "manifest_has_no_project_memory_api": not manifest.get("project_memory_api"),
        "manifest_has_no_dependency_closure": not manifest.get(
            "dependency_closure_only"
        ),
        "manifest_has_no_full_record_fetch": not manifest.get("full_record_fetch"),
        "manifest_has_no_allowed_memory_types": not manifest.get(
            "allowed_memory_types"
        ),
        "manifest_has_no_direct_canonical_mount": not manifest.get(
            "direct_canonical_mount"
        ),
        "manifest_keeps_native_web_search": manifest.get("native_web_search")
        is True,
        "internal_search_skill_not_allowed": "internal-search"
        not in allowed_skills(policy),
        "internal_search_skill_not_materialized": "internal-search"
        not in materialized_skills,
        "canonical_path_is_explicitly_denied": str(harness.canonical.resolve())
        in filesystem,
        "scheduler_private_path_is_explicitly_denied": str(harness.private.resolve())
        in filesystem,
        "command_network_is_disabled": overrides[
            f"permissions.{profile.name}.network.enabled"
        ]
        == "false",
    }
    details = {
        "workspace": str(workspace.path),
        "policy": policy.as_public_dict(),
        "access_manifest": manifest,
        "materialized_skills": materialized_skills,
    }
    return checks, details


def _materialize_isolation_matrix(harness: ProbeHarness) -> dict[str, Any]:
    cases = (
        ("brainstorm", None),
        ("multi-discipline", None),
        ("brainstorm", "A"),
        ("multi-discipline", "B"),
        ("computation", "C"),
        ("associate", "D"),
    )
    workers: list[dict[str, Any]] = []
    for index, (mode, sprint_lane) in enumerate(cases, 1):
        label = f"{mode}{'-sprint-' + sprint_lane if sprint_lane else ''}"
        call_id = f"MATERIALIZED-ISOLATION-{index}"
        policy = policy_for("worker", mode=mode, sprint_lane=sprint_lane)
        card = _task_card(
            task_id=f"T-MATERIALIZED-ISOLATION-{index}",
            mode=mode,
            objective=(
                "Inspect this sealed assignment without project-memory access and stage no "
                "canonical-memory proposal."
            ),
            main_obligation_ids=("O-ROOT",),
        )
        if sprint_lane:
            card["sprint_id"] = "S-LIVE-ISOLATION"
            card["sprint_lane"] = sprint_lane
        workspace = harness.materializer.create(
            call_id,
            root_problem=ROOT_PROBLEM,
            policy=policy,
            task_card=card,
            skills=sorted(allowed_skills(policy)),
        )
        checks, details = _materialized_isolation_checks(
            harness,
            workspace=workspace,
            policy=policy,
        )
        workers.append(
            {
                "case": label,
                "mode": mode,
                "sprint_lane": sprint_lane,
                "status": "passed" if all(checks.values()) else "failed",
                "checks": checks,
                **details,
            }
        )

    frozen_input = {
        "plan": {
            "target_obligation": {
                "id": "O-ROOT",
                "revision": 1,
                "statement": ROOT_PROBLEM,
            }
        },
        "lane_results": [
            {"lane": lane, "final_status": "progress", "final_summary": {}}
            for lane in ("A", "B", "C", "D")
        ],
    }
    summarizer_policy = policy_for("summarizer")
    summarizer_workspace = harness.materializer.create(
        "MATERIALIZED-SPRINT-SUMMARIZER",
        root_problem=ROOT_PROBLEM,
        policy=summarizer_policy,
        frozen_input=frozen_input,
        skills=sorted(allowed_skills(summarizer_policy)),
    )
    summarizer_checks, summarizer_details = _materialized_isolation_checks(
        harness,
        workspace=summarizer_workspace,
        policy=summarizer_policy,
    )
    input_files = sorted(
        str(path.relative_to(summarizer_workspace.input_path))
        for path in summarizer_workspace.input_path.rglob("*")
        if path.is_file()
    )
    summarizer_checks.update(
        {
            "frozen_input_is_exact": json.loads(
                (summarizer_workspace.input_path / "frozen_sprint.json").read_text(
                    encoding="utf-8"
                )
            )
            == frozen_input,
            "no_task_card_context_or_portfolio_materialized": set(input_files)
            == {"access_policy.json", "frozen_sprint.json", "root_problem.md"},
            "no_franta_skills_materialized": not summarizer_details[
                "materialized_skills"
            ],
        }
    )
    summarizer = {
        "status": "passed" if all(summarizer_checks.values()) else "failed",
        "checks": summarizer_checks,
        "input_files": input_files,
        **summarizer_details,
    }
    return {
        "workers": workers,
        "summarizer": summarizer,
        "checks": {
            "brainstorm_is_sealed": all(
                item["status"] == "passed"
                for item in workers
                if item["mode"] == "brainstorm"
            ),
            "multi_discipline_is_sealed": all(
                item["status"] == "passed"
                for item in workers
                if item["mode"] == "multi-discipline"
            ),
            "all_four_discovery_sprint_lanes_are_sealed": {
                item["sprint_lane"]
                for item in workers
                if item["sprint_lane"] and item["status"] == "passed"
            }
            == {"A", "B", "C", "D"},
            "discovery_sprint_summarizer_is_sealed": summarizer["status"]
            == "passed",
        },
    }


def probe_main(harness: ProbeHarness) -> dict[str, Any]:
    name = "main"
    call_id = "LIVE-MAIN-TASK-WRITING"
    policy = policy_for("main")
    batch_id = "BATCH-LIVE-MAIN"
    records = _broker_records()
    context = {
        "root_problem": ROOT_PROBLEM,
        "root": {"status": "unresolved", "obligation_id": "O-ROOT"},
        "foundation_policy": FOUNDATION_POLICY,
        "reserved_batch_id": batch_id,
        "free_non_verifier_slots": 1,
        "category_portfolio": {
            "revision": 1,
            "categories": [{"category_id": "CAT-ROUTES", "category_revision": 1}],
            "flattened_members": {
                "fact": ["F-1"],
                "route": ["R-1"],
                "memo": [],
                "claim": [],
                "obligation": ["O-ROOT"],
            },
            "selection_rationale": "Test the active constructive splicing route.",
        },
        "category_descriptions": [
            {
                "id": "CAT-ROUTES",
                "revision": 1,
                "status": "active",
                "name": "Constructive splicing",
                "description": "Closed-trail extraction and splicing approaches.",
                "main_progress": "The parity lemma is available as F-1.",
                "current_obstacles": "A worker should check the complete splice argument.",
                "members": {
                    "fact": ["F-1"],
                    "route": ["R-1"],
                    "memo": [],
                    "claim": [],
                    "obligation": ["O-ROOT"],
                },
            }
        ],
        "new_task_summaries": [
            {
                "task_id": "T-PREVIOUS-SPLICE-CHECK",
                "mode": "research",
                "objective": "Check the closed-trail splicing argument.",
                "state": "closed",
                "final_status": "progress",
                "final_summary": {
                    "cumulative_important_progress": (
                        "The remaining gap is to justify that an unused edge can be reached "
                        "from a vertex of the current closed trail before the next splice."
                    )
                },
                "canonical_changes": [],
            }
        ],
        "running_task_cards": [],
        "human_guidance_history": [],
        "seed_theorems": [],
        "event_cursor": 1,
        "portfolio_revision": 1,
        "terminal_resolution_call": False,
    }
    workspace = harness.materializer.create(
        call_id,
        root_problem=ROOT_PROBLEM,
        policy=policy,
        context=context,
        skills=sorted(allowed_skills(policy)),
    )
    observation = _base_observation(
        harness, name=name, call_id=call_id, workspace=workspace.path
    )
    audit = AuditLog(harness.root / "broker-main.jsonl")
    broker = MemoryBroker(
        InMemoryBackend(records), task_backend=_ProbeTaskBackend(), audit=audit
    )
    broker.start(harness.private / "main-memory.sock")
    binding = None
    try:
        binding = _issue_probe_binding(
            broker,
            policy=policy,
            call_id=call_id,
            workspace=workspace,
        )
        result, response = harness.invoke(
            call_id=call_id,
            role="main",
            mode=None,
            policy=policy,
            workspace=workspace,
            schema_name="main",
            broker_binding=binding,
        )
    finally:
        if binding is not None:
            broker.revoke(binding)
        broker.stop()

    artifacts = _read_artifacts(workspace.path, "task-writing")
    receipts = _skill_receipt_checks(
        workspace.path, skill="task-writing", artifacts=artifacts
    )
    artifact_ids = [str(item.get("operation_id")) for item in artifacts]
    response_ids = [str(item) for item in response.get("assignment_report_ids", [])]
    summary_reads = [
        index
        for index, event in enumerate(audit.events)
        if event.get("action") == "task_summary_read"
        and event.get("task_id") == "T-PREVIOUS-SPLICE-CHECK"
    ]
    full_reads = [
        index
        for index, event in enumerate(audit.events)
        if event.get("action") in {"memory_fetch", "task_artifact_fetch"}
    ]
    checks = {
        "transport_returncode_zero": result.returncode == 0,
        "decision_is_assignments": response.get("decision") == "assignments",
        "reserved_batch_preserved": response.get("batch_id") == batch_id,
        "one_task_writing_artifact": len(artifacts) == 1,
        "response_names_exact_artifact": response_ids == artifact_ids,
        "artifact_has_required_research_route": bool(artifacts)
        and artifacts[0].get("work_mode") == "research"
        and artifacts[0].get("main_route_ids") == ["R-1"],
        "task_summary_read_before_any_full_record": bool(summary_reads)
        and (not full_reads or summary_reads[0] < full_reads[0]),
        "assignment_resumes_summarized_task": bool(artifacts)
        and artifacts[0].get("if_resume") == "T-PREVIOUS-SPLICE-CHECK",
        "no_full_task_artifact_was_needed": not any(
            event.get("action") == "task_artifact_fetch" for event in audit.events
        ),
        "artifact_batch_preserved": bool(artifacts)
        and artifacts[0].get("batch_id") == batch_id,
        "hash_matched_skill_receipts": receipts[
            "all_artifacts_have_hash_matched_receipts"
        ],
        "one_transport_audited_task_writing_call": (
            harness.transport.successful_skill_invocation_count(
                call_id, "task-writing"
            )
            == 1
        ),
    }
    observation.update(
        {
            "status": "passed" if all(checks.values()) else "failed",
            "checks": checks,
            "response": response,
            "task_writing_artifacts": artifacts,
            "skill_receipts": receipts,
            "broker_audit": audit.events,
            "tool_activity": harness.tool_activity(call_id),
        }
    )
    return observation


def _final_progress_observation(
    harness: ProbeHarness,
    *,
    call_id: str,
    workspace: Path,
    response: Mapping[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
    artifacts = _read_artifacts(workspace, "record-progress")
    finals = [item for item in artifacts if item.get("is_final") is True]
    receipts = _skill_receipt_checks(
        workspace, skill="record-progress", artifacts=artifacts
    )
    checks = {
        "attempt_ended_true": response.get("attempt_ended") is True,
        "exactly_one_final_record_progress": len(finals) == 1,
        "final_response_matches_artifact": len(finals) == 1
        and response.get("final_progress_id") == finals[0].get("progress_id"),
        "hash_matched_skill_receipts": receipts[
            "all_artifacts_have_hash_matched_receipts"
        ],
        # Intermediate records are permitted.  Only the final record is
        # singular; every staged record must correspond to an audited command.
        "transport_audited_record_progress_calls_match_artifacts": bool(artifacts)
        and harness.transport.successful_skill_invocation_count(
            call_id, "record-progress"
        )
        == len(artifacts),
    }
    return checks, artifacts, receipts


def probe_research(harness: ProbeHarness) -> dict[str, Any]:
    name = "research"
    call_id = "LIVE-WORKER-RESEARCH"
    policy = policy_for("worker", mode="research")
    objective = (
        "Use internal-search to find the project route whose abstract names the "
        "chromatic-lantern splicing method. Compare the returned abstracts first, then fetch "
        "the full R-1 record because its exact residual-degree and splice steps are needed. "
        "Assess whether those steps form a coherent route to ROOT and record the assessment; "
        "do not propose a duplicate route or obligation."
    )
    task_card = _task_card(
        task_id="T-LIVE-RESEARCH",
        mode="research",
        objective=objective,
        main_route_ids=("R-1",),
    )
    workspace = harness.materializer.create(
        call_id,
        root_problem=ROOT_PROBLEM,
        policy=policy,
        task_card=task_card,
        skills=sorted(allowed_skills(policy)),
    )
    observation = _base_observation(
        harness, name=name, call_id=call_id, workspace=workspace.path
    )
    records = _broker_records()
    audit = AuditLog(harness.root / "broker-research.jsonl")
    broker = MemoryBroker(InMemoryBackend(records), audit=audit)
    broker.start(harness.private / "research-memory.sock")
    binding = None
    try:
        binding = _issue_probe_binding(
            broker,
            policy=policy,
            call_id=call_id,
            workspace=workspace,
        )
        result, response = harness.invoke(
            call_id=call_id,
            role="worker",
            mode="research",
            policy=policy,
            workspace=workspace,
            schema_name="worker",
            broker_binding=binding,
        )
    finally:
        if binding is not None:
            broker.revoke(binding)
        broker.stop()

    final_checks, artifacts, receipts = _final_progress_observation(
        harness,
        call_id=call_id,
        workspace=workspace.path,
        response=response,
    )
    broker_actions = [
        item
        for item in audit.events
        if item.get("caller_id") == call_id
        and item.get("action") in {"internal_search", "memory_fetch"}
    ]
    search_positions = [
        index
        for index, item in enumerate(broker_actions)
        if item.get("action") == "internal_search"
    ]
    fetch_positions = [
        index
        for index, item in enumerate(broker_actions)
        if item.get("action") == "memory_fetch"
    ]
    returned_before_fetch: set[str] = set()
    every_fetch_followed_abstract = True
    for index, item in enumerate(broker_actions):
        if item.get("action") == "internal_search":
            returned_before_fetch.update(str(value) for value in item.get("result_ids", []))
        elif item.get("action") == "memory_fetch":
            if str(item.get("memory_id")) not in returned_before_fetch:
                every_fetch_followed_abstract = False
    fetched_ids = [
        str(item.get("memory_id"))
        for item in broker_actions
        if item.get("action") == "memory_fetch"
    ]
    required_record_ids = {"R-1"}
    related_record_ids = {
        str(item)
        for record in records
        if record.memory_id in required_record_ids
        for item in record.metadata.get("related_memory_ids", [])
    }
    read_classification = _classify_full_record_reads(
        broker_actions,
        required_ids=sorted(required_record_ids),
        related_ids=sorted(related_record_ids),
    )
    checks = {
        **final_checks,
        "transport_returncode_zero": result.returncode == 0,
        "internal_search_was_used": bool(search_positions),
        "full_record_fetch_was_used": bool(fetch_positions),
        "abstract_search_preceded_every_fetch": every_fetch_followed_abstract,
        "objective_needed_record_was_fetched": "R-1" in fetched_ids,
        "no_clearly_unrelated_full_record_was_fetched": not read_classification[
            "clearly_unrelated_record_reads"
        ],
    }
    observation.update(
        {
            "status": "passed" if all(checks.values()) else "failed",
            "checks": checks,
            "response": response,
            "record_progress_artifacts": artifacts,
            "skill_receipts": receipts,
            "broker_audit": audit.events,
            "broker_action_order": broker_actions,
            "full_record_read_classification": read_classification,
            "tool_activity": harness.tool_activity(call_id),
        }
    )
    return observation


def probe_isolated(harness: ProbeHarness) -> dict[str, Any]:
    name = "isolated"
    call_id = "LIVE-WORKER-BRAINSTORM"
    policy = policy_for("worker", mode="brainstorm")
    objective = (
        "Using only this sealed task card and the root problem, independently outline one "
        "parity-based attack on O-ROOT. Do not use project-memory tools. Record the attempt "
        "without proposing a fact, route, or obligation."
    )
    task_card = _task_card(
        task_id="T-LIVE-BRAINSTORM",
        mode="brainstorm",
        objective=objective,
        main_obligation_ids=("O-ROOT",),
    )
    workspace = harness.materializer.create(
        call_id,
        root_problem=ROOT_PROBLEM,
        policy=policy,
        task_card=task_card,
        skills=sorted(allowed_skills(policy)),
    )
    observation = _base_observation(
        harness, name=name, call_id=call_id, workspace=workspace.path
    )
    broker = MemoryBroker(InMemoryBackend(()))
    broker.start(harness.private / "isolated-staging.sock")
    binding = None
    try:
        binding = _issue_probe_binding(
            broker,
            policy=policy,
            call_id=call_id,
            workspace=workspace,
        )
        result, response = harness.invoke(
            call_id=call_id,
            role="worker",
            mode="brainstorm",
            policy=policy,
            workspace=workspace,
            schema_name="worker",
            broker_binding=binding,
        )
    finally:
        if binding is not None:
            broker.revoke(binding)
        broker.stop()
    final_checks, artifacts, receipts = _final_progress_observation(
        harness,
        call_id=call_id,
        workspace=workspace.path,
        response=response,
    )
    tool_activity = harness.tool_activity(call_id)
    forbidden_tool_events = [
        item
        for item in tool_activity
        if str(item.get("tool", item.get("name", ""))).casefold()
        in {"internal_search", "memory_fetch", "fact_dependency_closure"}
    ]
    access_manifest = json.loads(
        workspace.access_manifest_path.read_text(encoding="utf-8")
    )
    isolation_matrix = _materialize_isolation_matrix(harness)
    checks = {
        **final_checks,
        "transport_returncode_zero": result.returncode == 0,
        "policy_has_no_project_memory_api": not policy.project_memory_api,
        "policy_has_no_dependency_closure": not policy.dependency_closure_only,
        "access_manifest_has_no_project_memory_api": not access_manifest.get(
            "project_memory_api"
        ),
        "internal_search_skill_not_materialized": not (
            workspace.path / ".agents" / "skills" / "internal-search"
        ).exists(),
        "no_memory_broker_tool_event": not forbidden_tool_events,
        **isolation_matrix["checks"],
    }
    observation.update(
        {
            "status": "passed" if all(checks.values()) else "failed",
            "checks": checks,
            "response": response,
            "record_progress_artifacts": artifacts,
            "skill_receipts": receipts,
            "tool_activity": tool_activity,
            "forbidden_tool_events": forbidden_tool_events,
            "access_manifest": access_manifest,
            "isolation_matrix": isolation_matrix,
        }
    )
    return observation


def _create_trimmer_scenario(
    root: Path,
) -> tuple[MemoryStore, CategoryStore, dict[str, Any], list[MemoryRecord]]:
    memory = MemoryStore(root / "canonical-memory.sqlite3", False)
    categories = CategoryStore(memory, root / "category-projections")
    task_id = memory.allocate_id("task")
    fact = memory.add_fact(
        "probe-fact",
        {
            "statement": "The degree sum of a finite graph is twice its number of edges.",
            "proof": "Each edge contributes one incidence at each of its two endpoints.",
            "predecessor_fact_ids": [],
            "originating_task_id": task_id,
            "foundation_policy_version": 1,
            "introduced_notation": [],
            "external_references": [],
            "root_resolution": None,
            "abstract": "The finite-graph degree-sum identity by counting incidences",
            "keywords": ["degree", "incidence"],
            "related_route_ids": [],
        },
    ).canonical_id
    assert fact is not None
    algebraic_route = memory.add_route(
        "probe-route-algebraic",
        {
            "abstract": "Cycle-space decomposition over the two-element field",
            "strategy_description": (
                "Represent even subgraphs as cycle-space vectors and extract circuits."
            ),
            "value_assessment": {
                "confidence": "structurally plausible",
                "success_gain": "would give a global decomposition",
                "failure_gain": "would isolate the connectivity step",
                "relevance": "direct",
                "novelty": "algebraic language",
            },
            "progress": ["The degree parity condition is linear."],
            "related_obligation_ids": [],
            "next_steps": ["Relate a cycle decomposition to one circuit."],
            "obstacles": ["Splicing cycles constructively."],
            "active_fact_ids": [fact],
            "relevant_memo_ids": [],
            "relevant_claim_ids": [],
        },
    ).canonical_id
    involution_route = memory.add_route(
        "probe-route-involution",
        {
            "abstract": "Local half-edge pairing and successor involution construction",
            "strategy_description": (
                "Pair incidences at every vertex and study the induced edge-successor orbits."
            ),
            "value_assessment": {
                "confidence": "constructive but incomplete",
                "success_gain": "would directly produce closed circuits",
                "failure_gain": "would expose orbit-merging obstruction",
                "relevance": "direct",
                "novelty": "local combinatorial language",
            },
            "progress": ["Every even set of incidences admits a pairing."],
            "related_obligation_ids": [],
            "next_steps": ["Merge distinct successor orbits."],
            "obstacles": ["A pairing may create several circuits."],
            "active_fact_ids": [fact],
            "relevant_memo_ids": [],
            "relevant_claim_ids": [],
        },
    ).canonical_id
    assert algebraic_route is not None and involution_route is not None
    algebraic_obligation = memory.add_obligation(
        "probe-obligation-algebraic",
        {
            "abstract": "Turn a cycle-space decomposition into a single Eulerian circuit",
            "statement": (
                "Show that cycles in a connected even subgraph can be spliced into one circuit."
            ),
            "importance": "This completes the algebraic decomposition route.",
            "predecessor_fact_ids": [fact],
            "partial_progress": ["Cycle extraction is understood."],
            "related_route_ids": [algebraic_route],
            "relations": [],
        },
    ).canonical_id
    involution_obligation = memory.add_obligation(
        "probe-obligation-involution",
        {
            "abstract": "Choose local half-edge pairings with one global successor orbit",
            "statement": (
                "Construct vertex pairings whose induced successor permutation is transitive."
            ),
            "importance": "This completes the local involution route.",
            "predecessor_fact_ids": [fact],
            "partial_progress": ["Arbitrary local pairings give a circuit cover."],
            "related_route_ids": [involution_route],
            "relations": [],
        },
    ).canonical_id
    assert algebraic_obligation is not None and involution_obligation is not None
    definition = {
        "proposal_id": "CAT-PROBE-BROAD",
        "name": "Eulerian constructions",
        "description": (
            "Current work on constructing Eulerian circuits in finite even-degree graphs."
        ),
        "main_progress": (
            "Cycle-space decomposition and local successor pairings each give partial "
            "constructions."
        ),
        "current_obstacles": (
            "Cycle splicing and successor-orbit transitivity remain unresolved."
        ),
        "members": {
            "fact": [fact],
            "route": [algebraic_route, involution_route],
            "memo": [],
            "claim": [],
            "obligation": [algebraic_obligation, involution_obligation],
        },
    }
    created = categories.create_category("probe-create-broad", definition)
    if created.status != "committed":
        raise RuntimeError(created.error or "could not create broad probe category")
    category_id = created.proposal_mappings["CAT-PROBE-BROAD"]
    portfolio = categories.commit_portfolio(
        "probe-initial-portfolio",
        {
            "expected_portfolio_revision": 0,
            "categories": {category_id: 1},
            "flattened_members": definition["members"],
            "base_event_id": 10,
            "confirmed_through_event_id": 10,
            "selection_rationale": "The only initial category contains all current work.",
            "human_guidance_reference": None,
        },
    )
    if portfolio.status != "committed":
        raise RuntimeError(portfolio.error or "could not create initial probe portfolio")
    identifiers = {
        "fact": fact,
        "algebraic_route": algebraic_route,
        "involution_route": involution_route,
        "algebraic_obligation": algebraic_obligation,
        "involution_obligation": involution_obligation,
        "broad_category": category_id,
    }
    broker_records = [
        MemoryRecord(
            fact,
            "fact",
            "The finite-graph degree-sum identity by counting incidences.",
            "Each edge contributes one incidence at each endpoint.",
        ),
        MemoryRecord(
            algebraic_route,
            "route",
            "Cycle-space decomposition over the two-element field.",
            "Represent even subgraphs as cycle-space vectors and extract circuits.",
        ),
        MemoryRecord(
            involution_route,
            "route",
            "Local half-edge pairing and successor involution construction.",
            "Pair incidences at vertices and analyze successor orbits.",
        ),
        MemoryRecord(
            algebraic_obligation,
            "obligation",
            "Turn a cycle-space decomposition into a single Eulerian circuit.",
            "Show cycles in a connected even subgraph can be spliced into one circuit.",
        ),
        MemoryRecord(
            involution_obligation,
            "obligation",
            "Choose local half-edge pairings with one global successor orbit.",
            "Construct pairings whose successor permutation is transitive.",
        ),
    ]
    return memory, categories, identifiers, broker_records


def probe_trimmer(harness: ProbeHarness) -> dict[str, Any]:
    name = "trimmer"
    call_id = "LIVE-TRIMMER-REDRAW"
    policy = policy_for("trimmer")
    scenario_root = harness.root / "trimmer-scenario"
    scenario_root.mkdir()
    memory, categories, identifiers, broker_records = _create_trimmer_scenario(
        scenario_root
    )
    broad = categories.get_category(identifiers["broad_category"])
    current_portfolio = categories.active_portfolio()
    assert current_portfolio is not None
    event_cursor = 20
    context = {
        "phase": "maintain",
        "root_problem": ROOT_PROBLEM,
        "root": {
            "status": "unresolved",
            "obligation_id": identifiers["algebraic_obligation"],
        },
        "foundation_policy": FOUNDATION_POLICY,
        "active_review": {
            "trigger": "assignment_threshold",
            "report": {
                "summary": (
                    "Perform the scheduled qualitative portfolio review after two "
                    "mathematically different routes advanced."
                )
            },
        },
        "active_trim": {
            "session_id": "TRIM-LIVE-REDRAW",
            "phase": "maintain",
            "cutoff_event_id": event_cursor,
        },
        "current_portfolio": current_portfolio,
        "all_category_summaries": [broad],
        "task_summaries": [
            {
                "task_id": "T-ALGEBRAIC-SUMMARY",
                "mode": "research",
                "objective": "Develop the cycle-space route.",
                "state": "closed",
                "final_status": "progress",
                "final_summary": {
                    "cumulative_important_progress": (
                        "Reduced the algebraic route to cycle splicing."
                    )
                },
                "canonical_changes": [],
            },
            {
                "task_id": "T-INVOLUTION-SUMMARY",
                "mode": "multi-discipline",
                "objective": "Develop the successor-involution route.",
                "state": "closed",
                "final_status": "progress",
                "final_summary": {
                    "cumulative_important_progress": (
                        "Reduced the local route to selecting one successor orbit."
                    )
                },
                "canonical_changes": [],
            },
        ],
        "event_cursor": event_cursor,
        "human_guidance": {"history": [], "pending": None},
        "active_sprint": None,
    }
    workspace = harness.materializer.create(
        call_id,
        root_problem=ROOT_PROBLEM,
        policy=policy,
        context=context,
        skills=sorted(allowed_skills(policy)),
    )
    observation = _base_observation(
        harness, name=name, call_id=call_id, workspace=workspace.path
    )
    audit = AuditLog(harness.root / "broker-trimmer.jsonl")
    broker = MemoryBroker(
        InMemoryBackend(broker_records),
        task_backend=_ProbeTaskBackend(context["task_summaries"]),
        audit=audit,
    )
    broker.start(harness.private / "trimmer-memory.sock")
    binding = None
    category_commit: dict[str, Any]
    try:
        binding = _issue_probe_binding(
            broker,
            policy=policy,
            call_id=call_id,
            workspace=workspace,
        )
        result, response = harness.invoke(
            call_id=call_id,
            role="trimmer",
            mode=None,
            policy=policy,
            workspace=workspace,
            schema_name="trimmer",
            broker_binding=binding,
        )
        proposal = response.get("proposal")
        if isinstance(proposal, Mapping):
            commit = categories.apply_trim_proposal(
                "live-trimmer-returned-proposal", proposal, actor="live-probe"
            )
            category_commit = {
                "status": commit.status,
                "error": commit.error,
                "category_ids": list(commit.category_ids),
                "portfolio_revision": commit.portfolio_revision,
                "proposal_mappings": dict(commit.proposal_mappings),
            }
        else:
            category_commit = {
                "status": "not_attempted",
                "error": "response proposal was not an object",
            }
    finally:
        if binding is not None:
            broker.revoke(binding)
        broker.stop()
        memory.close()

    proposal = response.get("proposal") if isinstance(response, Mapping) else None
    changes = (
        proposal.get("category_changes", [])
        if isinstance(proposal, Mapping)
        else []
    )
    splits = [
        item
        for item in changes
        if isinstance(item, Mapping)
        and item.get("kind") == "split"
        and item.get("source_category_id") == identifiers["broad_category"]
    ]
    split_results = splits[0].get("results", []) if len(splits) == 1 else []
    checks = {
        "transport_returncode_zero": result.returncode == 0,
        "decision_is_commit": response.get("decision") == "commit",
        "broad_category_was_split": len(splits) == 1,
        "split_has_at_least_two_results": isinstance(split_results, list)
        and len(split_results) >= 2,
        "returned_proposal_passes_category_store": category_commit.get("status")
        == "committed",
        "portfolio_revision_advanced": category_commit.get("portfolio_revision") == 2,
        "response_preserves_portfolio_revision": response.get(
            "expected_portfolio_revision"
        )
        == 1,
        "response_confirms_observed_events": response.get(
            "confirmed_through_event_id"
        )
        == event_cursor,
    }
    observation.update(
        {
            "status": "passed" if all(checks.values()) else "failed",
            "checks": checks,
            "response": response,
            "scenario_ids": identifiers,
            "mechanical_category_commit": category_commit,
            "broker_audit": audit.events,
            "tool_activity": harness.tool_activity(call_id),
        }
    )
    return observation


def _staged_skill_artifacts(
    workspace: MaterializedWorkspace, skill: str
) -> list[dict[str, Any]]:
    directory = workspace.outbox_path / skill
    if not directory.is_dir():
        return []
    values: list[dict[str, Any]] = []
    for path in sorted(directory.glob("*.json")):
        value = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(value, dict):
            values.append(value)
    return values


def _broker_skill_succeeded(
    events: Sequence[Mapping[str, Any]], skill: str
) -> bool:
    return any(
        event.get("action") == "broker_skill_succeeded"
        and event.get("skill") == skill
        for event in events
    )


def probe_discovery_sprint(harness: ProbeHarness) -> dict[str, Any]:
    """Test an uncued, qualitatively justified discovery-sprint decision."""

    name = "discovery-sprint"
    call_id = "LIVE-TRIMMER-DISCOVERY-SPRINT"
    policy = policy_for("trimmer")
    obligation_id = "O-LIVE-SPRINT-TARGET"
    route_id = "R-LIVE-REPEATED-MECHANISM"
    category_id = "CAT-LIVE-REPEATED-MECHANISM"
    target_statement = (
        "In a finite connected graph containing at least one edge in which every vertex "
        "has even degree, choose a pairing of the incident half-edges at each vertex so "
        "that the successor permutation obtained by traversing an edge and then taking "
        "the paired half-edge has exactly one orbit (equivalently, one closed trail uses "
        "every edge)."
    )
    repeated = (
        "Every recent attempt changes the order of the same local half-edge pairing "
        "construction, while the global orbit-merging obstruction remains unchanged."
    )
    task_summaries = [
        {
            "task_id": f"T-LIVE-PAIRING-{index}",
            "mode": "research" if index % 2 else "associate",
            "objective": "Vary the local pairing construction.",
            "state": "closed",
            "final_status": "progress",
            "final_summary": {
                "cumulative_important_progress": (
                    f"Variant {index} again produced several successor orbits and did "
                    "not change the orbit-merging obstruction."
                )
            },
            "canonical_changes": [],
        }
        for index in range(1, 6)
    ]
    context = {
        "phase": "maintain",
        "root_problem": ROOT_PROBLEM,
        "root": {"status": "unresolved", "obligation_id": obligation_id},
        "foundation_policy": FOUNDATION_POLICY,
        "active_review": {
            "trigger": "stuck",
            "report": {
                "summary": repeated,
                "repeated_mechanism": "local half-edge pairing variants",
                "unchanged_obstacle": "merging all successor orbits globally",
            },
        },
        "active_trim": {
            "session_id": "TRIM-LIVE-DISCOVERY",
            "phase": "maintain",
            "cutoff_event_id": 40,
        },
        "current_portfolio": {
            "revision": 1,
            "categories": [
                {
                    "category_id": category_id,
                    "category_revision": 1,
                    "current_revision": 1,
                    "status": "active",
                }
            ],
            "flattened_members": {
                "fact": [],
                "route": [route_id],
                "memo": [],
                "claim": [],
                "obligation": [obligation_id],
            },
            "base_event_id": 35,
            "confirmed_through_event_id": 35,
            "selection_rationale": "Continue the only active mechanism pending review.",
            "human_guidance_reference": None,
        },
        "all_category_summaries": [
            {
                "category_id": category_id,
                "name": "Local successor pairings",
                "description": "Variants of one local pairing mechanism.",
                "main_progress": "Arbitrary pairings give a circuit cover.",
                "current_obstacles": "No variant forces one global successor orbit.",
                "members": {
                    "fact": [],
                    "route": [route_id],
                    "memo": [],
                    "claim": [],
                    "obligation": [obligation_id],
                },
            }
        ],
        "task_summaries": task_summaries,
        "event_cursor": 40,
        "human_guidance": {"history": [], "pending": None},
        "active_sprint": None,
    }
    workspace = harness.materializer.create(
        call_id,
        root_problem=ROOT_PROBLEM,
        policy=policy,
        context=context,
        skills=sorted(allowed_skills(policy)),
    )
    observation = _base_observation(
        harness, name=name, call_id=call_id, workspace=workspace.path
    )
    audit = AuditLog(harness.root / "broker-discovery-sprint.jsonl")
    broker = MemoryBroker(
        InMemoryBackend(
            [
                MemoryRecord(
                    route_id,
                    "route",
                    "Local half-edge pairing variants produce a circuit cover.",
                    repeated,
                ),
                MemoryRecord(
                    obligation_id,
                    "obligation",
                    target_statement,
                    target_statement,
                ),
                MemoryRecord(
                    "F-LIVE-PERMUTATION-SURGERY",
                    "fact",
                    (
                        "A transposition joining points in different cycles of a finite "
                        "permutation merges those two cycles."
                    ),
                    (
                        "Permutation-cycle surgery is a remote algebraic model for joining "
                        "successor orbits."
                    ),
                ),
                MemoryRecord(
                    "R-LIVE-MATROID-EXCHANGE",
                    "route",
                    "Explore graphic-matroid circuit elimination as an orbit-joining move.",
                    (
                        "A circuit-elimination route may turn local exchanges into a "
                        "global bridge between circuit components."
                    ),
                ),
                MemoryRecord(
                    "M-LIVE-UNION-FIND",
                    "memo",
                    "Compare orbit joining with exchange steps in union-find algorithms.",
                    (
                        "A deliberately distant algorithmic analogy: each accepted exchange "
                        "strictly lowers the number of components."
                    ),
                ),
                MemoryRecord(
                    "C-LIVE-FLOW-BRIDGE",
                    "claim",
                    "Integral-flow recombination may suggest a bridge between circuit covers.",
                    (
                        "A nonauthoritative remote prompt based on flow decomposition and "
                        "recombination."
                    ),
                ),
            ]
        ),
        task_backend=_ProbeTaskBackend(task_summaries),
        audit=audit,
    )
    broker.start(harness.private / "discovery-sprint-memory.sock")
    binding = None
    try:
        binding = _issue_probe_binding(
            broker,
            policy=policy,
            call_id=call_id,
            workspace=workspace,
        )
        result, response = harness.invoke(
            call_id=call_id,
            role="trimmer",
            mode=None,
            policy=policy,
            workspace=workspace,
            schema_name="trimmer",
            broker_binding=binding,
        )
    finally:
        if binding is not None:
            broker.revoke(binding)
        broker.stop()

    artifacts = _staged_skill_artifacts(workspace, "discovery-sprint")
    plan = artifacts[0] if len(artifacts) == 1 else {}
    lanes = plan.get("lanes") if isinstance(plan, Mapping) else None
    lane_values = [item for item in lanes or [] if isinstance(item, Mapping)]
    target_only_lanes = all(
        lane.get("main_obligation_ids") == [obligation_id]
        and isinstance(lane.get("assignment_portfolio"), Mapping)
        and obligation_id
        in lane["assignment_portfolio"].get("obligation", [])
        for lane in lane_values
    )
    clean_lanes = lane_values[:2]
    distant_count = 0
    computation_material_count = 0
    if len(lane_values) == 4:
        computation_portfolio = lane_values[2].get("assignment_portfolio") or {}
        computation_material_count = sum(
            len(
                [
                    item
                    for item in computation_portfolio.get(memory_type, [])
                    if item != obligation_id
                ]
            )
            for memory_type in (
                "fact",
                "route",
                "memo",
                "claim",
                "obligation",
                "computation",
            )
        )
        distant_portfolio = lane_values[3].get("assignment_portfolio") or {}
        distant_count = sum(
            len(
                [
                    item
                    for item in distant_portfolio.get(memory_type, [])
                    if item != obligation_id
                ]
            )
            for memory_type in (
                "fact",
                "route",
                "memo",
                "claim",
                "obligation",
                "computation",
            )
        )
    model_audit = _call_model_audit(harness, (call_id,))
    tool_activity = harness.tool_activity(call_id)
    checks = {
        "transport_returncode_zero": result.returncode == 0,
        "decision_is_discovery_sprint": response.get("decision")
        == "discovery_sprint",
        "exactly_one_sprint_artifact": len(artifacts) == 1,
        "artifact_is_plan_for_active_target": plan.get("decision") == "plan"
        and plan.get("target_obligation_id") == obligation_id
        and plan.get("target_statement") == target_statement,
        "four_isolated_lane_modes_are_exact": isinstance(lanes, list)
        and [lane.get("mode") for lane in lane_values]
        == ["brainstorm", "multi-discipline", "computation", "associate"],
        "every_lane_keeps_the_exact_target": len(lane_values) == 4
        and target_only_lanes,
        "lanes_a_and_b_have_only_target_and_facts": len(clean_lanes) == 2
        and all(
            not lane["assignment_portfolio"].get(memory_type)
            for lane in clean_lanes
            for memory_type in ("route", "memo", "claim", "computation")
        ),
        "lane_b_has_a_nonempty_new_perspective": len(lane_values) == 4
        and bool(str(lane_values[1].get("selected_new_perspective") or "").strip()),
        "lane_c_has_an_explicit_computation_portfolio": len(lane_values) == 4
        and computation_material_count > 0,
        "lane_d_has_two_to_four_distant_material_ids": 2 <= distant_count <= 4,
        "broker_audited_successful_skill_call": _broker_skill_succeeded(
            audit.events, "discovery-sprint"
        ),
        "exactly_one_transport_audited_skill_call": (
            harness.transport.successful_skill_invocation_count(
                call_id, "discovery-sprint"
            )
            == 1
        ),
        "configured_model_and_effort_were_used": model_audit[
            "all_calls_audited_as_expected_gpt_6_astra"
        ],
        "no_unscheduled_collaboration_tool_activity": not _has_collaboration_activity(
            tool_activity
        ),
        "human_guidance_not_called": not _staged_skill_artifacts(
            workspace, "human-guidance"
        ),
    }
    observation.update(
        {
            "status": "passed" if all(checks.values()) else "failed",
            "checks": checks,
            "response": response,
            "sprint_artifacts": artifacts,
            "broker_audit": audit.events,
            "model_audit": model_audit,
            "tool_activity": tool_activity,
        }
    )
    return observation


def probe_human_guidance(harness: ProbeHarness) -> dict[str, Any]:
    """Test a justified advisory pause and real constrained PDF compilation."""

    name = "human-guidance"
    call_id = "LIVE-TRIMMER-HUMAN-GUIDANCE"
    tectonic = shutil.which("tectonic")
    if tectonic is None:
        raise RuntimeError("the live human-guidance probe requires configured Tectonic")
    policy = policy_for("trimmer")
    directions = [
        (
            f"O-LIVE-GUIDANCE-{index}",
            f"R-LIVE-GUIDANCE-{index}",
            label,
        )
        for index, label in enumerate(
            (
                "cycle-space decomposition",
                "ear decomposition",
                "successor permutations",
                "matroid circuit elimination",
                "flow decomposition",
                "induction by trail splicing",
            ),
            1,
        )
    ]
    category_members = {
        "fact": [],
        "route": [route_id for _, route_id, _ in directions],
        "memo": [],
        "claim": [],
        "obligation": [obligation_id for obligation_id, _, _ in directions],
    }
    guidance_categories = [
        {
            "category_id": f"CAT-LIVE-GUIDANCE-{index}",
            "name": label.title(),
            "description": f"A distinct follow-up mechanism based on {label}.",
            "main_progress": "The blind sprint identified a precise next obligation.",
            "current_obstacles": "The next obligation has not yet been assigned.",
            "members": {
                "fact": [],
                "route": [route_id],
                "memo": [],
                "claim": [],
                "obligation": [obligation_id],
            },
        }
        for index, (obligation_id, route_id, label) in enumerate(directions, 1)
    ]
    context = {
        "phase": "maintain",
        "root_problem": ROOT_PROBLEM,
        "root": {"status": "unresolved", "obligation_id": directions[0][0]},
        "foundation_policy": FOUNDATION_POLICY,
        "active_review": {
            "trigger": "discovery_sprint_synthesized",
            "report": {
                "summary": (
                    "All four sealed sprint lanes returned to the same orbit-merging "
                    "obstacle. Six mathematically distinct follow-up directions remain "
                    "credible, but only four worker slots exist and the synthesis found "
                    "no principled machine-only priority among them."
                )
            },
        },
        "active_trim": {
            "session_id": "TRIM-LIVE-GUIDANCE",
            "phase": "maintain",
            "cutoff_event_id": 70,
        },
        "current_portfolio": {
            "revision": 1,
            "categories": [
                {
                    "category_id": category["category_id"],
                    "category_revision": 1,
                    "current_revision": 1,
                    "status": "active",
                }
                for category in guidance_categories
            ],
            "flattened_members": category_members,
            "base_event_id": 65,
            "confirmed_through_event_id": 65,
            "selection_rationale": "Await the completed blind-sprint synthesis.",
            "human_guidance_reference": None,
        },
        "all_category_summaries": guidance_categories,
        "task_summaries": [
            {
                "task_id": f"T-LIVE-SPRINT-{lane}",
                "mode": mode,
                "objective": "Seek a mechanism outside local pairing variants.",
                "state": "closed",
                "final_status": "progress",
                "final_summary": {
                    "cumulative_important_progress": (
                        "Found a distinct language but returned to the same global "
                        "orbit-merging obstruction."
                    )
                },
                "canonical_changes": [],
            }
            for lane, mode in zip(
                "ABCD",
                ("brainstorm", "multi-discipline", "computation", "associate"),
                strict=True,
            )
        ]
        + [
            {
                "task_id": "T-LIVE-SPRINT-PREDECESSOR",
                "mode": "research",
                "objective": "Identify the original orbit-merging obstruction.",
                "state": "closed",
                "final_status": "progress",
                "final_summary": {
                    "cumulative_important_progress": (
                        "Isolated the global orbit-merging obstruction before the sprint."
                    )
                },
                "canonical_changes": [],
            }
        ],
        "event_cursor": 70,
        "human_guidance": {"history": [], "pending": None},
        "active_sprint": {
            "sprint_id": "S-LIVE-GUIDANCE",
            "phase": "synthesized",
            "shared_bottleneck": "global orbit merging",
        },
    }
    records = [
        MemoryRecord(
            obligation_id,
            "obligation",
            f"Resolve the next step for {label}.",
            f"Prove the precise reduction required by the {label} direction.",
        )
        for obligation_id, _, label in directions
    ] + [
        MemoryRecord(
            route_id,
            "route",
            f"Develop the {label} direction.",
            f"A mathematically distinct strategy based on {label}.",
        )
        for _, route_id, label in directions
    ]
    workspace = harness.materializer.create(
        call_id,
        root_problem=ROOT_PROBLEM,
        policy=policy,
        context=context,
        skills=sorted(allowed_skills(policy)),
    )
    observation = _base_observation(
        harness, name=name, call_id=call_id, workspace=workspace.path
    )
    audit = AuditLog(harness.root / "broker-human-guidance.jsonl")
    broker = MemoryBroker(
        InMemoryBackend(records),
        task_backend=_ProbeTaskBackend(context["task_summaries"]),
        audit=audit,
    )
    broker.start(harness.private / "human-guidance-memory.sock")
    binding = None
    try:
        binding = _issue_probe_binding(
            broker,
            policy=policy,
            call_id=call_id,
            workspace=workspace,
            tectonic_executable=tectonic,
        )
        result, response = harness.invoke(
            call_id=call_id,
            role="trimmer",
            mode=None,
            policy=policy,
            workspace=workspace,
            schema_name="trimmer",
            broker_binding=binding,
        )
    finally:
        if binding is not None:
            broker.revoke(binding)
        broker.stop()

    artifacts = _staged_skill_artifacts(workspace, "human-guidance")
    request = artifacts[0] if len(artifacts) == 1 else {}
    pdf_relative = request.get("pdf_path") if isinstance(request, Mapping) else None
    pdf_path = workspace.path / str(pdf_relative) if pdf_relative else None
    source_relative = (
        request.get("source_latex_path") if isinstance(request, Mapping) else None
    )
    source_path = workspace.path / str(source_relative) if source_relative else None
    source_text = (
        source_path.read_text(encoding="utf-8", errors="replace")
        if source_path is not None and source_path.is_file()
        else ""
    )
    model_audit = _call_model_audit(harness, (call_id,))
    tool_activity = harness.tool_activity(call_id)
    checks = {
        "transport_returncode_zero": result.returncode == 0,
        "decision_is_human_guidance": response.get("decision") == "human_guidance",
        "exactly_one_guidance_artifact": len(artifacts) == 1,
        "guidance_question_is_nonempty": bool(str(request.get("question") or "").strip()),
        "compiled_pdf_exists_and_is_valid": bool(
            pdf_path
            and pdf_path.is_file()
            and pdf_path.read_bytes().startswith(b"%PDF-")
        ),
        "latex_source_is_preserved": bool(source_text),
        "report_covers_every_active_direction": all(
            label.casefold() in source_text.casefold() for _, _, label in directions
        ),
        "report_covers_five_recent_tasks": all(
            f"T-LIVE-SPRINT-{lane}" in source_text for lane in "ABCD"
        )
        and "T-LIVE-SPRINT-PREDECESSOR" in source_text,
        "broker_audited_successful_skill_call": _broker_skill_succeeded(
            audit.events, "human-guidance"
        ),
        "exactly_one_transport_audited_skill_call": (
            harness.transport.successful_skill_invocation_count(
                call_id, "human-guidance"
            )
            == 1
        ),
        "configured_model_and_effort_were_used": model_audit[
            "all_calls_audited_as_expected_gpt_6_astra"
        ],
        "no_unscheduled_collaboration_tool_activity": not _has_collaboration_activity(
            tool_activity
        ),
        "second_sprint_not_called": not _staged_skill_artifacts(
            workspace, "discovery-sprint"
        ),
    }
    observation.update(
        {
            "status": "passed" if all(checks.values()) else "failed",
            "checks": checks,
            "response": response,
            "guidance_artifacts": artifacts,
            "broker_audit": audit.events,
            "model_audit": model_audit,
            "tool_activity": tool_activity,
        }
    )
    return observation


def probe_cas(harness: ProbeHarness) -> dict[str, Any]:
    """Test a real computation worker using configured Sage and final progress."""

    name = "cas"
    call_id = "LIVE-WORKER-CAS"
    sage = shutil.which("sage")
    if sage is None:
        raise RuntimeError("the live CAS probe requires configured Sage")
    policy = policy_for("worker", mode="computation")
    card = _task_card(
        task_id="T-LIVE-CAS",
        mode="computation",
        objective=(
            "Use the configured Sage capability to compute exactly which integers n from "
            "1 through 12 make n^2+n even. Preserve the exact reproducible computation, "
            "interpret it only as finite evidence, and finish through record-progress with "
            "the CAS operation attached."
        ),
        main_obligation_ids=("O-LIVE-CAS",),
    )
    workspace = harness.materializer.create(
        call_id,
        root_problem=ROOT_PROBLEM,
        policy=policy,
        task_card=card,
        skills=sorted(allowed_skills(policy)),
    )
    observation = _base_observation(
        harness, name=name, call_id=call_id, workspace=workspace.path
    )
    audit = AuditLog(harness.root / "broker-cas.jsonl")
    broker = MemoryBroker(
        InMemoryBackend(
            [
                MemoryRecord(
                    "O-LIVE-CAS",
                    "obligation",
                    "Inspect the parity of n^2+n on a finite range.",
                    "Compute n^2+n for n from 1 through 12.",
                )
            ]
        ),
        audit=audit,
    )
    broker.start(harness.private / "cas-memory.sock")
    binding = None
    try:
        binding = _issue_probe_binding(
            broker,
            policy=policy,
            call_id=call_id,
            workspace=workspace,
            cas_executables={"sage": sage},
        )
        result, response = harness.invoke(
            call_id=call_id,
            role="worker",
            mode="computation",
            policy=policy,
            workspace=workspace,
            schema_name="worker",
            broker_binding=binding,
        )
    finally:
        if binding is not None:
            broker.revoke(binding)
        broker.stop()

    computations = _staged_skill_artifacts(workspace, "CAS")
    progress = _staged_skill_artifacts(workspace, "record-progress")
    successful_computations = [
        item for item in computations if item.get("exit_status") == 0
    ]
    computation = (
        successful_computations[0] if len(successful_computations) == 1 else {}
    )
    final_progress = [item for item in progress if item.get("is_final")]
    attached_ids = (
        final_progress[0].get("computation_operation_ids", [])
        if len(final_progress) == 1
        else []
    )
    model_audit = _call_model_audit(
        harness,
        (call_id,),
        expected_efforts={call_id: "max"},
    )
    tool_activity = harness.tool_activity(call_id)
    checks = {
        "transport_returncode_zero": result.returncode == 0,
        "at_least_one_cas_artifact": bool(computations),
        "exactly_one_successful_cas_artifact": len(successful_computations) == 1,
        "cas_used_configured_sage": computation.get("software") == "sage",
        "cas_process_succeeded": computation.get("exit_status") == 0,
        "cas_output_is_nonempty": bool(str(computation.get("exact_output") or "").strip()),
        "exactly_one_final_record_progress": len(final_progress) == 1,
        "final_progress_attaches_cas_operation": computation.get("operation_id")
        in attached_ids,
        "worker_final_response_matches_progress": response.get("attempt_ended") is True
        and len(final_progress) == 1
        and response.get("final_progress_id") == final_progress[0].get("progress_id"),
        "broker_audited_cas": _broker_skill_succeeded(audit.events, "CAS"),
        "broker_audited_final_progress": _broker_skill_succeeded(
            audit.events, "record-progress"
        ),
        "transport_audited_cas_calls_match_artifacts": (
            harness.transport.successful_skill_invocation_count(call_id, "CAS")
            == len(computations)
        ),
        "exactly_one_transport_audited_final_progress_call": (
            harness.transport.successful_skill_invocation_count(
                call_id, "record-progress"
            )
            == 1
        ),
        "configured_model_and_effort_were_used": model_audit[
            "all_calls_audited_as_expected_gpt_6_astra"
        ],
        "no_unscheduled_collaboration_tool_activity": not _has_collaboration_activity(
            tool_activity
        ),
    }
    observation.update(
        {
            "status": "passed" if all(checks.values()) else "failed",
            "checks": checks,
            "response": response,
            "cas_artifacts": computations,
            "progress_artifacts": progress,
            "broker_audit": audit.events,
            "model_audit": model_audit,
            "tool_activity": tool_activity,
        }
    )
    return observation


def _repair_stop_manifest(harness: ProbeHarness) -> Path:
    """Create the disposable project's manifest with the Design model defaults."""

    path = harness.root / "repair-stop-bootstrap.toml"
    path.write_text(
        "\n".join(
            [
                "[project]",
                'name = "live-repair-stop-probe"',
                'directory = "repair-stop-project"',
                f"root_problem = {json.dumps(ROOT_PROBLEM)}",
                f"foundation_policy = {json.dumps(FOUNDATION_POLICY)}",
                "",
                "[models.default]",
                'model = "gpt-6-astra"',
                'reasoning_effort = "ultra"',
                "",
                "[models.synthesizer]",
                'model = "gpt-6-astra"',
                'reasoning_effort = "xhigh"',
                "",
                "[context_budgets]",
                "main_portfolio_tokens = 60000",
                "worker_portfolio_tokens = 40000",
                "summarizer_input_tokens = 60000",
                "",
                "[tools]",
                f"codex = {json.dumps(harness.codex_binary)}",
                "",
                "[agents]",
                "native_web_search = true",
                "",
                "[initial]",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return path


def _start_repair_probe_services(runtime: FrantaRuntime) -> Path | None:
    """Start the production broker, shortening only an overlong Unix socket path."""

    default_socket = runtime.layout.private / "memory-broker.sock"
    if len(os.fsencode(str(default_socket))) < 90:
        runtime.start_services()
        return None
    temporary_parent = "/private/tmp" if Path("/private/tmp").is_dir() else "/tmp"
    short_root = Path(
        tempfile.mkdtemp(prefix="drs-broker-", dir=temporary_parent)
    )
    runtime.broker.start(short_root / "b.sock")
    runtime._services_started = True
    return short_root


def _seed_repair_predecessor(
    runtime: FrantaRuntime,
    *,
    root_obligation_id: str,
) -> dict[str, Any]:
    """Publish one supporting lemma through the verified scheduler path."""

    task_id = runtime.scheduler.submit_batch(
        "BATCH-LIVE-REPAIR-PREDECESSOR",
        [
            {
                "report_id": "AR-LIVE-REPAIR-PREDECESSOR",
                "objective": (
                    "Prove the maximal-trail parity lemma used by the repair fixture."
                ),
                "if_resume": None,
                "mode": "associate",
                "main_route_ids": [],
                "main_obligation_ids": [root_obligation_id],
                "perspective": None,
                "portfolio": _empty_portfolio(),
                "reason": "Create a complete non-vacuous predecessor verifier record.",
            }
        ],
    )[0]
    attempt = runtime.scheduler.start_task_attempt(task_id)
    operation = {
        "operation_id": REPAIR_PREDECESSOR_OPERATION_ID,
        "kind": "fact",
        "proposal_id": REPAIR_PREDECESSOR_PROPOSAL_ID,
        "candidate_id": REPAIR_PREDECESSOR_CANDIDATE_ID,
        "candidate_version": 1,
        "statement": (
            "In a finite graph in which every vertex has even degree, a maximal trail "
            "starting at a vertex v ends at v."
        ),
        "proof": (
            "At each vertex other than v, every arrival along a previously unused edge "
            "uses one incident edge. Since the total degree is even, another unused "
            "incident edge remains whenever the trail is about to stop there. Thus a "
            "maximal trail cannot first stop away from v. Finiteness makes the trail "
            "finite, so it ends at v."
        ),
        "predecessor_fact_ids": [],
        "originating_task_id": task_id,
        "foundation_policy_version": 1,
        "introduced_notation": [
            {
                "symbol": "v",
                "definition": "the starting vertex of the trail",
                "scope": "this lemma",
            }
        ],
        "external_references": [],
        "root_resolution": None,
        "abstract": (
            "A maximal trail in a finite even-degree graph returns to its starting vertex "
            "by an unused-edge parity argument."
        ),
        "keywords": ["maximal trail", "even degree", "parity"],
        "related_route_ids": [],
    }
    runtime.scheduler.ingest_progress(
        {
            "progress_id": "PRG-LIVE-REPAIR-PREDECESSOR",
            "task_id": task_id,
            "attempt": attempt,
            "sequence": 1,
            "is_final": True,
            "operations": [operation],
            "completion_evidence_ids": [REPAIR_PREDECESSOR_OPERATION_ID],
            "outcome_status": "finished",
            "attempt_summary": {
                "work_mode": "associate",
                "task": "Prove the maximal-trail parity lemma.",
                "proposed_outcome": "finished",
                "cumulative_important_progress": (
                    "Proved the supporting lemma by parity at the endpoint."
                ),
                "completion_evidence_operation_ids": [
                    REPAIR_PREDECESSOR_OPERATION_ID
                ],
                "most_promising_next_steps": (
                    "Use the lemma in the deliberately flawed root candidate."
                ),
            },
        }
    )
    synthesis = _accept_new_synthesis(
        runtime, REPAIR_PREDECESSOR_OPERATION_ID
    )
    bundle = runtime.scheduler.verification_bundle(
        REPAIR_PREDECESSOR_OPERATION_ID
    )
    report = {
        "verdict": "correct",
        **{
            field: bundle[field]
            for field in _VERIFIER_CONFIRMATION_FIELDS
        },
        "errors": [],
    }
    canonical_id = runtime.scheduler.apply_verifier_report(
        REPAIR_PREDECESSOR_OPERATION_ID, report
    )
    if not canonical_id:
        raise RuntimeError("could not publish the verified repair predecessor")
    state = runtime.scheduler.state
    if state["tasks"][task_id]["state"] != TaskState.CLOSED.value:
        raise RuntimeError("verified repair-predecessor task did not close")
    return {
        "fact_id": str(canonical_id),
        "task_id": task_id,
        "operation_id": REPAIR_PREDECESSOR_OPERATION_ID,
        "synthesis_report": synthesis,
        "verification_bundle": bundle,
        "verification_report": report,
    }


def _repair_assignment(root_obligation_id: str, predecessor_id: str) -> dict[str, Any]:
    return {
        "report_id": "AR-LIVE-ROOT-REPAIR",
        "objective": (
            "Prove ROOT. If the verifier rejects the seeded first version, use the immutable "
            "verifier-revision supplement to repair exactly its gap. Submit a fresh Fact "
            f"candidate {REPAIR_V2_CANDIDATE_ID} at version 1, retain cited predecessor "
            f"{predecessor_id}, use fresh temporary proposal ID {REPAIR_V2_PROPOSAL_ID}, "
            "preserve the supplement's repair chain and prior verifier report, declare the "
            "ROOT outcome, and "
            "finish through record-progress. Do not merely restate the verifier's objection."
        ),
        "if_resume": None,
        "mode": "associate",
        "main_route_ids": [],
        "main_obligation_ids": [root_obligation_id],
        "perspective": None,
        "portfolio": {
            "fact": [predecessor_id],
            "route": [],
            "memo": [],
            "claim": [],
            "obligation": [],
            "computation": [],
        },
        "reason": "Exercise one real verifier-guided repair on a root proof.",
    }


def _seed_flawed_root_candidate(
    runtime: FrantaRuntime,
    *,
    task_id: str,
    predecessor_id: str,
) -> None:
    """Seed a deliberately invalid first version without consuming a model turn."""

    attempt = runtime.scheduler.start_task_attempt(task_id)
    operation = {
        "operation_id": REPAIR_V1_OPERATION_ID,
        "kind": "fact",
        "proposal_id": REPAIR_V1_PROPOSAL_ID,
        "candidate_id": REPAIR_V1_CANDIDATE_ID,
        "candidate_version": 1,
        "statement": ROOT_PROBLEM,
        "proof": (
            f"Fix a connected component containing an edge. By {predecessor_id}, a maximal "
            "trail from an incident vertex is closed. Since this trail is closed, it contains "
            "every edge of the component and is therefore an Eulerian circuit. Apply this to "
            "each component."
        ),
        "predecessor_fact_ids": [predecessor_id],
        "originating_task_id": task_id,
        "foundation_policy_version": 1,
        "introduced_notation": [],
        "external_references": [],
        "root_resolution": {"target": "ROOT", "outcome": "proved"},
        "abstract": (
            "Every finite connected even-degree graph has an Eulerian circuit, purportedly "
            "from one maximal closed trail."
        ),
        "keywords": ["Eulerian circuit", "maximal trail", "even degree"],
        "related_route_ids": [],
    }
    runtime.scheduler.ingest_progress(
        {
            "progress_id": "PRG-LIVE-ROOT-REPAIR-V1",
            "task_id": task_id,
            "attempt": attempt,
            "sequence": 1,
            "is_final": True,
            "operations": [operation],
            "completion_evidence_ids": [REPAIR_V1_OPERATION_ID],
            "outcome_status": "finished",
            "attempt_summary": {
                "work_mode": "associate",
                "task": "Prove ROOT.",
                "proposed_outcome": "finished",
                "cumulative_important_progress": (
                    "Seeded the deliberately flawed first root-proof version for live review."
                ),
                "completion_evidence_operation_ids": [REPAIR_V1_OPERATION_ID],
                "most_promising_next_steps": "Apply the verifier's exact repair request.",
            },
        }
    )


def _accept_new_synthesis(runtime: FrantaRuntime, operation_id: str) -> dict[str, Any]:
    """Use the production scheduler boundary but no synthesizer model turn."""

    operation = runtime.scheduler.state["operations"][operation_id]
    report = {
        "resolution": "new",
        "operation_digest": operation["input_digest"],
        "relied_on": [],
    }
    runtime.scheduler.apply_synthesizer_result(operation_id, report)
    return report


_VERIFIER_CONFIRMATION_FIELDS = (
    "candidate_id",
    "candidate_version",
    "operation_id",
    "bundle_digest",
    "verifier_attempt_id",
    "predecessor_ids",
    "introduced_notation",
    "external_references",
    "root_resolution",
)


def _verifier_envelope_checks(
    bundle: Mapping[str, Any],
    response: Mapping[str, Any],
    *,
    predecessor_id: str,
    expected_verdict: str,
) -> dict[str, bool]:
    predecessor_records = bundle.get("predecessor_records")
    immutable_core = {
        "id",
        "statement",
        "proof",
        "predecessor_fact_ids",
        "originating_task_id",
        "foundation_policy_version",
        "introduced_notation",
        "external_references",
        "root_resolution",
    }
    full_predecessor = (
        isinstance(predecessor_records, list)
        and len(predecessor_records) == 1
        and predecessor_records[0].get("id") == predecessor_id
        and immutable_core <= set(predecessor_records[0])
        and bool(predecessor_records[0].get("statement"))
        and bool(predecessor_records[0].get("proof"))
    )
    return {
        "verdict_matches_expected": response.get("verdict") == expected_verdict,
        "all_exact_envelope_fields_match": all(
            response.get(field) == bundle.get(field)
            for field in _VERIFIER_CONFIRMATION_FIELDS
        ),
        "complete_cited_predecessor_record_supplied": full_predecessor,
        "incorrect_has_detailed_errors": (
            expected_verdict != "incorrect"
            or bool(response.get("errors"))
        ),
        "correct_has_no_errors": (
            expected_verdict != "correct"
            or response.get("errors") == []
        ),
    }


def _ordered_repair_events(
    events: Sequence[Mapping[str, Any]],
    *,
    repair_operation_id: str,
    repair_progress_id: str,
) -> dict[str, Any]:
    expected = (
        ("seed_ready", "fact_ready_for_synthesis", REPAIR_V1_OPERATION_ID, None),
        (
            "seed_synthesized",
            "synthesizer_resolution_received",
            REPAIR_V1_OPERATION_ID,
            None,
        ),
        (
            "seed_bundle",
            "verification_bundle_materialized",
            REPAIR_V1_OPERATION_ID,
            None,
        ),
        ("seed_rejected", "fact_verified_incorrect", REPAIR_V1_OPERATION_ID, None),
        ("fresh_repair_progress", "progress_received", None, repair_progress_id),
        (
            "fresh_repair_synthesized",
            "synthesizer_resolution_received",
            repair_operation_id,
            None,
        ),
        (
            "fresh_repair_bundle",
            "verification_bundle_materialized",
            repair_operation_id,
            None,
        ),
        (
            "fresh_repair_verified",
            "fact_verified_correct",
            repair_operation_id,
            None,
        ),
        (
            "fresh_repair_published",
            "verified_fact_published",
            repair_operation_id,
            None,
        ),
        ("root_resolved", "root_resolution_first", repair_operation_id, None),
        ("terminal_main", "terminal_main_decision", None, None),
    )
    positions: dict[str, int] = {}
    event_ids: dict[str, Any] = {}
    cursor = -1
    for label, event_type, operation_id, progress_id in expected:
        found = None
        for index in range(cursor + 1, len(events)):
            event = events[index]
            payload = event.get("payload") or {}
            if event.get("type") != event_type:
                continue
            if operation_id is not None and payload.get("operation_id") != operation_id:
                continue
            if progress_id is not None and payload.get("progress_id") != progress_id:
                continue
            found = index
            break
        if found is None:
            continue
        cursor = found
        positions[label] = found
        event_ids[label] = events[found].get("event_id")
    return {
        "positions": positions,
        "event_ids": event_ids,
        "complete_and_ordered": len(positions) == len(expected),
        "expected_labels": [item[0] for item in expected],
    }


def _call_model_audit(
    harness: ProbeHarness,
    call_ids: Sequence[str],
    *,
    expected_efforts: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    expected_by_call = {
        str(call_id): str(effort)
        for call_id, effort in (expected_efforts or {}).items()
    }
    rows: dict[str, list[dict[str, Any]]] = {}
    for call_id in call_ids:
        rows[call_id] = [
            item
            for item in _read_jsonl(harness.state / "calls" / f"{call_id}.jsonl")
            if item.get("type") == "transport.call_started"
        ]
    return {
        "call_started_rows": rows,
        "expected_efforts": {
            call_id: expected_by_call.get(call_id, "ultra") for call_id in call_ids
        },
        "all_calls_audited_as_expected_gpt_6_astra": all(
            values
            and all(
                item.get("model") == "gpt-6-astra"
                and item.get("reasoning_effort")
                == expected_by_call.get(call_id, "ultra")
                for item in values
            )
            for call_id, values in rows.items()
        ),
    }


def _has_collaboration_activity(items: Sequence[Mapping[str, Any]]) -> bool:
    markers = ("spawn_agent", "wait_agent", "functions.collaboration", "collab")
    for item in items:
        text = json.dumps(dict(item), ensure_ascii=False).casefold()
        if any(marker in text for marker in markers):
            return True
    return False


def probe_repair_stop(
    harness: ProbeHarness,
    *,
    executor: AgentExecutor | None = None,
) -> dict[str, Any]:
    """Run a real reject/repair/accept/root-stop path through FrantaRuntime."""

    name = "repair-stop"
    manifest = load_manifest(_repair_stop_manifest(harness))
    runtime = FrantaRuntime.initialize(
        manifest,
        executor=executor,
        transport=harness.transport,
    )
    observation: dict[str, Any] = {
        "probe": name,
        "project_dir": str(runtime.layout.root),
        "deterministic_synthesis": True,
    }
    short_broker_root: Path | None = None
    try:
        with runtime.lock:
            short_broker_root = _start_repair_probe_services(runtime)
            runtime.scheduler.commit_initial_trim({"category_ids": []})
            root_obligation_id = str(
                runtime.scheduler.state["root"]["obligation_id"]
            )
            predecessor_fixture = _seed_repair_predecessor(
                runtime,
                root_obligation_id=root_obligation_id,
            )
            predecessor_id = str(predecessor_fixture["fact_id"])
            task_id = runtime.scheduler.submit_batch(
                "BATCH-LIVE-ROOT-REPAIR",
                [_repair_assignment(root_obligation_id, predecessor_id)],
            )[0]

            _seed_flawed_root_candidate(
                runtime,
                task_id=task_id,
                predecessor_id=predecessor_id,
            )
            first_synthesis = _accept_new_synthesis(
                runtime, REPAIR_V1_OPERATION_ID
            )
            first_bundle = runtime.scheduler.verification_bundle(
                REPAIR_V1_OPERATION_ID
            )
            first_verifier_call = runtime.scheduler.prepare_verifier_call(
                REPAIR_V1_OPERATION_ID
            )
            runtime._run_memory_review_call(first_verifier_call)
            first_response = runtime.scheduler.state["calls"][
                first_verifier_call
            ]["result"]
            rejected_state = runtime.scheduler.state
            revision_supplement = rejected_state["tasks"][task_id].get(
                "pending_attempt_supplement"
            )

            runtime._run_worker_batch([task_id])
            post_worker_state = runtime.scheduler.state
            repair_attempt = post_worker_state["tasks"][task_id]["attempts"][-1]
            staged_repair_operations = [
                operation
                for operation in post_worker_state["operations"].values()
                if operation.get("task_id") == task_id
                and int(operation.get("attempt", -1))
                == int(repair_attempt.get("attempt", -2))
                and operation.get("candidate_id") == REPAIR_V2_CANDIDATE_ID
                and operation.get("payload", {}).get("proposal_id")
                == REPAIR_V2_PROPOSAL_ID
            ]
            if len(staged_repair_operations) != 1:
                raise RuntimeError(
                    "repair worker did not stage exactly one fresh repair Fact"
                )
            second_operation = staged_repair_operations[0]
            second_operation_id = str(second_operation["operation_id"])
            if second_operation["state"] != OperationState.VERIFYING.value:
                raise RuntimeError(
                    "fresh repair Fact did not finish live synthesis"
                )
            second_synthesizer_call = str(
                second_operation.get("synthesizer_call_id") or ""
            )
            second_synthesis = copy.deepcopy(
                post_worker_state["calls"].get(second_synthesizer_call, {}).get(
                    "result"
                )
                or {}
            )
            repair_call_id = str(repair_attempt["call_id"])
            repair_workspace = runtime.layout.workspaces / repair_call_id
            repair_response = copy.deepcopy(
                post_worker_state["calls"][repair_call_id].get("result") or {}
            )
            repair_artifacts = _read_artifacts(
                repair_workspace, "record-progress"
            )
            repair_receipts = _skill_receipt_checks(
                repair_workspace,
                skill="record-progress",
                artifacts=repair_artifacts,
            )
            final_repair_artifacts = [
                item for item in repair_artifacts if item.get("is_final") is True
            ]
            second_progress_id = str(second_operation["progress_id"])
            repair_final_progress_id = (
                str(final_repair_artifacts[0].get("progress_id"))
                if len(final_repair_artifacts) == 1
                else ""
            )

            second_bundle = runtime.scheduler.verification_bundle(
                second_operation_id
            )
            second_verifier_call = runtime.scheduler.prepare_verifier_call(
                second_operation_id
            )
            runtime._run_memory_review_call(second_verifier_call)
            accepted_state = runtime.scheduler.state
            second_response = accepted_state["calls"][second_verifier_call][
                "result"
            ]
            canonical_fact_id = accepted_state["operations"][
                second_operation_id
            ].get("canonical_id")
            if not canonical_fact_id:
                raise RuntimeError("second verifier did not publish the repaired fact")

            root_event_id = accepted_state["root"].get("resolution_event_id")
            tasks_at_resolution = set(accepted_state["tasks"])
            calls_before_main = set(accepted_state["calls"])
            runtime._run_main()
            final_state = runtime.scheduler.state
            rejected_lineage = final_state["fact_lineages"][
                REPAIR_V1_CANDIDATE_ID
            ]
            resolved_repair_lineage = final_state["fact_lineages"][
                REPAIR_V2_CANDIDATE_ID
            ]
            rejected_proposal = final_state["fact_proposals"][
                REPAIR_V1_PROPOSAL_ID
            ]
            resolved_repair_proposal = final_state["fact_proposals"][
                REPAIR_V2_PROPOSAL_ID
            ]
            new_main_calls = [
                call_id
                for call_id in set(final_state["calls"]) - calls_before_main
                if final_state["calls"][call_id].get("kind") == "main"
            ]
            if len(new_main_calls) != 1:
                raise RuntimeError("terminal handling did not create exactly one main call")
            main_call_id = new_main_calls[0]
            main_response = final_state["calls"][main_call_id]["result"]
            main_workspace = runtime.layout.workspaces / main_call_id
            main_artifacts = _read_artifacts(main_workspace, "task-writing")
            main_receipts = _skill_receipt_checks(
                main_workspace,
                skill="task-writing",
                artifacts=main_artifacts,
            )

            task_ids_after_resolution = sorted(
                set(final_state["tasks"]) - tasks_at_resolution
            )
            post_resolution_tasks = [
                final_state["tasks"][item] for item in task_ids_after_resolution
            ]
            proof_writer_tasks = [
                item
                for item in post_resolution_tasks
                if item["task_card"].get("mode") == "proof-writer"
            ]
            declined_writer = main_response.get("decline_proof_writer") is True
            terminal_branch_valid = (
                declined_writer
                and not post_resolution_tasks
                and not main_artifacts
                and final_state["gate"] == GateState.COMPLETED.value
            ) or (
                not declined_writer
                and len(post_resolution_tasks) == 1
                and len(proof_writer_tasks) == 1
                and len(main_artifacts) == 1
                and final_state["gate"] == GateState.RESOLUTION_PENDING.value
            )

            first_checks = _verifier_envelope_checks(
                first_bundle,
                first_response,
                predecessor_id=predecessor_id,
                expected_verdict="incorrect",
            )
            second_checks = _verifier_envelope_checks(
                second_bundle,
                second_response,
                predecessor_id=predecessor_id,
                expected_verdict="correct",
            )
            event_order = _ordered_repair_events(
                final_state["events"],
                repair_operation_id=second_operation_id,
                repair_progress_id=second_progress_id,
            )
            model_audit = _call_model_audit(
                harness,
                (
                    first_verifier_call,
                    repair_call_id,
                    second_synthesizer_call,
                    second_verifier_call,
                    main_call_id,
                ),
                expected_efforts={
                    repair_call_id: "max",
                    second_synthesizer_call: "xhigh",
                },
            )
            single_agent_preflight = [
                item
                for item in _read_jsonl(
                    harness.state / "single-agent-preflight.jsonl"
                )
                if item.get("status") == "passed"
                and item.get("model") == "gpt-6-astra"
            ]
            preflight_efforts = {
                str(item.get("reasoning_effort")) for item in single_agent_preflight
            }
            relevant_call_ids = {
                first_verifier_call,
                repair_call_id,
                second_synthesizer_call,
                second_verifier_call,
                main_call_id,
            }
            tool_activity = [
                item
                for item in _read_jsonl(harness.state / "tool_activity.jsonl")
                if item.get("call_id") in relevant_call_ids
            ]
            broker_audit = list(runtime.read_audit.events)
            verifier_actions = [
                item
                for item in broker_audit
                if item.get("caller_id")
                in {first_verifier_call, second_verifier_call}
            ]
            verifier_manifests = [
                json.loads(
                    (
                        runtime.layout.workspaces
                        / call_id
                        / "input"
                        / "access_policy.json"
                    ).read_text(encoding="utf-8")
                )
                for call_id in (first_verifier_call, second_verifier_call)
            ]
            repair_card = json.loads(
                (repair_workspace / "input" / "task_card.json").read_text(
                    encoding="utf-8"
                )
            )
            repair_operations = [
                operation
                for artifact in repair_artifacts
                for operation in artifact.get("operations", [])
                if isinstance(operation, Mapping)
            ]
            main_assignment_ids = [
                str(item)
                for item in main_response.get("assignment_report_ids", [])
            ]
            main_artifact_ids = [
                str(item.get("operation_id")) for item in main_artifacts
            ]
            events_after_root = [
                event
                for event in final_state["events"]
                if root_event_id is not None
                and int(event.get("event_id", 0)) > int(root_event_id)
            ]
            post_root_research_launches = [
                event
                for event in events_after_root
                if event.get("type") == "task_launch_intent_committed"
                and final_state["tasks"]
                .get(str((event.get("payload") or {}).get("task_id")), {})
                .get("task_card", {})
                .get("mode")
                != "proof-writer"
            ]
            predecessor_record = runtime.store.get(predecessor_id).to_dict()
            predecessor_operation = final_state["operations"][
                REPAIR_PREDECESSOR_OPERATION_ID
            ]
            predecessor_task = final_state["tasks"][
                str(predecessor_fixture["task_id"])
            ]
            predecessor_task_publication = runtime.store.operation_status(
                f"task-close:{predecessor_fixture['task_id']}"
            )

            checks = {
                "predecessor_fixture_used_verified_scheduler_path": (
                    predecessor_operation.get("state")
                    == OperationState.COMMITTED.value
                    and predecessor_operation.get("verified_correct") is True
                    and predecessor_operation.get("verifier_report")
                    == predecessor_fixture["verification_report"]
                    and predecessor_task.get("state") == TaskState.CLOSED.value
                    and predecessor_record.get("originating_task_id")
                    == predecessor_fixture["task_id"]
                    and predecessor_task_publication is not None
                    and predecessor_task_publication.status == "committed"
                ),
                "seeded_rejected_fact_is_mathematically_flawed": (
                    "closed, it contains every edge"
                    in str(first_bundle.get("proof", ""))
                ),
                "first_verifier_rejected_exact_envelope": all(
                    first_checks.values()
                ),
                "scheduler_created_exact_verifier_revision_supplement": (
                    isinstance(revision_supplement, Mapping)
                    and revision_supplement.get("kind") == "verifier_revision"
                    and revision_supplement.get("candidate_id")
                    == REPAIR_V1_CANDIDATE_ID
                    and revision_supplement.get("proposal_id")
                    == REPAIR_V1_PROPOSAL_ID
                    and revision_supplement.get("operation_id")
                    == REPAIR_V1_OPERATION_ID
                    and revision_supplement.get("repair_chain_id")
                    == REPAIR_V1_PROPOSAL_ID
                    and revision_supplement.get("revision_request") == 1
                    and revision_supplement.get("fresh_proposal_required") is True
                    and revision_supplement.get("fresh_candidate_required") is True
                    and revision_supplement.get("candidate_version") == 1
                    and revision_supplement.get("verification_report")
                    == first_response
                ),
                "worker_received_immutable_revision_supplement": (
                    repair_card.get("attempt_supplement") == revision_supplement
                    and repair_attempt.get("supplement") == revision_supplement
                ),
                "worker_staged_fresh_repair_fact": (
                    second_operation.get("candidate_id")
                    == REPAIR_V2_CANDIDATE_ID
                    and second_operation.get("candidate_version") == 1
                    and second_operation["payload"].get("proposal_id")
                    == REPAIR_V2_PROPOSAL_ID
                    and second_operation.get("repair_chain_id")
                    == REPAIR_V1_PROPOSAL_ID
                    and second_operation.get("repair_request") == 1
                    and second_operation["payload"].get("predecessor_fact_ids")
                    == [predecessor_id]
                    and second_operation["payload"].get("root_resolution")
                    == {"target": "ROOT", "outcome": "proved"}
                ),
                "worker_used_final_record_progress": (
                    repair_response.get("final_progress_id")
                    == repair_final_progress_id
                    and len(final_repair_artifacts) == 1
                    and repair_response.get("final_progress_id")
                    == final_repair_artifacts[0].get("progress_id")
                    and repair_receipts[
                        "all_artifacts_have_hash_matched_receipts"
                    ]
                    and harness.transport.successful_skill_invocation_count(
                        repair_call_id, "record-progress"
                    )
                    == len(repair_artifacts)
                ),
                "deterministic_synthesis_accepted_fixture_seed_and_fresh_repair": (
                    predecessor_fixture["synthesis_report"]["resolution"]
                    == "new"
                    and first_synthesis["resolution"] == "new"
                    and second_synthesis["resolution"] == "new"
                    and final_state["calls"][second_synthesizer_call].get("kind")
                    == "synthesizer"
                    and final_state["calls"][second_synthesizer_call].get("status")
                    == CallState.COMMITTED.value
                ),
                "fresh_repair_verifier_accepted_exact_envelope": all(
                    second_checks.values()
                ),
                "fresh_repair_verifier_received_prior_rejection": (
                    second_bundle.get("prior_verification_report")
                    == first_response
                ),
                "seed_and_repair_verifiers_received_exact_stored_predecessor": (
                    first_bundle.get("predecessor_records")
                    == [predecessor_record]
                    and second_bundle.get("predecessor_records")
                    == [predecessor_record]
                ),
                "repaired_fact_was_published_as_root_solution": (
                    final_state["root"].get("solution_fact_id")
                    == canonical_fact_id
                    and final_state["root"].get("outcome") == "proved"
                    and final_state["root"].get("obligation_status")
                    == "resolved"
                ),
                "fresh_repair_lineages_preserve_reject_then_repair": (
                    rejected_lineage.get("revision_requests") == 1
                    and rejected_lineage.get("versions")
                    == {"1": REPAIR_V1_OPERATION_ID}
                    and rejected_lineage.get("repair_chain_id")
                    == REPAIR_V1_PROPOSAL_ID
                    and rejected_lineage.get("closed") is True
                    and rejected_lineage.get("concession_required") is False
                    and resolved_repair_lineage.get("revision_requests") == 1
                    and resolved_repair_lineage.get("versions")
                    == {"1": second_operation_id}
                    and resolved_repair_lineage.get("repair_chain_id")
                    == REPAIR_V1_PROPOSAL_ID
                    and resolved_repair_lineage.get("closed") is True
                    and resolved_repair_lineage.get("concession_required") is False
                    and rejected_proposal.get("state") == "rejected"
                    and resolved_repair_proposal.get("state") == "resolved"
                    and resolved_repair_proposal.get("canonical_id")
                    == canonical_fact_id
                ),
                "fresh_repair_closes_source_task_without_stale_intent": (
                    final_state["tasks"][task_id].get("state")
                    == TaskState.CLOSED.value
                    and "pending_attempt_supplement"
                    not in final_state["tasks"][task_id]
                    and len(final_state["tasks"][task_id].get("attempts", [])) == 2
                ),
                "fresh_repair_event_sequence_is_complete_and_ordered": (
                    event_order["complete_and_ordered"]
                ),
                "main_made_terminal_decision": (
                    main_response.get("decision") == "terminal"
                    and final_state["root"].get("terminal_main_decision_done")
                    is True
                    and terminal_branch_valid
                ),
                "terminal_task_writing_branch_is_exact": (
                    (
                        declined_writer
                        and not main_assignment_ids
                        and not main_artifact_ids
                        and harness.transport.successful_skill_invocation_count(
                            main_call_id, "task-writing"
                        )
                        == 0
                    )
                    or (
                        not declined_writer
                        and main_assignment_ids == main_artifact_ids
                        and main_receipts[
                            "all_artifacts_have_hash_matched_receipts"
                        ]
                        and harness.transport.successful_skill_invocation_count(
                            main_call_id, "task-writing"
                        )
                        == 1
                    )
                ),
                "no_post_resolution_research_was_launched": (
                    not post_root_research_launches
                    and all(
                        item["task_card"].get("mode") == "proof-writer"
                        for item in post_resolution_tasks
                    )
                ),
                "verifier_access_is_fact_only_and_audited": (
                    all(
                        manifest.get("allowed_memory_types") == ["fact"]
                        and manifest.get("project_memory_api") is True
                        and not manifest.get("direct_canonical_mount")
                        and not manifest.get("task_summary_api")
                        and not manifest.get("task_artifact_api")
                        for manifest in verifier_manifests
                    )
                    and all(
                        item.get("action") != "internal_search"
                        or item.get("memory_types") == ["fact"]
                        for item in verifier_actions
                    )
                    and all(
                        item.get("action") != "memory_fetch"
                        or str(item.get("memory_id", "")).startswith("F-")
                        for item in verifier_actions
                    )
                ),
                "worker_and_main_keep_project_wide_memory_access": (
                    policy_for("worker", mode="associate").project_memory_api
                    and policy_for("main").project_memory_snapshot
                    and not policy_for("main").project_memory_api
                    and not policy_for(
                        "worker", mode="associate"
                    ).direct_canonical_mount
                    and not policy_for("main").direct_canonical_mount
                ),
                "all_real_roles_used_configured_model_and_effort": model_audit[
                    "all_calls_audited_as_expected_gpt_6_astra"
                ],
                "single_agent_surface_preflight_passed": {
                    "max",
                    "ultra",
                }.issubset(preflight_efforts),
                "no_unscheduled_collaboration_tool_activity": not _has_collaboration_activity(
                    tool_activity
                ),
                "only_one_repair_fact_operation_was_staged": (
                    len(
                        [
                            item
                            for item in repair_operations
                            if item.get("kind") == "fact"
                        ]
                    )
                    == 1
                ),
            }
            observation.update(
                {
                    "status": "passed" if all(checks.values()) else "failed",
                    "checks": checks,
                    "task_id": task_id,
                    "predecessor_fact_id": predecessor_id,
                    "predecessor_fixture": predecessor_fixture,
                    "canonical_root_fact_id": canonical_fact_id,
                    "operation_ids": {
                        "rejected_seed": REPAIR_V1_OPERATION_ID,
                        "fresh_repair": second_operation_id,
                    },
                    "call_ids": {
                        "seed_verifier": first_verifier_call,
                        "repair_worker": repair_call_id,
                        "fresh_repair_synthesizer": second_synthesizer_call,
                        "fresh_repair_verifier": second_verifier_call,
                        "terminal_main": main_call_id,
                    },
                    "synthesis_reports": [
                        predecessor_fixture["synthesis_report"],
                        first_synthesis,
                        second_synthesis,
                    ],
                    "seed_verifier": {
                        "bundle": first_bundle,
                        "response": first_response,
                        "checks": first_checks,
                    },
                    "revision_supplement": revision_supplement,
                    "repair_worker": {
                        "response": repair_response,
                        "task_card": repair_card,
                        "record_progress_artifacts": repair_artifacts,
                        "skill_receipts": repair_receipts,
                    },
                    "fresh_repair_verifier": {
                        "bundle": second_bundle,
                        "response": second_response,
                        "checks": second_checks,
                    },
                    "terminal_main": {
                        "response": main_response,
                        "task_writing_artifacts": main_artifacts,
                        "skill_receipts": main_receipts,
                        "post_resolution_task_ids": task_ids_after_resolution,
                    },
                    "event_order": event_order,
                    "events": final_state["events"],
                    "model_audit": model_audit,
                    "single_agent_preflight": single_agent_preflight,
                    "broker_audit": broker_audit,
                    "tool_activity": tool_activity,
                    "final_gate": final_state["gate"],
                }
            )
    finally:
        runtime.close()
        if short_broker_root is not None:
            try:
                short_broker_root.rmdir()
            except OSError:
                pass
    return observation


PROBES: dict[str, Callable[[ProbeHarness], dict[str, Any]]] = {
    "main": probe_main,
    "research": probe_research,
    "isolated": probe_isolated,
    "trimmer": probe_trimmer,
    "discovery-sprint": probe_discovery_sprint,
    "human-guidance": probe_human_guidance,
    "cas": probe_cas,
    "repair-stop": probe_repair_stop,
}


def _exception_observation(name: str, exc: BaseException) -> dict[str, Any]:
    result: dict[str, Any] = {
        "probe": name,
        "status": "error",
        "error_type": type(exc).__name__,
        "error": str(exc),
        "traceback": traceback.format_exc(),
    }
    if isinstance(exc, CodexTransportError) and exc.result is not None:
        result["transport_result"] = {
            "call_id": exc.result.call_id,
            "thread_id": exc.result.thread_id,
            "returncode": exc.result.returncode,
            "final_message": exc.result.final_message,
            "stderr": exc.result.stderr,
        }
    return result


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--probe",
        dest="probes",
        action="append",
        choices=PROBE_NAMES,
        help="run one named probe; repeat for several (default: all)",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        help="fresh directory for workspaces and observations (default: /tmp)",
    )
    parser.add_argument("--codex-binary", default="codex")
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=None,
        help="optional per-session transport timeout; omitted means no added timeout",
    )
    parser.add_argument(
        "--stop-on-error",
        action="store_true",
        help="do not launch later probes after an error or failed check",
    )
    parser.add_argument(
        "--compact",
        action="store_true",
        help="print compact JSON instead of indented JSON",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    selected = list(dict.fromkeys(args.probes or PROBE_NAMES))
    if args.output_dir is None:
        output_root = Path(
            tempfile.mkdtemp(prefix=f"franta-live-probe-{_utc_stamp()}-")
        ).resolve()
        harness_root = output_root / "run"
    else:
        output_root = args.output_dir.expanduser().resolve()
        output_root.mkdir(parents=True, exist_ok=False)
        harness_root = output_root / "run"

    harness = ProbeHarness(
        harness_root,
        codex_binary=args.codex_binary,
        timeout_seconds=args.timeout_seconds,
    )
    observations: list[dict[str, Any]] = []
    for name in selected:
        try:
            observation = PROBES[name](harness)
        except BaseException as exc:  # preserve diagnostics even for abrupt probe failures
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            observation = _exception_observation(name, exc)
        observations.append(observation)
        if args.stop_on_error and observation.get("status") != "passed":
            break

    summary = {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "project_root": str(PROJECT_ROOT),
        "output_root": str(output_root),
        "selected_probes": selected,
        "completed_probes": [item.get("probe") for item in observations],
        "overall_status": (
            "passed"
            if len(observations) == len(selected)
            and all(item.get("status") == "passed" for item in observations)
            else "failed"
        ),
        "observations": observations,
    }
    encoded = json.dumps(
        summary,
        ensure_ascii=False,
        indent=None if args.compact else 2,
        sort_keys=True,
    )
    (output_root / "observations.json").write_text(encoded + "\n", encoding="utf-8")
    print(encoded)
    return 0 if summary["overall_status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
