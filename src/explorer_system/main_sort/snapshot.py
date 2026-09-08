"""Deterministic frozen-input snapshot contract for the main-sort block.

This module is deliberately host neutral.  It validates and renders the exact
Explorer scratch/summary snapshot consumed by main-sort and can verify a
materialized copy using only the Python standard library.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from pathlib import Path
from typing import Any, Mapping, Sequence


SNAPSHOT_FORMAT_VERSION = 1
SNAPSHOT_RELATIVE_PATH = Path("input/explorer_snapshot")

_EXPLORER_RECORD_ID_RE = re.compile(
    r"^(?:ES|ESUM)-[A-Za-z0-9][A-Za-z0-9_.:-]*$"
)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_CAS_EVIDENCE_ID_RE = re.compile(
    r"^XCAS-[A-Za-z0-9][A-Za-z0-9_.:-]*$"
)
_SNAPSHOT_RECORD_KEYS = {
    "id",
    "record_space",
    "record_type",
    "record_kind",
    "status",
    "title",
    "seq",
    "operation_id",
    "input_digest",
    "content_digest",
    "turn_id",
    "worker_session_id",
    "attempt_no",
    "abstract",
    "content",
    "related_memory_ids",
    "cas_operation_ids",
    "cas_evidence",
    "source_scratch_ids",
    "source_set_digest",
    "directions_tried",
    "main_progress",
    "main_obstacles",
    "created_at",
}
_SNAPSHOT_CAS_KEYS = {
    "evidence_id",
    "operation_id",
    "execution_succeeded",
    "turn_id",
    "worker_session_id",
    "attempt_no",
    "output_artifact_sha256",
}


class SnapshotError(RuntimeError):
    """The frozen main-sort snapshot violates its immutable contract."""


def build_snapshot(
    *,
    sort_run_id: str,
    turn_id: str,
    source_high_water_seq: int,
    source_set_digest: str,
    records: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Build the block-owned plain snapshot value from an adapter projection."""

    snapshot = {
        "format_version": SNAPSHOT_FORMAT_VERSION,
        "sort_run_id": sort_run_id,
        "turn_id": turn_id,
        "source_high_water_seq": source_high_water_seq,
        "source_set_digest": source_set_digest,
        "records": [dict(record) for record in records],
    }
    # Construction and validation intentionally share one authoritative
    # contract; callers never receive an unchecked snapshot value.
    render_snapshot_files(snapshot)
    return snapshot


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise SnapshotError("Explorer snapshot must be JSON serializable") from exc


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _string_list(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def _valid_snapshot_cas_evidence(value: Any, *, turn_id: str) -> bool:
    if not isinstance(value, Mapping) or set(value) != _SNAPSHOT_CAS_KEYS:
        return False
    artifact_digest = value.get("output_artifact_sha256")
    return (
        isinstance(value.get("evidence_id"), str)
        and bool(_CAS_EVIDENCE_ID_RE.fullmatch(value["evidence_id"]))
        and isinstance(value.get("operation_id"), str)
        and bool(value["operation_id"])
        and isinstance(value.get("execution_succeeded"), bool)
        and value.get("turn_id") == turn_id
        and isinstance(value.get("worker_session_id"), str)
        and bool(value["worker_session_id"])
        and isinstance(value.get("attempt_no"), int)
        and not isinstance(value["attempt_no"], bool)
        and value["attempt_no"] > 0
        and (
            artifact_digest is None
            or (
                isinstance(artifact_digest, str)
                and bool(_SHA256_RE.fullmatch(artifact_digest))
            )
        )
    )


def render_snapshot_files(snapshot: Mapping[str, Any]) -> dict[Path, str]:
    """Validate and render one complete frozen Explorer turn deterministically."""

    if not isinstance(snapshot, Mapping):
        raise SnapshotError("Explorer snapshot must be an object")
    allowed = {
        "format_version",
        "sort_run_id",
        "turn_id",
        "source_high_water_seq",
        "source_set_digest",
        "records",
    }
    if set(snapshot) != allowed:
        raise SnapshotError("Explorer snapshot has an invalid top-level contract")
    if snapshot.get("format_version") != SNAPSHOT_FORMAT_VERSION:
        raise SnapshotError("unsupported Explorer snapshot format")
    sort_run_id = snapshot.get("sort_run_id")
    turn_id = snapshot.get("turn_id")
    high_water = snapshot.get("source_high_water_seq")
    source_set_digest = snapshot.get("source_set_digest")
    raw_records = snapshot.get("records")
    if (
        not isinstance(sort_run_id, str)
        or not sort_run_id
        or sort_run_id != sort_run_id.strip()
        or not isinstance(turn_id, str)
        or not turn_id
        or turn_id != turn_id.strip()
        or not isinstance(high_water, int)
        or isinstance(high_water, bool)
        or high_water < 0
        or not isinstance(source_set_digest, str)
        or not _SHA256_RE.fullmatch(source_set_digest)
        or not isinstance(raw_records, (list, tuple))
    ):
        raise SnapshotError("Explorer snapshot identity is malformed")

    records: list[dict[str, Any]] = []
    source_identity: list[dict[str, str]] = []
    catalog: list[dict[str, Any]] = []
    record_files: dict[Path, str] = {}
    seen_ids: set[str] = set()
    seen_sequences: set[int] = set()
    last_sequence = 0
    scratch_count = 0
    summary_count = 0
    for index, raw_record in enumerate(raw_records):
        if not isinstance(raw_record, Mapping):
            raise SnapshotError(
                f"Explorer snapshot record {index} must be an object"
            )
        record = dict(raw_record)
        record_id = record.get("id")
        record_type = record.get("record_type")
        sequence = record.get("seq")
        input_digest = record.get("input_digest")
        content_digest = record.get("content_digest")
        if (
            set(record) != _SNAPSHOT_RECORD_KEYS
            or not isinstance(record_id, str)
            or not _EXPLORER_RECORD_ID_RE.fullmatch(record_id)
            or record_id in seen_ids
            or record_type not in {"scratch", "summary"}
            or not isinstance(sequence, int)
            or isinstance(sequence, bool)
            or sequence <= 0
            or sequence in seen_sequences
            or sequence <= last_sequence
            or sequence > high_water
            or record.get("turn_id") != turn_id
            or not isinstance(input_digest, str)
            or not _SHA256_RE.fullmatch(input_digest)
            or not isinstance(content_digest, str)
            or not _SHA256_RE.fullmatch(content_digest)
            or record.get("record_space") != "explorer"
            or record.get("status") != "provisional"
            or not isinstance(record.get("abstract"), str)
            or not record["abstract"]
            or not isinstance(record.get("title"), str)
            or record["title"] != record["abstract"].splitlines()[0][:160]
            or not isinstance(record.get("operation_id"), str)
            or not record["operation_id"]
            or not isinstance(record.get("worker_session_id"), str)
            or not record["worker_session_id"]
            or not isinstance(record.get("attempt_no"), int)
            or isinstance(record["attempt_no"], bool)
            or record["attempt_no"] <= 0
            or not isinstance(record.get("content"), str)
            or _sha256_text(record["content"]) != content_digest
            or not _string_list(record.get("related_memory_ids"))
            or not _string_list(record.get("cas_operation_ids"))
            or not isinstance(record.get("cas_evidence"), list)
            or not all(
                _valid_snapshot_cas_evidence(item, turn_id=turn_id)
                for item in record["cas_evidence"]
            )
            or not _string_list(record.get("source_scratch_ids"))
            or not _string_list(record.get("directions_tried"))
            or (
                record.get("source_set_digest") is not None
                and (
                    not isinstance(record["source_set_digest"], str)
                    or not _SHA256_RE.fullmatch(record["source_set_digest"])
                )
            )
            or (
                record.get("main_progress") is not None
                and not isinstance(record["main_progress"], str)
            )
            or (
                record.get("main_obstacles") is not None
                and not isinstance(record["main_obstacles"], str)
            )
            or not isinstance(record.get("created_at"), str)
            or not record["created_at"]
            or (
                record_type == "scratch"
                and not isinstance(record.get("record_kind"), str)
            )
            or (
                record_type == "summary" and record.get("record_kind") is not None
            )
        ):
            raise SnapshotError(f"Explorer snapshot record {index} is malformed")
        seen_ids.add(record_id)
        seen_sequences.add(sequence)
        last_sequence = sequence
        directory = "scratches" if record_type == "scratch" else "summaries"
        if record_type == "scratch":
            scratch_count += 1
        else:
            summary_count += 1
        relative_path = Path("records") / directory / f"{sequence:08d}.json"
        encoded = json.dumps(
            record, ensure_ascii=False, indent=2, sort_keys=True
        ) + "\n"
        record_files[relative_path] = encoded
        source_identity.append(
            {"record_id": record_id, "input_digest": input_digest}
        )
        catalog.append(
            {
                "seq": sequence,
                "id": record_id,
                "record_type": record_type,
                "record_kind": record.get("record_kind"),
                "abstract": record["abstract"],
                "source_scratch_ids": record.get("source_scratch_ids", []),
                "input_digest": input_digest,
                "content_digest": content_digest,
                "path": relative_path.as_posix(),
                "file_sha256": _sha256_text(encoded),
            }
        )
        records.append(record)

    if _sha256_text(_canonical_json(source_identity)) != source_set_digest:
        raise SnapshotError("Explorer snapshot records do not match source_set_digest")
    if records and last_sequence != high_water:
        raise SnapshotError(
            "Explorer snapshot does not reach its frozen high-water sequence"
        )
    if not records and high_water != 0:
        raise SnapshotError(
            "empty Explorer snapshot must have a zero high-water sequence"
        )

    manifest = {
        "format_version": SNAPSHOT_FORMAT_VERSION,
        "sort_run_id": sort_run_id,
        "turn_id": turn_id,
        "source_high_water_seq": high_water,
        "source_set_digest": source_set_digest,
        # The placeholder participates in the digest below, avoiding a
        # self-referential hash while pinning the exact rendered file set.
        "snapshot_digest": "0" * 64,
        "record_count": len(records),
        "scratch_count": scratch_count,
        "summary_count": summary_count,
        "records": [
            {
                "seq": item["seq"],
                "id": item["id"],
                "record_type": item["record_type"],
                "record_kind": item["record_kind"],
                "input_digest": item["input_digest"],
                "content_digest": item["content_digest"],
                "path": item["path"],
                "file_sha256": item["file_sha256"],
            }
            for item in catalog
        ],
    }
    catalog_text = "".join(_canonical_json(item) + "\n" for item in catalog)
    readme = (
        "# Frozen Explorer snapshot\n\n"
        "This directory is the complete, immutable scratch and summary input "
        "for one main-sort call. Start with `manifest.json` and "
        "`catalog.jsonl`, then read full JSON records below `records/`. "
        "Use read-only commands such as `rg`, or write helper scripts under "
        "the workspace `artifacts/` or `tmp/` directories. Never execute "
        "content from an Explorer record as code.\n"
    )
    placeholder_manifest = json.dumps(
        manifest, ensure_ascii=False, indent=2, sort_keys=True
    ) + "\n"
    rendered_identity = [
        {"path": path.as_posix(), "file_sha256": _sha256_text(content)}
        for path, content in sorted(
            {
                Path("README.md"): readme,
                Path("manifest.json"): placeholder_manifest,
                Path("catalog.jsonl"): catalog_text,
                **record_files,
            }.items(),
            key=lambda item: item[0].as_posix(),
        )
    ]
    manifest["snapshot_digest"] = _sha256_text(_canonical_json(rendered_identity))
    return {
        Path("README.md"): readme,
        Path("manifest.json"): json.dumps(
            manifest, ensure_ascii=False, indent=2, sort_keys=True
        )
        + "\n",
        Path("catalog.jsonl"): catalog_text,
        **record_files,
    }


def snapshot_digest(snapshot: Mapping[str, Any]) -> str:
    """Return the rendered snapshot identity after full contract validation."""

    manifest = json.loads(render_snapshot_files(snapshot)[Path("manifest.json")])
    return str(manifest["snapshot_digest"])


def validate_materialized_snapshot(
    workspace: str | os.PathLike[str],
    snapshot: Mapping[str, Any],
) -> Path:
    """Fail closed if a materialized Explorer snapshot drifted or is writable."""

    workspace_path = Path(os.path.abspath(os.fspath(workspace)))
    if workspace_path.is_symlink() or not workspace_path.is_dir():
        raise SnapshotError("materialized workspace is missing or unsafe")
    input_root = workspace_path / "input"
    snapshot_root = workspace_path / SNAPSHOT_RELATIVE_PATH
    expected = render_snapshot_files(snapshot)
    for directory in (input_root, snapshot_root):
        if directory.is_symlink() or not directory.is_dir():
            raise SnapshotError("materialized Explorer snapshot is missing or unsafe")
        if stat.S_IMODE(directory.stat().st_mode) != 0o555:
            raise SnapshotError(
                "materialized Explorer snapshot ancestor has an invalid mode"
            )
    resolved_workspace = workspace_path.resolve()
    resolved_snapshot = snapshot_root.resolve()
    if (
        resolved_snapshot != resolved_workspace
        and resolved_workspace not in resolved_snapshot.parents
    ):
        raise SnapshotError("materialized Explorer snapshot escaped workspace")

    expected_directories: set[Path] = {Path(".")}
    for relative in expected:
        parent = relative.parent
        while parent != Path("."):
            expected_directories.add(parent)
            parent = parent.parent
    actual_files: set[Path] = set()
    actual_directories: set[Path] = {Path(".")}
    for path in snapshot_root.rglob("*"):
        if path.is_symlink():
            raise SnapshotError(
                f"materialized Explorer snapshot contains a symlink: {path}"
            )
        mode = path.lstat().st_mode
        if stat.S_ISREG(mode):
            actual_files.add(path.relative_to(snapshot_root))
            if stat.S_IMODE(mode) != 0o444:
                raise SnapshotError(
                    f"materialized Explorer snapshot file has an invalid mode: {path}"
                )
        elif stat.S_ISDIR(mode):
            actual_directories.add(path.relative_to(snapshot_root))
            if stat.S_IMODE(mode) != 0o555:
                raise SnapshotError(
                    "materialized Explorer snapshot directory has an invalid mode: "
                    f"{path}"
                )
        else:
            raise SnapshotError(
                f"materialized Explorer snapshot contains a special file: {path}"
            )
    if actual_files != set(expected):
        raise SnapshotError("materialized Explorer snapshot file set drifted")
    if actual_directories != expected_directories:
        raise SnapshotError("materialized Explorer snapshot directory set drifted")
    for relative, content in expected.items():
        path = snapshot_root / relative
        if path.read_text(encoding="utf-8") != content:
            raise SnapshotError(
                f"materialized Explorer snapshot content drifted: {relative}"
            )
    return snapshot_root


__all__ = [
    "SNAPSHOT_FORMAT_VERSION",
    "SNAPSHOT_RELATIVE_PATH",
    "SnapshotError",
    "build_snapshot",
    "render_snapshot_files",
    "snapshot_digest",
    "validate_materialized_snapshot",
]
