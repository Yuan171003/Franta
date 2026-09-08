"""Bootstrap manifest parsing and mechanical validation.

TOML is an implementation choice.  The parsed configuration deliberately contains
only mechanical settings; it does not encode mathematical priorities or scoring.
"""

from __future__ import annotations

from pathlib import Path
import tomllib
from typing import Any, Mapping

from .contracts.configuration import (
    AdvisorSettings,
    AdvisorSettingsError,
    BootstrapManifest,
    DEFAULT_MODEL,
    DEFAULT_REASONING,
    ExplorerSettings,
    InitialMaterial,
    Limits,
    ModelSettings,
    RetrySettings,
    SYNTH_REASONING,
    TimeoutSettings,
    ToolSettings,
)
from .contracts.failures import ConfigurationError


def _table(data: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = data.get(key, {})
    if not isinstance(value, Mapping):
        raise ConfigurationError(f"{key} must be a TOML table")
    return value


def _load_text(base: Path, table: Mapping[str, Any], inline: str, file_key: str) -> str:
    direct = table.get(inline)
    file_name = table.get(file_key)
    if bool(direct) == bool(file_name):
        raise ConfigurationError(f"set exactly one of project.{inline} and project.{file_key}")
    if direct:
        if not isinstance(direct, str) or not direct.strip():
            raise ConfigurationError(f"project.{inline} must be nonempty text")
        return direct.strip()
    path = (base / str(file_name)).resolve()
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise ConfigurationError(f"cannot read {path}: {exc}") from exc


def _model(table: Mapping[str, Any], *, synthesizer: bool = False) -> ModelSettings:
    expected_effort = SYNTH_REASONING if synthesizer else DEFAULT_REASONING
    return ModelSettings(
        model=str(table.get("model", DEFAULT_MODEL)),
        reasoning_effort=str(table.get("reasoning_effort", expected_effort)),
    )


def _dataclass_values(cls: type[Any], table: Mapping[str, Any]) -> Any:
    allowed = cls.__dataclass_fields__
    unknown = set(table) - set(allowed)
    if unknown:
        raise ConfigurationError(f"unknown {cls.__name__} keys: {', '.join(sorted(unknown))}")
    return cls(**table)


def _tool_command(base: Path, value: Any, label: str) -> str:
    """Keep PATH names portable and anchor explicit paths to the manifest."""

    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise ConfigurationError(f"{label} must be a nonempty executable name or path")
    if "/" in value or value.startswith("~"):
        try:
            return str((base / Path(value).expanduser()).resolve())
        except (OSError, RuntimeError) as exc:
            raise ConfigurationError(f"cannot resolve {label}: {exc}") from exc
    return value


def load_manifest(path: str | Path) -> BootstrapManifest:
    source = Path(path).resolve()
    try:
        data = tomllib.loads(source.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigurationError(f"cannot load manifest {source}: {exc}") from exc

    project = _table(data, "project")
    name = project.get("name")
    directory = project.get("directory")
    if not isinstance(name, str) or not name.strip():
        raise ConfigurationError("project.name must be nonempty")
    if not isinstance(directory, str) or not directory.strip():
        raise ConfigurationError("project.directory must be nonempty")
    project_dir = (source.parent / directory).resolve()
    root_problem = _load_text(source.parent, project, "root_problem", "root_problem_file")
    foundation = _load_text(
        source.parent, project, "foundation_policy", "foundation_policy_file"
    )

    retry_table = _table(data, "retries")
    limit_table = _table(data, "limits")
    retries = _dataclass_values(RetrySettings, retry_table)
    timeouts = _dataclass_values(TimeoutSettings, _table(data, "timeouts"))
    limits = _dataclass_values(Limits, limit_table)
    retries.validate()
    timeouts.validate()
    limits.validate()

    models = _table(data, "models")
    default_model = _model(_table(models, "default"))
    synthesizer_model = _model(_table(models, "synthesizer"), synthesizer=True)

    tool_data = dict(_table(data, "tools"))
    extra_cas = tool_data.pop("extra_cas", {})
    if not isinstance(extra_cas, Mapping):
        raise ConfigurationError("tools.extra_cas must be a table")
    unknown_tools = set(tool_data) - (set(ToolSettings.__dataclass_fields__) - {"extra_cas"})
    if unknown_tools:
        raise ConfigurationError("unknown tools keys: " + ", ".join(sorted(unknown_tools)))
    normalized_tools = {
        name: _tool_command(source.parent, value, f"tools.{name}")
        for name, value in tool_data.items()
    }
    normalized_cas = {}
    for name, value in extra_cas.items():
        if not isinstance(name, str) or not name.strip():
            raise ConfigurationError("tools.extra_cas names must be nonempty text")
        normalized_cas[name] = _tool_command(source.parent, value, f"tools.extra_cas.{name}")
    tools = ToolSettings(extra_cas=normalized_cas, **normalized_tools)

    budgets = _table(data, "context_budgets")
    for budget_name, budget in budgets.items():
        if not isinstance(budget, int) or isinstance(budget, bool) or budget <= 0:
            raise ConfigurationError(f"context_budgets.{budget_name} must be positive")

    initial_data = _table(data, "initial")
    initial_kwargs: dict[str, tuple[Mapping[str, Any], ...]] = {}
    for field_name in InitialMaterial.__dataclass_fields__:
        entries = initial_data.get(field_name, [])
        if not isinstance(entries, list) or any(not isinstance(x, Mapping) for x in entries):
            raise ConfigurationError(f"initial.{field_name} must be an array of tables")
        initial_kwargs[field_name] = tuple(dict(x) for x in entries)

    agents = _table(data, "agents")
    native_web_search = agents.get("native_web_search", True)
    if native_web_search is not True:
        raise ConfigurationError(
            "agents.native_web_search must be true: the approved design grants it to all "
            "workers, main agents, and trimmers"
        )

    explorer: ExplorerSettings | None = None
    if "explorer" in data:
        explorer = _dataclass_values(ExplorerSettings, _table(data, "explorer"))
        explorer.validate()
        if explorer.max_workers > limits.max_non_verifier_workers:
            raise ConfigurationError(
                "explorer.max_workers cannot exceed limits.max_non_verifier_workers"
            )

    advisor: AdvisorSettings | None = None
    if "advisor" in data:
        if explorer is None:
            raise ConfigurationError(
                "the [advisor] table requires the [explorer] table"
            )
        advisor = _dataclass_values(AdvisorSettings, _table(data, "advisor"))
        try:
            advisor.validate()
        except AdvisorSettingsError as exc:
            raise ConfigurationError(str(exc)) from None

    return BootstrapManifest(
        source=source,
        project_name=name.strip(),
        project_dir=project_dir,
        root_problem=root_problem,
        foundation_policy=foundation,
        context_budgets=dict(budgets),
        default_model=default_model,
        synthesizer_model=synthesizer_model,
        retries=retries,
        timeouts=timeouts,
        limits=limits,
        tools=tools,
        initial=InitialMaterial(**initial_kwargs),
        native_web_search=True,
        explorer=explorer,
        advisor=advisor,
    )
