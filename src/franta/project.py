"""Project layout and safe path resolution.

The physical layout is an implementation choice.  Keeping it in one value
object lets the scheduler and permission-profile builder agree on every private
or agent-readable boundary.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os

from .contracts.failures import AccessDenied


@dataclass(frozen=True)
class ProjectLayout:
    root: Path

    @classmethod
    def at(cls, root: str | Path) -> "ProjectLayout":
        return cls(Path(root).resolve())

    @property
    def database(self) -> Path:
        return self.root / "scheduler.sqlite3"

    @property
    def canonical(self) -> Path:
        return self.root / "canonical"

    @property
    def indexes(self) -> Path:
        return self.root / "indexes"

    @property
    def portfolios(self) -> Path:
        return self.root / "portfolios"

    @property
    def categories(self) -> Path:
        return self.root / "categories"

    @property
    def audit(self) -> Path:
        return self.root / "audit"

    @property
    def task_archive(self) -> Path:
        return self.root / "task-archive"

    @property
    def private(self) -> Path:
        return self.root / "private"

    @property
    def explorer_database(self) -> Path:
        """Scheduler-private, noncanonical Explorer record store."""

        return self.private / "explorer.sqlite3"

    @property
    def explorer_cas_archive(self) -> Path:
        """Content-addressed evidence retained outside agent workspaces."""

        return self.private / "explorer-cas"

    @property
    def main_memory_snapshots(self) -> Path:
        """Scheduler-private immutable inputs for recoverable Main calls."""

        return self.private / "main-memory-snapshots"

    @property
    def advisor_memory_snapshots(self) -> Path:
        """Scheduler-private immutable inputs supplied through the Advisor port."""

        return self.private / "advisor-memory-snapshots"

    @property
    def advisor_reports(self) -> Path:
        """Human-readable, immutable Advisor selection reports."""

        return self.root / "advisor-reports"

    @property
    def workspaces(self) -> Path:
        return self.root / "workspaces"

    @property
    def schemas(self) -> Path:
        return self.root / "schemas"

    @property
    def foundation(self) -> Path:
        return self.root / "foundation-v1.md"

    @property
    def root_problem(self) -> Path:
        return self.root / "root-problem.md"

    @property
    def manifest_snapshot(self) -> Path:
        return self.root / "bootstrap-manifest.toml"

    @property
    def lock_file(self) -> Path:
        return self.root / "scheduler.lock"

    @property
    def scheduler_private_paths(self) -> tuple[Path, ...]:
        return (
            self.database,
            self.database.with_name(self.database.name + "-wal"),
            self.database.with_name(self.database.name + "-shm"),
            self.private,
            self.task_archive,
            self.audit,
        )

    def create(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        for directory in (
            self.canonical,
            self.indexes,
            self.portfolios,
            self.categories,
            self.audit,
            self.task_archive,
            self.private,
            self.workspaces,
            self.schemas,
        ):
            directory.mkdir(parents=True, exist_ok=True)

    def workspace(self, owner_id: str, *, create: bool = False) -> Path:
        if not owner_id or owner_id in {".", ".."} or any(
            separator in owner_id for separator in ("/", "\\", os.sep)
        ):
            raise AccessDenied("workspace owner must be a single safe path component")
        candidate = self.workspaces / owner_id
        resolved_parent = candidate.parent.resolve()
        if resolved_parent != self.workspaces.resolve():
            raise AccessDenied("workspace path escapes the project workspace root")
        if create:
            candidate.mkdir(parents=True, exist_ok=False)
        return candidate


def require_beneath(path: str | Path, root: str | Path) -> Path:
    """Resolve a path without allowing traversal or symlink escape."""

    resolved = Path(path).resolve(strict=True)
    allowed = Path(root).resolve(strict=True)
    if resolved != allowed and allowed not in resolved.parents:
        raise AccessDenied(f"path {resolved} is outside {allowed}")
    return resolved
