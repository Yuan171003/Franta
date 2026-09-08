"""Portable Explorer agent prompts, model routes, and response schemas.

This module is intentionally self-contained: it imports no host-agent package.
The host integrates it through :func:`build_launch_spec` and
:func:`write_response_schemas`.  Copying ``explorer_system`` to another host
therefore does not carry that host's prompt dispatcher or response contracts
with it.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import tempfile
from textwrap import dedent
from typing import Any

from .main_sort import (
    DEFAULT_MAIN_SORT_BLOCK,
    HostSortTools,
)


EXPLORER_MODEL_NAME = "gpt-6-astra"
EXPLORER_REASONING_EFFORT = "max"
MAIN_SORT_REASONING_EFFORT = (
    DEFAULT_MAIN_SORT_BLOCK.model_route().reasoning_effort
)
MAIN_SORT_RESPONSE_SCHEMA = DEFAULT_MAIN_SORT_BLOCK.response_schema()
EXPLORER_AGENT_ROLES = frozenset({"explorer-worker", "main-sort"})
EXPLORER_ACCESS_MODES = frozenset(
    {"check-result", "portfolio", "full-memory"}
)
EXPLORER_GUIDANCE_VARIANTS: dict[str, tuple[str, ...]] = {
    "check-result": (
        "check-result-promising",
        "check-result-uncommon",
    ),
    "portfolio": (
        "portfolio-synthesize",
        "portfolio-select-best",
        "portfolio-diversify",
        "portfolio-unconstrained",
    ),
    "full-memory": ("full-memory-adaptive",),
}

_GUIDANCE_TEXT = {
    "check-result-promising": (
        "Think freely, choose whichever research direction appears most promising, "
        "and attack the ROOT problem."
    ),
    "check-result-uncommon": (
        "Think freely, deliberately choose a research direction that is uncommon but "
        "may provide new results, and attack the ROOT problem."
    ),
    "portfolio-synthesize": (
        "Although this is not a compulsory requirement, you should actively combine "
        "your ideas and findings in attempt 1 with memories in the portfolio to develop "
        "a new attack on ROOT."
    ),
    "portfolio-select-best": (
        "Although this is not a compulsory requirement, you should review the progress "
        "you made in attempt 1 and the memories in the portfolio, then choose the most "
        "promising direction and continue attacking ROOT along it."
    ),
    "portfolio-diversify": (
        "Although this is not a compulsory requirement, you should explicitly brainstorm "
        "research directions genuinely different from existing ones."
    ),
    # This arm deliberately adds no strategy instruction beyond the common prompt.
    "portfolio-unconstrained": "",
    "full-memory-adaptive": (
        "You should explicitly brainstorm research directions genuinely different from "
        "existing ones, or combine genuinely different directions into a new attack; "
        "you may continue an earlier route when its concrete progress makes that choice "
        "stronger."
    ),
}


@dataclass(frozen=True)
class ExplorerModelRoute:
    """Host-neutral model selection for one Explorer-owned call."""

    model: str
    reasoning_effort: str


@dataclass(frozen=True)
class ExplorerLaunchSpec:
    """Complete portable prompt/model/schema selection for a host adapter."""

    role: str
    prompt: str
    schema_name: str
    model_route: ExplorerModelRoute


def _normalized_role(role: str) -> str:
    if not isinstance(role, str):
        return ""
    return role.strip().lower().replace("_", "-")


def owns_agent_role(role: str) -> bool:
    """Return whether the portable block owns this agent-facing role."""

    return _normalized_role(role) in EXPLORER_AGENT_ROLES


def validate_guidance_variant(mode: str, guidance_variant: str) -> str:
    """Validate one persisted strategy variant for a staged-access attempt."""

    normalized_mode = mode.strip().lower().replace("_", "-")
    variants = EXPLORER_GUIDANCE_VARIANTS.get(normalized_mode)
    if variants is None:
        raise ValueError(
            "guidance variants are available only for check-result, portfolio, "
            "or full-memory"
        )
    if guidance_variant not in variants:
        expected = ", ".join(repr(value) for value in variants)
        raise ValueError(
            f"guidance_variant for {normalized_mode!r} must be one of {expected}"
        )
    return guidance_variant


def select_guidance_variant(mode: str, *, entropy: str) -> str:
    """Select a reproducible variant that the host can persist with the call.

    Selection is deliberately separate from prompt rendering.  A host supplies
    durable attempt entropy, persists the returned enum in the call input, and
    passes that exact enum back on every retry or recovery launch.
    """

    normalized_mode = mode.strip().lower().replace("_", "-")
    variants = EXPLORER_GUIDANCE_VARIANTS.get(normalized_mode)
    if variants is None:
        raise ValueError(
            "guidance variants are available only for check-result, portfolio, "
            "or full-memory"
        )
    if (
        not isinstance(entropy, str)
        or not entropy
        or entropy != entropy.strip()
    ):
        raise ValueError("guidance selection entropy must be nonempty exact text")
    digest = hashlib.sha256(
        b"explorer-guidance-v1\0"
        + normalized_mode.encode("utf-8")
        + b"\0"
        + entropy.encode("utf-8")
    ).digest()
    return variants[int.from_bytes(digest, "big") % len(variants)]


def _legacy_explorer_worker_prompt(
    root_problem: str,
    *,
    mode: str,
    input_path: str,
    host: str,
    human_guidance: str | None = None,
) -> str:
    """Render the byte-frozen recovery-only v1 worker prompt."""

    if mode == "clean-room":
        memory_guidance = (
            "This is the first attempt in your lineage. It is a strict clean-room attack: "
            "project-memory and Explorer-search tools are mechanically unavailable. Use only "
            "ROOT, your own reasoning, native web search, and results you compute during this call."
        )
        if human_guidance is not None:
            memory_guidance = (
                "This is the first attempt in your lineage. It is a strict clean-room attack: "
                "project-memory and Explorer-search tools are mechanically unavailable. Use only "
                "ROOT, the human guidance below, your own reasoning, native web search, and results "
                "you compute during this call."
            )
    else:
        memory_guidance = (
            "Use `explorer-search` freely when it helps. Search summaries first, then fetch only "
            "useful full records. Results marked `record_space=explorer` are provisional ideas, "
            f"not established premises; among {host} records only active facts are established. "
            "You may continue, combine, or replace earlier directions."
        )
    direction_guidance = (
        "Think freely, choose whichever research direction appears most promising, and attack it."
        if human_guidance is None
        else "You must strictly follow human guidance below in this attempt."
    )
    prompt = dedent(
        f"""
        You are an Explorer mathematical research worker. Your single goal is to attack ROOT:
        Here, "Your single goal" describes only this Explorer assignment. Do not use Codex
        thread goals or call `create_goal`, `get_goal`, or `update_goal`.

        {root_problem.strip()}

        Read {input_path}. {memory_guidance}

        {direction_guidance}
        Do not create a special direction record. If a direction or pivot produces reusable
        mathematical content, preserve that content as an ordinary scratch of the most fitting
        kind. Describe every direction tried in the attempt-final summary.

        When a proof, claim, route, obligation, computation, idea, intuition, discovery,
        example, counterexample, obstacle, or possible pivot may help later research, append it
        with `record-scratch`. Scratch is provisional and append-only: do not verify, integrate,
        publish, edit, or delete {host} memory. Continue attacking ROOT after recording progress.

        If you believe ROOT is proved or disproved, first record the complete argument as a
        `proof` scratch. Only then name that exact scratch ID as `root_candidate_scratch_id` in
        the final response and set `root_candidate_outcome` to `proved` or `disproved`. This is a
        verification request, never a declaration that ROOT is resolved. Otherwise return null
        for both root-candidate fields.

        Before every normal exit call `record-summary` once, identifying the directions tried,
        main progress, main obstacles, and source scratch IDs. Return `attempt_ended=true`, the
        exact summary ID and the applicable stop reason. Work for up to the configured attempt
        deadline; an open problem is not by itself a reason to stop.
        
        *Hard constraint*: If you have worked for 3 hours, gracefully stop, call `record-memory`, and then exit.
        """
    ).strip()
    return _with_explorer_human_guidance(prompt, human_guidance)


def _with_explorer_human_guidance(prompt: str, human_guidance: str | None) -> str:
    if human_guidance is None:
        return prompt
    return (
        prompt
        + "\n\nHuman guidance for this attempt\n\n"
        + "You must strictly follow human guidance: the supplied approach is the binding "
        "research direction for this attempt. Pursue it seriously and do not silently pivot "
        "to a different approach. Mathematical claims in the guidance remain unproved; "
        "all existing evidence, access, recording, and verification requirements still apply. "
        "If the approach fails or rests on an incorrect claim, record concrete obstacles, "
        "counterexamples, or a rigorous refutation. In your normal attempt-final summary, "
        "explain how you pursued the guidance and any obstacles encountered.\n\n"
        "BEGIN HUMAN GUIDANCE\n"
        + human_guidance
        + "\nEND HUMAN GUIDANCE"
    )


def explorer_worker_prompt(
    root_problem: str,
    *,
    mode: str,
    guidance_variant: str | None = None,
    human_guidance: str | None = None,
    input_path: str = "input/context.json",
    host_agent_name: str = "host collaborator",
) -> str:
    """Return the deliberately small, phase-bound Explorer worker prompt."""

    host = host_agent_name.strip()
    if not host:
        raise ValueError("host_agent_name must be nonempty")
    normalized_mode = mode.strip().lower().replace("_", "-")
    if normalized_mode not in EXPLORER_ACCESS_MODES | {"clean-room", "explore"}:
        raise ValueError(
            "Explorer worker mode must be 'check-result', 'portfolio', "
            "or 'full-memory'"
        )
    if human_guidance is not None:
        if not isinstance(human_guidance, str) or not human_guidance.strip():
            raise ValueError("human_guidance must be nonempty text")
        if normalized_mode not in {"check-result", "clean-room"}:
            raise ValueError("human_guidance is available only for Explorer attempt 1")
    if normalized_mode in {"clean-room", "explore"}:
        if guidance_variant is not None:
            raise ValueError("recovery-only v1 prompts do not accept guidance_variant")
        return _legacy_explorer_worker_prompt(
            root_problem,
            mode=normalized_mode,
            input_path=input_path,
            host=host,
            human_guidance=human_guidance,
        )

    if guidance_variant is None:
        raise ValueError("staged-access Explorer prompts require guidance_variant")
    variant = validate_guidance_variant(normalized_mode, guidance_variant)
    think_guidance = _GUIDANCE_TEXT[variant] if human_guidance is None else ""
    guidance_suffix = f" {think_guidance}" if think_guidance else ""

    if normalized_mode == "check-result":
        introduction = (
            "This is attempt 1 in your lineage. Initially you receive only ROOT: choose a research "
            "direction and attack it independently. Your sole memory-reading skill is "
            if human_guidance is None
            else "This is attempt 1 in your lineage. Initially you receive ROOT and the human "
            "guidance below; follow that research direction. Your sole memory-reading skill is "
        )
        initial_material = "ROOT" if human_guidance is None else "ROOT, the human guidance below"
        memory_guidance = (
            introduction
            + "`check-result`. Use it *only* when further progress requires knowing whether one strict, "
            f"single mathematical proposition has been proved, disproved, or computed. No other {host} or Explorer memory search or fetch is "
            "available. Use only "
            f"{initial_material}, your own reasoning, native web search, and results you compute during this call.{guidance_suffix}"
        )
    elif normalized_mode == "portfolio":
        memory_guidance = (
            "This is attempt 2 in your lineage. Your sole memory-reading skills are "
            "`portfolio-search` and `check-result`. The immutable portfolio contains up to three routes, six high-level "
            "memos, and the summaries and scratches of up to two other attempts, and it is chosen deliberately to improve your research. Use `check-result` to search for other proved results *only* when further progress requires knowing whether one strict, "
            "single mathematical proposition has been proved, disproved, or computed. Do not use any other memory search or "
            f"fetch.{guidance_suffix}"
        )
    else:
        memory_guidance = (
            "This is attempt 3 in your lineage. Use `explorer-search` freely across the full "
            "launch-authorized memory snapshot: search summaries first, then fetch only useful full "
            f"records. Results marked `record_space=explorer` are provisional ideas.{guidance_suffix}"
        )
    direction_guidance = (
        "Think freely, choose whichever research direction appears most promising, and attack it."
        if human_guidance is None
        else "You must strictly follow human guidance below in this attempt."
    )
    prompt = dedent(
        f"""
        You are an Explorer mathematical research worker. Your single goal is to attack ROOT:
        Here, "Your single goal" describes only this Explorer assignment. Do not use Codex
        thread goals or call `create_goal`, `get_goal`, or `update_goal`.

        {root_problem.strip()}

        Read {input_path}. {memory_guidance}

        {direction_guidance}

        When a proof, claim, route, obligation, computation, idea, intuition, discovery,
        example, counterexample, obstacle, or possible pivot may help later research, append it
        with `record-scratch`. Scratch is provisional and append-only: do not verify, integrate,
        publish, edit, or delete {host} memory. Continue attacking ROOT after recording progress.

        If you believe ROOT is proved or disproved, first record the complete argument as a
        `proof` scratch. Only then name that exact scratch ID as `root_candidate_scratch_id` in
        the final response and set `root_candidate_outcome` to `proved` or `disproved`. This is a
        verification request, never a declaration that ROOT is resolved. Otherwise return null
        for both root-candidate fields.

        Before every normal exit call `record-summary` once, identifying the directions tried,
        main progress, main obstacles, and source scratch IDs. Return `attempt_ended=true`, the
        exact summary ID and the applicable stop reason. Work for up to the configured attempt
        deadline; an open problem is not by itself a reason to stop.
        
        *Hard constraint*: *Bias strongly* toward continued investigation. *Do not* stop because the first several approaches fail, the problem is known to be open, or further work appears to be difficult. If you make important progress, you should also continue to make more progress and attack the ROOT. Unless you have proved or disproved the ROOT problem, *do not* finish within 0.5 hours. However, if you have run for 3 hours, gracefully stop, call `record-summary`, and then exit.
        """
    ).strip()
    return _with_explorer_human_guidance(prompt, human_guidance)


def build_launch_spec(
    role: str,
    *,
    root_problem: str,
    mode: str | None = None,
    guidance_variant: str | None = None,
    human_guidance: str | None = None,
    input_path: str | None = None,
    host_agent_name: str = "host collaborator",
    host_sort_tools: HostSortTools | None = None,
) -> ExplorerLaunchSpec:
    """Select an Explorer-owned prompt, model route, and response schema."""

    normalized = _normalized_role(role)
    if normalized == "explorer-worker":
        if mode is None:
            raise ValueError("Explorer worker launch requires a mode")
        prompt = explorer_worker_prompt(
            root_problem,
            mode=mode,
            guidance_variant=guidance_variant,
            human_guidance=human_guidance,
            input_path=input_path or "input/context.json",
            host_agent_name=host_agent_name,
        )
        route = ExplorerModelRoute(
            model=EXPLORER_MODEL_NAME,
            reasoning_effort=EXPLORER_REASONING_EFFORT,
        )
    elif normalized == "main-sort":
        if human_guidance is not None:
            raise ValueError("main-sort does not accept human_guidance")
        if guidance_variant is not None:
            raise ValueError("main-sort does not accept guidance_variant")
        main_sort_spec = DEFAULT_MAIN_SORT_BLOCK.build_launch_spec(
            root_problem=root_problem,
            input_path=input_path or "input/task_card.json",
            host_agent_name=host_agent_name,
            host_tools=host_sort_tools,
        )
        return ExplorerLaunchSpec(
            role=main_sort_spec.role,
            prompt=main_sort_spec.prompt,
            schema_name=main_sort_spec.schema_name,
            model_route=ExplorerModelRoute(
                model=main_sort_spec.model_route.model,
                reasoning_effort=main_sort_spec.model_route.reasoning_effort,
            ),
        )
    else:
        raise ValueError(f"role is not owned by Explorer: {role!r}")
    schema_name = normalized
    if normalized == "explorer-worker" and mode is not None:
        normalized_mode = mode.strip().lower().replace("_", "-")
        if normalized_mode in EXPLORER_ACCESS_MODES:
            schema_name = "explorer-worker-v2"
    return ExplorerLaunchSpec(
        role=normalized,
        prompt=prompt,
        schema_name=schema_name,
        model_route=route,
    )


def main_sort_prompt(
    root_problem: str,
    *,
    input_path: str = "input/task_card.json",
    host_agent_name: str = "host collaborator",
    host_tools: HostSortTools | None = None,
) -> str:
    """Compatibility delegate to the isolated main-sort interface."""

    return DEFAULT_MAIN_SORT_BLOCK.build_launch_spec(
        root_problem=root_problem,
        input_path=input_path,
        host_agent_name=host_agent_name,
        host_tools=host_tools,
    ).prompt


def _object(properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    if len(required) != len(set(required)) or set(required) != set(properties):
        raise ValueError("strict object schemas must require every property exactly once")
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


RESPONSE_SCHEMAS: dict[str, dict[str, Any]] = {
    "explorer-worker": _object(
        {
            "attempt_ended": {"type": "boolean"},
            "final_summary_id": {"type": "string"},
            "root_candidate_scratch_id": {"type": ["string", "null"]},
            "root_candidate_outcome": {
                "type": ["string", "null"],
                "enum": ["proved", "disproved", None],
            },
            "stop_reason": {
                "type": "string",
                "enum": ["attempt_complete", "time_limit", "root_candidate"],
            },
        },
        [
            "attempt_ended",
            "final_summary_id",
            "root_candidate_scratch_id",
            "root_candidate_outcome",
            "stop_reason",
        ],
    ),
    "explorer-worker-v2": _object(
        {
            "attempt_ended": {"type": "boolean"},
            "final_summary_id": {"type": "string"},
            "root_candidate_scratch_id": {"type": ["string", "null"]},
            "root_candidate_outcome": {
                "type": ["string", "null"],
                "enum": ["proved", "disproved", None],
            },
            "stop_reason": {
                "type": "string",
                "enum": ["attempt_complete", "time_limit", "root_candidate"],
            },
        },
        [
            "attempt_ended",
            "final_summary_id",
            "root_candidate_scratch_id",
            "root_candidate_outcome",
            "stop_reason",
        ],
    ),
    "main-sort": MAIN_SORT_RESPONSE_SCHEMA,
}


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _atomic_write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def write_response_schemas(directory: str | Path) -> dict[str, Path]:
    """Write only Explorer-owned schemas, without a host schema dependency."""

    root = Path(directory)
    written: dict[str, Path] = {}
    for name, schema in RESPONSE_SCHEMAS.items():
        path = root / f"{name}.schema.json"
        _atomic_write_text(path, _canonical_json(schema) + "\n")
        written[name] = path
    return written


__all__ = [
    "EXPLORER_ACCESS_MODES",
    "EXPLORER_AGENT_ROLES",
    "EXPLORER_GUIDANCE_VARIANTS",
    "EXPLORER_MODEL_NAME",
    "EXPLORER_REASONING_EFFORT",
    "MAIN_SORT_REASONING_EFFORT",
    "RESPONSE_SCHEMAS",
    "ExplorerLaunchSpec",
    "ExplorerModelRoute",
    "HostSortTools",
    "build_launch_spec",
    "explorer_worker_prompt",
    "main_sort_prompt",
    "owns_agent_role",
    "select_guidance_variant",
    "validate_guidance_variant",
    "write_response_schemas",
]
