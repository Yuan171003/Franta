---
name: record-scratch
description: Stage an Explorer worker's provisional mathematical ideas and discoveries without publishing or verifying host memory.
---

# Record scratch

Use this skill whenever an Explorer attempt produces an idea, claim, possible route, obligation,
calculation, example, counterexample, obstacle, route change, or other potentially reusable
progress. Record it promptly at its current level of confidence; do not delay the record in order
to verify it, reconcile it with host memory, or turn it into a polished canonical proposal.

Call the launch-bound `record_scratch` tool with one self-contained entry as its `payload`. Follow
the compact field contract in [references/payload.md](references/payload.md). Give the entry a
precise searchable abstract and enough main content for a later Explorer or host sorter to
understand the reasoning without conversational context. Record freely within the launch-bound
per-attempt and per-turn safety limits.

The service, not the worker, allocates the unique `ES-*` record ID and binds trusted provenance.
Scratch is provisional, unverified, append-only, and noncanonical. It never changes a published
fact, route, memo, claim, obligation, or computation. Do not edit a staged scratch artifact or
write directly to an outbox or memory store.

There is no dedicated direction record. Preserve reusable content from a direction or pivot as
an ordinary `route`, `idea`, `progress`, `obstacle`, or other fitting scratch. The attempt-final
summary records the directions tried, main progress, and obstacles.

A purported proof or disproof of the root problem follows a strict ordering boundary: first stage
the complete argument as a `proof` scratch and receive its trusted `ES-*` `record_id`; only then
may the final response cite that exact ID as `root_candidate_scratch_id`. The candidate requests
later host handling; it is not verification and does not complete the root problem.
