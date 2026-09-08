"""Runtime validation for Franta skills and the policy-bound memory MCP adapter."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from ..explorer_adapter import (
    EXPLORER_SKILLS,
    ExplorerToolValidationError,
    MainSortContractError,
    explorer_tool_definitions,
    explorer_worker_skills,
    normalize_main_sort_computation_promotions,
    normalize_scratch_payload,
    normalize_summary_payload,
    validate_main_sort_progress_operation,
)
from ..advisor_adapter import (
    AdvisorCycleContext,
    SelectionReport,
    render_selection_report,
    validate_advisor_breakthrough_evidence,
)

from .broker import BrokerClient
from .cas_process import CASProcessScope, CAS_TIMEOUT_SECONDS
from ..contracts.agent_access import AccessPolicy, SEARCHABLE_MEMORY_TYPES


SKILLS = (
    "task-writing",
    "internal-search",
    "task-search",
    "selection-report",
    "record-progress",
    "CAS",
    "human-guidance",
    "discovery-sprint",
    *EXPLORER_SKILLS,
)
_ALIASES = {name.casefold(): name for name in SKILLS}
PORTFOLIO_TYPES = ("fact", "route", "memo", "claim", "obligation", "computation")


class SkillRuntimeError(RuntimeError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical_skill(name: str) -> str:
    try:
        return _ALIASES[name.casefold()]
    except KeyError as exc:
        raise SkillRuntimeError(f"unknown Franta skill: {name!r}") from exc


def _completion_evidence_ids(value: Any, *, field: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise SkillRuntimeError(f"{field} must be a list of string IDs")
    if len(set(value)) != len(value):
        raise SkillRuntimeError(f"{field} must not contain duplicate IDs")
    return list(value)


def allowed_skills(policy: AccessPolicy) -> frozenset[str]:
    role = policy.role
    mode = policy.mode
    if role in {"main", "main-agent"}:
        return frozenset({"task-writing", "task-search"})
    if role == "advisor":
        if mode == "proposal":
            return frozenset({"selection-report", "task-search"})
        if mode == "finalize":
            return frozenset({"task-search"})
        return frozenset()
    if role == "main-sort":
        return frozenset(
            {"internal-search", "task-search", "record-progress"}
        )
    if role == "explorer-worker":
        if policy.explorer_access_policy_version == 2:
            return explorer_worker_skills(
                access_mode=policy.explorer_access_mode
            ) | {"CAS"}
        return explorer_worker_skills(
            search_enabled=policy.explorer_memory_api
        ) | {"CAS"}
    if role == "trimmer":
        return frozenset(
            {"internal-search", "task-search", "human-guidance", "discovery-sprint"}
        )
    if role == "synthesizer":
        return frozenset({"internal-search"})
    if role in {"summarizer", "discovery-sprint-summarizer"}:
        return frozenset()
    if role == "scheduler":
        return frozenset({"task-writing"})
    if role == "verifier" or mode == "verifier":
        return frozenset({"internal-search", "CAS"})
    if role in {"proof-writer", "proofwriter"} or mode == "proof-writer":
        return frozenset({"internal-search", "record-progress", "CAS"})
    if role == "worker":
        values = {"record-progress", "CAS"}
        if policy.project_memory_api:
            values.add("internal-search")
        return frozenset(values)
    return frozenset()


@dataclass(frozen=True)
class SkillContext:
    workspace: Path
    policy: AccessPolicy
    task_id: str | None = None
    attempt: int | None = None
    batch_id: str | None = None
    task_card: Mapping[str, Any] | None = None

    @classmethod
    def load(cls, workspace: str | os.PathLike[str] | None = None) -> "SkillContext":
        root = Path(workspace or os.environ.get("FRANTA_WORKSPACE") or os.getcwd()).resolve()
        policy_path = root / "input" / "access_policy.json"
        try:
            public = json.loads(policy_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SkillRuntimeError("missing or invalid materialized access policy") from exc
        public["allowed_memory_types"] = frozenset(public.get("allowed_memory_types", ()))
        public["writable_workspace_paths"] = tuple(
            public.get("writable_workspace_paths", ("outbox", "artifacts", "tmp"))
        )
        policy = AccessPolicy(**public)
        card_path = root / "input" / "task_card.json"
        card: Mapping[str, Any] | None = None
        if card_path.exists():
            try:
                card = json.loads(card_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                raise SkillRuntimeError("invalid materialized task card") from exc
        return cls(
            workspace=root,
            policy=policy,
            task_id=str(card.get("task_id")) if card and card.get("task_id") else None,
            attempt=int(card.get("attempt", 1)) if card else None,
            batch_id=str(card.get("batch_id")) if card and card.get("batch_id") else None,
            task_card=card,
        )


def _inside(root: Path, candidate: Path) -> bool:
    resolved_root = root.resolve()
    resolved = candidate.resolve()
    return resolved == resolved_root or resolved_root in resolved.parents


def _trusted_executable(value: Any, label: str) -> Path:
    if isinstance(value, os.PathLike):
        value = os.fspath(value)
    if not isinstance(value, str) or not value.strip():
        raise SkillRuntimeError(f"{label} is not configured")
    if "\x00" in value:
        raise SkillRuntimeError(f"configured {label} executable is invalid")
    # Bare command names refer to the scheduler's PATH, never a shell command.
    # Explicit relative paths are made absolute when loading the manifest.
    candidate = value if "/" in value or value.startswith("~") else shutil.which(value)
    if candidate is None:
        raise SkillRuntimeError(f"configured {label} executable is unavailable on PATH")
    executable = Path(candidate).expanduser().resolve()
    if not executable.is_file() or not os.access(executable, os.X_OK):
        raise SkillRuntimeError(f"configured {label} executable is unavailable")
    return executable


def _clean_tool_environment(
    context: SkillContext, *, use_host_home: bool = False
) -> dict[str, str]:
    tool_home = context.workspace / "tmp" / "tool-home"
    tool_home.mkdir(mode=0o700, exist_ok=True)
    environment = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "TMPDIR": str((context.workspace / "tmp").resolve()),
        "HOME": (
            os.environ.get("HOME", str(tool_home)) if use_host_home else str(tool_home)
        ),
    }
    for key in ("LANG", "LC_ALL"):
        if os.environ.get(key):
            environment[key] = os.environ[key]
    if use_host_home:
        for key in ("XDG_CACHE_HOME", "TECTONIC_CACHE_DIR"):
            if os.environ.get(key):
                environment[key] = str(Path(os.environ[key]).expanduser().resolve())
    return environment


def _tectonic_cache_paths() -> list[Path]:
    """Locate only Tectonic's existing cache, without exposing the host home."""

    override = os.environ.get("TECTONIC_CACHE_DIR")
    if override:
        candidates = [Path(override).expanduser()]
    elif sys.platform == "darwin":
        candidates = [Path.home() / "Library" / "Caches" / "Tectonic"]
    else:
        cache_home = os.environ.get("XDG_CACHE_HOME")
        root = Path(cache_home).expanduser() if cache_home else Path.home() / ".cache"
        # Tectonic releases using app_dirs and directories differ in case.
        candidates = [root / "Tectonic", root / "tectonic"]
    paths: list[Path] = []
    for path in candidates:
        if path.is_dir() and not any(path.samefile(existing) for existing in paths):
            paths.append(path.resolve())
    return paths


