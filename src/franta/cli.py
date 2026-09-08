"""Terminal entry points for fresh, resumed, and observational Franta runs."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence, TextIO

from .config import load_manifest
from .evaluation import evaluate_project
from .human_guidance import submit_human_guidance
from .runtime import FrantaRuntime


def _print_json(value: Mapping[str, Any], output: TextIO) -> None:
    json.dump(value, output, ensure_ascii=False, indent=2, sort_keys=True)
    output.write("\n")


def _cycles(value: str) -> int:
    parsed = int(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("max cycles must be nonnegative")
    return parsed


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="franta",
        description="Run or inspect a recoverable Franta mathematical-research project.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    initialize = subparsers.add_parser(
        "init", help="create canonical scheduler state from a bootstrap manifest"
    )
    initialize.add_argument("manifest")

    start = subparsers.add_parser(
        "start", help="initialize from a manifest and run from the fresh durable state"
    )
    start.add_argument("manifest")
    start.add_argument("--max-cycles", type=_cycles)
    start.add_argument("--no-dashboard", action="store_true")
    start.add_argument("--dashboard-port", type=int, default=1113)

    resume = subparsers.add_parser(
        "resume", help="recover a stopped project and continue exact persisted work"
    )
    resume.add_argument("project")
    resume.add_argument("--max-cycles", type=_cycles)
    resume.add_argument("--no-dashboard", action="store_true")
    resume.add_argument("--dashboard-port", type=int, default=1113)

    dashboard = subparsers.add_parser("dashboard", help="open or reuse the independent local dashboard")
    dashboard.add_argument("project")
    dashboard.add_argument("--port", type=int, default=1113)

    status = subparsers.add_parser("status", help="show durable project status")
    status.add_argument("project")

    evaluate = subparsers.add_parser(
        "evaluate", help="run the read-only requirement-6 evaluation harness"
    )
    evaluate.add_argument("project")

    suggest = subparsers.add_parser(
        "suggest", help="queue a human research suggestion, including while a project runs"
    )
    suggest.add_argument("project")
    suggest.add_argument("text", help="exact guidance text, or @PATH to a UTF-8 Markdown file")

    guidance = subparsers.add_parser(
        "guidance", help="answer a persisted human-guidance request"
    )
    guidance.add_argument("project")
    guidance.add_argument("request_id")
    guidance.add_argument("response")

    cancel = subparsers.add_parser(
        "cancel-guidance", help="cancel a persisted human-guidance request"
    )
    cancel.add_argument("project")
    cancel.add_argument("request_id")

    advisor_feedback = subparsers.add_parser(
        "advisor-feedback",
        help="answer a persisted Advisor selection request with structured JSON",
    )
    advisor_feedback.add_argument("project")
    advisor_feedback.add_argument("request_id")
    advisor_feedback.add_argument(
        "response",
        help=(
            "JSON object with one or two listed/custom choices, or @PATH to a JSON file"
        ),
    )

    cancel_sprint = subparsers.add_parser(
        "cancel-sprint", help="cancel a persisted discovery sprint"
    )
    cancel_sprint.add_argument("project")
    cancel_sprint.add_argument("sprint_id")
    cancel_sprint.add_argument("--reason", required=True)

    rebuild = subparsers.add_parser(
        "rebuild-projections", help="recreate deterministic readable projections"
    )
    rebuild.add_argument("project")

    references = subparsers.add_parser(
        "refs", help="list durable typed temporary-memory references"
    )
    references.add_argument("project")
    references.add_argument(
        "--state", choices=("pending", "resolved", "rejected", "abandoned")
    )

    reference_status = subparsers.add_parser(
        "ref-status", help="show one temporary-memory reference"
    )
    reference_status.add_argument("project")
    reference_status.add_argument("temporary_id")

    resolve_reference = subparsers.add_parser(
        "resolve-ref", help="explicitly correct a temporary target to a canonical ID"
    )
    resolve_reference.add_argument("project")
    resolve_reference.add_argument("temporary_id")
    resolve_reference.add_argument("canonical_id")
    resolve_reference.add_argument("--resolution", required=True)
    resolve_reference.add_argument("--operation-id", required=True)

    abandon_reference = subparsers.add_parser(
        "abandon-ref", help="explicitly abandon an unresolved temporary target"
    )
    abandon_reference.add_argument("project")
    abandon_reference.add_argument("temporary_id")
    abandon_reference.add_argument("--reason", required=True)
    abandon_reference.add_argument("--operation-id", required=True)
    return parser


def _open(project: str) -> FrantaRuntime:
    return FrantaRuntime.open(Path(project).resolve())


def _start_dashboard(runtime: FrantaRuntime, arguments: Any) -> str | None:
    if arguments.no_dashboard or arguments.max_cycles == 0:
        return None
    try:
        from .dashboard_adapter import ensure_dashboard
        url = ensure_dashboard(runtime.layout.root, port=arguments.dashboard_port)
        print(f"Dashboard: {url}", file=sys.stderr, flush=True)
        return url
    except Exception as exc:
        print(f"Dashboard unavailable: {exc}", file=sys.stderr, flush=True)
        return None


def main(argv: Sequence[str] | None = None, *, output: TextIO | None = None) -> int:
    """Execute one CLI command.

    A foreground run may be interrupted by the operator. All agent calls and
    accepted artifacts are persisted before launch or commit, so ``resume`` is
    the supported continuation path rather than an old chat transcript.
    """

    stream = output or sys.stdout
    arguments = _parser().parse_args(argv)
    runtime: FrantaRuntime | None = None
    try:
        if arguments.command in {"init", "start"}:
            runtime = FrantaRuntime.initialize(load_manifest(arguments.manifest))
            dashboard_url = _start_dashboard(runtime, arguments) if arguments.command == "start" else None
            result = (
                runtime.run(max_cycles=arguments.max_cycles)
                if arguments.command == "start"
                else runtime.status()
            )
            if dashboard_url:
                result["dashboard_url"] = dashboard_url
            _print_json(result, stream)
            return 0
        if arguments.command == "status":
            _print_json(FrantaRuntime.read_status(arguments.project), stream)
            return 0
        if arguments.command == "evaluate":
            report = evaluate_project(arguments.project)
            stream.write(report.to_json(indent=2) + "\n")
            return 1 if report.status.value == "fail" else 0
        if arguments.command == "dashboard":
            from .dashboard_adapter import ensure_dashboard
            url = ensure_dashboard(arguments.project, port=arguments.port)
            _print_json({"dashboard_url": url}, stream)
            return 0
        if arguments.command == "suggest":
            text = arguments.text
            if text.startswith("@"):
                text = Path(text[1:]).read_bytes().decode("utf-8")
            snapshot = submit_human_guidance(arguments.project, text)
            _print_json(
                {
                    **snapshot,
                    "status": "pending",
                    "path": str(Path(arguments.project).resolve() / snapshot["relative_path"]),
                },
                stream,
            )
            return 0

        runtime = _open(arguments.project)
        if arguments.command == "resume":
            dashboard_url = _start_dashboard(runtime, arguments)
            result = runtime.run(resume=True, max_cycles=arguments.max_cycles)
            if dashboard_url:
                result["dashboard_url"] = dashboard_url
        elif arguments.command == "guidance":
            runtime.submit_guidance(arguments.request_id, arguments.response)
            result = runtime.status()
        elif arguments.command == "cancel-guidance":
            runtime.cancel_guidance(arguments.request_id)
            result = runtime.status()
        elif arguments.command == "advisor-feedback":
            raw_response = arguments.response
            if raw_response.startswith("@"):
                raw_response = Path(raw_response[1:]).read_text(encoding="utf-8")
            response = json.loads(raw_response)
            if not isinstance(response, Mapping):
                raise ValueError("Advisor feedback must be a JSON object")
            runtime.submit_advisor_feedback(arguments.request_id, response)
            result = runtime.status()
        elif arguments.command == "cancel-sprint":
            result = {
                "sprint_id": arguments.sprint_id,
                "status": runtime.cancel_sprint(
                    arguments.sprint_id,
                    arguments.reason,
                ),
            }
        elif arguments.command == "rebuild-projections":
            memory_paths = runtime.store.rebuild_projections()
            category_paths = runtime.categories.rebuild_projections()
            result = {
                "project": str(runtime.layout.root),
                "memory_projections": len(memory_paths),
                "category_projections": len(category_paths),
            }
        elif arguments.command == "refs":
            result = {
                "references": runtime.list_temporary_references(state=arguments.state)
            }
        elif arguments.command == "ref-status":
            result = runtime.temporary_reference_status(arguments.temporary_id)
        elif arguments.command == "resolve-ref":
            affected = runtime.resolve_temporary_reference(
                arguments.temporary_id,
                arguments.canonical_id,
                resolution=arguments.resolution,
                operation_id=arguments.operation_id,
            )
            result = {
                "reference": runtime.temporary_reference_status(arguments.temporary_id),
                "affected_source_ids": list(affected),
            }
        elif arguments.command == "abandon-ref":
            affected = runtime.abandon_temporary_reference(
                arguments.temporary_id,
                reason=arguments.reason,
                operation_id=arguments.operation_id,
            )
            result = {
                "reference": runtime.temporary_reference_status(arguments.temporary_id),
                "affected_source_ids": list(affected),
            }
        else:  # pragma: no cover - argparse makes this unreachable
            raise AssertionError(arguments.command)
        _print_json(result, stream)
        return 0
    except KeyboardInterrupt:
        print("franta: interrupted; resume with `franta resume PROJECT`", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"franta: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    finally:
        if runtime is not None:
            runtime.close()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = ["main"]
