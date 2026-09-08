# Explorer summary payload contract

Call `record_summary` with one object as `payload`:

```json
{
  "operation_id": "OP-EXPLORER-SUMMARY-1",
  "abstract": "Degeneration analysis reduces the root problem to one uncontrolled boundary term.",
  "content": "The attempt moved from direct dimension estimates to semistable reduction. The normalization sequence appears to be the most promising next step.",
  "directions_tried": ["Direct incidence dimension estimates", "Semistable degeneration and normalization"],
  "main_progress": "The incidence calculation identifies the only term not covered by the existing dimension estimate.",
  "main_obstacles": "No argument yet forces that boundary term to vanish in the singular case.",
  "source_scratch_ids": ["ES-EXPLORER-1", "ES-EXPLORER-2"]
}
```

`abstract`, `content`, `directions_tried`, `main_progress`, `main_obstacles`, and
`source_scratch_ids` are required. `operation_id` is optional.

- `operation_id`, when supplied, is a nonempty staging ID. Omit it to let the runtime generate
  one. Never reuse it within the workspace; duplicate staging is rejected.
- `abstract` is the concise searchable description of the attempt's central result or obstacle.
- `content` is the self-contained high-level synthesis, including useful next steps when any.
- `directions_tried` is a nonempty ordered list of at most 64 direction descriptions. These are
  texts, not direction IDs.
- `main_progress` and `main_obstacles` clearly separate achieved progress from unresolved
  barriers.
- `source_scratch_ids` is a duplicate-free list of already accepted `ES-*` scratch records that
  support the summary. Include all important mathematical sources; directions themselves are
  recorded as text in `directions_tried`. Never invent an ID to make the attempt appear productive.

The caller must not supply a summary ID, root-candidate field, verification status, worker
identity, attempt number, turn, timestamp, or sequence. The service returns the allocated
`ESUM-*` `record_id`. Root-candidate signaling occurs only in the final response and must cite a
trusted `ES-*` `proof` record made before this summary.