def _runtime_read_root(executable: Path) -> Path:
    """Return the narrow installed tree needed by one configured executable."""

    resolved = executable.resolve()
    parts = resolved.parts
    if "Cellar" in parts:
        index = parts.index("Cellar")
        if len(parts) > index + 2:
            return Path(*parts[: index + 3])
    if resolved.parent.name == "bin" and resolved.parent.parent != Path.home():
        return resolved.parent.parent
    return resolved.parent


def _constrained_command(
    executable: Path,
    arguments: Sequence[str],
    *,
    workspace: Path,
    read_paths: Iterable[str | os.PathLike[str]] = (),
    denied_paths: Iterable[str | os.PathLike[str]] = (),
) -> list[str]:
    """Build a shell-free command confined to required runtime and call files."""

    direct = [str(executable), *arguments]
    if sys.platform == "darwin":
        sandbox = Path("/usr/bin/sandbox-exec")
        if not sandbox.is_file():
            raise SkillRuntimeError("filesystem-and-network confinement is unavailable")
        workspace = workspace.resolve()
        allowed_reads = {
            Path("/System"),
            Path("/usr"),
            Path("/bin"),
            Path("/sbin"),
            Path("/Library"),
            Path("/opt/homebrew"),
            Path("/private/etc"),
            Path("/private/var/select"),
            workspace,
            _runtime_read_root(executable),
            *(Path(raw).expanduser().resolve() for raw in read_paths),
        }
        read_filters = " ".join(
            ["(literal \"/\")"]
            + [
                f"(subpath {json.dumps(str(path), ensure_ascii=False)})"
                for path in sorted(allowed_reads, key=lambda item: str(item))
            ]
        )
        metadata_paths = {Path("/")}
        for path in allowed_reads:
            metadata_paths.update(path.parents)
        metadata_filters = " ".join(
            [read_filters]
            + [
                f"(literal {json.dumps(str(path), ensure_ascii=False)})"
                for path in sorted(metadata_paths, key=lambda item: str(item))
            ]
        )
        allowed_writes = [workspace / name for name in ("outbox", "artifacts", "tmp")]
        write_filters = " ".join(
            f"(subpath {json.dumps(str(path), ensure_ascii=False)})"
            for path in allowed_writes
        )
        clauses = [
            "(version 1)",
            "(allow default)",
            "(deny network*)",
            f"(deny file-read-data (require-not (require-any {read_filters})))",
            f"(deny file-map-executable (require-not (require-any {read_filters})))",
            f"(deny file-read-metadata (require-not (require-any {metadata_filters})))",
            f"(deny file-read-xattr (require-not (require-any {metadata_filters})))",
            f"(deny file-write* (require-not (require-any {write_filters})))",
        ]
        for raw in denied_paths:
            path = str(Path(raw).resolve())
            encoded = json.dumps(path, ensure_ascii=False)
            clauses.append(f"(deny file-read* (subpath {encoded}))")
            clauses.append(f"(deny file-write* (subpath {encoded}))")
        return [str(sandbox), "-p", " ".join(clauses), *direct]
    if sys.platform.startswith("linux"):
        sandbox = shutil.which("bwrap")
        if sandbox is None:
            raise SkillRuntimeError(
                "filesystem-and-network confinement is unavailable: "
                "install bubblewrap (bwrap) on Linux and enable user namespaces"
            )
        workspace = workspace.resolve()
        # Build a new filesystem from runtime trees instead of mounting the
        # host root. /proc has a private PID namespace; /dev is a fresh minimal
        # device tree. Only the call's three output directories are host-writable.
        allowed_reads = {
            *(Path(name) for name in ("/usr", "/bin", "/sbin", "/lib", "/lib64", "/etc")),
            _runtime_read_root(executable),
            workspace,
            *(Path(raw).expanduser().resolve() for raw in read_paths),
        }
        allowed_reads = {path for path in allowed_reads if path.exists()}
        command = [
            sandbox, "--die-with-parent", "--new-session", "--unshare-all",
            "--cap-drop", "ALL", "--proc", "/proc", "--dev", "/dev",
        ]
        for path in sorted(allowed_reads, key=lambda item: (len(item.parts), str(item))):
            command.extend(["--ro-bind", str(path), str(path)])
        for name in ("outbox", "artifacts", "tmp"):
            path = workspace / name
            if path.is_symlink() or not path.is_dir():
                raise SkillRuntimeError(f"unsafe or missing CAS workspace directory: {name}")
            command.extend(["--bind", str(path), str(path)])
        for path in sorted(
            {Path(raw).expanduser().resolve() for raw in denied_paths},
            key=lambda item: (len(item.parts), str(item)),
        ):
            if not path.exists() or not any(_inside(root, path) for root in allowed_reads):
                continue
            if path.is_dir():
                command.extend(["--tmpfs", str(path), "--remount-ro", str(path)])
            else:
                command.extend(["--ro-bind", "/dev/null", str(path)])
        command.extend(["--chdir", str(workspace), "--", *direct])
        return command
    raise SkillRuntimeError("filesystem-and-network confinement is unavailable")


def _run_constrained(
    context: SkillContext,
    executable: Path,
    arguments: Sequence[str],
    *,
    input_text: str | None = None,
    timeout_seconds: float | None = None,
    read_paths: Iterable[str | os.PathLike[str]] = (),
    denied_paths: Iterable[str | os.PathLike[str]] = (),
    use_host_home: bool = False,
    process_scope: CASProcessScope | None = None,
    deadline: float | None = None,
) -> subprocess.CompletedProcess[str]:
    command = _constrained_command(
        executable,
        arguments,
        workspace=context.workspace,
        read_paths=read_paths,
        denied_paths=denied_paths,
    )
    try:
        environment = _clean_tool_environment(context, use_host_home=use_host_home)
        if process_scope is not None:
            assert deadline is not None
            completed = process_scope.run(
                command,
                input_text=input_text,
                cwd=context.workspace,
                env=environment,
                deadline=deadline,
            )
        else:
            completed = subprocess.run(
                command,
                input=input_text,
                text=True,
                capture_output=True,
                check=False,
                timeout=timeout_seconds,
                cwd=context.workspace,
                env=environment,
                shell=False,
            )
        if (
            command
            and command[0] == "/usr/bin/sandbox-exec"
            and completed.returncode != 0
            and (
                "sandbox_apply" in completed.stderr
                or "invalid profile" in completed.stderr.casefold()
            )
        ):
            detail = completed.stderr.strip()
            raise SkillRuntimeError(
                "filesystem-and-network confinement could not be applied"
                + (f": {detail}" if detail else "")
            )
        if (
            sys.platform.startswith("linux")
            and command
            and completed.returncode != 0
            and any(line.startswith("bwrap:") for line in completed.stderr.splitlines())
        ):
            raise SkillRuntimeError(
                "filesystem-and-network confinement could not be applied; "
                "check bubblewrap and user-namespace support: " + completed.stderr.strip()
            )
        return completed
    except (OSError, subprocess.SubprocessError) as exc:
        raise SkillRuntimeError(f"constrained tool execution failed: {exc}") from exc


