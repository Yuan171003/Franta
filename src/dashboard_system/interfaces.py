"""Read-only research views and explicit operator commands supplied by a host."""

from typing import Any, Mapping, Protocol


class DashboardReadPort(Protocol):
    def overview(self) -> dict[str, Any]: ...

    def main_memory(self, *, kind: str = "all", query: str = "", offset: int = 0,
                    limit: int = 50) -> dict[str, Any]: ...

    def main_record(self, record_id: str) -> dict[str, Any] | None: ...

    def memory_graph(self) -> dict[str, Any]: ...

    def explorer_memory(self, *, record_type: str, query: str = "", offset: int = 0,
                        limit: int = 50) -> dict[str, Any]: ...

    def monitor_snapshot(self) -> dict[str, Any]: ...


class OperatorCommandPort(Protocol):
    def submit_guidance(self, text: str) -> dict[str, Any]: ...

    def submit_advisor_feedback(self, request_id: str,
                               response: Mapping[str, Any]) -> dict[str, Any]: ...


class MonitorPort(Protocol):
    def summarize(self, snapshot: Mapping[str, Any]) -> dict[str, Any]: ...
