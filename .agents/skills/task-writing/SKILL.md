---
name: task-writing
description: Serialize one finalized Franta worker assignment without changing its mathematics.
---

# Task writing

Serialize a finalized assignment; do not plan or revise it. The main agent uses this skill
exactly once per assignment after deciding the whole batch. The scheduler uses it once for each
of the four finalized discovery-sprint lane blueprints.

Preserve the supplied objective, mode, selected memories, continuation choice, target, route,
obligations, and perspective exactly. The staging JSON must contain:

- `batch_finalized: true`, the scheduler-issued `batch_id`, and a unique `operation_id` for the
  assignment report;
- a self-contained `objective`, `work_mode`, and `if_resume` (a selected previous task ID, or
  `null`);
- `main_route_ids`, `main_obligation_ids`, and `selected_new_perspective`, using empty lists or
  `null` where they do not apply;
- `assignment_portfolio` with exactly six ID lists: `fact`, `route`, `memo`, `claim`,
  `obligation`, and `computation`;
- a brief `reason`, including deliberate omissions or unusual combinations; and
- `root_solution_fact_id` for proof-writer mode.

Mechanical mode rules: research has exactly one main route; brainstorm has exactly one main
obligation and a fact-only assignment portfolio; multi-discipline has at least one main
obligation, a nonempty seed perspective, and only necessary facts in its portfolio; computation
includes a route or obligation;
proof-writer names the active root-solution fact. Associate and reformulate need no distinguished
route or obligation. The explicit maximum is 20 distinct selected portfolio memories; the
scheduler-added verified dependency closure for proof-writer mode is exempt.

Set `if_resume` when:
- it has accumulated route-specific context plus concrete next gate
- it has a clear plausible plan to provide a breakthrough, and the plan really offers a way to solve, weaken, or escape the main obstacle
- the objective is the same or closely related.. Otherwise use `null`. 
The scheduler permits at most six explicit cross-task resume launches in one lineage; after that it returns
`fresh_start_required`, so restage the assignment with `if_resume: null`.

Call the launch-bound `task_writing` tool with the finalized report as its `payload`.

This stages an intermediate report only. The scheduler validates the finalized batch, allocates
the task ID, snapshots the portfolio, and launches the task. Do not invent a task ID or write
canonical state.