def _artifact_file(context: SkillContext, value: Any, *, suffix: str | None = None) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise SkillRuntimeError("artifact path must be nonempty relative text")
    relative = Path(value)
    if relative.is_absolute() or ".." in relative.parts:
        raise SkillRuntimeError("artifact path must stay inside artifacts/")
    path = context.workspace / relative
    artifacts = context.workspace / "artifacts"
    if not _inside(artifacts, path) or not path.is_file() or path.is_symlink():
        raise SkillRuntimeError("artifact must be a regular file inside artifacts/")
    if suffix is not None and path.suffix.casefold() != suffix.casefold():
        raise SkillRuntimeError(f"artifact must have the {suffix} suffix")
    return path


def _write_new_file(path: Path, data: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        path.unlink(missing_ok=True)
        raise


class SkillRuntime:
    """Validate calls and stage immutable, noncanonical skill artifacts."""

    def __init__(self, context: SkillContext) -> None:
        self.context = context

    def invoke(self, name: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        skill = _canonical_skill(name)
        if skill not in allowed_skills(self.context.policy):
            raise SkillRuntimeError(
                f"{skill} is unavailable for {self.context.policy.role}/{self.context.policy.mode}"
            )
        if skill in {"internal-search", "task-search", "explorer-search"}:
            raise SkillRuntimeError(
                f"{skill} must use the policy-bound Franta MCP tools"
            )
        value = dict(payload)
        if skill == "task-writing":
            value = self._task_writing(value)
        elif skill == "record-progress":
            value = self._record_progress(value)
        elif skill == "CAS":
            value = self._cas_record(value)
        elif skill == "human-guidance":
            value = self._human_guidance(value)
        elif skill == "discovery-sprint":
            value = self._discovery_sprint(value)
        elif skill == "record-scratch":
            value = self._record_scratch(value)
        elif skill == "record-summary":
            value = self._record_summary(value)
        elif skill == "selection-report":
            value = self._selection_report(value)
        result = self._stage(skill, value)
        if skill == "selection-report":
            report = SelectionReport.from_dict(value)
            result.update(
                {
                    "selection_report_id": report.selection_report_id,
                    "selection_report_digest": report.digest,
                    "feedback_request_id": report.feedback_request_id,
                    "report_path": report.report_path,
                }
            )
        return result

    def _stage(self, skill: str, value: Mapping[str, Any]) -> dict[str, Any]:
        operation_id = str(value.get("operation_id") or f"OP-{uuid.uuid4()}")
        safe = operation_id.replace(":", "-")
        if not safe or any(character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-" for character in safe):
            raise SkillRuntimeError("operation_id is not safe for staging")
        directory = self.context.workspace / "outbox" / skill
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{safe}.json"
        if path.exists() or path.is_symlink():
            raise SkillRuntimeError(f"staged operation already exists: {operation_id}")
        # These two fields are runtime-owned.  A payload may not forge the
        # skill name or make the receipt name a different operation.
        output = {**dict(value), "skill": skill, "operation_id": operation_id}
        temporary = path.with_suffix(".json.new")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(output, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        self._audit(skill, operation_id, path)
        result = {
            "operation_id": operation_id,
            "staged_path": str(path),
            "artifact": output,
        }
        if output.get("record_id"):
            result["record_id"] = output["record_id"]
        return result

    def _record_scratch(self, value: dict[str, Any]) -> dict[str, Any]:
        try:
            return normalize_scratch_payload(value)
        except ExplorerToolValidationError as exc:
            raise SkillRuntimeError(str(exc)) from None

    def _record_summary(self, value: dict[str, Any]) -> dict[str, Any]:
        try:
            return normalize_summary_payload(value)
        except ExplorerToolValidationError as exc:
            raise SkillRuntimeError(str(exc)) from None

    def _selection_report(self, value: dict[str, Any]) -> dict[str, Any]:
        """Validate and render one report bound to the active Advisor round."""

        if self.context.policy.role != "advisor":
            raise SkillRuntimeError("selection-report requires an Advisor launch")
        context_path = self.context.workspace / "input" / "context.json"
        if not context_path.is_file() or context_path.is_symlink():
            raise SkillRuntimeError("selection-report requires the active Advisor context")
        try:
            context_value = json.loads(context_path.read_text(encoding="utf-8"))
            if not isinstance(context_value, Mapping):
                raise TypeError("Advisor context is not an object")
            advisor_context = AdvisorCycleContext.from_dict(context_value)
            report = SelectionReport.from_dict(value)
            report.validate_for_context(advisor_context)
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
            raise SkillRuntimeError(f"invalid selection-report payload or context: {exc}") from None

        snapshot_root = self.context.workspace / advisor_context.memory_snapshot.relative_path
        catalog_path = snapshot_root / "catalog.jsonl"
        if (
            snapshot_root.is_symlink()
            or catalog_path.is_symlink()
            or not catalog_path.is_file()
        ):
            raise SkillRuntimeError(
                "selection-report requires the authenticated memory catalog"
            )
        try:
            evidence_revisions: dict[str, int] = {}
            for line in catalog_path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                entry = json.loads(line)
                if not isinstance(entry, Mapping):
                    raise TypeError("memory catalog row is not an object")
                memory_id = entry.get("id")
                revision = entry.get("revision")
                if (
                    not isinstance(memory_id, str)
                    or not memory_id
                    or memory_id in evidence_revisions
                    or not isinstance(revision, int)
                    or isinstance(revision, bool)
                    or revision < 1
                ):
                    raise TypeError("memory catalog row has an invalid revision")
                evidence_revisions[memory_id] = revision
        except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
            raise SkillRuntimeError(
                f"selection-report memory catalog is unreadable: {exc}"
            ) from None
        try:
            validate_advisor_breakthrough_evidence(
                report,
                advisor_context,
                current_revisions=evidence_revisions,
                freshness=context_value.get("breakthrough_evidence_freshness"),
            )
        except (TypeError, ValueError) as exc:
            raise SkillRuntimeError(
                f"selection-report breakthrough evidence is invalid: {exc}"
            ) from None

        relative = Path(report.report_path)
        artifacts = (self.context.workspace / "artifacts").resolve()
        report_path = self.context.workspace / relative
        if (
            relative.is_absolute()
            or not relative.parts
            or relative.parts[0] != "artifacts"
            or ".." in relative.parts
            or relative.suffix.casefold() != ".md"
            or not _inside(artifacts, report_path)
        ):
            raise SkillRuntimeError(
                "selection-report report_path must be a confined Markdown path under artifacts/"
            )
        if report_path.exists() or report_path.is_symlink():
            raise SkillRuntimeError("selection-report artifact already exists")
        report_path.parent.mkdir(parents=True, exist_ok=True)
        if not _inside(artifacts, report_path.parent) or report_path.parent.is_symlink():
            raise SkillRuntimeError("selection-report artifact directory is unsafe")
        markdown = render_selection_report(report)
        _write_new_file(report_path, markdown.encode("utf-8"))
        return report.to_dict()

    def _audit(self, skill: str, operation_id: str, path: Path) -> None:
        audit = self.context.workspace / "outbox" / "skill_activity.jsonl"
        artifact_sha256 = hashlib.sha256(path.read_bytes()).hexdigest()
        with audit.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    {
                        "time": _now(),
                        "status": "succeeded",
                        "skill": skill,
                        "operation_id": operation_id,
                        "artifact_sha256": artifact_sha256,
                        "relative_path": str(path.relative_to(self.context.workspace)),
                        "call_id": self.context.workspace.name,
                        "role": self.context.policy.role,
                        "mode": self.context.policy.mode,
                        "task_id": self.context.task_id,
                        "attempt": self.context.attempt,
                    },
                    sort_keys=True,
                )
                + "\n"
            )
            handle.flush()
            os.fsync(handle.fileno())

    def _task_writing(self, value: dict[str, Any]) -> dict[str, Any]:
        if value.pop("batch_finalized", False) is not True:
            raise SkillRuntimeError("task-writing is allowed only after the whole batch is finalized")
        mode = str(value.get("work_mode", "")).replace("_", "-")
        if mode not in {
            "research",
            "brainstorm",
            "associate",
            "multi-discipline",
            "reformulate",
            "computation",
            "proof-writer",
        }:
            raise SkillRuntimeError("assign_report has an invalid work_mode")
        portfolio = value.get("assignment_portfolio")
        if not isinstance(portfolio, Mapping) or set(portfolio) != set(PORTFOLIO_TYPES):
            raise SkillRuntimeError("assignment_portfolio must contain exactly the six typed lists")
        unique: set[str] = set()
        for memory_type in PORTFOLIO_TYPES:
            entries = portfolio[memory_type]
            if not isinstance(entries, list) or not all(isinstance(item, str) for item in entries):
                raise SkillRuntimeError(f"portfolio {memory_type} entries must be ID strings")
            unique.update(entries)
        if len(unique) > 20:
            raise SkillRuntimeError("assignment portfolio exceeds the explicit limit of 20 memories")
        routes = value.get("main_route_ids") or ([] if value.get("main_route_id") is None else [value["main_route_id"]])
        obligations = value.get("main_obligation_ids") or []
        perspective = value.get("selected_new_perspective")
        if mode == "research" and len(routes) != 1:
            raise SkillRuntimeError("research mode requires exactly one main route")
        if mode == "brainstorm" and len(obligations) != 1:
            raise SkillRuntimeError("brainstorm mode requires exactly one main obligation")
        if mode == "brainstorm" and any(
            portfolio[k] for k in PORTFOLIO_TYPES if k != "fact"
        ):
            raise SkillRuntimeError(
                "brainstorm's distinguished obligation is embedded outside its fact-only portfolio"
            )
        if mode == "multi-discipline" and (not obligations or not str(perspective or "").strip()):
            raise SkillRuntimeError("multi-discipline requires obligations and a seed perspective")
        if mode == "computation" and not (
            routes or obligations or portfolio["route"] or portfolio["obligation"]
        ):
            raise SkillRuntimeError("computation mode requires a route or obligation")
        if mode == "proof-writer" and not value.get("root_solution_fact_id"):
            raise SkillRuntimeError("proof-writer requires the active root solution fact ID")
        value["work_mode"] = mode
        value["main_route_ids"] = list(routes)
        value["main_obligation_ids"] = list(obligations)
        value.setdefault("if_resume", None)
        value.setdefault("selected_new_perspective", None)
        return value

    def _record_progress(self, value: dict[str, Any]) -> dict[str, Any]:
        if not self.context.task_id or not self.context.attempt:
            raise SkillRuntimeError("record-progress requires a materialized task and attempt")
        if "computations" in value:
            raise SkillRuntimeError(
                "record-progress cannot contain computation bodies; cite authenticated CAS operation IDs"
            )
        operations = value.get("operations", [])
        if not isinstance(operations, list):
            raise SkillRuntimeError("record-progress operations must be a list")
        seen_operation_ids: set[str] = set()
        sort_run_id = None
        if self.context.policy.role == "main-sort":
            card = self.context.task_card or {}
            sort_run_id = str(card.get("sort_run_id") or "")
            if not sort_run_id:
                raise SkillRuntimeError("main-sort task card has no sort_run_id")
        for operation in operations:
            if not isinstance(operation, Mapping):
                raise SkillRuntimeError("record-progress operations must be objects")
            operation_id = operation.get("operation_id")
            if (
                not isinstance(operation_id, str)
                or not operation_id
                or operation_id != operation_id.strip()
            ):
                raise SkillRuntimeError(
                    "each record-progress operation requires a nonempty exact string operation_id"
                )
            if operation_id in seen_operation_ids:
                raise SkillRuntimeError(
                    "record-progress operation_id values must be unique within the progress record"
                )
            seen_operation_ids.add(operation_id)
            provenance = operation.get("explorer_provenance")
            if self.context.policy.role != "main-sort":
                if provenance is not None:
                    raise SkillRuntimeError(
                        "Explorer provenance is available only to main-sort"
                    )
                continue
            try:
                validate_main_sort_progress_operation(
                    operation, sort_run_id=sort_run_id
                )
            except MainSortContractError as exc:
                raise SkillRuntimeError(str(exc)) from exc
        computation_ids = value.get("computation_operation_ids", [])
        if (
            not isinstance(computation_ids, list)
            or not all(isinstance(item, str) and item for item in computation_ids)
            or len(set(computation_ids)) != len(computation_ids)
        ):
            raise SkillRuntimeError(
                "computation_operation_ids must be a duplicate-free list of string IDs"
            )
        value["computation_operation_ids"] = list(computation_ids)
        explorer_computations = value.get("explorer_computation_promotions", [])
        if self.context.policy.role != "main-sort":
            if explorer_computations:
                raise SkillRuntimeError(
                    "Explorer computation promotion is available only to main-sort"
                )
            value.pop("explorer_computation_promotions", None)
        else:
            try:
                value["explorer_computation_promotions"] = (
                    normalize_main_sort_computation_promotions(
                        explorer_computations
                    )
                )
            except MainSortContractError as exc:
                raise SkillRuntimeError(str(exc)) from exc
        task_id = value.get("task_id", self.context.task_id)
        attempt = int(value.get("attempt", self.context.attempt))
        if task_id != self.context.task_id or attempt != self.context.attempt:
            raise SkillRuntimeError("record-progress cannot target another task or attempt")
        sequence = value.get("sequence")
        if not isinstance(sequence, int) or sequence < 1:
            raise SkillRuntimeError("record-progress sequence must be a positive integer")
        directory = self.context.workspace / "outbox" / "record-progress"
        existing: list[Mapping[str, Any]] = []
        if directory.exists():
            for path in directory.glob("*.json"):
                try:
                    existing.append(json.loads(path.read_text(encoding="utf-8")))
                except json.JSONDecodeError:
                    continue
        prior = [entry for entry in existing if entry.get("attempt") == attempt]
        expected = max((int(entry.get("sequence", 0)) for entry in prior), default=0) + 1
        if sequence != expected:
            raise SkillRuntimeError(f"record-progress sequence must be {expected}")
        if any(entry.get("is_final") for entry in prior):
            raise SkillRuntimeError("this attempt already has its final progress record")
        is_final = bool(value.get("is_final", value.get("final", False)))
        if is_final and value.get("outcome_status") not in {"finished", "progress", "failed"}:
            raise SkillRuntimeError("final progress requires finished, progress, or failed")
        if is_final:
            summary = value.get("attempt_summary")
            if not isinstance(summary, Mapping):
                raise SkillRuntimeError("final progress requires an attempt_summary object")
            required = {
                "work_mode",
                "task",
                "proposed_outcome",
                "cumulative_important_progress",
                "completion_evidence_operation_ids",
                "most_promising_next_steps",
            }
            missing = sorted(required - set(summary))
            if missing:
                raise SkillRuntimeError(
                    "attempt_summary is missing: " + ", ".join(missing)
                )
            if summary.get("proposed_outcome") != value.get("outcome_status"):
                raise SkillRuntimeError(
                    "attempt_summary proposed_outcome must match outcome_status"
                )
            evidence = _completion_evidence_ids(
                value.get("completion_evidence_ids"),
                field="completion_evidence_ids",
            )
            summary_evidence = _completion_evidence_ids(
                summary.get("completion_evidence_operation_ids"),
                field="attempt_summary completion_evidence_operation_ids",
            )
            if set(evidence) != set(summary_evidence):
                raise SkillRuntimeError(
                    "attempt_summary completion evidence must match completion_evidence_ids"
                )
        value.update(
            {
                "progress_id": value.get("progress_id") or f"PRG-{uuid.uuid4()}",
                "task_id": task_id,
                "attempt": attempt,
                "sequence": sequence,
                "is_final": is_final,
            }
        )
        value.pop("final", None)
        return value

    def _cas_record(self, value: dict[str, Any]) -> dict[str, Any]:
        required = {
            "software",
            "software_version",
            "exact_input",
            "exact_output",
            "exit_status",
            "description",
            "assumptions",
            "interpretation",
        }
        missing = sorted(required - set(value))
        if missing:
            raise SkillRuntimeError(f"CAS record is missing: {', '.join(missing)}")
        if not all(
            str(value[field]).strip()
            for field in ("software", "description", "assumptions", "interpretation")
        ):
            raise SkillRuntimeError(
                "CAS software, description, assumptions, and interpretation must be nonempty"
            )
        value.setdefault("task_id", self.context.task_id)
        value.setdefault("environment_versions", {})
        value.setdefault("error_output", "")
        value.setdefault("random_seed", None)
        value.setdefault("related_ids", {})
        return value

    def _human_guidance(self, value: dict[str, Any]) -> dict[str, Any]:
        pdf_value = value.get("pdf_path")
        if not isinstance(pdf_value, str):
            raise SkillRuntimeError("human-guidance requires a compiled PDF path")
        pdf = Path(pdf_value)
        if not pdf.is_absolute():
            pdf = self.context.workspace / pdf
        artifacts = self.context.workspace / "artifacts"
        if not _inside(artifacts, pdf) or not pdf.is_file() or pdf.is_symlink():
            raise SkillRuntimeError("human-guidance PDF must be a regular artifact file")
        if pdf.read_bytes()[:5] != b"%PDF-":
            raise SkillRuntimeError("human-guidance artifact is not a compiled PDF")
        if not value.get("question"):
            raise SkillRuntimeError("human-guidance requires the question for the operator")
        value.setdefault("request_id", f"HG-{uuid.uuid4()}")
        value["pdf_path"] = str(pdf.relative_to(self.context.workspace))
        return value

    @staticmethod
    def _discovery_sprint(value: dict[str, Any]) -> dict[str, Any]:
        decision = value.get("decision")
        if decision == "no_sprint":
            if not str(value.get("reason", "")).strip():
                raise SkillRuntimeError("no_sprint requires a concise reason")
            return value
        if decision != "plan":
            raise SkillRuntimeError("discovery-sprint decision must be no_sprint or plan")
        lanes = value.get("lanes")
        if not isinstance(lanes, list) or len(lanes) != 4:
            raise SkillRuntimeError("a discovery sprint requires exactly four lane blueprints")
        modes = [lane.get("mode") for lane in lanes if isinstance(lane, Mapping)]
        if modes != ["brainstorm", "multi-discipline", "computation", "associate"]:
            raise SkillRuntimeError("discovery sprint lanes must be A/B/C/D in the specified modes")
        target_id = value.get("target_obligation_id")
        target_revision = value.get("target_obligation_revision")
        if (
            not isinstance(target_id, str)
            or not target_id.strip()
            or not isinstance(target_revision, int)
            or isinstance(target_revision, bool)
            or target_revision < 1
            or not isinstance(value.get("target_statement"), str)
            or not value["target_statement"].strip()
        ):
            raise SkillRuntimeError(
                "sprint plan requires one target obligation ID, positive revision, and statement"
            )

        for index, lane in enumerate(lanes):
            if not isinstance(lane, Mapping):
                raise SkillRuntimeError("every discovery-sprint lane must be an object")
            label = "ABCD"[index]
            lane_label = lane.get("lane")
            if lane_label is not None and lane_label != label:
                raise SkillRuntimeError(f"discovery-sprint lane {label} has the wrong label")
            if not str(lane.get("objective", "")).strip():
                raise SkillRuntimeError(f"discovery-sprint lane {label} needs an objective")
            if lane.get("main_obligation_ids") != [target_id]:
                raise SkillRuntimeError(
                    f"discovery-sprint lane {label} must target only {target_id}"
                )
            if lane.get("main_route_ids") not in (None, []):
                raise SkillRuntimeError(
                    f"discovery-sprint lane {label} may not have a main route"
                )
            portfolio = lane.get("assignment_portfolio")
            if not isinstance(portfolio, Mapping) or set(portfolio) != set(
                PORTFOLIO_TYPES
            ):
                raise SkillRuntimeError(
                    f"discovery-sprint lane {label} needs exactly the six portfolio lists"
                )
            material_ids: list[str] = []
            for memory_type in PORTFOLIO_TYPES:
                ids = portfolio[memory_type]
                if not isinstance(ids, list) or not all(
                    isinstance(memory_id, str) for memory_id in ids
                ):
                    raise SkillRuntimeError(
                        f"discovery-sprint lane {label} portfolio {memory_type} must be an ID list"
                    )
                material_ids.extend(ids)
            if target_id in material_ids:
                raise SkillRuntimeError(
                    f"discovery-sprint lane {label} must carry its target as the main obligation, not portfolio material"
                )
            perspective = lane.get("selected_new_perspective")
            if label == "B":
                if not isinstance(perspective, str) or not perspective.strip():
                    raise SkillRuntimeError(
                        "discovery-sprint lane B needs one selected new perspective"
                    )
            elif perspective is not None:
                raise SkillRuntimeError(
                    f"selected_new_perspective is allowed only for discovery-sprint lane B, not {label}"
                )
            if label in {"A", "B"} and any(
                portfolio[k] for k in PORTFOLIO_TYPES if k != "fact"
            ):
                raise SkillRuntimeError(
                    f"discovery-sprint lane {label} may receive only active facts besides its target"
                )
            if label == "C":
                computation_portfolio = lane.get("computation_portfolio")
                if not isinstance(computation_portfolio, list) or not computation_portfolio:
                    raise SkillRuntimeError(
                        "discovery-sprint lane C needs an explicit computation portfolio"
                    )
                if not all(
                    isinstance(item, str) and item.strip()
                    for item in computation_portfolio
                ):
                    raise SkillRuntimeError(
                        "discovery-sprint lane C computation portfolio must contain nonempty instructions"
                    )
            if label == "D":
                if len(material_ids) < 2 or len(material_ids) > 4:
                    raise SkillRuntimeError(
                        "discovery-sprint lane D needs two to four distant material IDs"
                    )
                if len(set(material_ids)) != len(material_ids):
                    raise SkillRuntimeError(
                        "discovery-sprint lane D material IDs must be distinct"
                    )
            reason = lane.get("reason", lane.get("material_and_omissions_reason"))
            if not isinstance(reason, str) or not reason.strip():
                raise SkillRuntimeError(
                    f"discovery-sprint lane {label} must explain supplied material and omissions"
                )
        return value


def execute_cas(
    context: SkillContext,
    payload: Mapping[str, Any],
    *,
    configured_executables: Mapping[str, str] | None = None,
    timeout_seconds: float | None = None,
    denied_paths: Iterable[str | os.PathLike[str]] = (),
    process_scope: CASProcessScope | None = None,
    deadline: float | None = None,
) -> dict[str, Any]:
    """Execute one configured CAS command without a shell and stage its record."""

    timeout = CAS_TIMEOUT_SECONDS if timeout_seconds is None else timeout_seconds
    if not math.isfinite(timeout) or timeout <= 0:
        raise SkillRuntimeError("CAS timeout must be a finite positive number")
    local_deadline = time.monotonic() + min(timeout, CAS_TIMEOUT_SECONDS)
    if deadline is not None and not math.isfinite(deadline):
        raise SkillRuntimeError("CAS deadline must be finite")
    deadline = min(local_deadline, deadline) if deadline is not None else local_deadline
    process_scope = process_scope or CASProcessScope()
    process_scope.check()
    if "CAS" not in allowed_skills(context.policy):
        raise SkillRuntimeError(
            f"CAS is unavailable for {context.policy.role}/{context.policy.mode}"
    )
    configured = configured_executables or {}
    software = payload.get("software")
    if (
        not isinstance(configured, Mapping)
        or not isinstance(software, str)
        or software not in configured
    ):
        raise SkillRuntimeError("requested CAS is not configured for this call")
    executable = _trusted_executable(configured[software], str(software))
    arguments = payload.get("arguments", [])
    if not isinstance(arguments, list) or not all(isinstance(arg, str) for arg in arguments):
        raise SkillRuntimeError("CAS arguments must be a string list")
    exact_input_value = payload.get("exact_input")
    if not isinstance(exact_input_value, str):
        raise SkillRuntimeError("CAS exact_input must be text")
    exact_input = exact_input_value
    execution_arguments = list(arguments)
    execution_input: str | None = exact_input
    if software.casefold() == "sage" and not execution_arguments:
        # Sage treats stdin as an interactive REPL.  In particular, a compound
        # statement at EOF can be left unexecuted while the process still exits
        # successfully.  Pass the exact program as one shell-free batch
        # argument so EOF cannot silently discard trailing input.
        execution_arguments = ["-c", exact_input]
        execution_input = None
    completed = _run_constrained(
        context,
        executable,
        execution_arguments,
        input_text=execution_input,
        timeout_seconds=max(0.0, deadline - time.monotonic()),
        denied_paths=denied_paths,
        process_scope=process_scope,
        deadline=deadline,
    )
    version_args = payload.get("version_arguments", ["--version"])
    if not isinstance(version_args, list) or not all(
        isinstance(arg, str) for arg in version_args
    ):
        raise SkillRuntimeError("CAS version_arguments must be a string list")
    version = _run_constrained(
        context,
        executable,
        version_args,
        timeout_seconds=max(0.0, deadline - time.monotonic()),
        denied_paths=denied_paths,
        process_scope=process_scope,
        deadline=deadline,
    )
    process_scope.check()
    record = dict(payload)
    # The task archive retains the complete staged invocation.  Canonical
    # computation memory stores the same reproducibility data in its declared
    # schema instead of leaking skill-control arguments as unknown fields.
    environment_versions = dict(record.get("environment_versions", {}))
    environment_versions.setdefault("executable", str(executable))
    environment_versions.setdefault(
        "invocation_arguments", json.dumps(execution_arguments, ensure_ascii=False)
    )
    environment_versions.setdefault(
        "network_confinement",
        "filesystem-and-network-denied-launcher",
    )
    record.pop("arguments", None)
    record.pop("version_arguments", None)
    record.update(
        {
            "software": software,
            "software_version": (version.stdout or version.stderr).strip(),
            "exact_input": exact_input,
            "exact_output": completed.stdout,
            "error_output": completed.stderr,
            "exit_status": completed.returncode,
            "environment_versions": environment_versions,
        }
    )
    output_artifact = record.get("output_artifact")
    if output_artifact is not None:
        if not isinstance(output_artifact, Mapping):
            raise SkillRuntimeError("output_artifact must be an object")
        artifact = _artifact_file(context, output_artifact.get("path"))
        record["output_artifact"] = {
            "path": str(artifact.relative_to(context.workspace)),
            "sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
        }
    staged = SkillRuntime(context).invoke("CAS", record)
    # A broker receipt authenticates staging, not mathematical success.  Keep
    # failed executions as nonauthoritative audit records and expose their
    # status explicitly; progress ingestion separately prevents them from
    # becoming canonical computation evidence.
    staged["execution_succeeded"] = completed.returncode == 0
    return staged


def compile_human_guidance(
    context: SkillContext,
    payload: Mapping[str, Any],
    *,
    tectonic_executable: str | os.PathLike[str] | None,
    timeout_seconds: float | None = None,
    denied_paths: Iterable[str | os.PathLike[str]] = (),
) -> dict[str, Any]:
    """Compile one trimmer report with trusted Tectonic, validate it, and stage it."""

    if context.policy.role != "trimmer":
        raise SkillRuntimeError("human-guidance is available only to the trimmer")
    unexpected = set(payload) - {"latex_path", "question", "request_id", "operation_id"}
    if unexpected:
        raise SkillRuntimeError(
            "human-guidance contains unsupported fields: " + ", ".join(sorted(unexpected))
        )
    question = payload.get("question")
    if not isinstance(question, str) or not question.strip():
        raise SkillRuntimeError("human-guidance requires a question for the operator")
    source = _artifact_file(context, payload.get("latex_path"), suffix=".tex")
    executable = _trusted_executable(tectonic_executable, "Tectonic")
    build_root = context.workspace / "tmp"
    if build_root.is_symlink() or not _inside(context.workspace, build_root):
        raise SkillRuntimeError("unsafe human-guidance build directory")
    build = build_root / f"human-guidance-{uuid.uuid4().hex}"
    build.mkdir(mode=0o700)
    target = context.workspace / "artifacts" / source.with_suffix(".pdf").name
    if target.exists() or target.is_symlink():
        raise SkillRuntimeError("human-guidance PDF target already exists")
    try:
        compiler_reads = _tectonic_cache_paths()
        completed = _run_constrained(
            context,
            executable,
            [
                "--only-cached",
                "--untrusted",
                "--color",
                "never",
                "--outdir",
                str(build),
                str(source),
            ],
            timeout_seconds=timeout_seconds,
            read_paths=compiler_reads,
            denied_paths=denied_paths,
            use_host_home=True,
        )
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout).strip()
            raise SkillRuntimeError(
                "human-guidance report did not compile successfully"
                + (f": {detail}" if detail else "")
            )
        compiled = build / source.with_suffix(".pdf").name
        if compiled.is_symlink() or not compiled.is_file():
            raise SkillRuntimeError("Tectonic did not produce the expected PDF")
        data = compiled.read_bytes()
        if not data.startswith(b"%PDF-"):
            raise SkillRuntimeError("Tectonic output is not a PDF")
        _write_new_file(target, data)
        version = _run_constrained(
            context,
            executable,
            ["--version"],
            timeout_seconds=timeout_seconds,
            read_paths=compiler_reads,
            denied_paths=denied_paths,
            use_host_home=True,
        )
        staged = dict(payload)
        staged.pop("latex_path", None)
        staged.update(
            {
                "pdf_path": str(target.relative_to(context.workspace)),
                "source_latex_path": str(source.relative_to(context.workspace)),
                "compiler": "tectonic",
                "compiler_version": (version.stdout or version.stderr).strip(),
            }
        )
        return SkillRuntime(context).invoke("human-guidance", staged)
    except Exception:
        target.unlink(missing_ok=True)
        raise
    finally:
        if _inside(build_root, build):
            shutil.rmtree(build, ignore_errors=True)


def _load_json(path: str) -> Mapping[str, Any]:
    if path == "-":
        value = json.load(sys.stdin)
    else:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, Mapping):
        raise SkillRuntimeError("skill input must be a JSON object")
    return value


