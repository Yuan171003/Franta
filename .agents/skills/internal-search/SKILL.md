---
name: internal-search
description: Search authorized Franta memory abstracts and fetch selected full records through the audited broker.
---

# Internal search

Use this skill when authorized project memory may help the current decision.

1. Call `internal_search` with a mathematical query and a nonempty subset of `fact`, `route`,
   `memo`, `claim`, `obligation`, and `computation`; request at most 10 results.
2. Read the returned abstracts, derived computation summaries, and status labels first.
3. Call `memory_fetch` by canonical ID only for records whose full content is needed.

Before proposing a new route or obligation, search for an existing one. Read abstracts first and
fetch a plausible same record only when its full content is needed. Prefer an update when it
already represents the same strategy or statement and the new work adds genuine progress;
otherwise submit no duplicate or empty update.

Among memory records, only active facts are established project truth. Inactive facts and
withdrawn claims are hidden unless historical results are requested; historical records are
visibly marked and are never premises. Other memory types remain exploratory.

Never open a canonical database or memory path from the shell. Search and fetch are audited and
limited by the launch-bound access policy. Isolated calls receive neither tool. A proof-writer
instead calls `fact_dependency_closure`, reads its abstracts, and fetches only needed facts from
that authorized active closure.

Native web search is separate from project-memory search. External material remains
nonauthoritative until precisely cited and checked through the ordinary fact pipeline.
