"""Agent-visible access, search, and staged-tool contracts.

The broker, filesystem materialization, permissions, and transport enforcement
remain in their owning implementation blocks.  These values describe the
immutable policy passed between those blocks.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Any, Iterable, Mapping


MEMORY_TYPES = frozenset(
    {"fact", "route", "memo", "claim", "obligation", "task", "computation"}
)
SEARCHABLE_MEMORY_TYPES = frozenset(MEMORY_TYPES - {"task"})
ORDINARY_SEARCH_MODES = frozenset(
    {"research", "associate", "reformulate", "computation"}
)
ISOLATED_MODES = frozenset({"brainstorm", "multi-discipline"})
SPRINT_LANES = frozenset({"A", "B", "C", "D"})
STAGING_TOOL_BY_SKILL = {
    "task-writing": "task_writing",
    "selection-report": "selection_report",
    "record-progress": "record_progress",
    "human-guidance": "human_guidance",
    "discovery-sprint": "discovery_sprint",
    "CAS": "execute_cas",
    "record-scratch": "record_scratch",
    "record-summary": "record_summary",
}
STAGING_SKILL_BY_TOOL = {tool: skill for skill, tool in STAGING_TOOL_BY_SKILL.items()}
CAS_TOOL_ARGUMENTS = frozenset(
    {
        "software",
        "arguments",
        "version_arguments",
        "exact_input",
        "description",
        "assumptions",
        "environment_versions",
        "random_seed",
        "interpretation",
        "related_ids",
        "fact_candidate_operation_ids",
        "output_artifact",
        "operation_id",
    }
)

_ID_RE = re.compile(r"^(?:F|R|M|CL|O|T|C)-[A-Za-z0-9][A-Za-z0-9_.:-]*$")


@dataclass(frozen=True)
class AccessPolicy:
    """The complete agent-visible access policy for one call."""

    name: str
    role: str
    mode: str | None = None
    sprint_lane: str | None = None
    allowed_memory_types: frozenset[str] = frozenset()
    project_memory_api: bool = False
    project_memory_snapshot: bool = False
    full_record_fetch: bool = False
    dependency_closure_only: bool = False
    category_api: bool = False
    task_summary_api: bool = False
    task_artifact_api: bool = False
    explorer_memory_api: bool = False
    explorer_first_attempt: bool = False
    # Explorer access policy v2 is carried only by newly planned Explorer
    # attempts.  ``None`` keeps the legacy v1 wire shape byte-for-byte stable
    # for old in-flight calls and for every non-Explorer role.
    explorer_access_policy_version: int | None = None
    explorer_access_mode: str | None = None
    explorer_access_grant_id: str | None = None
    explorer_access_grant_digest: str | None = None
    native_web_search: bool = False
    command_network: bool = False
    direct_canonical_mount: bool = False
    writable_workspace_paths: tuple[str, ...] = ("outbox", "artifacts", "tmp")

    @property
    def isolated_from_project_memory(self) -> bool:
        return (
            not self.project_memory_api
            and not self.project_memory_snapshot
            and not self.dependency_closure_only
        )

    def permits_types(self, requested: Iterable[str]) -> bool:
        values = frozenset(requested)
        return bool(values) and values <= self.allowed_memory_types

    def as_public_dict(self) -> dict[str, Any]:
        """Return the non-secret policy information safe to materialize."""

        result = {
            "name": self.name,
            "role": self.role,
            "mode": self.mode,
            "sprint_lane": self.sprint_lane,
            "allowed_memory_types": sorted(self.allowed_memory_types),
            "project_memory_api": self.project_memory_api,
            "full_record_fetch": self.full_record_fetch,
            "dependency_closure_only": self.dependency_closure_only,
            "category_api": self.category_api,
            "task_summary_api": self.task_summary_api,
            "task_artifact_api": self.task_artifact_api,
            "native_web_search": self.native_web_search,
            "command_network": self.command_network,
            "direct_canonical_mount": False,
            "writable_workspace_paths": list(self.writable_workspace_paths),
        }
        # These optional fields did not exist in the legacy wire contract.
        # Omit false defaults so existing policies and compatibility digests
        # remain byte-for-byte stable.
        if self.explorer_memory_api:
            result["explorer_memory_api"] = True
        if self.project_memory_snapshot:
            result["project_memory_snapshot"] = True
        if self.explorer_first_attempt:
            result["explorer_first_attempt"] = True
        if self.explorer_access_policy_version is not None:
            result.update(
                {
                    "explorer_access_policy_version": self.explorer_access_policy_version,
                    "explorer_access_mode": self.explorer_access_mode,
                    "explorer_access_grant_id": self.explorer_access_grant_id,
                    "explorer_access_grant_digest": self.explorer_access_grant_digest,
                }
            )
        return result


def staging_skills_for_policy(policy: AccessPolicy) -> frozenset[str]:
    """Return only the design-authorized staged skills for one launch policy."""

    role = policy.role
    mode = policy.mode
    if role in {"main", "main-agent", "scheduler"}:
        return frozenset({"task-writing"})
    if role == "advisor" and mode == "proposal":
        return frozenset({"selection-report"})
    if role == "main-sort":
        return frozenset({"record-progress"})
    if role == "explorer-worker":
        return frozenset({"record-scratch", "record-summary", "CAS"})
    if role == "trimmer":
        return frozenset({"human-guidance", "discovery-sprint"})
    if role == "verifier" or mode == "verifier":
        return frozenset({"CAS"})
    if role in {"proof-writer", "proofwriter"} or mode == "proof-writer":
        return frozenset({"record-progress", "CAS"})
    if role == "worker":
        return frozenset({"record-progress", "CAS"})
    return frozenset()


def policy_for(
    role: str,
    *,
    mode: str | None = None,
    sprint_lane: str | None = None,
    review_memory_type: str | None = None,
) -> AccessPolicy:
    """Return the design-approved policy for a role/mode."""

    role = role.strip().lower().replace("_", "-")
    mode = mode.strip().lower().replace("_", "-") if mode else None
    if sprint_lane is not None:
        sprint_lane = sprint_lane.upper()
        if sprint_lane not in SPRINT_LANES:
            raise ValueError(f"unknown discovery-sprint lane: {sprint_lane!r}")

    common: dict[str, Any] = {
        "name": f"{role}{'-' + mode if mode else ''}",
        "role": role,
        "mode": mode,
        "sprint_lane": sprint_lane,
        "direct_canonical_mount": False,
        "command_network": False,
    }

    if role in {"main", "main-agent"}:
        return AccessPolicy(
            **common,
            project_memory_snapshot=True,
            allowed_memory_types=MEMORY_TYPES,
            task_summary_api=True,
            task_artifact_api=True,
            native_web_search=True,
        )
    if role == "advisor":
        if mode not in {"proposal", "finalize"}:
            raise ValueError("advisor policy requires proposal or finalize mode")
        return AccessPolicy(
            **common,
            project_memory_snapshot=True,
            allowed_memory_types=MEMORY_TYPES,
            task_summary_api=True,
            task_artifact_api=True,
            native_web_search=True,
        )
    if role == "main-sort":
        return AccessPolicy(
            **common,
            project_memory_api=True,
            full_record_fetch=True,
            allowed_memory_types=SEARCHABLE_MEMORY_TYPES,
            task_summary_api=True,
            task_artifact_api=True,
            native_web_search=True,
        )
    if role == "explorer-worker":
        if mode not in {
            "clean-room",
            "explore",
            "check-result",
            "portfolio",
            "full-memory",
        }:
            raise ValueError(
                "explorer-worker policy requires a supported attempt access mode"
            )
        # The two old modes are intentionally retained as v1 recovery modes.
        # Their public dictionaries must not acquire any v2 fields.
        if mode == "clean-room":
            return AccessPolicy(
                **common,
                explorer_first_attempt=True,
                native_web_search=True,
            )
        if mode in {"check-result", "portfolio"}:
            return AccessPolicy(
                **common,
                explorer_access_policy_version=2,
                explorer_access_mode=mode,
                native_web_search=True,
            )
        if mode == "full-memory":
            return AccessPolicy(
                **common,
                # The joint Explorer API needs read authority over the host
                # backend, but the broker still withholds internal_search and
                # memory_fetch for this v2 mode.
                project_memory_api=True,
                explorer_memory_api=True,
                full_record_fetch=True,
                allowed_memory_types=SEARCHABLE_MEMORY_TYPES,
                explorer_access_policy_version=2,
                explorer_access_mode=mode,
                native_web_search=True,
            )
        return AccessPolicy(
            **common,
            project_memory_api=True,
            explorer_memory_api=True,
            full_record_fetch=True,
            allowed_memory_types=SEARCHABLE_MEMORY_TYPES,
            native_web_search=True,
        )
    if role == "trimmer":
        return AccessPolicy(
            **common,
            project_memory_api=True,
            full_record_fetch=True,
            allowed_memory_types=SEARCHABLE_MEMORY_TYPES,
            category_api=True,
            task_summary_api=True,
            task_artifact_api=True,
            native_web_search=True,
        )
    if role == "synthesizer":
        if review_memory_type not in {"fact", "route", "obligation"}:
            raise ValueError("synthesizer policy requires fact, route, or obligation")
        return AccessPolicy(
            **common,
            project_memory_api=True,
            full_record_fetch=True,
            allowed_memory_types=frozenset({review_memory_type}),
        )
    if role in {"summarizer", "discovery-sprint-summarizer"}:
        return AccessPolicy(**common, native_web_search=True)

    if role == "verifier" or mode == "verifier":
        return AccessPolicy(
            **common,
            project_memory_api=True,
            full_record_fetch=True,
            allowed_memory_types=frozenset({"fact"}),
            native_web_search=True,
        )
    if role in {"proof-writer", "proofwriter"} or mode == "proof-writer":
        return AccessPolicy(
            **common,
            allowed_memory_types=frozenset({"fact"}),
            full_record_fetch=True,
            dependency_closure_only=True,
            native_web_search=True,
        )

    if role not in {"worker", "scheduler"}:
        raise ValueError(f"unknown agent role: {role!r}")
    if role == "scheduler":
        return AccessPolicy(**common)
    if not mode:
        raise ValueError("worker policy requires a mode")
    if sprint_lane or mode in ISOLATED_MODES:
        return AccessPolicy(**common, native_web_search=True)
    if mode not in ORDINARY_SEARCH_MODES:
        raise ValueError(f"unknown worker mode: {mode!r}")
    return AccessPolicy(
        **common,
        project_memory_api=True,
        full_record_fetch=True,
        allowed_memory_types=SEARCHABLE_MEMORY_TYPES,
        native_web_search=True,
    )


@dataclass(frozen=True)
class MemoryRecord:
    """Agent-facing read representation returned by the audited broker."""

    memory_id: str
    memory_type: str
    abstract: str
    content: str
    title: str = ""
    active: bool = True
    withdrawn: bool = False
    revision: int | None = None
    predecessor_ids: tuple[str, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.memory_type not in MEMORY_TYPES:
            raise ValueError(f"unknown memory type: {self.memory_type!r}")
        if not _ID_RE.fullmatch(self.memory_id):
            raise ValueError(f"invalid memory ID: {self.memory_id!r}")

    def summary(self, *, score: float | None = None) -> dict[str, Any]:
        result: dict[str, Any] = {
            "id": self.memory_id,
            "memory_type": self.memory_type,
            "title": self.title or self.abstract[:100],
            "abstract": self.abstract,
        }
        if self.memory_type == "fact":
            result["status"] = "active" if self.active else "inactive"
        elif self.memory_type == "claim":
            result["status"] = "withdrawn" if self.withdrawn else "current"
        if self.revision is not None:
            result["revision"] = self.revision
        if score is not None:
            result["relevance"] = score
        return result

    def full(self) -> dict[str, Any]:
        value = self.summary()
        value.update(
            {
                "content": self.content,
                "predecessor_ids": list(self.predecessor_ids),
                "metadata": dict(self.metadata),
            }
        )
        return value


__all__ = [
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
