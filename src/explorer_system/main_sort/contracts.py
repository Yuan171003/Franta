"""Host-neutral contracts owned by the isolated main-sort block.

Only standard-library types live here.  A collaborator may therefore import
these values when constructing its adapter without importing Explorer worker
or host runtime code.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


MAIN_SORT_ROLE = "main-sort"
MAIN_SORT_SCHEMA_NAME = MAIN_SORT_ROLE
MAIN_SORT_MODEL_NAME = "gpt-6-astra"
MAIN_SORT_REASONING_EFFORT = "ultra"


def _object(properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    if len(required) != len(set(required)) or set(required) != set(properties):
        raise ValueError("strict object schemas must require every property exactly once")
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


def _string_array() -> dict[str, Any]:
    return {"type": "array", "items": {"type": "string"}}


MAIN_SORT_RESPONSE_SCHEMA: dict[str, Any] = _object(
    {
        "sort_ended": {"type": "boolean"},
        "final_progress_id": {"type": "string"},
        "selected_explorer_record_ids": _string_array(),
        "deferred_computation_record_ids": _string_array(),
    },
    [
        "sort_ended",
        "final_progress_id",
        "selected_explorer_record_ids",
        "deferred_computation_record_ids",
    ],
)


@dataclass(frozen=True)
class HostSortTools:
    """Host tool and field names supplied at the main-sort seam."""

    published_search: str = "published-memory-search"
    progress_writer: str = "publish-progress"
    provenance_field: str = "explorer_source"
    computation_exports_field: str = "explorer_computation_exports"

    def __post_init__(self) -> None:
        for name, value in vars(self).items():
            if (
                not isinstance(value, str)
                or not value
                or value != value.strip()
                or any(character.isspace() for character in value)
            ):
                raise ValueError(f"{name} must be a nonempty whitespace-free name")


@dataclass(frozen=True)
class MainSortModelRoute:
    """Host-neutral model selection for one main-sort call."""

    model: str
    reasoning_effort: str


@dataclass(frozen=True)
class MainSortLaunchSpec:
    """Complete main-sort prompt/model/schema selection for a host adapter."""

    role: str
    prompt: str
    schema_name: str
    model_route: MainSortModelRoute


__all__ = [
    "MAIN_SORT_MODEL_NAME",
    "MAIN_SORT_REASONING_EFFORT",
    "MAIN_SORT_RESPONSE_SCHEMA",
    "MAIN_SORT_ROLE",
    "MAIN_SORT_SCHEMA_NAME",
    "HostSortTools",
    "MainSortLaunchSpec",
    "MainSortModelRoute",
]
