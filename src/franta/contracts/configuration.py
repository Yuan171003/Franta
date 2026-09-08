"""Typed bootstrap-configuration contracts.

Parsing TOML and reading referenced files remain implementation concerns in
``franta.config``.  This module owns only the immutable values and their
mechanical, context-free validation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import hashlib
import json
from typing import Any, Mapping

from ..advisor_adapter import (
    PortableAdvisorSettings as _PortableAdvisorSettings,
    AdvisorSettingsError,
)

from ..explorer_adapter import (
    PortableExplorerSettings as _PortableExplorerSettings,
    ExplorerSettingsValidationError,
)

from .failures import ConfigurationError


DEFAULT_MODEL = "gpt-6-astra"
DEFAULT_REASONING = "ultra"
SYNTH_REASONING = "xhigh"


AdvisorSettings = _PortableAdvisorSettings


@dataclass(frozen=True)
class ModelSettings:
    model: str = DEFAULT_MODEL
    reasoning_effort: str = DEFAULT_REASONING


@dataclass(frozen=True)
class RetrySettings:
    worker_interruptions: int = 3
    verifier_transport: int = 3
    synthesizer_transport: int = 3
    summarizer_transport: int = 3
    main_transport: int = 3
    trimmer_transport: int = 3

    def validate(self) -> None:
        for name, value in vars(self).items():
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise ConfigurationError(f"retries.{name} must be a positive integer")


@dataclass(frozen=True)
class TimeoutSettings:
    """Optional operator-configured watchdog deadline for one Codex call."""

    agent_call_seconds: float | None = None

    def validate(self) -> None:
        value = self.agent_call_seconds
        if value is not None and (
            isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0
        ):
            raise ConfigurationError(
                "timeouts.agent_call_seconds must be a positive number"
            )


@dataclass(frozen=True)
class ExplorerSettings:
    """Franta manifest wire shape adapted to portable Explorer settings."""

    enabled: bool = True
    max_workers: int = 4
    attempts_per_worker: int = 3
    attempt_seconds: int = 3 * 60 * 60
    explorer_admission_seconds: int = 2 * 60 * 60
    franta_admission_seconds: int = 8 * 60 * 60
    max_scratch_per_attempt: int = 256
    max_scratch_per_turn: int = 4096
    max_abstract_bytes: int = 4096
    max_content_bytes: int = 262_144

    def validate(self) -> None:
        try:
            _PortableExplorerSettings(
                enabled=self.enabled,
                max_workers=self.max_workers,
                attempts_per_worker=self.attempts_per_worker,
                attempt_seconds=self.attempt_seconds,
                explorer_admission_seconds=self.explorer_admission_seconds,
                host_admission_seconds=self.franta_admission_seconds,
                max_scratch_per_attempt=self.max_scratch_per_attempt,
                max_scratch_per_turn=self.max_scratch_per_turn,
                max_abstract_bytes=self.max_abstract_bytes,
                max_content_bytes=self.max_content_bytes,
            ).validate()
        except ExplorerSettingsValidationError as exc:
            message = str(exc).replace(
                "explorer.host_admission_seconds",
                "explorer.franta_admission_seconds",
            )
            if exc.code == "enabled_must_be_true":
                message = (
                    "explorer.enabled must be true when the [explorer] table is present; "
                    "remove the table to retain legacy Franta scheduling"
                )
            raise ConfigurationError(message) from None


@dataclass(frozen=True)
class Limits:
    max_non_verifier_workers: int = 4
    max_parallel_verifiers: int = 2
    max_portfolio_memories: int = 20
    max_search_results: int = 10
    worker_resume_launches: int = 6
    verifier_revision_requests: int = 2
    assignments_per_trim_review: int = 8
    trimmer_rounds_per_session: int = 3

    def validate(self) -> None:
        if not 1 <= self.max_non_verifier_workers <= 4:
            raise ConfigurationError("limits.max_non_verifier_workers must be between 1 and 4")
        if self.max_parallel_verifiers <= 0:
            raise ConfigurationError("limits.max_parallel_verifiers must be positive")
        if self.max_portfolio_memories <= 0:
            raise ConfigurationError("limits.max_portfolio_memories must be positive")
        if not 1 <= self.max_search_results <= 10:
            raise ConfigurationError("limits.max_search_results must be between 1 and 10")
        # These values are explicit workflow invariants in Design.md, not tunable
        # heuristics or memory quotas.
        if self.worker_resume_launches != 6:
            raise ConfigurationError("limits.worker_resume_launches must remain 6")
        if self.verifier_revision_requests != 2:
            raise ConfigurationError("limits.verifier_revision_requests must remain 2")
        if self.assignments_per_trim_review != 8:
            raise ConfigurationError("limits.assignments_per_trim_review must remain 8")
        if self.trimmer_rounds_per_session != 3:
            raise ConfigurationError("limits.trimmer_rounds_per_session must remain 3")


@dataclass(frozen=True)
class ToolSettings:
    codex: str = "codex"
    sage: str | None = None
    macaulay2: str | None = None
    tectonic: str | None = None
    extra_cas: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class InitialMaterial:
    obligations: tuple[Mapping[str, Any], ...] = ()
    routes: tuple[Mapping[str, Any], ...] = ()
    memos: tuple[Mapping[str, Any], ...] = ()
    claims: tuple[Mapping[str, Any], ...] = ()
    seed_theorems: tuple[Mapping[str, Any], ...] = ()


@dataclass(frozen=True)
class BootstrapManifest:
    source: Path
    project_name: str
    project_dir: Path
    root_problem: str
    foundation_policy: str
    context_budgets: Mapping[str, int]
    default_model: ModelSettings
    synthesizer_model: ModelSettings
    retries: RetrySettings
    timeouts: TimeoutSettings
    limits: Limits
    tools: ToolSettings
    initial: InitialMaterial
    native_web_search: bool = True
    explorer: ExplorerSettings | None = None
    advisor: AdvisorSettings | None = None

    @property
    def manifest_digest(self) -> str:
        payload = {
            "project_name": self.project_name,
            "project_dir": str(self.project_dir),
            "root_problem": self.root_problem,
            "foundation_policy": self.foundation_policy,
            "context_budgets": dict(self.context_budgets),
            "default_model": vars(self.default_model),
            "synthesizer_model": vars(self.synthesizer_model),
            "retries": vars(self.retries),
            "timeouts": vars(self.timeouts),
            "limits": vars(self.limits),
            "tools": {
                **vars(self.tools),
                "extra_cas": dict(self.tools.extra_cas),
            },
            "initial": {k: list(v) for k, v in vars(self.initial).items()},
            "native_web_search": self.native_web_search,
        }
        # Preserve the exact digest contract for every legacy manifest.  The
        # Explorer contract participates in identity only when explicitly
        # enabled by a manifest table.
        if self.explorer is not None:
            payload["explorer"] = vars(self.explorer)
        if self.advisor is not None:
            payload["advisor"] = vars(self.advisor)
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(raw.encode()).hexdigest()


__all__ = [
    "AdvisorSettings",
    "AdvisorSettingsError",
    "BootstrapManifest",
    "DEFAULT_MODEL",
    "DEFAULT_REASONING",
    "ExplorerSettings",
    "InitialMaterial",
    "Limits",
    "ModelSettings",
    "RetrySettings",
    "SYNTH_REASONING",
    "TimeoutSettings",
    "ToolSettings",
]
