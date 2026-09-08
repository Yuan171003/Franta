"""Operator feedback inbox; the research runner remains its sole state writer."""

from __future__ import annotations

from datetime import datetime, timezone
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
