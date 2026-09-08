"""Create copy-only agent workspaces with progressive-disclosure layers."""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from ..contracts.agent_access import AccessPolicy
from .main_memory_snapshot import (
    MAIN_MEMORY_SNAPSHOT_RELATIVE_PATH,
    MainMemorySnapshot,
    MainMemorySnapshotError,
    validate_main_memory_snapshot,
)
from .snapshots import (
    MemorySnapshot,
    SnapshotValidationError,
    unique_memory_snapshots,
)


_COMPONENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
# Compatibility names retained for callers of the Franta materializer.  Runtime
# behavior obtains the authoritative values through franta.explorer_adapter.
EXPLORER_SNAPSHOT_FORMAT_VERSION = 1
EXPLORER_SNAPSHOT_RELATIVE_PATH = Path("input/explorer_snapshot")


class MaterializationError(RuntimeError):
    pass


@dataclass(frozen=True)
class MaterializedWorkspace:
    path: Path
    input_path: Path
    outbox_path: Path
    artifacts_path: Path
    tmp_path: Path
    task_card_path: Path | None
    root_problem_path: Path
    portfolio_index_path: Path | None
    access_manifest_path: Path

    def subprocess_environment(self) -> dict[str, str]:
        """Non-secret environment values safe for the agent process."""

        return {
            "FRANTA_WORKSPACE": str(self.path),
            "FRANTA_OUTBOX": str(self.outbox_path),
            "FRANTA_ARTIFACTS": str(self.artifacts_path),
            "TMPDIR": str(self.tmp_path),
        }


def _component(value: str, label: str) -> str:
    if not _COMPONENT_RE.fullmatch(value) or value in {".", ".."}:
        raise MaterializationError(f"unsafe {label}: {value!r}")
    return value


def _assert_no_symlink(path: Path, stop: Path) -> None:
    current = path
    stop = stop.resolve()
    while True:
        if current.exists() and current.is_symlink():
            raise MaterializationError(f"symlink is forbidden in materialized path: {current}")
        if current == stop:
            break
        if stop not in current.parents:
            raise MaterializationError("materialized path escaped its workspace")
        current = current.parent


def _target(workspace: Path, relative: str | Path) -> Path:
    relative_path = Path(relative)
    if relative_path.is_absolute() or ".." in relative_path.parts:
        raise MaterializationError(f"unsafe materialized relative path: {relative!r}")
    target = workspace.joinpath(relative_path)
    _assert_no_symlink(target.parent, workspace)
    resolved_parent = target.parent.resolve()
    if resolved_parent != workspace.resolve() and workspace.resolve() not in resolved_parent.parents:
        raise MaterializationError("materialized path escaped its workspace")
    if target.exists() and target.is_symlink():
        raise MaterializationError(f"refusing to overwrite symlink: {target}")
    return target


def _atomic_text(workspace: Path, relative: str | Path, text: str, mode: int = 0o444) -> Path:
    target = _target(workspace, relative)
    target.parent.mkdir(parents=True, exist_ok=True)
    _assert_no_symlink(target.parent, workspace)
    temporary = target.with_name(f".{target.name}.new")
    if temporary.exists() or temporary.is_symlink():
        raise MaterializationError(f"temporary target already exists: {temporary}")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(temporary, flags, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, target)
    except Exception:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise
    return target


def _atomic_json(
    workspace: Path,
    relative: str | Path,
    value: Any,
    mode: int = 0o444,
) -> Path:
    return _atomic_text(
        workspace,
        relative,
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        mode,
    )


def _explorer_snapshot_files(
    snapshot: Mapping[str, Any],
) -> dict[Path, str]:
    from ..explorer_adapter import (
        MainSortSnapshotError,
        render_main_sort_snapshot_files,
    )

    try:
        return render_main_sort_snapshot_files(snapshot)
    except MainSortSnapshotError as exc:
        raise MaterializationError(str(exc)) from exc


def _explorer_snapshot_relative_path() -> Path:
    from ..explorer_adapter import main_sort_snapshot_contract

    _version, relative_path = main_sort_snapshot_contract()
    return relative_path


def explorer_snapshot_digest(snapshot: Mapping[str, Any]) -> str:
    """Compatibility facade for the isolated main-sort snapshot contract."""

    from ..explorer_adapter import MainSortSnapshotError, main_sort_snapshot_digest

    try:
        return main_sort_snapshot_digest(snapshot)
    except MainSortSnapshotError as exc:
        raise MaterializationError(str(exc)) from exc


