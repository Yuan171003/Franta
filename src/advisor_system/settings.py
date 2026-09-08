"""Configuration intrinsic to the portable Advisor block."""

from __future__ import annotations

from dataclasses import dataclass

from .contracts import MAX_SELECTED_SUBPROBLEMS, OBLIGATION_COUNT


ADVISOR_MODEL_NAME = "gpt-6-astra"
ADVISOR_REASONING_EFFORT = "ultra"
ADVISOR_SESSION_KEY = "advisor:project"
ADVISOR_MEMORY_ACCESS_PROFILE = "main-agent-equivalent"


class AdvisorSettingsValidationError(ValueError):
    """An intrinsic Advisor setting violates the fixed workflow contract."""

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class AdvisorSettings:
    """Portable model route and fixed cardinalities for Advisor calls."""

    enabled: bool = True
    model: str = ADVISOR_MODEL_NAME
    reasoning_effort: str = ADVISOR_REASONING_EFFORT
    session_key: str = ADVISOR_SESSION_KEY
    memory_access_profile: str = ADVISOR_MEMORY_ACCESS_PROFILE
    obligation_count: int = OBLIGATION_COUNT
    max_selected_subproblems: int = MAX_SELECTED_SUBPROBLEMS

    def validate(self) -> None:
        if self.enabled is not True:
            raise AdvisorSettingsValidationError(
                "advisor.enabled must be true when Advisor is configured",
                code="enabled_must_be_true",
            )
        for name in ("model", "reasoning_effort", "session_key", "memory_access_profile"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip() or value != value.strip():
                raise AdvisorSettingsValidationError(
                    f"advisor.{name} must be canonical text", code=f"invalid_{name}"
                )
        if self.obligation_count != OBLIGATION_COUNT:
            raise AdvisorSettingsValidationError(
                "Advisor must propose exactly five obligations",
                code="invalid_obligation_count",
            )
        if self.max_selected_subproblems != MAX_SELECTED_SUBPROBLEMS:
            raise AdvisorSettingsValidationError(
                "Advisor may select at most two subproblems",
                code="invalid_max_selected_subproblems",
            )


def must_resume_session(*, advisor_index: int, stage: str) -> bool:
    """Return whether this logical call must use the existing Advisor session."""

    if not isinstance(advisor_index, int) or isinstance(advisor_index, bool) or advisor_index < 1:
        raise AdvisorSettingsValidationError(
            "advisor_index must be positive", code="invalid_advisor_index"
        )
    if stage not in ("proposal", "finalize"):
        raise AdvisorSettingsValidationError("unknown Advisor stage", code="invalid_stage")
    return stage == "finalize" or advisor_index > 1


# Short alias retained for callers that prefer the subsystem-wide naming style.
AdvisorSettingsError = AdvisorSettingsValidationError


__all__ = [
    "ADVISOR_MEMORY_ACCESS_PROFILE",
    "ADVISOR_MODEL_NAME",
    "ADVISOR_REASONING_EFFORT",
    "ADVISOR_SESSION_KEY",
    "AdvisorSettings",
    "AdvisorSettingsError",
    "AdvisorSettingsValidationError",
    "must_resume_session",
]
