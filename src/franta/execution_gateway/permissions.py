"""Least-privilege Codex permission profiles for Block 6 launches."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from ..contracts.agent_access import AccessPolicy


def _toml_string(value: str) -> str:
    # JSON strings are valid TOML basic strings.
    return json.dumps(value, ensure_ascii=False)


def _toml_inline_table(value: Mapping[str, Any]) -> str:
    entries: list[str] = []
    for key, item in value.items():
        if isinstance(item, Mapping):
            rendered = _toml_inline_table(item)
        elif isinstance(item, bool):
            rendered = "true" if item else "false"
        else:
            rendered = _toml_string(str(item))
        entries.append(f"{_toml_string(str(key))} = {rendered}")
    return "{" + ", ".join(entries) + "}"


@dataclass(frozen=True)
class CodexPermissionProfile:
    """A least-privilege Codex beta permission profile.

    The active task workspace is read-only except for the three staging
    directories.  Canonical and scheduler-private roots are explicit denies,
    and local commands have no network even when native web search is enabled.
    """

    name: str
    denied_paths: tuple[str, ...] = ()
    writable_relative_paths: tuple[str, ...] = ("outbox", "artifacts", "tmp")

    @classmethod
    def for_policy(
        cls,
        policy: AccessPolicy,
        *,
        canonical_path: str | os.PathLike[str] | None = None,
        private_paths: Sequence[str | os.PathLike[str]] = (),
    ) -> "CodexPermissionProfile":
        denied: list[str] = []
        for value in ([canonical_path] if canonical_path is not None else []):
            denied.append(str(Path(value).resolve()))
        denied.extend(str(Path(value).resolve()) for value in private_paths)
        safe_name = re.sub(r"[^a-z0-9-]+", "-", policy.name.lower()).strip("-")
        return cls(
            name=f"franta-{safe_name or 'agent'}",
            denied_paths=tuple(dict.fromkeys(denied)),
            writable_relative_paths=policy.writable_workspace_paths,
        )

    def config_overrides(self) -> tuple[tuple[str, str], ...]:
        """Return ``-c key=value`` components without legacy sandbox keys."""

        prefix = f"permissions.{self.name}"
        workspace_rules = {".": "read"}
        for relative in self.writable_relative_paths:
            if not relative or Path(relative).is_absolute() or ".." in Path(relative).parts:
                raise ValueError(f"unsafe writable workspace path: {relative!r}")
            workspace_rules[relative] = "write"
        filesystem: dict[str, Any] = {
            ":minimal": "read",
            # The transport points TMPDIR inside the sealed workspace.
            ":tmpdir": "write",
            ":slash_tmp": "deny",
            ":workspace_roots": workspace_rules,
        }
        for path in self.denied_paths:
            filesystem[path] = "deny"
        values: list[tuple[str, str]] = [
            ("default_permissions", _toml_string(self.name)),
            (f"{prefix}.description", _toml_string("Franta sealed agent workspace")),
            (f"{prefix}.filesystem", _toml_inline_table(filesystem)),
            (f"{prefix}.network.enabled", "false"),
            ("shell_environment_policy.inherit", _toml_string("core")),
            (
                'shell_environment_policy.filters."FRANTA_BROKER_TOKEN"',
                _toml_string("exclude"),
            ),
            (
                'shell_environment_policy.filters."FRANTA_BROKER_SOCKET"',
                _toml_string("exclude"),
            ),
            (
                'shell_environment_policy.filters."FRANTA_BROKER_CAPABILITY_FILE"',
                _toml_string("exclude"),
            ),
        ]
        return tuple(values)


__all__ = ["CodexPermissionProfile"]