def _broker_client_from_environment() -> BrokerClient:
    capability_path = os.environ.get("FRANTA_BROKER_CAPABILITY_FILE")
    if not capability_path:
        raise SkillRuntimeError("Franta MCP broker capability is unavailable")
    path = Path(capability_path)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SkillRuntimeError("invalid Franta MCP broker capability") from exc
    socket_path = value.get("socket_path")
    token = value.get("token")
    if not isinstance(socket_path, str) or not isinstance(token, str):
        raise SkillRuntimeError("malformed Franta MCP broker capability")
    # Do not leave the secret in an environment variable inherited by descendants.
    os.environ.pop("FRANTA_BROKER_CAPABILITY_FILE", None)
    return BrokerClient(socket_path, token)


def _tool_definitions(
    enabled: Iterable[str], *, configured_cas_software: Iterable[str] = ()
) -> list[dict[str, Any]]:
    supplied_cas_names = tuple(configured_cas_software)
    if any(
        not isinstance(name, str)
        or not name
        or name != name.strip()
        or "/" in name
        or "\\" in name
        for name in supplied_cas_names
    ):
        raise SkillRuntimeError("configured CAS choices must be path-free names")
    cas_names = tuple(sorted(set(supplied_cas_names)))
    definitions = {
        "internal_search": {
            "name": "internal_search",
            "description": "Search authorized project-memory abstracts; read results before fetching full records.",
            "inputSchema": {
                "type": "object",
                "required": ["query", "memory_types"],
                "properties": {
                    "query": {"type": "string", "minLength": 1},
                    "memory_types": {
                        "type": "array",
                        "minItems": 1,
                        "items": {"enum": sorted(SEARCHABLE_MEMORY_TYPES)},
                    },
                    "limit": {"type": "integer", "minimum": 1, "maximum": 10},
                    "include_inactive": {"type": "boolean"},
                    "include_withdrawn": {"type": "boolean"},
                },
                "additionalProperties": False,
            },
        },
        "memory_fetch": {
            "name": "memory_fetch",
            "description": "Fetch one complete authorized record by canonical ID when its abstract is insufficient.",
            "inputSchema": {
                "type": "object",
                "required": ["memory_id"],
                "properties": {"memory_id": {"type": "string"}},
                "additionalProperties": False,
            },
        },
        **explorer_tool_definitions(
            SEARCHABLE_MEMORY_TYPES,
            host_name="Franta",
        ),
        "fact_dependency_closure": {
            "name": "fact_dependency_closure",
            "description": "List abstracts in the active root-solution fact dependency closure and authorize on-demand fetch.",
            "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        },
        "task_summary": {
            "name": "task_summary",
            "description": "Read one authorized task summary and its artifact descriptors before selecting any full artifact.",
            "inputSchema": {
                "type": "object",
                "required": ["task_id"],
                "properties": {"task_id": {"type": "string"}},
                "additionalProperties": False,
            },
        },
        "task_artifact_fetch": {
            "name": "task_artifact_fetch",
            "description": "Fetch one selected authorized full task artifact when its summary is insufficient.",
            "inputSchema": {
                "type": "object",
                "required": ["task_id", "artifact_id"],
                "properties": {
                    "task_id": {"type": "string"},
                    "artifact_id": {"type": "string"},
                },
                "additionalProperties": False,
            },
        },
        "task_writing": {
            "name": "task_writing",
            "description": "Stage one already-finalized worker assignment without changing its mathematics.",
            "inputSchema": {
                "type": "object",
                "required": ["payload"],
                "properties": {
                    "payload": {"type": "object", "additionalProperties": True}
                },
                "additionalProperties": False,
            },
        },
        "selection_report": {
            "name": "selection_report",
            "description": (
                "Validate exactly five ranked Advisor obligations against the active "
                "round, render their human-facing Markdown report, and stage the "
                "immutable structured report."
            ),
            "inputSchema": {
                "type": "object",
                "required": ["payload"],
                "properties": {
                    "payload": {"type": "object", "additionalProperties": True}
                },
                "additionalProperties": False,
            },
        },
        "record_progress": {
            "name": "record_progress",
            "description": "Stage one immutable worker progress record, including the required final record before normal exit.",
            "inputSchema": {
                "type": "object",
                "required": ["payload"],
                "properties": {
                    "payload": {"type": "object", "additionalProperties": True}
                },
                "additionalProperties": False,
            },
        },
        "discovery_sprint": {
            "name": "discovery_sprint",
            "description": "Stage one trimmer decision: no sprint, or one finalized four-lane isolated sprint plan.",
            "inputSchema": {
                "type": "object",
                "required": ["payload"],
                "properties": {
                    "payload": {"type": "object", "additionalProperties": True}
                },
                "additionalProperties": False,
            },
        },
        "human_guidance": {
            "name": "human_guidance",
            "description": "Compile a trimmer-authored LaTeX report with configured Tectonic and stage the validated guidance request.",
            "inputSchema": {
                "type": "object",
                "required": ["latex_path", "question"],
                "properties": {
                    "latex_path": {"type": "string", "minLength": 1},
                    "question": {"type": "string", "minLength": 1},
                    "request_id": {"type": "string", "minLength": 1},
                    "operation_id": {"type": "string", "minLength": 1},
                },
                "additionalProperties": False,
            },
        },
        "execute_cas": {
            "name": "execute_cas",
            "description": (
                "Run one configured CAS reproducibly without a shell or network and "
                "stage its nonauthoritative record. Configured choices: "
                + ", ".join(cas_names)
                + "."
            ),
            "inputSchema": {
                "type": "object",
                "required": [
                    "software",
                    "exact_input",
                    "description",
                    "assumptions",
                    "interpretation",
                ],
                "properties": {
                    "software": {"type": "string", "enum": list(cas_names)},
                    "arguments": {"type": "array", "items": {"type": "string"}},
                    "version_arguments": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "exact_input": {"type": "string"},
                    "description": {"type": "string", "minLength": 1},
                    "assumptions": {"type": "string", "minLength": 1},
                    "environment_versions": {"type": "object"},
                    "random_seed": {},
                    "interpretation": {"type": "string"},
                    "related_ids": {
                        "type": "object",
                        "properties": {
                            name: {
                                "type": "array",
                                "items": {"type": "string"},
                            }
                            for name in (
                                "fact",
                                "route",
                                "memo",
                                "claim",
                                "obligation",
                            )
                        },
                        "additionalProperties": False,
                    },
                    "fact_candidate_operation_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "output_artifact": {"type": "object"},
                    "operation_id": {"type": "string", "minLength": 1},
                },
                "additionalProperties": False,
            },
        },
    }
    return [definitions[name] for name in sorted(enabled)]


def mcp_server() -> int:
    """Run a small MCP stdio adapter; all authorization remains in the broker."""

    client = _broker_client_from_environment()
    description = client.call("describe", {})
    enabled = frozenset(description.get("enabled_tools", ()))
    tools = _tool_definitions(
        enabled,
        configured_cas_software=description.get("configured_cas_software", ()),
    )
    for line in sys.stdin:
        if not line.strip():
            continue
        request: Mapping[str, Any] = {}
        try:
            request = json.loads(line)
            method = request.get("method")
            request_id = request.get("id")
            if method == "initialize":
                result = {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "franta-memory", "version": "1"},
                }
            elif method == "tools/list":
                result = {"tools": tools}
            elif method == "tools/call":
                params = request.get("params", {})
                name = params.get("name")
                arguments = params.get("arguments", {})
                if name not in enabled:
                    raise SkillRuntimeError("MCP tool exceeds the launch-bound policy")
                value = client.call(str(name), arguments)
                result = {
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps(value, ensure_ascii=False, sort_keys=True),
                        }
                    ],
                    "structuredContent": {"result": value},
                    "isError": False,
                }
            elif method == "ping":
                result = {}
            elif request_id is None:
                continue
            else:
                raise SkillRuntimeError(f"unsupported MCP method: {method}")
            response = {"jsonrpc": "2.0", "id": request_id, "result": result}
        except Exception as exc:
            response = {
                "jsonrpc": "2.0",
                "id": request.get("id") if isinstance(request, Mapping) else None,
                "error": {"code": -32000, "message": str(exc)},
            }
        sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
        sys.stdout.flush()
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m franta.skill_runtime")
    subparsers = parser.add_subparsers(dest="command", required=True)
    stage = subparsers.add_parser("stage")
    stage.add_argument("skill")
    stage.add_argument("input", nargs="?", default="-")
    stage.add_argument("--workspace")
    cas = subparsers.add_parser("execute-cas")
    cas.add_argument("input", nargs="?", default="-")
    cas.add_argument("--workspace")
    subparsers.add_parser("mcp-server")
    args = parser.parse_args(argv)
    try:
        if args.command == "mcp-server":
            return mcp_server()
        context = SkillContext.load(args.workspace)
        payload = _load_json(args.input)
        result = (
            execute_cas(context, payload)
            if args.command == "execute-cas"
            else SkillRuntime(context).invoke(args.skill, payload)
        )
        json.dump(result, sys.stdout, ensure_ascii=False, sort_keys=True)
        sys.stdout.write("\n")
        return 0
    except Exception as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover - exercised by Codex/MCP processes
    raise SystemExit(main())


__all__ = [
    "SKILLS",
    "SkillContext",
    "SkillRuntime",
    "SkillRuntimeError",
    "allowed_skills",
    "compile_human_guidance",
    "execute_cas",
    "main",
    "mcp_server",
]
