"""Scheduler-owned category and category-portfolio artifacts.

Categories are mutable organizational views, not an eighth memory type.  This
module stores their authoritative JSON state in ``MemoryStore``'s private
compare-and-swap control area.  Markdown files are deterministic, rebuildable
projections and never become canonical mathematical evidence.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import tempfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from ..contracts.canonical import (
    ConflictError,
    IdempotencyConflict,
    MemoryType,
    NotFoundError,
    ValidationError,
)
from ..read_access.snapshots import (
    SnapshotValidationError,
    validate_category_portfolio_snapshot,
    validate_event_id,
)
from ..store import MemoryStore
from .rendering import render_category, render_portfolio


CATEGORY_SCHEMA_VERSION = 1
CATEGORY_MEMBER_TYPES = (
    MemoryType.FACT,
    MemoryType.ROUTE,
    MemoryType.MEMO,
    MemoryType.CLAIM,
    MemoryType.OBLIGATION,
)
_MEMBER_KEYS = tuple(item.value for item in CATEGORY_MEMBER_TYPES)
_CATEGORY_SET_FIELDS = frozenset(
    {"name", "description", "main_progress", "current_obstacles"}
)


def _utc_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _mapping(value: Any, field_name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValidationError(f"{field_name} must be an object")
    return copy.deepcopy(dict(value))


def _text(value: Any, field_name: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise ValidationError(f"{field_name} must be a string")
    value = value.strip()
    if not allow_empty and not value:
        raise ValidationError(f"{field_name} must not be empty")
    return value


def _ids(value: Any, field_name: str, *, allow_empty: bool = True) -> list[str]:
    if value is None:
        result: list[str] = []
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        result = [_text(item, f"{field_name}[{index}]") for index, item in enumerate(value)]
    else:
        raise ValidationError(f"{field_name} must be a list of IDs")
    if len(set(result)) != len(result):
        raise ValidationError(f"{field_name} contains duplicate IDs")
    if not allow_empty and not result:
        raise ValidationError(f"{field_name} must not be empty")
    return result


def _empty_members() -> dict[str, list[str]]:
    return {key: [] for key in _MEMBER_KEYS}


@dataclass(frozen=True)
class CategoryOperationResult:
    operation_id: str
    status: str
    category_ids: tuple[str, ...] = ()
    portfolio_snapshot_id: str | None = None
    portfolio_revision: int | None = None
    proposal_mappings: Mapping[str, str] = field(default_factory=dict)
    error: str | None = None
    replayed: bool = False


class CategoryStore:
    """Atomic scheduler-owned category and portfolio repository."""

    def __init__(
        self,
        memory_store: MemoryStore,
        projection_dir: str | os.PathLike[str] | None | bool = None,
        *,
        control_key: str = "categories.v1",
    ) -> None:
        self.memory_store = memory_store
        self.control_key = _text(control_key, "control_key")
        if projection_dir is False:
            self.projection_dir: Path | None = None
        elif projection_dir is None:
            if memory_store.db_path == ":memory:":
                self.projection_dir = None
            else:
                database_path = Path(memory_store.db_path)
                self.projection_dir = database_path.parent / f"{database_path.stem}.categories"
        else:
            self.projection_dir = Path(projection_dir)
        self._ensure_state()

    @staticmethod
    def _initial_state() -> dict[str, Any]:
        return {
            "schema_version": CATEGORY_SCHEMA_VERSION,
            "categories": {},
            "category_history": {},
            "proposal_mappings": {},
            "portfolio_revision": 0,
            "active_portfolio": None,
            "portfolio_history": [],
            "operations": {},
            "events": [],
        }

    def _ensure_state(self) -> None:
        with self.memory_store.transaction():
            loaded = self.memory_store.load_control_state(self.control_key)
            if loaded is None:
                try:
                    self.memory_store.compare_and_swap_control_state(
                        self.control_key, None, self._initial_state()
                    )
                except ConflictError:
                    loaded = self.memory_store.load_control_state(self.control_key)
                    if loaded is None:
                        raise
            else:
                _, state = loaded
                if state.get("schema_version") != CATEGORY_SCHEMA_VERSION:
                    raise RuntimeError(
                        f"unsupported category schema version {state.get('schema_version')}"
                    )

    def _load(self) -> tuple[int, dict[str, Any]]:
        loaded = self.memory_store.load_control_state(self.control_key)
        if loaded is None:
            raise RuntimeError("category control state disappeared")
        revision, state = loaded
        if state.get("schema_version") != CATEGORY_SCHEMA_VERSION:
            raise RuntimeError("category schema version mismatch")
        return revision, state

    @property
    def state_revision(self) -> int:
        return self._load()[0]

    def operation_status(self, operation_id: str) -> CategoryOperationResult | None:
        _, state = self._load()
        raw = state["operations"].get(operation_id)
        return None if raw is None else self._result(raw, replayed=False)

    @staticmethod
    def _result(raw: Mapping[str, Any], *, replayed: bool) -> CategoryOperationResult:
        result = raw.get("result", {})
        return CategoryOperationResult(
            operation_id=raw["operation_id"],
            status=raw["status"],
            category_ids=tuple(result.get("category_ids", [])),
            portfolio_snapshot_id=result.get("portfolio_snapshot_id"),
            portfolio_revision=result.get("portfolio_revision"),
            proposal_mappings=dict(result.get("proposal_mappings", {})),
            error=raw.get("error"),
            replayed=replayed,
        )

    def _normalize_members(
        self, value: Any, field_name: str = "members"
    ) -> dict[str, list[str]]:
        raw = _mapping(value or {}, field_name)
        unknown = set(raw) - set(_MEMBER_KEYS)
        if unknown:
            raise ValidationError(
                f"{field_name} permits only fact/route/memo/claim/obligation; "
                f"unknown groups: {sorted(unknown)}"
            )
        members = _empty_members()
        for kind in CATEGORY_MEMBER_TYPES:
            ids = _ids(raw.get(kind.value, []), f"{field_name}.{kind.value}")
            for memory_id in ids:
                try:
                    record = self.memory_store.get(memory_id)
                except NotFoundError as exc:
                    raise ValidationError(
                        f"{field_name}.{kind.value} references missing memory {memory_id}"
                    ) from exc
                if record.memory_type is not kind:
                    raise ValidationError(
                        f"{memory_id} is {record.memory_type.value}, not {kind.value}"
                    )
            members[kind.value] = sorted(ids)
        return members

    def _normalize_category_definition(
        self, value: Any, field_name: str
    ) -> dict[str, Any]:
        raw = _mapping(value, field_name)
        allowed = {
            "proposal_id",
            "name",
            "description",
            "main_progress",
            "current_obstacles",
            "members",
        }
        unknown = set(raw) - allowed
        if unknown:
            raise ValidationError(f"{field_name} has unknown fields: {sorted(unknown)}")
        proposal_id = raw.get("proposal_id")
        if proposal_id is not None:
            proposal_id = _text(proposal_id, f"{field_name}.proposal_id")
        return {
            "proposal_id": proposal_id,
            "name": _text(raw.get("name"), f"{field_name}.name"),
            "description": _text(raw.get("description"), f"{field_name}.description"),
            "main_progress": _text(
                raw.get("main_progress", ""),
                f"{field_name}.main_progress",
                allow_empty=True,
            ),
            "current_obstacles": _text(
                raw.get("current_obstacles", ""),
                f"{field_name}.current_obstacles",
                allow_empty=True,
            ),
            "members": self._normalize_members(
                raw.get("members", {}), f"{field_name}.members"
            ),
        }

    @staticmethod
    def _new_category(category_id: str, definition: Mapping[str, Any]) -> dict[str, Any]:
        now = _utc_now()
        return {
            "id": category_id,
            "revision": 1,
            "status": "active",
            "name": definition["name"],
            "description": definition["description"],
            "main_progress": definition["main_progress"],
            "current_obstacles": definition["current_obstacles"],
            "members": copy.deepcopy(definition["members"]),
            "superseded_by": [],
            "created_at": now,
            "updated_at": now,
        }

    @staticmethod
    def _archive_revision(state: dict[str, Any], category: Mapping[str, Any]) -> None:
        state["category_history"].setdefault(category["id"], []).append(
            copy.deepcopy(dict(category))
        )

    @staticmethod
    def _require_category(
        state: Mapping[str, Any], category_id: str, *, active: bool = True
    ) -> dict[str, Any]:
        category = state["categories"].get(category_id)
        if category is None:
            raise ValidationError(f"unknown category: {category_id}")
        if active and category["status"] != "active":
            raise ValidationError(
                f"category {category_id} is historical ({category['status']})"
            )
        return category

    @staticmethod
    def _check_revision(category: Mapping[str, Any], expected: Any, field: str) -> None:
        if not isinstance(expected, int) or isinstance(expected, bool):
            raise ValidationError(f"{field} must be an integer")
        if expected != category["revision"]:
            raise ConflictError(
                f"category {category['id']} revision conflict: expected {expected}, "
                f"current {category['revision']}"
            )

    def _member_entries(self, members: Mapping[str, Sequence[str]]) -> dict[str, list[dict[str, Any]]]:
        entries: dict[str, list[dict[str, Any]]] = {
            key: [] for key in _MEMBER_KEYS
        }
        for key in _MEMBER_KEYS:
            typed_entries: list[dict[str, Any]] = []
            for memory_id in sorted(members.get(key, [])):
                try:
                    record = self.memory_store.get(memory_id)
                except NotFoundError:
                    typed_entries.append(
                        {"id": memory_id, "type": key, "status": "missing", "active": False}
                    )
                else:
                    typed_entries.append(
                        {
                            "id": memory_id,
                            "type": key,
                            "status": record.status,
                            "active": record.active,
                        }
                    )
            entries[key] = typed_entries
        return entries

    def _compose_category(self, category: Mapping[str, Any]) -> dict[str, Any]:
        result = copy.deepcopy(dict(category))
        result["member_entries"] = self._member_entries(result["members"])
        return result

    def get_category(self, category_id: str) -> dict[str, Any]:
        _, state = self._load()
        category = state["categories"].get(category_id)
        if category is None:
            raise NotFoundError(f"category not found: {category_id}")
        return self._compose_category(category)

    def list_categories(self, *, active_only: bool = True) -> list[dict[str, Any]]:
        _, state = self._load()
        categories = [
            category
            for category in state["categories"].values()
            if not active_only or category["status"] == "active"
        ]
        return [
            self._compose_category(category)
            for category in sorted(categories, key=lambda item: item["id"])
        ]

    def category_history(self, category_id: str) -> list[dict[str, Any]]:
        _, state = self._load()
        if category_id not in state["categories"]:
            raise NotFoundError(f"category not found: {category_id}")
        return copy.deepcopy(state["category_history"].get(category_id, []))

    def _compose_portfolio(self, snapshot: Mapping[str, Any]) -> dict[str, Any]:
        result = copy.deepcopy(dict(snapshot))
        _, state = self._load()
        for entry in result["categories"]:
            category = state["categories"].get(entry["category_id"])
            entry["status"] = "missing" if category is None else category["status"]
            entry["current_revision"] = None if category is None else category["revision"]
        result["flattened_member_entries"] = self._member_entries(
            result["flattened_members"]
        )
        try:
            validate_category_portfolio_snapshot(
                result,
                current_categories=state["categories"],
                category_history=state["category_history"],
                allow_missing_categories=True,
            )
        except SnapshotValidationError as exc:
            raise ValidationError(str(exc)) from exc
        return result

    def active_portfolio(self) -> dict[str, Any] | None:
        _, state = self._load()
        snapshot = state.get("active_portfolio")
        return None if snapshot is None else self._compose_portfolio(snapshot)

    def portfolio_history(self) -> list[dict[str, Any]]:
        _, state = self._load()
        return [self._compose_portfolio(item) for item in state["portfolio_history"]]

    def _write_atomic(self, target: Path, content: str) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(
            prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, target)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def rebuild_projections(self) -> list[Path]:
        if self.projection_dir is None:
            return []
        paths: list[Path] = []
        for category in self.list_categories(active_only=False):
            path = self.projection_dir / "categories" / f"{category['id']}.md"
            self._write_atomic(path, render_category(category))
            paths.append(path)
        for snapshot in self.portfolio_history():
            path = (
                self.projection_dir
                / "portfolios"
                / f"portfolio-{snapshot['revision']:06d}-{snapshot['snapshot_id']}.md"
            )
            self._write_atomic(path, render_portfolio(snapshot))
            paths.append(path)
        return paths

    @staticmethod
    def _resolve_category_ref(reference: Any, mappings: Mapping[str, str], field: str) -> str:
        value = _text(reference, field)
        return mappings.get(value, value)

    @staticmethod
    def _append_event(
        state: dict[str, Any], event_type: str, actor: str, details: Mapping[str, Any]
    ) -> None:
        state["events"].append(
            {
                "event_index": len(state["events"]) + 1,
                "event_type": event_type,
                "actor": actor,
                "details": copy.deepcopy(dict(details)),
                "created_at": _utc_now(),
            }
        )

    def _apply_create(
        self,
        state: dict[str, Any],
        action: Mapping[str, Any],
        mappings: dict[str, str],
        touched: set[str],
    ) -> None:
        raw = _mapping(action, "create action")
        unknown = set(raw) - {
            "kind",
            "proposal_id",
            "name",
            "description",
            "main_progress",
            "current_obstacles",
            "members",
        }
        if unknown:
            raise ValidationError(f"create action has unknown fields: {sorted(unknown)}")
        definition = self._normalize_category_definition(
            {key: value for key, value in raw.items() if key != "kind"}, "create"
        )
        proposal_id = definition["proposal_id"]
        if not proposal_id:
            raise ValidationError("create.proposal_id is required")
        if proposal_id in mappings or proposal_id in state["proposal_mappings"]:
            raise ValidationError(f"category proposal ID already resolved: {proposal_id}")
        category_id = self.memory_store.allocate_id("category")
        category = self._new_category(category_id, definition)
        state["categories"][category_id] = category
        state["category_history"].setdefault(category_id, [])
        state["proposal_mappings"][proposal_id] = category_id
        mappings[proposal_id] = category_id
        touched.add(category_id)

    def _apply_update(
        self,
        state: dict[str, Any],
        action: Mapping[str, Any],
        mappings: Mapping[str, str],
        touched: set[str],
    ) -> None:
        raw = _mapping(action, "category update")
        kind = raw.get("kind")
        if kind not in {"update", "revise", "rename", "membership"}:
            raise ValidationError(f"invalid category update kind: {kind}")
        common = {"kind", "category_id", "expected_revision"}
        if kind == "rename":
            allowed = common | {"name"}
            set_values = {"name": raw.get("name")}
            add_raw: Any = {}
            remove_raw: Any = {}
        elif kind == "membership":
            allowed = common | {"add_members", "remove_members"}
            set_values = {}
            add_raw = raw.get("add_members", {})
            remove_raw = raw.get("remove_members", {})
        else:
            allowed = common | {"set", "add_members", "remove_members"}
            set_values = _mapping(raw.get("set", {}), "update.set")
            add_raw = raw.get("add_members", {})
            remove_raw = raw.get("remove_members", {})
        unknown = set(raw) - allowed
        if unknown:
            raise ValidationError(f"{kind} action has unknown fields: {sorted(unknown)}")
        category_id = self._resolve_category_ref(
            raw.get("category_id"), mappings, f"{kind}.category_id"
        )
        category = self._require_category(state, category_id)
        self._check_revision(
            category, raw.get("expected_revision"), f"{kind}.expected_revision"
        )
        unknown_set = set(set_values) - _CATEGORY_SET_FIELDS
        if unknown_set:
            raise ValidationError(f"update.set has unknown fields: {sorted(unknown_set)}")
        normalized_set: dict[str, str] = {}
        for field_name, value in set_values.items():
            normalized_set[field_name] = _text(
                value,
                f"update.set.{field_name}",
                allow_empty=field_name in {"main_progress", "current_obstacles"},
            )
        additions = self._normalize_members(add_raw, "add_members")
        removals = self._normalize_members(remove_raw, "remove_members")
        for member_type in _MEMBER_KEYS:
            overlap = set(additions[member_type]) & set(removals[member_type])
            if overlap:
                raise ValidationError(
                    f"cannot add and remove the same {member_type} IDs: {sorted(overlap)}"
                )
        updated = copy.deepcopy(category)
        changed = False
        for field_name, value in normalized_set.items():
            if updated[field_name] != value:
                updated[field_name] = value
                changed = True
        for member_type in _MEMBER_KEYS:
            values = set(updated["members"][member_type])
            before = set(values)
            values.update(additions[member_type])
            values.difference_update(removals[member_type])
            if values != before:
                updated["members"][member_type] = sorted(values)
                changed = True
        if not changed:
            raise ValidationError(f"{kind} action makes no effective change")
        self._archive_revision(state, category)
        updated["revision"] += 1
        updated["updated_at"] = _utc_now()
        state["categories"][category_id] = updated
        touched.add(category_id)

    def _apply_merge(
        self,
        state: dict[str, Any],
        action: Mapping[str, Any],
        mappings: dict[str, str],
        touched: set[str],
    ) -> None:
        raw = _mapping(action, "merge action")
        allowed = {"kind", "source_category_ids", "expected_revisions", "result"}
        unknown = set(raw) - allowed
        if unknown:
            raise ValidationError(f"merge action has unknown fields: {sorted(unknown)}")
        source_refs = _ids(
            raw.get("source_category_ids"), "merge.source_category_ids", allow_empty=False
        )
        sources = [
            self._resolve_category_ref(item, mappings, "merge.source_category_ids")
            for item in source_refs
        ]
        if len(sources) < 2:
            raise ValidationError("merge requires at least two source categories")
        if len(set(sources)) != len(sources):
            raise ValidationError("merge source categories must be distinct")
        expected_value = raw.get("expected_revisions")
        if isinstance(expected_value, Mapping):
            expected_entries = list(expected_value.items())
        elif isinstance(expected_value, Sequence) and not isinstance(
            expected_value, (str, bytes, bytearray)
        ):
            expected_entries = []
            for index, value in enumerate(expected_value):
                entry = _mapping(value, f"merge.expected_revisions[{index}]")
                if set(entry) != {"category_id", "category_revision"}:
                    raise ValidationError(
                        "merge expected-revision entries require exactly "
                        "category_id and category_revision"
                    )
                expected_entries.append(
                    (entry["category_id"], entry["category_revision"])
                )
        else:
            raise ValidationError("merge.expected_revisions must be a mapping or list")
        expected: dict[str, Any] = {}
        for reference, revision in expected_entries:
            category_id = self._resolve_category_ref(
                reference, mappings, "merge.expected_revisions category_id"
            )
            if category_id in expected:
                raise ValidationError(
                    f"merge.expected_revisions repeats category {category_id}"
                )
            expected[category_id] = revision
        if set(expected) != set(sources):
            raise ValidationError("merge.expected_revisions must name exactly the sources")
        source_categories: list[dict[str, Any]] = []
        for category_id in sources:
            category = self._require_category(state, category_id)
            self._check_revision(
                category,
                expected[category_id],
                f"merge.expected_revisions.{category_id}",
            )
            source_categories.append(category)
        definition = self._normalize_category_definition(raw.get("result"), "merge.result")
        proposal_id = definition["proposal_id"]
        if not proposal_id:
            raise ValidationError("merge.result.proposal_id is required")
        if proposal_id in mappings or proposal_id in state["proposal_mappings"]:
            raise ValidationError(f"category proposal ID already resolved: {proposal_id}")
        result_id = self.memory_store.allocate_id("category")
        state["categories"][result_id] = self._new_category(result_id, definition)
        state["category_history"].setdefault(result_id, [])
        state["proposal_mappings"][proposal_id] = result_id
        mappings[proposal_id] = result_id
        touched.add(result_id)
        for category in source_categories:
            self._archive_revision(state, category)
            archived = copy.deepcopy(category)
            archived["revision"] += 1
            archived["status"] = "merged"
            archived["superseded_by"] = [result_id]
            archived["updated_at"] = _utc_now()
            state["categories"][category["id"]] = archived
            touched.add(category["id"])

    def _apply_split(
        self,
        state: dict[str, Any],
        action: Mapping[str, Any],
        mappings: dict[str, str],
        touched: set[str],
    ) -> None:
        raw = _mapping(action, "split action")
        allowed = {"kind", "source_category_id", "expected_revision", "results"}
        unknown = set(raw) - allowed
        if unknown:
            raise ValidationError(f"split action has unknown fields: {sorted(unknown)}")
        source_id = self._resolve_category_ref(
            raw.get("source_category_id"), mappings, "split.source_category_id"
        )
        source = self._require_category(state, source_id)
        self._check_revision(
            source, raw.get("expected_revision"), "split.expected_revision"
        )
        results_raw = raw.get("results")
        if not isinstance(results_raw, Sequence) or isinstance(
            results_raw, (str, bytes, bytearray)
        ):
            raise ValidationError("split.results must be a list")
        if len(results_raw) < 2:
            raise ValidationError("split requires at least two explicit result categories")
        definitions = [
            self._normalize_category_definition(item, f"split.results[{index}]")
            for index, item in enumerate(results_raw)
        ]
        proposal_ids = [definition["proposal_id"] for definition in definitions]
        if any(not item for item in proposal_ids):
            raise ValidationError("every split result requires proposal_id")
        if len(set(proposal_ids)) != len(proposal_ids):
            raise ValidationError("split result proposal IDs must be distinct")
        for proposal_id in proposal_ids:
            if proposal_id in mappings or proposal_id in state["proposal_mappings"]:
                raise ValidationError(f"category proposal ID already resolved: {proposal_id}")
        result_ids: list[str] = []
        for definition in definitions:
            category_id = self.memory_store.allocate_id("category")
            state["categories"][category_id] = self._new_category(category_id, definition)
            state["category_history"].setdefault(category_id, [])
            state["proposal_mappings"][definition["proposal_id"]] = category_id
            mappings[definition["proposal_id"]] = category_id
            result_ids.append(category_id)
            touched.add(category_id)
        self._archive_revision(state, source)
        archived = copy.deepcopy(source)
        archived["revision"] += 1
        archived["status"] = "split"
        archived["superseded_by"] = sorted(result_ids)
        archived["updated_at"] = _utc_now()
        state["categories"][source_id] = archived
        touched.add(source_id)

    def _apply_changes(
        self,
        state: dict[str, Any],
        changes: Any,
        mappings: dict[str, str],
        touched: set[str],
    ) -> None:
        if changes is None:
            return
        if not isinstance(changes, Sequence) or isinstance(changes, (str, bytes, bytearray)):
            raise ValidationError("category_changes must be a list")
        for index, action in enumerate(changes):
            action_mapping = _mapping(action, f"category_changes[{index}]")
            kind = action_mapping.get("kind")
            if kind == "create":
                self._apply_create(state, action_mapping, mappings, touched)
            elif kind in {"update", "revise", "rename", "membership"}:
                self._apply_update(state, action_mapping, mappings, touched)
            elif kind == "merge":
                self._apply_merge(state, action_mapping, mappings, touched)
            elif kind == "split":
                self._apply_split(state, action_mapping, mappings, touched)
            else:
                raise ValidationError(
                    f"category_changes[{index}].kind is unsupported: {kind}"
                )

    @staticmethod
    def _normalize_event_id(value: Any, field_name: str) -> str | int:
        try:
            return validate_event_id(value, field_name)
        except SnapshotValidationError as exc:
            raise ValidationError(str(exc)) from exc

    def _normalize_portfolio_categories(
        self,
        raw_value: Any,
        mappings: Mapping[str, str],
    ) -> list[dict[str, Any]]:
        entries: list[tuple[Any, Any]] = []
        if isinstance(raw_value, Mapping):
            entries = list(raw_value.items())
        elif isinstance(raw_value, Sequence) and not isinstance(
            raw_value, (str, bytes, bytearray)
        ):
            for index, raw in enumerate(raw_value):
                item = _mapping(raw, f"portfolio.categories[{index}]")
                if set(item) != {"category_id", "category_revision"}:
                    raise ValidationError(
                        "portfolio category entries require exactly category_id and category_revision"
                    )
                entries.append((item["category_id"], item["category_revision"]))
        else:
            raise ValidationError("portfolio.categories must be a mapping or list")
        if not entries:
            raise ValidationError("category portfolio must select at least one category")
        result: list[dict[str, Any]] = []
        seen: set[str] = set()
        for index, (reference, revision) in enumerate(entries):
            category_id = self._resolve_category_ref(
                reference, mappings, f"portfolio.categories[{index}].category_id"
            )
            if category_id in seen:
                raise ValidationError(f"portfolio repeats category {category_id}")
            seen.add(category_id)
            if not isinstance(revision, int) or isinstance(revision, bool):
                raise ValidationError("portfolio category revision must be an integer")
            result.append(
                {"category_id": category_id, "category_revision": revision}
            )
        return sorted(result, key=lambda item: item["category_id"])

    @staticmethod
    def _flatten_category_members(
        categories: Sequence[Mapping[str, Any]], state: Mapping[str, Any]
    ) -> dict[str, list[str]]:
        flattened: dict[str, set[str]] = {key: set() for key in _MEMBER_KEYS}
        for entry in categories:
            category = state["categories"][entry["category_id"]]
            for key in _MEMBER_KEYS:
                flattened[key].update(category["members"][key])
        return {key: sorted(flattened[key]) for key in _MEMBER_KEYS}

    def _apply_portfolio(
        self,
        state: dict[str, Any],
        raw_value: Any,
        mappings: Mapping[str, str],
    ) -> dict[str, Any]:
        raw = _mapping(raw_value, "portfolio")
        allowed = {
            "expected_portfolio_revision",
            "categories",
            "flattened_members",
            "base_event_id",
            "confirmed_through_event_id",
            "selection_rationale",
            "human_guidance_reference",
        }
        unknown = set(raw) - allowed
        if unknown:
            raise ValidationError(f"portfolio has unknown fields: {sorted(unknown)}")
        expected = raw.get("expected_portfolio_revision")
        if not isinstance(expected, int) or isinstance(expected, bool):
            raise ValidationError("portfolio.expected_portfolio_revision must be an integer")
        if expected != state["portfolio_revision"]:
            raise ConflictError(
                f"portfolio revision conflict: expected {expected}, "
                f"current {state['portfolio_revision']}"
            )
        categories = self._normalize_portfolio_categories(raw.get("categories"), mappings)
        for entry in categories:
            category = self._require_category(state, entry["category_id"])
            self._check_revision(
                category,
                entry["category_revision"],
                f"portfolio category {entry['category_id']} revision",
            )
        derived_flattened = self._flatten_category_members(categories, state)
        supplied_flattened = self._normalize_members(
            raw.get("flattened_members"), "portfolio.flattened_members"
        )
        if supplied_flattened != derived_flattened:
            raise ValidationError(
                "portfolio.flattened_members does not exactly equal the selected category union"
            )
        base_event_id = self._normalize_event_id(
            raw.get("base_event_id"), "portfolio.base_event_id"
        )
        confirmed_event_id = self._normalize_event_id(
            raw.get("confirmed_through_event_id"),
            "portfolio.confirmed_through_event_id",
        )
        if isinstance(base_event_id, int) and isinstance(confirmed_event_id, int):
            if confirmed_event_id < base_event_id:
                raise ValidationError(
                    "confirmed_through_event_id cannot precede base_event_id"
                )
        guidance = raw.get("human_guidance_reference")
        if guidance is not None:
            guidance = _text(guidance, "portfolio.human_guidance_reference")
        revision = state["portfolio_revision"] + 1
        snapshot = {
            "snapshot_id": f"CP-{uuid.uuid4().hex}",
            "revision": revision,
            "categories": categories,
            "flattened_members": derived_flattened,
            "base_event_id": base_event_id,
            "confirmed_through_event_id": confirmed_event_id,
            "selection_rationale": _text(
                raw.get("selection_rationale"), "portfolio.selection_rationale"
            ),
            "human_guidance_reference": guidance,
            "created_at": _utc_now(),
        }
        state["portfolio_revision"] = revision
        state["active_portfolio"] = copy.deepcopy(snapshot)
        state["portfolio_history"].append(copy.deepcopy(snapshot))
        return snapshot

    def apply_trim_proposal(
        self,
        operation_id: str,
        proposal: Mapping[str, Any],
        *,
        actor: str = "trimmer",
    ) -> CategoryOperationResult:
        """Apply explicit semantic category changes and an optional portfolio.

        The trimmer determines names, meanings, members, merges, splits, and
        selected directions.  This method performs only mechanical schema,
        ID, status, revision, closure, and atomicity checks.
        """

        operation_id = _text(operation_id, "operation_id")
        actor = _text(actor, "actor")
        proposal_copy = _mapping(proposal, "proposal")
        input_hash = _digest(proposal_copy)
        committed = False
        with self.memory_store.transaction():
            control_revision, state = self._load()
            existing = state["operations"].get(operation_id)
            if existing is not None:
                if existing["input_hash"] != input_hash:
                    raise IdempotencyConflict(
                        f"category operation {operation_id} was replayed differently"
                    )
                return self._result(existing, replayed=True)
            expected_state_revision = proposal_copy.get("expected_state_revision")
            if expected_state_revision is not None:
                if not isinstance(expected_state_revision, int) or isinstance(
                    expected_state_revision, bool
                ):
                    raise ValidationError("expected_state_revision must be an integer")
                if expected_state_revision != control_revision:
                    raise ConflictError(
                        f"category state revision conflict: expected {expected_state_revision}, "
                        f"current {control_revision}"
                    )
            allowed = {"expected_state_revision", "category_changes", "portfolio"}
            unknown = set(proposal_copy) - allowed
            working = copy.deepcopy(state)
            mappings: dict[str, str] = {}
            touched: set[str] = set()
            portfolio: dict[str, Any] | None = None
            error: str | None = None
            try:
                with self.memory_store.transaction():
                    if unknown:
                        raise ValidationError(
                            f"trim proposal has unknown fields: {sorted(unknown)}"
                        )
                    self._apply_changes(
                        working,
                        proposal_copy.get("category_changes", []),
                        mappings,
                        touched,
                    )
                    if proposal_copy.get("portfolio") is not None:
                        portfolio = self._apply_portfolio(
                            working, proposal_copy["portfolio"], mappings
                        )
                    if not touched and portfolio is None:
                        raise ValidationError("trim proposal contains no effective operation")
            except (ValidationError, ConflictError) as exc:
                # The nested savepoint rolls back CAT allocations.  Persist a
                # terminal rejection against the unmodified category state.
                working = copy.deepcopy(state)
                error = str(exc)
                operation_record = {
                    "operation_id": operation_id,
                    "input_hash": input_hash,
                    "status": "rejected",
                    "error": error,
                    "result": {},
                    "created_at": _utc_now(),
                }
                working["operations"][operation_id] = operation_record
                self._append_event(
                    working,
                    "category_proposal_rejected",
                    actor,
                    {"operation_id": operation_id, "error": error},
                )
            else:
                result_payload = {
                    "category_ids": sorted(touched),
                    "portfolio_snapshot_id": None
                    if portfolio is None
                    else portfolio["snapshot_id"],
                    "portfolio_revision": None
                    if portfolio is None
                    else portfolio["revision"],
                    "proposal_mappings": dict(sorted(mappings.items())),
                }
                operation_record = {
                    "operation_id": operation_id,
                    "input_hash": input_hash,
                    "status": "committed",
                    "error": None,
                    "result": result_payload,
                    "created_at": _utc_now(),
                }
                working["operations"][operation_id] = operation_record
                self._append_event(
                    working,
                    "category_proposal_committed",
                    actor,
                    {
                        "operation_id": operation_id,
                        "category_ids": sorted(touched),
                        "portfolio_snapshot_id": result_payload[
                            "portfolio_snapshot_id"
                        ],
                    },
                )
                committed = True
            self.memory_store.compare_and_swap_control_state(
                self.control_key, control_revision, working
            )
            result = self._result(operation_record, replayed=False)
        if committed:
            self.rebuild_projections()
        return result

    def create_category(
        self,
        operation_id: str,
        definition: Mapping[str, Any],
        *,
        actor: str = "trimmer",
    ) -> CategoryOperationResult:
        action = {"kind": "create", **copy.deepcopy(dict(definition))}
        return self.apply_trim_proposal(
            operation_id, {"category_changes": [action]}, actor=actor
        )

    def revise_category(
        self,
        operation_id: str,
        category_id: str,
        expected_revision: int,
        *,
        set_fields: Mapping[str, Any] | None = None,
        add_members: Mapping[str, Sequence[str]] | None = None,
        remove_members: Mapping[str, Sequence[str]] | None = None,
        actor: str = "trimmer",
    ) -> CategoryOperationResult:
        action = {
            "kind": "revise",
            "category_id": category_id,
            "expected_revision": expected_revision,
            "set": copy.deepcopy(dict(set_fields or {})),
            "add_members": copy.deepcopy(dict(add_members or {})),
            "remove_members": copy.deepcopy(dict(remove_members or {})),
        }
        return self.apply_trim_proposal(
            operation_id, {"category_changes": [action]}, actor=actor
        )

    def rename_category(
        self,
        operation_id: str,
        category_id: str,
        expected_revision: int,
        name: str,
        *,
        actor: str = "trimmer",
    ) -> CategoryOperationResult:
        return self.apply_trim_proposal(
            operation_id,
            {
                "category_changes": [
                    {
                        "kind": "rename",
                        "category_id": category_id,
                        "expected_revision": expected_revision,
                        "name": name,
                    }
                ]
            },
            actor=actor,
        )

    def change_membership(
        self,
        operation_id: str,
        category_id: str,
        expected_revision: int,
        *,
        add_members: Mapping[str, Sequence[str]] | None = None,
        remove_members: Mapping[str, Sequence[str]] | None = None,
        actor: str = "trimmer",
    ) -> CategoryOperationResult:
        return self.apply_trim_proposal(
            operation_id,
            {
                "category_changes": [
                    {
                        "kind": "membership",
                        "category_id": category_id,
                        "expected_revision": expected_revision,
                        "add_members": copy.deepcopy(dict(add_members or {})),
                        "remove_members": copy.deepcopy(dict(remove_members or {})),
                    }
                ]
            },
            actor=actor,
        )

    def merge_categories(
        self,
        operation_id: str,
        source_category_ids: Sequence[str],
        expected_revisions: Mapping[str, int],
        result_definition: Mapping[str, Any],
        *,
        actor: str = "trimmer",
    ) -> CategoryOperationResult:
        return self.apply_trim_proposal(
            operation_id,
            {
                "category_changes": [
                    {
                        "kind": "merge",
                        "source_category_ids": list(source_category_ids),
                        "expected_revisions": dict(expected_revisions),
                        "result": copy.deepcopy(dict(result_definition)),
                    }
                ]
            },
            actor=actor,
        )

    def split_category(
        self,
        operation_id: str,
        source_category_id: str,
        expected_revision: int,
        result_definitions: Sequence[Mapping[str, Any]],
        *,
        actor: str = "trimmer",
    ) -> CategoryOperationResult:
        return self.apply_trim_proposal(
            operation_id,
            {
                "category_changes": [
                    {
                        "kind": "split",
                        "source_category_id": source_category_id,
                        "expected_revision": expected_revision,
                        "results": [copy.deepcopy(dict(item)) for item in result_definitions],
                    }
                ]
            },
            actor=actor,
        )

    def commit_portfolio(
        self,
        operation_id: str,
        portfolio: Mapping[str, Any],
        *,
        actor: str = "trimmer",
    ) -> CategoryOperationResult:
        return self.apply_trim_proposal(
            operation_id, {"portfolio": copy.deepcopy(dict(portfolio))}, actor=actor
        )
