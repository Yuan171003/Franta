"""Launch-bound broker transport joining Block 6 tools to Block 5 reads."""

from __future__ import annotations

import hmac
import errno
import json
import math
import os
import re
import secrets
import shutil
import socket
import socketserver
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from ..contracts.agent_access import (
    AccessPolicy,
    CAS_TOOL_ARGUMENTS,
    STAGING_SKILL_BY_TOOL,
    STAGING_TOOL_BY_SKILL,
    _ID_RE,
    staging_skills_for_policy,
)
from ..read_access.audit import AuditLog
from ..read_access.memory import AccessError, AuditedMemoryAPI, MemoryBackend
from ..read_access.tasks import AuditedTaskAPI, TaskBackend
from .cas_process import CAS_TIMEOUT_SECONDS


_CAS_SOFTWARE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.+-]{0,127}$")
# sockaddr_un.sun_path has 104 bytes on macOS, including its trailing NUL.
# Keep one portable limit and count filesystem-encoded bytes, not characters.
_UNIX_SOCKET_PATH_MAX_BYTES = 103


class BrokerError(RuntimeError):
    """The memory broker rejected a malformed or unauthenticated request."""


@dataclass(frozen=True)
class BrokerBinding:
    socket_path: str
    token: str = field(repr=False)
    enabled_tools: tuple[str, ...] = ()


@dataclass
class _Capability:
    token: str
    api: AuditedMemoryAPI
    task_api: AuditedTaskAPI | None
    explorer_api: Any | None
    enabled_tools: frozenset[str]
    staging_handler: Callable[[str, Mapping[str, Any]], Mapping[str, Any]] | None = None
    cas_software_names: tuple[str, ...] = ()
    cas_handler: Callable[[Mapping[str, Any], float], Mapping[str, Any]] | None = None
    on_revoke: Callable[[], None] | None = None