def validate_explorer_snapshot(
    workspace: str | os.PathLike[str],
    snapshot: Mapping[str, Any],
) -> Path:
    """Compatibility facade for materialized-snapshot validation."""

    from ..explorer_adapter import (
        MainSortSnapshotError,
        validate_main_sort_materialized_snapshot,
    )

    try:
        return validate_main_sort_materialized_snapshot(workspace, snapshot)
    except MainSortSnapshotError as exc:
        raise MaterializationError(str(exc)) from exc


class WorkspaceMaterializer:
    """Materialize exact scheduler-supplied values, never links into canonical state."""

    def __init__(
        self,
        root: str | os.PathLike[str],
        *,
        skill_source: str | os.PathLike[str] | None = None,
        additional_skill_sources: Sequence[str | os.PathLike[str]] = (),
    ) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        if self.root.is_symlink():
            raise MaterializationError("workspace root may not be a symlink")
        if skill_source is None:
            package_skills = Path(__file__).resolve().parents[1] / "skills"
            skill_source = (
                package_skills if package_skills.is_dir()
                else Path(__file__).resolve().parents[3] / ".agents" / "skills"
            )
        self.skill_source = Path(skill_source).resolve()
        self.skill_sources = (
            self.skill_source,
            *(Path(item).resolve() for item in additional_skill_sources),
        )

    def create(
        self,
        call_id: str,
        *,
        root_problem: str,
        policy: AccessPolicy,
        task_card: Mapping[str, Any] | None = None,
        portfolio: Sequence[MemorySnapshot] = (),
        context: Mapping[str, Any] | None = None,
        frozen_input: Mapping[str, Any] | None = None,
        explorer_snapshot: Mapping[str, Any] | None = None,
        main_memory_snapshot: MainMemorySnapshot | None = None,
        skills: Iterable[str] = (),
    ) -> MaterializedWorkspace:
        call_id = _component(call_id, "call ID")
        if not isinstance(root_problem, str) or not root_problem.strip():
            raise MaterializationError("every agent workspace requires a root problem")
        workspace = self.root / call_id
        if workspace.exists() or workspace.is_symlink():
            raise MaterializationError(f"workspace already exists: {workspace}")
        workspace.mkdir(mode=0o700)
        for relative in ("input", "outbox", "artifacts", "tmp"):
            _target(workspace, relative).mkdir(mode=0o700)

        root_path = _atomic_text(workspace, "input/root_problem.md", root_problem.rstrip() + "\n")
        access_path = _atomic_json(workspace, "input/access_policy.json", policy.as_public_dict())

        task_path: Path | None = None
        if task_card is not None:
            card = dict(task_card)
            supplied_root = card.get("root_problem")
            if supplied_root is not None and supplied_root != root_problem:
                raise MaterializationError("task card root problem does not match the project root")
            card["root_problem"] = root_problem
            card["access_policy"] = policy.as_public_dict()
            task_path = _atomic_json(workspace, "input/task_card.json", card)
        if context is not None:
            _atomic_json(workspace, "input/context.json", dict(context))
        if frozen_input is not None:
            if portfolio:
                raise MaterializationError("a blind frozen-input call cannot also receive a portfolio")
            _atomic_json(workspace, "input/frozen_sprint.json", dict(frozen_input))

        if explorer_snapshot is not None:
            snapshot_root = _explorer_snapshot_relative_path()
            for relative, content in _explorer_snapshot_files(
                explorer_snapshot
            ).items():
                _atomic_text(
                    workspace,
                    snapshot_root / relative,
                    content,
                )

        if main_memory_snapshot is not None:
            try:
                source = validate_main_memory_snapshot(
                    main_memory_snapshot.root,
                    expected_snapshot_id=main_memory_snapshot.snapshot_id,
                    expected_snapshot_digest=main_memory_snapshot.snapshot_digest,
                )
            except MainMemorySnapshotError as exc:
                raise MaterializationError(str(exc)) from exc
            destination = _target(workspace, MAIN_MEMORY_SNAPSHOT_RELATIVE_PATH)
            if destination.exists() or destination.is_symlink():
                raise MaterializationError(
                    "Main memory snapshot destination already exists"
                )
            shutil.copytree(
                source.root,
                destination,
                symlinks=False,
                copy_function=shutil.copy2,
            )

        portfolio_path: Path | None = None
        if portfolio:
            try:
                exact_portfolio = unique_memory_snapshots(portfolio)
            except SnapshotValidationError as exc:
                raise MaterializationError(str(exc)) from exc
            summaries: list[dict[str, Any]] = []
            for snapshot in exact_portfolio:
                summaries.append(snapshot.summary())
                header = {
                    "id": snapshot.memory_id,
                    "memory_type": snapshot.memory_type,
                    "revision": snapshot.revision,
                    "status": snapshot.status,
                    "metadata": dict(snapshot.metadata),
                }
                body = (
                    "---\n"
                    + json.dumps(header, ensure_ascii=False, sort_keys=True)
                    + "\n---\n\n"
                    + snapshot.content.rstrip()
                    + "\n"
                )
                _atomic_text(
                    workspace,
                    Path("input/portfolio/records") / f"{snapshot.memory_id}.md",
                    body,
                )
            # Abstracts form the cheap first layer; records are the explicit full layer.
            portfolio_path = _atomic_json(
                workspace,
                "input/portfolio/index.json",
                {"records": summaries},
            )

        self._copy_skills(workspace, tuple(dict.fromkeys(skills)))
        self._freeze_inputs(workspace)
        if explorer_snapshot is not None:
            validate_explorer_snapshot(workspace, explorer_snapshot)
        if main_memory_snapshot is not None:
            try:
                validate_main_memory_snapshot(
                    workspace / MAIN_MEMORY_SNAPSHOT_RELATIVE_PATH,
                    expected_snapshot_id=main_memory_snapshot.snapshot_id,
                    expected_snapshot_digest=main_memory_snapshot.snapshot_digest,
                )
            except MainMemorySnapshotError as exc:
                raise MaterializationError(str(exc)) from exc
        return MaterializedWorkspace(
            path=workspace,
            input_path=workspace / "input",
            outbox_path=workspace / "outbox",
            artifacts_path=workspace / "artifacts",
            tmp_path=workspace / "tmp",
            task_card_path=task_path,
            root_problem_path=root_path,
            portfolio_index_path=portfolio_path,
            access_manifest_path=access_path,
        )

    def _copy_skills(self, workspace: Path, skills: Sequence[str]) -> None:
        if not skills:
            return
        destination_root = _target(workspace, ".agents/skills")
        destination_root.mkdir(parents=True, mode=0o755)
        for name in skills:
            _component(name, "skill name")
            source = next(
                (
                    root / name
                    for root in self.skill_sources
                    if (root / name).is_dir() and not (root / name).is_symlink()
                ),
                None,
            )
            if source is None:
                raise MaterializationError(f"missing or unsafe skill source: {name}")
            destination = destination_root / name
            destination.mkdir(mode=0o755)
            for source_path in source.rglob("*"):
                if source_path.is_symlink():
                    raise MaterializationError(f"skill contains a forbidden symlink: {source_path}")
                relative = source_path.relative_to(source)
                if any(part.startswith(".") or part == "__pycache__" for part in relative.parts):
                    continue
                target = _target(workspace, Path(".agents/skills") / name / relative)
                if source_path.is_dir():
                    target.mkdir(mode=0o755, exist_ok=True)
                elif source_path.is_file():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with source_path.open("rb") as source_handle:
                        data = source_handle.read()
                    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
                    if hasattr(os, "O_NOFOLLOW"):
                        flags |= os.O_NOFOLLOW
                    descriptor = os.open(target, flags, 0o444)
                    with os.fdopen(descriptor, "wb") as target_handle:
                        target_handle.write(data)
                else:
                    raise MaterializationError(f"unsupported skill entry: {source_path}")

    @staticmethod
    def _freeze_inputs(workspace: Path) -> None:
        for root_name in ("input", ".agents"):
            root = workspace / root_name
            if not root.exists():
                continue
            for path in sorted(root.rglob("*"), reverse=True):
                if path.is_symlink():
                    raise MaterializationError(f"materialized input contains symlink: {path}")
                if path.is_dir():
                    path.chmod(0o555)
                elif path.is_file():
                    executable = bool(path.stat().st_mode & stat.S_IXUSR)
                    path.chmod(0o555 if executable else 0o444)
            root.chmod(0o555)


__all__ = [
    "EXPLORER_SNAPSHOT_FORMAT_VERSION",
    "EXPLORER_SNAPSHOT_RELATIVE_PATH",
    "MAIN_MEMORY_SNAPSHOT_RELATIVE_PATH",
    "MaterializationError",
    "MaterializedWorkspace",
    "MemorySnapshot",
    "WorkspaceMaterializer",
    "explorer_snapshot_digest",
    "validate_explorer_snapshot",
]
