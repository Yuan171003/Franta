---
name: record-summary
description: Close one Explorer planned attempt with a provisional append-only summary that cites its durable scratch records.
---

# Record summary

Use this skill exactly once before an Explorer planned attempt ends normally. It closes the
attempt's write stream: after a summary is accepted, do not record more scratch, replace the
summary, or change a direction for that attempt. An interrupted attempt may lack a summary and is
handled by the Explorer controller rather than repaired by inventing a normal ending.

Call the launch-bound `record_summary` tool with the attempt summary as its `payload`. Follow
[references/payload.md](references/payload.md). Summarize the directions pursued, main progress,
main obstacles, and useful next steps, and cite the durable `ES-*` scratch records that carry the
important mathematical content. Compress and orient; do not verify scratch, deduplicate it into
host memory, or publish memos, claims, computations, routes, obligations, or facts.

The service allocates the unique `ESUM-*` record ID and binds trusted worker, turn, and attempt
provenance. The summary is provisional, append-only, noncanonical, and searchable by later
authorized attempts.

If the attempt appears to prove or disprove the root problem, first use `record-scratch` for the
complete argument as a `proof` record. Root-candidate signaling belongs only to the final
response, which cites the trusted `ES-*` proof as `root_candidate_scratch_id`; it is not a summary
field.