class _BrokerUnixServer(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True
    allow_reuse_address = False


class _BrokerHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        line = self.rfile.readline(1_048_577)
        if not line or len(line) > 1_048_576:
            return
        try:
            request = json.loads(line)
            response = self.server.broker.dispatch(request)  # type: ignore[attr-defined]
            value = {"ok": True, "result": response}
        except Exception as exc:  # the broker boundary must return a closed error
            value = {"ok": False, "error": str(exc), "error_type": type(exc).__name__}
        self.wfile.write((json.dumps(value, ensure_ascii=False) + "\n").encode("utf-8"))


class MemoryBroker:
    """Scheduler-owned authenticated broker for the local Franta MCP adapter."""

    def __init__(
        self,
        backend: MemoryBackend,
        *,
        task_backend: TaskBackend | None = None,
        audit: AuditLog | None = None,
    ) -> None:
        self.backend = backend
        self.task_backend = task_backend
        self.audit = audit or AuditLog()
        self._capabilities: dict[str, _Capability] = {}
        self._lock = threading.Lock()
        self._server: _BrokerUnixServer | None = None
        self._thread: threading.Thread | None = None
        self._spool_dir: Path | None = None
        self._stop_event = threading.Event()
        self.socket_path: str | None = None

    def start(self, socket_path: str | os.PathLike[str]) -> None:
        if not hasattr(socket, "AF_UNIX"):
            raise RuntimeError("the Franta memory broker requires Unix-domain sockets")
        if self._server is not None or self._spool_dir is not None:
            raise RuntimeError("broker already started")
        path = Path(socket_path).resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() or path.is_symlink():
            raise RuntimeError(f"broker socket path already exists: {path}")
        try:
            if len(os.fsencode(path)) > _UNIX_SOCKET_PATH_MAX_BYTES:
                raise OSError(errno.ENAMETOOLONG, "Unix-domain socket path is too long")
            server = _BrokerUnixServer(str(path), _BrokerHandler)
        except OSError as exc:
            if not isinstance(exc, PermissionError) and exc.errno not in {
                errno.EACCES, errno.EPERM, errno.ENAMETOOLONG,
            }:
                raise
            # Some managed hosts forbid binding and some project paths exceed
            # the platform's Unix-domain socket address size. A private
            # scheduler-owned request spool preserves the same capability
            # boundary and keeps the agent shell away from canonical state.
            spool = Path(str(path) + ".spool")
            if spool.exists() or spool.is_symlink():
                raise RuntimeError(f"broker spool path already exists: {spool}")
            spool.mkdir(mode=0o700)
            self._spool_dir = spool
            self.socket_path = "file://" + str(spool)
            self._stop_event.clear()
            self._thread = threading.Thread(target=self._serve_spool, daemon=True)
            self._thread.start()
            return
        server.broker = self  # type: ignore[attr-defined]
        os.chmod(path, 0o600)
        self._server = server
        self.socket_path = str(path)
        self._thread = threading.Thread(target=server.serve_forever, daemon=True)
        self._thread.start()

    def _serve_spool(self) -> None:
        assert self._spool_dir is not None
        while not self._stop_event.is_set():
            found = False
            for request_path in self._spool_dir.glob("*.request"):
                found = True
                stem = request_path.name.removesuffix(".request")
                processing = self._spool_dir / f"{stem}.processing"
                try:
                    os.replace(request_path, processing)
                except FileNotFoundError:
                    continue
                try:
                    request = json.loads(processing.read_text(encoding="utf-8"))
                    response = {"ok": True, "result": self.dispatch(request)}
                except Exception as exc:
                    response = {
                        "ok": False,
                        "error": str(exc),
                        "error_type": type(exc).__name__,
                    }
                response_path = self._spool_dir / f"{stem}.response"
                temporary = self._spool_dir / f"{stem}.response.new"
                descriptor = os.open(
                    temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
                )
                with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                    json.dump(response, handle, ensure_ascii=False)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, response_path)
                processing.unlink(missing_ok=True)
            if not found:
                self._stop_event.wait(0.02)

    def stop(self) -> None:
        self._stop_event.set()
        with self._lock:
            bindings = tuple(
                BrokerBinding(self.socket_path or "", token)
                for token, capability in self._capabilities.items()
                if capability.on_revoke is not None
            )
        errors: list[Exception] = []
        for binding in bindings:
            try:
                self.revoke(binding)
            except Exception as exc:
                errors.append(exc)
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=2)
        path = self.socket_path
        self._server = None
        self._thread = None
        self.socket_path = None
        spool = self._spool_dir
        self._spool_dir = None
        if path:
            if not path.startswith("file://"):
                try:
                    Path(path).unlink()
                except FileNotFoundError:
                    pass
        if spool is not None:
            shutil.rmtree(spool, ignore_errors=True)
        if errors:
            raise errors[0]

    def issue(
        self,
        policy: AccessPolicy,
        *,
        caller_id: str,
        root_fact_id: str | None = None,
        staging_handler: Callable[[str, Mapping[str, Any]], Mapping[str, Any]] | None = None,
        staging_skills: Iterable[str] = (),
        cas_software_names: Iterable[str] = (),
        explorer_api: Any | None = None,
        cas_handler: Callable[[Mapping[str, Any], float], Mapping[str, Any]] | None = None,
        on_revoke: Callable[[], None] | None = None,
    ) -> BrokerBinding:
        if self.socket_path is None:
            raise RuntimeError("broker must be started before issuing a capability")
        tools: set[str] = set()
        if policy.project_memory_api and policy.explorer_access_policy_version != 2:
            tools.update({"internal_search", "memory_fetch"})
        if policy.dependency_closure_only:
            if not root_fact_id or not _ID_RE.fullmatch(root_fact_id):
                raise ValueError("proof-writer capability requires a canonical root fact ID")
            tools.update({"fact_dependency_closure", "memory_fetch"})
        explorer_access_mode = policy.explorer_access_mode
        if explorer_access_mode is not None:
            if policy.explorer_access_policy_version != 2:
                raise AccessError("unsupported Explorer access policy version")
            if explorer_api is None:
                raise RuntimeError(
                    "Explorer v2 policy requires a grant-bound Explorer API"
                )
            if not policy.explorer_access_grant_id or not (
                policy.explorer_access_grant_digest
            ):
                raise AccessError("Explorer v2 policy lacks its immutable grant")
            if explorer_access_mode == "check-result":
                tools.add("check_result")
            elif explorer_access_mode == "portfolio":
                tools.update(
                    {"check_result", "portfolio_search", "portfolio_fetch"}
                )
            elif explorer_access_mode == "full-memory":
                tools.update({"explorer_search", "explorer_fetch"})
            else:
                raise AccessError("unknown Explorer access mode")
        elif policy.explorer_memory_api:
            if explorer_api is None:
                raise RuntimeError("Explorer policy requires a scope-bound Explorer API")
            tools.update({"explorer_search", "explorer_fetch"})
        elif explorer_api is not None:
            raise AccessError("Explorer API exceeds the launch-bound role")
        task_api: AuditedTaskAPI | None = None
        if policy.task_summary_api or policy.task_artifact_api:
            if self.task_backend is None:
                raise RuntimeError("task-search policy requires a task backend")
            task_api = AuditedTaskAPI(
                self.task_backend,
                policy,
                audit=self.audit,
                caller_id=caller_id,
            )
            if policy.task_summary_api:
                tools.add("task_summary")
            if policy.task_artifact_api:
                tools.add("task_artifact_fetch")
        stage_names = frozenset(staging_skills)
        unknown_staging = stage_names - set(STAGING_TOOL_BY_SKILL)
        if unknown_staging:
            raise ValueError(f"unsupported broker staging skills: {sorted(unknown_staging)}")
        unauthorized_staging = stage_names - staging_skills_for_policy(policy)
        if unauthorized_staging:
            raise AccessError(
                "staging skills exceed the launch-bound role: "
                + ", ".join(sorted(unauthorized_staging))
            )
        if stage_names and staging_handler is None:
            raise ValueError("broker staging skills require a staging handler")
        if isinstance(cas_software_names, (str, bytes)):
            raise ValueError("CAS software names must be an iterable of trusted names")
        supplied_cas_names = tuple(cas_software_names)
        if any(
            not isinstance(name, str) or not _CAS_SOFTWARE_NAME_RE.fullmatch(name)
            for name in supplied_cas_names
        ):
            raise ValueError("CAS software names must be path-free trusted names")
        cas_names = tuple(sorted(set(supplied_cas_names)))
        if "CAS" in stage_names and not cas_names:
            raise ValueError("CAS staging requires at least one configured software name")
        if "CAS" not in stage_names and cas_names:
            raise AccessError("CAS software names require a CAS-authorized launch")
        tools.update(STAGING_TOOL_BY_SKILL[skill] for skill in stage_names)
        if not tools:
            raise AccessError("this call has no audited broker capability")
        token = secrets.token_urlsafe(32)
        api = AuditedMemoryAPI(
            self.backend,
            policy,
            audit=self.audit,
            caller_id=caller_id,
            root_fact_id=root_fact_id,
        )
        with self._lock:
            self._capabilities[token] = _Capability(
                token,
                api,
                task_api,
                explorer_api,
                frozenset(tools),
                staging_handler,
                cas_names,
                cas_handler,
                on_revoke,
            )
        self.audit.append(
            {
                "action": "broker_capability_issued",
                "caller_id": caller_id,
                "policy": policy.name,
                "tools": sorted(tools),
            }
        )
        return BrokerBinding(self.socket_path, token, tuple(sorted(tools)))

    def revoke(self, binding: BrokerBinding) -> None:
        capability = None
        with self._lock:
            for token in list(self._capabilities):
                if hmac.compare_digest(token, binding.token):
                    capability = self._capabilities.pop(token)
                    break
        if capability is not None:
            try:
                if capability.on_revoke is not None:
                    capability.on_revoke()
            finally:
                self.audit.append(
                    {
                        "action": "broker_capability_revoked",
                        "caller_id": capability.api.caller_id,
                        "policy": capability.api.policy.name,
                    }
                )

    @staticmethod
    def _only(args: Mapping[str, Any], allowed: frozenset[str]) -> None:
        unexpected = set(args) - allowed
        if unexpected:
            raise BrokerError(f"unexpected broker arguments: {sorted(unexpected)}")

    def dispatch(self, request: Mapping[str, Any]) -> Any:
        fields = {"token", "operation", "arguments"}
        if set(request) != fields and not (
            request.get("operation") == "execute_cas"
            and set(request) == fields | {"deadline"}
        ):
            raise BrokerError("malformed broker request")
        token = request.get("token")
        operation = request.get("operation")
        arguments = request.get("arguments")
        if not isinstance(token, str) or not isinstance(operation, str):
            raise BrokerError("malformed broker authentication or operation")
        if not isinstance(arguments, Mapping):
            raise BrokerError("broker arguments must be an object")
        with self._lock:
            capability = self._capabilities.get(token)
        if capability is None:
            self.audit.append({"action": "broker_auth_rejected"})
            raise BrokerError("invalid or revoked broker capability")
        if operation == "describe":
            self._only(arguments, frozenset())
            return {
                "enabled_tools": sorted(capability.enabled_tools),
                "allowed_memory_types": sorted(capability.api.policy.allowed_memory_types),
                "configured_cas_software": list(capability.cas_software_names),
            }
        if operation not in capability.enabled_tools:
            raise AccessError("operation exceeds the launch-bound capability")
        cas_deadline = time.monotonic() + CAS_TIMEOUT_SECONDS
        if operation == "execute_cas":
            supplied_deadline = request.get("deadline", cas_deadline)
            if (
                isinstance(supplied_deadline, bool)
                or not isinstance(supplied_deadline, (int, float))
                or not math.isfinite(supplied_deadline)
            ):
                raise BrokerError("CAS deadline must be a finite number")
            cas_deadline = min(cas_deadline, supplied_deadline)
            if time.monotonic() >= cas_deadline:
                raise BrokerError("CAS request timed out before execution")
        if operation == "internal_search":
            self._only(
                arguments,
                frozenset(
                    {
                        "query",
                        "memory_types",
                        "limit",
                        "include_inactive",
                        "include_withdrawn",
                    }
                ),
            )
            return capability.api.search(
                str(arguments.get("query", "")),
                arguments.get("memory_types", ()),
                limit=int(arguments.get("limit", 10)),
                include_inactive=bool(arguments.get("include_inactive", False)),
                include_withdrawn=bool(arguments.get("include_withdrawn", False)),
            )
        if operation == "memory_fetch":
            self._only(arguments, frozenset({"memory_id"}))
            memory_id = arguments.get("memory_id")
            if not isinstance(memory_id, str):
                raise BrokerError("memory_id must be a string")
            return capability.api.fetch(memory_id)
        if operation == "explorer_search":
            self._only(
                arguments,
                frozenset(
                    {
                        "query",
                        "record_types",
                        "limit",
                        "include_inactive",
                        "include_withdrawn",
                    }
                ),
            )
            if capability.explorer_api is None:
                raise AccessError("Explorer search is unavailable for this call")
            return capability.explorer_api.search(
                str(arguments.get("query", "")),
                arguments.get("record_types", ()),
                limit=int(arguments.get("limit", 10)),
                include_inactive=bool(arguments.get("include_inactive", False)),
                include_withdrawn=bool(arguments.get("include_withdrawn", False)),
            )
        if operation == "explorer_fetch":
            self._only(arguments, frozenset({"record_id"}))
            if capability.explorer_api is None:
                raise AccessError("Explorer fetch is unavailable for this call")
            record_id = arguments.get("record_id")
            if not isinstance(record_id, str):
                raise BrokerError("record_id must be a string")
            return capability.explorer_api.fetch(record_id)
        if operation == "check_result":
            self._only(arguments, frozenset({"kind", "statement"}))
            if capability.explorer_api is None:
                raise AccessError("check-result is unavailable for this call")
            kind = arguments.get("kind")
            statement = arguments.get("statement")
            if not isinstance(kind, str) or not isinstance(statement, str):
                raise BrokerError("check-result requires string kind and statement")
            return capability.explorer_api.check_result(kind, statement)
        if operation == "portfolio_search":
            self._only(arguments, frozenset({"query", "limit"}))
            if capability.explorer_api is None:
                raise AccessError("portfolio-search is unavailable for this call")
            query = arguments.get("query")
            if not isinstance(query, str):
                raise BrokerError("portfolio-search query must be a string")
            return capability.explorer_api.portfolio_search(
                query, limit=int(arguments.get("limit", 10))
            )
        if operation == "portfolio_fetch":
            self._only(arguments, frozenset({"portfolio_item_id"}))
            if capability.explorer_api is None:
                raise AccessError("portfolio fetch is unavailable for this call")
            portfolio_item_id = arguments.get("portfolio_item_id")
            if not isinstance(portfolio_item_id, str):
                raise BrokerError("portfolio_item_id must be a string")
            return capability.explorer_api.portfolio_fetch(portfolio_item_id)
        if operation == "fact_dependency_closure":
            self._only(arguments, frozenset())
            return capability.api.dependency_closure()
        if operation == "task_summary":
            self._only(arguments, frozenset({"task_id"}))
            if capability.task_api is None:
                raise AccessError("task-search is unavailable for this call")
            return capability.task_api.summary(arguments.get("task_id"))
        if operation == "task_artifact_fetch":
            self._only(arguments, frozenset({"task_id", "artifact_id"}))
            if capability.task_api is None:
                raise AccessError("task-search is unavailable for this call")
            return capability.task_api.fetch(
                arguments.get("task_id"), arguments.get("artifact_id")
            )
        if operation in STAGING_SKILL_BY_TOOL:
            if operation in {
                "task_writing",
                "selection_report",
                "record_progress",
                "discovery_sprint",
                "record_scratch",
                "record_summary",
            }:
                self._only(arguments, frozenset({"payload"}))
                payload = arguments.get("payload")
                if not isinstance(payload, Mapping):
                    raise BrokerError(f"{operation} requires a payload object")
            elif operation == "human_guidance":
                self._only(
                    arguments,
                    frozenset({"latex_path", "question", "request_id", "operation_id"}),
                )
                payload = arguments
            else:
                self._only(arguments, CAS_TOOL_ARGUMENTS)
                payload = arguments
                software = payload.get("software")
                if software not in capability.cas_software_names:
                    raise AccessError("CAS software is not configured for this launch")
            if capability.staging_handler is None:
                raise AccessError("skill staging is unavailable for this call")
            skill = STAGING_SKILL_BY_TOOL[operation]
            try:
                if skill == "CAS" and capability.cas_handler is not None:
                    result = capability.cas_handler(payload, cas_deadline)
                else:
                    result = capability.staging_handler(skill, payload)
            except Exception as exc:
                self.audit.append(
                    {
                        "action": "broker_skill_failed",
                        "caller_id": capability.api.caller_id,
                        "policy": capability.api.policy.name,
                        "skill": skill,
                        "tool": operation,
                        "error_type": type(exc).__name__,
                    }
                )
                raise
            self.audit.append(
                {
                    "action": "broker_skill_succeeded",
                    "caller_id": capability.api.caller_id,
                    "policy": capability.api.policy.name,
                    "skill": skill,
                    "tool": operation,
                    "operation_id": result.get("operation_id"),
                }
            )
            return result
        raise BrokerError(f"unknown broker operation: {operation}")


