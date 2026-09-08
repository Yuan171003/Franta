"""Persistent Codex CLI JSONL transport for Franta agent calls."""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from .broker import BrokerBinding
from .cas_process import CAS_TIMEOUT_SECONDS
from .permissions import CodexPermissionProfile
from ..contracts.agent_access import AccessPolicy
from ..prompts import ModelConfig, model_config


_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_FRANTA_STAGE_COMMAND = re.compile(
    r"(?<![A-Za-z0-9_.-])(?:[^\s\"']*/)?python(?:3(?:\.\d+)*)?\s+"
    r"-m\s+franta\.skill_runtime\s+stage\s+"
    r"(task-writing|selection-report|record-progress)(?=\s|[\"']|$)"
)
_FRANTA_MCP_SKILLS = {
    "task_writing": "task-writing",
    "task-writing": "task-writing",
    "selection_report": "selection-report",
    "selection-report": "selection-report",
    "record_progress": "record-progress",
    "record-progress": "record-progress",
    "discovery_sprint": "discovery-sprint",
    "discovery-sprint": "discovery-sprint",
    "human_guidance": "human-guidance",
    "human-guidance": "human-guidance",
    "execute_cas": "CAS",
    "CAS": "CAS",
}

# A Franta call is a new Codex process even when it resumes a deliberately
# selected Codex conversation.  In particular, it must not inherit the host
# Codex task identity that launched the scheduler.
_FRANTA_ENVIRONMENT_KEYS = frozenset(
    {
        "ALL_PROXY",
        "COLORTERM",
        "CURL_CA_BUNDLE",
        "HOME",
        "HTTPS_PROXY",
        "HTTP_PROXY",
        "LANG",
        "LOGNAME",
        "NO_PROXY",
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
        "OPENAI_ORGANIZATION",
        "OPENAI_PROJECT",
        "PATH",
        "REQUESTS_CA_BUNDLE",
        "SHELL",
        "SSL_CERT_DIR",
        "SSL_CERT_FILE",
        "TERM",
        "USER",
        "all_proxy",
        "https_proxy",
        "http_proxy",
        "no_proxy",
    }
)


class CodexTransportError(RuntimeError):
    def __init__(self, message: str, *, result: "CodexResult | None" = None) -> None:
        super().__init__(message)
        self.result = result


@dataclass(frozen=True)
class CodexRequest:
    call_id: str
    role: str
    prompt: str
    workspace: str | os.PathLike[str]
    policy: AccessPolicy
    permission_profile: CodexPermissionProfile
    session_key: str | None = None
    resume: bool = False
    resume_thread_id: str | None = None
    broker_binding: BrokerBinding | None = None
    timeout_seconds: float | None = None
    output_schema: str | os.PathLike[str] | None = None
    model_config: ModelConfig | None = None
    lease_epoch: int = 1
    launch_attempt: int = 1


@dataclass(frozen=True)
class CodexResult:
    call_id: str
    thread_id: str | None
    resumed: bool
    returncode: int
    final_message: str
    events: tuple[Mapping[str, Any], ...] = ()
    stderr: str = ""


@dataclass(frozen=True)
class _Completed:
    returncode: int
    events: tuple[Mapping[str, Any], ...]
    stderr: str
    invalid_lines: tuple[dict[str, Any], ...] = ()
    cancel_kind: str | None = None
    cancel_reason: str | None = None


Runner = Callable[..., Any]


@dataclass
class _ActiveProcess:
    call_id: str
    process: Any
    audit_path: Path
    cancel_event: threading.Event = field(default_factory=threading.Event)
    cancel_reason: str | None = None
    cancel_kind: str | None = None
    cancel_requested_at: float | None = None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _toml(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return json.dumps(value, ensure_ascii=False)


def _append_jsonl(path: Path, value: Mapping[str, Any], lock: threading.Lock) -> None:
    encoded = json.dumps(dict(value), ensure_ascii=False, sort_keys=True) + "\n"
    with lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())


class ThreadLedger:
    """Append-only logical-session to Codex-thread bindings."""

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()

    def record(
        self,
        *,
        session_key: str,
        thread_id: str,
        call_id: str,
        role: str,
    ) -> None:
        _append_jsonl(
            self.path,
            {
                "type": "thread_binding",
                "time": _now(),
                "session_key": session_key,
                "thread_id": thread_id,
                "call_id": call_id,
                "role": role,
            },
            self._lock,
        )

    def resolve(self, session_key: str) -> str | None:
        if not self.path.exists():
            return None
        found: str | None = None
        with self._lock, self.path.open("r", encoding="utf-8") as handle:
            for line in handle:
                try:
                    value = json.loads(line)
                except json.JSONDecodeError:
                    # A crash may leave only the final line incomplete.
                    continue
                if (
                    value.get("type") == "thread_binding"
                    and value.get("session_key") == session_key
                    and isinstance(value.get("thread_id"), str)
                ):
                    found = value["thread_id"]
        return found


