# `selection_report` payload

Submit one object with these exact fields:

- `selection_report_id`: a stable, unique artifact ID for this Advisor round.
- `feedback_request_id`: a stable, unique ID for the human decision requested by this round.
- `advisor_index`, `source_cycle`, `target_cycle`: exact context integers.
- `original_problem_digest`: exact context digest; never recompute or substitute it.
- `memory_snapshot_id`, `memory_snapshot_digest`: exact authenticated snapshot binding.
- `previous_assignments_digest`: exact context binding for the complete assignment history.
- `report_path`: a confined relative Markdown path under `artifacts/`, such as
  `artifacts/selection-report.md`.
- `human_question`: a clear request that the human choose one or two listed obligations or state
  one or two custom obligations.
- `obligations`: exactly five objects, stored in rank order 1 through 5.

For a repeated obligation, consult the launch-bound context's
`breakthrough_evidence_freshness.records`. A cited record's
`newer_than_advisor_index` must be at least the `advisor_index` of every prior assignment named
by that obligation. Records omitted from this map have not advanced since any prior assignment.

Each obligation object has:

- `obligation_id`: a stable ID unique among the five obligations in this report.
- `rank`: its integer position, with every value 1 through 5 appearing once in order.
- `title`: concise mathematical name.
- `statement`: self-contained and precise problem statement suitable for direct assignment.
- `importance`: why this is important research independently of the original problem.
- `landscape_change`: what resolving it would unlock, rule out, classify, or reduce.
- `relationship_to_root`: its rigorous connection to the original problem without restating it.
- `novelty`: how it differs from the complete prior assignment history.
- `previous_assignment_ids`: empty unless this is a justified repeat.
- `repeat_justification`: `null` unless repeated; otherwise explain both exceptional importance
  and why the cited breakthrough now makes the work significantly easier.
- `breakthrough_evidence_ids`: empty unless repeated; otherwise immutable memory evidence IDs.
  Each cited record must be new or revision-advanced relative to every prior assignment named by
  this obligation; evidence already present at the same revision is rejected as stale.

The five statements and IDs must be distinct. Never place prose outside this object in the tool
call. The tool owns Markdown rendering and returns `selection_report_id`,
`selection_report_digest`, `feedback_request_id`, and `report_path`; copy those values exactly
into the strict proposal response and end the call.
