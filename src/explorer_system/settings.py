"""Portable configuration values intrinsic to the Explorer subsystem.

Host applications remain responsible for parsing their configuration format
and for enforcing cross-system constraints (for example, comparing Explorer's
worker limit with a host scheduler limit).  This module validates only the
fixed invariants owned by Explorer itself.
"""

from __future__ import annotations

from dataclasses import dataclass


class ExplorerSettingsValidationError(ValueError):
    """An Explorer setting violates an intrinsic subsystem invariant."""

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ExplorerSettings:
    """Opt-in timing and bounded-storage settings for Explorer alternation.

    The host decides what the absence of an Explorer configuration means.
    Once configured, these values describe admission windows; already
    admitted attempts are drained gracefully after a window closes.
    """

    enabled: bool = True
    max_workers: int = 4
    attempts_per_worker: int = 3
    attempt_seconds: int = 4 * 60 * 60
    explorer_admission_seconds: int = 2 * 60 * 60
    host_admission_seconds: int = 8 * 60 * 60
    max_scratch_per_attempt: int = 256
    max_scratch_per_turn: int = 4096
    max_abstract_bytes: int = 4096
    max_content_bytes: int = 262_144

    def validate(self) -> None:
        if self.enabled is not True:
            raise ExplorerSettingsValidationError(
                "explorer.enabled must be true when Explorer is configured; "
                "omit Explorer configuration to disable it",
                code="enabled_must_be_true",
            )
        if (
            not isinstance(self.max_workers, int)
            or isinstance(self.max_workers, bool)
            or not 1 <= self.max_workers <= 4
        ):
            raise ExplorerSettingsValidationError(
                "explorer.max_workers must be between 1 and 4",
                code="invalid_max_workers",
            )
        if self.attempts_per_worker != 3:
            raise ExplorerSettingsValidationError(
                "explorer.attempts_per_worker must remain 3",
                code="invalid_attempt_count",
            )
        for name in (
            "attempt_seconds",
            "explorer_admission_seconds",
            "host_admission_seconds",
        ):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise ExplorerSettingsValidationError(
                    f"explorer.{name} must be a positive integer",
                    code=f"invalid_{name}",
                )
        if self.attempt_seconds > 4 * 60 * 60:
            raise ExplorerSettingsValidationError(
                "explorer.attempt_seconds cannot exceed 14400 seconds",
                code="attempt_limit_exceeds_contract",
            )
        for name in (
            "max_scratch_per_attempt",
            "max_scratch_per_turn",
            "max_abstract_bytes",
            "max_content_bytes",
        ):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise ExplorerSettingsValidationError(
                    f"explorer.{name} must be a positive integer",
                    code=f"invalid_{name}",
                )
        if self.max_scratch_per_turn < self.max_scratch_per_attempt:
            raise ExplorerSettingsValidationError(
                "explorer.max_scratch_per_turn must be at least max_scratch_per_attempt",
                code="invalid_scratch_bounds",
            )
        if self.max_abstract_bytes > 4096:
            raise ExplorerSettingsValidationError(
                "explorer.max_abstract_bytes cannot exceed the fixed 4096-byte skill contract",
                code="abstract_limit_exceeds_contract",
            )
        if self.max_content_bytes > 262_144:
            raise ExplorerSettingsValidationError(
                "explorer.max_content_bytes cannot exceed the fixed 262144-byte skill contract",
                code="content_limit_exceeds_contract",
            )


__all__ = ["ExplorerSettings", "ExplorerSettingsValidationError"]
