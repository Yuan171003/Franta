# Advisor subsystem

`advisor_system` is the portable, peer-level block that turns one completed research cycle into
the visible problem assignment for the next Explorer–Franta pair. It owns Advisor value contracts,
the pure replay-safe lifecycle reducer, prompts and response schemas, model settings, the portable
program, and the packaged `selection-report` skill. It does not own scheduler persistence, model
transport, human-feedback delivery, audited memory storage, or downstream phase changes.

The two-call lifecycle is:

1. `proposing`: a host freezes the complete preceding memory view under its
   `main-agent-equivalent` access profile and supplies every previous problem assignment. The
   persistent Advisor proposes exactly five ranked obligations and calls `selection_report`.
   If it exceptionally repeats a prior subproblem, the trusted tool requires each cited
   breakthrough record to be new or revision-advanced since every named assignment snapshot.
2. `waiting_for_human`: the proposal call ends. No Advisor call may resume until the host binds
   structured feedback selecting one or two listed obligations or one or two custom overrides.
3. `ready_to_finalize` / `finalizing`: the host resumes the same project-wide Advisor session. The
   response must reproduce the human choices exactly and in order.
4. `completed`: the reducer validates every context/report/feedback digest and deterministically
   renders an immutable `ProblemAssignment` for research cycle `i + 1` without changing the actual
   original problem.

`AdvisorHost` is the main orchestration port. Hosts implement durable state access, idempotent call
planning/execution, trusted selection-report acceptance, and feedback delivery. `AdvisorMemoryPort`
freezes the authorized memory snapshot; `AdvisorAssignmentSink` is the optional downstream handoff.
The state functions are pure JSON-in/JSON-out transitions, so a host may persist them in any store
and append their returned domain events to its own log.

This package is deliberately dependency-directed: no module in `advisor_system` imports `franta` or
`explorer_system`. Host adapters translate between those systems and these public contracts.