class BrokerClient:
    """Minimal client used only by the MCP subprocess."""

    def __init__(self, socket_path: str, token: str, *, timeout: float | None = None) -> None:
        self.socket_path = socket_path
        self.token = token
        self.timeout = 30.0 if timeout is None else timeout
        self.cas_timeout = CAS_TIMEOUT_SECONDS if timeout is None else timeout

    def call(self, operation: str, arguments: Mapping[str, Any]) -> Any:
        timeout = self.cas_timeout if operation == "execute_cas" else self.timeout
        deadline = time.monotonic() + timeout
        request_value = {
            "token": self.token,
            "operation": operation,
            "arguments": dict(arguments),
        }
        if operation == "execute_cas":
            # The local broker uses the same monotonic clock.  Carry the
            # original budget through queueing and both CAS subprocesses.
            request_value["deadline"] = deadline
        if self.socket_path.startswith("file://"):
            return self._call_spool(
                request_value,
                deadline=deadline if operation == "execute_cas" else None,
            )
        request = json.dumps(request_value, ensure_ascii=False).encode("utf-8") + b"\n"
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(timeout)
            connection.connect(self.socket_path)
            connection.sendall(request)
            received = bytearray()
            while not received.endswith(b"\n"):
                if operation == "execute_cas":
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise BrokerError("CAS request timed out")
                    connection.settimeout(remaining)
                chunk = connection.recv(65_536)
                if not chunk:
                    break
                received.extend(chunk)
                if len(received) > 4_194_304:
                    raise BrokerError("broker response exceeded size limit")
        if not received:
            raise BrokerError("broker closed without a response")
        response = json.loads(received)
        if not response.get("ok"):
            raise BrokerError(str(response.get("error", "broker request failed")))
        return response.get("result")

    def _call_spool(
        self, request: Mapping[str, Any], *, deadline: float | None = None
    ) -> Any:
        spool = Path(self.socket_path.removeprefix("file://"))
        request_id = uuid.uuid4().hex
        request_path = spool / f"{request_id}.request"
        response_path = spool / f"{request_id}.response"
        temporary = spool / f"{request_id}.request.new"
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(dict(request), handle, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, request_path)
        if deadline is None:
            deadline = time.monotonic() + self.timeout
        while not response_path.exists():
            if time.monotonic() >= deadline:
                request_path.unlink(missing_ok=True)
                raise BrokerError("broker request timed out")
            time.sleep(0.02)
        try:
            response = json.loads(response_path.read_text(encoding="utf-8"))
        finally:
            response_path.unlink(missing_ok=True)
        if not response.get("ok"):
            raise BrokerError(str(response.get("error", "broker request failed")))
        return response.get("result")


__all__ = [
    "BrokerBinding",
    "BrokerClient",
    "BrokerError",
    "MemoryBroker",
]
