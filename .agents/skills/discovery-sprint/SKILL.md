---
name: discovery-sprint
description: Let the trimmer plan four isolated approaches when research is stuck on one mechanism.
---

# Discovery sprint

Trimmer only. Use when recent work keeps varying one central mechanism without changing its main
obstacle. This is a qualitative mathematical judgment, not a failure count or stagnation score;
do not infer stuckness from one failure or an unfinished task.

Return `no_sprint` with a concise reason, or plan one sprint around exactly one active
obligation. Read a current `stuck` report when present, category and recent task summaries, then
memory abstracts and full records only as needed. For a plan, follow the compact contract in
[references/plan.md](references/plan.md) and state the exact materials and deliberate omissions
for every lane. Plan only when `discovery_sprint_available` is true and
`configured_non_verifier_slots` is four; otherwise return `no_sprint` instead of persisting an
unlaunchable sprint.

The four lane blueprints must appear in this order:

1. clean-room `brainstorm`: target plus only strictly necessary active facts;
2. representation-shift `multi-discipline`: the same target, necessary active facts, and one
   genuinely untried perspective;
3. sealed `computation` with `CAS`: target plus an explicit computation portfolio;
4. sealed remote-composition `associate`: target plus two to four deliberately distant items.

Every lane has a separate sealed workspace, no `internal-search` or canonical-memory path, and no
access to another lane before the completion barrier. Call the launch-bound `discovery_sprint`
tool with the decision or finalized plan as its `payload`.

The scheduler validates and persists the plan, waits for all four slots, launches all lanes
atomically, enforces the barrier, and gives only the frozen synthesis input to a fresh blind
summarizer. This skill never launches workers or writes canonical memory.