def parse_jsonl_events(text: str) -> tuple[dict[str, Any], ...]:
    events: list[dict[str, Any]] = []
    for number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise CodexTransportError(f"invalid Codex JSONL at stdout line {number}") from exc
        if not isinstance(value, dict):
            raise CodexTransportError(f"Codex JSONL line {number} is not an object")
        events.append(value)
    return tuple(events)


def _thread_id(events: Iterable[Mapping[str, Any]]) -> str | None:
    for event in events:
        if event.get("type") not in {"thread.started", "thread_started"}:
            continue
        for key in ("thread_id", "threadId", "id"):
            value = event.get(key)
            if isinstance(value, str) and value:
                return value
        thread = event.get("thread")
        if isinstance(thread, Mapping):
            for key in ("id", "thread_id", "threadId"):
                value = thread.get(key)
                if isinstance(value, str) and value:
                    return value
    return None


def _final_message(events: Iterable[Mapping[str, Any]]) -> str:
    messages: list[str] = []
    for event in events:
        item = event.get("item")
        if isinstance(item, Mapping) and item.get("type") in {
            "agent_message",
            "assistant_message",
        }:
            text = item.get("text") or item.get("content")
            if isinstance(text, str):
                messages.append(text)
        elif event.get("type") in {"agent_message", "assistant_message"}:
            text = event.get("text") or event.get("message")
            if isinstance(text, str):
                messages.append(text)
    return messages[-1] if messages else ""


def _tool_activity(event: Mapping[str, Any]) -> dict[str, Any] | None:
    item = event.get("item")
    if not isinstance(item, Mapping):
        return None
    item_type = str(item.get("type", ""))
    if item_type not in {
        "command_execution",
        "mcp_tool_call",
        "web_search",
        "web_search_call",
        "tool_call",
    }:
        return None
    result: dict[str, Any] = {
        "event_type": event.get("type"),
        "tool_type": item_type,
        "item_id": item.get("id"),
        "status": item.get("status"),
    }
    for key in ("command", "server", "tool", "name", "query", "exit_code"):
        if key in item:
            result[key] = item[key]
    if item_type == "mcp_tool_call" and str(item.get("server") or "") == "franta":
        arguments = item.get("arguments")
        if isinstance(arguments, Mapping):
            for key in (
                "query",
                "memory_types",
                "limit",
                "include_inactive",
                "include_withdrawn",
                "memory_id",
                "task_id",
                "artifact_id",
            ):
                if key in arguments:
                    result[key] = arguments[key]
        returned = item.get("result")
        if isinstance(returned, Mapping):
            structured = returned.get(
                "structured_content", returned.get("structuredContent")
            )
            values = structured.get("result") if isinstance(structured, Mapping) else None
            if isinstance(values, Sequence) and not isinstance(
                values, (str, bytes, bytearray)
            ):
                result_ids = [
                    str(value["id"])
                    for value in values
                    if isinstance(value, Mapping) and value.get("id")
                ]
                if result_ids:
                    result["result_ids"] = result_ids
    invocations = list(_franta_stage_invocations(item.get("command")))
    if item_type == "mcp_tool_call" and str(item.get("server") or "") == "franta":
        staged_skill = _FRANTA_MCP_SKILLS.get(str(item.get("tool") or ""))
        if staged_skill:
            invocations.append(staged_skill)
    if invocations:
        result["franta_skill_invocations"] = invocations
    return result


def _franta_stage_invocations(command: Any) -> tuple[str, ...]:
    """Identify actual Franta staging commands in an audited command event."""

    if isinstance(command, Sequence) and not isinstance(
        command, (str, bytes, bytearray)
    ):
        text = " ".join(str(part) for part in command)
    elif isinstance(command, str):
        text = command
    else:
        return ()
    return tuple(_FRANTA_STAGE_COMMAND.findall(text))


def _activity_succeeded(value: Mapping[str, Any]) -> bool:
    event_type = str(value.get("event_type") or "").strip().casefold()
    if event_type in {"item.started", "item_started", "tool.started", "tool_started"}:
        return False
    exit_code = value.get("exit_code")
    if exit_code is not None:
        try:
            if int(exit_code) != 0:
                return False
        except (TypeError, ValueError):
            return False
    status = str(value.get("status") or "").strip().casefold()
    if status in {"failed", "failure", "error", "cancelled", "canceled"}:
        return False
    if status in {
        "created",
        "in_progress",
        "incomplete",
        "pending",
        "queued",
        "running",
        "started",
    }:
        return False
    if status in {"completed", "complete", "success", "succeeded"}:
        return True
    return event_type in {"item.completed", "item_completed"} and exit_code is not None


