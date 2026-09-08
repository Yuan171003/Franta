"""Policy-filtered task-summary and archived-artifact reads."""

from __future__ import annotations

from typing import Any, Mapping, Protocol

from ..contracts.agent_access import AccessPolicy, _ID_RE
from .audit import AuditLog
from .memory import AccessError


class TaskBackend(Protocol):
    """Read-only scheduler view used by the audited task-search capability."""

    def get_task_summary(self, task_id: str) -> Mapping[str, Any] | None: ...

    def get_task_artifact(
        self, task_id: str, artifact_id: str
    ) -> Mapping[str, Any] | None: ...


class AuditedTaskAPI:
    """Policy-filtered task summary and selected-artifact access."""

    def __init__(
        self,
        backend: TaskBackend,
        policy: AccessPolicy,
        *,
        audit: AuditLog,
        caller_id: str,
    ) -> None:
        self.backend = backend
        self.policy = policy
        self.audit = audit
        self.caller_id = caller_id

    @staticmethod
    def _task_id(value: Any) -> str:
        if not isinstance(value, str) or not value.startswith("T-") or not _ID_RE.fullmatch(value):
            raise AccessError("task-search requires a canonical T- task ID")
        return value

    def summary(self, task_id: str) -> dict[str, Any]:
        if not self.policy.task_summary_api:
            raise AccessError("task summary access is unavailable for this call")
        task_id = self._task_id(task_id)
        value = self.backend.get_task_summary(task_id)
        if value is None:
            raise AccessError(f"unknown task ID: {task_id}")
        result = dict(value)
        self.audit.append(
            {
                "action": "task_summary_read",
                "caller_id": self.caller_id,
                "policy": self.policy.name,
                "task_id": task_id,
            }
        )
        return result

    def fetch(self, task_id: str, artifact_id: str) -> dict[str, Any]:
        if not self.policy.task_artifact_api:
            raise AccessError("task artifact access is unavailable for this call")
        task_id = self._task_id(task_id)
        if not isinstance(artifact_id, str) or not artifact_id or len(artifact_id) > 160:
            raise AccessError("task_artifact_fetch requires one artifact ID")
        value = self.backend.get_task_artifact(task_id, artifact_id)
        if value is None:
            raise AccessError(f"unknown task artifact: {task_id}/{artifact_id}")
        result = dict(value)
        self.audit.append(
            {
                "action": "task_artifact_read",
                "caller_id": self.caller_id,
                "policy": self.policy.name,
                "task_id": task_id,
                "artifact_id": artifact_id,
            }
        )
        return result


__all__ = ["AuditedTaskAPI", "TaskBackend"]
