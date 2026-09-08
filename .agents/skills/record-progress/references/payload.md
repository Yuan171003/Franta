# Progress payload contract

Use one JSON object with `sequence`, `is_final`, `outcome_status`, a concise
`progress_since_previous`, `operations`, `computation_operation_ids`, and `fact_challenges`.
The runtime binds the task ID and attempt. Each operation is flat: it contains `kind`, a unique
`operation_id`, and the fields below.

## Add operations

- `fact`: a fresh `candidate_id`, `candidate_version: 1`, a fresh `proposal_id`, `statement`,
  complete `proof`, `predecessor_fact_ids`, `originating_task_id`, `foundation_policy_version`,
  `introduced_notation`, `external_references`, optional `root_resolution`, `abstract`,
  `keywords`, and `related_route_ids`. A worker must never reuse a `candidate_id` in another fact
  operation or progress file, including for a correction or repair. Temporary predecessor
  proposal IDs may appear in `predecessor_fact_ids` and the proof until the scheduler performs
  any internal normalization.
- `route_add`: `proposal_id`, `abstract`, immutable `strategy_description`, qualitative
  `value_assessment`, `progress`, `related_obligation_ids`, `next_steps`, `obstacles`,
  `active_fact_ids`, `relevant_memo_ids`, and `relevant_claim_ids`.
- `memo`: `proposal_id`, `abstract`, `genre` (`high-level` or `normal`), `content`, and
  `related_route_ids`.
- `claim_add`: `proposal_id`, `abstract`, `content`, and `related_route_ids`.
- `obligation_add`: `proposal_id`, `abstract`, self-contained `statement`, qualitative
  `importance`, `predecessor_fact_ids`, `partial_progress`, `related_route_ids`, and `relations`.
  Propose an obligation only when proving or disproving it would seriously affect a route or the
  root problem.

The five qualitative route values are `confidence`, `success_gain`, `failure_gain`, `relevance`,
and `novelty`; each is a short description, never a numeric score. Every abstract names the
principal objects and hypotheses or target, plus the central method or conclusion, in concise
searchable prose. An introduced-notation item has `symbol`, `definition`, and `scope`. An
external reference has `source_type`, `authors`, `title`, `stable_identifier_or_url`, `locator`,
`exact_result`, and `role`. A root resolution is exactly
`{"target":"ROOT","outcome":"proved"}` or `{"target":"ROOT","outcome":"disproved"}`.

An obligation relation contains `relation_type`, `premise_memory_ids`, `conclusion` (an
obligation ID or `ROOT`), `explanation`, and `supporting_fact_ids`. Without supporting active
facts it is conjectural, not a premise.

## Update and remove operations

`route_update` and `obligation_update` contain `target_id`, `expected_base_revision`, `set`,
`append`, `add_ids`, `remove_ids`, `explanation`, and `supporting_memory_ids`. Omitted patch
fields mean unchanged. Route updates cannot change `strategy_description`; obligation updates
cannot change `statement` or `predecessor_fact_ids`.

- Route `set`: `abstract`, `value_assessment`, `progress`, `next_steps`, `obstacles`; `append`:
  `progress`, `next_steps`, `obstacles`; relationship fields: `related_obligation_ids`,
  `active_fact_ids`, `relevant_memo_ids`, `relevant_claim_ids`.
- Obligation `set`: `abstract`, `importance`, `partial_progress`, `relations`; `append`:
  `partial_progress`, `relations`; relationship field: `related_route_ids`.
- `claim_remove`: `target_id`, optional `replacement_id`, and optional `reason`.
- `obligation_remove`: `target_id`, optional `resolving_fact_ids`, `refuting_fact_ids`,
  `replacement_obligation_id`, and `reason`. Never remove the root obligation.

Non-fact relationship fields may cite an unpublished route, memo, claim, or obligation by its
temporary `proposal_id`, including across later progress records. Do not copy that ID into an
abstract, strategy, statement, memo/claim content, explanation, or any other prose or
mathematical field. The source may publish while the typed reference remains visibly pending; it
is not an active premise or reciprocal link. When the target publishes or deduplicates, the
scheduler atomically resolves every pending typed slot to its canonical ID and activates the
links. Cycles among these non-fact links are allowed and do not require one progress file.

If the target is rejected, its pending links remain rejected until a corrected operation using
the same typed temporary ID succeeds, or the target is explicitly abandoned. A task does not
close while its links are pending or rejected, and no timeout abandons them. Only the fact
dependency graph must be acyclic. Facts may cite an uncommitted fact candidate's temporary ID in
their predecessor list and at exact citation points in the proof; normalization creates a new
candidate version. A rejected predecessor returns every dependent candidate without ID
substitution.

## Verifier repair and concession

On a verifier-feedback attempt, follow the immutable attempt supplement, but never reuse the
rejected fact's `candidate_id`. Submit any corrected proof as a new fact with a fresh
`candidate_id`, fresh `proposal_id`, fresh `operation_id`, and `candidate_version: 1`. The
candidate named in the supplement is audit context, not an ID to reuse. After the second
verifier-requested repair is rejected, submit no further corrected fact for that rejected line.
In the final concession attempt, record every smaller surviving result only as a claim and always
add a memo describing the attempted strategy, precise failure, and reusable insight. Close that
attempt as `progress` or `failed` according to the value recovered.

## Challenges and final records

A challenge contains `challenge_id`, `fact_id`, `alleged_failure`, and optional `evidence`. It
requests verifier review; it does not revoke a fact itself.

For a final record, set top-level `completion_evidence_ids` to required operation IDs and include:

```json
{
  "attempt_summary": {
    "work_mode": "research",
    "task": "the assigned objective",
    "proposed_outcome": "finished",
    "cumulative_important_progress": "...",
    "completion_evidence_operation_ids": ["OP-..."],
    "most_promising_next_steps": "..."
  }
}
```

The two evidence lists must match. A normally ending attempt has exactly one final record.