class CodexTransport:
    """Launch/resume Codex sessions with sealed permissions and durable JSONL."""

    def __init__(
        self,
        state_dir: str | os.PathLike[str],
        *,
        codex_binary: str = "codex",
        runner: Runner | None = None,
        python_executable: str | None = None,
        source_root: str | os.PathLike[str] | None = None,
        host_codex_home: str | os.PathLike[str] | None = None,
        default_timeout_seconds: float | None = None,
        termination_grace_seconds: float = 5.0,
    ) -> None:
        self.state_dir = Path(state_dir).resolve()
        self.state_dir.mkdir(parents=True, exist_ok=True)
        try:
            self.state_dir.chmod(0o700)
        except OSError:
            pass
        self.codex_binary = codex_binary
        # ``runner`` is a process-factory injection point retained for tests;
        # production always uses the streaming/cancellable Popen primitive.
        self.runner = runner or subprocess.Popen
        self.python_executable = python_executable or sys.executable
        # This module lives one package level deeper than the legacy transport.
        # Preserve the original default of the repository's ``src`` root so
        # child MCP processes can still import ``franta``.
        self.source_root = Path(source_root or Path(__file__).resolve().parents[2]).resolve()
        configured_home = host_codex_home or os.environ.get("CODEX_HOME")
        self.host_codex_home = Path(configured_home or (Path.home() / ".codex")).resolve()
        self.codex_home = self.state_dir / "codex-home"
        if default_timeout_seconds is not None and default_timeout_seconds <= 0:
            raise ValueError("default_timeout_seconds must be positive")
        if termination_grace_seconds <= 0:
            raise ValueError("termination_grace_seconds must be positive")
        self.default_timeout_seconds = default_timeout_seconds
        self.termination_grace_seconds = float(termination_grace_seconds)
        self.ledger = ThreadLedger(self.state_dir / "threads.jsonl")
        self._audit_lock = threading.Lock()
        self._home_lock = threading.Lock()
        self._preflight_lock = threading.Lock()
        self._active_lock = threading.Lock()
        self._active: dict[str, _ActiveProcess] = {}
        self._single_agent_preflight_models: set[tuple[str, str]] = set()

    def successful_skill_invocation_count(self, call_id: str, skill: str) -> int:
        """Count successful, transport-audited Franta stage commands for one call."""

        if not _SAFE_ID.fullmatch(call_id):
            raise ValueError(f"unsafe call ID: {call_id!r}")
        path = self.state_dir / "tool_activity.jsonl"
        if not path.exists():
            return 0
        count = 0
        with self._audit_lock, path.open("r", encoding="utf-8") as handle:
            for line in handle:
                try:
                    value = json.loads(line)
                except json.JSONDecodeError:
                    # Recovery tolerates only a torn final append.
                    continue
                if value.get("call_id") != call_id or not _activity_succeeded(value):
                    continue
                names = value.get("franta_skill_invocations")
                if not isinstance(names, list):
                    names = list(_franta_stage_invocations(value.get("command")))
                count += sum(1 for name in names if name == skill)
        return count

    def completed_final_message(self, call_id: str) -> str | None:
        """Return the final message only when the audited call ended normally."""

        if not _SAFE_ID.fullmatch(call_id):
            raise ValueError(f"unsafe call ID: {call_id!r}")
        path = self.state_dir / "calls" / f"{call_id}.jsonl"
        if not path.exists():
            return None
        events: list[Mapping[str, Any]] = []
        returncode: int | None = None
        with self._audit_lock, path.open("r", encoding="utf-8") as handle:
            for line in handle:
                try:
                    value = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if value.get("type") == "transport.call_started":
                    events = []
                    returncode = None
                elif value.get("type") == "transport.codex_event" and isinstance(
                    value.get("event"), Mapping
                ):
                    events.append(value["event"])
                elif value.get("type") == "transport.call_ended":
                    try:
                        returncode = int(value.get("returncode"))
                    except (TypeError, ValueError):
                        returncode = None
        if returncode != 0:
            return None
        return _final_message(events)

    def audited_thread_id(self, call_id: str, *, role: str = "worker") -> str | None:
        """Recover the last unambiguous thread ID from one private call audit."""

        if not _SAFE_ID.fullmatch(call_id):
            raise ValueError(f"unsafe call ID: {call_id!r}")
        path = self.state_dir / "calls" / f"{call_id}.jsonl"
        if not path.exists():
            return None
        if path.is_symlink() or not path.is_file():
            raise CodexTransportError(f"unsafe Codex call audit for {call_id}")
        latest: str | None = None
        active = False
        observed: set[str] = set()
        with self._audit_lock, path.open("r", encoding="utf-8") as handle:
            for line in handle:
                try:
                    value = json.loads(line)
                except json.JSONDecodeError:
                    # A stopped process can tear only its final append.  Such
                    # bytes provide no thread evidence.
                    continue
                if not isinstance(value, Mapping):
                    continue
                event_type = value.get("type")
                if event_type == "transport.call_started":
                    if active:
                        if len(observed) > 1:
                            raise CodexTransportError(
                                f"conflicting audited thread IDs for {call_id}"
                            )
                        if observed:
                            latest = next(iter(observed))
                    active = (
                        value.get("call_id") == call_id
                        and value.get("role") == role
                    )
                    observed = set()
                    continue
                if not active:
                    continue
                candidate: str | None = None
                if event_type == "transport.codex_event" and isinstance(
                    value.get("event"), Mapping
                ):
                    candidate = _thread_id((value["event"],))
                elif event_type == "transport.call_ended":
                    raw = value.get("thread_id")
                    if isinstance(raw, str) and raw:
                        candidate = raw
                if candidate:
                    observed.add(candidate)
        if active:
            if len(observed) > 1:
                raise CodexTransportError(
                    f"conflicting audited thread IDs for {call_id}"
                )
            if observed:
                latest = next(iter(observed))
        return latest

    def recover_thread_binding(
        self,
        *,
        session_key: str,
        call_ids: Sequence[str],
        role: str = "worker",
    ) -> str | None:
        """Restore a missing ledger row from scheduler-linked call evidence."""

        persisted = self.ledger.resolve(session_key)
        if persisted is not None:
            return persisted
        observed: dict[str, str] = {}
        for call_id in call_ids:
            candidate = self.audited_thread_id(str(call_id), role=role)
            if candidate is not None:
                observed[str(call_id)] = candidate
        recovered_ids = set(observed.values())
        if len(recovered_ids) > 1:
            raise CodexTransportError(
                f"conflicting audited thread IDs for session {session_key}"
            )
        if not recovered_ids:
            return None
        recovered = next(iter(recovered_ids))
        source_call_id = next(
            call_id for call_id, thread_id in observed.items() if thread_id == recovered
        )
        self.ledger.record(
            session_key=session_key,
            thread_id=recovered,
            call_id=source_call_id,
            role=role,
        )
        return recovered

    def _effective_permissions(
        self, profile: CodexPermissionProfile
    ) -> CodexPermissionProfile:
        denied = tuple(dict.fromkeys((*profile.denied_paths, str(self.state_dir))))
        return CodexPermissionProfile(
            name=profile.name,
            denied_paths=denied,
            writable_relative_paths=profile.writable_relative_paths,
        )

    def _prepare_codex_home(self) -> None:
        """Create a private Codex home without host skills, plugins, or config.

        Authentication is copied once into the scheduler-private home.  Codex
        may refresh that private copy without mutating or repeatedly replacing
        the operator's login.  Session files then remain available for an
        intentional ``if_resume`` while no host-global skill directory is in
        the launched process's Codex home.
        """

        with self._home_lock:
            self.codex_home.mkdir(parents=True, exist_ok=True, mode=0o700)
            try:
                self.codex_home.chmod(0o700)
            except OSError:
                pass
            destination = self.codex_home / "auth.json"
            source = self.host_codex_home / "auth.json"
            if destination.exists() or not source.is_file() or source.is_symlink():
                return
            temporary = self.codex_home / "auth.json.new"
            try:
                with source.open("rb") as reader, temporary.open("xb") as writer:
                    shutil.copyfileobj(reader, writer)
                    writer.flush()
                    os.fsync(writer.fileno())
                temporary.chmod(0o600)
                os.replace(temporary, destination)
            finally:
                try:
                    temporary.unlink()
                except FileNotFoundError:
                    pass

    def _clean_environment(
        self,
        workspace: Path,
        capability_path: Path | None,
    ) -> dict[str, str]:
        """Build the host environment for one clean Franta Codex process."""

        environment = {
            key: value
            for key, value in os.environ.items()
            if key in _FRANTA_ENVIRONMENT_KEYS or key.startswith("LC_")
        }
        environment.update(
            {
                "CODEX_HOME": str(self.codex_home),
                "TMPDIR": str(workspace / "tmp"),
                "PYTHONPATH": str(self.source_root),
            }
        )
        if capability_path is not None:
            environment["FRANTA_BROKER_CAPABILITY_FILE"] = str(capability_path)
        return environment

    @staticmethod
    def _prepare_workspace_root(workspace: Path) -> None:
        """Stop Codex project discovery at the sealed call workspace."""

        marker = workspace / ".franta-root"
        if marker.exists() or marker.is_symlink():
            if marker.is_symlink() or not marker.is_file():
                raise CodexTransportError("unsafe Franta workspace root marker")
            return
        descriptor = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o444)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write("sealed Franta agent workspace\n")
            handle.flush()
            os.fsync(handle.fileno())

    @staticmethod
    def _signal_process(process: Any, *, force: bool) -> None:
        """Signal the whole launched process group without broad PID matching."""

        if process.poll() is not None:
            return
        try:
            if os.name == "posix" and isinstance(getattr(process, "pid", None), int):
                os.killpg(
                    os.getpgid(process.pid),
                    signal.SIGKILL if force else signal.SIGTERM,
                )
            elif force:
                process.kill()
            else:
                process.terminate()
        except (OSError, ProcessLookupError):
            return

    def _request_cancel(
        self,
        active: _ActiveProcess,
        *,
        kind: str,
        reason: str,
    ) -> bool:
        with self._active_lock:
            if active.process.poll() is not None or active.cancel_event.is_set():
                return False
            active.cancel_reason = str(reason)
            active.cancel_kind = str(kind)
            active.cancel_requested_at = time.monotonic()
            active.cancel_event.set()
        _append_jsonl(
            active.audit_path,
            {
                "type": "transport.cancel_requested",
                "time": _now(),
                "call_id": active.call_id,
                "kind": str(kind),
                "reason": str(reason),
            },
            self._audit_lock,
        )
        self._signal_process(active.process, force=False)
        return True

    def cancel(self, call_id: str, *, reason: str = "scheduler requested cancellation") -> bool:
        """Request cancellation of one currently running launch.

        The call's durable scheduler state remains authoritative.  This method
        only stops the process; the runtime records the resulting interruption.
        """

        with self._active_lock:
            active = self._active.get(call_id)
            if active is None or active.process.poll() is not None:
                return False
        return self._request_cancel(
            active,
            kind="scheduler",
            reason=str(reason),
        ) or active.cancel_event.is_set()

    @staticmethod
    def _config_arguments(overrides: Sequence[tuple[str, str]]) -> list[str]:
        result: list[str] = []
        for key, value in overrides:
            result.extend(["-c", f"{key}={value}"])
        return result

    def build_command(
        self,
        request: CodexRequest,
        *,
        thread_id: str | None,
    ) -> list[str]:
        workspace = str(Path(request.workspace).resolve())
        config = request.model_config or model_config(request.role)
        overrides: list[tuple[str, str]] = [
            ("model_reasoning_effort", _toml(config.reasoning_effort)),
            ("model_context_window", "872000"),
            ("model_auto_compact_token_limit", "780000"),
            ("approval_policy", _toml("never")),
            ("web_search", _toml("live" if request.policy.native_web_search else "disabled")),
            ("tools.web_search", "true" if request.policy.native_web_search else "false"),
            # Franta owns delegation and capability selection.  The worker sees
            # only the role skills materialized in its clean workspace.
            ("agents.enabled", "false"),
            ("features.multi_agent", "false"),
            ("features.multi_agent_v2", "false"),
            ("features.apps", "false"),
            ("features.enable_mcp_apps", "false"),
            ("features.plugins", "false"),
            ("features.remote_plugin", "false"),
            ("features.plugin_sharing", "false"),
            ("features.tool_suggest", "false"),
            ("include_apps_instructions", "false"),
            ("include_collaboration_mode_instructions", "false"),
            ("project_root_markers", _toml([".franta-root"])),
            ("features.skill_mcp_dependency_install", "false"),
            ("shell_environment_policy.set.PYTHONPATH", _toml(str(self.source_root))),
            ("shell_environment_policy.set.FRANTA_WORKSPACE", _toml(workspace)),
            (
                "shell_environment_policy.set.FRANTA_OUTBOX",
                _toml(str(Path(workspace) / "outbox")),
            ),
            (
                "shell_environment_policy.set.FRANTA_ARTIFACTS",
                _toml(str(Path(workspace) / "artifacts")),
            ),
            ("shell_environment_policy.set.TMPDIR", _toml(str(Path(workspace) / "tmp"))),
        ]
        if request.role == "worker":
            overrides.append(("features.goals", "false"))
        overrides.extend(self._effective_permissions(request.permission_profile).config_overrides())

        if request.broker_binding is not None:
            overrides.extend(
                [
                    ("mcp_servers.franta.command", _toml(self.python_executable)),
                    (
                        "mcp_servers.franta.args",
                        _toml(["-m", "franta.skill_runtime", "mcp-server"]),
                    ),
                    ("mcp_servers.franta.cwd", _toml(workspace)),
                    ("mcp_servers.franta.env.PYTHONPATH", _toml(str(self.source_root))),
                    (
                        "mcp_servers.franta.env_vars",
                        _toml(["FRANTA_BROKER_CAPABILITY_FILE"]),
                    ),
                    (
                        "mcp_servers.franta.enabled_tools",
                        _toml(list(request.broker_binding.enabled_tools)),
                    ),
                    ("mcp_servers.franta.required", "true"),
                    # The enabled list and capability token are launch-bound;
                    # only those exact tools may run without an interactive UI.
                    ("mcp_servers.franta.default_tools_approval_mode", _toml("approve")),
                ]
            )
            if "execute_cas" in request.broker_binding.enabled_tools:
                overrides.append(
                    ("mcp_servers.franta.tool_timeout_sec", str(int(CAS_TIMEOUT_SECONDS)))
                )

        if thread_id:
            command = [
                self.codex_binary,
                "exec",
                "resume",
                "--json",
                "--skip-git-repo-check",
                "--ignore-user-config",
                "--ignore-rules",
                "--model",
                config.model,
            ]
            command.extend(self._config_arguments(overrides))
            if request.output_schema is not None:
                command.extend(["--output-schema", str(Path(request.output_schema).resolve())])
            command.extend([thread_id, "-"])
            return command

        command = [
            self.codex_binary,
            "exec",
            "--json",
            "--color",
            "never",
            "--skip-git-repo-check",
            "--ignore-user-config",
            "--ignore-rules",
            "--model",
            config.model,
            "--cd",
            workspace,
        ]
        command.extend(self._config_arguments(overrides))
        if request.output_schema is not None:
            command.extend(["--output-schema", str(Path(request.output_schema).resolve())])
        command.append("-")
        return command

    def _preflight_single_agent_surface(
        self,
        request: CodexRequest,
        *,
        workspace: Path,
        environment: Mapping[str, str],
    ) -> None:
        """Fail closed unless Codex actually removes its sub-agent surface.

        Model metadata can select multi-agent instructions even when
        legacy feature flags are false.  Codex 0.148 supports the authoritative
        ``agents.enabled=false`` setting.  Render the model-visible prompt once
        per configured model before launching any production agent so a CLI
        regression cannot silently give workers an unscheduled delegation path.
        """

        # Process-factory injection is used by deterministic transport tests.
        # Production uses subprocess.Popen and receives the real CLI preflight.
        if self.runner is not subprocess.Popen:
            return
        config = request.model_config or model_config(request.role)
        key = (config.model, config.reasoning_effort)
        with self._preflight_lock:
            if key in self._single_agent_preflight_models:
                return
            overrides = [
                ("model", _toml(config.model)),
                ("model_reasoning_effort", _toml(config.reasoning_effort)),
                ("model_context_window", "872000"),
                ("model_auto_compact_token_limit", "780000"),
                ("agents.enabled", "false"),
                ("features.multi_agent", "false"),
                ("features.multi_agent_v2", "false"),
                ("include_collaboration_mode_instructions", "false"),
                ("project_root_markers", _toml([".franta-root"])),
            ]
            command = [self.codex_binary, "debug", "prompt-input"]
            command.extend(self._config_arguments(overrides))
            command.append("Franta single-agent transport preflight.")
            try:
                completed = subprocess.run(
                    command,
                    cwd=workspace,
                    env=dict(environment),
                    text=True,
                    capture_output=True,
                    timeout=30,
                    check=False,
                )
            except (OSError, subprocess.SubprocessError) as exc:
                raise CodexTransportError(
                    f"cannot verify the Codex single-agent surface: {exc}"
                ) from exc
            rendered = completed.stdout.casefold()
            forbidden = (
                "primary agent in a team",
                "<multi_agent_mode>",
                "spawn_agent",
                "functions.collaboration",
            )
            if completed.returncode != 0 or any(marker in rendered for marker in forbidden):
                detail = completed.stderr.strip() or "collaboration instructions remain visible"
                raise CodexTransportError(
                    "Codex did not honor agents.enabled=false; refusing to launch "
                    f"an agent with unscheduled sub-worker access: {detail}"
                )
            _append_jsonl(
                self.state_dir / "single-agent-preflight.jsonl",
                {
                    "type": "transport.single_agent_preflight",
                    "time": _now(),
                    "model": config.model,
                    "reasoning_effort": config.reasoning_effort,
                    "status": "passed",
                },
                self._audit_lock,
            )
            self._single_agent_preflight_models.add(key)

    def _write_capability_file(self, request: CodexRequest) -> Path | None:
        binding = request.broker_binding
        if binding is None:
            return None
        directory = self.state_dir / "broker-capabilities"
        directory.mkdir(mode=0o700, exist_ok=True)
        path = directory / f"{request.call_id}.json"
        if path.exists() or path.is_symlink():
            raise CodexTransportError(f"capability file already exists for {request.call_id}")
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(
                {"socket_path": binding.socket_path, "token": binding.token},
                handle,
                ensure_ascii=False,
            )
            handle.flush()
            os.fsync(handle.fileno())
        return path

    def _run(
        self,
        command: Sequence[str],
        *,
        call_id: str,
        role: str,
        audit_path: Path,
        prompt: str,
        cwd: Path,
        environment: Mapping[str, str],
        timeout: float | None,
    ) -> _Completed:
        spawn_arguments: dict[str, Any] = {
            "stdin": subprocess.PIPE,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "text": True,
            "cwd": str(cwd),
            "env": dict(environment),
            "bufsize": 1,
        }
        if os.name == "posix":
            spawn_arguments["start_new_session"] = True
        try:
            process = self.runner(list(command), **spawn_arguments)
        except Exception as exc:
            raise CodexTransportError(f"failed to start Codex: {exc}") from exc
        for attribute in ("stdin", "stdout", "stderr"):
            if getattr(process, attribute, None) is None:
                self._signal_process(process, force=True)
                raise CodexTransportError(
                    f"Codex process factory did not provide a {attribute} stream"
                )
        if not callable(getattr(process, "poll", None)) or not callable(
            getattr(process, "wait", None)
        ):
            self._signal_process(process, force=True)
            raise CodexTransportError("Codex process factory is not cancellable")

        active = _ActiveProcess(call_id=call_id, process=process, audit_path=audit_path)
        with self._active_lock:
            if call_id in self._active:
                self._signal_process(process, force=True)
                raise CodexTransportError(f"Codex call {call_id} is already running")
            self._active[call_id] = active

        deadline = time.monotonic() + timeout if timeout is not None else None
        deadline_at = (
            (datetime.now(timezone.utc) + timedelta(seconds=timeout)).isoformat()
            if timeout is not None
            else None
        )
        _append_jsonl(
            audit_path,
            {
                "type": "transport.process_started",
                "time": _now(),
                "call_id": call_id,
                "pid": getattr(process, "pid", None),
                "process_group_id": getattr(process, "pid", None)
                if os.name == "posix"
                else None,
                "timeout_seconds": timeout,
                "deadline_at": deadline_at,
            },
            self._audit_lock,
        )

        events: list[Mapping[str, Any]] = []
        invalid_lines: list[dict[str, Any]] = []
        stderr_parts: list[str] = []

        def read_stdout() -> None:
            for number, line in enumerate(process.stdout, 1):
                if not line.strip():
                    continue
                try:
                    value = json.loads(line)
                except json.JSONDecodeError as exc:
                    invalid = {
                        "line_number": number,
                        "line": line.rstrip("\r\n"),
                        "error": str(exc),
                    }
                    invalid_lines.append(invalid)
                    _append_jsonl(
                        audit_path,
                        {
                            "type": "transport.invalid_jsonl",
                            "time": _now(),
                            **invalid,
                        },
                        self._audit_lock,
                    )
                    continue
                if not isinstance(value, Mapping):
                    invalid = {
                        "line_number": number,
                        "line": line.rstrip("\r\n"),
                        "error": "Codex JSONL value is not an object",
                    }
                    invalid_lines.append(invalid)
                    _append_jsonl(
                        audit_path,
                        {
                            "type": "transport.invalid_jsonl",
                            "time": _now(),
                            **invalid,
                        },
                        self._audit_lock,
                    )
                    continue
                event = dict(value)
                events.append(event)
                _append_jsonl(
                    audit_path,
                    {"type": "transport.codex_event", "time": _now(), "event": event},
                    self._audit_lock,
                )
                activity = _tool_activity(event)
                if activity is not None:
                    _append_jsonl(
                        self.state_dir / "tool_activity.jsonl",
                        {
                            "type": "tool_activity",
                            "time": _now(),
                            "call_id": call_id,
                            "role": role,
                            **activity,
                        },
                        self._audit_lock,
                    )

        def read_stderr() -> None:
            for line in process.stderr:
                stderr_parts.append(str(line))

        stdout_thread = threading.Thread(
            target=read_stdout,
            name=f"franta-codex-stdout-{call_id}",
            daemon=True,
        )
        stderr_thread = threading.Thread(
            target=read_stderr,
            name=f"franta-codex-stderr-{call_id}",
            daemon=True,
        )
        stdout_thread.start()
        stderr_thread.start()

        try:
            try:
                process.stdin.write(prompt)
                process.stdin.flush()
            finally:
                process.stdin.close()
            while process.poll() is None:
                if deadline is not None and time.monotonic() >= deadline:
                    self._request_cancel(
                        active,
                        kind="timeout",
                        reason="Codex call exceeded its configured timeout",
                    )
                requested_at = active.cancel_requested_at
                if (
                    requested_at is not None
                    and time.monotonic() - requested_at >= self.termination_grace_seconds
                ):
                    self._signal_process(process, force=True)
                time.sleep(0.05)
            returncode = int(process.wait())
        except Exception:
            self._request_cancel(
                active,
                kind="transport",
                reason="transport failed while communicating with Codex",
            )
            self._signal_process(process, force=True)
            try:
                process.wait(timeout=self.termination_grace_seconds)
            except Exception:
                pass
            raise
        finally:
            stdout_thread.join(timeout=self.termination_grace_seconds)
            stderr_thread.join(timeout=self.termination_grace_seconds)
            for stream_name in ("stdout", "stderr"):
                try:
                    getattr(process, stream_name).close()
                except Exception:
                    pass
            with self._active_lock:
                if self._active.get(call_id) is active:
                    self._active.pop(call_id, None)

        return _Completed(
            returncode=returncode,
            events=tuple(events),
            stderr="".join(stderr_parts),
            invalid_lines=tuple(invalid_lines),
            cancel_kind=active.cancel_kind,
            cancel_reason=active.cancel_reason,
        )

    def invoke(self, request: CodexRequest) -> CodexResult:
        if not _SAFE_ID.fullmatch(request.call_id):
            raise ValueError(f"unsafe call ID: {request.call_id!r}")
        workspace = Path(request.workspace).resolve()
        if not workspace.is_dir() or workspace.is_symlink():
            raise CodexTransportError(f"missing or unsafe agent workspace: {workspace}")
        thread_id = request.resume_thread_id
        if request.resume and thread_id is None and request.session_key:
            thread_id = self.ledger.resolve(request.session_key)
        if request.resume and thread_id is None:
            raise CodexTransportError("resume requested without a persisted Codex thread ID")

        timeout = (
            request.timeout_seconds
            if request.timeout_seconds is not None
            else self.default_timeout_seconds
        )
        if timeout is not None and timeout <= 0:
            raise CodexTransportError("Codex timeout must be positive")
        self._prepare_workspace_root(workspace)
        self._prepare_codex_home()
        preflight_environment = self._clean_environment(workspace, None)
        self._preflight_single_agent_surface(
            request,
            workspace=workspace,
            environment=preflight_environment,
        )
        capability_path = self._write_capability_file(request)
        environment = self._clean_environment(workspace, capability_path)
        command = self.build_command(request, thread_id=thread_id)
        call_audit = self.state_dir / "calls" / f"{request.call_id}.jsonl"
        _append_jsonl(
            call_audit,
            {
                "type": "transport.call_started",
                "time": _now(),
                "call_id": request.call_id,
                "role": request.role,
                "session_key": request.session_key,
                "lease_epoch": int(request.lease_epoch),
                "launch_attempt": int(request.launch_attempt),
                "workspace_generation": (
                    f"{int(workspace.stat().st_dev)}:{int(workspace.stat().st_ino)}"
                ),
                "resumed": bool(thread_id),
                "input_hash": __import__("hashlib").sha256(
                    request.prompt.encode("utf-8")
                ).hexdigest(),
                "permission_profile": request.permission_profile.name,
                "native_web_search": request.policy.native_web_search,
                "broker_tools": list(request.broker_binding.enabled_tools)
                if request.broker_binding
                else [],
                "timeout_seconds": timeout,
                "clean_codex_home": str(self.codex_home),
                "model": (request.model_config or model_config(request.role)).model,
                "reasoning_effort": (
                    request.model_config or model_config(request.role)
                ).reasoning_effort,
            },
            self._audit_lock,
        )
        try:
            try:
                completed = self._run(
                    command,
                    call_id=request.call_id,
                    role=request.role,
                    audit_path=call_audit,
                    prompt=request.prompt,
                    cwd=workspace,
                    environment=environment,
                    timeout=timeout,
                )
            except Exception as exc:
                _append_jsonl(
                    call_audit,
                    {
                        "type": "transport.call_ended",
                        "time": _now(),
                        "returncode": None,
                        "thread_id": thread_id,
                        "transport_error": str(exc),
                    },
                    self._audit_lock,
                )
                if isinstance(exc, CodexTransportError):
                    raise
                raise CodexTransportError(
                    f"Codex transport failed: {exc}"
                ) from exc
        finally:
            if capability_path is not None:
                try:
                    capability_path.unlink()
                except FileNotFoundError:
                    pass

        events = completed.events
        parsed_thread = _thread_id(events) or thread_id
        result = CodexResult(
            call_id=request.call_id,
            thread_id=parsed_thread,
            resumed=bool(thread_id),
            returncode=completed.returncode,
            final_message=_final_message(events),
            events=events,
            stderr=completed.stderr,
        )
        _append_jsonl(
            call_audit,
            {
                "type": "transport.call_ended",
                "time": _now(),
                "returncode": completed.returncode,
                "thread_id": parsed_thread,
                "stderr": completed.stderr,
                "cancel_kind": completed.cancel_kind,
                "cancel_reason": completed.cancel_reason,
                "invalid_jsonl_lines": len(completed.invalid_lines),
            },
            self._audit_lock,
        )
        if parsed_thread and request.session_key:
            self.ledger.record(
                session_key=request.session_key,
                thread_id=parsed_thread,
                call_id=request.call_id,
                role=request.role,
            )
        if completed.cancel_kind == "timeout":
            raise CodexTransportError(
                "Codex call exceeded its configured timeout", result=result
            )
        if completed.cancel_kind is not None:
            raise CodexTransportError(
                completed.cancel_reason or "Codex call was cancelled", result=result
            )
        if completed.invalid_lines:
            raise CodexTransportError("invalid Codex JSONL output", result=result)
        if completed.returncode != 0:
            raise CodexTransportError(
                f"Codex exited with status {completed.returncode}", result=result
            )
        if parsed_thread is None:
            raise CodexTransportError("Codex JSONL did not contain a thread ID", result=result)
        return result


__all__ = [
    "CodexRequest",
    "CodexResult",
    "CodexTransport",
    "CodexTransportError",
    "ThreadLedger",
    "parse_jsonl_events",
]
