"""Operator feedback inbox; the research runner remains its sole state writer."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Mapping
import uuid

from .advisor_adapter import feedback_from_operator


def dashboard_directory(project: str | Path) -> Path:
    root = Path(project).resolve(strict=True)
    for path in (root / "private", root / "private" / "dashboard"):
        if path.is_symlink():
            raise ValueError("dashboard directory must not be a symlink")
        path.mkdir(mode=0o700, exist_ok=True)
        if not path.is_dir():
            raise ValueError("dashboard directory is unavailable")
    return root / "private" / "dashboard"


def _json(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise ValueError("unsafe dashboard command file")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("dashboard command must be a JSON object")
    return value


def publish_json(path: Path, value: Mapping[str, Any], *, replace: bool = False) -> None:
    """Publish a complete dashboard-owned file, preserving existing commands."""

    if path.parent.is_symlink() or path.is_symlink():
        raise ValueError("unsafe dashboard output path")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    data = (json.dumps(dict(value), ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
    try:
        with temporary.open("xb") as stream:
            os.chmod(temporary, 0o600)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if replace:
            os.replace(temporary, path)
        else:
            os.link(temporary, path)
        descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        temporary.unlink(missing_ok=True)


def submit_advisor_command(project: str | Path, request_id: str,
                           response: Mapping[str, Any], *, read_port: Any) -> dict[str, Any]:
    if not isinstance(request_id, str) or not request_id.strip():
        raise ValueError("Advisor request ID is required")
    if not isinstance(response, Mapping):
        raise ValueError("Advisor feedback must be an object")
    # A single immutable command belongs to each report, including double-clicks.
    command_id = "AFB-" + hashlib.sha256(request_id.encode("utf-8")).hexdigest()
    directory = dashboard_directory(project)
    path = directory / "commands" / f"{command_id}.json"
    exact = json.loads(json.dumps(dict(response), ensure_ascii=False))
    if path.exists():
        existing = _json(path)
        if existing.get("request_id") != request_id or existing.get("response") != exact:
            raise ValueError("This Advisor report already has different submitted feedback")
        receipt = directory / "receipts" / path.name
        return _json(receipt) if receipt.exists() else {"command_id": command_id, "status": "queued"}
    advisor = read_port.overview().get("advisor") or {}
    if advisor.get("status") != "waiting_for_human" or advisor.get("request_id") != request_id:
        raise ValueError("Advisor is not waiting for this report's feedback")
    feedback_from_operator(report=advisor["report"], response=exact)
    command = {
        "command_id": command_id,
        "kind": "advisor_feedback",
        "request_id": request_id,
        "response": exact,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        publish_json(path, command)
    except FileExistsError:
        existing = _json(path)
        if existing.get("request_id") != request_id or existing.get("response") != exact:
            raise ValueError("This Advisor report already has different submitted feedback")
    return {"command_id": command_id, "status": "queued"}


def pending_advisor_commands(project: str | Path) -> list[Path]:
    directory = Path(project) / "private" / "dashboard"
    if directory.is_symlink() or (directory / "commands").is_symlink():
        return []
    return [
        path for path in sorted((directory / "commands").glob("AFB-*.json"))
        if not (directory / "receipts" / path.name).exists()
    ]


def consume_advisor_commands(runtime: Any) -> bool:
    """Called only while the existing runner owns its project lock."""

    changed = False
    for path in pending_advisor_commands(runtime.layout.root):
        receipt: dict[str, Any] = {"command_id": path.stem}
        try:
            command = _json(path)
            if isinstance(command.get("request_id"), str):
                receipt["request_id"] = command["request_id"]
            if command.get("kind") != "advisor_feedback" or command.get("command_id") != path.stem:
                raise ValueError("invalid dashboard command")
            runtime.submit_advisor_feedback(command["request_id"], command["response"])
            receipt.update(status="accepted", request_id=command["request_id"])
            changed = True
        except Exception as exc:
            receipt.update(status="rejected", error=str(exc))
        receipt["processed_at"] = datetime.now(timezone.utc).isoformat()
        destination = path.parent.parent / "receipts" / path.name
        try:
            publish_json(destination, receipt)
        except FileExistsError:
            pass
    return changed


def _attempt_limit_command(path: Path) -> dict[str, Any]:
    command = _json(path)
    if command.get("kind") != "attempt_limits" or command.get("command_id") != path.stem:
        raise ValueError("invalid attempt limit command")
    for field in ("explorer_limit", "franta_limit"):
        if type(command.get(field)) is not int or command[field] < 1:
            raise ValueError("Attempt limits must be positive integers")
    submitted = datetime.fromisoformat(command["created_at"].replace("Z", "+00:00"))
    if submitted.tzinfo is None:
        raise ValueError("Attempt limit submission time must include a timezone")
    command["created_at"] = submitted.astimezone(timezone.utc).isoformat()
    command["effective_at"] = (submitted + timedelta(seconds=120)).astimezone(timezone.utc).isoformat()
    return command


def pending_attempt_limit_commands(project: str | Path) -> list[Path]:
    directory = Path(project) / "private" / "dashboard"
    if directory.is_symlink() or (directory / "commands").is_symlink():
        return []
    return [
        path for path in sorted((directory / "commands").glob("ATL-*.json"))
        if not (directory / "receipts" / path.name).exists()
    ]


def latest_pending_attempt_limits(project: str | Path) -> dict[str, Any] | None:
    """Read the latest valid inbox edit without constructing a state writer."""

    commands = []
    for path in pending_attempt_limit_commands(project):
        try:
            commands.append(_attempt_limit_command(path))
        except (OSError, ValueError, TypeError, KeyError, AttributeError, OverflowError):
            continue
    return max(commands, key=lambda item: (item["created_at"], item["command_id"]), default=None)


def submit_attempt_limit_command(project: str | Path, explorer_limit: int,
                                 franta_limit: int) -> dict[str, Any]:
    """Persist each edit immediately; the runner owns delayed adoption."""

    if any(type(value) is not int or value < 1 for value in (explorer_limit, franta_limit)):
        raise ValueError("Attempt limits must be positive integers")
    directory = dashboard_directory(project)
    lock_path = directory / "attempt-limits.lock"
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "r+") as guard:
        fcntl.flock(guard, fcntl.LOCK_EX)
        # Serialize browser tabs and keep a strict submission order, including
        # edits arriving in the same clock tick. Immutable files survive restarts.
        submitted = datetime.now(timezone.utc)
        for path in (directory / "commands").glob("ATL-*.json"):
            try:
                previous = _attempt_limit_command(path)
                submitted = max(submitted, datetime.fromisoformat(previous["created_at"]) + timedelta(microseconds=1))
            except (OSError, ValueError, TypeError, KeyError, AttributeError, OverflowError):
                continue
        command_id = "ATL-" + uuid.uuid4().hex
        command = {
            "command_id": command_id, "kind": "attempt_limits",
            "explorer_limit": explorer_limit, "franta_limit": franta_limit,
            "created_at": submitted.isoformat(),
            "effective_at": (submitted + timedelta(seconds=120)).isoformat(),
        }
        publish_json(directory / "commands" / f"{command_id}.json", command)
    return {**command, "status": "queued"}


def consume_attempt_limit_commands(runtime: Any) -> bool:
    """Queue only the newest edit while the runner owns the project lock."""

    pending: list[tuple[Path, dict[str, Any]]] = []

    def receipt(path: Path, **result: Any) -> None:
        destination = path.parent.parent / "receipts" / path.name
        try:
            publish_json(destination, {
                "command_id": path.stem, "processed_at": datetime.now(timezone.utc).isoformat(),
                **result,
            })
        except FileExistsError:
            pass

    for path in pending_attempt_limit_commands(runtime.layout.root):
        try:
            pending.append((path, _attempt_limit_command(path)))
        except (OSError, ValueError, TypeError, KeyError, AttributeError, OverflowError) as exc:
            receipt(path, status="rejected", error=str(exc))
    if not pending:
        return False
    newest_path, newest = max(pending, key=lambda item: (item[1]["created_at"], item[1]["command_id"]))
    changed = runtime.scheduler.queue_attempt_limits(
        explorer_limit=newest["explorer_limit"], franta_limit=newest["franta_limit"],
        command_id=newest["command_id"], submitted_at=newest["created_at"],
    )
    # Receipt publication follows the durable queue operation. A crash between
    # them replays the same command ID instead of restarting its grace period.
    receipt(newest_path, status="accepted", effective_at=newest["effective_at"])
    for path, _command in pending:
        if path != newest_path:
            receipt(path, status="superseded", superseded_by=newest["command_id"])
    return changed
