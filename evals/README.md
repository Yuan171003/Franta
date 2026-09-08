# Franta evaluation harness

`franta.evaluation` is a read-only observer for scheduler snapshots, canonical
memory metadata, store read audits, skill activity, and Codex JSONL tool logs.
It does not launch agents, change state, or repair a failed run.

## Report API

```python
from franta.evaluation import evaluate_files, evaluate_project, evaluate_snapshot

report = evaluate_files("evals/fixtures/complete_observation.json")
print(report.to_json())
print(report.scenario("fact_repair").status.value)
```

The CLI-friendly module entry point prints the same JSON:

```sh
PYTHONPATH=src python -m franta.evaluation \
  --snapshot evals/fixtures/complete_observation.json

PYTHONPATH=src python -m franta.evaluation \
  --project /path/to/project --events /path/to/extra.jsonl
```

The project form opens SQLite with `mode=ro` and `PRAGMA query_only`; it scans
only JSONL audit, private-state, and workspace logs beneath the supplied
project root. A failing scenario produces exit status 1. Pass and inconclusive
reports produce exit status 0 so callers can decide how to handle unavailable
evidence.

Every scenario has one of three statuses:

- `pass`: the supplied observations affirm the behavior;
- `fail`: the observations contain a concrete invariant violation;
- `inconclusive`: the scenario did not occur, the observation window is
  partial, or the decision requires mathematical judgement.

The overall report fails if any scenario fails. Otherwise, it is inconclusive
if any scenario is inconclusive.

## Scenarios

The report checks:

- scheduler-state coherence and configured hard concurrency limits;
- memory counts and required qualitative importance/value signals;
- role-appropriate skill use and required calls in a declared-complete window;
- search/abstract and task-summary ordering before full reads;
- brainstorm, multi-discipline, and sealed sprint-lane access attempts;
- category redraw after an explicit qualitative imbalance judgement;
- verifier rejection, two-request repair, and mandatory concession behavior;
- lost-lease fencing, stale-output rejection, task-attempt continuity, and
  resumed Codex sessions;
- research stopping and optional single proof-writer behavior after root
  resolution.

Memory counts are evidence only. The evaluator deliberately defines no count
quota, fact-to-claim ratio, category size threshold, or forced split. A
`memory_health_review`/`memory_importance_review` observation with a qualitative
verdict is needed for a semantic pass. Similarly, category imbalance must be
declared by a semantic observation; it is never inferred numerically.

Isolation evidence distinguishes brainstorm, multi-discipline, the complete
four-mode discovery-sprint lane set, and its fresh summarizer. Persisted lane
policies must be explicitly sealed, and a persisted summarizer call must match
the sprint's frozen synthesis input exactly.

Progressive disclosure is a `SHOULD`, not a full-read ban. An observed fetch
after a search result, materialized abstract, or explicit need justification
passes. A direct read with no visible precursor is inconclusive unless the log
explicitly marks it unnecessary or emits `progressive_disclosure_violation`.

## Snapshot and live observations

A snapshot is a JSON object. Recognized top-level fields are:

```text
scheduler          durable scheduler state
categories         category control state
memories           composed memory records
read_audit         store search/fetch audit rows
audit_events       canonical-store audit rows
skill_activity     successful skill activity rows
tool_activity      Codex tool activity rows
live_events        additional semantic or runtime observations
observation_scope  log-coverage declarations
```

Set `observation_scope.skills` to `true` only when the supplied window contains
all assignment and attempt activity in the snapshot. Without that declaration,
the evaluator can reject an observed wrong-context skill call, but missing
skill evidence stays inconclusive rather than becoming a false failure.

Audited `task_summary` and `task_artifact_fetch` calls are recognized as the
`task-search` skill and are valid only for main-agent and trimmer calls. Started,
pending, running, or incomplete tool rows remain attempt evidence and never
count as successful calls.

`LiveSessionObservationParser` accepts raw Codex JSONL, transport-wrapped Codex
events, `tool_activity.jsonl`, store read audits, scheduler events, and
`skill_activity.jsonl`. Malformed lines are retained as diagnostics. Parsing is
observational: there is intentionally no auto-fix hook or mutation callback.

The fixture [complete_observation.json](fixtures/complete_observation.json)
shows a complete synthetic observation in which every scenario passes.

## Documented implementation choices

- Missing scenario evidence yields `inconclusive`, not an assumed pass/fail.
- A skill window is complete only by an explicit snapshot declaration.
- Scheduler-configured hard limits and the design's two verifier revision
  requests are checked; no additional numerical rule is introduced.
- A justified direct full read is allowed, preserving agent judgement.
- Category redraw evidence may be create, revise, membership change, merge,
  split, or another explicit redraw/retain rationale; no forced operation type
  is imposed.
- The parser reports failures and malformed observations but never invokes a
  fixer, scheduler action, skill, or model.
